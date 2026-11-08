#!/usr/bin/env bash
# 새벽 작동 시간대(기본 01:00-08:00) 동안만, PC(WSL)가 켜져 있을 때 fleet 데몬이 돌도록 cron 을 등록한다.
# - 01:00~07:59 사이 10분마다 기동 시도. flock 으로 1개 인스턴스만. 데몬은 시간대 밖이면 스스로 종료한다.
# - PC 를 03:00 에 켜도 10분 안에 시작되고, 08:00 이후에는 새 작업을 시작하지 않는다.
# 사용: scripts/install-cron.sh [--remove]
set -euo pipefail
FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARK="# hermes-fleet"
LINE="*/10 1-7 * * * cd $FLEET_DIR && /usr/bin/flock -n $FLEET_DIR/state/daemon.lock $FLEET_DIR/scripts/hermes-fleet daemon >> $FLEET_DIR/logs/daemon.log 2>&1 $MARK"
# 웹 모니터/CLI 의 요청(state/requests)을 시간대 밖에서도 2분 안에 처리하는 러너. 같은 flock 이라 야간 데몬과 겹치지 않는다. 요청이 없으면 조용히 종료.
LINE2="*/2 * * * * cd $FLEET_DIR && /usr/bin/flock -n $FLEET_DIR/state/daemon.lock $FLEET_DIR/scripts/hermes-fleet daemon --on-demand >> $FLEET_DIR/logs/daemon.log 2>&1 $MARK-ondemand"

mkdir -p "$FLEET_DIR/state" "$FLEET_DIR/logs"
current="$(crontab -l 2>/dev/null | grep -v "$MARK" || true)"   # $MARK 로 끝나는 줄과 $MARK-ondemand 줄 모두 제거
if [ "${1:-}" = "--remove" ]; then
  printf '%s\n' "$current" | crontab -
  echo "✓ hermes-fleet cron 항목 제거"
  exit 0
fi
if ! pgrep -x cron >/dev/null 2>&1; then
  echo "⚠ cron 데몬이 실행 중이 아닙니다. 'sudo service cron start' 후 다시 실행하세요." >&2
fi
printf '%s\n%s\n%s\n' "$current" "$LINE" "$LINE2" | sed '/^$/d' | crontab -
echo "✓ cron 등록:"
crontab -l | grep "$MARK"
echo "  로그: tail -f $FLEET_DIR/logs/daemon.log"
echo "  해제: $0 --remove"
