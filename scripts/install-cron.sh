#!/usr/bin/env bash
# fleet 데몬이 PC(WSL)가 켜져 있을 때 fleet.yaml 의 작동 시간대(active_hours) 동안 돌도록 cron 을 등록한다.
# - 하루 종일 10분마다 기동 시도. flock 으로 1개 인스턴스만. 시간대 판정은 cron 이 아니라 데몬이 fleet.yaml 의 active_hours 로 하며,
#   시간대 밖이면(요청도 없으면) 로그 없이 바로 종료한다. → 웹 모니터에서 시간대를 바꾸면 cron 재등록 없이 다음 10분 틱부터 적용된다.
# - PC 를 시간대 중간에 켜도 10분 안에 시작되고, 시간대가 끝나면 새 작업을 시작하지 않는다.
# 사용: scripts/install-cron.sh [--remove]
set -euo pipefail
FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARK="# hermes-fleet"
TZLINE="CRON_TZ=Asia/Seoul $MARK-tz"   # cron 의 분/시 판정도 한국 시간으로 (Debian cron 은 CRON_TZ 지원)
LINE="*/10 * * * * cd $FLEET_DIR && /usr/bin/flock -n $FLEET_DIR/state/daemon.lock $FLEET_DIR/scripts/hermes-fleet daemon >> $FLEET_DIR/logs/daemon.log 2>&1 $MARK"
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
printf '%s\n%s\n%s\n%s\n' "$current" "$TZLINE" "$LINE" "$LINE2" | sed '/^$/d' | crontab -
echo "✓ cron 등록:"
crontab -l | grep "$MARK"
echo "  로그: tail -f $FLEET_DIR/logs/daemon.log"
echo "  해제: $0 --remove"
