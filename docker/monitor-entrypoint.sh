#!/bin/sh
# 모니터 컨테이너 엔트리포인트: 기존 하네스 설정(fleet.yaml)이 있으면 그대로 쓰고, 없으면(받은 직후) 자동 init 뒤 모니터를 띄운다.
#   HERMES_INIT=auto|skip      (기본 auto) fleet.yaml 이 없을 때 init 실행 여부
#   HERMES_INIT_ARGS="..."     init 에 넘길 추가 인자 (예: --provider openai --base-url http://host.docker.internal:1234/v1 --model qwen2.5-coder:7b --include a,b)
set -eu
FLEET_DIR="${HERMES_FLEET_DIR:?HERMES_FLEET_DIR 가 필요합니다}"
HERMES_FLEET_DIR_ORIG="$FLEET_DIR"
# 보정: compose 의 ${PWD} 가 hermes-fleet 이 아닌 다른 폴더(예: 상위 폴더에서 `docker compose -f hermes-fleet/docker-compose.yml up`)로 풀린 경우,
# HERMES_FLEET_DIR 에는 fleet.example.yaml 이 없다. 그 아래(2단계까지, 글롭)에서 실제 hermes-fleet 폴더를 찾아 바꿔 끼운다.
if [ ! -f "$FLEET_DIR/fleet.example.yaml" ] && [ ! -f "$FLEET_DIR/fleet.yaml" ]; then
  found=""
  for c in "$FLEET_DIR"/hermes-fleet/fleet.example.yaml "$FLEET_DIR"/*/fleet.example.yaml "$FLEET_DIR"/*/hermes-fleet/fleet.example.yaml; do
    [ -f "$c" ] && { found="$c"; break; }
  done
  if [ -n "$found" ]; then
    FLEET_DIR="$(dirname "$found")"
    export HERMES_FLEET_DIR="$FLEET_DIR"
    echo "[entrypoint] HERMES_FLEET_DIR 보정: ${HERMES_FLEET_DIR_ORIG:-$FLEET_DIR} → $FLEET_DIR (compose 를 hermes-fleet 폴더에서, 또는 scripts/monitor.sh up 으로 실행하면 보정이 필요 없습니다)"
  else
    echo "[entrypoint] 경고: $FLEET_DIR 에 hermes-fleet 이 없습니다 — FLEET_DIR 환경변수로 hermes-fleet 경로를 지정하거나 scripts/monitor.sh up 을 쓰세요" >&2
  fi
fi
if [ -f "$FLEET_DIR/fleet.yaml" ]; then
  echo "[entrypoint] 기존 하네스 설정 사용: $FLEET_DIR/fleet.yaml"
elif [ "${HERMES_INIT:-auto}" = "skip" ]; then
  echo "[entrypoint] fleet.yaml 없음 — HERMES_INIT=skip 이라 초기화하지 않고 읽기 전용으로 띄웁니다 (웹 배너 안내)"
else
  echo "[entrypoint] fleet.yaml 없음 — 이 환경을 읽어 초기화합니다 (root = $(dirname "$FLEET_DIR"))"
  # shellcheck disable=SC2086
  if python -m hermes_fleet.setup --fleet-dir "$FLEET_DIR" ${HERMES_INIT_ARGS:-}; then
    echo "[entrypoint] 초기화 완료 → fleet.yaml / .env 생성. 호스트에서 데몬을 쓰려면 scripts/install-cron.sh 를 실행하세요."
  else
    echo "[entrypoint] 초기화 실패 — 읽기 전용으로 띄웁니다. 터미널에서 scripts/init.sh 로 다시 시도하세요." >&2
  fi
fi
exec "$@"
