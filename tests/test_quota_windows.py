import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect
import quota

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
CONFIG["timezone"] = "Europe/Prague"

SEP_19 = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
WEEK = timedelta(days=7)


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def sample(resets_at, at="2026-09-12T17:00:00+00:00", pct=3.0, weighted=0.0):
    return {
        "ts": at,
        "seven_day_pct": pct,
        "seven_day_resets_at": resets_at,
        "weighted_so_far": weighted,
    }


ONE = [SEP_19]


def test_a_midweek_moment_lands_in_the_window_that_started_at_the_last_reset():
    assert quota.window_instant(ts("2026-09-15T10:00:00Z"), ONE) == SEP_19 - WEEK


def test_the_reset_instant_itself_opens_the_new_window():
    assert quota.window_instant(SEP_19, ONE) == SEP_19


def test_one_second_before_the_reset_is_still_the_old_window():
    assert quota.window_instant(SEP_19 - timedelta(seconds=1), ONE) == SEP_19 - WEEK


def test_a_moment_after_the_last_known_reset_steps_forward_by_seven_days():
    assert quota.window_instant(SEP_19 + timedelta(days=9), ONE) == SEP_19 + WEEK
    assert quota.window_instant(SEP_19 + timedelta(days=30), ONE) == SEP_19 + 4 * WEEK


def test_a_moment_before_the_first_known_reset_steps_backward_by_seven_days():
    assert quota.window_instant(SEP_19 - timedelta(days=9), ONE) == SEP_19 - 2 * WEEK
    assert quota.window_instant(SEP_19 - timedelta(days=30), ONE) == SEP_19 - 5 * WEEK


def test_a_gap_between_known_instants_still_cuts_weekly_windows():
    instants = [SEP_19 - 2 * WEEK, SEP_19]
    assert quota.window_instant(SEP_19 - timedelta(days=4), instants) == SEP_19 - WEEK
    assert quota.window_instant(SEP_19 - timedelta(days=10), instants) == SEP_19 - 2 * WEEK


def test_a_shifted_reset_closes_the_previous_window_early():
    shifted = SEP_19 + timedelta(days=3)
    instants = [SEP_19, shifted]
    assert quota.window_end(SEP_19, instants) == shifted
    assert quota.window_instant(SEP_19 + timedelta(days=2), instants) == SEP_19
    assert quota.window_instant(shifted, instants) == shifted
    assert quota.window_end(shifted, instants) == shifted + WEEK


def test_an_unshifted_window_is_exactly_seven_days():
    assert quota.window_end(SEP_19, ONE) == SEP_19 + WEEK


def test_no_instants_means_no_answer():
    assert quota.window_instant(SEP_19, []) is None


def test_window_start_uses_the_reset_instant_when_samples_exist():
    instants = quota.reset_instants([sample("2026-09-19T03:00:00.046991+00:00")])
    assert collect.window_start(ts("2026-09-15T10:00:00Z"), CONFIG, instants).isoformat() == "2026-09-12"


def test_window_start_without_samples_falls_back_to_the_configured_weekday():
    assert collect.window_start(ts("2026-09-15T10:00:00Z"), CONFIG, []).isoformat() == "2026-09-12"
    assert collect.window_start(ts("2026-09-15T10:00:00Z"), CONFIG, None).isoformat() == "2026-09-12"


def test_the_real_boundary_differs_from_the_configured_one_by_the_five_hour_offset():
    instants = [SEP_19]
    late_friday = ts("2026-09-18T23:00:00Z")
    assert collect.window_start(late_friday, CONFIG, None).isoformat() == "2026-09-19"
    assert collect.window_start(late_friday, CONFIG, instants).isoformat() == "2026-09-12"


def test_the_cut_is_the_same_utc_instant_across_a_dst_change():
    instants = [datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)]
    before = collect.window_bounds(collect.window_start(ts("2026-10-26T12:00:00Z"), CONFIG, instants), CONFIG, instants)
    after = collect.window_bounds(collect.window_start(ts("2026-11-02T12:00:00Z"), CONFIG, instants), CONFIG, instants)
    assert before[0] == datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
    assert after[0] == datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)
    assert after[0] - before[0] == WEEK


def test_the_configured_cut_drifts_by_an_hour_across_the_same_dst_change():
    before = collect.window_bounds(collect.window_start(ts("2026-10-26T12:00:00Z"), CONFIG, None), CONFIG, None)
    after = collect.window_bounds(collect.window_start(ts("2026-11-02T12:00:00Z"), CONFIG, None), CONFIG, None)
    assert after[0] - before[0] == WEEK + timedelta(hours=1)


def test_window_bounds_report_where_the_boundary_came_from():
    instants = [SEP_19]
    start = collect.window_start(ts("2026-09-20T12:00:00Z"), CONFIG, instants)
    start_utc, end_utc, source = collect.window_bounds(start, CONFIG, instants)
    assert (start_utc, end_utc, source) == (SEP_19, SEP_19 + WEEK, "quota-sample")
    assert collect.window_bounds(start, CONFIG, None)[2] == "config"


def test_window_bounds_fall_back_when_the_date_has_no_matching_instant():
    instants = [SEP_19]
    from datetime import date

    assert collect.window_bounds(date(2026, 9, 20), CONFIG, instants)[2] == "config"


def test_window_keys_stay_the_local_start_date():
    instants = [SEP_19]
    start = collect.window_start(ts("2026-09-20T12:00:00Z"), CONFIG, instants)
    assert collect.window_key(start) == "week_2026_09_19"


def test_bucketing_splits_a_session_on_the_real_instant():
    instants = [SEP_19]
    records = [
        {"ts": "2026-09-19T02:00:00Z", "sessionId": "s1", "weighted": 100.0},
        {"ts": "2026-09-19T03:30:00Z", "sessionId": "s1", "weighted": 200.0},
    ]
    buckets = collect.bucket_records(records, CONFIG, instants)
    assert sorted(buckets) == ["2026-09-12", "2026-09-19"]
    assert [r["weighted"] for r in buckets["2026-09-12"]] == [100.0]


def test_a_window_is_closed_only_after_its_real_end(tmp_path):
    instants = [SEP_19]
    start = collect.window_start(SEP_19, CONFIG, instants)
    assert collect._window_is_closed(start, CONFIG, SEP_19 + WEEK - timedelta(seconds=1), instants) is False
    assert collect._window_is_closed(start, CONFIG, SEP_19 + WEEK, instants) is True


def transcript(root, *moments):
    root.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u%d" % index,
                "timestamp": moment,
                "sessionId": "s1",
                "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 10}},
            }
        )
        for index, moment in enumerate(moments)
    ]
    (root / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_collect_cuts_stored_windows_on_the_reset_instant(tmp_path):
    out = tmp_path / "out"
    (out / "data").mkdir(parents=True)
    quota.samples_path(out / "data").write_text(
        json.dumps(sample("2026-09-19T03:00:00.046991+00:00")) + "\n", encoding="utf-8"
    )
    root = transcript(tmp_path / "projects", "2026-09-19T02:00:00.000Z", "2026-09-19T04:00:00.000Z")
    collect.run(CONFIG, root, out, out / "state.json")
    assert sorted(p.stem for p in (out / "data" / "records").glob("week_*.jsonl")) == [
        "week_2026_09_12",
        "week_2026_09_19",
    ]
    stored = json.loads((out / "data" / "week_2026_09_19.json").read_text(encoding="utf-8"))
    assert stored["window"]["boundary_source"] == "quota-sample"
    assert stored["window"]["start_utc"] == "2026-09-19T03:00:00+00:00"


def test_collect_without_samples_cuts_on_the_configured_fallback(tmp_path):
    out = tmp_path / "out"
    root = transcript(tmp_path / "projects", "2026-09-19T02:00:00.000Z", "2026-09-19T04:00:00.000Z")
    collect.run(CONFIG, root, out, out / "state.json")
    assert [p.stem for p in (out / "data" / "records").glob("week_*.jsonl")] == ["week_2026_09_19"]
    stored = json.loads((out / "data" / "week_2026_09_19.json").read_text(encoding="utf-8"))
    assert stored["window"]["boundary_source"] == "config"
