"""FastAPI 앱: /api/* JSON + 정적 SPA(/). 쓰기는 fleet.yaml 편집(레포 선택/승인/스케줄/달력)과 요청 큐, 그리고 Jev 레포 판정(POST /api/review) 이다.

기동(startup) 때 fleet.yaml 이 있고 review.on_start(기본 true, HERMES_REVIEW=skip 으로 끔)면 Jev 판정을 백그라운드 스레드로 한 번 돌린다 —
Docker 컨테이너가 (재)기동될 때마다 다른 레포의 변경을 보고 프로젝트로 진행할지 다시 판단하는 지점이다.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..fleetfile import FleetFileError
from .. import review as rv
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


class ReviewBody(BaseModel):
    dry_run: bool = False
    trigger: str = "manual"


class ScheduleBody(BaseModel):
    active_hours: str | None = None
    merge_at: str | None = None
    no_new_work_minutes_before_end: int | None = None
    poll_seconds: int | None = None
    endpoint_retry_seconds: int | None = None


class EnhancementBody(BaseModel):
    repo: str
    brief: str
    title: str = ""

class ProjectBody(BaseModel):
    name: str
    description: str

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(collector: Collector | None = None) -> FastAPI:
    col = collector or Collector.from_env()
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 기동 직후: Docker 컨테이너가 (재)기동될 때 다른 레포의 변경을 보고 프로젝트로 진행할지 Jev 로 다시 판정한다(백그라운드 스레드).
        try:
            if col.initialized() and rv.on_start_enabled(col.config()):
                print("[review] 기동 시 Jev 레포 판정을 백그라운드로 시작합니다 (HERMES_REVIEW=skip 또는 fleet.yaml review.on_start: false 로 끔)", flush=True)
                col.start_review_async("start")
        except Exception as exc:  # noqa: BLE001 — 모니터 기동을 막지 않는다
            print(f"[review] 기동 시 판정 시작 실패: {exc}", flush=True)
        yield

    app = FastAPI(title="Hermes Fleet Monitor", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.collector = col

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "fleet_dir": str(col.paths.fleet_dir), "config_exists": col.paths.config.exists()}

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        return col.overview()

    @app.get("/api/system")
    def system_metrics() -> dict[str, Any]:
        """CPU/RAM 및 NVIDIA GPU 사용률의 현재값과 모니터 실행 중 이력."""
        return col.system_metrics()

    @app.get("/api/llm-usage")
    def llm_usage() -> dict[str, Any]:
        return col.llm_usage()

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

    @app.get("/api/enhancements")
    def enhancements(repo: str | None = None) -> list[dict[str, Any]]:
        return col.enhancements(repo)

    @app.get("/api/enhancements/{enhancement_id}")
    def enhancement(enhancement_id: str) -> dict[str, Any]:
        try:
            return col.enhancement(enhancement_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/enhancements")
    def enhancement_create(body: EnhancementBody) -> dict[str, Any]:
        """요구사항 원문을 저장하고 on-demand 데몬이 백로그로 분해할 요청을 큐에 넣는다."""
        try:
            return col.create_enhancement(body.repo, body.brief, body.title)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        return col.projects()

    @app.post("/api/projects")
    def project_create(body: ProjectBody) -> dict[str, Any]:
        try:
            return col.create_project(body.name, body.description)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

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

    @app.get("/api/review")
    def review_status() -> dict[str, Any]:
        """마지막 Jev 레포 판정(state/review.json)과 설정·실행 중 여부."""
        return col.review_status()

    @app.post("/api/review")
    def review_run(body: ReviewBody | None = None) -> dict[str, Any]:
        """지금 Jev 판정을 돌린다(동기, 레포당 한 번 호출). dry_run=true 면 fleet.yaml 을 고치지 않는다."""
        body = body or ReviewBody()
        try:
            return col.run_review(trigger=body.trigger or "manual", dry_run=body.dry_run)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
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
