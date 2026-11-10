"""요청 큐와 레포별 일시정지 마커 (state/ 아래 파일 기반).

웹 모니터(컨테이너)는 다른 레포에 쓸 수 없고 LLM 크루도 없으므로, 조작은 모두 '요청 파일'로 남기고 호스트의 데몬이 실행한다.
- state/requests/<id>.json          대기(pending) / 실행 중(running) 요청
- state/requests/done/<id>.json     끝난 요청(done | failed | skipped), 최근 N개만 보관
- state/paused/<repo>               레포별 일시정지 마커 (있으면 데몬이 그 레포의 새 작업·머지·요청을 건너뛴다)

요청 action: run(다음 todo 또는 지정 티켓 실행) · discover(발굴) · merge(지금 머지) · approve(proposed→todo) · retry(blocked→todo)
            · stop(진행 중 작업을 단계 경계에서 중단하고 레포 일시정지) · deploy(fleet.yaml deploy.command 실행)
            · enhance(웹에 등록한 장문 요구사항을 승인 대기 백로그로 분해)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

VALID_ACTIONS = ("run", "discover", "merge", "approve", "retry", "stop", "deploy", "enhance", "bootstrap")
TICKET_ACTIONS = ("approve", "retry", "enhance", "bootstrap")          # 티켓/프로젝트 ID 필수
KEEP_DONE = 60
_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def requests_dir(state_dir: Path) -> Path:
    return state_dir / "requests"


def done_dir(state_dir: Path) -> Path:
    return state_dir / "requests" / "done"


def paused_dir(state_dir: Path) -> Path:
    return state_dir / "paused"


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data["id"] = path.stem
    return data


def create_request(state_dir: Path, repo: str, action: str, ticket: str | None = None, note: str | None = None, source: str = "web") -> dict[str, Any]:
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action {action!r} (use {VALID_ACTIONS})")
    if action in TICKET_ACTIONS and not ticket:
        raise ValueError(f"action {action!r} requires a ticket id")
    repo = _NAME.sub("", repo)
    ticket = ticket.upper() if ticket else None
    if not repo:
        raise ValueError("repo required")
    dup = pending_for(state_dir, repo, action, ticket)
    if dup:
        return dup
    now = time.time()
    rid = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{int((now % 1) * 1000):03d}-{repo}-{action}" + (f"-{ticket}" if ticket else "")
    data = {"repo": repo, "action": action, "ticket": ticket, "note": note, "source": source, "status": "pending",
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))}
    _write(requests_dir(state_dir) / f"{rid}.json", data)
    return {"id": rid, **data}


def list_pending(state_dir: Path) -> list[dict[str, Any]]:
    d = requests_dir(state_dir)
    if not d.is_dir():
        return []
    out = [r for r in (_read(p) for p in sorted(d.glob("*.json"))) if r]
    return out


def list_done(state_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    d = done_dir(state_dir)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [r for r in (_read(p) for p in files) if r]


def pending_for(state_dir: Path, repo: str, action: str | None = None, ticket: str | None = None) -> dict[str, Any] | None:
    for r in list_pending(state_dir):
        if r.get("repo") == repo and (action is None or r.get("action") == action) and (ticket is None or r.get("ticket") == ticket):
            return r
    return None


def next_pending(state_dir: Path) -> dict[str, Any] | None:
    for r in list_pending(state_dir):
        if r.get("status") == "pending":
            return r
    return None


def mark_running(state_dir: Path, req: dict[str, Any]) -> None:
    path = requests_dir(state_dir) / f"{req['id']}.json"
    data = {k: v for k, v in req.items() if k != "id"}
    data.update(status="running", started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _write(path, data)


def complete(state_dir: Path, req: dict[str, Any], status: str, detail: str = "") -> dict[str, Any]:
    assert status in ("done", "failed", "skipped")
    src = requests_dir(state_dir) / f"{req['id']}.json"
    data = {k: v for k, v in req.items() if k != "id"}
    data.update(status=status, detail=detail[:2000], finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _write(done_dir(state_dir) / f"{req['id']}.json", data)
    try:
        src.unlink()
    except OSError:
        pass
    _prune(state_dir)
    return {"id": req["id"], **data}


def cancel(state_dir: Path, request_id: str) -> bool:
    rid = _NAME.sub("", request_id)
    path = requests_dir(state_dir) / f"{rid}.json"
    req = _read(path)
    if not req or req.get("status") != "pending":
        return False
    complete(state_dir, req, "skipped", "cancelled")
    return True


def _prune(state_dir: Path) -> None:
    d = done_dir(state_dir)
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_DONE:]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------- 레포별 일시정지 ----------

def is_repo_paused(state_dir: Path, repo: str) -> bool:
    return (paused_dir(state_dir) / _NAME.sub("", repo)).exists()


def paused_repos(state_dir: Path) -> list[str]:
    d = paused_dir(state_dir)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def set_repo_paused(state_dir: Path, repo: str, paused: bool, note: str = "") -> bool:
    path = paused_dir(state_dir) / _NAME.sub("", repo)
    if paused:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((note or f"paused at {time.strftime('%Y-%m-%d %H:%M:%S')}") + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
    return paused
