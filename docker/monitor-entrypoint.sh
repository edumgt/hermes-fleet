#!/bin/sh
# 모니터 컨테이너 엔트리포인트: 기존 하네스 설정(fleet.yaml)이 있으면 그대로 쓰고, 없으면(받은 직후) 자동 init 뒤 모니터를 띄운다.
#   HERMES_INIT=auto|skip      (기본 auto) fleet.yaml 이 없을 때 init 실행 여부
#   HERMES_INIT_ARGS="..."     init 에 넘길 추가 인자 (예: --provider openai --base-url http://host.docker.internal:1234/v1 --model qwen2.5-coder:7b --include a,b)
set -eu
FLEET_DIR="${HERMES_FLEET_DIR:?HERMES_FLEET_DIR 가 필요합니다}"
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
