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
from .collect import Collector, parse_log_line


class Selection(BaseModel):
    selected: bool

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

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()
