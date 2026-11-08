"""FastAPI 앱: /api/* JSON + 정적 SPA(/). 쓰기는 레포 선택/해제(PUT /api/repos/{name}/selection → fleet.yaml) 하나뿐이다."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..fleetfile import FleetFileError
from .collect import Collector, parse_log_line, tail_lines


class Selection(BaseModel):
    selected: bool


class Paused(BaseModel):
    paused: bool


class RequestBody(BaseModel):
    action: str
    ticket: str | None = None


class ApprovalBody(BaseModel):
    mode: str   # auto | manual | default(레포 예외 삭제)


class CalendarBody(BaseModel):
    entries: dict[str, str | None]


class ScheduleBody(BaseModel):
    active_hours: str | None = None
    merge_at: str | None = None
    no_new_work_minutes_before_end: int | None = None
    poll_seconds: int | None = None
    endpoint_retry_seconds: int | None = None

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(collector: Collector | None = None) -> FastAPI:
    col = collector or Collector.from_env()
    app = FastAPI(title="Hermes Fleet Monitor", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.collector = col

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "fleet_dir": str(col.paths.fleet_dir), "config_exists": col.paths.config.exists()}

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        return col.overview()

    @app.get("/api/repos")
    def repos(all: bool = Query(True, description="제외/비대상 레포 포함")) -> list[dict[str, Any]]:  # noqa: A002
        return col.repos(include_ineligible=all)

    @app.get("/api/repos/{name}")
    def repo(name: str) -> dict[str, Any]:
        data = col.repo_detail(name)
        if data is None:
            raise HTTPException(404, f"repo not found: {name}")
        return data

    @app.put("/api/repos/{name}/selection")
    def selection(name: str, body: Selection) -> dict[str, Any]:
        try:
            return col.set_selected(name, body.selected)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except FleetFileError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/repos/{name}/approval")
    def repo_approval(name: str, body: ApprovalBody) -> dict[str, Any]:
        try:
            return col.set_repo_approval(name, body.mode)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, FleetFileError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/approval")
    def default_approval(body: ApprovalBody) -> dict[str, Any]:
        try:
            return col.set_default_approval(body.mode)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, FleetFileError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/repos/{name}/pause")
    def repo_pause(name: str, body: Paused) -> dict[str, Any]:
        try:
            return col.set_repo_paused(name, body.paused)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/repos/{name}/requests")
    def repo_request(name: str, body: RequestBody) -> dict[str, Any]:
        try:
            return col.create_request(name, body.action, body.ticket)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/requests")
    def requests_list() -> dict[str, Any]:
        return col.requests()

    @app.delete("/api/requests/{request_id}")
    def request_cancel(request_id: str) -> dict[str, Any]:
        try:
            ok = col.cancel_request(request_id)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not ok:
            raise HTTPException(404, "pending request not found")
        return {"cancelled": request_id}

    @app.put("/api/pause")
    def pause_all(body: Paused) -> dict[str, Any]:
        try:
            return col.set_paused_all(body.paused)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.put("/api/schedule")
    def schedule(body: ScheduleBody) -> dict[str, Any]:
        try:
            return col.update_schedule(body.model_dump())
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except FleetFileError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/calendar")
    def calendar(body: CalendarBody) -> dict[str, Any]:
        """날짜별 시간대 예외 저장: entries = {"2026-11-09": "13:00-18:00", "2026-11-10": "off", "2026-11-11": null(삭제)}"""
        try:
            return col.update_calendar(body.entries)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (FleetFileError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/workflow")
    def workflow(ci: bool = Query(False, description="GitHub Actions 실행 상태도 조회(캐시 2분)")) -> dict[str, Any]:
        return col.workflow_board(with_ci=ci)

    @app.get("/api/stats")
    def stats(days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
        return col.stats(days)

    @app.get("/api/repos/{name}/tickets/{ticket_id}")
    def ticket(name: str, ticket_id: str) -> dict[str, Any]:
        data = col.ticket_detail(name, ticket_id)
        if data is None:
            raise HTTPException(404, f"ticket not found: {name}/{ticket_id}")
        return data

    @app.get("/api/repos/{name}/tickets/{ticket_id}/artifacts/{filename}")
    def artifact(name: str, ticket_id: str, filename: str) -> dict[str, Any]:
        data = col.artifact(name, ticket_id, filename)
        if data is None:
            raise HTTPException(404, f"artifact not found: {filename}")
        return data

    @app.get("/api/timeline")
    def timeline(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return col.timeline(limit)

    @app.get("/api/logs")
    def logs(lines: int = Query(300, ge=1, le=5000), repo: str | None = None, q: str | None = None) -> dict[str, Any]:
        return col.log_tail(lines, repo or None, q or None)

    @app.get("/api/logs/stream")
    async def logs_stream(request: Request, tail: int = Query(200, ge=0, le=2000)) -> StreamingResponse:
        async def gen():
            offset = col.log_size()
            initial = col.log_tail(tail)["lines"] if tail else []
            yield "event: init\ndata: " + json.dumps({"lines": initial, "size": offset}, ensure_ascii=False) + "\n\n"
            idle = 0
            while not await request.is_disconnected():
                offset, new = await asyncio.to_thread(col.log_read_from, offset)
                if new:
                    idle = 0
                    payload = [parse_log_line(line) for line in new]
                    yield "event: lines\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": keepalive\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/crew")
    def crew(repo: str | None = None, limit: int = Query(300, ge=1, le=3000)) -> dict[str, Any]:
        return col.crew(repo or None, limit)

    @app.get("/api/crew/stream")
    async def crew_stream(request: Request, repo: str | None = None, tail: int = Query(300, ge=0, le=3000)) -> StreamingResponse:
        """init: 스냅샷(rooms + 최근 메시지) → 이후 daemon.log 새 줄을 파서에 이어 넣어 'messages' 이벤트(새 메시지 + 갱신된 rooms)로 보낸다."""
        async def gen():
            offset = col.log_size()
            parser = col.crew_parser()
            lines, _ = tail_lines(col.paths.daemon_log, col.CREW_TAIL_LINES, col.CREW_TAIL_BYTES)
            parser.feed(lines)
            snap = col.crew(repo or None, tail, parser=parser)
            yield "event: init\ndata: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
            idle = 0
            while not await request.is_disconnected():
                offset, new = await asyncio.to_thread(col.log_read_from, offset)
                if new:
                    idle = 0
                    fresh = parser.feed(new)
                    if repo:
                        fresh = [m for m in fresh if m["repo"] in (repo, None)]
                    snap = col.crew(repo or None, 0, parser=parser)
                    yield "event: messages\ndata: " + json.dumps({"messages": fresh, "rooms": snap["rooms"], "daemon": snap["daemon"], "now": snap["now"]}, ensure_ascii=False) + "\n\n"
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": keepalive\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.middleware("http")
    async def no_heuristic_cache(request: Request, call_next):
        """정적 파일(index/app.js/style.css/crew3d.js)에 Cache-Control 이 없으면 브라우저가 Last-Modified 기반으로 며칠씩 캐시해
        새 배포 뒤에도 옛 CSS/JS 가 남는다(달력이 세로로 늘어진 화면). 매번 재검증(ETag → 304)하게 한다."""
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()
