import datetime as dt

import pytest

from hermes_fleet.schedule import can_start_work, is_active, minutes_until_end, parse_clock, parse_window


def test_window_01_to_08():
    w = parse_window("01:00-08:00")
    assert is_active(dt.time(1, 0), w) and is_active(dt.time(7, 59), w)
    assert not is_active(dt.time(0, 59), w) and not is_active(dt.time(8, 0), w) and not is_active(dt.time(13, 0), w)


def test_window_over_midnight_and_always():
    w = parse_window("22:00-06:00")
    assert is_active(dt.time(23, 0), w) and is_active(dt.time(3, 0), w) and not is_active(dt.time(12, 0), w)
    assert is_active(dt.time(12, 0), parse_window("")) and parse_window(None) is None


def test_margin_before_end():
    w = parse_window("01:00-08:00")
    assert minutes_until_end(dt.time(7, 0), w) == pytest.approx(60)
    assert can_start_work(dt.time(7, 0), w, 45)
    assert not can_start_work(dt.time(7, 20), w, 45)
    assert not can_start_work(dt.time(9, 0), w, 45)
    assert can_start_work(dt.time(9, 0), None, 45)


def test_parse_clock_and_bad_spec():
    assert parse_clock("07:30") == dt.time(7, 30) and parse_clock("") is None
    with pytest.raises(ValueError):
        parse_window("1am-8am")


# ---------- 날짜별 시간대(달력) ----------
from hermes_fleet.schedule import Schedule  # noqa: E402


def test_calendar_override_off_and_spillover():
    sch = Schedule("01:00-08:00", {"2026-11-10": "13:00-18:00", "2026-11-11": "off", "2026-11-12": "21:00-03:00"})
    # 11-09: 예외 없음 → 기본
    assert sch.is_active(dt.datetime(2026, 11, 9, 2, 0)) and not sch.is_active(dt.datetime(2026, 11, 9, 13, 0))
    # 11-10: 13-18시만
    assert not sch.is_active(dt.datetime(2026, 11, 10, 2, 0)) and sch.is_active(dt.datetime(2026, 11, 10, 14, 0)) and not sch.is_active(dt.datetime(2026, 11, 10, 18, 0))
    assert sch.minutes_until_end(dt.datetime(2026, 11, 10, 17, 0)) == pytest.approx(60) and sch.can_start_work(dt.datetime(2026, 11, 10, 17, 0), 45)
    # 11-11: 쉼 → 종일 비활성, 다음 시작은 11-12 21:00
    assert not sch.is_active(dt.datetime(2026, 11, 11, 2, 0)) and not sch.is_active(dt.datetime(2026, 11, 11, 14, 0))
    assert sch.next_start(dt.datetime(2026, 11, 11, 9, 0)) == dt.datetime(2026, 11, 12, 21, 0)
    # 11-12 21:00-03:00: 12일 밤과 13일 새벽(spill-over) 모두 활성, 13일 새벽 01:00 은 기본(01-08)이 아니라 전날 창의 연속으로 03:00 에 끝난다
    assert sch.is_active(dt.datetime(2026, 11, 12, 22, 0)) and sch.is_active(dt.datetime(2026, 11, 13, 2, 30))
    assert sch.minutes_until_end(dt.datetime(2026, 11, 13, 2, 30)) == pytest.approx(30)
    assert sch.window_on(dt.date(2026, 11, 13)) == ("default", (dt.time(1, 0), dt.time(8, 0)))
    assert sch.is_active(dt.datetime(2026, 11, 13, 5, 0))       # 03:00 이후엔 13일 자체 기본 창(01-08)
    assert sch.next_start(dt.datetime(2026, 11, 12, 9, 0)) == dt.datetime(2026, 11, 12, 21, 0)
    assert sch.calendar_specs() == {"2026-11-10": "13:00-18:00", "2026-11-11": "off", "2026-11-12": "21:00-03:00"}
    days = sch.days(dt.date(2026, 11, 10), 3)
    assert [d["kind"] for d in days] == ["override", "off", "override"] and days[0]["spec"] == "13:00-18:00"


def test_calendar_always_default_and_bad_values():
    sch = Schedule("", {"2026-11-11": "off"})
    assert sch.is_active(dt.datetime(2026, 11, 10, 23, 59)) and sch.minutes_until_end(dt.datetime(2026, 11, 10, 12, 0)) == float("inf")
    assert not sch.is_active(dt.datetime(2026, 11, 11, 12, 0)) and sch.next_start(dt.datetime(2026, 11, 11, 12, 0)) == dt.datetime(2026, 11, 12, 0, 0)
    with pytest.raises(ValueError):
        Schedule("01:00-08:00", {"2026-11-11": "1pm-5pm"})
    with pytest.raises(ValueError):
        Schedule("01:00-08:00", {"11/11": "13:00-18:00"})



def test_process_timezone_is_kst(monkeypatch):
    """모든 날짜·시간은 한국 시간: 패키지를 임포트하면 호스트 TZ 와 무관하게 프로세스 시간대가 Asia/Seoul 이다."""
    import os
    import time

    import hermes_fleet.settings  # noqa: F401  (임포트 시 TZ 고정)

    assert os.environ["TZ"] == "Asia/Seoul"
    assert time.strftime("%z", time.localtime()) == "+0900"
    assert dt.datetime.now().astimezone().utcoffset() == dt.timedelta(hours=9)
