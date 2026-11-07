"""작동 시간대 계산: 'HH:MM-HH:MM' (자정 넘는 구간 허용), 종료 전 여유 시간."""
from __future__ import annotations

import datetime as dt
import re

_HOURS = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(spec: str | None) -> tuple[dt.time, dt.time] | None:
    if not spec or not str(spec).strip():
        return None
    m = _HOURS.match(str(spec))
    if not m:
        raise ValueError(f"active_hours must look like 'HH:MM-HH:MM', got {spec!r}")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    return dt.time(h1, m1), dt.time(h2, m2)


def parse_clock(spec: str | None) -> dt.time | None:
    if not spec or not str(spec).strip():
        return None
    h, m = (int(x) for x in str(spec).split(":"))
    return dt.time(h, m)


def is_active(now: dt.time, window: tuple[dt.time, dt.time] | None) -> bool:
    if window is None:
        return True
    start, end = window
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def minutes_until_end(now: dt.time, window: tuple[dt.time, dt.time] | None) -> float:
    """시간대 종료까지 남은 분. 시간대 밖이면 0, 항상 활성이면 무한대."""
    if window is None:
        return float("inf")
    if not is_active(now, window):
        return 0.0
    end = window[1]
    cur = now.hour * 60 + now.minute + now.second / 60
    fin = end.hour * 60 + end.minute
    if fin < cur:
        fin += 24 * 60
    return fin - cur


def can_start_work(now: dt.time, window: tuple[dt.time, dt.time] | None, margin_minutes: int) -> bool:
    """새 티켓/발굴을 시작해도 되는가: 시간대 안이고 종료까지 margin 이상 남았을 때."""
    return is_active(now, window) and minutes_until_end(now, window) > margin_minutes
