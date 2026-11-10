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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..backlog import VALID_STATUS, Ticket, TicketError, load_tickets
from .. import requests as rq
from .. import enhancements as eh
from .. import projects as pj
from ..fleetfile import SCHEDULE_KEYS, FleetFileError, is_selected, set_repo_selected, update_approval, update_calendar, update_schedule
from ..llm import check_endpoint
from .. import jev, review as rv
import threading
from ..merge import list_ticket_branches, ticket_on_branch
from ..org import load_org_for, load_template
from ..repos import FleetConfig, RepoProfile, detect_profile
from ..routing import ModelRouter
from ..schedule import Schedule, parse_clock
from ..settings import FLEET_DIR, REPO_WORKSPACE_DIR, Settings, load_dotenv
from ..tools import git
from ..workflow import detect_delivery, github_runs, sync_status
from .crew import CrewFeedParser, roles_from_org

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
LOG_REPO = re.compile(r"\[(?:fleet|merge)\]\[([^\]]+)\]|^\[([^\]]+)\]\[(?:pm|design|dev|qa)\]")
LOG_TICKET_START = re.compile(r"\[fleet\]\[([^\]]+)\] ▶ ([A-Za-z]+-\d+) (.*?) \[(P\d)\]$")
LOG_STAGE = re.compile(r"^\[([^\]]+)\]\[(pm|design|dev|qa)\] iteration (\d+) → (.*)$")
DEPLOYISH = re.compile(r"deploy|\bcd\b|release|publish|rollout", re.I)
MERGE_SECTION = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$")
MERGE_OUTCOME = re.compile(r"^- (merged|conflict|verify-failed|skipped|error)\s+(\S+) \(([^)]+)\)\s*(.*)$")
START_MARK = "[fleet] start "
STOP_MARKS = ("[fleet] outside active hours; exiting", "[fleet] interrupted", "tickets, stopping")
OD_START = "[fleet] on-demand start"
OD_STOP = "[fleet] on-demand done"
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
        self._delivery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.started_at = time.time()
        self._review_lock = threading.Lock()
        self._review_thread: threading.Thread | None = None
        self._review_cache: tuple[float, dict[str, Any]] | None = None
        self._system_history: deque[dict[str, Any]] = deque(maxlen=360)
        self._previous_cpu: tuple[int, int] | None = None

    @classmethod
    def from_env(cls) -> "Collector":
        return cls(MonitorPaths.from_env())

    # ----- 설정 -----
    def config(self) -> FleetConfig:
        if self.paths.config.exists():
            return FleetConfig.load(self.paths.config)
        example = self.paths.fleet_dir / "fleet.example.yaml"      # 받은 직후(초기화 전): 템플릿 기본값 + 상위 폴더 스캔으로 읽기 전용 표시
        cfg = FleetConfig.load(example) if example.exists() else FleetConfig(root=self.paths.fleet_dir.parent)
        cfg.source = None
        return cfg

    def initialized(self) -> bool:
        return self.paths.config.exists()

    # ----- 시스템 사용량 -----
    def system_metrics(self) -> dict[str, Any]:
        """호스트의 CPU/RAM 및 NVIDIA GPU 사용률을 수집한다.

        psutil 같은 추가 패키지는 쓰지 않는다. CPU는 /proc/stat 두 스냅샷의
        차이로, 메모리는 MemAvailable 기준으로 계산하며, GPU가 없거나
        nvidia-smi를 쓸 수 없는 환경도 정상적인 빈 데이터로 반환한다.
        """
        cpu = self._cpu_percent()
        memory = self._memory_percent()
        gpu, gpu_memory, gpu_note = self._gpu_metrics()
        sample = {
            "at": self.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "cpu": cpu,
            "memory": memory,
            "gpu": gpu,
            "gpu_memory": gpu_memory,
        }
        self._system_history.append(sample)
        return {"sample": sample, "history": list(self._system_history), "gpu_available": gpu is not None, "gpu_note": gpu_note,
                "retention": "최근 360개 샘플 (약 1시간 · 10초 갱신)"}

    def _cpu_percent(self) -> float | None:
        try:
            fields = (Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]).split()[1:]
            values = [int(v) for v in fields]
            total, idle = sum(values), values[3] + (values[4] if len(values) > 4 else 0)
        except (OSError, IndexError, ValueError):
            return None
        previous, self._previous_cpu = self._previous_cpu, (total, idle)
        if previous is None or total <= previous[0]:
            return None
        return round(max(0.0, min(100.0, 100 * (1 - (idle - previous[1]) / (total - previous[0])))), 1)

    @staticmethod
    def _memory_percent() -> float | None:
        try:
            values = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
            total, available = values["MemTotal"], values["MemAvailable"]
            return round(max(0.0, min(100.0, 100 * (1 - available / total))), 1)
        except (OSError, KeyError, ValueError):
            return None

    @staticmethod
    def _gpu_metrics() -> tuple[float | None, float | None, str | None]:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, None, "NVIDIA GPU 없음 또는 nvidia-smi를 실행할 수 없습니다."
        if result.returncode or not result.stdout.strip():
            return None, None, "NVIDIA GPU 없음 또는 nvidia-smi를 실행할 수 없습니다."
        try:
            rows = [[float(v.strip()) for v in line.split(",")] for line in result.stdout.splitlines() if line.strip()]
            gpu = sum(row[0] for row in rows) / len(rows)
            used, total = sum(row[1] for row in rows), sum(row[2] for row in rows)
            return round(gpu, 1), round(100 * used / total, 1) if total else None, None
        except (ValueError, IndexError, ZeroDivisionError):
            return None, None, "GPU 사용량을 해석할 수 없습니다."

    def settings(self) -> Settings:
        return Settings.from_env()

    # ----- 스케줄 -----
    def schedule(self, cfg: FleetConfig, now: dt.datetime) -> dict[str, Any]:
        sch = Schedule(cfg.active_hours, cfg.calendar)
        merge_time = parse_clock(cfg.merge_at)
        active = sch.is_active(now)
        margin = int(cfg.no_new_work_minutes_before_end)
        nxt = sch.next_start(now)
        remain = sch.minutes_until_end(now)
        today_kind, today_w = sch.window_on(now.date())
        from ..schedule import window_spec  # noqa: WPS433
        first = (now.date().replace(day=1) - dt.timedelta(days=31)).replace(day=1)   # 지난달 1일부터 3개월치
        return {
            "active_hours": cfg.active_hours, "merge_at": cfg.merge_at, "in_window": active,
            "can_start_work": sch.can_start_work(now, margin),
            "minutes_until_end": None if remain == float("inf") else round(remain),
            "no_new_work_minutes_before_end": margin, "poll_seconds": int(cfg.poll_seconds),
            "next_window_start": nxt.strftime("%Y-%m-%dT%H:%M:%S") if nxt else None,
            "merge_window_open": bool(merge_time and active and now.time() >= merge_time),
            "calendar": sch.calendar_specs(),
            "today": {"date": now.date().isoformat(), "kind": today_kind, "spec": window_spec(today_w)},
            "days": sch.days(first, 31 * 3 + 7),
        }

    def update_calendar(self, entries: dict[str, Any]) -> dict[str, Any]:
        self._require_writable()
        update_calendar(self.paths.config, entries)
        self._cfg = None
        cfg = self.config()
        return {"calendar": cfg.calendar, "schedule": self.schedule(cfg, self.now())}

    # ----- 데몬 -----
    def daemon(self, cfg: FleetConfig, now: dt.datetime) -> dict[str, Any]:
        log = self.paths.daemon_log
        lines, _ = tail_lines(log, 400)
        mtime = log.stat().st_mtime if log.exists() else None
        last_start = last_stop = -1
        od_start = od_stop = -1
        start_ts = None
        for i, line in enumerate(lines):
            if START_MARK in line:
                last_start = i
                m = LOG_TS.match(line)
                start_ts = m.group(1) if m else None
            if any(m in line for m in STOP_MARKS):
                last_stop = i
            if OD_START in line:
                od_start = i
            if OD_STOP in line:
                od_stop = i
        lock_held = probe_lock(self.paths.lock_file)
        poll = int(cfg.poll_seconds)
        stale_after = max(3 * poll, 2700)
        age = (time.time() - mtime) if mtime else None
        fresh = age is not None and age <= stale_after
        paused = self.paths.pause_file.exists()

        on_demand_running = od_start > od_stop and od_start > last_start and fresh
        if lock_held is True:
            status, method = "running", "flock"
        elif last_start > last_stop and fresh:
            status, method = "running", "log"
        elif on_demand_running:
            status, method = "running", "on-demand"
        elif last_start > last_stop:
            status, method = "stale", "log"
        else:
            status, method = "stopped", "log"

        activity = self._current_activity(lines, status == "running")
        in_window = Schedule(cfg.active_hours, cfg.calendar).is_active(now)
        return {
            "status": status, "method": method, "paused": paused,
            "pause_note": self.paths.pause_file.read_text(encoding="utf-8").strip() if paused else None,
            "lock_held": lock_held, "log_exists": log.exists(), "log_size": log.stat().st_size if log.exists() else 0,
            "log_mtime": _iso(mtime), "log_age_seconds": round(age) if age is not None else None,
            "stale_after_seconds": stale_after, "started_at": start_ts.replace(" ", "T") if start_ts else None,
            "expected_running": in_window and not paused,
            "activity": activity,
            "paused_repos": rq.paused_repos(self.paths.state_dir),
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

    # ----- 크루 룸 (역할별 대화 피드) -----
    CREW_TAIL_LINES = 60000          # CrewAI 패널이 한 단계에 수백 줄이라 넉넉히 읽는다 (바이트 상한은 CREW_TAIL_BYTES)
    CREW_TAIL_BYTES = 6 * 1024 * 1024

    def _crew_orgs(self) -> dict[str, dict[str, Any]]:
        """대상 레포별 org(roles/stages). 템플릿 또는 레포의 hermes/config/org.yaml."""
        orgs: dict[str, dict[str, Any]] = {}
        for sc in self.scan():
            if not sc.target:
                continue
            try:
                orgs[sc.profile.name] = load_org_for(sc.profile, self.paths.template_org)
            except (OSError, ValueError, KeyError):
                continue
        return orgs

    def crew_parser(self, orgs: dict[str, dict[str, Any]] | None = None) -> CrewFeedParser:
        orgs = orgs if orgs is not None else self._crew_orgs()
        try:
            template = load_template(self.paths.template_org)
        except (OSError, ValueError, KeyError):
            template = {"roles": {}, "stages": {}, "discover": {}}

        def role_for(repo: str | None, stage: str) -> str:
            org = orgs.get(repo or "", template)
            if stage == "discover":
                return (org.get("discover") or {}).get("role") or "techlead"
            spec = (org.get("stages") or {}).get(stage)
            return spec["role"] if spec else {"pm": "pm", "design": "designer", "dev": "developer", "qa": "qa"}.get(stage, "system")

        def role_by_title(repo: str | None, title: str) -> str | None:
            org = orgs.get(repo or "", template)
            t = title.strip().lower()
            for key, spec in (org.get("roles") or {}).items():
                if str(spec.get("role", "")).strip().lower() == t:
                    return key
            return CrewFeedParser._default_role_by_title(repo, title)

        return CrewFeedParser(role_for=role_for, role_by_title=role_by_title)

    def crew(self, repo: str | None = None, limit: int = 300, parser: CrewFeedParser | None = None) -> dict[str, Any]:
        """크루 룸 스냅샷: 레포별 역할 목록·상태 + 최근 메시지. parser 를 넘기면(SSE) 그 상태를 쓰고, 없으면 로그 tail 을 새로 파싱."""
        now = self.now()
        cfg = self.config()
        orgs = self._crew_orgs()
        if parser is None:
            parser = self.crew_parser(orgs)
            lines, _ = tail_lines(self.paths.daemon_log, self.CREW_TAIL_LINES, self.CREW_TAIL_BYTES)
            parser.feed(lines)
        d = self.daemon(cfg, now)
        running = d["status"] == "running"
        rooms = []
        for name, org in orgs.items():
            roles = roles_from_org(org)
            rooms.append({"name": name, "roles": roles, "status": parser.status(name, [r["key"] for r in roles], running),
                          "active": bool(running and parser.repos.get(name) and parser.repos[name].active),
                          "ticket": parser.repos[name].ticket if name in parser.repos else None,
                          "stage": parser.repos[name].stage if name in parser.repos else None,
                          "paused": rq.is_repo_paused(self.paths.state_dir, name), "org_source": org.get("_source")})
        msgs = [m for m in parser.messages if repo is None or m["repo"] in (repo, None)]
        return {"now": now.strftime("%Y-%m-%dT%H:%M:%S"), "daemon": {"status": d["status"], "paused": d["paused"], "activity": d["activity"]},
                "rooms": rooms, "messages": msgs[-limit:], "total_messages": len(msgs)}

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
            profile.approval = cfg.approval_for(child.name)
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
        pending = [r["action"] + (f" {r['ticket']}" if r.get("ticket") else "") for r in rq.list_pending(self.paths.state_dir) if r.get("repo") == p.name]
        base: dict[str, Any] = {"name": p.name, "root": str(p.root), "eligible": s.target, "fe_be_ok": p.eligible, "selected": s.selected,
                                "excluded": s.excluded, "in_include": s.in_include, "reasons": reasons,
                                "paused": rq.is_repo_paused(self.paths.state_dir, p.name), "pending_requests": pending, "deny": list(p.deny),
                                "frontend": p.frontend.label() if p.frontend else None, "backend": p.backend.label() if p.backend else None,
                                "base_branch": p.base_branch, "has_remote": p.has_remote,
                                "approval": p.approval, "approval_override": p.name in (self.config().approval.get("repos") or {}),
                                "review": self._review_rows().get(p.name)}
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
            d = self.delivery(p)
            ci = self.ci_runs(d, p.base_branch)
            base["delivery"] = d
            base["ci"] = ci
            base["last_deploy"] = state.get("last_deploy")
            base["workflow"] = self.workflow_stages(base, d, ci, self.daemon(self.config(), self.now()).get("activity"))
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

    # ----- 전달 체인 / 워크플로 -----
    def delivery(self, p: RepoProfile, ttl: float = 60.0) -> dict[str, Any]:
        key = str(p.root)
        hit = self._delivery_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        d = detect_delivery(p.root, p.base_branch, p.deploy).to_dict()
        d["sync"] = sync_status(p.root, p.base_branch) if d["remote_url"] else {"tracked": False}
        self._delivery_cache[key] = (time.time(), d)
        return d

    @staticmethod
    def _github_token() -> str | None:
        return os.environ.get("MONITOR_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or None

    def ci_runs(self, d: dict[str, Any], base_branch: str) -> dict[str, Any] | None:
        if d.get("remote_host") != "github" or not d.get("owner"):
            return None
        return github_runs(d["owner"], d["repo"], base_branch, self._github_token())

    def workflow_stages(self, r: dict[str, Any], d: dict[str, Any], ci: dict[str, Any] | None, activity: dict[str, Any] | None) -> list[dict[str, Any]]:
        """레포 하나의 전개 단계: 발굴 → 승인 → 크루 → 리뷰 → 머지 → push → CI/CD → 배포."""
        target = bool(r.get("eligible"))
        counts = r.get("counts") or {}
        state = self._repo_state(r["name"])
        verdicts = r.get("verdicts") or {}
        runs_all = parse_merge_report(self._read_text(Path(r["root"]) / REPO_WORKSPACE_DIR / "merge-report.md", 60_000))
        last_push = next((x["at"] for x in runs_all if x.get("pushed")), None)
        lm = r.get("last_merge") or {}
        disc_dir = Path(r["root"]) / REPO_WORKSPACE_DIR / "discover"
        disc_files = sorted(disc_dir.glob("*.md")) if disc_dir.is_dir() else []
        last_disc = disc_files[-1].stem if disc_files else None
        if last_disc and len(last_disc) == 15:
            last_disc = f"{last_disc[:4]}-{last_disc[4:6]}-{last_disc[6:8]}T{last_disc[9:11]}:{last_disc[11:13]}:{last_disc[13:15]}"
        mine = activity if activity and activity.get("repo") == r["name"] else None
        off = "none" if not target else None
        manual = (r.get("approval") or "auto") == "manual"
        stages = [
            {"key": "discover", "label": "발굴", "mode": off or "auto", "count": counts.get("proposed", 0) if manual else counts.get("todo", 0) + counts.get("proposed", 0), "at": last_disc,
             "status": "ok" if (counts.get("proposed") if manual else counts.get("todo") or counts.get("proposed")) else "idle",
             "detail": (f"승인 대기 {counts.get('proposed', 0)}" if manual else f"제안 → 바로 todo {counts.get('todo', 0)}") if target else "대상 아님"},
            {"key": "approve", "label": "승인", "mode": off or ("manual" if manual else "auto"), "count": counts.get("todo", 0), "status": "ok" if counts.get("todo") else "idle",
             "detail": f"todo {counts.get('todo', 0)} (사람/웹 승인)" if manual else f"자동 승인 · todo {counts.get('todo', 0)}"},
            {"key": "crew", "label": "크루 작업", "mode": off or "auto", "count": counts.get("in-progress", 0) + (1 if mine and mine.get("kind") == "ticket" else 0),
             "status": "running" if mine else ("ok" if r.get("processed_count") else "idle"), "at": (r.get("last_ticket") or {}).get("finished_at"),
             "detail": (f"진행 중 {mine.get('id')} {('· ' + mine['stage']['name']) if mine.get('stage') else ''}" if mine else f"처리 {r.get('processed_count', 0)} · PASS {verdicts.get('PASS', 0)} / FAIL {verdicts.get('FAIL', 0)} / ERR {verdicts.get('ERROR', 0)}")},
            {"key": "review", "label": "리뷰 / QA", "mode": off or "manual", "count": counts.get("review", 0), "fail": counts.get("blocked", 0),
             "status": "fail" if counts.get("blocked") else ("ok" if counts.get("review") else "idle"), "detail": f"리뷰 대기 {counts.get('review', 0)} · 막힘 {counts.get('blocked', 0)}"},
            {"key": "merge", "label": "머지 · 검증", "mode": off or ("auto" if r.get("has_verification") else "manual"), "count": counts.get("done", 0), "at": (lm.get("at") or "").replace(" ", "T") or None,
             "status": "fail" if lm.get("issues") else ("ok" if lm.get("merged") else ("warn" if target and not r.get("has_verification") else "idle")),
             "detail": (f"마지막 {len(lm.get('merged') or [])}건" + (f" · 문제 {', '.join(lm['issues'])}" if lm.get("issues") else "")) if lm else ("검증 명령 없음 → 자동 머지 제외" if target and not r.get("has_verification") else f"{r.get('verify_steps') and len(r['verify_steps']) or 0}단계 검증 후 머지")},
            {"key": "push", "label": "GitHub push", "mode": d.get("push_mode", "none"), "at": (last_push or "").replace(" ", "T") or None,
             "status": "ok" if last_push else ("warn" if d.get("sync", {}).get("ahead") else "idle"),
             "detail": (d.get("remote_web") or d.get("remote_url") or "원격 없음") + (f" · ahead {d['sync']['ahead']} / behind {d['sync']['behind']}" if d.get("sync", {}).get("tracked") else ""),
             "link": d.get("remote_web")},
        ]
        wf_names = [w["name"] for w in d.get("workflows", []) if w.get("on_base_push")]
        ci_status, ci_detail, ci_at, ci_link = "idle", (", ".join(wf_names) if wf_names else "push 에 반응하는 워크플로 없음"), None, (ci or {}).get("web")
        if ci and ci.get("ok") and ci.get("runs"):
            runs = ci["runs"]
            if any(x.get("status") in ("in_progress", "queued") for x in runs):
                ci_status = "running"
            elif any(x.get("conclusion") not in ("success", "skipped", None) for x in runs):
                ci_status = "fail"
            else:
                ci_status = "ok"
            ci_detail = " · ".join(f"{x['name']}: {x.get('conclusion') or x.get('status')}" for x in runs[:4])
            ci_at = max((x.get("updated_at") or "" for x in runs), default=None) or None
            ci_link = runs[0].get("url") or ci_link
        elif ci and not ci.get("ok"):
            ci_detail += f" · Actions 조회 실패: {ci.get('error')}"
        stages.append({"key": "cicd", "label": "CI/CD (Actions)", "mode": d.get("cicd_mode", "none"), "status": ci_status, "detail": ci_detail, "at": ci_at, "link": ci_link,
                       "workflows": [w["name"] for w in d.get("workflows", [])]})
        ld = state.get("last_deploy")
        dep_runs = [x for x in (ci or {}).get("runs", []) if DEPLOYISH.search(x.get("name") or "")]
        if ld:
            dep_status, dep_detail, dep_at = ("ok" if ld.get("ok") else "fail"), f"fleet 배포 {'성공' if ld.get('ok') else '실패'} ({ld.get('source')})", (ld.get("at") or "").replace(" ", "T")
        elif dep_runs:
            x = dep_runs[0]
            dep_status = "running" if x.get("status") in ("in_progress", "queued") else ("ok" if x.get("conclusion") == "success" else "fail")
            dep_detail, dep_at = f"{x['name']}: {x.get('conclusion') or x.get('status')} @{x.get('sha')}", x.get("updated_at")
        else:
            dep_status, dep_detail, dep_at = "idle", {"actions": "push 시 Actions 가 배포", "fleet": "push 뒤 fleet 이 deploy.command 실행", "manual": "사람이 실행(웹 '배포' 요청 가능)" if (d.get("override") or {}).get("command") else "사람이 실행 (스크립트/IaC 탐지)", "none": "배포 없음"}.get(d.get("deploy_mode"), ""), None
        stages.append({"key": "deploy", "label": "배포" + (" (AWS)" if d.get("aws") else ""), "mode": d.get("deploy_mode", "none"), "status": dep_status, "detail": dep_detail, "at": dep_at,
                       "target": d.get("deploy_target"), "aws": d.get("aws"), "link": dep_runs[0].get("url") if dep_runs else None,
                       "can_request": bool((d.get("override") or {}).get("command")) and target})
        return stages

    def workflow_board(self, with_ci: bool = False) -> dict[str, Any]:
        cfg = self.config()
        now = self.now()
        activity = self.daemon(cfg, now).get("activity")
        rows = []
        scanned = self.scan(cfg)
        summaries = {s.profile.name: self._repo_summary(s) for s in scanned}
        deliveries = {s.profile.name: self.delivery(s.profile) for s in scanned}
        ci_map: dict[str, Any] = {}
        if with_ci:
            from concurrent.futures import ThreadPoolExecutor  # noqa: WPS433
            gh = [(s.profile.name, deliveries[s.profile.name], s.profile.base_branch) for s in scanned if deliveries[s.profile.name].get("remote_host") == "github"]
            with ThreadPoolExecutor(max_workers=6) as ex:
                for name, res in zip([g[0] for g in gh], ex.map(lambda g: self.ci_runs(g[1], g[2]), gh)):
                    ci_map[name] = res
        for s in scanned:
            r = summaries[s.profile.name]
            d = deliveries[s.profile.name]
            stages = self.workflow_stages(r, d, ci_map.get(s.profile.name), activity)
            reach = next((st["key"] for st in reversed(stages) if st["mode"] != "none"), None)
            rows.append({"name": r["name"], "eligible": r["eligible"], "fe_be_ok": r["fe_be_ok"], "selected": r["selected"], "paused": r.get("paused", False),
                         "summary": d.get("summary"), "remote_host": d.get("remote_host"), "aws": d.get("aws"), "deploy_mode": d.get("deploy_mode"), "deploy_target": d.get("deploy_target"),
                         "reach": reach, "stages": stages, "ci": ci_map.get(s.profile.name)})
        rows.sort(key=lambda x: (not x["eligible"], not x["fe_be_ok"], x["name"]))
        totals = {"targets": sum(1 for x in rows if x["eligible"]), "push": sum(1 for x in rows if x["eligible"] and x["stages"][5]["mode"] == "auto"),
                  "cicd": sum(1 for x in rows if x["eligible"] and x["stages"][6]["mode"] == "auto"),
                  "deploy_auto": sum(1 for x in rows if x["eligible"] and x["stages"][7]["mode"] in ("actions", "fleet")),
                  "aws": sum(1 for x in rows if x["eligible"] and x["aws"])}
        return {"rows": rows, "totals": totals, "with_ci": with_ci, "github_token": bool(self._github_token()), "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S")}

    # ----- 제어 (요청 큐 / 일시정지 / 스케줄) -----
    # ----- Jev 레포 판정(review) -----
    def _review_rows(self) -> dict[str, dict[str, Any]]:
        last = rv.load_last(self.paths.fleet_dir)
        return {r["name"]: {**r, "at": last.get("at")} for r in last.get("repos", []) if isinstance(r, dict) and r.get("name")}

    def review_running(self) -> bool:
        t = self._review_thread
        return bool(t and t.is_alive())

    def review_status(self) -> dict[str, Any]:
        """마지막 판정(state/review.json) + 설정 + 실행 중 여부 + Jev 키 설정 여부."""
        cfg = self.config()
        try:
            settings = rv.ReviewSettings.from_config(cfg.review)
            cfg_error = None
        except ValueError as exc:
            settings, cfg_error = rv.ReviewSettings(), str(exc)
        jset = jev.JevSettings.from_env(settings.model)
        last = rv.load_last(self.paths.fleet_dir)
        return {"running": self.review_running(), "on_start": settings.on_start and rv.on_start_enabled(cfg), "mode": settings.mode,
                "min_confidence": settings.min_confidence, "pinned": settings.pinned, "model": jset.model, "base_url": jset.base_url,
                "key_configured": jset.configured, "config_error": cfg_error, "last": last or None,
                "writable": self.paths.config_writable()}

    def llm_usage(self) -> dict[str, Any]:
        """Canvas 라우팅 화면용: 설정, 마지막 실제 선택, Jev 및 현재 작업."""
        settings = self.settings()
        router = ModelRouter.from_env(settings)
        fleet = read_json(self.paths.fleet_state)
        jset = jev.JevSettings.from_env()
        now = self.now()
        return {
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "routing": {"mode": router.mode, "gpu_max_percent": router.gpu_max_percent,
                        "gpu_memory_max_percent": router.gpu_memory_max_percent,
                        "local": {"provider": router.local.provider, "model": router.local.model, "base_url": router.local.base_url},
                        "external": {"provider": router.external.provider, "model": router.external.model, "base_url": router.external.base_url}},
            "last_route": fleet.get("last_llm_route"),
            "jev": {"provider": jset.provider, "model": jset.model, "configured": jset.configured},
            "activity": self.daemon(self.config(), now).get("activity"),
        }

    def run_review(self, trigger: str = "manual", dry_run: bool = False) -> dict[str, Any]:
        """동기 실행(요청 API). 이미 돌고 있으면 409 가 되도록 PermissionError 대신 RuntimeError."""
        if not self._review_lock.acquire(blocking=False):
            raise RuntimeError("Jev 판정이 이미 실행 중입니다")
        try:
            if not self.initialized():
                raise ValueError("fleet.yaml 이 없어 판정할 수 없습니다 (먼저 init)")
            return rv.run_review(self.paths.fleet_dir, self.config(), trigger=trigger, dry_run=dry_run, log=lambda m: print(m, flush=True))
        finally:
            self._review_lock.release()

    def start_review_async(self, trigger: str = "start") -> bool:
        """모니터 기동 때 백그라운드 스레드로 판정(헬스체크·첫 화면을 막지 않는다). 이미 실행 중이면 False."""
        if self.review_running():
            return False
        t = threading.Thread(target=self._safe_review, args=(trigger,), name="jev-review", daemon=True)
        self._review_thread = t
        t.start()
        return True

    def _safe_review(self, trigger: str) -> None:
        try:
            self.run_review(trigger)
        except Exception as exc:  # noqa: BLE001
            print(f"[review] 실패: {exc}", flush=True)

    def _require_writable(self) -> None:
        if not os.access(self.paths.fleet_dir, os.W_OK):
            raise PermissionError(f"hermes-fleet 디렉터리에 쓸 수 없습니다 (읽기 전용 마운트?): {self.paths.fleet_dir}")

    def requests(self) -> dict[str, Any]:
        return {"pending": rq.list_pending(self.paths.state_dir), "recent": rq.list_done(self.paths.state_dir, 25),
                "paused_repos": rq.paused_repos(self.paths.state_dir), "paused_all": self.paths.pause_file.exists()}

    def create_request(self, repo: str, action: str, ticket: str | None = None) -> dict[str, Any]:
        self._require_writable()
        s = self.find(repo)
        if s is None:
            raise ValueError(f"root 아래에 없는 레포: {repo}")
        if action != "stop" and not s.target:
            raise ValueError(f"{repo} 은(는) fleet 대상이 아닙니다 (선택 여부/FE·BE 확인)")
        if action == "deploy" and not s.profile.deploy.get("command"):
            raise ValueError(f"{repo}: fleet.yaml repos.{repo}.deploy.command 가 없어 배포 요청을 만들 수 없습니다")
        return rq.create_request(self.paths.state_dir, repo, action, ticket, source="web")

    def enhancements(self, repo: str | None = None) -> list[dict[str, Any]]:
        return eh.list_enhancements(self.paths.state_dir, repo)

    def enhancement(self, enhancement_id: str) -> dict[str, Any]:
        return eh.get_enhancement(self.paths.state_dir, enhancement_id)

    def create_enhancement(self, repo: str, brief: str, title: str = "") -> dict[str, Any]:
        self._require_writable()
        selected = self.find(repo)
        if selected is None:
            raise ValueError(f"root 아래에 없는 레포: {repo}")
        if not selected.target:
            raise ValueError(f"{repo} 은(는) fleet 대상이 아닙니다. 먼저 레포 목록에서 대상으로 선택하세요")
        record = eh.create_enhancement(self.paths.state_dir, repo, brief, title)
        request = rq.create_request(self.paths.state_dir, repo, "enhance", record["id"], source="web")
        return {"enhancement": record, "request": request}

    def projects(self) -> list[dict[str, Any]]:
        return pj.list_projects(self.paths.state_dir)

    def create_project(self, name: str, description: str) -> dict[str, Any]:
        self._require_writable()
        record = pj.create(self.paths.state_dir, name, description)
        request = rq.create_request(self.paths.state_dir, record["name"], "bootstrap", record["id"], source="web")
        return {"project": record, "request": request}

    def cancel_request(self, request_id: str) -> bool:
        self._require_writable()
        return rq.cancel(self.paths.state_dir, request_id)

    def set_repo_approval(self, repo: str, mode: str | None) -> dict[str, Any]:
        """레포의 사람 승인 모드(auto|manual|default=예외 삭제)를 fleet.yaml approval.repos 에 저장."""
        self._require_writable()
        if self.find(repo) is None:
            raise ValueError(f"root 아래에 없는 레포: {repo}")
        saved = update_approval(self.paths.config, repos={repo: None if (mode or "default") == "default" else mode})
        self._cfg = None
        return {"repo": repo, "approval": self.config().approval_for(repo), "override": repo in saved["repos"], "approval_config": saved}

    def set_default_approval(self, mode: str) -> dict[str, Any]:
        self._require_writable()
        saved = update_approval(self.paths.config, default=mode)
        self._cfg = None
        return {"approval_config": saved}

    def set_repo_paused(self, repo: str, paused: bool) -> dict[str, Any]:
        self._require_writable()
        if self.find(repo) is None:
            raise ValueError(f"root 아래에 없는 레포: {repo}")
        rq.set_repo_paused(self.paths.state_dir, repo, paused, "paused from web")
        return {"repo": repo, "paused": paused}

    def set_paused_all(self, paused: bool) -> dict[str, Any]:
        self._require_writable()
        if paused:
            self.paths.pause_file.parent.mkdir(parents=True, exist_ok=True)
            self.paths.pause_file.write_text(f"paused from web at {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
        elif self.paths.pause_file.exists():
            self.paths.pause_file.unlink()
        return {"paused": paused}

    def update_schedule(self, fields: dict[str, Any]) -> dict[str, Any]:
        if not self.paths.config_writable():
            raise PermissionError(f"fleet.yaml 에 쓸 수 없습니다: {self.paths.config}")
        clean = {k: v for k, v in fields.items() if k in SCHEDULE_KEYS and v is not None}
        if not clean:
            raise FleetFileError("바꿀 항목이 없습니다")
        try:
            result = update_schedule(self.paths.config, **clean)
        except ValueError as exc:               # parse_window 의 형식 오류 포함
            raise FleetFileError(str(exc)) from exc
        cfg = self.config()
        result["schedule"] = self.schedule(cfg, self.now())
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
            "now": now.strftime("%Y-%m-%dT%H:%M:%S"), "tz": "KST (Asia/Seoul, UTC+9)",
            "fleet_dir": str(self.paths.fleet_dir), "config": str(self.paths.config), "config_writable": self.paths.config_writable(), "initialized": self.initialized(),
            "root": str(cfg.root), "include": [str(x) for x in (cfg.include or [])], "exclude": [str(x) for x in (cfg.exclude or [])],
            "schedule": self.schedule(cfg, now), "daemon": self.daemon(cfg, now), "llm": self.llm(),
            "cursor": fleet.get("cursor"), "last_run": fleet.get("last_run"),
            "repos": repos, "eligible_count": len(targets), "totals": totals,
            "control": {"writable": os.access(self.paths.fleet_dir, os.W_OK), "config_writable": self.paths.config_writable(),
                        "paused_all": self.paths.pause_file.exists(), "paused_repos": rq.paused_repos(self.paths.state_dir),
                        "pending": rq.list_pending(self.paths.state_dir), "recent": rq.list_done(self.paths.state_dir, 12)},
            "auto_merge": cfg.auto_merge, "discover": cfg.discover, "approval": cfg.approval, "frontend_guide": self._frontend_guide_summary(),
            "monitor": {"uptime_seconds": round(time.time() - self.started_at), "in_docker": Path("/.dockerenv").exists()},
            "review": self.review_status(),
        }
