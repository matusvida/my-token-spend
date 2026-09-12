import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
CONFIG["timezone"] = "Europe/Prague"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_midweek_timestamp_maps_to_preceding_saturday():
    assert collect.window_start(ts("2026-08-25T10:00:00Z"), CONFIG).isoformat() == "2026-08-22"


def test_saturday_after_local_reset_starts_new_window():
    assert collect.window_start(ts("2026-08-29T06:00:00Z"), CONFIG).isoformat() == "2026-08-29"


def test_utc_timestamp_before_midnight_is_already_next_window_in_local_time():
    assert collect.window_start(ts("2026-08-28T23:30:00Z"), CONFIG).isoformat() == "2026-08-29"


def test_utc_timestamp_still_friday_locally_stays_in_old_window():
    assert collect.window_start(ts("2026-08-28T21:00:00Z"), CONFIG).isoformat() == "2026-08-22"


def test_exact_local_reset_instant_belongs_to_new_window():
    assert collect.window_start(ts("2026-08-28T22:00:00Z"), CONFIG).isoformat() == "2026-08-29"


def test_one_second_before_local_reset_belongs_to_old_window():
    assert collect.window_start(ts("2026-08-28T21:59:59Z"), CONFIG).isoformat() == "2026-08-22"


def test_winter_offset_is_one_hour_not_two():
    assert collect.window_start(ts("2026-01-02T22:30:00Z"), CONFIG).isoformat() == "2025-12-27"
    assert collect.window_start(ts("2026-01-02T23:30:00Z"), CONFIG).isoformat() == "2026-01-03"


def test_window_key_formats_underscored_date():
    assert collect.window_key(collect.window_start(ts("2026-08-25T10:00:00Z"), CONFIG)) == "week_2026_08_22"


def test_session_straddling_reset_splits_across_two_windows():
    records = [
        {"ts": "2026-08-28T20:00:00Z", "sessionId": "s1", "weighted": 100.0},
        {"ts": "2026-08-28T23:30:00Z", "sessionId": "s1", "weighted": 200.0},
        {"ts": "2026-08-29T09:00:00Z", "sessionId": "s1", "weighted": 300.0},
    ]
    buckets = collect.bucket_records(records, CONFIG)
    assert sorted(buckets) == ["2026-08-22", "2026-08-29"]
    assert [r["weighted"] for r in buckets["2026-08-22"]] == [100.0]
    assert [r["weighted"] for r in buckets["2026-08-29"]] == [200.0, 300.0]


def test_an_unset_timezone_falls_back_to_the_machines_own_zone():
    local = dict(CONFIG, timezone=None)
    assert isinstance(collect.zone(local), collect.SystemZone)
    assert collect.zone_label(local).startswith("local time (")


def test_a_configured_timezone_still_wins_over_the_machines():
    assert collect.zone(CONFIG) is not None
    assert collect.zone_label(CONFIG) == "Europe/Prague"
    assert str(collect.zone(CONFIG)) == "Europe/Prague"


def test_the_machine_zone_is_not_a_single_frozen_offset():
    zone = collect.SystemZone()
    for moment in (datetime(2026, 1, 15, 12, 0), datetime(2026, 7, 15, 12, 0)):
        stamp = time.mktime(moment.timetuple())
        daylight = time.localtime(stamp).tm_isdst > 0
        expected = timedelta(seconds=-(time.altzone if daylight else time.timezone))
        assert zone.utcoffset(moment) == expected
        assert bool(zone.dst(moment)) == (daylight and time.daylight != 0)


def test_window_start_with_an_unset_timezone_lands_on_the_reset_weekday():
    local = dict(CONFIG, timezone=None)
    start = collect.window_start(ts("2026-08-25T10:00:00Z"), local)
    assert collect.WEEKDAYS[start.weekday()] == local["reset_weekday"]
