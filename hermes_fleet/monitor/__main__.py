"""python -m hermes_fleet.monitor [--host H] [--port P] [--reload]"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-fleet monitor", description="Hermes Fleet 웹 모니터(읽기 전용)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)
    try:
        import uvicorn  # noqa: WPS433
    except ImportError:
        print("✗ uvicorn/fastapi 가 없습니다: uv sync --extra monitor  (또는 docker compose up -d)", file=sys.stderr)
        return 1
    uvicorn.run("hermes_fleet.monitor.app:app", host=args.host, port=args.port, reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
