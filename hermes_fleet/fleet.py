"""Fleet 데몬: 작동 시간대(기본 01:00-08:00) 안에서 대상 레포를 라운드 로빈으로 순회한다.

한 사이클(tick):
  1. PAUSE 파일 → 대기. 시간대 밖 → 종료 신호(cron 모드) 또는 대기.
  2. 레포별 하루 한 번 자동 머지(merge_at 이후, LLM 불필요).
  3. 종료 45분 전부터는 새 작업을 시작하지 않는다.
  4. LLM 엔드포인트 확인 → 커서 다음 레포부터 todo 티켓이 있는 첫 레포를 처리(브랜치 hermes/<ID>, 커밋).
  5. todo 가 모두 비면 발굴(Tech Lead)을 커서 다음 레포부터 한 곳 실행해 proposed 티켓을 커밋.
레포 전제: 기준 브랜치에 있고 작업 트리가 깨끗할 것. 다른 브랜치에 있으면 건드리지 않고 건너뛴다(사람이 작업 중).
"""
from __future__ import annotations

import datetime as dt
import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import requests as rq
from .backlog import Ticket, find_ticket, load_tickets
from .discover import run_discover          # 데몬 시작 시점에 미리 적재 (hermes-fleet 자신이 대상일 때 브랜치 전환 중 지연 import 를 피한다)
from .enhancements import plan_enhancement, update_enhancement
from .projects import bootstrap as bootstrap_project, update as update_project
from .llm import check_endpoint
from .merge import MergeConfig, MergeReport, auto_merge
from .org import load_org_for
from .pipeline import Pipeline, PipelineAborted
from .repos import FleetConfig, RepoProfile, eligible_repos
from .routing import ModelRouter, RouteDecision
from .schedule import Schedule, parse_clock
from .settings import PAUSE_FILE, STATE_DIR, Settings
from .tools import git
from .workflow import run_deploy

Logger = Callable[[str], None]
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SCAFFOLD_GITIGNORE = "workspace/*/logs/\nworkspace/discover/\nworkspace/daemon.log\nworkspace/PAUSE\n.venv/\n__pycache__/\n"
SCAFFOLD_README = """# Backlog

Hermes Fleet(상위 폴더 하네스)이 처리하는 작업 티켓입니다. 파일 하나가 티켓 하나이며 YAML frontmatter + 마크다운 본문 형식입니다.

- 상태 전이: proposed →(사람 `hermes-fleet approve <repo> <ID>`)→ todo →(데몬)→ in-progress →(QA PASS)→ review →(일일 자동 머지)→ done
- QA FAIL 또는 오류 시 blocked (사람이 티켓을 고쳐 todo로 되돌림)
- 산출물: hermes/workspace/<ID>/ (prd.md, design.md, dev-report.md, qa-report.md, summary.md)
"""


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"tickets": {}}


def save_state(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_next(tickets: list[Ticket], state: dict[str, Any]) -> Ticket | None:
    handled = set(state.get("tickets", {}))
    todo = [t for t in tickets if t.status == "todo" and t.id not in handled]
    todo.sort(key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), t.id))
    return todo[0] if todo else None


def rotate(items: list, cursor: str | None) -> list:
    """cursor(레포 이름) 다음 항목부터 시작하도록 회전."""
    names = [p.name for p in items]
    if cursor in names:
        i = names.index(cursor) + 1
        return items[i:] + items[:i]
    return items


def ensure_scaffold(profile: RepoProfile) -> list[Path]:
    """레포에 hermes/backlog, hermes/.gitignore, workspace 가 없으면 만든다(다음 커밋에 포함)."""
    created = []
    hermes = profile.root / "hermes"
    for path, content in ((hermes / ".gitignore", SCAFFOLD_GITIGNORE), (hermes / "backlog" / "README.md", SCAFFOLD_README), (hermes / "workspace" / ".gitkeep", "")):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            created.append(path)
    return created


@dataclass
class DaemonOptions:
    once: bool = False
    exit_outside_window: bool = True     # cron 모드: 시간대 밖이면 종료
    discover: bool = True
    max_tickets: int | None = None
    only: list[str] | None = None        # 특정 레포만
    on_demand: bool = False              # 대기 중인 요청(state/requests)만 처리하고 종료. 시간대 무관. cron 2분 간격 러너용


class FleetDaemon:
    def __init__(self, cfg: FleetConfig, options: DaemonOptions, settings: Settings | None = None, state_dir: Path = STATE_DIR,
                 pause_file: Path = PAUSE_FILE, log: Logger = print, sleep: Callable[[float], None] = time.sleep,
                 repos: Callable[[FleetConfig], list[RepoProfile]] = eligible_repos, now: Callable[[], dt.datetime] = dt.datetime.now) -> None:
        self.cfg = cfg
        self.options = options
        self.settings = settings or Settings.from_env()
        self.router = ModelRouter.from_env(self.settings)
        self._work_settings = self.settings
        self.state_dir = state_dir
        self.pause_file = pause_file
        self.log = lambda msg: log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")
        self.sleep = sleep
        self.repos = repos
        self.now = now
        self.schedule = Schedule(cfg.active_hours, cfg.calendar)
        self.merge_time = parse_clock(cfg.merge_at)
        self.processed = 0
        self.merge_cfg = MergeConfig.from_dict(cfg.auto_merge)
        self._paused_logged: set[str] = set()

    # ---------- 상태 ----------
    def fleet_state_path(self) -> Path:
        return self.state_dir / "fleet.json"

    def repo_state_path(self, name: str) -> Path:
        return self.state_dir / f"{name}.json"

    # ---------- 메인 루프 ----------
    def run(self) -> int:
        if self.options.on_demand:
            return self._run_on_demand()
        if self._idle_outside_window():
            return 0  # cron 모드: 시간대 밖이고 할 일(요청)도 없으면 로그 없이 종료 — cron 이 하루 종일 10분마다 깨우므로 조용해야 한다
        self.log(f"[fleet] start root={self.cfg.root} hours={self.cfg.active_hours} merge_at={self.cfg.merge_at} routing={self.router.mode}")
        while True:
            try:
                outcome = self.tick()
            except KeyboardInterrupt:
                self.log("[fleet] interrupted")
                return 0
            except Exception as exc:  # noqa: BLE001
                self.log(f"[fleet] unexpected error: {exc}\n{traceback.format_exc()}")
                outcome = "wait"
            if outcome == "stop":
                self.log("[fleet] outside active hours; exiting (cron will start again in the window)")
                return 0
            if self.options.once:
                return 0
            if self.options.max_tickets and self.processed >= self.options.max_tickets:
                self.log(f"[fleet] processed {self.processed} tickets, stopping")
                return 0
            if outcome == "wait":
                self.sleep(self.cfg.poll_seconds)

    def _idle_outside_window(self) -> bool:
        """cron 모드(exit_outside_window)에서 지금 시작할 이유가 없는가: 시간대(fleet.yaml active_hours) 밖이고 대기 요청도 없음.

        cron 줄은 시간을 고정하지 않고(`*/10 * * * *`) 하루 종일 깨우며, 시간대 판정은 전부 fleet.yaml 의 active_hours 로 한다.
        그래서 웹 모니터에서 스케줄을 바꾸면 cron 을 다시 등록하지 않아도 다음 10분 틱부터 그대로 적용된다."""
        if not self.options.exit_outside_window:
            return False
        self._refresh_config()
        if rq.next_pending(self.state_dir) is not None:
            return False
        return not self.schedule.is_active(self.now())

    def _run_on_demand(self) -> int:
        """웹/CLI 요청만 처리하고 종료. 요청이 없으면 아무 로그도 남기지 않는다(2분마다 돌아도 조용하도록)."""
        if self.pause_file.exists() or rq.next_pending(self.state_dir) is None:
            return 0
        self.log(f"[fleet] on-demand start root={self.cfg.root} routing={self.router.mode}")
        while True:
            try:
                outcome = self.tick()
            except KeyboardInterrupt:
                outcome = "stop"
            except Exception as exc:  # noqa: BLE001
                self.log(f"[fleet] unexpected error: {exc}\n{traceback.format_exc()}")
                outcome = "stop"
            if outcome != "work":
                break
        self.log("[fleet] on-demand done; exiting")
        return 0

    def tick(self) -> str:
        """'work' | 'wait' | 'stop'"""
        self._refresh_config()
        if self.pause_file.exists():
            if self.options.on_demand:
                return "stop"
            if self.options.exit_outside_window and not self.schedule.is_active(self.now()):
                return "stop"  # 일시정지 + 시간대 밖: 살아서 폴링할 이유가 없다
            self.log(f"[fleet] paused ({self.pause_file}); run `hermes-fleet resume`")
            return "wait"
        now = self.now()

        # 0) 웹/CLI 요청 — 시간대와 무관하게 먼저 처리
        req = rq.next_pending(self.state_dir)
        if req:
            self._handle_request(req)
            return "work"
        if self.options.on_demand:
            return "stop"

        if not self.schedule.is_active(now):
            return "stop" if self.options.exit_outside_window else "wait"

        profiles = self._targets()
        if not profiles:
            self.log("[fleet] no eligible repos (need both frontend and backend); waiting")
            return "wait"

        # 1) 레포별 하루 한 번 자동 머지
        for p in profiles:
            if self._merge_due(p, now):
                self._run_merge(p)
                return "work"

        if not self.schedule.can_start_work(now, int(self.cfg.no_new_work_minutes_before_end)):
            self.log(f"[fleet] within {self.cfg.no_new_work_minutes_before_end} min of window end; not starting new work")
            return "wait"

        if not self._prepare_work_model():
            self.sleep(self.cfg.endpoint_retry_seconds)
            return "wait"

        fleet_state = load_state(self.fleet_state_path())
        ordered = rotate(profiles, fleet_state.get("cursor"))

        # 2) todo 티켓이 있는 첫 레포 (approval=auto 레포는 proposed 를 먼저 자동 승인)
        for p in ordered:
            if not self._ready(p):
                continue
            self._auto_approve(p)
            state = load_state(self.repo_state_path(p.name))
            ticket = pick_next(load_tickets(p.backlog_dir), state)
            if ticket:
                self._run_ticket(p, ticket, state)
                self._advance(fleet_state, p)
                return "work"

        # 3) 발굴 — 새 제안이 없으면 다음 레포로 넘어가고, 모두 비면 대기한다. 같은 레포는 cooldown 동안 다시 발굴하지 않는다
        #    (예전엔 빈 발굴도 'work' 로 돌아와 레포들을 쉼 없이 돌며 LLM 을 계속 호출했다).
        if self.options.discover:
            for p in ordered:
                if self._ready(p) and self._should_discover(p):
                    created = self._run_discover(p)
                    self._advance(fleet_state, p)
                    if created:
                        return "work"

        self.log("[fleet] nothing to do in any repo (waiting for approvals/merges)")
        return "wait"

    # ---------- 레포 선택/전제 ----------
    def _refresh_config(self) -> None:
        changed = self.cfg.refresh()
        if not changed:
            return
        self.schedule = Schedule(self.cfg.active_hours, self.cfg.calendar)
        self.merge_time = parse_clock(self.cfg.merge_at)
        self.merge_cfg = MergeConfig.from_dict(self.cfg.auto_merge)
        self.log(f"[fleet] fleet.yaml changed: {', '.join(changed)} (hours={self.cfg.active_hours} calendar={len(self.cfg.calendar)}일 merge_at={self.cfg.merge_at} include={self.cfg.include} exclude={self.cfg.exclude})")

    def _targets(self) -> list[RepoProfile]:
        self._refresh_config()
        profiles = self.repos(self.cfg)
        if self.options.only:
            profiles = [p for p in profiles if p.name in self.options.only]
        active = []
        for p in profiles:
            if rq.is_repo_paused(self.state_dir, p.name):
                if p.name not in self._paused_logged:
                    self.log(f"[fleet][{p.name}] paused (state/paused/{p.name}); skipping until resumed")
                    self._paused_logged.add(p.name)
                continue
            self._paused_logged.discard(p.name)
            active.append(p)
        return active

    # ---------- 요청(웹/CLI) ----------
    def _handle_request(self, req: dict[str, Any]) -> None:
        repo, action, ticket_id = req["repo"], req["action"], req.get("ticket")
        rq.mark_running(self.state_dir, req)
        self.log(f"[fleet][{repo}] ▶ request {action}{' ' + ticket_id if ticket_id else ''} ({req['id']})")
        try:
            status, detail = self._execute_request(repo, action, ticket_id)
        except Exception as exc:  # noqa: BLE001
            status, detail = "failed", str(exc)
            if action == "enhance" and ticket_id:
                try:
                    update_enhancement(self.state_dir, ticket_id, status="failed", error=str(exc))
                except Exception:  # noqa: BLE001 — 원래 요청 오류를 덮어쓰지 않는다
                    pass
            if action == "bootstrap" and ticket_id:
                try:
                    update_project(self.state_dir, ticket_id, status="failed", error=str(exc))
                except Exception:  # noqa: BLE001
                    pass
            self.log(f"[fleet][{repo}] request {action} error: {exc}\n{traceback.format_exc()}")
        rq.complete(self.state_dir, req, status, detail)
        self.log(f"[fleet][{repo}] ■ request {action}: {status} — {detail[:200]}")

    def _execute_request(self, repo: str, action: str, ticket_id: str | None) -> tuple[str, str]:
        if action == "stop":
            rq.set_repo_paused(self.state_dir, repo, True, "stopped from request")
            return "done", "nothing running; repo paused (resume to continue)"
        if action == "bootstrap":
            project = bootstrap_project(self.state_dir, self.cfg.root, self.cfg.source or Path("fleet.yaml"), ticket_id or "")
            self.cfg.refresh()
            p = next((x for x in self.repos(self.cfg) if x.name == project["name"]), None)
            if p is None:
                raise ValueError("생성한 프로젝트 프로필을 fleet에서 찾지 못했습니다")
            state = load_state(self.repo_state_path(p.name))
            ticket = find_ticket(project["ticket"], p.backlog_dir)
            if not self._prepare_work_model():
                return "failed", "선택한 LLM 엔드포인트에 연결할 수 없습니다"
            self._run_ticket(p, ticket, state)
            return "done", f"{project['name']} 생성·비공개 GitHub 구성·{ticket.id} 실행 완료"
        p = next((x for x in self.repos(self.cfg) if x.name == repo), None)
        if p is None:
            return "failed", "not a fleet target (FE/BE 미확인이거나 선택되지 않음)"
        if rq.is_repo_paused(self.state_dir, repo):
            return "skipped", "repo is paused; resume first"
        if not self._ready(p):
            return "skipped", f"repo not on {p.base_branch} or working tree dirty; left for a human"
        if action == "deploy":
            if not p.deploy.get("command"):
                return "skipped", "fleet.yaml repos.<repo>.deploy.command 가 없습니다"
            self.log(f"[fleet][{p.name}] ▶ deploy (request)")
            ok, detail = run_deploy(p.root, p.deploy, log=self.log, timeout=self.merge_cfg.verify_timeout)
            state = load_state(self.repo_state_path(p.name))
            state["last_deploy"] = {"ok": ok, "detail": detail, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "target": p.deploy.get("target"), "source": "request"}
            save_state(state, self.repo_state_path(p.name))
            self.log(f"[fleet][{p.name}] ■ deploy: {'ok' if ok else 'failed'}")
            return ("done" if ok else "failed"), detail.splitlines()[0] if detail else ""
        if action == "enhance":
            if not self._prepare_work_model():
                return "failed", "선택한 LLM 엔드포인트에 연결할 수 없습니다"
            created = plan_enhancement(p, self.state_dir, ticket_id or "", settings=self._work_settings, log=self.log)
            if created or not git.is_clean(p.root):
                names = ", ".join("-".join(path.stem.split("-")[:2]) for path in created) or "project artifacts"
                git.commit_all(f"hermes(enhancement): plan {ticket_id} ({names})\n\nCustomer brief → proposed tickets; approval required before implementation.", p.root)
            return "done", f"{ticket_id}: {len(created)} proposal(s) created; approve tickets to implement"
        if action == "merge":
            report = self._run_merge(p)
            return "done", f"merged={[o.ticket_id for o in report.merged]} pushed={report.pushed}" + (f" note={report.note}" if report.note else "")
        if action in ("approve", "retry"):
            ticket = find_ticket(ticket_id, p.backlog_dir)
            state = load_state(self.repo_state_path(p.name))
            if action == "approve":
                if ticket.status != "proposed":
                    return "skipped", f"{ticket.id} status={ticket.status} (needs proposed)"
                ticket.set_status("todo")
                git.commit_all(f"chore({ticket.id}): approve → todo (request)", p.root)
                return "done", f"{ticket.id} approved → todo"
            removed = state.get("tickets", {}).pop(ticket.id, None)
            if ticket.status in ("blocked", "in-progress", "review"):
                ticket.set_status("todo")
                git.commit_all(f"chore({ticket.id}): retry → todo (request)", p.root)
            elif ticket.status != "todo":
                return "skipped", f"{ticket.id} status={ticket.status} (needs blocked/todo)"
            save_state(state, self.repo_state_path(p.name))
            return "done", f"{ticket.id} reset → todo" + (" (previous run record cleared)" if removed else "")
        if not self._prepare_work_model():
            return "failed", "선택한 LLM 엔드포인트에 연결할 수 없습니다"
        if action == "discover":
            ensure_scaffold(p)
            created = self._run_discover(p)
            return "done", f"discover finished: {len(created)} proposal(s)"
        state = load_state(self.repo_state_path(p.name))
        if ticket_id:
            ticket = find_ticket(ticket_id, p.backlog_dir)
            if ticket.status != "todo":
                return "skipped", f"{ticket.id} status={ticket.status} (needs todo; approve/retry first)"
            state.get("tickets", {}).pop(ticket.id, None)   # 재실행 허용
        else:
            ticket = pick_next(load_tickets(p.backlog_dir), state)
            if ticket is None:
                return "skipped", "no todo tickets (approve a proposal first)"
        fleet_state = load_state(self.fleet_state_path())
        self._run_ticket(p, ticket, state)
        self._advance(fleet_state, p)
        entry = state.get("tickets", {}).get(ticket.id, {})
        ok = entry.get("verdict") in ("PASS", "FAIL")
        return ("done" if ok else "failed"), f"{ticket.id} → {entry.get('status')} (QA {entry.get('verdict')}, {entry.get('seconds')}s)" + (f": {entry.get('error')}" if entry.get("error") else "")

    def _abort_requested(self, repo: str) -> bool:
        """진행 중인 티켓의 단계 경계에서 호출. stop 요청이 있으면 그 요청을 끝내고 레포를 일시정지한 뒤 True."""
        req = rq.pending_for(self.state_dir, repo, "stop")
        if not req:
            return False
        rq.complete(self.state_dir, req, "done", "running ticket aborted at stage boundary; repo paused")
        rq.set_repo_paused(self.state_dir, repo, True, "stopped from request")
        return True

    def _ready(self, p: RepoProfile) -> bool:
        branch = git.current_branch(p.root)
        if branch != p.base_branch:
            if branch.startswith("hermes/") and git.is_clean(p.root):
                git.checkout(p.base_branch, p.root)   # 우리가 만든 브랜치에서 돌아온다
            else:
                self.log(f"[fleet][{p.name}] on branch '{branch}' (not {p.base_branch}); leaving it for a human")
                return False
        if not git.is_clean(p.root):
            self.log(f"[fleet][{p.name}] working tree has uncommitted changes; skipping")
            return False
        return True

    def _advance(self, fleet_state: dict[str, Any], p: RepoProfile) -> None:
        fleet_state["cursor"] = p.name
        fleet_state["last_run"] = {"repo": p.name, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        save_state(fleet_state, self.fleet_state_path())

    def _prepare_work_model(self) -> bool:
        """새 생성 작업 직전에 경로를 선택하고 선택된 엔드포인트를 확인한다."""
        decision: RouteDecision = self.router.choose(check_endpoint)
        info = check_endpoint(decision.settings) if decision.route != "local" else {"ok": True}
        if not info.get("ok"):
            self.log(f"[fleet] {decision.route} LLM endpoint unreachable ({info.get('error', 'unknown error')}); retry in {self.cfg.endpoint_retry_seconds}s")
            return False
        self._work_settings = decision.settings
        fleet_state = load_state(self.fleet_state_path())
        fleet_state["last_llm_route"] = {**decision.as_dict(), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        save_state(fleet_state, self.fleet_state_path())
        self.log(f"[fleet] LLM route={decision.route} model={decision.settings.litellm_model} @ {decision.settings.base_url} — {decision.reason}")
        return True

    # ---------- 머지 ----------
    def _merge_due(self, p: RepoProfile, now: dt.datetime) -> bool:
        if self.merge_time is None or now.time() < self.merge_time:
            return False
        state = load_state(self.repo_state_path(p.name))
        return state.get("last_merge_date") != now.date().isoformat()

    def _run_merge(self, p: RepoProfile) -> MergeReport:
        state = load_state(self.repo_state_path(p.name))
        state["last_merge_date"] = self.now().date().isoformat()
        if git.current_branch(p.root).startswith("hermes/") and git.is_clean(p.root):
            git.checkout(p.base_branch, p.root)
        self.log(f"[fleet][{p.name}] ▶ daily merge")
        report = auto_merge(p, self.merge_cfg, log=self.log)
        state["last_merge"] = {"at": report.started_at, "merged": [o.ticket_id for o in report.merged], "pushed": report.pushed, "note": report.note,
                               "issues": [f"{o.ticket_id}:{o.result}" for o in report.outcomes if o.result in ("conflict", "verify-failed")]}
        if getattr(report, "deploy", None):
            state["last_deploy"] = {**report.deploy, "source": "merge"}
        save_state(state, self.repo_state_path(p.name))
        self.log(f"[fleet][{p.name}] ■ daily merge: merged={len(report.merged)} pushed={report.pushed}")
        return report

    # ---------- 티켓 ----------
    def _run_ticket(self, p: RepoProfile, ticket: Ticket, state: dict[str, Any]) -> None:
        self.log(f"[fleet][{p.name}] ▶ {ticket.id} {ticket.title} [{ticket.priority}]")
        started = time.time()
        entry: dict[str, Any] = {"branch": f"hermes/{ticket.id}", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        ensure_scaffold(p)
        pipe = Pipeline(p, ticket, settings=self._work_settings, org=load_org_for(p), log=self.log, should_abort=lambda: self._abort_requested(p.name))
        try:
            result = pipe.run(create_branch=True, commit=False)
            verdict = "PASS" if result.passed else "FAIL" if result.passed is False else "N/A"
            ticket.set_status("review" if result.passed else "blocked")
            entry.update(verdict=verdict, iterations=result.iterations, status=ticket.status)
            msg = f"hermes({ticket.id}): {ticket.title}\n\nQA verdict: {verdict} (iterations={result.iterations})\nartifacts: hermes/workspace/{ticket.id}/"
        except PipelineAborted as exc:
            self.log(f"[fleet][{p.name}] ⏹ {ticket.id} aborted: {exc}")
            entry.update(verdict="ABORTED", error=str(exc), status="blocked")
            try:
                ticket.set_status("blocked")
            except Exception:  # noqa: BLE001
                pass
            msg = f"hermes({ticket.id}): partial work (aborted by stop request)\n\n{exc}"
        except Exception as exc:  # noqa: BLE001
            self.log(f"[fleet][{p.name}] ✗ {ticket.id} failed: {exc}")
            entry.update(verdict="ERROR", error=str(exc), status="blocked")
            try:
                ticket.set_status("blocked")
            except Exception:  # noqa: BLE001
                pass
            msg = f"hermes({ticket.id}): partial work (pipeline error)\n\n{exc}"
        commit = git.commit_all(msg + "\n\nCo-Authored-By: Hermes Fleet <hermes@local>", p.root)
        entry.update(commit=commit, seconds=round(time.time() - started), finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        state.setdefault("tickets", {})[ticket.id] = entry
        save_state(state, self.repo_state_path(p.name))
        self.processed += 1
        self.log(f"[fleet][{p.name}] ■ {ticket.id} → {entry.get('status')} on {entry['branch']} ({entry['seconds']}s)")
        if git.is_clean(p.root):
            git.checkout(p.base_branch, p.root)

    # ---------- 승인 모드 ----------
    def _auto_approve(self, p: RepoProfile) -> int:
        """approval=auto 인 레포의 proposed 티켓을 todo 로 바꾸고 커밋한다. 바꾼 수를 돌려준다."""
        if p.approval != "auto":
            return 0
        try:
            # 장문 요구사항 기반 프로젝트 티켓은 레포의 기본 auto 모드와 무관하게
            # 사람 검토가 선행되어야 한다. 원문 범위가 큰 작업을 발견 티켓처럼
            # 즉시 실행하면 의도하지 않은 대규모 변경으로 이어질 수 있다.
            tickets = [t for t in load_tickets(p.backlog_dir)
                       if t.status == "proposed" and "## 기능 강화 프로젝트" not in t.body]
        except Exception:  # noqa: BLE001 — 깨진 티켓 파일은 티켓 처리 단계에서 보고된다
            return 0
        if not tickets:
            return 0
        for t in tickets:
            t.set_status("todo")
        ids = ", ".join(t.id for t in tickets)
        git.commit_all(f"hermes(approve): auto-approve {len(tickets)} tickets ({ids})\n\napproval=auto — 사람 승인 없이 진행", p.root)
        self.log(f"[fleet][{p.name}] approval=auto: auto-approved {len(tickets)} proposed → todo ({ids})")
        return len(tickets)

    # ---------- 발굴 ----------
    def _should_discover(self, p: RepoProfile) -> bool:
        limit = int(self.cfg.discover.get("max_open_proposed", 4))
        waiting = ("proposed",) if p.approval == "manual" else ("proposed", "todo")   # auto 모드는 아직 손대지 않은 todo 도 '쌓인 것'으로 센다
        open_tickets = [t for t in load_tickets(p.backlog_dir) if t.status in waiting]
        if len(open_tickets) >= limit:
            self.log(f"[fleet][{p.name}] {len(open_tickets)} {'proposals await approval' if p.approval == 'manual' else 'tickets wait in backlog'} (limit {limit}); skipping discover")
            return False
        cooldown = int(self.cfg.discover.get("cooldown_minutes", 180))
        last = load_state(self.repo_state_path(p.name)).get("last_discover_at")
        if cooldown > 0 and last:
            try:
                elapsed = (self.now() - dt.datetime.fromisoformat(last)).total_seconds() / 60
            except ValueError:
                elapsed = float("inf")
            if elapsed < cooldown:
                return False   # 조용히 건너뜀 (10분마다 로그가 쌓이지 않도록)
        return True

    def _run_discover(self, p: RepoProfile) -> list[Path]:
        self.log(f"[fleet][{p.name}] ▶ discover (Tech Lead)")
        state = load_state(self.repo_state_path(p.name))
        state["last_discover_at"] = self.now().strftime("%Y-%m-%dT%H:%M:%S")
        save_state(state, self.repo_state_path(p.name))
        ensure_scaffold(p)
        auto = p.approval == "auto"
        created = run_discover(p, self._work_settings, max_proposals=int(self.cfg.discover.get("max_proposals", 2)), log=self.log, status="todo" if auto else "proposed")
        if created or not git.is_clean(p.root):
            names = ", ".join("-".join(c.stem.split("-")[:2]) for c in created) or "scaffold"
            note = "status: todo — approval=auto, 사람 승인 없이 진행" if auto else f"status: proposed — 사람 승인 필요 (hermes-fleet approve {p.name} <ID>)"
            git.commit_all(f"hermes(discover): propose {len(created)} tickets ({names})\n\n{note}", p.root)
            if auto and created:
                self.log(f"[fleet][{p.name}] approval=auto: {len(created)} tickets go straight to todo")
            self.log(f"[fleet][{p.name}] ■ proposed {len(created)} tickets")
        else:
            self.log(f"[fleet][{p.name}] ■ discover produced no new tickets")
        return created
