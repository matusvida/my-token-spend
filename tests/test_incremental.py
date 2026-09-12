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
