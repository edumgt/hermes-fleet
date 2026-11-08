#!/usr/bin/env python3
"""웹 모니터 미리보기용 가짜 fleet 을 만든다 (LLM·실제 레포 불필요).

  python3 scripts/demo_fleet.py /tmp/hermes-fleet-demo
  → /tmp/hermes-fleet-demo/root/{shop,blog,ops-portal,notes}  (git 저장소, FE+BE, hermes/backlog·workspace·hermes/* 브랜치)
    /tmp/hermes-fleet-demo/fleet/{fleet.yaml,state/,logs/daemon.log}

scripts/monitor.sh demo 가 이 스크립트를 호출해 별도 컨테이너(포트 8790)로 띄운다.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

random.seed(7)
NOW = dt.datetime.now().replace(microsecond=0)


def g(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def commit(root: Path, msg: str) -> None:
    g(root, "add", "-A")
    if subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False).stdout.strip():
        g(root, "commit", "-q", "-m", msg)


def ticket_md(tid: str, title: str, priority: str, status: str, area: str, body: str) -> str:
    return (f"---\nid: {tid}\ntitle: {title}\npriority: {priority}\nstatus: {status}\narea: {area}\nstages: [pm, design, dev, qa]\n"
            f"acceptance:\n  - 테스트가 통과한다\n  - 기존 화면 스타일을 따른다\n---\n## 배경\n\n{body}\n\n## 범위\n\n- 구현\n- 테스트 추가\n")


def make_repo(root: Path, name: str, be_dir: str) -> Path:
    r = root / name
    r.mkdir(parents=True)
    g(r, "init", "-q", "-b", "main")
    g(r, "config", "user.email", "demo@local"); g(r, "config", "user.name", "demo")
    (r / "frontend").mkdir(); (r / "frontend" / "index.html").write_text("<html><body>hi</body></html>\n")
    (r / be_dir).mkdir(parents=True)
    (r / be_dir / "requirements.txt").write_text("fastapi\n"); (r / be_dir / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (r / be_dir / "tests").mkdir(); (r / be_dir / "tests" / "test_main.py").write_text("def test_add():\n    assert 1 + 1 == 2\n")
    (r / "hermes").mkdir(); (r / "hermes" / ".gitignore").write_text("workspace/*/logs/\nworkspace/discover/\n")
    g(r, "remote", "add", "origin", f"https://github.com/demo-org/{name}.git")
    wf = r / ".github" / "workflows"; wf.mkdir(parents=True)
    if name in ("shop", "ops-portal"):
        wf.joinpath("cd.yml").write_text("name: CD — Deploy to EC2\non:\n  push:\n    branches: [main]\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: aws-actions/configure-aws-credentials@v4\n      - uses: appleboy/ssh-action@v1\n")
    if name in ("shop", "blog"):
        wf.joinpath("ci.yml").write_text("name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n")
    if name == "blog":
        (r / "deploy.sh").write_text("aws s3 sync frontend/ s3://blog-site --delete\naws cloudfront create-invalidation --distribution-id E123 --paths '/*'\n")
    (r / "hermes" / "backlog").mkdir(); (r / "hermes" / "backlog" / "README.md").write_text("# Backlog\n")
    commit(r, "initial")
    return r


def process_ticket(r: Path, tid: str, title: str, verdict: str, finished: dt.datetime, iterations: int, state: dict, log: list[str], seconds_by_stage: dict[str, float]) -> None:
    """hermes/<ID> 브랜치에 산출물과 review/blocked 상태를 커밋하고 state·log 를 채운다."""
    g(r, "checkout", "-q", "-b", f"hermes/{tid}")
    ws = r / "hermes" / "workspace" / tid
    (ws / "logs").mkdir(parents=True)
    total = sum(seconds_by_stage.values())
    started = finished - dt.timedelta(seconds=total + 30)
    t = started
    events = []
    log.append(f"{started:%Y-%m-%d %H:%M:%S} [fleet][{r.name}] ▶ {tid} {title} [P1]")
    for stage, secs in seconds_by_stage.items():
        it = 1 if stage in ("pm", "design") else iterations
        for i in range(1, it + 1):
            s = secs / it
            t += dt.timedelta(seconds=s)
            out = f"hermes/workspace/{tid}/{ {'pm': 'prd', 'design': 'design', 'dev': 'dev-report', 'qa': 'qa-report'}[stage] }{'' if i == 1 else f'.iter{i}'}.md"
            log.append(f"{t - dt.timedelta(seconds=s):%Y-%m-%d %H:%M:%S} [{r.name}][{stage}] iteration {i} → {out}")
            (r / out).write_text(f"# {stage} {tid} (iteration {i})\n\n내용…\n" + (f"\nVERDICT: {'PASS' if verdict == 'PASS' and i == it else 'FAIL'}\n" if stage == "qa" else ""))
            events.append({"ts": t.strftime("%Y-%m-%dT%H:%M:%S"), "stage": stage, "iteration": i, "seconds": round(s, 1), "output": out, "chars": random.randint(1500, 6000)})
            if stage == "qa":
                log.append(f"{t:%Y-%m-%d %H:%M:%S} [qa] iteration {i} verdict: {'PASS' if verdict == 'PASS' and i == it else 'FAIL'}")
    (ws / "logs" / "run.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
    (ws / "summary.md").write_text(f"# Run Summary: {tid} {title}\n\n- verdict: {verdict}\n")
    (r / "frontend" / f"{tid.lower()}.js").write_text("export const x = 1\n")
    status = "review" if verdict == "PASS" else "blocked"
    for p in (r / "hermes" / "backlog").glob(f"{tid}-*.md"):
        p.write_text(p.read_text().replace("status: todo", f"status: {status}"))
    commit(r, f"hermes({tid}): {title}\n\nQA verdict: {verdict} (iterations={iterations})")
    sha = g(r, "rev-parse", "--short", "HEAD").strip()
    g(r, "checkout", "-q", "main")
    state["tickets"][tid] = {"branch": f"hermes/{tid}", "started_at": started.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": verdict, "iterations": iterations,
                             "status": status, "commit": f"[hermes/{tid} {sha}] hermes({tid}): {title}", "seconds": int(total), "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%S")}
    log.append(f"{finished:%Y-%m-%d %H:%M:%S} [fleet][{r.name}] ■ {tid} → {status} on hermes/{tid} ({int(total)}s)")


def main(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    root = out / "root"
    fleet = out / "fleet"
    (fleet / "state").mkdir(parents=True); (fleet / "logs").mkdir()
    (fleet / "templates").mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "templates" / "org.yaml", fleet / "templates" / "org.yaml")
    (fleet / "fleet.yaml").write_text(f"""# 데모 fleet 설정
root: {root}
active_hours: "01:00-08:00"
no_new_work_minutes_before_end: 45
merge_at: "07:30"
poll_seconds: 600
include: []
exclude:
  - notes          # 메모용 레포 — 작업 대상 아님
repos:
  blog:
    deploy: {{target: aws-s3-cloudfront, trigger: fleet, command: "echo 'aws s3 sync … (demo)'", note: "push 뒤 fleet 이 S3 동기화 + CloudFront 무효화"}}
discover:
  max_proposals: 2
  max_open_proposed: 4
auto_merge:
  verify: true
  require_verification: true
  push: true
""")
    log: list[str] = []
    day = (NOW - dt.timedelta(days=3)).replace(hour=1, minute=0, second=2)
    log.append(f"{day:%Y-%m-%d %H:%M:%S} [fleet] start root={root} hours=01:00-08:00 merge_at=07:30 model=ollama/hermes3:8b @ http://172.29.32.1:11434")

    specs = {
        "shop": ("backend", [("HX-001", "장바구니 수량 변경 시 합계 재계산", "PASS", 1, {"pm": 300, "design": 240, "dev": 1500, "qa": 420}),
                             ("HX-002", "주문 API 입력 검증 추가", "PASS", 2, {"pm": 280, "dev": 2400, "qa": 900}),
                             ("HX-003", "결제 실패 시 재시도 안내", "FAIL", 2, {"pm": 260, "design": 300, "dev": 2600, "qa": 800})]),
        "blog": ("server", [("HX-001", "댓글 목록 페이지네이션", "PASS", 1, {"pm": 200, "design": 180, "dev": 1100, "qa": 350}),
                            ("HX-002", "게시글 검색 인덱스 테스트", "ERROR", 1, {"pm": 220, "dev": 400})]),
        "ops-portal": ("api", [("HX-001", "헬스체크 엔드포인트 타임아웃 처리", "PASS", 1, {"pm": 250, "dev": 1300, "qa": 500})]),
    }
    merged_days = {"shop": ["HX-001"], "blog": ["HX-001"], "ops-portal": []}
    for i, (name, (be, tickets)) in enumerate(specs.items()):
        r = make_repo(root, name, be)
        for tid, title, *_ in tickets:
            (r / "hermes" / "backlog" / f"{tid}-{tid.lower()}.md").write_text(ticket_md(tid, title, "P1", "todo", "fullstack", "근거: 코드 리뷰에서 발견."))
        (r / "hermes" / "backlog" / "HX-010-proposal.md").write_text(ticket_md("HX-010", f"{name} 에러 로깅 표준화 (Tech Lead 제안)", "P2", "proposed", "backend", "Tech Lead 에이전트 제안."))
        (r / "hermes" / "backlog" / "HX-011-todo.md").write_text(ticket_md("HX-011", f"{name} 접근성 라벨 보강", "P2", "todo", "frontend", "승인됨."))
        commit(r, "hermes(discover): propose tickets")
        state: dict = {"tickets": {}}
        when = NOW - dt.timedelta(days=3, hours=-1 - i)
        for n, (tid, title, verdict, iters, stages) in enumerate(tickets):
            process_ticket(r, tid, title, verdict, when + dt.timedelta(days=n, minutes=40 + 25 * i), iters, state, log, stages)
        # 머지 기록 (merge-report.md + state)
        report = []
        for d in range(2, 0, -1):
            at = (NOW - dt.timedelta(days=d)).replace(hour=7, minute=30, second=1 + i)
            lines = [f"## {at:%Y-%m-%d %H:%M:%S}", ""]
            merged = merged_days[name] if d == 2 else []
            for tid in merged:
                lines.append(f"- merged         hermes/{tid} ({tid}) demo")
                g(r, "merge", "-q", "--no-ff", "--no-edit", "-m", f"merge({tid}): demo", f"hermes/{tid}")
                for p in (r / "hermes" / "backlog").glob(f"{tid}-*.md"):
                    p.write_text(p.read_text().replace("status: review", "status: done"))
                commit(r, f"chore({tid}): status done")
                g(r, "branch", "-q", "-d", f"hermes/{tid}")
            if name == "shop" and d == 1:
                lines.append("- conflict       hermes/HX-002 (HX-002) CONFLICT (content): Merge conflict in backend/main.py")
            lines.append(f"- pushed: {'True' if merged else 'False'}")
            report.append("\n".join(lines) + "\n\n")
            log.append(f"{at:%Y-%m-%d %H:%M:%S} [fleet][{name}] ▶ daily merge")
            log.append(f"{at + dt.timedelta(seconds=40):%Y-%m-%d %H:%M:%S} [fleet][{name}] ■ daily merge: merged={len(merged)} pushed={bool(merged)}")
            state["last_merge_date"] = at.date().isoformat()
            state["last_merge"] = {"at": at.strftime("%Y-%m-%d %H:%M:%S"), "merged": merged, "pushed": bool(merged), "note": "", "issues": ["HX-002:conflict"] if name == "shop" and d == 1 else []}
            for tid in merged:
                state["tickets"][tid]["status"] = "done"
        (r / "hermes" / "workspace").mkdir(exist_ok=True)
        (r / "hermes" / "workspace" / "merge-report.md").write_text("".join(report))
        (fleet / "state" / f"{name}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))

    notes = make_repo(root, "notes", "app")   # exclude 된 레포 (FE/BE 는 있음)
    (notes / "hermes" / "backlog" / "HX-001-x.md").write_text(ticket_md("HX-001", "메모 정리", "P3", "todo", "docs", "-")); commit(notes, "t")
    tools = root / "cli-tools"; tools.mkdir(); g(tools, "init", "-q", "-b", "main"); (tools / "main.py").write_text("print(1)\n"); g(tools, "config", "user.email", "d@l"); g(tools, "config", "user.name", "d"); commit(tools, "i")

    (fleet / "state" / "fleet.json").write_text(json.dumps({"cursor": "blog", "last_run": {"repo": "blog", "at": (NOW - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")}}))
    log.sort(key=lambda l: l[:19])
    tail = NOW - dt.timedelta(minutes=3)
    log += [f"{tail - dt.timedelta(minutes=20):%Y-%m-%d %H:%M:%S} [fleet] start root={root} hours=01:00-08:00 merge_at=07:30 model=ollama/hermes3:8b @ http://172.29.32.1:11434",
            f"{tail - dt.timedelta(minutes=19):%Y-%m-%d %H:%M:%S} [fleet][shop] ▶ HX-011 shop 접근성 라벨 보강 [P2]",
            f"{tail - dt.timedelta(minutes=18):%Y-%m-%d %H:%M:%S} [shop][pm] iteration 1 → hermes/workspace/HX-011/prd.md",
            "🚀 Crew: crew", "└── 📋 Task: 1 · Status: Executing Task...",
            f"{tail - dt.timedelta(minutes=12):%Y-%m-%d %H:%M:%S} [shop][design] iteration 1 → hermes/workspace/HX-011/design.md",
            f"{tail:%Y-%m-%d %H:%M:%S} [shop][dev] iteration 1 → hermes/workspace/HX-011/dev-report.md"]
    (fleet / "logs" / "daemon.log").write_text("\n".join(log) + "\n")
    (fleet / "state" / "daemon.lock").write_text("")
    # 제어 상태 예시: 일시정지된 레포, 대기 중/처리된 요청
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from hermes_fleet import requests as rq  # noqa: E402
    rq.set_repo_paused(fleet / "state", "ops-portal", True, "paused from web")
    rq.create_request(fleet / "state", "shop", "run")
    rq.create_request(fleet / "state", "blog", "discover")
    rq.complete(fleet / "state", rq.create_request(fleet / "state", "blog", "merge"), "done", "merged=['HX-001'] pushed=True")
    rq.complete(fleet / "state", rq.create_request(fleet / "state", "shop", "approve", "HX-010"), "failed", "LLM endpoint unreachable (timed out)")
    rq.complete(fleet / "state", rq.create_request(fleet / "state", "ops-portal", "run"), "skipped", "repo is paused; resume first")
    print(f"✓ demo fleet: {fleet}  (root: {root})")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hermes-fleet-demo").resolve())
