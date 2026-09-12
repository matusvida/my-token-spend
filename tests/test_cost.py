import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cost


def entry(session, usd, models=None):
    return {
        "type": "cost-state",
        "sessionId": session,
        "totalCostUSD": usd,
        "modelUsage": models
        or {
            "claude-opus-5": {
                "inputTokens": 100,
                "outputTokens": 200,
                "cacheReadInputTokens": 300,
                "cacheCreationInputTokens": 400,
                "costUSD": usd,
            }
        },
    }


def test_capture_keeps_session_total_and_per_model_counts():
    captured = cost.capture(entry("s1", 1.5))
    assert captured["session"] == "s1"
    assert captured["usd"] == 1.5
    assert captured["models"]["claude-opus-5"]["output"] == 200
    assert captured["models"]["claude-opus-5"]["cache_read"] == 300


def test_capture_ignores_other_entry_types_and_incomplete_states():
    assert cost.capture({"type": "assistant"}) is None
    assert cost.capture({"type": "cost-state", "sessionId": "s1"}) is None
    assert cost.capture({"type": "cost-state", "totalCostUSD": 1.0}) is None


def test_merge_keeps_the_last_state_per_session_and_never_shrinks(tmp_path):
    cost.merge(tmp_path, [cost.capture(entry("s1", 1.0)), cost.capture(entry("s2", 2.0))])
    known = cost.merge(tmp_path, [cost.capture(entry("s1", 3.0))])
    assert known["s1"]["usd"] == 3.0
    assert known["s2"]["usd"] == 2.0
    assert json.loads((tmp_path / cost.STORE_NAME).read_text(encoding="utf-8"))["s2"]["usd"] == 2.0


def test_merge_discards_the_store_when_asked(tmp_path):
    cost.merge(tmp_path, [cost.capture(entry("s1", 1.0))])
    known = cost.merge(tmp_path, [cost.capture(entry("s2", 2.0))], discard_stored=True)
    assert set(known) == {"s2"}


def test_window_block_sums_the_sessions_it_is_given_and_states_its_coverage():
    costs = {"s1": {"session": "s1", "usd": 1.25, "models": {}}, "s2": {"session": "s2", "usd": 2.0, "models": {}}}
    block = cost.window_block(["s1", "s2", "s3"], costs)
    assert block["usd"] == 3.25
    assert block["priced_sessions"] == 2
    assert block["sessions"] == 3
    assert "not what the subscription bills" in block["label"]


def test_window_block_without_any_priced_session_reports_none():
    block = cost.window_block(["s1"], {})
    assert block["usd"] is None
    assert block["priced_sessions"] == 0


def test_sessions_are_owned_by_the_window_holding_their_first_record():
    windows = {
        "2026-08-15": [{"sessionId": "s1", "ts": "2026-08-16T10:00:00+00:00"}],
        "2026-08-22": [
            {"sessionId": "s1", "ts": "2026-08-23T10:00:00+00:00"},
            {"sessionId": "s2", "ts": "2026-08-23T11:00:00+00:00"},
        ],
    }
    owned = cost.sessions_by_window(windows)
    assert owned["2026-08-15"] == {"s1"}
    assert owned["2026-08-22"] == {"s2"}


def test_calibrate_recovers_the_prices_that_generated_the_sessions():
    prices = {"input": 3e-6, "output": 15e-6, "cache_create": 3.75e-6, "cache_read": 0.3e-6}
    rng = random.Random(7)
    costs = {}
    for index in range(12):
        counts = {
            "input": rng.randrange(500, 40000),
            "output": rng.randrange(100, 9000),
            "cache_create": rng.randrange(1000, 90000),
            "cache_read": rng.randrange(10000, 900000),
        }
        usd = sum(prices[cls] * counts[cls] for cls in prices)
        costs["s%d" % index] = {
            "session": "s%d" % index,
            "usd": usd,
            "models": {"claude-opus-5": dict(counts, usd=usd)},
        }
    config = {
        "token_class_weights": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.1, "output": 5.0},
        "model_weights": {"claude-opus-5": 5.0, "claude-sonnet-5": 1.0},
        "default_model_weight": 1.0,
    }
    rows = cost.calibrate(costs, config)
    opus = [row for row in rows if row["model"] == "claude-opus-5"][0]
    assert opus["reason"] is None
    assert opus["sessions"] == 12
    assert abs(opus["implied"]["output"] - 5.0) < 0.05
    assert abs(opus["implied"]["cache_read"] - 0.1) < 0.01
    assert opus["configured"]["cache_create"] == 1.25


def test_calibrate_refuses_a_model_with_too_few_sessions():
    costs = {
        "s1": {
            "session": "s1",
            "usd": 1.0,
            "models": {"claude-opus-5": {"input": 1, "output": 1, "cache_create": 1, "cache_read": 1, "usd": 1.0}},
        }
    }
    config = {"token_class_weights": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.1, "output": 5.0},
              "model_weights": {}, "default_model_weight": 1.0}
    rows = cost.calibrate(costs, config)
    assert rows[0]["implied"] is None
    assert "sessions" in rows[0]["reason"]


def test_calibrate_report_names_both_sides():
    costs = {}
    config = {"token_class_weights": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.1, "output": 5.0},
              "model_weights": {}, "default_model_weight": 1.0}
    text = cost.calibrate_report(cost.calibrate(costs, config))
    assert "no cost-state" in text


def test_calibrate_refuses_a_fit_with_no_positive_price():
    costs = {}
    for index in range(12):
        counts = {"input": 100 + index, "output": 200 + 2 * index, "cache_create": 300 + 3 * index,
                  "cache_read": 400 + 4 * index}
        usd = 1e-6 * (counts["input"] + counts["output"])
        costs["s%d" % index] = {"session": "s%d" % index, "usd": usd,
                                "models": {"m": dict(counts, usd=usd)}}
    config = {"token_class_weights": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.1, "output": 5.0},
              "model_weights": {}, "default_model_weight": 1.0}
    row = cost.calibrate(costs, config)[0]
    assert row["implied"] is None
    assert "positive price" in row["reason"]


def test_calibrate_marks_an_unstable_fit_rather_than_hiding_it():
    early = {"input": 3e-6, "output": 15e-6, "cache_create": 3.75e-6, "cache_read": 0.3e-6}
    late = dict(early, output=45e-6)
    rng = random.Random(5)
    costs = {}
    for index in range(16):
        counts = {
            "input": rng.randrange(500, 40000),
            "output": rng.randrange(100, 9000),
            "cache_create": rng.randrange(1000, 90000),
            "cache_read": rng.randrange(10000, 900000),
        }
        prices = early if index < 8 else late
        usd = sum(prices[cls] * counts[cls] for cls in prices)
        costs["s%02d" % index] = {"session": "s%02d" % index, "usd": usd,
                                  "models": {"m": dict(counts, usd=usd)}}
    config = {"token_class_weights": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.1, "output": 5.0},
              "model_weights": {}, "default_model_weight": 1.0}
    row = cost.calibrate(costs, config)[0]
    assert row["implied"] is not None
    assert "unstable" in row["reason"]
    assert "unstable" in cost.calibrate_report([row])


def test_calibrate_weights_prints_both_sides_and_writes_nothing(monkeypatch, tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import cli
    import paths

    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    home = paths.ensure_home()
    data_dir = paths.data_dir(home)
    rng = random.Random(3)
    prices = {"input": 3e-6, "output": 15e-6, "cache_create": 3.75e-6, "cache_read": 0.3e-6}
    fresh = []
    for index in range(10):
        counts = {
            "input": rng.randrange(500, 40000),
            "output": rng.randrange(100, 9000),
            "cache_create": rng.randrange(1000, 90000),
            "cache_read": rng.randrange(10000, 900000),
        }
        usd = sum(prices[cls] * counts[cls] for cls in prices)
        fresh.append(
            {"session": "s%d" % index, "usd": usd, "models": {"claude-opus-5": dict(counts, usd=usd)}}
        )
    cost.merge(data_dir, fresh)
    before = sorted(path.name for path in Path(data_dir).iterdir())

    assert cli.main(["collect", "--calibrate-weights"]) == 0
    out = capsys.readouterr().out
    assert "configured" in out and "implied" in out
    assert "nothing is written" in out
    assert sorted(path.name for path in Path(data_dir).iterdir()) == before
