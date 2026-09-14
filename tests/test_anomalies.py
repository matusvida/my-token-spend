import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rules

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
W = CONFIG["token_class_weights"]

HEADLESS_CWD = "C:\\workspace\\tools\\daily-improvement-review"
TYPED_CWD = "C:\\workspace\\srst"


def rec(
    ts,
    session="s1",
    model="claude-sonnet-5",
    output=0,
    cache_read=0,
    cache_create=0,
    sidechain=False,
    agent=None,
    agent_id=None,
    skill=None,
    cwd=TYPED_CWD,
    entrypoint="cli",
    prompt_source=None,
    mcp_server=None,
    tools=(),
    prompt=None,
    uuid="u",
):
    weighted = rules.model_weight(model, CONFIG)[0] * (
        W["output"] * output + W["cache_read"] * cache_read + W["cache_create"] * cache_create
    )
    return {
        "ts": ts,
        "uuid": uuid,
        "sessionId": session,
        "model": model,
        "model_known": True,
        "effort": "high",
        "isSidechain": sidechain,
        "agentId": agent_id or ("a" if sidechain else None),
        "attributionAgent": agent,
        "attributionSkill": skill,
        "mcp_server": mcp_server,
        "cwd": cwd,
        "entrypoint": entrypoint,
        "prompt_source": prompt_source,
        "gitBranch": "master",
        "version": "2.1.227",
        "input": 0,
        "output": output,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "thinking": 0,
        "weighted": weighted,
        "tools": list(tools),
        "text_chars": 0,
        "is_api_error": False,
        "prompt": prompt,
    }


def tool(name, digest="h1", result_chars=None, is_error=None):
    return {"name": name, "hash": digest, "result_chars": result_chars, "is_error": is_error, "denied": False}


def config(**anomalies):
    merged = copy.deepcopy(CONFIG)
    merged.setdefault("anomalies", {}).update(anomalies)
    return merged


def found(records, conf=None, key=None):
    results = rules.anomalies(records, conf or CONFIG)
    return [item for item in results if key is None or item["key"] == key]


def stamp(minute, second=0, hour=10):
    total = hour * 3600 + minute * 60 + second
    return "2026-09-%02dT%02d:%02d:%02d+00:00" % (
        6 + total // 86400,
        total // 3600 % 24,
        total // 60 % 60,
        total % 60,
    )


REPLY = "reply_skill_headless"
REPEAT = "repeated_tool_input"
GROWTH = "context_growth_tool"
FAIL = "failing_tool"
UNATTRIBUTED = "unattributed_subagents"
MCP = "mcp_server_share"


def reply_records(headless_turns, typed_turns):
    records = []
    for index in range(headless_turns):
        records.append(
            rec(
                stamp(index),
                session="headless%d" % index,
                skill="prose:reply-style",
                cwd=HEADLESS_CWD,
                output=1000,
                uuid="h%d" % index,
            )
        )
    for index in range(typed_turns):
        records.append(
            rec(
                stamp(index),
                session="typed%d" % index,
                skill="prose:reply-style",
                cwd=TYPED_CWD,
                output=1000,
                uuid="t%d" % index,
            )
        )
    return records


def test_a_reply_skill_mostly_inside_headless_sessions_is_an_anomaly():
    items = found(reply_records(7, 3), key=REPLY)
    assert len(items) == 1
    assert items[0]["subject"] == "prose:reply-style"
    assert "70" in items[0]["claim"]


def test_a_reply_skill_below_the_headless_share_is_not_an_anomaly():
    assert found(reply_records(2, 8), key=REPLY) == []


def test_the_reply_skill_action_names_the_skill_file_and_the_project():
    action = found(reply_records(7, 3), key=REPLY)[0]["action"]
    assert "prose/skills/reply-style/SKILL.md" in action
    assert "daily-improvement-review" in action


def test_a_session_marked_headless_by_its_prompt_source_counts_as_headless():
    records = [
        rec(stamp(i), session="s%d" % i, skill="prose:reply-style", cwd=TYPED_CWD,
            prompt_source="scheduled", output=1000, uuid="p%d" % i)
        for i in range(7)
    ] + [
        rec(stamp(i), session="t%d" % i, skill="prose:reply-style", cwd=TYPED_CWD, output=1000, uuid="q%d" % i)
        for i in range(3)
    ]
    assert len(found(records, key=REPLY)) == 1


def test_a_session_marked_headless_by_its_entrypoint_counts_as_headless():
    records = [
        rec(stamp(i), session="s%d" % i, skill="prose:reply-style", cwd=TYPED_CWD,
            entrypoint="sdk-cli", output=1000, uuid="e%d" % i)
        for i in range(7)
    ] + [
        rec(stamp(i), session="t%d" % i, skill="prose:reply-style", cwd=TYPED_CWD, output=1000, uuid="f%d" % i)
        for i in range(3)
    ]
    assert len(found(records, key=REPLY)) == 1


def test_a_skill_that_is_not_a_reply_skill_is_never_this_anomaly():
    records = reply_records(7, 3)
    for record in records:
        record["attributionSkill"] = "glab"
    assert found(records, key=REPLY) == []


def repeat_records(count, span_minutes=10, digest="h1"):
    step = (span_minutes * 60.0) / max(1, count - 1) if count > 1 else 0
    records = []
    for index in range(count):
        seconds = int(index * step)
        records.append(
            rec(
                stamp(seconds // 60, seconds % 60),
                agent_id="run1",
                sidechain=True,
                output=100,
                tools=[tool("Bash", digest)],
                prompt="Collapse PPS to a single master-data call",
                uuid="r%d" % index,
            )
        )
    return records


def test_the_same_tool_input_repeated_past_the_threshold_is_an_anomaly():
    items = found(repeat_records(101), key=REPEAT)
    assert len(items) == 1
    assert items[0]["numbers"]["repeats"] == 101
    assert "101" in items[0]["claim"]


def test_repeats_below_the_minimum_are_not_an_anomaly():
    assert found(repeat_records(20), key=REPEAT) == []


def test_repeats_spread_beyond_the_window_are_not_an_anomaly():
    assert found(repeat_records(25, span_minutes=90), key=REPEAT) == []


def test_the_repeat_action_names_the_run_and_the_tool():
    action = found(repeat_records(101), key=REPEAT)[0]["action"]
    assert "Bash" in action
    assert "Collapse PPS to a single master-data call" in action


def growth_records(tool_name="Bash", result_chars=40000, turns=30, other_chars=0):
    records = []
    carry = 0
    for index in range(turns):
        calls = [tool(tool_name, "h%d" % index, result_chars=result_chars)]
        if other_chars:
            calls.append(tool("Read", "o%d" % index, result_chars=other_chars))
        records.append(
            rec(
                stamp(index),
                session="grow",
                cache_read=carry,
                output=50,
                tools=calls,
                uuid="g%d" % index,
            )
        )
        carry += result_chars // 2 + (other_chars // 2 if other_chars else 0)
    return records


def test_one_tool_dominating_context_growth_with_large_results_is_an_anomaly():
    items = found(growth_records(), key=GROWTH)
    assert len(items) == 1
    assert items[0]["subject"] == "Bash"
    assert items[0]["numbers"]["median_result_bytes"] == 40000


def test_a_dominating_tool_with_small_results_and_little_volume_is_not_an_anomaly():
    assert found(growth_records(result_chars=2000), key=GROWTH) == []


def test_a_dominating_tool_with_small_results_but_megabytes_of_them_is_an_anomaly():
    items = found(growth_records(result_chars=1024, turns=5000), key=GROWTH)
    assert len(items) == 1
    assert items[0]["numbers"]["total_result_mb"] > 4.0


def test_a_tool_with_too_few_results_is_not_an_anomaly():
    assert found(growth_records(turns=15), key=GROWTH) == []


def test_a_tool_below_the_growth_share_is_not_an_anomaly():
    assert found(growth_records(other_chars=40000), key=GROWTH) == []


def test_the_growth_action_names_the_tool_and_the_median_size():
    action = found(growth_records(), key=GROWTH)[0]["action"]
    assert "Bash" in action
    assert "39 KB" in action


def fail_records(bash_failures, other_failures):
    records = []
    for index in range(bash_failures):
        records.append(
            rec(stamp(index % 60), output=100, tools=[tool("Bash", "b%d" % index, 10, True)], uuid="fb%d" % index)
        )
    for index in range(other_failures):
        records.append(
            rec(stamp(index % 60), output=100, tools=[tool("Grep", "o%d" % index, 10, True)], uuid="fo%d" % index)
        )
    return records


def test_failures_concentrated_in_one_tool_are_an_anomaly():
    items = found(fail_records(155, 37), key=FAIL)
    assert len(items) == 1
    assert items[0]["subject"] == "Bash"
    assert items[0]["numbers"]["failures"] == 155


def test_too_few_failures_are_not_an_anomaly():
    assert found(fail_records(30, 5), key=FAIL) == []


def test_failures_spread_across_tools_are_not_an_anomaly():
    assert found(fail_records(60, 60), key=FAIL) == []


def test_the_failure_action_names_the_tool():
    assert "Bash" in found(fail_records(155, 37), key=FAIL)[0]["action"]


def unattributed_records(unnamed, named):
    records = [
        rec(stamp(i), sidechain=True, agent_id="u%d" % i, output=1000, uuid="uu%d" % i) for i in range(unnamed)
    ]
    records += [
        rec(stamp(i), sidechain=True, agent_id="n%d" % i, agent="Explore", output=1000, uuid="nn%d" % i)
        for i in range(named)
    ]
    return records


def test_mostly_unattributed_subagent_spend_is_an_anomaly():
    items = found(unattributed_records(7, 3), key=UNATTRIBUTED)
    assert len(items) == 1
    assert "subagent_type" in items[0]["action"]


def test_subagent_spend_that_is_mostly_attributed_is_not_an_anomaly():
    assert found(unattributed_records(3, 7), key=UNATTRIBUTED) == []


def mcp_records(server_turns, other_turns, server="claude.ai Linear"):
    records = [
        rec(stamp(i % 60), mcp_server=server, output=1000, uuid="m%d" % i) for i in range(server_turns)
    ]
    records += [rec(stamp(i % 60), output=1000, uuid="x%d" % i) for i in range(other_turns)]
    return records


def test_an_mcp_server_above_its_share_of_the_window_is_an_anomaly():
    items = found(mcp_records(6, 94), key=MCP)
    assert len(items) == 1
    assert items[0]["subject"] == "claude.ai Linear"
    assert items[0]["numbers"]["turns"] == 6


def test_an_mcp_server_below_its_share_is_not_an_anomaly():
    assert found(mcp_records(2, 98), key=MCP) == []


def test_the_mcp_action_names_the_server_and_its_turn_count():
    action = found(mcp_records(6, 94), key=MCP)[0]["action"]
    assert "claude.ai Linear" in action
    assert "6" in action


def test_every_anomaly_carries_a_claim_numbers_a_chart_an_action_coverage_and_a_score():
    records = reply_records(7, 3) + repeat_records(101) + fail_records(155, 37) + mcp_records(6, 94)
    items = rules.anomalies(records, CONFIG)
    assert items
    for item in items:
        assert item["claim"] and item["action"]
        assert isinstance(item["numbers"], dict) and item["numbers"]
        assert item["chart"]["kind"] in ("bars", "table")
        assert 0.0 <= item["coverage"]["share"] <= 1.0
        assert item["coverage"]["field"]
        assert item["score"] >= 1.0


def test_anomalies_come_back_ranked_by_score():
    records = reply_records(7, 3) + repeat_records(101) + fail_records(155, 37) + mcp_records(6, 94)
    scores = [item["score"] for item in rules.anomalies(records, CONFIG)]
    assert scores == sorted(scores, reverse=True)


def test_a_window_with_nothing_strange_has_no_anomalies():
    assert rules.anomalies([rec(stamp(i), output=10, uuid="q%d" % i) for i in range(30)], CONFIG) == []


def test_the_thresholds_come_from_the_config():
    assert found(repeat_records(25), conf=config(repeat_min=200), key=REPEAT) == []
    assert found(mcp_records(6, 94), conf=config(mcp_share=0.9), key=MCP) == []


def test_the_shipped_config_carries_the_anomaly_defaults():
    block = CONFIG["anomalies"]
    assert block["reply_skills"] == ["prose:reply-style", "prose:bro"]
    assert block["repeat_min"] == 20
    assert block["repeat_minutes"] == 15
    assert block["growth_share"] == 0.6
    assert block["result_kb"] == 8
    assert block["fail_min"] == 50
    assert block["unattributed_share"] == 0.5
    assert block["mcp_share"] == 0.04
