"""hermes-fleet CLI: init(환경 초기화) / repos / check / status / backlog / new / approve / run / discover / merge / daemon / pause / resume / scaffold"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from . import requests as rq
from .backlog import TicketError, find_ticket, load_tickets, new_ticket
from .fleet import DaemonOptions, FleetDaemon, ensure_scaffold, load_state
from .llm import check_endpoint
from .org import load_org_for, load_template
from .repos import FleetConfig, RepoProfile, eligible_repos, scan_repos
from .settings import PAUSE_FILE, STATE_DIR, Settings, fleet_config_path
from .tools import git


def _cfg(args: argparse.Namespace) -> FleetConfig:
    return FleetConfig.load(Path(args.config) if getattr(args, "config", None) else fleet_config_path())


def _repo(cfg: FleetConfig, name: str) -> RepoProfile:
    for p in scan_repos(cfg):
        if p.name == name:
            if not p.eligible:
                raise TicketError(f"{name}: 작업 대상이 아님 ({', '.join(p.reasons)})")
            return p
    raise TicketError(f"repo not found under {cfg.root}: {name}")


def cmd_repos(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print(f"root={cfg.root}  active_hours={cfg.active_hours}  merge_at={cfg.merge_at}")
    print(f"{'REPO':<20} {'TARGET':<7} {'FRONTEND':<26} {'BACKEND':<30} {'BASE':<8} REASON")
    for p in scan_repos(cfg):
        if not args.all and not p.eligible:
            continue
        fe = p.frontend.label() if p.frontend else "-"
        be = p.backend.label() if p.backend else "-"
        print(f"{p.name:<20} {'yes' if p.eligible else 'no':<7} {fe:<26} {be:<30} {p.base_branch:<8} {'; '.join(p.reasons)}")
        if args.verbose and p.eligible:
            for cwd, cmd in p.verify_steps():
                print(f"{'':<20} verify: ({cwd}) {' '.join(cmd)}")
            print(f"{'':<20} org: {load_org_for(p)['_source']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    s = Settings.from_env()
    print(f"provider={s.provider} model={s.model} base_url={s.base_url}")
    info = check_endpoint(s)
    if not info["ok"]:
        print(f"✗ endpoint unreachable: {info['url']} ({info['error']})")
        return 1
    print(f"✓ endpoint ok: {info['url']}")
    if info["models"]:
        print("  models: " + ", ".join(info["models"][:20]))
    if not info.get("model_available", True):
        print(f"✗ model '{s.model}' not found on endpoint (ollama pull {s.model})")
        return 1
    try:
        org = load_template()
        missing = sorted({str(r["model"]) for r in org["roles"].values() if r.get("model") and info["models"] and str(r["model"]) not in info["models"]})
        for m in missing:
            print(f"✗ role model '{m}' not found on endpoint (ollama pull {m})")
        if missing:
            return 1
        print("✓ templates/org.yaml ok")
        cfg = _cfg(args)
        repos = eligible_repos(cfg)
        print(f"✓ fleet.yaml ok — {len(repos)} target repo(s): " + ", ".join(p.name for p in repos))
        for p in repos:
            load_org_for(p)
            load_tickets(p.backlog_dir)
    except (TicketError, ValueError, OSError) as exc:
        print(f"✗ {exc}")
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if PAUSE_FILE.exists():
        print(f"⏸ 일시정지 중 ({PAUSE_FILE.read_text(encoding='utf-8').strip()}) → hermes-fleet resume")
    fleet = load_state(STATE_DIR / "fleet.json")
    if fleet.get("last_run"):
        print(f"마지막 실행: {fleet['last_run']}")
    for p in eligible_repos(cfg):
        state = load_state(STATE_DIR / f"{p.name}.json")
        tickets = load_tickets(p.backlog_dir)
        proposed = [t for t in tickets if t.status == "proposed"]
        todo = [t for t in tickets if t.status == "todo" and t.id not in state.get("tickets", {})]
        review = {k: e for k, e in state.get("tickets", {}).items() if e.get("status") == "review"}
        blocked = {k: e for k, e in state.get("tickets", {}).items() if e.get("status") == "blocked"}
        print(f"\n## {p.name}  (FE {p.frontend.dir} · BE {p.backend.dir} · base {p.base_branch} · branch {git.current_branch(p.root)})")
        last = state.get("last_merge")
        if last:
            print(f"  마지막 자동 머지: {last.get('at')} merged={last.get('merged')} pushed={last.get('pushed')}" + (f" issues={last['issues']}" if last.get("issues") else "") + (f" note={last['note']}" if last.get("note") else ""))
        print(f"  승인 대기 (proposed): {len(proposed)}")
        for t in proposed:
            print(f"    {t.id} [{t.priority}] {t.title}   → hermes-fleet approve {p.name} {t.id}")
        print(f"  리뷰 대기 (QA PASS, 자동 머지 예정): {len(review)}")
        for tid, e in review.items():
            print(f"    {tid} on {e.get('branch')}  iterations={e.get('iterations')}  → git -C {p.root} diff {p.base_branch}..{e.get('branch')}")
        print(f"  막힘 (QA FAIL/오류): {len(blocked)}")
        for tid, e in blocked.items():
            print(f"    {tid} on {e.get('branch')}  verdict={e.get('verdict')}  {e.get('error', '')[:120]}")
        print(f"  대기 중 (todo): {len(todo)}")
        for t in todo:
            print(f"    {t.id} [{t.priority}] {t.title}")
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    repos = [_repo(cfg, args.repo)] if args.repo else eligible_repos(cfg)
    print(f"{'REPO':<18} {'ID':<8} {'PRI':<4} {'STATUS':<12} {'AREA':<10} TITLE")
    for p in repos:
        for t in sorted(load_tickets(p.backlog_dir), key=lambda t: (t.priority, t.id)):
            if args.status and t.status != args.status:
                continue
            print(f"{p.name:<18} {t.id:<8} {t.priority:<4} {t.status:<12} {t.area:<10} {t.title}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    p = _repo(_cfg(args), args.repo)
    ensure_scaffold(p)
    path = new_ticket(args.id, args.title, p.backlog_dir)
    print(f"created {path}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    p = _repo(_cfg(args), args.repo)
    ticket = find_ticket(args.ticket, p.backlog_dir)
    if ticket.status != "proposed":
        print(f"✗ {ticket.id} is '{ticket.status}', not 'proposed'")
        return 1
    ticket.set_status("todo")
    print(f"✓ {p.name} {ticket.id} approved → todo ({ticket.path.name})")
    if args.commit:
        print(git.commit_all(f"chore({ticket.id}): approve → todo", p.root))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import Pipeline  # noqa: WPS433

    p = _repo(_cfg(args), args.repo)
    ticket = find_ticket(args.ticket, p.backlog_dir)
    settings = Settings.from_env()
    info = check_endpoint(settings)
    if not info["ok"]:
        print(f"✗ LLM endpoint unreachable: {info['url']} ({info['error']})")
        return 1
    stages = [s.strip() for s in args.stages.split(",") if s.strip()] if args.stages else None
    pipe = Pipeline(p, ticket, settings=settings, stages=stages, max_iterations=args.max_iter)
    try:
        result = pipe.run(create_branch=not args.no_branch, commit=args.commit)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1
    verdict = "PASS" if result.passed else "FAIL" if result.passed is False else "N/A"
    print(f"\n=== {p.name} {ticket.id} finished: verdict={verdict} iterations={result.iterations} ===\nartifacts: {pipe.workspace}")
    return 0 if result.passed is not False else 2


def cmd_discover(args: argparse.Namespace) -> int:
    from .discover import run_discover  # noqa: WPS433

    cfg = _cfg(args)
    p = _repo(cfg, args.repo)
    settings = Settings.from_env()
    info = check_endpoint(settings)
    if not info["ok"]:
        print(f"✗ LLM endpoint unreachable: {info['url']} ({info['error']})")
        return 1
    ensure_scaffold(p)
    created = run_discover(p, settings, max_proposals=args.max or int(cfg.discover.get("max_proposals", 2)))
    print(f"\n{len(created)} proposal(s) written to {p.backlog_dir}. Approve with: hermes-fleet approve {p.name} <ID>")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    from .merge import MergeConfig, auto_merge  # noqa: WPS433

    cfg = _cfg(args)
    repos = [_repo(cfg, args.repo)] if args.repo else eligible_repos(cfg)
    bad = False
    for p in repos:
        report = auto_merge(p, MergeConfig.from_dict(cfg.auto_merge, push_override=False if args.no_push else None), dry_run=args.dry_run)
        for o in report.outcomes:
            print(f"{p.name:<18} {o.result:14s} {o.branch} ({o.ticket_id}) {o.detail.splitlines()[0] if o.detail else ''}")
        if report.note:
            print(f"{p.name:<18} note: {report.note}")
        print(f"{p.name:<18} merged={len(report.merged)} pushed={report.pushed}")
        bad = bad or any(o.result in ("conflict", "verify-failed") for o in report.outcomes)
    return 2 if bad else 0


def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.active_hours is not None:
        cfg.active_hours = args.active_hours or None
    if args.merge_at is not None:
        cfg.merge_at = args.merge_at or None
    opts = DaemonOptions(once=args.once, exit_outside_window=not args.forever, discover=not args.no_discover, max_tickets=args.max_tickets, only=args.only or None,
                         on_demand=args.on_demand)
    return FleetDaemon(cfg, opts).run()


def cmd_init_env(args: argparse.Namespace) -> int:
    """받은 환경에서 처음 실행: 형제 레포 스캔 → fleet.yaml/.env 생성 → LLM 점검 → 다음 단계."""
    from .setup import init_environment, next_steps  # noqa: WPS433

    fleet_dir = Path(args.fleet_dir) if args.fleet_dir else fleet_config_path().parent
    include = [n for chunk in (args.include or []) for n in chunk.split(",") if n.strip()]
    root = Path(args.root).expanduser() if args.root else None
    if not include and not args.yes and sys.stdin.isatty():
        # 대화형: 후보를 보여 주고 고르게 한다
        from .repos import scan_repos as _scan  # noqa: WPS433
        cands = [p for p in _scan(FleetConfig(root=(root or fleet_dir.parent).resolve())) if p.name != fleet_dir.name]
        if cands:
            print("대상 레포를 고르세요 (번호를 쉼표로, 비우면 FE+BE 가 탐지된 레포 전부, 'none' 이면 없음):")
            for i, c in enumerate(cands, 1):
                print(f"  {i:>2}. {'●' if c.eligible else '○'} {c.name:<28} {'; '.join(c.reasons)}")
            try:
                ans = input("> ").strip()
            except EOFError:
                ans = ""
            if ans.lower() == "none":
                args.yes = True; include = []; select_all = False
            elif ans:
                idx = [int(x) for x in re.split(r"[,\s]+", ans) if x.isdigit()]
                include = [cands[i - 1].name for i in idx if 0 < i <= len(cands)]
    select_all = not (locals().get("select_all") is False)
    res = init_environment(fleet_dir, root=root, include=include or None, select_all=select_all, provider=args.provider, model=args.model,
                           base_url=args.base_url, api_key=args.api_key, force=args.force, scaffold=args.scaffold)
    if args.cron:
        import subprocess  # noqa: WPS433

        subprocess.run([str(fleet_dir / "scripts" / "install-cron.sh")], check=False)
    print()
    for n in res.notes:
        print(f"⚠ {n}")
    print("다음 단계:")
    for st in next_steps(res):
        print(f"  {st}")
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    repos = [_repo(cfg, r) for r in args.repo] if args.repo else eligible_repos(cfg)
    for p in repos:
        created = ensure_scaffold(p)
        print(f"{p.name}: {'created ' + ', '.join(str(c.relative_to(p.root)) for c in created) if created else 'already initialised'}")
        if created and args.commit:
            print("  " + git.commit_all("chore(hermes): init fleet backlog scaffold", p.root))
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from .monitor.__main__ import main as monitor_main  # noqa: WPS433

    argv = ["--host", args.host, "--port", str(args.port)] + (["--reload"] if args.reload else [])
    return monitor_main(argv)


def cmd_pause(args: argparse.Namespace) -> int:
    if getattr(args, "repo", None):
        rq.set_repo_paused(STATE_DIR, args.repo, True)
        print(f"✓ {args.repo} paused (이 레포의 새 작업·머지·요청 건너뜀). resume: hermes-fleet resume {args.repo}")
        return 0
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(f"paused at {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
    print("✓ paused (진행 중인 티켓은 마친 뒤 대기). resume: hermes-fleet resume")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    if getattr(args, "repo", None):
        rq.set_repo_paused(STATE_DIR, args.repo, False)
        print(f"✓ {args.repo} resumed")
        return 0
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        print("✓ resumed")
    else:
        print("(not paused)")
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    if args.list:
        for r in rq.list_pending(STATE_DIR):
            print(f"{r['status']:8s} {r['id']}")
        for r in rq.list_done(STATE_DIR, 10):
            print(f"{r['status']:8s} {r['id']}  {r.get('detail', '')[:100]}")
        return 0
    req = rq.create_request(STATE_DIR, args.repo, args.action, args.ticket, source="cli")
    print(f"✓ queued {req['id']} — 데몬(또는 on-demand 러너, 최대 2분)이 처리합니다. 바로 실행: hermes-fleet daemon --on-demand")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes-fleet", description="여러 저장소를 순회하는 Hermes(CrewAI) 가상 조직 하네스")
    p.add_argument("--config", help="fleet.yaml 경로 (기본: hermes-fleet/fleet.yaml 또는 HERMES_FLEET_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("repos", help="작업 대상 레포(FE+BE)와 선정 사유")
    r.add_argument("--all", action="store_true", help="제외된 레포도 표시")
    r.add_argument("-v", "--verbose", action="store_true", help="검증 명령과 org 출처 표시")
    r.set_defaults(func=cmd_repos)
    sub.add_parser("check", help="LLM 엔드포인트/모델/설정/백로그 점검").set_defaults(func=cmd_check)
    sub.add_parser("status", help="레포별 승인 대기/리뷰 대기/막힘/대기 요약").set_defaults(func=cmd_status)

    b = sub.add_parser("backlog", help="티켓 목록")
    b.add_argument("repo", nargs="?")
    b.add_argument("--status", choices=["proposed", "todo", "in-progress", "review", "done", "blocked"])
    b.set_defaults(func=cmd_backlog)

    n = sub.add_parser("new", help="새 티켓 파일 생성")
    n.add_argument("repo"); n.add_argument("id", help="예: HX-010"); n.add_argument("title")
    n.set_defaults(func=cmd_new)

    a = sub.add_parser("approve", help="proposed 티켓을 todo로 승인")
    a.add_argument("repo"); a.add_argument("ticket")
    a.add_argument("--commit", action="store_true", help="승인 상태 변경을 바로 커밋")
    a.set_defaults(func=cmd_approve)

    ru = sub.add_parser("run", help="티켓 파이프라인 수동 실행")
    ru.add_argument("repo"); ru.add_argument("ticket")
    ru.add_argument("--stages", help="예: dev,qa")
    ru.add_argument("--max-iter", type=int, default=None)
    ru.add_argument("--no-branch", action="store_true")
    ru.add_argument("--commit", action="store_true")
    ru.set_defaults(func=cmd_run)

    d = sub.add_parser("discover", help="Tech Lead 발굴(제안 티켓 생성)")
    d.add_argument("repo"); d.add_argument("--max", type=int, default=None)
    d.set_defaults(func=cmd_discover)

    m = sub.add_parser("merge", help="QA PASS(review) 브랜치 머지·검증·푸시")
    m.add_argument("repo", nargs="?")
    m.add_argument("--dry-run", action="store_true"); m.add_argument("--no-push", action="store_true")
    m.set_defaults(func=cmd_merge)

    dm = sub.add_parser("daemon", help="자율 모드: 시간대 안에서 레포 순회")
    dm.add_argument("--once", action="store_true", help="한 사이클만")
    dm.add_argument("--forever", action="store_true", help="시간대 밖에서도 종료하지 않고 대기(systemd 모드). 기본은 시간대 밖이면 종료(cron 모드)")
    dm.add_argument("--no-discover", action="store_true")
    dm.add_argument("--max-tickets", type=int, default=None)
    dm.add_argument("--only", nargs="*", help="특정 레포만")
    dm.add_argument("--active-hours", default=None, help='예: "01:00-08:00" (빈 문자열이면 항상)')
    dm.add_argument("--merge-at", default=None, help='예: "07:30" (빈 문자열이면 안 함)')
    dm.add_argument("--on-demand", action="store_true", help="대기 중인 요청(state/requests)만 처리하고 종료 (시간대 무관, cron 2분 러너)")
    dm.set_defaults(func=cmd_daemon)

    rqp = sub.add_parser("request", help="웹과 같은 요청을 큐에 넣는다: run|discover|merge|approve|retry|stop")
    rqp.add_argument("repo", nargs="?"); rqp.add_argument("action", nargs="?", choices=list(rq.VALID_ACTIONS)); rqp.add_argument("ticket", nargs="?")
    rqp.add_argument("--list", action="store_true")
    rqp.set_defaults(func=cmd_request)

    ie = sub.add_parser("init", help="환경 초기화: 상위 폴더의 레포를 스캔해 fleet.yaml/.env 를 만들고 LLM 을 점검 (받은 직후 1회)")
    ie.add_argument("--root", help="레포들이 있는 폴더 (기본: hermes-fleet 의 상위 폴더)")
    ie.add_argument("--include", action="append", help="대상 레포 이름(쉼표 구분, 반복 가능). 없으면 FE+BE 탐지 레포 전부")
    ie.add_argument("--provider", choices=["ollama", "openai"]); ie.add_argument("--model"); ie.add_argument("--base-url"); ie.add_argument("--api-key")
    ie.add_argument("--force", action="store_true", help="기존 fleet.yaml/.env 를 덮어쓴다")
    ie.add_argument("--scaffold", action="store_true", help="대상 레포에 hermes/ 스캐폴드도 만든다")
    ie.add_argument("--cron", action="store_true", help="끝나면 scripts/install-cron.sh 실행")
    ie.add_argument("--yes", "-y", action="store_true", help="묻지 않고 기본값으로 진행")
    ie.add_argument("--fleet-dir", help=argparse.SUPPRESS)
    ie.set_defaults(func=cmd_init_env)
    i = sub.add_parser("scaffold", help="레포에 hermes/backlog 스캐폴드 생성")
    i.add_argument("repo", nargs="*"); i.add_argument("--commit", action="store_true")
    i.set_defaults(func=cmd_scaffold)
    mo = sub.add_parser("monitor", help="웹 모니터(읽기 전용) 실행 — uv sync --extra monitor 또는 docker compose up")
    mo.add_argument("--host", default="127.0.0.1"); mo.add_argument("--port", type=int, default=8787)
    mo.add_argument("--reload", action="store_true")
    mo.set_defaults(func=cmd_monitor)
    pa = sub.add_parser("pause", help="데몬 일시정지 (레포를 주면 그 레포만)")
    pa.add_argument("repo", nargs="?"); pa.set_defaults(func=cmd_pause)
    re_ = sub.add_parser("resume", help="데몬 재개 (레포를 주면 그 레포만)")
    re_.add_argument("repo", nargs="?"); re_.set_defaults(func=cmd_resume)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config) if getattr(args, "config", None) else fleet_config_path()
    if args.command != "init" and not cfg_path.exists():
        print(f"✗ 설정이 없습니다: {cfg_path}\n  이 환경에서 처음이면 먼저 초기화하세요:  scripts/init.sh   (또는 uv run hermes-fleet init)", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except TicketError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
