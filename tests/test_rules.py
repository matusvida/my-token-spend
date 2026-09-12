import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rules

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
W = CONFIG["token_class_weights"]
MW = CONFIG["model_weights"]


def rec(
    ts,
    session="s1",
    model="claude-sonnet-5",
    input=0,
    output=0,
    cache_create=0,
    cache_read=0,
    sidechain=False,
    agent=None,
    tools=(),
    prompt=None,
    uuid="u",
):
    weighted = rules.model_weight(model, CONFIG)[0] * (
        W["input"] * input
        + W["output"] * output
        + W["cache_create"] * cache_create
        + W["cache_read"] * cache_read
    )
    return {
        "ts": ts,
        "uuid": uuid,
        "sessionId": session,
        "model": model,
        "model_known": True,
        "effort": "high",
        "isSidechain": sidechain,
        "agentId": "a" if sidechain else None,
        "attributionAgent": agent,
        "attributionSkill": None,
        "cwd": "C:\\workspace\\srst",
        "gitBranch": "master",
        "version": "2.1.227",
        "input": input,
        "output": output,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "thinking": 0,
        "weighted": weighted,
        "tools": [{"name": n, "hash": h} for n, h in tools],
        "text_chars": 0,
        "is_api_error": False,
        "prompt": prompt,
    }


def at(minute):
    return "2026-08-25T10:%02d:00+00:00" % minute


def find(findings, rule):
    return [f for f in findings if f["rule"] == rule]


def test_no_records_produces_no_findings():
    assert rules.evaluate([], CONFIG) == []


def test_every_finding_carries_a_weighted_cost():
    records = [rec(at(i), cache_read=400000, tools=[("Read", "h1")]) for i in range(20)]
    findings = rules.evaluate(records, CONFIG)
    assert findings
    for f in findings:
        assert isinstance(f["weighted_cost"], float) and f["weighted_cost"] >= 0


def test_findings_are_ranked_by_weighted_cost():
    records = [rec(at(i), cache_read=400000, output=100, tools=[("Read", "h1")]) for i in range(20)]
    costs = [f["weighted_cost"] for f in rules.evaluate(records, CONFIG)]
    assert costs == sorted(costs, reverse=True)


def test_context_bloat_does_not_fire_on_a_short_session():
    records = [rec(at(i), cache_read=400000) for i in range(3)]
    assert find(rules.evaluate(records, CONFIG), "context_bloat") == []


def test_context_bloat_does_not_fire_when_cache_reads_stay_under_threshold():
    records = [rec(at(i), cache_read=10000) for i in range(30)]
    assert find(rules.evaluate(records, CONFIG), "context_bloat") == []


def test_context_bloat_fires_on_a_long_session_with_large_cache_reads():
    records = [rec(at(i), cache_read=400000) for i in range(20)]
    assert len(find(rules.evaluate(records, CONFIG), "context_bloat")) == 1


def test_context_bloat_cost_is_the_weighted_excess_above_the_threshold():
    threshold = CONFIG["thresholds"]["context_bloat"]["cache_read_per_turn"]
    records = [rec(at(i), cache_read=400000) for i in range(20)]
    expected = 20 * MW["claude-sonnet-5"] * W["cache_read"] * (400000 - threshold)
    assert find(rules.evaluate(records, CONFIG), "context_bloat")[0]["weighted_cost"] == expected


def test_context_bloat_prices_opus_turns_at_the_opus_weight():
    threshold = CONFIG["thresholds"]["context_bloat"]["cache_read_per_turn"]
    records = [rec(at(i), model="claude-opus-5", cache_read=400000) for i in range(20)]
    expected = 20 * MW["claude-opus-5"] * W["cache_read"] * (400000 - threshold)
    assert find(rules.evaluate(records, CONFIG), "context_bloat")[0]["weighted_cost"] == expected


def test_context_bloat_is_reported_per_session():
    records = [rec(at(i), session="s1", cache_read=400000) for i in range(20)]
    records += [rec(at(i), session="s2", cache_read=400000) for i in range(20)]
    subjects = {f["subject"] for f in find(rules.evaluate(records, CONFIG), "context_bloat")}
    assert subjects == {"s1", "s2"}


def test_subagent_storm_does_not_fire_below_the_turn_count():
    records = [rec(at(i), sidechain=True, agent="explorer", output=1000) for i in range(5)]
    records += [rec(at(50), output=10)]
    assert find(rules.evaluate(records, CONFIG), "subagent_storm") == []


def test_subagent_storm_does_not_fire_when_the_cost_share_is_small():
    records = [rec(at(i), sidechain=True, agent="explorer", output=1) for i in range(40)]
    records += [rec(at(50), output=100000)]
    assert find(rules.evaluate(records, CONFIG), "subagent_storm") == []


def test_subagent_storm_fires_and_costs_the_total_sidechain_spend():
    records = [rec(at(i), sidechain=True, agent="explorer", output=1000) for i in range(40)]
    records += [rec(at(50), output=100)]
    finding = find(rules.evaluate(records, CONFIG), "subagent_storm")[0]
    assert finding["subject"] == "s1"
    assert finding["weighted_cost"] == 40 * MW["claude-sonnet-5"] * W["output"] * 1000
    assert finding["evidence"]["sidechain_turns"] == 40


def test_agent_type_skew_ranks_agents_by_weighted_cost():
    records = [rec(at(i), sidechain=True, agent="explorer", output=20000) for i in range(5)]
    records += [rec(at(20 + i), sidechain=True, agent="reviewer", output=60000) for i in range(5)]
    findings = find(rules.evaluate(records, CONFIG), "agent_type_skew")
    assert [f["subject"] for f in findings] == ["reviewer", "explorer"]
    assert findings[0]["weighted_cost"] == 5 * MW["claude-sonnet-5"] * W["output"] * 60000


def test_agent_type_skew_ignores_agents_below_the_minimum():
    records = [rec(at(0), sidechain=True, agent="tiny", output=10)]
    assert find(rules.evaluate(records, CONFIG), "agent_type_skew") == []


def test_agent_type_skew_is_capped_at_top_n():
    top_n = CONFIG["thresholds"]["agent_type_skew"]["top_n"]
    records = [
        rec(at(i), sidechain=True, agent="agent%d" % i, output=100000) for i in range(top_n + 4)
    ]
    assert len(find(rules.evaluate(records, CONFIG), "agent_type_skew")) == top_n


def test_model_mismatch_ignores_sonnet_turns():
    records = [
        rec(at(i), model="claude-sonnet-5", output=10, cache_read=300000, tools=[("Read", "h")])
        for i in range(60)
    ]
    assert find(rules.evaluate(records, CONFIG), "model_mismatch") == []


def test_model_mismatch_ignores_opus_turns_with_long_output():
    records = [
        rec(at(i), model="claude-opus-5", output=5000, cache_read=300000, tools=[("Read", "h")])
        for i in range(60)
    ]
    assert find(rules.evaluate(records, CONFIG), "model_mismatch") == []


def test_model_mismatch_ignores_opus_turns_with_many_tool_calls():
    records = [
        rec(
            at(i),
            model="claude-opus-5",
            output=10,
            cache_read=300000,
            tools=[("Read", "a"), ("Read", "b"), ("Read", "c")],
        )
        for i in range(60)
    ]
    assert find(rules.evaluate(records, CONFIG), "model_mismatch") == []


def test_model_mismatch_fires_on_trivial_opus_turns():
    records = [
        rec(at(i), model="claude-opus-5", output=10, cache_read=300000, tools=[("Read", "h")])
        for i in range(60)
    ]
    findings = find(rules.evaluate(records, CONFIG), "model_mismatch")
    assert len(findings) == 1
    assert findings[0]["evidence"]["turns"] == 60


def test_model_mismatch_cost_is_the_gap_to_the_downgrade_model():
    records = [
        rec(at(i), model="claude-opus-5", output=10, cache_read=300000, tools=[("Read", "h")])
        for i in range(60)
    ]
    raw = W["output"] * 10 + W["cache_read"] * 300000
    expected = 60 * (MW["claude-opus-5"] - MW["claude-sonnet-5"]) * raw
    assert find(rules.evaluate(records, CONFIG), "model_mismatch")[0]["weighted_cost"] == expected


def test_redundant_reads_ignores_a_pair_of_identical_reads():
    records = [rec(at(i), output=1000, tools=[("Read", "same")]) for i in range(2)]
    assert find(rules.evaluate(records, CONFIG), "redundant_reads") == []


def test_redundant_reads_fires_on_the_third_identical_read():
    records = [rec(at(i), output=1000, tools=[("Read", "same")]) for i in range(3)]
    assert len(find(rules.evaluate(records, CONFIG), "redundant_reads")) == 1


def test_redundant_reads_costs_only_the_repeat_occurrences():
    records = [rec(at(i * 5), output=1000, tools=[("Read", "same")]) for i in range(4)]
    per_turn = MW["claude-sonnet-5"] * W["output"] * 1000
    assert find(rules.evaluate(records, CONFIG), "redundant_reads")[0]["weighted_cost"] == 3 * per_turn


def test_redundant_reads_splits_cost_between_tool_calls_in_one_turn():
    records = [rec(at(i * 5), output=1000, tools=[("Read", "same"), ("Grep", "other")]) for i in range(3)]
    per_tool = MW["claude-sonnet-5"] * W["output"] * 1000 / 2
    assert find(rules.evaluate(records, CONFIG), "redundant_reads")[0]["weighted_cost"] == 2 * per_tool


def test_redundant_reads_ignores_tools_outside_the_configured_list():
    records = [rec(at(i * 5), output=1000, tools=[("Agent", "same")]) for i in range(5)]
    assert find(rules.evaluate(records, CONFIG), "redundant_reads") == []


def test_redundant_reads_does_not_span_sessions():
    records = [rec(at(i * 5), session="s%d" % i, output=1000, tools=[("Read", "same")]) for i in range(4)]
    assert find(rules.evaluate(records, CONFIG), "redundant_reads") == []


def test_loop_retry_ignores_identical_calls_that_are_not_consecutive():
    records = [
        rec(at(0), output=1000, tools=[("Bash", "x")]),
        rec(at(1), output=1000, tools=[("Bash", "y")]),
        rec(at(2), output=1000, tools=[("Bash", "x")]),
        rec(at(3), output=1000, tools=[("Bash", "z")]),
        rec(at(4), output=1000, tools=[("Bash", "x")]),
    ]
    assert find(rules.evaluate(records, CONFIG), "loop_retry") == []


def test_loop_retry_fires_on_a_consecutive_run_of_identical_calls():
    records = [rec(at(i), output=1000, tools=[("Bash", "x")]) for i in range(4)]
    findings = find(rules.evaluate(records, CONFIG), "loop_retry")
    assert len(findings) == 1
    assert findings[0]["evidence"]["run_length"] == 4


def test_loop_retry_costs_the_redundant_attempts_only():
    records = [rec(at(i), output=1000, tools=[("Bash", "x")]) for i in range(4)]
    per_turn = MW["claude-sonnet-5"] * W["output"] * 1000
    assert find(rules.evaluate(records, CONFIG), "loop_retry")[0]["weighted_cost"] == 3 * per_turn


def test_loop_retry_covers_any_tool_not_just_reads():
    records = [rec(at(i), output=1000, tools=[("Agent", "x")]) for i in range(5)]
    assert len(find(rules.evaluate(records, CONFIG), "loop_retry")) == 1


def test_whale_turns_reports_the_most_expensive_messages_first():
    records = [rec(at(i), output=1000 * (i + 1), prompt="p%d" % i, uuid="u%d" % i) for i in range(3)]
    findings = find(rules.evaluate(records, CONFIG), "whale_turns")
    assert [f["evidence"]["uuid"] for f in findings] == ["u2", "u1", "u0"]


def test_whale_turn_cost_is_the_turns_own_weighted_cost():
    records = [rec(at(0), output=1000, uuid="u0")]
    expected = MW["claude-sonnet-5"] * W["output"] * 1000
    assert find(rules.evaluate(records, CONFIG), "whale_turns")[0]["weighted_cost"] == expected


def test_whale_turns_are_labelled_with_the_triggering_prompt():
    records = [rec(at(0), output=1000, prompt="refactor the pricing service", uuid="u0")]
    finding = find(rules.evaluate(records, CONFIG), "whale_turns")[0]
    assert finding["evidence"]["prompt"] == "refactor the pricing service"


def test_whale_turns_are_capped_at_top_n():
    top_n = CONFIG["thresholds"]["whale_turns"]["top_n"]
    records = [rec(at(i), output=1000 * (i + 1), uuid="u%d" % i) for i in range(top_n + 5)]
    assert len(find(rules.evaluate(records, CONFIG), "whale_turns")) == top_n


def test_whale_turns_skip_zero_cost_turns():
    records = [rec(at(0), uuid="u0")]
    assert find(rules.evaluate(records, CONFIG), "whale_turns") == []


def test_model_mismatch_survives_a_downgrade_model_with_no_configured_weight():
    config = json.loads(json.dumps(CONFIG))
    config["thresholds"]["model_mismatch"]["downgrade_model"] = "claude-sonnet-6"
    records = [
        rec(at(i), model="claude-opus-5", output=10, cache_read=300000, tools=[("Read", "h")])
        for i in range(60)
    ]
    raw = W["output"] * 10 + W["cache_read"] * 300000
    findings = find(rules.evaluate(records, config), "model_mismatch")
    assert len(findings) == 1
    assert findings[0]["evidence"]["downgrade_model"] == "claude-sonnet-6"
    assert findings[0]["weighted_cost"] == 60 * (MW["claude-opus-5"] - config["default_model_weight"]) * raw


def test_model_weight_resolves_a_suffix_to_its_family():
    assert rules.model_weight("claude-fable-5-1", CONFIG) == (5.0, rules.FAMILY)
    assert rules.model_weight("claude-haiku-4-5-20251001", CONFIG) == (0.33, rules.EXACT)
    assert rules.model_weight("claude-opus-5", CONFIG) == (5.0, rules.EXACT)
    assert rules.model_weight("claude-zebra-9", CONFIG) == (1.0, rules.DEFAULT)


def test_an_exact_weight_wins_over_the_family_weight():
    config = json.loads(json.dumps(CONFIG))
    config["model_weights"]["claude-opus-5-1"] = 7.0
    assert rules.model_weight("claude-opus-5-1", config) == (7.0, rules.EXACT)
    assert rules.model_weight("claude-opus-5-2", config) == (5.0, rules.FAMILY)


def test_a_family_priced_two_ways_never_prices_an_unlisted_sibling():
    config = json.loads(json.dumps(CONFIG))
    config["model_weights"]["claude-sonnet-6"] = 3.0
    assert rules.model_weight("claude-sonnet-7", config) == (1.0, rules.DEFAULT)


def test_the_family_weight_is_the_one_most_of_its_members_agree_on():
    config = json.loads(json.dumps(CONFIG))
    config["model_weights"]["claude-opus-4-8-1"] = 9.0
    assert rules.model_weight("claude-opus-6", config) == (5.0, rules.FAMILY)


def test_model_mismatch_prices_a_point_release_at_its_family_weight():
    records = [
        rec(at(i), model="claude-fable-5-1", output=10, cache_read=300000, tools=[("Read", "h")], uuid="u%d" % i)
        for i in range(60)
    ]
    for record in records:
        record["weighted"] = 5.0 * (W["output"] * 10 + W["cache_read"] * 300000)
    raw = W["output"] * 10 + W["cache_read"] * 300000
    findings = find(rules.evaluate(records, CONFIG), "model_mismatch")
    assert len(findings) == 1
    assert findings[0]["weighted_cost"] == 60 * (5.0 - 1.0) * raw


def test_every_module_shares_one_model_family_implementation():
    import advice
    import tune

    assert advice.model_family is rules.model_family
    assert advice._model_family is rules.model_family
    assert tune._model_family is rules.model_family


def with_results(record, sizes):
    for tool, size in zip(record["tools"], sizes):
        tool["result_chars"] = size
    return record


def repeat_cost(records, tool_name):
    finding = [f for f in find(rules.evaluate(records, CONFIG), "redundant_reads") if f["evidence"]["tool"] == tool_name]
    return finding[0]["weighted_cost"] if finding else 0.0


def test_a_repeated_call_carries_the_share_of_its_own_result_size():
    records = [
        with_results(rec(at(i), output=1000, tools=[("Read", "h1"), ("Bash", "h2")]), [9000, 1000])
        for i in range(4)
    ]
    big, small = repeat_cost(records, "Read"), repeat_cost(records, "Bash")
    assert round(big / small, 6) == 9.0


def test_calls_split_a_turn_evenly_when_no_result_size_was_recorded():
    records = [rec(at(i), output=1000, tools=[("Read", "h1"), ("Bash", "h2")]) for i in range(4)]
    assert repeat_cost(records, "Read") == repeat_cost(records, "Bash")


def test_a_turn_with_one_unrecorded_result_falls_back_to_an_even_split():
    records = [
        with_results(rec(at(i), output=1000, tools=[("Read", "h1"), ("Bash", "h2")]), [9000, None])
        for i in range(4)
    ]
    assert repeat_cost(records, "Read") == repeat_cost(records, "Bash")


def test_a_turn_whose_results_were_all_empty_falls_back_to_an_even_split():
    records = [
        with_results(rec(at(i), output=1000, tools=[("Read", "h1"), ("Bash", "h2")]), [0, 0])
        for i in range(4)
    ]
    assert repeat_cost(records, "Read") == repeat_cost(records, "Bash")
