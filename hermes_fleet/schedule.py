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


# ---------- 날짜별 시간대(달력) ----------
OFF = "off"
_OFF_WORDS = {"off", "none", "쉼", "휴무", "x", "-"}


def parse_calendar(raw: dict | None) -> dict[dt.date, tuple[dt.time, dt.time] | None]:
    """fleet.yaml calendar: {"2026-11-09": "13:00-18:00", "2026-11-10": off} → {date: window | None(쉬는 날)}. 형식이 틀리면 ValueError."""
    out: dict[dt.date, tuple[dt.time, dt.time] | None] = {}
    for key, value in (raw or {}).items():
        day = key if isinstance(key, dt.date) else dt.date.fromisoformat(str(key).strip())
        if value is None or value is False or str(value).strip().lower() in _OFF_WORDS or str(value).strip() == "":
            out[day] = None
        else:
            out[day] = parse_window(str(value))
    return out


def window_spec(window: tuple[dt.time, dt.time] | None) -> str | None:
    return None if window is None else f"{window[0].hour:02d}:{window[0].minute:02d}-{window[1].hour:02d}:{window[1].minute:02d}"


class Schedule:
    """기본 시간대(active_hours) + 날짜별 예외(calendar)로 '지금 작동 중인가' 를 판정한다.

    - 날짜 항목의 시간대는 그 날짜에 시작하는 창이다. 자정을 넘는 창(21:00-03:00)은 다음 날 새벽까지 이어진다(spill-over).
    - 날짜 항목이 off 면 그 날은 새 창을 열지 않는다(전날 창의 새벽 이어짐은 그대로 끝까지 간다).
    - 항목이 없는 날은 기본 시간대를 따른다. 기본이 비어 있으면(항상) 그 날은 종일 작동.
    """

    def __init__(self, default: str | None, calendar: dict | None = None) -> None:
        self.default_spec = (default or "").strip() or None
        self.default = parse_window(self.default_spec)
        self.calendar = parse_calendar(calendar)

    def window_on(self, day: dt.date) -> tuple[str, tuple[dt.time, dt.time] | None]:
        """('override' | 'off' | 'default' | 'always', window). always = 기본이 비어 있고 예외도 없는 날."""
        if day in self.calendar:
            w = self.calendar[day]
            return ("off", None) if w is None else ("override", w)
        return ("default", self.default) if self.default else ("always", None)

    def _entry(self, now: dt.datetime) -> tuple[dt.date, tuple[dt.time, dt.time] | None] | None:
        """지금 열려 있는 창 (시작 날짜, window). window None = 종일. 없으면 None."""
        t, today = now.time(), now.date()
        kind_y, wy = self.window_on(today - dt.timedelta(days=1))
        if wy and wy[0] > wy[1] and t < wy[1]:
            return today - dt.timedelta(days=1), wy
        kind, w = self.window_on(today)
        if kind == "off":
            return None
        if kind == "always":
            return today, None
        if w[0] <= w[1]:
            return (today, w) if w[0] <= t < w[1] else None
        return (today, w) if t >= w[0] else None

    def is_active(self, now: dt.datetime) -> bool:
        return self._entry(now) is not None

    def minutes_until_end(self, now: dt.datetime) -> float:
        e = self._entry(now)
        if e is None:
            return 0.0
        day, w = e
        if w is None:
            return float("inf")
        end = dt.datetime.combine(day + dt.timedelta(days=1 if w[0] > w[1] else 0), w[1])
        return max(0.0, (end - now).total_seconds() / 60)

    def can_start_work(self, now: dt.datetime, margin_minutes: int) -> bool:
        return self.is_active(now) and self.minutes_until_end(now) > margin_minutes

    def next_start(self, now: dt.datetime, horizon_days: int = 60) -> dt.datetime | None:
        """지금 닫혀 있을 때 다음 창이 열리는 시각. 열려 있으면 None."""
        if self.is_active(now):
            return None
        for i in range(horizon_days + 1):
            day = now.date() + dt.timedelta(days=i)
            kind, w = self.window_on(day)
            if kind == "off":
                continue
            cand = dt.datetime.combine(day, w[0] if w else dt.time(0, 0))
            if cand > now:
                return cand
        return None

    def days(self, start: dt.date, count: int) -> list[dict]:
        """달력 표시용: [{date, kind, spec}]"""
        out = []
        for i in range(count):
            day = start + dt.timedelta(days=i)
            kind, w = self.window_on(day)
            out.append({"date": day.isoformat(), "kind": kind, "spec": window_spec(w)})
        return out

    def calendar_specs(self) -> dict[str, str]:
        return {d.isoformat(): (window_spec(w) or OFF) for d, w in sorted(self.calendar.items())}
