"""모니터 데이터 수집. fleet.yaml·state/·logs/·각 레포의 hermes/backlog·workspace·git 을 읽어 JSON 직렬화 가능한 dict 로 만든다.

쓰기는 레포 선택/해제(fleet.yaml 의 include/exclude 편집) 하나뿐이다.
데몬 실행 여부는 (1) state/daemon.lock 의 flock 이 잡혀 있는지, (2) daemon.log 의 시작/종료 마커와 최근 갱신 시각으로 판정한다.
Docker Desktop(별도 VM) 안에서는 호스트의 flock 이 보이지 않으므로 '잠금 획득 실패'만 신뢰하고 나머지는 로그로 판단한다.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..backlog import VALID_STATUS, Ticket, TicketError, load_tickets
from ..fleetfile import FleetFileError, is_selected, set_repo_selected
from ..llm import check_endpoint
from ..merge import list_ticket_branches, ticket_on_branch
from ..org import load_org_for, load_template
from ..repos import FleetConfig, RepoProfile, detect_profile
from ..schedule import can_start_work, is_active, minutes_until_end, parse_clock, parse_window
from ..settings import FLEET_DIR, REPO_WORKSPACE_DIR, Settings, load_dotenv
from ..tools import git

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
LOG_REPO = re.compile(r"\[(?:fleet|merge)\]\[([^\]]+)\]|^\[([^\]]+)\]\[(?:pm|design|dev|qa)\]")
LOG_TICKET_START = re.compile(r"\[fleet\]\[([^\]]+)\] ▶ ([A-Za-z]+-\d+) (.*?) \[(P\d)\]$")
LOG_STAGE = re.compile(r"^\[([^\]]+)\]\[(pm|design|dev|qa)\] iteration (\d+) → (.*)$")
MERGE_SECTION = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$")
MERGE_OUTCOME = re.compile(r"^- (merged|conflict|verify-failed|skipped|error)\s+(\S+) \(([^)]+)\)\s*(.*)$")
START_MARK = "[fleet] start "
STOP_MARKS = ("[fleet] outside active hours; exiting", "[fleet] interrupted", "tickets, stopping")
ARTIFACT_SUFFIXES = {".md", ".jsonl", ".txt", ".json", ".log"}
STAGES = ("pm", "design", "dev", "qa")
TAIL_BYTES = 2 * 1024 * 1024


# ---------- 경로 ----------

@dataclass
class MonitorPaths:
    fleet_dir: Path
    config: Path

    @property
    def state_dir(self) -> Path:
        return self.fleet_dir / "state"

    @property
    def log_dir(self) -> Path:
        return self.fleet_dir / "logs"

    @property
    def pause_file(self) -> Path:
        return self.state_dir / "PAUSE"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "daemon.lock"

    @property
    def daemon_log(self) -> Path:
        return self.log_dir / "daemon.log"

    @property
    def fleet_state(self) -> Path:
        return self.state_dir / "fleet.json"

    @property
    def template_org(self) -> Path:
        mounted = self.fleet_dir / "templates" / "org.yaml"
        return mounted if mounted.exists() else FLEET_DIR / "templates" / "org.yaml"

    def config_writable(self) -> bool:
        return os.access(self.config, os.W_OK)

    @classmethod
    def from_env(cls) -> "MonitorPaths":
        fleet_dir = Path(os.environ.get("HERMES_FLEET_DIR", "").strip() or FLEET_DIR).expanduser()
        load_dotenv(fleet_dir / ".env")
        env_cfg = os.environ.get("HERMES_FLEET_CONFIG", "").strip()
        return cls(fleet_dir=fleet_dir, config=Path(env_cfg).expanduser() if env_cfg else fleet_dir / "fleet.yaml")


@dataclass
class ScannedRepo:
    profile: RepoProfile
    excluded: bool
    in_include: bool

    @property
    def selected(self) -> bool:
        return not self.excluded and self.in_include

    @property
    def target(self) -> bool:
        return self.selected and self.profile.eligible


# ---------- 공용 헬퍼 ----------

def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tail_lines(path: Path, max_lines: int, max_bytes: int = TAIL_BYTES) -> tuple[list[str], bool]:
    """파일 끝 max_bytes 안에서 마지막 max_lines 줄. (lines, truncated)"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return [], False
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # 잘린 첫 줄 제거
    return lines[-max_lines:], len(lines) > max_lines or start > 0


def parse_log_line(line: str) -> dict[str, Any]:
    m = LOG_TS.match(line)
    ts, msg = (m.group(1), m.group(2)) if m else (None, line)
    repo = None
    rm = LOG_REPO.search(msg)
    if rm:
        repo = rm.group(1) or rm.group(2)
    low = msg.lower()
    if "✗" in msg or "error" in low or "failed" in low or "unreachable" in low:
        level = "error"
    elif "▶" in msg:
        level = "start"
    elif "■" in msg or "✓" in msg:
        level = "done"
    elif "paused" in msg or "not starting new work" in msg or "waiting" in msg:
        level = "wait"
    else:
        level = "info"
    return {"ts": ts, "repo": repo, "msg": msg, "level": level, "raw": line}


def parse_merge_report(text: str | None) -> list[dict[str, Any]]:
    """workspace/merge-report.md → [{at, outcomes:[{result, branch, ticket_id, detail}], pushed, note}] (최신순)."""
    runs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        m = MERGE_SECTION.match(line)
        if m:
            cur = {"at": m.group(1), "outcomes": [], "pushed": False, "note": ""}
            runs.append(cur)
            continue
        if cur is None:
            continue
        om = MERGE_OUTCOME.match(line)
        if om:
            cur["outcomes"].append({"result": om.group(1), "branch": om.group(2), "ticket_id": om.group(3), "detail": om.group(4)})
        elif line.startswith("- pushed:"):
            cur["pushed"] = line.split(":", 1)[1].strip().lower() == "true"
        elif line.startswith("- note:"):
            cur["note"] = line.split(":", 1)[1].strip()
    for r in runs:
        r["merged"] = [o["ticket_id"] for o in r["outcomes"] if o["result"] == "merged"]
        r["issues"] = [f"{o['ticket_id']}:{o['result']}" for o in r["outcomes"] if o["result"] in ("conflict", "verify-failed")]
    return list(reversed(runs))


def probe_lock(path: Path) -> bool | None:
    """True = 잠금이 잡혀 있음(데몬 실행 중). False = 잡을 수 있음. None = 파일 없음/확인 불가."""
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except (BlockingIOError, PermissionError):
        return True
    except OSError:
        return None
    finally:
        os.close(fd)


def _git(args: list[str], root: Path, timeout: int = 30) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, check=False).stdout  # noqa: S603,S607
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S") if ts else None


def _day(ts: str | None) -> str | None:
    return ts[:10] if ts and len(ts) >= 10 else None


def _ticket_dict(t: Ticket, effective_status: str | None = None, source: str = "file") -> dict[str, Any]:
    return {"id": t.id, "title": t.title, "priority": t.priority, "status": effective_status or t.status, "file_status": t.status,
            "status_source": source, "area": t.area, "stages": t.stages, "acceptance": t.acceptance,
            "file": t.path.name if t.path else None}


def read_run_events(workspace: Path, ticket_id: str) -> list[dict[str, Any]]:
    path = workspace / ticket_id / "logs" / "run.jsonl"
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return events[-200:]


def stage_seconds(events: list[dict[str, Any]]) -> dict[str, float]:
    out = {s: 0.0 for s in STAGES}
    for e in events:
        if e.get("stage") in out:
            out[e["stage"]] += float(e.get("seconds") or 0)
    return {k: round(v, 1) for k, v in out.items()}


# ---------- 수집기 ----------

class Collector:
    def __init__(self, paths: MonitorPaths, now: Callable[[], dt.datetime] = dt.datetime.now, llm_cache_seconds: float = 30.0) -> None:
        self.paths = paths
        self.now = now
        self.llm_cache_seconds = llm_cache_seconds
        self._llm_cache: tuple[float, dict[str, Any]] | None = None
        self.started_at = time.time()

    @classmethod
    def from_env(cls) -> "Collector":
        return cls(MonitorPaths.from_env())

    # ----- 설정 -----
    def config(self) -> FleetConfig:
        return FleetConfig.load(self.paths.config)

    def settings(self) -> Settings:
        return Settings.from_env()

    # ----- 스케줄 -----
    def schedule(self, cfg: FleetConfig, now: dt.datetime) -> dict[str, Any]:
        window = parse_window(cfg.active_hours)
        merge_time = parse_clock(cfg.merge_at)
        active = is_active(now.time(), window)
        margin = int(cfg.no_new_work_minutes_before_end)
        next_start = None
        if window and not active:
            cand = dt.datetime.combine(now.date(), window[0])
            if cand <= now:
                cand += dt.timedelta(days=1)
            next_start = cand.strftime("%Y-%m-%dT%H:%M:%S")
        remain = minutes_until_end(now.time(), window)
        return {
            "active_hours": cfg.active_hours, "merge_at": cfg.merge_at, "in_window": active,
            "can_start_work": can_start_work(now.time(), window, margin),
            "minutes_until_end": None if remain == float("inf") else round(remain),
            "no_new_work_minutes_before_end": margin, "poll_seconds": int(cfg.poll_seconds),
            "next_window_start": next_start,
            "merge_window_open": bool(merge_time and active and now.time() >= merge_time),
        }

    # ----- 데몬 -----
    def daemon(self, cfg: FleetConfig, now: dt.datetime) -> dict[str, Any]:
        log = self.paths.daemon_log
        lines, _ = tail_lines(log, 400)
        mtime = log.stat().st_mtime if log.exists() else None
        last_start = last_stop = -1
        start_ts = None
        for i, line in enumerate(lines):
            if START_MARK in line:
                last_start = i
                m = LOG_TS.match(line)
                start_ts = m.group(1) if m else None
            if any(m in line for m in STOP_MARKS):
                last_stop = i
        lock_held = probe_lock(self.paths.lock_file)
        poll = int(cfg.poll_seconds)
        stale_after = max(3 * poll, 2700)
        age = (time.time() - mtime) if mtime else None
        fresh = age is not None and age <= stale_after
        paused = self.paths.pause_file.exists()

        if lock_held is True:
            status, method = "running", "flock"
        elif last_start > last_stop and fresh:
            status, method = "running", "log"
        elif last_start > last_stop:
            status, method = "stale", "log"
        else:
            status, method = "stopped", "log"

        activity = self._current_activity(lines, status == "running")
        in_window = is_active(now.time(), parse_window(cfg.active_hours))
        return {
            "status": status, "method": method, "paused": paused,
            "pause_note": self.paths.pause_file.read_text(encoding="utf-8").strip() if paused else None,
            "lock_held": lock_held, "log_exists": log.exists(), "log_size": log.stat().st_size if log.exists() else 0,
            "log_mtime": _iso(mtime), "log_age_seconds": round(age) if age is not None else None,
            "stale_after_seconds": stale_after, "started_at": start_ts.replace(" ", "T") if start_ts else None,
            "expected_running": in_window and not paused,
            "activity": activity,
        }

    def _current_activity(self, lines: list[str], running: bool) -> dict[str, Any] | None:
        """마지막 '▶' 이후 '■/✗' 가 없으면 진행 중인 작업으로 본다."""
        current: dict[str, Any] | None = None
        for line in lines:
            p = parse_log_line(line)
            msg = p["msg"]
            if "▶" in msg:
                m = LOG_TICKET_START.search(msg)
                if m:
                    current = {"kind": "ticket", "repo": m.group(1), "id": m.group(2), "title": m.group(3), "priority": m.group(4), "since": p["ts"], "stage": None}
                elif "daily merge" in msg:
                    current = {"kind": "merge", "repo": p["repo"], "since": p["ts"], "stage": None}
                elif "discover" in msg:
                    current = {"kind": "discover", "repo": p["repo"], "since": p["ts"], "stage": None}
            elif ("■" in msg or "✗" in msg) and "[fleet]" in msg and current:
                current = None
            elif current:
                sm = LOG_STAGE.match(msg)
                if sm:
                    current["stage"] = {"name": sm.group(2), "iteration": int(sm.group(3)), "output": sm.group(4), "since": p["ts"]}
        if current and not running:
            current["note"] = "데몬이 실행 중이 아닌 것으로 보여 미완료로 끝났을 수 있음"
        return current

    # ----- LLM -----
    def llm(self) -> dict[str, Any]:
        if self._llm_cache and time.time() - self._llm_cache[0] < self.llm_cache_seconds:
            return self._llm_cache[1]
        s = self.settings()
        info = check_endpoint(s, timeout=3.0)
        role_models: dict[str, str] = {"(default)": s.model}
        try:
            org = load_template(self.paths.template_org)
            for name, role in org["roles"].items():
                if role.get("model"):
                    role_models[name] = str(role["model"])
        except (OSError, ValueError, KeyError):
            pass
        available = set(info.get("models") or [])
        missing = sorted({m for m in role_models.values() if available and m not in available})
        out = {"provider": s.provider, "model": s.model, "base_url": s.base_url, "url": info.get("url"), "ok": bool(info.get("ok")),
               "error": info.get("error"), "models": info.get("models") or [], "role_models": role_models, "missing_models": missing,
               "checked_at": self.now().strftime("%Y-%m-%dT%H:%M:%S")}
        self._llm_cache = (time.time(), out)
        return out

    # ----- 레포 스캔 (제외/비포함 레포도 프로파일링) -----
    def scan(self, cfg: FleetConfig | None = None) -> list[ScannedRepo]:
        cfg = cfg or self.config()
        out: list[ScannedRepo] = []
        if not cfg.root.is_dir():
            return out
        include = [str(x) for x in (cfg.include or [])]
        exclude = [str(x) for x in (cfg.exclude or [])]
        for child in sorted(cfg.root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith(".") or not git.is_repo(child):
                continue
            profile = detect_profile(child, (cfg.repos or {}).get(child.name))
            out.append(ScannedRepo(profile=profile, excluded=child.name in exclude, in_include=not include or child.name in include))
        return out

    def find(self, name: str) -> ScannedRepo | None:
        return next((s for s in self.scan() if s.profile.name == name), None)

    def _repo_state(self, name: str) -> dict[str, Any]:
        return read_json(self.paths.state_dir / f"{name}.json")

    def _tickets(self, p: RepoProfile, state: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        try:
            tickets = load_tickets(p.backlog_dir)
        except TicketError as exc:
            return [], str(exc)
        handled = state.get("tickets", {})
        out = []
        for t in tickets:
            entry = handled.get(t.id)
            if entry and t.status in ("todo", "in-progress") and entry.get("status") in VALID_STATUS:
                out.append(_ticket_dict(t, entry["status"], "state"))
            else:
                out.append(_ticket_dict(t))
        return out, None

    def _history(self, p: RepoProfile, state: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for tid, e in state.get("tickets", {}).items():
            ev = read_run_events(p.workspace_dir, tid)
            rows.append({"id": tid, **e, "stage_seconds": stage_seconds(ev), "stage_events": len(ev)})
        rows.sort(key=lambda r: r.get("finished_at") or r.get("started_at") or "", reverse=True)
        return rows

    def _repo_summary(self, s: ScannedRepo, detail: bool = False) -> dict[str, Any]:
        p = s.profile
        reasons = list(p.reasons)
        if s.excluded:
            reasons.insert(0, "exclude 목록")
        elif not s.in_include:
            reasons.insert(0, "include 목록에 없음")
        base: dict[str, Any] = {"name": p.name, "root": str(p.root), "eligible": s.target, "fe_be_ok": p.eligible, "selected": s.selected,
                                "excluded": s.excluded, "in_include": s.in_include, "reasons": reasons,
                                "frontend": p.frontend.label() if p.frontend else None, "backend": p.backend.label() if p.backend else None,
                                "base_branch": p.base_branch, "has_remote": p.has_remote}
        if not p.eligible:
            return base
        state = self._repo_state(p.name)
        tickets, error = self._tickets(p, state)
        counts = {st: 0 for st in VALID_STATUS}
        for t in tickets:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        branch = git.current_branch(p.root)
        branches = [{"branch": b, "ticket_id": tid} for b, tid in list_ticket_branches(p.root)]
        log1 = _git(["log", "-1", "--format=%h%x09%ci%x09%s"], p.root).strip().split("\t")
        last_commit = {"hash": log1[0], "date": log1[1], "subject": log1[2]} if len(log1) == 3 else None
        processed = state.get("tickets", {})
        verdicts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
        total_seconds = 0
        for e in processed.values():
            verdicts[e.get("verdict") or "ERROR"] = verdicts.get(e.get("verdict") or "ERROR", 0) + 1
            total_seconds += int(e.get("seconds") or 0)
        last_ticket = None
        if processed:
            tid, e = max(processed.items(), key=lambda kv: kv[1].get("finished_at") or kv[1].get("started_at") or "")
            last_ticket = {"id": tid, **e}
        base.update({
            "current_branch": branch, "on_base": branch == p.base_branch, "clean": git.is_clean(p.root),
            "has_verification": p.has_verification(),
            "verify_steps": [{"cwd": cwd, "cmd": " ".join(cmd)} for cwd, cmd in p.verify_steps()],
            "org_source": self._org_source(p), "counts": counts, "ticket_count": len(tickets), "backlog_error": error,
            "hermes_branches": branches, "last_commit": last_commit, "last_merge": state.get("last_merge"),
            "last_merge_date": state.get("last_merge_date"), "processed_count": len(processed), "verdicts": verdicts,
            "total_seconds": total_seconds, "last_ticket": last_ticket,
            "backlog_dir": str(p.backlog_dir), "scaffolded": p.backlog_dir.is_dir(),
        })
        if detail:
            base["tickets"] = sorted(tickets, key=lambda t: (t["priority"], t["id"]))
            base["history"] = self._history(p, state)
            report = self._read_text(p.workspace_dir / "merge-report.md", 60_000)
            base["merge_report"] = report
            base["merge_history"] = parse_merge_report(report)
            disc = p.workspace_dir / "discover"
            base["discover_runs"] = sorted((f.name for f in disc.glob("*.md")), reverse=True)[:20] if disc.is_dir() else []
            base["log_lines"] = self.log_tail(200, repo=p.name)["lines"]
            base["daily"] = self._daily_for([base])
        return base

    def _org_source(self, p: RepoProfile) -> str | None:
        try:
            return load_org_for(p, self.paths.template_org)["_source"]
        except (OSError, ValueError, KeyError):
            return None

    @staticmethod
    def _read_text(path: Path, limit: int) -> str | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return text if len(text) <= limit else text[-limit:]

    def repos(self, include_ineligible: bool = True) -> list[dict[str, Any]]:
        return [self._repo_summary(s) for s in self.scan() if include_ineligible or s.target]

    def repo_detail(self, name: str) -> dict[str, Any] | None:
        s = self.find(name)
        return self._repo_summary(s, detail=True) if s else None

    def set_selected(self, name: str, selected: bool) -> dict[str, Any]:
        if self.find(name) is None:
            raise FleetFileError(f"root 아래에 없는 레포: {name}")
        if not self.paths.config_writable():
            raise PermissionError(f"fleet.yaml 에 쓸 수 없습니다 (읽기 전용 마운트?): {self.paths.config}")
        result = set_repo_selected(self.paths.config, name, selected)
        cfg = self.config()
        result["now_selected"] = is_selected(name, cfg.include, cfg.exclude)
        return result

    # ----- 티켓 -----
    def ticket_detail(self, repo: str, ticket_id: str) -> dict[str, Any] | None:
        s = self.find(repo)
        if not s or not s.profile.eligible:
            return None
        p = s.profile
        state = self._repo_state(p.name)
        try:
            ticket = next((t for t in load_tickets(p.backlog_dir) if t.id.lower() == ticket_id.lower()), None)
        except TicketError:
            ticket = None
        branch = f"hermes/{ticket_id.upper()}"
        branch_exists = any(b == branch for b, _ in list_ticket_branches(p.root))
        on_branch = ticket_on_branch(p.root, branch, ticket_id) if branch_exists else None
        if ticket is None and on_branch is None:
            return None
        src = ticket or on_branch
        entry = state.get("tickets", {}).get(src.id)
        effective = entry["status"] if entry and src.status in ("todo", "in-progress") else src.status
        out = _ticket_dict(src, effective, "state" if entry and effective != src.status else "file")
        events = read_run_events(p.workspace_dir, src.id)
        out.update({
            "repo": p.name, "body": src.body, "path": str(src.path) if src.path else None,
            "branch": branch if branch_exists else None, "branch_status": on_branch.status if on_branch else None,
            "state": entry, "artifacts": self._artifacts(p, src.id, branch if branch_exists else None),
            "events": events, "stage_seconds": stage_seconds(events),
            "diff_stat": _git(["diff", "--stat", f"{p.base_branch}..{branch}"], p.root)[-6000:] if branch_exists else None,
        })
        return out

    def _artifacts(self, p: RepoProfile, ticket_id: str, branch: str | None) -> list[dict[str, Any]]:
        ws = p.workspace_dir / ticket_id
        found: dict[str, dict[str, Any]] = {}
        if ws.is_dir():
            for f in sorted(ws.iterdir()):
                if f.is_file() and f.suffix in ARTIFACT_SUFFIXES:
                    st = f.stat()
                    found[f.name] = {"name": f.name, "size": st.st_size, "mtime": _iso(st.st_mtime), "source": "worktree"}
        if branch:
            prefix = f"{REPO_WORKSPACE_DIR}/{ticket_id}/"
            for path in _git(["ls-tree", "-r", "--name-only", branch, prefix], p.root).split():
                name = path[len(prefix):]
                if "/" not in name and Path(name).suffix in ARTIFACT_SUFFIXES and name not in found:
                    found[name] = {"name": name, "size": None, "mtime": None, "source": "branch"}
        return sorted(found.values(), key=lambda a: a["name"])

    def artifact(self, repo: str, ticket_id: str, name: str) -> dict[str, Any] | None:
        s = self.find(repo)
        if not s or not s.profile.eligible or "/" in name or name.startswith(".") or Path(name).suffix not in ARTIFACT_SUFFIXES:
            return None
        p = s.profile
        tid = ticket_id.upper()
        path = p.workspace_dir / tid / name
        if path.is_file():
            return {"name": name, "source": "worktree", "content": self._read_text(path, 200_000)}
        branch = f"hermes/{tid}"
        if any(b == branch for b, _ in list_ticket_branches(p.root)):
            text = _git(["show", f"{branch}:{REPO_WORKSPACE_DIR}/{tid}/{name}"], p.root)
            if text:
                return {"name": name, "source": "branch", "content": text[-200_000:]}
        return None

    # ----- 로그 -----
    def log_tail(self, lines: int = 500, repo: str | None = None, query: str | None = None) -> dict[str, Any]:
        log = self.paths.daemon_log
        raw, truncated = tail_lines(log, max(lines * (4 if (repo or query) else 1), lines))
        parsed = [parse_log_line(line) for line in raw]
        if repo:
            parsed = [p for p in parsed if p["repo"] == repo]
        if query:
            q = query.lower()
            parsed = [p for p in parsed if q in p["raw"].lower()]
        parsed = parsed[-lines:]
        st = log.stat() if log.exists() else None
        return {"path": str(log), "exists": log.exists(), "size": st.st_size if st else 0, "mtime": _iso(st.st_mtime) if st else None,
                "truncated": truncated, "lines": parsed}

    def log_size(self) -> int:
        try:
            return self.paths.daemon_log.stat().st_size
        except OSError:
            return 0

    def log_read_from(self, offset: int) -> tuple[int, list[str]]:
        """offset 이후 새 줄(완결된 줄만). 파일이 줄어들면 처음부터."""
        log = self.paths.daemon_log
        try:
            size = log.stat().st_size
        except OSError:
            return 0, []
        if size < offset:
            offset = 0
        if size == offset:
            return offset, []
        with log.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
        if not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut < 0:
                return offset, []
            data = data[: cut + 1]
        return offset + len(data), data.decode("utf-8", errors="replace").splitlines()

    # ----- 타임라인 -----
    def timeline(self, limit: int = 100) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        fleet = read_json(self.paths.fleet_state)
        if fleet.get("last_run"):
            events.append({"ts": fleet["last_run"].get("at"), "kind": "cursor", "repo": fleet["last_run"].get("repo"), "summary": "fleet 커서 이동"})
        for s in self.scan():
            p = s.profile
            if not p.eligible:
                continue
            state = self._repo_state(p.name)
            for tid, e in state.get("tickets", {}).items():
                events.append({"ts": e.get("finished_at") or e.get("started_at"), "kind": "ticket", "repo": p.name, "id": tid,
                               "verdict": e.get("verdict"), "status": e.get("status"), "branch": e.get("branch"), "seconds": e.get("seconds"),
                               "iterations": e.get("iterations"), "commit": (e.get("commit") or "")[:160], "error": (e.get("error") or "")[:300],
                               "summary": f"{tid} → {e.get('status')} (QA {e.get('verdict')}, {e.get('iterations')}회, {e.get('seconds')}s)"})
            runs = parse_merge_report(self._read_text(p.workspace_dir / "merge-report.md", 60_000))
            lm = state.get("last_merge")
            if lm and lm.get("at") and all(r["at"] != lm["at"] for r in runs):   # 보고서 파일이 없을 때(검증 불가 레포 등)도 state 기록은 보여 준다
                runs.append({"at": lm["at"], "merged": lm.get("merged") or [], "pushed": bool(lm.get("pushed")), "issues": lm.get("issues") or [], "note": lm.get("note") or ""})
            for run in runs:
                events.append({"ts": run["at"].replace(" ", "T"), "kind": "merge", "repo": p.name, "merged": run["merged"], "pushed": run["pushed"],
                               "issues": run["issues"], "note": run["note"],
                               "summary": f"일일 머지: {len(run['merged'])}건 merged, push={'yes' if run['pushed'] else 'no'}" + (f", issues={run['issues']}" if run["issues"] else "") + (f" — {run['note']}" if run["note"] else "")})
        lines, _ = tail_lines(self.paths.daemon_log, 3000)
        for line in lines:
            p = parse_log_line(line)
            if p["ts"] and ("▶" in p["msg"] or "■" in p["msg"] or "✗" in p["msg"] or "[fleet] start" in p["msg"] or "exiting" in p["msg"] or "[discover]" in p["msg"]):
                events.append({"ts": p["ts"].replace(" ", "T"), "kind": "log", "repo": p["repo"], "level": p["level"], "summary": p["msg"]})
        events = [e for e in events if e.get("ts")]
        events.sort(key=lambda e: e["ts"], reverse=True)
        return events[:limit]

    # ----- 통계 (차트용) -----
    def _daily_for(self, repos: list[dict[str, Any]], days: int = 14) -> list[dict[str, Any]]:
        today = self.now().date()
        buckets = {(today - dt.timedelta(days=i)).isoformat(): {"PASS": 0, "FAIL": 0, "ERROR": 0, "merged": 0, "seconds": 0} for i in range(days - 1, -1, -1)}
        for r in repos:
            state = self._repo_state(r["name"])
            for e in state.get("tickets", {}).values():
                day = _day(e.get("finished_at") or e.get("started_at"))
                if day in buckets:
                    buckets[day][e.get("verdict") if e.get("verdict") in ("PASS", "FAIL") else "ERROR"] += 1
                    buckets[day]["seconds"] += int(e.get("seconds") or 0)
            for run in parse_merge_report(self._read_text(Path(r["root"]) / REPO_WORKSPACE_DIR / "merge-report.md", 60_000)):
                day = _day(run["at"])
                if day in buckets:
                    buckets[day]["merged"] += len(run["merged"])
        return [{"date": d, **v} for d, v in buckets.items()]

    def stats(self, days: int = 14) -> dict[str, Any]:
        repos = [r for r in self.repos() if r["fe_be_ok"]]
        per_repo = []
        stage_totals = {s: 0.0 for s in STAGES}
        for r in repos:
            state = self._repo_state(r["name"])
            stage_sum = {s: 0.0 for s in STAGES}
            for tid in state.get("tickets", {}):
                for s, v in stage_seconds(read_run_events(Path(r["root"]) / REPO_WORKSPACE_DIR, tid)).items():
                    stage_sum[s] += v
                    stage_totals[s] += v
            merged_total = sum(len(run["merged"]) for run in parse_merge_report(self._read_text(Path(r["root"]) / REPO_WORKSPACE_DIR / "merge-report.md", 60_000)))
            per_repo.append({"name": r["name"], "selected": r["selected"], "target": r["eligible"], "counts": r["counts"], "verdicts": r["verdicts"],
                             "processed": r["processed_count"], "total_seconds": r["total_seconds"], "merged_total": merged_total,
                             "stage_seconds": {k: round(v, 1) for k, v in stage_sum.items()}})
        return {"days": days, "daily": self._daily_for(repos, days), "per_repo": per_repo,
                "stage_totals": {k: round(v, 1) for k, v in stage_totals.items()}}

    def _frontend_guide_summary(self) -> list[dict[str, str]]:
        try:
            guide = load_template(self.paths.template_org).get("frontend_guide") or {}
        except (OSError, ValueError, KeyError):
            return []
        return [{"area": str(i.get("area", "")), "use": str(i.get("use", "")), "npm": str(i.get("npm", ""))} for i in guide.get("stack", []) if isinstance(i, dict)]

    # ----- 개요 -----
    def overview(self) -> dict[str, Any]:
        now = self.now()
        cfg = self.config()
        repos = self.repos()
        targets = [r for r in repos if r["eligible"]]
        totals = {st: 0 for st in VALID_STATUS}
        for r in targets:
            for st, n in r.get("counts", {}).items():
                totals[st] = totals.get(st, 0) + n
        fleet = read_json(self.paths.fleet_state)
        return {
            "now": now.strftime("%Y-%m-%dT%H:%M:%S"), "tz": time.strftime("%Z"),
            "fleet_dir": str(self.paths.fleet_dir), "config": str(self.paths.config), "config_writable": self.paths.config_writable(),
            "root": str(cfg.root), "include": [str(x) for x in (cfg.include or [])], "exclude": [str(x) for x in (cfg.exclude or [])],
            "schedule": self.schedule(cfg, now), "daemon": self.daemon(cfg, now), "llm": self.llm(),
            "cursor": fleet.get("cursor"), "last_run": fleet.get("last_run"),
            "repos": repos, "eligible_count": len(targets), "totals": totals,
            "auto_merge": cfg.auto_merge, "discover": cfg.discover, "frontend_guide": self._frontend_guide_summary(),
            "monitor": {"uptime_seconds": round(time.time() - self.started_at), "in_docker": Path("/.dockerenv").exists()},
        }
