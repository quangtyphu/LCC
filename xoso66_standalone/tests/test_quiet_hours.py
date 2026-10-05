# -*- coding: utf-8 -*-

from datetime import datetime

from xoso66_time_util import (
    VN_TZ,
    auto_bet_in_quiet_hours,
    vn_hour_in_quiet_range,
)


def test_vn_hour_in_quiet_range_same_day():
    assert vn_hour_in_quiet_range(2, 2, 8) is True
    assert vn_hour_in_quiet_range(7, 2, 8) is True
    assert vn_hour_in_quiet_range(8, 2, 8) is False
    assert vn_hour_in_quiet_range(1, 2, 8) is False


def test_vn_hour_in_quiet_range_overnight():
    assert vn_hour_in_quiet_range(23, 22, 6) is True
    assert vn_hour_in_quiet_range(5, 22, 6) is True
    assert vn_hour_in_quiet_range(12, 22, 6) is False


def test_auto_bet_in_quiet_hours_nested_config():
    acfg = {"quiet_hours": {"enabled": True, "start_hour": 2, "end_hour": 8}}
    at_3 = datetime(2026, 10, 5, 3, 30, tzinfo=VN_TZ)
    at_9 = datetime(2026, 10, 5, 9, 0, tzinfo=VN_TZ)
    assert auto_bet_in_quiet_hours(acfg, now=at_3) is True
    assert auto_bet_in_quiet_hours(acfg, now=at_9) is False
