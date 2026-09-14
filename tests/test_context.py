import json
from pathlib import Path

import context
import rules

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
UTC_CONFIG = dict(CONFIG, timezone="UTC")
W = CONFIG["token_class_weights"]
THRESHOLD = CONFIG["thresholds"]["context_bloat"]["cache_read_per_turn"]


def rec(
    ts,
    session="s1",
    model="claude-sonnet-5",
    cache_read=0,
    cache_create=0,
    output=0,
    agent_id=None,
    sidechain=False,
    tools=(),
    after_compaction=False,
    compacted=False,
    api_error=False,
    prompt=None,
):
    weighted = rules.model_weight(model, CONFIG)[0] * (
        W["output"] * output + W["cache_create"] * cache_create + W["cache_read"] * cache_read
    )
    return {
        "ts": ts,
        "uuid": ts,
        "sessionId": session,
        "model": model,
        "isSidechain": sidechain,
        "agentId": agent_id,
        "attributionAgent": None,
        "cwd": "C:\\workspace\\srst",
        "gitBranch": "master",
        "input": 0,
        "output": output,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "thinking": 0,
        "weighted": weighted,
        "tools": [dict(t) for t in tools],
        "text_chars": 0,
        "is_api_error": api_error,
        "prompt": prompt,
        "after_compaction": after_compaction,
        "compacted": compacted,
    }


def tool(name, chars=None, tool_use_id=None):
    return {"name": name, "hash": name + "-h", "tool_use_id": tool_use_id, "result_chars": chars}


def at(minute):
    return "2026-08-25T14:%02d:00+00:00" % minute


def growth_of(session, config=CONFIG):
    return context.summarize_session(session, config)


def test_first_turn_of_a_thread_has_no_growth():
    summary = growth_of([rec(at(0), cache_read=500000)])
    assert summary["growth_total"] == 0


def test_growth_is_the_rise_in_carried_context():
    session = [
        rec(at(0), cache_read=100000, tools=[tool("Bash", 4000)]),
        rec(at(1), cache_read=140000),
    ]
    assert growth_of(session)["growth_total"] == 40000


def test_growth_counts_cache_creation_as_well_as_cache_read():
    session = [
        rec(at(0), cache_read=100000, cache_create=0, tools=[tool("Bash", 10)]),
        rec(at(1), cache_read=100000, cache_create=5000),
    ]
    assert growth_of(session)["growth_total"] == 5000


def test_shrinking_context_never_counts_as_negative_growth():
    session = [
        rec(at(0), cache_read=200000, tools=[tool("Bash", 10)]),
        rec(at(1), cache_read=10000),
    ]
    assert growth_of(session)["growth_total"] == 0


def test_growth_resets_to_zero_after_a_compaction():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 10)]),
        rec(at(1), cache_read=400000, after_compaction=True),
        rec(at(2), cache_read=410000),
    ]
    summary = growth_of(session)
    assert summary["growth_total"] == 10000
    assert summary["compactions"] == 1


def test_a_context_managed_turn_also_resets_growth():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 10)]),
        rec(at(1), cache_read=400000, compacted=True),
    ]
    summary = growth_of(session)
    assert summary["growth_total"] == 0
    assert summary["compactions"] == 1


def test_subagent_turns_do_not_grow_the_main_thread():
    session = [
        rec(at(0), cache_read=800000, tools=[tool("Bash", 100)]),
        rec(at(1), cache_read=20000, sidechain=True, agent_id="a1"),
        rec(at(2), cache_read=810000),
    ]
    assert growth_of(session)["growth_total"] == 10000


def test_each_subagent_thread_grows_on_its_own():
    session = [
        rec(at(0), cache_read=20000, sidechain=True, agent_id="a1", tools=[tool("Read", 10)]),
        rec(at(1), cache_read=30000, sidechain=True, agent_id="a2"),
        rec(at(2), cache_read=50000, sidechain=True, agent_id="a1"),
    ]
    assert growth_of(session)["growth_total"] == 30000


def test_a_zero_usage_error_turn_is_skipped_rather_than_treated_as_a_reset():
    session = [
        rec(at(0), cache_read=100000, tools=[tool("Bash", 10)]),
        rec(at(1), cache_read=0, api_error=True),
        rec(at(2), cache_read=120000),
    ]
    assert growth_of(session)["growth_total"] == 20000


def test_growth_is_split_between_tool_results_by_their_size():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 3000), tool("Read", 1000)]),
        rec(at(1), cache_read=50000),
    ]
    by_tool = {entry["tool"]: entry["tokens"] for entry in growth_of(session)["growth_by_tool"]}
    assert by_tool == {"Bash": 30000, "Read": 10000}


def test_growth_after_a_text_only_turn_goes_to_the_prompt():
    session = [rec(at(0), cache_read=10000), rec(at(1), cache_read=30000)]
    summary = growth_of(session)
    assert summary["prompt_growth"] == 20000
    assert summary["growth_by_tool"] == []


def test_growth_is_unattributed_when_a_tool_result_size_is_missing():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 3000), tool("Read", None)]),
        rec(at(1), cache_read=30000),
    ]
    summary = growth_of(session)
    assert summary["unattributed_growth"] == 20000
    assert summary["growth_by_tool"] == []


def test_growth_is_unattributed_when_every_result_was_empty():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 0)]),
        rec(at(1), cache_read=30000),
    ]
    assert growth_of(session)["unattributed_growth"] == 20000


def test_attributed_share_reports_how_much_growth_found_a_cause():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 500)]),
        rec(at(1), cache_read=30000, tools=[tool("Read", None)]),
        rec(at(2), cache_read=50000),
    ]
    assert growth_of(session)["attributed_share"] == 0.5


def test_top_results_are_the_largest_results_with_tool_and_timestamp():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 400000), tool("Read", 10)]),
        rec(at(1), cache_read=90000),
    ]
    top = growth_of(session)["top_results"]
    assert [entry["tool"] for entry in top] == ["Bash", "Read"]
    assert top[0]["chars"] == 400000 and top[0]["ts"] == at(0)


def test_top_results_are_capped_at_ten():
    tools = [tool("Bash", 1000 + i) for i in range(14)]
    session = [rec(at(0), cache_read=10000, tools=tools), rec(at(1), cache_read=90000)]
    assert len(growth_of(session)["top_results"]) == 10


def test_coverage_counts_the_tool_calls_that_carry_a_result_size():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 10), tool("Read", None)]),
        rec(at(1), cache_read=30000),
    ]
    coverage = growth_of(session)["tool_results_coverage"]
    assert (coverage["present"], coverage["total"], coverage["share"]) == (1, 2, 0.5)


def test_carry_tax_matches_the_weighted_cache_read_above_the_threshold():
    session = [rec(at(i), cache_read=THRESHOLD + 1000) for i in range(3)]
    expected = rules.model_weight("claude-sonnet-5", CONFIG)[0] * W["cache_read"] * 3000
    summary = growth_of(session)
    assert summary["carry_tax"] == expected
    assert summary["excess_tokens"] == 3000


def test_detail_names_the_top_tool_its_share_and_the_largest_result():
    session = [
        rec(at(2), cache_read=THRESHOLD + 1000, tools=[tool("Bash", 422000), tool("Read", 1000)]),
        rec(at(3), cache_read=THRESHOLD + 900000),
    ]
    detail = context.detail(growth_of(session, UTC_CONFIG), THRESHOLD, UTC_CONFIG)
    assert "above the 150,000-token threshold" in detail
    assert "of the context growth came from" in detail
    assert "came from 1 Bash result," in detail
    assert "412 KB at 14:02" in detail


def test_detail_never_names_a_tool_input_hash():
    session = [
        rec(at(2), cache_read=THRESHOLD + 1000, tools=[tool("Bash", 422000)]),
        rec(at(3), cache_read=THRESHOLD + 900000),
    ]
    assert "Bash-h" not in context.detail(growth_of(session, UTC_CONFIG), THRESHOLD, UTC_CONFIG)


def test_detail_states_the_missing_coverage_when_nothing_can_be_attributed():
    session = [
        rec(at(2), cache_read=THRESHOLD + 1000, tools=[tool("Bash", None)]),
        rec(at(3), cache_read=THRESHOLD + 900000),
    ]
    detail = context.detail(growth_of(session, UTC_CONFIG), THRESHOLD, UTC_CONFIG)
    assert "tool results were not recorded" in detail
    assert "1 of 1" in detail


def test_window_block_ranks_sessions_by_carry_tax_and_states_coverage():
    records = [rec(at(i), session="s1", cache_read=THRESHOLD + 10000) for i in range(13)]
    records += [rec(at(i), session="s2", cache_read=THRESHOLD + 100) for i in range(13)]
    block = context.window_block(records, CONFIG)
    assert block["threshold"] == THRESHOLD
    assert [entry["session"] for entry in block["sessions"]] == ["s1", "s2"]
    assert block["coverage"]["total"] == 0
    assert "tool results" in block["statement"]


def test_window_block_keeps_a_per_turn_series_for_the_chart():
    records = [rec(at(i), cache_read=THRESHOLD + 1000 * i) for i in range(13)]
    series = context.window_block(records, CONFIG)["sessions"][0]["series"]
    assert len(series) == 13
    assert series[0][0] == at(0)


def test_window_block_skips_sessions_that_stay_under_the_threshold():
    records = [rec(at(i), cache_read=1000) for i in range(13)]
    assert context.window_block(records, CONFIG)["sessions"] == []


def test_a_long_session_series_is_downsampled_for_the_chart():
    session = [rec(at(0), cache_read=THRESHOLD + i) for i in range(context.SERIES_POINTS + 500)]
    for i, record in enumerate(session):
        record["ts"] = "2026-08-25T14:00:%06.3f+00:00" % (i * 0.001)
    summary = growth_of(session)
    assert len(summary["series"]) <= context.SERIES_POINTS + 1
    assert summary["series"][-1][0] == session[-1]["ts"]


def test_a_downsampled_bin_carries_its_largest_point_and_the_count_behind_it():
    session = [rec(at(0), cache_read=THRESHOLD + 1000) for _ in range(context.SERIES_POINTS + 500)]
    for i, record in enumerate(session):
        record["ts"] = "2026-08-25T14:00:%06.3f+00:00" % (i * 0.001)
    session[5]["cache_read"] = THRESHOLD + 9_000_000
    summary = growth_of(session)
    assert summary["series_points"] == context.SERIES_POINTS + 500
    assert len(summary["series"]) <= context.SERIES_POINTS
    assert max(point[1] for point in summary["series"]) == THRESHOLD + 9_000_000


def test_an_undersampled_series_reports_one_point_per_turn():
    session = [rec(at(i), cache_read=THRESHOLD + 1000 * i) for i in range(13)]
    summary = growth_of(session)
    assert summary["series_points"] == len(summary["series"])


def test_compaction_timestamps_are_kept_for_the_chart_markers():
    session = [
        rec(at(0), cache_read=10000),
        rec(at(1), cache_read=20000, after_compaction=True),
    ]
    assert growth_of(session)["compaction_ts"] == [at(1)]


def test_parallel_calls_on_one_turn_each_count_as_a_result():
    session = [
        rec(at(0), cache_read=10000, tools=[tool("Bash", 100), tool("Bash", 300)]),
        rec(at(1), cache_read=30000),
    ]
    entry = growth_of(session)["growth_by_tool"][0]
    assert (entry["tool"], entry["results"]) == ("Bash", 2)


def test_detail_names_the_largest_result_of_the_leading_tool_even_outside_the_top_ten():
    tools = [tool("Bash", 4000)] + [tool("Read", 20000 + i) for i in range(11)]
    session = [
        rec(at(2), cache_read=THRESHOLD + 1000, tools=tools),
        rec(at(3), cache_read=THRESHOLD + 900000),
    ]
    summary = growth_of(session, UTC_CONFIG)
    summary["growth_by_tool"] = [entry for entry in summary["growth_by_tool"] if entry["tool"] == "Bash"]
    assert "Bash" not in [entry["tool"] for entry in summary["top_results"]]
    assert "the largest 4 KB at 14:02" in context.detail(summary, THRESHOLD, UTC_CONFIG)


def test_downsampling_keeps_every_token_of_growth():
    session = [rec(at(0), cache_read=1000 * i, tools=[tool("Bash", 10)]) for i in range(1200)]
    for i, record in enumerate(session):
        record["ts"] = "2026-08-25T14:00:%06.3f+00:00" % (i * 0.001)
    summary = growth_of(session)
    assert sum(point[2] for point in summary["series"]) == summary["growth_total"]


def test_a_downsampled_point_carries_the_source_index_of_its_peak():
    session = [rec(at(0), cache_read=THRESHOLD + 1000) for _ in range(context.SERIES_POINTS + 500)]
    for i, record in enumerate(session):
        record["ts"] = "2026-08-25T14:00:%06.3f+00:00" % (i * 0.001)
    session[5]["cache_read"] = THRESHOLD + 9_000_000
    series = growth_of(session)["series"]
    peak = max(series, key=lambda point: point[1])
    assert peak[4] == 5
    assert [point[4] for point in series] == sorted(point[4] for point in series)


def test_an_undersampled_series_carries_no_source_index():
    session = [rec(at(i), cache_read=THRESHOLD + 1000 * i) for i in range(13)]
    assert all(len(point) == 4 for point in growth_of(session)["series"])
