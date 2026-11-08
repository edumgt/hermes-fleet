#!/usr/bin/env bash
# Hermes Fleet 웹 모니터를 Docker 로 띄우고 내린다.
#   scripts/monitor.sh up        빌드 + 기동(백그라운드)
#   scripts/monitor.sh down      중지·삭제
#   scripts/monitor.sh restart   재시작(코드 변경 반영: 다시 빌드)
#   scripts/monitor.sh logs      컨테이너 로그 따라가기
#   scripts/monitor.sh status    상태·헬스체크
#   scripts/monitor.sh local     Docker 없이 uv 가상환경으로 실행 (uv sync 필요)
#   scripts/monitor.sh demo      가짜 fleet(scripts/demo_fleet.py)으로 미리보기 컨테이너 기동 → http://localhost:8790
#   scripts/monitor.sh demo-down 미리보기 컨테이너 제거
set -euo pipefail
FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
cd "$FLEET_DIR"
PORT="${MONITOR_PORT:-8787}"
BIND="${MONITOR_BIND:-127.0.0.1}"

case "${1:-up}" in
  up)
    docker compose up -d --build monitor
    echo "✓ monitor: http://${BIND}:${PORT}  (API 문서: /api/docs)"
    ;;
  down)
    docker compose down
    ;;
  restart)
    docker compose up -d --build --force-recreate monitor
    echo "✓ monitor restarted: http://${BIND}:${PORT}"
    ;;
  logs)
    docker compose logs -f --tail=200 monitor
    ;;
  status)
    docker compose ps monitor
    echo
    curl -fsS "http://${BIND}:${PORT}/api/health" && echo
    ;;
  demo)
    DEMO="${DEMO_DIR:-/tmp/hermes-fleet-demo}"
    python3 "$FLEET_DIR/scripts/demo_fleet.py" "$DEMO"
    docker compose build -q monitor
    docker rm -f hermes-fleet-monitor-demo >/dev/null 2>&1 || true
    docker run -d --name hermes-fleet-monitor-demo -p "${BIND}:${DEMO_PORT:-8790}:8787" \
      -v "$DEMO:$DEMO:rw" -e HERMES_FLEET_DIR="$DEMO/fleet" -e TZ="${TZ:-Asia/Seoul}" \
      --add-host host.docker.internal:host-gateway hermes-fleet-monitor:local >/dev/null
    echo "✓ demo monitor: http://${BIND}:${DEMO_PORT:-8790}  (데이터: $DEMO)"
    ;;
  demo-down)
    docker rm -f hermes-fleet-monitor-demo
    ;;
  local)
    shift || true
    exec uv run --project "$FLEET_DIR" --directory "$FLEET_DIR" python -m hermes_fleet.monitor --host "$BIND" --port "$PORT" "$@"
    ;;
  *)
    echo "usage: $0 {up|down|restart|logs|status|local|demo|demo-down}" >&2
    exit 2
    ;;
esac
