import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())


def entry(**overrides):
    base = {
        "type": "assistant",
        "uuid": "u1",
        "timestamp": "2026-08-25T10:00:00.123Z",
        "sessionId": "s1",
        "isSidechain": False,
        "effort": "high",
        "cwd": "C:\\workspace\\srst",
        "gitBranch": "master",
        "version": "2.1.227",
        "message": {
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_creation_input_tokens": 400,
                "cache_read_input_tokens": 8000,
                "output_tokens_details": {"thinking_tokens": 50},
            },
        },
    }
    base.update(overrides)
    return base


def test_non_assistant_entry_is_not_a_record():
    assert collect.normalize({"type": "user", "message": {"content": "hi"}}, CONFIG) is None


def test_assistant_entry_without_usage_is_not_a_record():
    assert collect.normalize({"type": "assistant", "message": {"model": "x"}}, CONFIG) is None


def test_token_counts_are_extracted():
    r = collect.normalize(entry(), CONFIG)
    assert (r["input"], r["output"], r["cache_create"], r["cache_read"], r["thinking"]) == (100, 200, 400, 8000, 50)


def test_weighted_cost_applies_class_and_model_weights():
    r = collect.normalize(entry(), CONFIG)
    assert r["weighted"] == 1.0 * (100 + 1.25 * 400 + 0.1 * 8000 + 5.0 * 200)


def test_opus_costs_five_times_sonnet_for_the_same_usage():
    sonnet = collect.normalize(entry(), CONFIG)
    opus = collect.normalize(entry(message=dict(entry()["message"], model="claude-opus-5")), CONFIG)
    assert opus["weighted"] == 5.0 * sonnet["weighted"]


def test_unknown_model_falls_back_to_default_weight_and_is_flagged():
    r = collect.normalize(entry(message=dict(entry()["message"], model="claude-zebra-9")), CONFIG)
    assert r["model"] == "claude-zebra-9"
    assert r["model_known"] is False
    assert r["weighted"] == 1.0 * (100 + 1.25 * 400 + 0.1 * 8000 + 5.0 * 200)


def test_known_model_is_flagged_known():
    assert collect.normalize(entry(), CONFIG)["model_known"] is True


def test_timestamp_is_normalized_to_utc_z():
    assert collect.normalize(entry(), CONFIG)["ts"] == "2026-08-25T10:00:00.123000+00:00"


def test_sidechain_attribution_fields_are_carried():
    r = collect.normalize(
        entry(isSidechain=True, agentId="a1", attributionAgent="general-purpose", attributionSkill="glab"),
        CONFIG,
    )
    assert (r["isSidechain"], r["agentId"], r["attributionAgent"], r["attributionSkill"]) == (
        True,
        "a1",
        "general-purpose",
        "glab",
    )


def test_missing_optional_fields_do_not_crash():
    e = entry()
    del e["effort"], e["gitBranch"]
    r = collect.normalize(e, CONFIG)
    assert r["effort"] is None and r["gitBranch"] is None


def test_tool_calls_are_captured_with_a_stable_input_hash():
    e = entry()
    e["message"]["content"] = [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "b.py"}},
    ]
    r = collect.normalize(e, CONFIG)
    assert [t["name"] for t in r["tools"]] == ["Read", "Read"]
    assert r["tools"][0]["hash"] != r["tools"][1]["hash"]
    again = collect.normalize(e, CONFIG)
    assert again["tools"][0]["hash"] == r["tools"][0]["hash"]


def test_tool_input_hash_ignores_key_order():
    a = entry()
    a["message"]["content"] = [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"x": 1, "y": 2}}]
    b = entry()
    b["message"]["content"] = [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"y": 2, "x": 1}}]
    assert collect.normalize(a, CONFIG)["tools"][0]["hash"] == collect.normalize(b, CONFIG)["tools"][0]["hash"]


def test_text_length_is_recorded_for_model_mismatch_detection():
    assert collect.normalize(entry(), CONFIG)["text_chars"] == len("hello")


def test_api_error_entries_are_flagged():
    assert collect.normalize(entry(isApiErrorMessage=True), CONFIG)["is_api_error"] is True
    assert collect.normalize(entry(), CONFIG)["is_api_error"] is False


def test_a_point_release_suffix_is_priced_at_its_family_weight():
    r = collect.normalize(entry(message=dict(entry()["message"], model="claude-fable-5-1")), CONFIG)
    assert r["model_known"] is True
    assert r["weighted"] == 5.0 * (100 + 1.25 * 400 + 0.1 * 8000 + 5.0 * 200)


def test_a_dated_suffix_is_priced_at_its_family_weight():
    r = collect.normalize(entry(message=dict(entry()["message"], model="claude-sonnet-5-20260130")), CONFIG)
    assert r["model_known"] is True
    assert r["weighted"] == 1.0 * (100 + 1.25 * 400 + 0.1 * 8000 + 5.0 * 200)


def test_an_exact_entry_beats_the_family_weight():
    config = json.loads(json.dumps(CONFIG))
    config["model_weights"]["claude-opus-5-cheap"] = 0.5
    r = collect.normalize(entry(message=dict(entry()["message"], model="claude-opus-5-cheap")), config)
    assert r["weighted"] == 0.5 * (100 + 1.25 * 400 + 0.1 * 8000 + 5.0 * 200)


def test_synthetic_records_stay_free():
    r = collect.normalize(entry(message=dict(entry()["message"], model="<synthetic>")), CONFIG)
    assert r["weighted"] == 0.0
    assert r["model_known"] is True


def test_a_family_with_conflicting_weights_does_not_price_its_suffixes():
    config = json.loads(json.dumps(CONFIG))
    config["model_weights"]["claude-sonnet-6"] = 2.0
    r = collect.normalize(entry(message=dict(entry()["message"], model="claude-sonnet-7")), config)
    assert r["model_known"] is False
    assert r["weighted"] == 1.0 * (100 + 1.25 * 400 + 0.1 * 8000 + 5.0 * 200)


def test_a_model_of_an_unconfigured_family_is_still_unknown():
    r = collect.normalize(entry(message=dict(entry()["message"], model="claude-zebra-9-1")), CONFIG)
    assert r["model_known"] is False


def test_mcp_plugin_and_effort_attribution_is_carried():
    r = collect.normalize(
        entry(
            attributionMcpServer="datadog-mcp",
            attributionMcpTool="search_datadog_logs",
            attributionPlugin="prose",
            perTurnEffort="high",
        ),
        CONFIG,
    )
    assert r["mcp_server"] == "datadog-mcp"
    assert r["mcp_tool"] == "search_datadog_logs"
    assert r["plugin"] == "prose"
    assert r["per_turn_effort"] == "high"


def test_attribution_fields_absent_from_the_entry_are_none():
    r = collect.normalize(entry(), CONFIG)
    assert (r["mcp_server"], r["mcp_tool"], r["plugin"], r["per_turn_effort"]) == (None, None, None, None)


def test_stop_reason_is_carried_from_the_message():
    assert collect.normalize(entry(message=dict(entry()["message"], stop_reason="tool_use")), CONFIG)["stop_reason"] == "tool_use"
    assert collect.normalize(entry(), CONFIG)["stop_reason"] is None


def test_the_cache_creation_split_is_kept_and_still_sums_to_cache_create():
    e = entry()
    e["message"]["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 300,
        "ephemeral_1h_input_tokens": 100,
    }
    r = collect.normalize(e, CONFIG)
    assert (r["cache_create_5m"], r["cache_create_1h"]) == (300, 100)
    assert r["cache_create"] == 400


def test_the_cache_split_does_not_change_the_weighted_cost():
    plain = collect.normalize(entry(), CONFIG)
    e = entry()
    e["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 400}
    assert collect.normalize(e, CONFIG)["weighted"] == plain["weighted"]


def test_a_turn_without_a_cache_creation_object_has_no_split():
    r = collect.normalize(entry(), CONFIG)
    assert (r["cache_create_5m"], r["cache_create_1h"]) == (None, None)


def test_a_context_managed_turn_is_flagged_as_compacted():
    e = entry()
    e["message"]["context_management"] = {"applied_edits": []}
    assert collect.normalize(e, CONFIG)["compacted"] is True
    assert collect.normalize(entry(), CONFIG)["compacted"] is False


def test_a_tool_call_keeps_the_id_the_result_will_be_joined_on():
    e = entry()
    e["message"]["content"] = [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]
    assert collect.normalize(e, CONFIG)["tools"][0]["tool_use_id"] == "toolu_1"


def test_the_entrypoint_of_a_turn_is_captured():
    assert collect.normalize(entry(entrypoint="sdk-cli"), CONFIG)["entrypoint"] == "sdk-cli"


def test_a_turn_without_an_entrypoint_records_none():
    assert collect.normalize(entry(), CONFIG)["entrypoint"] is None


def test_a_double_encoded_prompt_is_repaired_when_it_is_captured():
    record = collect._prompt_text(
        {"message": {"content": [{"type": "text", "text": "acting for MatÃºÅ¡ Vida"}]}}, 200
    )
    assert record == "acting for Matúš Vida"


def test_a_double_encoded_agent_dispatch_prompt_is_repaired():
    calls = collect.agent_calls(
        {
            "type": "assistant",
            "uuid": "u1",
            "timestamp": "2026-08-25T10:00:00.000Z",
            "sessionId": "s1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Agent",
                        "input": {"prompt": "acting for MatÃºÅ¡ Vida", "description": "d"},
                    }
                ]
            },
        },
        CONFIG,
    )
    assert calls[0]["prompt_head"] == "acting for Matúš Vida"
