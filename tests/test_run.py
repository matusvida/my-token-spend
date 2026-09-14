import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())


def assistant_line(uuid, ts, output=1000, session="s1", model="claude-sonnet-5"):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": session,
            "isSidechain": False,
            "effort": "high",
            "cwd": "C:\\workspace\\srst",
            "gitBranch": "master",
            "version": "2.1.227",
            "message": {
                "model": model,
                "content": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": output,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    return {"root": tmp_path / "projects", "out": out, "state": out / "state.json", "proj": root}


def run(workspace, **kwargs):
    return collect.run(
        config=CONFIG,
        root=workspace["root"],
        out_dir=workspace["out"],
        state_path=workspace["state"],
        **kwargs,
    )


def test_run_writes_the_window_json_and_markdown(workspace):
    (workspace["proj"] / "a.jsonl").write_text(assistant_line("u1", "2026-08-25T10:00:00Z") + "\n", encoding="utf-8")
    run(workspace)
    assert (workspace["out"] / "data" / "week_2026_08_22.json").exists()
    assert (workspace["out"] / "reports" / "week_2026_08_22.md").exists()


def test_a_session_straddling_the_reset_is_split_across_two_windows(workspace):
    lines = [
        assistant_line("u1", "2026-08-28T20:00:00Z", output=100),
        assistant_line("u2", "2026-08-28T23:30:00Z", output=200),
        assistant_line("u3", "2026-08-29T09:00:00Z", output=300),
    ]
    (workspace["proj"] / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(workspace)
    old = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    new = json.loads((workspace["out"] / "data" / "week_2026_08_29.json").read_text())
    assert old["totals"]["turns"] == 1 and old["totals"]["output"] == 100
    assert new["totals"]["turns"] == 2 and new["totals"]["output"] == 500


def test_a_session_straddling_the_reset_splits_its_usd_like_its_weighted_tokens(workspace):
    lines = [
        assistant_line("u1", "2026-08-28T20:00:00Z", output=100),
        cost_state_line("s1", 1.0),
        assistant_line("u2", "2026-08-29T09:00:00Z", output=200),
        assistant_line("u3", "2026-08-29T10:00:00Z", output=300),
        cost_state_line("s1", 6.0),
    ]
    (workspace["proj"] / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(workspace)
    old = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    new = json.loads((workspace["out"] / "data" / "week_2026_08_29.json").read_text())
    assert old["cost_usd"]["usd"] == 1.0
    assert new["cost_usd"]["usd"] == 5.0
    assert old["cost_usd"]["crossing_sessions"] == 1
    assert new["cost_usd"]["crossing_sessions"] == 1
    old_share = old["cost_usd"]["usd"] / (old["cost_usd"]["usd"] + new["cost_usd"]["usd"])
    weighted_share = old["totals"]["weighted"] / (old["totals"]["weighted"] + new["totals"]["weighted"])
    assert abs(old_share - weighted_share) < 0.01


def test_a_run_over_an_empty_corpus_fails_loudly(workspace):
    with pytest.raises(collect.CollectionError):
        run(workspace)


def test_a_run_over_transcripts_without_usage_fails_loudly(workspace):
    (workspace["proj"] / "a.jsonl").write_text(json.dumps({"type": "system", "x": 1}) + "\n", encoding="utf-8")
    with pytest.raises(collect.CollectionError):
        run(workspace)


def test_repeated_runs_converge_on_the_same_window_file(workspace):
    (workspace["proj"] / "a.jsonl").write_text(assistant_line("u1", "2026-08-25T10:00:00Z") + "\n", encoding="utf-8")
    run(workspace)
    first = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    run(workspace)
    second = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    first["generated_at"] = second["generated_at"] = None
    first["window"] = second["window"] = None
    first["ceiling"] = second["ceiling"] = None
    assert first == second


def test_an_incremental_second_run_still_reports_the_full_window(workspace):
    path = workspace["proj"] / "a.jsonl"
    path.write_text(assistant_line("u1", "2026-08-25T10:00:00Z") + "\n", encoding="utf-8")
    run(workspace)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(assistant_line("u2", "2026-08-26T10:00:00Z") + "\n")
    run(workspace)
    window = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    assert window["totals"]["turns"] == 2


def test_re_reading_a_rotated_file_does_not_double_count(workspace):
    path = workspace["proj"] / "a.jsonl"
    path.write_text(assistant_line("u1", "2026-08-25T10:00:00Z") + "\n", encoding="utf-8")
    run(workspace)
    run(workspace, backfill=True)
    window = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    assert window["totals"]["turns"] == 1


def test_state_is_persisted_between_runs(workspace):
    (workspace["proj"] / "a.jsonl").write_text(assistant_line("u1", "2026-08-25T10:00:00Z") + "\n", encoding="utf-8")
    run(workspace)
    state = json.loads(workspace["state"].read_text())
    assert list(state["files"].values())[0]["offset"] > 0


def test_window_filter_writes_only_the_requested_window(workspace):
    lines = [
        assistant_line("u1", "2026-08-25T10:00:00Z"),
        assistant_line("u2", "2026-08-30T10:00:00Z"),
    ]
    (workspace["proj"] / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(workspace, window="2026-08-29")
    assert (workspace["out"] / "data" / "week_2026_08_29.json").exists()
    assert not (workspace["out"] / "data" / "week_2026_08_22.json").exists()


def test_malformed_lines_are_counted_in_the_window(workspace):
    lines = [assistant_line("u1", "2026-08-25T10:00:00Z"), "{broken", assistant_line("u2", "2026-08-25T11:00:00Z")]
    (workspace["proj"] / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(workspace)
    window = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    assert window["parse"]["malformed_lines"] == 1
    assert window["totals"]["turns"] == 2


def test_ceiling_is_calibrated_across_all_windows(workspace):
    lines = [
        assistant_line("u1", "2026-08-01T10:00:00Z", output=1000),
        assistant_line("u2", "2026-08-09T10:00:00Z", output=5000),
        assistant_line("u3", "2026-08-16T10:00:00Z", output=9000),
        assistant_line("u4", "2026-08-25T10:00:00Z", output=100),
    ]
    (workspace["proj"] / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(workspace)
    window = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    assert window["ceiling"]["method"] == "top-cluster"
    assert window["ceiling"]["estimate"] > 0


def test_markdown_names_the_window_and_its_findings(workspace):
    lines = [assistant_line("u%d" % i, "2026-08-25T10:%02d:00Z" % i, output=50000) for i in range(12)]
    (workspace["proj"] / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(workspace)
    text = (workspace["out"] / "reports" / "week_2026_08_22.md").read_text(encoding="utf-8")
    assert "week_2026_08_22" in text
    assert "whale_turns" in text
    assert "estimate" in text.lower()


def test_subdirectories_of_the_transcript_root_are_walked(workspace):
    nested = workspace["proj"] / "session" / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent.jsonl").write_text(assistant_line("u1", "2026-08-25T10:00:00Z") + "\n", encoding="utf-8")
    run(workspace)
    window = json.loads((workspace["out"] / "data" / "week_2026_08_22.json").read_text())
    assert window["totals"]["turns"] == 1


def _tool_turn(uuid, ts, call_id):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": "s1",
            "isSidechain": False,
            "cwd": "C:\workspace\srst",
            "message": {
                "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {"c": call_id}}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


def _result(call_id, text, is_error=True):
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": is_error}
                ]
            },
        }
    )


def stored_tools(workspace, key="week_2026_08_22"):
    path = workspace["out"] / "data" / "records" / (key + ".jsonl")
    return [json.loads(line)["tools"] for line in path.read_text().splitlines() if line.strip()]


def test_a_result_that_arrives_in_a_later_pass_is_filled_into_the_stored_turn(workspace):
    transcript = workspace["proj"] / "a.jsonl"
    transcript.write_text(_tool_turn("u1", "2026-08-25T10:00:00Z", "c1") + "\n", encoding="utf-8")
    run(workspace)
    assert stored_tools(workspace)[0][0]["result_chars"] is None

    transcript.write_text(
        _tool_turn("u1", "2026-08-25T10:00:00Z", "c1") + "\n" + _result("c1", "boom") + "\n", encoding="utf-8"
    )
    run(workspace)
    tool = stored_tools(workspace)[0][0]
    assert (tool["result_chars"], tool["is_error"], tool["denied"]) == (4, True, False)


def _agent_turn(uuid, ts, call_id, description="Implement section 2", model="opus"):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": "s1",
            "isSidechain": False,
            "cwd": "C:\workspace\srst",
            "message": {
                "model": "claude-opus-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "Agent",
                        "input": {
                            "description": description,
                            "subagent_type": "general-purpose",
                            "model": model,
                            "prompt": "x" * 900,
                        },
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


def agent_calls(workspace, key="week_2026_08_22"):
    path = workspace["out"] / "data" / "records" / ("agent_calls_" + key + ".jsonl")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_an_agent_dispatch_is_stored_with_its_description(workspace):
    (workspace["proj"] / "a.jsonl").write_text(
        _agent_turn("u1", "2026-08-25T10:00:00Z", "toolu_1") + "\n", encoding="utf-8"
    )
    run(workspace)
    call = agent_calls(workspace)[0]
    assert call["tool_use_id"] == "toolu_1"
    assert call["description"] == "Implement section 2"
    assert call["subagent_type"] == "general-purpose"
    assert call["model"] == "opus"
    assert call["sessionId"] == "s1"
    assert call["parent_uuid"] == "u1"
    assert call["prompt_chars"] == 900
    assert len(call["prompt_head"]) == CONFIG["prompt_label_chars"]


def test_agent_dispatches_are_deduplicated_by_tool_use_id(workspace):
    line = _agent_turn("u1", "2026-08-25T10:00:00Z", "toolu_1")
    (workspace["proj"] / "a.jsonl").write_text(line + "\n", encoding="utf-8")
    run(workspace)
    (workspace["proj"] / "a.jsonl").write_text(
        line + "\n" + _agent_turn("u2", "2026-08-25T11:00:00Z", "toolu_2") + "\n", encoding="utf-8"
    )
    run(workspace, backfill=True)
    assert sorted(c["tool_use_id"] for c in agent_calls(workspace)) == ["toolu_1", "toolu_2"]


def test_agent_dispatches_are_never_lost_when_the_transcript_is_pruned(workspace):
    transcript = workspace["proj"] / "a.jsonl"
    transcript.write_text(_agent_turn("u1", "2026-08-25T10:00:00Z", "toolu_1") + "\n", encoding="utf-8")
    run(workspace)
    transcript.write_text(_agent_turn("u2", "2026-08-25T11:00:00Z", "toolu_2") + "\n", encoding="utf-8")
    run(workspace, backfill=True)
    assert sorted(c["tool_use_id"] for c in agent_calls(workspace)) == ["toolu_1", "toolu_2"]


def test_a_non_agent_tool_call_is_not_an_agent_dispatch(workspace):
    (workspace["proj"] / "a.jsonl").write_text(
        _tool_turn("u1", "2026-08-25T10:00:00Z", "c1") + "\n", encoding="utf-8"
    )
    run(workspace)
    assert not list((workspace["out"] / "data" / "records").glob("agent_calls_*.jsonl"))


def cost_state_line(session, usd, ts_models=None):
    return json.dumps(
        {
            "type": "cost-state",
            "sessionId": session,
            "totalCostUSD": usd,
            "modelUsage": ts_models
            or {"claude-sonnet-5": {"inputTokens": 10, "outputTokens": 1000, "costUSD": usd}},
        }
    )


def test_run_prices_a_window_from_the_cost_state_entries(workspace):
    (workspace["proj"] / "a.jsonl").write_text(
        "\n".join(
            [
                assistant_line("u1", "2026-08-25T10:00:00Z"),
                cost_state_line("s1", 0.5),
                assistant_line("u2", "2026-08-25T11:00:00Z"),
                cost_state_line("s1", 1.25),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary = run(workspace)
    block = summary["windows"][0]["cost_usd"]
    assert block["usd"] == 1.25
    assert block["priced_sessions"] == 1
    assert "not what the subscription bills" in block["label"]


def test_a_session_is_priced_into_the_window_of_the_cost_state_entry(workspace):
    (workspace["proj"] / "a.jsonl").write_text(
        "\n".join(
            [
                assistant_line("u1", "2026-08-21T10:00:00Z"),
                assistant_line("u2", "2026-08-25T10:00:00Z"),
                cost_state_line("s1", 2.0),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary = run(workspace)
    by_key = {w["window"]["key"]: w["cost_usd"] for w in summary["windows"]}
    assert by_key["week_2026_08_15"]["usd"] is None
    assert by_key["week_2026_08_22"]["usd"] == 2.0


def test_the_cost_store_survives_a_transcript_that_no_longer_carries_the_state(workspace):
    path = workspace["proj"] / "a.jsonl"
    path.write_text(
        assistant_line("u1", "2026-08-25T10:00:00Z") + "\n" + cost_state_line("s1", 3.0) + "\n",
        encoding="utf-8",
    )
    run(workspace)
    path.write_text(assistant_line("u1", "2026-08-25T10:00:00Z") + "\n", encoding="utf-8")
    summary = run(workspace, backfill=True)
    assert summary["windows"][0]["cost_usd"]["usd"] == 3.0
