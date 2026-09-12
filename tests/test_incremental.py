import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())


def assistant_line(uuid, ts="2026-08-25T10:00:00Z", tokens=100):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": "s1",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-5",
                "content": [],
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


def user_line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def write(path, lines, trailing_newline=True):
    body = "\n".join(lines) + ("\n" if trailing_newline else "")
    path.write_text(body, encoding="utf-8")


def test_fresh_file_yields_every_record_and_records_the_full_offset(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1"), assistant_line("u2")])
    result = collect.read_file(f, CONFIG, None)
    assert [r["uuid"] for r in result["records"]] == ["u1", "u2"]
    assert result["state"]["offset"] == f.stat().st_size


def test_unchanged_file_is_skipped_on_the_second_run(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1")])
    first = collect.read_file(f, CONFIG, None)
    second = collect.read_file(f, CONFIG, first["state"])
    assert second["records"] == []
    assert second["state"]["offset"] == first["state"]["offset"]


def test_appended_lines_are_the_only_new_records(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1")])
    first = collect.read_file(f, CONFIG, None)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(assistant_line("u2") + "\n")
    second = collect.read_file(f, CONFIG, first["state"])
    assert [r["uuid"] for r in second["records"]] == ["u2"]


def test_truncated_file_is_reread_from_the_start(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1"), assistant_line("u2"), assistant_line("u3")])
    first = collect.read_file(f, CONFIG, None)
    write(f, [assistant_line("v1")])
    second = collect.read_file(f, CONFIG, first["state"])
    assert [r["uuid"] for r in second["records"]] == ["v1"]
    assert second["state"]["offset"] == f.stat().st_size


def test_rotated_file_of_equal_size_but_new_mtime_is_reread(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1")])
    first = collect.read_file(f, CONFIG, None)
    write(f, [assistant_line("v1")])
    os.utime(f, (first["state"]["mtime"] + 10, first["state"]["mtime"] + 10))
    second = collect.read_file(f, CONFIG, first["state"])
    assert [r["uuid"] for r in second["records"]] == ["v1"]


def test_malformed_lines_are_skipped_and_counted(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1"), "{not json", assistant_line("u2")])
    result = collect.read_file(f, CONFIG, None)
    assert [r["uuid"] for r in result["records"]] == ["u1", "u2"]
    assert result["malformed"] == 1


def test_partial_trailing_line_is_not_consumed_until_complete(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1"), assistant_line("u2")], trailing_newline=False)
    first = collect.read_file(f, CONFIG, None)
    assert [r["uuid"] for r in first["records"]] == ["u1"]
    with f.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    second = collect.read_file(f, CONFIG, first["state"])
    assert [r["uuid"] for r in second["records"]] == ["u2"]


def test_records_carry_the_preceding_user_prompt(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [user_line("fix the failing build"), assistant_line("u1")])
    result = collect.read_file(f, CONFIG, None)
    assert result["records"][0]["prompt"] == "fix the failing build"


def test_prompt_carries_across_an_incremental_resume(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [user_line("fix the failing build"), assistant_line("u1")])
    first = collect.read_file(f, CONFIG, None)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(assistant_line("u2") + "\n")
    second = collect.read_file(f, CONFIG, first["state"])
    assert second["records"][0]["prompt"] == "fix the failing build"


def test_prompt_is_truncated_to_the_configured_length(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [user_line("x" * 500), assistant_line("u1")])
    result = collect.read_file(f, CONFIG, None)
    assert len(result["records"][0]["prompt"]) == CONFIG["prompt_label_chars"]


def test_tool_result_user_turns_do_not_overwrite_the_prompt(tmp_path):
    f = tmp_path / "a.jsonl"
    tool_result = json.dumps(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    )
    write(f, [user_line("real prompt"), tool_result, assistant_line("u1")])
    result = collect.read_file(f, CONFIG, None)
    assert result["records"][0]["prompt"] == "real prompt"


def tool_line(uuid, calls, ts="2026-08-25T10:00:00Z"):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": "s1",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "tool_use", "id": call_id, "name": name, "input": {"n": call_id}}
                    for call_id, name in calls
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


def result_line(call_id, content, is_error=False):
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}
                ]
            },
        }
    )


def test_a_tool_result_is_joined_onto_the_call_that_issued_it(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [tool_line("u1", [("c1", "Bash"), ("c2", "Read")]), result_line("c1", "boom", True), result_line("c2", "ok")])
    tools = collect.read_file(f, CONFIG, None)["records"][0]["tools"]
    assert (tools[0]["result_chars"], tools[0]["is_error"], tools[0]["denied"]) == (4, True, False)
    assert (tools[1]["result_chars"], tools[1]["is_error"], tools[1]["denied"]) == (2, False, False)


def test_a_permission_denial_is_flagged_as_denied(tmp_path):
    f = tmp_path / "a.jsonl"
    denial = "The user doesn't want to proceed with this tool use. The tool use was rejected"
    write(f, [tool_line("u1", [("c1", "Edit")]), result_line("c1", denial, True)])
    tool = collect.read_file(f, CONFIG, None)["records"][0]["tools"][0]
    assert tool["denied"] is True and tool["is_error"] is True


def test_result_chars_count_only_the_text_of_a_block_list(tmp_path):
    f = tmp_path / "a.jsonl"
    blocks = [{"type": "text", "text": "abcde"}, {"type": "image", "source": {"data": "x" * 500}}]
    write(f, [tool_line("u1", [("c1", "Read")]), result_line("c1", blocks)])
    assert collect.read_file(f, CONFIG, None)["records"][0]["tools"][0]["result_chars"] == 5


def test_a_call_whose_result_has_not_arrived_yet_carries_no_outcome(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [tool_line("u1", [("c1", "Bash")])])
    first = collect.read_file(f, CONFIG, None)
    tool = first["records"][0]["tools"][0]
    assert (tool["result_chars"], tool["is_error"], tool["denied"]) == (None, None, None)
    assert first["pending_results"] == {}


def test_a_result_arriving_after_its_turn_was_stored_is_handed_back_for_patching(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [tool_line("u1", [("c1", "Bash")])])
    first = collect.read_file(f, CONFIG, None)
    write(f, [tool_line("u1", [("c1", "Bash")]), result_line("c1", "boom", True)])
    second = collect.read_file(f, CONFIG, first["state"])
    assert second["records"] == []
    assert second["pending_results"]["c1"] == {"result_chars": 4, "is_error": True, "denied": False}


def test_the_turn_after_a_compact_summary_is_flagged(tmp_path):
    f = tmp_path / "a.jsonl"
    compact = json.dumps({"type": "user", "isCompactSummary": True, "message": {"content": "summary"}})
    write(f, [assistant_line("u1"), compact, assistant_line("u2"), assistant_line("u3")])
    flags = [r["after_compaction"] for r in collect.read_file(f, CONFIG, None)["records"]]
    assert flags == [False, True, False]


def test_a_compact_summary_at_the_tail_flags_the_next_pass(tmp_path):
    f = tmp_path / "a.jsonl"
    compact = json.dumps({"type": "user", "isCompactSummary": True, "message": {"content": "summary"}})
    write(f, [assistant_line("u1"), compact])
    first = collect.read_file(f, CONFIG, None)
    write(f, [assistant_line("u1"), compact, assistant_line("u2")])
    assert collect.read_file(f, CONFIG, first["state"])["records"][0]["after_compaction"] is True


def test_every_turn_of_a_subagent_file_carries_the_call_that_spawned_it(tmp_path):
    f = tmp_path / "agent-1.jsonl"
    source = json.dumps({"type": "user", "sourceToolUseID": "toolu_9", "message": {"content": "go"}})
    write(f, [assistant_line("u1"), source, assistant_line("u2")])
    assert [r["source_tool_use_id"] for r in collect.read_file(f, CONFIG, None)["records"]] == ["toolu_9", "toolu_9"]


def test_the_spawning_call_survives_an_incremental_resume(tmp_path):
    f = tmp_path / "agent-1.jsonl"
    source = json.dumps({"type": "user", "sourceToolUseID": "toolu_9", "message": {"content": "go"}})
    write(f, [source, assistant_line("u1")])
    first = collect.read_file(f, CONFIG, None)
    write(f, [source, assistant_line("u1"), assistant_line("u2")])
    second = collect.read_file(f, CONFIG, first["state"])
    assert second["records"][0]["source_tool_use_id"] == "toolu_9"


def test_a_main_session_turn_has_no_spawning_call(tmp_path):
    f = tmp_path / "a.jsonl"
    write(f, [assistant_line("u1")])
    assert collect.read_file(f, CONFIG, None)["records"][0]["source_tool_use_id"] is None
