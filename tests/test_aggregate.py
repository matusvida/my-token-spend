import json
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
CONFIG["timezone"] = "Europe/Prague"


def rec(ts, session="s1", model="claude-sonnet-5", output=0, cache_read=0, sidechain=False, agent=None, cwd="C:\\a"):
    weights = CONFIG["token_class_weights"]
    weight = CONFIG["model_weights"].get(model, CONFIG["default_model_weight"])
    return {
        "ts": ts,
        "uuid": "u",
        "sessionId": session,
        "model": model,
        "model_known": model in CONFIG["model_weights"],
        "effort": "high",
        "isSidechain": sidechain,
        "agentId": None,
        "attributionAgent": agent,
        "attributionSkill": None,
        "cwd": cwd,
        "gitBranch": "master",
        "version": "2.1.227",
        "input": 0,
        "output": output,
        "cache_create": 0,
        "cache_read": cache_read,
        "thinking": 0,
        "weighted": weight * (weights["output"] * output + weights["cache_read"] * cache_read),
        "tools": [],
        "text_chars": 0,
        "is_api_error": False,
        "prompt": "do the thing",
    }


STATS = {"files_scanned": 1, "files_read": 1, "malformed_lines": 0}


def test_window_metadata_spans_saturday_to_saturday():
    window = collect.aggregate_window(date(2026, 8, 22), [rec("2026-08-25T10:00:00+00:00")], CONFIG, STATS, None)
    assert window["window"]["key"] == "week_2026_08_22"
    assert window["window"]["start"] == "2026-08-22"
    assert window["window"]["end"] == "2026-08-29"


def test_totals_sum_the_records():
    records = [rec("2026-08-25T10:00:00+00:00", output=100), rec("2026-08-26T10:00:00+00:00", output=300)]
    totals = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, None)["totals"]
    assert totals["turns"] == 2
    assert totals["output"] == 400
    assert totals["weighted"] == sum(r["weighted"] for r in records)


def test_sidechain_totals_are_tracked_separately():
    records = [rec("2026-08-25T10:00:00+00:00", output=100, sidechain=True), rec("2026-08-25T11:00:00+00:00", output=100)]
    totals = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, None)["totals"]
    assert totals["sidechain_turns"] == 1
    assert totals["sidechain_weighted"] == records[0]["weighted"]


def test_days_are_bucketed_in_local_time():
    window = collect.aggregate_window(
        date(2026, 8, 22), [rec("2026-08-25T23:30:00+00:00", output=100)], CONFIG, STATS, None
    )
    assert [d["date"] for d in window["by_day"]] == ["2026-08-26"]


def test_breakdowns_are_ranked_by_weighted_cost():
    records = [rec("2026-08-25T10:00:00+00:00", model="claude-sonnet-5", output=100)]
    records += [rec("2026-08-25T11:00:00+00:00", model="claude-opus-5", output=100)]
    window = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, None)
    assert [m["key"] for m in window["by_model"]] == ["claude-opus-5", "claude-sonnet-5"]


def test_unknown_models_are_reported_by_name():
    window = collect.aggregate_window(
        date(2026, 8, 22), [rec("2026-08-25T10:00:00+00:00", model="claude-zebra-9", output=100)], CONFIG, STATS, None
    )
    assert [u["key"] for u in window["unknown_models"]] == ["claude-zebra-9"]


def test_sessions_carry_their_span_and_first_prompt():
    records = [rec("2026-08-25T10:00:00+00:00", output=1), rec("2026-08-26T10:00:00+00:00", output=1)]
    session = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, None)["by_session"][0]
    assert session["key"] == "s1"
    assert session["first_ts"] == "2026-08-25T10:00:00+00:00"
    assert session["last_ts"] == "2026-08-26T10:00:00+00:00"
    assert session["first_prompt"] == "do the thing"


def test_findings_are_embedded_with_a_per_rule_summary():
    records = [rec("2026-08-25T10:00:00+00:00", output=100000, session="s1")]
    window = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, None)
    assert window["findings"]
    assert window["findings_by_rule"]["whale_turns"]["count"] == 1


def test_aggregation_is_deterministic_for_the_same_input():
    records = [rec("2026-08-25T10:00:00+00:00", output=100), rec("2026-08-26T10:00:00+00:00", output=300)]
    first = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, None)
    second = collect.aggregate_window(date(2026, 8, 22), list(reversed(records)), CONFIG, STATS, None)
    first["generated_at"] = second["generated_at"] = None
    assert first == second


def test_ceiling_override_wins_over_calibration():
    config = json.loads(json.dumps(CONFIG))
    config["ceiling"]["override"] = 1234.0
    ceiling = collect.estimate_ceiling({"2026-08-22": 10.0, "2026-08-15": 20.0, "2026-08-08": 30.0}, config)
    assert ceiling["estimate"] == 1234.0
    assert ceiling["method"] == "override"


def test_ceiling_is_unknown_with_too_few_windows():
    ceiling = collect.estimate_ceiling({"2026-08-22": 10.0}, CONFIG)
    assert ceiling["estimate"] is None
    assert ceiling["method"] == "insufficient-data"


def test_ceiling_is_the_top_cluster_mean_with_headroom():
    totals = {"w%d" % i: float(i) for i in range(1, 9)}
    ceiling = collect.estimate_ceiling(totals, CONFIG)
    assert ceiling["method"] == "top-cluster"
    assert ceiling["cluster_size"] == 2
    assert ceiling["estimate"] == ((8.0 + 7.0) / 2) * CONFIG["ceiling"]["headroom"]


def test_ceiling_is_always_flagged_approximate_unless_overridden():
    assert collect.estimate_ceiling({"w%d" % i: float(i) for i in range(1, 9)}, CONFIG)["approximate"] is True


def test_percent_of_ceiling_is_derived_when_a_ceiling_is_known():
    ceiling = {"estimate": 1000.0, "method": "top-cluster", "approximate": True}
    records = [rec("2026-08-25T10:00:00+00:00", output=20)]
    window = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, ceiling)
    assert window["ceiling"]["percent_used"] == 10.0


def test_percent_of_ceiling_is_absent_when_no_ceiling_is_known():
    ceiling = {"estimate": None, "method": "insufficient-data", "approximate": True}
    window = collect.aggregate_window(date(2026, 8, 22), [rec("2026-08-25T10:00:00+00:00")], CONFIG, STATS, ceiling)
    assert window["ceiling"]["percent_used"] is None


def ceiling_at(now_utc, estimate=1000.0, weighted=800.0, elapsed_days=5.0):
    end_local = datetime(2026, 8, 29, 0, 0, tzinfo=ZoneInfo(CONFIG["timezone"]))
    ceiling = {"estimate": estimate, "method": "top-cluster", "approximate": True}
    return collect.ceiling_block(
        ceiling,
        weighted,
        elapsed_days,
        datetime.fromisoformat(now_utc.replace("Z", "+00:00")),
        end_local,
    )


def test_sustainable_rate_is_a_per_day_figure_while_over_a_day_remains():
    block = ceiling_at("2026-08-27T10:00:00Z")
    assert block["sustainable_rate_basis"] == "per-day"
    assert block["sustainable_rate_per_day"] == round(200.0 / 1.5, 2)


def test_sustainable_rate_is_suppressed_on_the_final_day():
    block = ceiling_at("2026-08-28T20:00:00Z")
    assert block["sustainable_rate_basis"] == "final-day"
    assert block["sustainable_rate_per_day"] is None
    assert block["remaining_hours"] == 2.0
    assert block["remaining_weighted"] == 200.0


def test_sustainable_rate_is_suppressed_for_a_closed_window():
    block = ceiling_at("2026-08-30T10:00:00Z")
    assert block["sustainable_rate_basis"] == "window-closed"
    assert block["sustainable_rate_per_day"] is None
    assert block["remaining_hours"] == 0.0


def test_sustainable_rate_never_exceeds_the_budget_actually_left():
    for hours_left in (0.25, 0.5, 1, 2, 6, 12, 23, 24, 25, 48, 96):
        now = datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc) - timedelta(hours=hours_left)
        block = ceiling_at(now.isoformat().replace("+00:00", "Z"))
        rate = block["sustainable_rate_per_day"]
        assert rate is None or rate <= block["remaining_weighted"] + 1e-9, (hours_left, rate)


def test_no_sustainable_rate_without_a_ceiling_estimate():
    block = ceiling_at("2026-08-27T10:00:00Z", estimate=None)
    assert block["sustainable_rate_basis"] == "no-ceiling"
    assert block["sustainable_rate_per_day"] is None


def test_projected_exhaustion_is_reported_when_the_budget_runs_out_before_the_reset():
    block = ceiling_at("2026-08-27T10:00:00Z", estimate=1000.0, weighted=800.0, elapsed_days=5.0)
    assert block["exhausts_before_reset"] is True
    assert block["projected_exhaustion"].startswith("2026-08-28T")


def test_projected_exhaustion_is_suppressed_when_it_falls_after_the_reset():
    block = ceiling_at("2026-08-27T10:00:00Z", estimate=100000.0, weighted=800.0, elapsed_days=5.0)
    assert block["exhausts_before_reset"] is False
    assert block["projected_exhaustion"] is None


def test_projected_exhaustion_is_absent_for_a_closed_window():
    block = ceiling_at("2026-08-30T10:00:00Z")
    assert block["projected_exhaustion"] is None
    assert block["exhausts_before_reset"] is None


def new_rec(ts, **overrides):
    base = rec(ts)
    base.update(
        {
            "mcp_server": None,
            "mcp_tool": None,
            "plugin": None,
            "per_turn_effort": None,
            "stop_reason": "end_turn",
            "cache_create_5m": 0,
            "cache_create_1h": 0,
            "compacted": False,
            "after_compaction": False,
            "source_tool_use_id": None,
        }
    )
    base.update(overrides)
    return base


def aggregate(records):
    return collect.aggregate_window(date(2026, 8, 22), records, CONFIG, STATS, None)


def test_the_schema_version_marks_the_records_that_carry_the_new_fields():
    assert aggregate([new_rec("2026-08-25T10:00:00+00:00")])["schema_version"] == 2


def test_spend_is_broken_down_by_mcp_server_and_plugin():
    window = aggregate(
        [
            new_rec("2026-08-25T10:00:00+00:00", mcp_server="datadog-mcp", plugin="datadog", output=100),
            new_rec("2026-08-25T11:00:00+00:00", mcp_server="datadog-mcp", plugin="datadog", output=200),
            new_rec("2026-08-25T12:00:00+00:00", mcp_server="gitlab", plugin=None, output=50),
        ]
    )
    assert [entry["key"] for entry in window["by_mcp_server"]] == ["datadog-mcp", "gitlab"]
    assert window["by_mcp_server"][0]["turns"] == 2
    assert [entry["key"] for entry in window["by_plugin"]] == ["datadog"]


def test_records_written_before_the_new_fields_existed_still_aggregate():
    window = aggregate([rec("2026-08-25T10:00:00+00:00", output=100)])
    assert window["totals"]["turns"] == 1
    assert window["by_mcp_server"] == []
    assert window["field_coverage"]["mcp_server"]["share"] == 0.0


def test_every_new_lens_states_the_share_of_turns_that_carry_its_field():
    window = aggregate(
        [
            new_rec("2026-08-25T10:00:00+00:00", mcp_server="datadog-mcp"),
            rec("2026-08-25T11:00:00+00:00"),
        ]
    )
    assert window["field_coverage"]["mcp_server"] == {"present": 1, "total": 2, "share": 0.5}
    assert window["field_coverage"]["stop_reason"]["share"] == 0.5
    assert window["field_coverage"]["compacted"]["share"] == 0.5


def test_tool_result_coverage_is_a_share_of_calls_not_of_turns():
    old = rec("2026-08-25T10:00:00+00:00")
    old["tools"] = [{"name": "Bash", "hash": "h1"}]
    fresh = new_rec("2026-08-25T11:00:00+00:00")
    fresh["tools"] = [
        {"name": "Bash", "hash": "h2", "tool_use_id": "c1", "result_chars": 10, "is_error": False, "denied": False},
        {"name": "Read", "hash": "h3", "tool_use_id": "c2", "result_chars": None, "is_error": None, "denied": None},
    ]
    coverage = aggregate([old, fresh])["field_coverage"]["tool_results"]
    assert coverage == {"present": 1, "total": 3, "share": 1 / 3}


def test_context_growth_is_aggregated_per_session_for_the_report():
    threshold = CONFIG["thresholds"]["context_bloat"]["cache_read_per_turn"]
    records = []
    for i in range(13):
        record = new_rec("2026-08-25T10:%02d:00+00:00" % i, cache_read=threshold + 10000 * i)
        record["tools"] = [
            {"name": "Bash", "hash": "h%d" % i, "tool_use_id": "c%d" % i, "result_chars": 5000, "is_error": False, "denied": False}
        ]
        records.append(record)
    block = aggregate(records)["context"]
    assert block["threshold"] == threshold
    assert block["sessions"][0]["session"] == "s1"
    assert block["sessions"][0]["growth_by_tool"][0]["tool"] == "Bash"
    assert block["coverage"]["share"] == 1.0
