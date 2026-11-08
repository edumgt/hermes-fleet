#!/usr/bin/env bash
# (선택) systemd 가 켜진 WSL/Linux 에서 부팅 시 자동 시작. systemd 가 꺼져 있으면 install-cron.sh 를 쓰세요.
set -euo pipefail
FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="$(command -v uv || echo /root/.local/bin/uv)"
[ "$(id -u)" -eq 0 ] || { echo "root로 실행하세요 (sudo $0)"; exit 1; }
STATE="$(systemctl is-system-running 2>/dev/null || true)"
if [ "$STATE" != "running" ] && [ "$STATE" != "degraded" ]; then
  echo "systemd 가 활성화되어 있지 않습니다 (state: ${STATE:-none}). /etc/wsl.conf 에 [boot] systemd=true 를 넣고 'wsl --shutdown' 후 재실행하거나, scripts/install-cron.sh 를 사용하세요."
  exit 1
fi
sed -e "s#/opt/hermes-fleet#$FLEET_DIR#g" -e "s#/root/.local/bin/uv#$UV_BIN#g" "$FLEET_DIR/systemd/hermes-fleet.service" > /etc/systemd/system/hermes-fleet.service
mkdir -p "$FLEET_DIR/state" "$FLEET_DIR/logs"
systemctl daemon-reload
systemctl enable --now hermes-fleet.service
echo "✓ hermes-fleet 서비스 설치·시작 (상태: systemctl status hermes-fleet)"
