import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cli
import collect
import paths
import quota

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
CONFIG["timezone"] = "Europe/Prague"

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
SEP_12 = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)
INSTANTS = [SEP_12]
CEILING = 1000000.0


def sample(offset_hours, pct, weighted, resets_at="2026-09-12T03:00:00.046991+00:00"):
    return {
        "ts": (NOW - timedelta(hours=offset_hours)).isoformat(),
        "seven_day_pct": pct,
        "seven_day_resets_at": resets_at,
        "weighted_so_far": weighted,
    }


def on_the_line(*pairs):
    return [sample(hours, pct, CEILING * pct / 100.0) for hours, pct in pairs]


def fit(samples, config=None):
    return collect.quota_fit(samples, config or CONFIG, instants=INSTANTS, now=NOW)


def test_a_clean_fit_recovers_the_ceiling():
    fitted = fit(on_the_line((72, 20.0), (48, 40.0), (24, 60.0)))
    assert fitted["method"] == "quota-fit"
    assert fitted["estimate"] == pytest.approx(CEILING)
    assert fitted["samples_used"] == 3
    assert fitted["band_pct"] == 0.0


def test_scatter_around_the_line_widens_the_confidence_band():
    samples = [sample(72, 20.0, 180000.0), sample(48, 40.0, 440000.0), sample(24, 60.0, 570000.0)]
    fitted = fit(samples)
    assert 0.0 < fitted["band_pct"] < 20.0
    assert 850000.0 < fitted["estimate"] < 1050000.0


def test_two_samples_are_not_enough():
    assert fit(on_the_line((48, 40.0), (24, 60.0))) is None


def test_the_minimum_sample_count_is_configurable():
    config = copy.deepcopy(CONFIG)
    config["ceiling"]["quota_fit"]["min_samples"] = 2
    assert fit(on_the_line((48, 40.0), (24, 60.0)), config)["samples_used"] == 2


def test_samples_below_the_ten_percent_floor_are_dropped():
    samples = on_the_line((96, 2.0), (72, 20.0), (48, 40.0), (24, 60.0))
    fitted = fit(samples)
    assert fitted["samples_used"] == 3


def test_a_window_of_only_low_samples_produces_no_fit():
    assert fit(on_the_line((72, 1.0), (48, 2.0), (24, 3.0))) is None


def test_a_sample_with_no_spend_recorded_is_dropped():
    samples = on_the_line((72, 20.0), (48, 40.0), (24, 60.0)) + [sample(12, 80.0, 0.0)]
    assert fit(samples)["samples_used"] == 3


def test_samples_from_older_windows_fall_out_of_the_fit():
    recent = on_the_line((72, 20.0), (48, 40.0), (24, 60.0))
    ancient = [sample(24 * 40 + hours, pct, CEILING * pct / 100.0) for hours, pct in ((72, 20.0), (48, 40.0))]
    config = copy.deepcopy(CONFIG)
    config["ceiling"]["quota_fit"]["windows"] = 1
    fitted = collect.quota_fit(recent + ancient, config, instants=INSTANTS, now=NOW)
    assert fitted["samples_used"] == 3


def test_samples_from_a_future_window_are_ignored():
    future = [
        {
            "ts": (NOW + timedelta(days=10)).isoformat(),
            "seven_day_pct": 90.0,
            "seven_day_resets_at": "2026-09-12T03:00:00+00:00",
            "weighted_so_far": 9000000.0,
        }
    ]
    assert fit(on_the_line((72, 20.0), (48, 40.0), (24, 60.0)) + future)["samples_used"] == 3


def test_a_fresh_sample_is_marked_fresh_and_a_stale_one_is_not():
    assert fit(on_the_line((72, 20.0), (48, 40.0), (1, 60.0)))["latest_pct_is_fresh"] is True
    assert fit(on_the_line((72, 20.0), (48, 40.0), (24, 60.0)))["latest_pct_is_fresh"] is False


def test_the_fit_beats_a_configured_override():
    config = copy.deepcopy(CONFIG)
    config["ceiling"]["override"] = 42.0
    totals = {"2026-09-12": 500000.0}
    ceiling = collect.estimate_ceiling(
        totals, config, samples=on_the_line((72, 20.0), (48, 40.0), (24, 60.0)), instants=INSTANTS, now=NOW
    )
    assert ceiling["method"] == "quota-fit"


def test_without_a_fit_the_override_still_wins():
    config = copy.deepcopy(CONFIG)
    config["ceiling"]["override"] = 42.0
    ceiling = collect.estimate_ceiling({"2026-09-12": 500000.0}, config, samples=[], instants=INSTANTS, now=NOW)
    assert ceiling["method"] == "override"


def test_without_a_fit_or_an_override_the_top_cluster_still_wins():
    totals = {"a": 100.0, "b": 200.0, "c": 300.0}
    assert collect.estimate_ceiling(totals, CONFIG, samples=[])["method"] == "top-cluster"


def test_too_few_samples_and_too_few_windows_leave_no_ceiling():
    ceiling = collect.estimate_ceiling({"a": 100.0}, CONFIG, samples=on_the_line((24, 60.0)))
    assert ceiling["method"] == "insufficient-data"
    assert ceiling["estimate"] is None


def block(ceiling, weighted, is_current=True):
    return collect.ceiling_block(ceiling, weighted, 3.0, NOW, NOW + timedelta(days=4), is_current)


def test_a_fresh_sample_becomes_the_percent_used_directly():
    fitted = fit(on_the_line((72, 20.0), (48, 40.0), (1, 61.0)))
    filled = block(fitted, 500000.0)
    assert filled["percent_used"] == 61.0
    assert filled["percent_used_source"] == "quota-sample"


def test_a_stale_sample_leaves_the_percent_derived_from_the_fit():
    fitted = fit(on_the_line((72, 20.0), (48, 40.0), (24, 60.0)))
    filled = block(fitted, 500000.0)
    assert filled["percent_used"] == pytest.approx(100.0 * 500000.0 / fitted["estimate"], rel=1e-6)
    assert filled["percent_used_source"] == "ceiling-estimate"


def test_a_closed_window_never_borrows_todays_percent():
    fitted = fit(on_the_line((72, 20.0), (48, 40.0), (1, 61.0)))
    filled = block(fitted, 500000.0, is_current=False)
    assert filled["percent_used_source"] == "ceiling-estimate"


def test_a_top_cluster_ceiling_never_borrows_the_sample_percent():
    ceiling = {"method": "top-cluster", "estimate": 1000.0, "approximate": True, "latest_pct": 61.0, "latest_pct_is_fresh": True}
    assert block(ceiling, 500.0)["percent_used"] == 50.0


def test_the_method_text_names_the_fit_and_its_band():
    fitted = fit(on_the_line((72, 20.0), (48, 40.0), (24, 60.0)))
    text = collect.ceiling_method_text(fitted)
    assert text == "fitted from 3 usage samples, +/-0%"
    assert collect.ceiling_noun(fitted) == "your weekly quota"


def test_the_method_text_still_names_the_heavy_week_estimate():
    ceiling = collect.estimate_ceiling({"a": 100.0, "b": 200.0, "c": 300.0}, CONFIG, samples=[])
    assert collect.ceiling_method_text(ceiling) == "estimated ceiling, from your own heavy weeks"
    assert collect.ceiling_noun(ceiling) == "an estimated ceiling"
    assert collect.quota_is_known(ceiling) is False


def test_an_override_counts_as_a_known_quota():
    config = copy.deepcopy(CONFIG)
    config["ceiling"]["override"] = 42.0
    ceiling = collect.estimate_ceiling({"a": 1.0}, config, samples=[])
    assert collect.quota_is_known(ceiling) is True
    assert collect.ceiling_method_text(ceiling) == "the ceiling set in your config"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    return paths.ensure_home()


def test_the_quota_command_says_when_nothing_has_been_sampled(home, capsys):
    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "samples          : 0" in out
    assert "Run collect once" in out


def test_the_quota_command_prints_the_latest_sample_and_the_ceiling(home, capsys):
    data_dir = paths.data_dir(home)
    for entry in on_the_line((72, 20.0), (48, 40.0), (24, 60.0)):
        quota.append_sample(data_dir, dict(entry, per_model={"seven_day_opus": {"pct": 12.0, "resets_at": None}},
                                           extra_usage={"is_enabled": True, "monthly_limit": 30000,
                                                        "used_credits": 0.0, "currency": "EUR"},
                                           five_hour_pct=18.0, five_hour_resets_at="2026-09-18T13:00:00+00:00"))
    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "samples          : 3" in out
    assert "seven day used   : 60.0%" in out
    assert "five hour used   : 18.0%" in out
    assert "seven_day_opus : 12.0%" in out
    assert "extra usage      : 0.0 of 30000 EUR used" in out
    assert "2026-09-12T03:00:00+00:00" in out
    assert "fitted from 3 usage samples" in out
    assert "your weekly quota" in out


def test_the_phrase_names_the_quota_only_when_it_is_one():
    fitted = fit(on_the_line((72, 20.0), (48, 40.0), (24, 60.0)))
    assert collect.ceiling_phrase(fitted) == "your weekly quota, fitted from 3 usage samples, +/-0%"
    top = collect.estimate_ceiling({"a": 100.0, "b": 200.0, "c": 300.0}, CONFIG, samples=[])
    assert collect.ceiling_phrase(top) == "estimated ceiling, from your own heavy weeks"
    assert "quota" not in collect.ceiling_phrase(top)
