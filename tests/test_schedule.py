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
