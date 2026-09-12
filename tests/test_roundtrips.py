import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import roundtrips
import rules


def record(uuid, session="s1", tools=(), weighted=100.0, ts="2026-08-22T10:00:00+00:00", api_error=False):
    return {
        "uuid": uuid,
        "sessionId": session,
        "ts": ts,
        "weighted": weighted,
        "tools": [{"name": name, "hash": digest} for name, digest in tools],
        "is_api_error": api_error,
    }


def transcript(path, entries):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def assistant(uuid, calls):
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"content": [{"type": "tool_use", "id": call_id, "name": name} for call_id, name in calls]},
    }


def result(call_id, text, is_error=False):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": is_error}]},
    }


def detector(analysis, key):
    for item in analysis["detectors"]:
        if item["key"] == key:
            return item
    raise AssertionError("no detector %s" % key)


def test_a_failed_call_costs_the_share_of_the_turn_that_issued_it(tmp_path):
    transcript(
        tmp_path / "a.jsonl",
        [assistant("u1", [("c1", "Bash"), ("c2", "Read")]), result("c1", "Exit code 1", True), result("c2", "ok")],
    )
    index = roundtrips.scan(tmp_path)
    analysis = rules.round_trips([record("u1", tools=[("Bash", "h1"), ("Read", "h2")], weighted=100.0)], index)
    failed = detector(analysis, rules.FAILED_CALLS)
    assert failed["count"] == 1
    assert failed["weighted_cost"] == 50.0
    assert failed["evidence"]["by_tool"] == [("Bash", 1)]


def test_a_successful_call_costs_nothing(tmp_path):
    transcript(tmp_path / "a.jsonl", [assistant("u1", [("c1", "Bash")]), result("c1", "fine")])
    index = roundtrips.scan(tmp_path)
    analysis = rules.round_trips([record("u1", tools=[("Bash", "h1")])], index)
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 0


def test_repeating_an_input_that_already_failed_is_counted_separately(tmp_path):
    transcript(
        tmp_path / "a.jsonl",
        [
            assistant("u1", [("c1", "Bash")]),
            result("c1", "Exit code 1", True),
            assistant("u2", [("c2", "Bash")]),
            result("c2", "Exit code 1", True),
        ],
    )
    index = roundtrips.scan(tmp_path)
    records = [
        record("u1", tools=[("Bash", "same")], weighted=100.0),
        record("u2", tools=[("Bash", "same")], weighted=40.0, ts="2026-08-22T10:01:00+00:00"),
    ]
    analysis = rules.round_trips(records, index)
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 2
    retried = detector(analysis, rules.RETRIED_AFTER_FAILURE)
    assert retried["count"] == 1
    assert retried["weighted_cost"] == 40.0


def test_a_different_input_after_a_failure_is_not_a_retry(tmp_path):
    transcript(
        tmp_path / "a.jsonl",
        [
            assistant("u1", [("c1", "Bash")]),
            result("c1", "Exit code 1", True),
            assistant("u2", [("c2", "Bash")]),
            result("c2", "ok"),
        ],
    )
    index = roundtrips.scan(tmp_path)
    records = [
        record("u1", tools=[("Bash", "first")]),
        record("u2", tools=[("Bash", "second")], ts="2026-08-22T10:01:00+00:00"),
    ]
    assert detector(rules.round_trips(records, index), rules.RETRIED_AFTER_FAILURE)["count"] == 0


def test_a_permission_rejection_is_reported_as_its_own_subset(tmp_path):
    transcript(
        tmp_path / "a.jsonl",
        [
            assistant("u1", [("c1", "Bash")]),
            result("c1", "The user doesn't want to proceed with this tool use. The tool use was rejected", True),
        ],
    )
    index = roundtrips.scan(tmp_path)
    analysis = rules.round_trips([record("u1", tools=[("Bash", "h")])], index)
    assert detector(analysis, rules.PERMISSION_DENIED)["count"] == 1
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 1


def test_api_error_turns_come_from_the_record_and_need_no_transcript(tmp_path):
    analysis = rules.round_trips(
        [record("u1", weighted=500.0, api_error=True), record("u2", weighted=1.0)], roundtrips.empty_index()
    )
    api = detector(analysis, rules.API_ERROR_TURNS)
    assert api["count"] == 1
    assert api["weighted_cost"] == 500.0


def test_coverage_reports_how_much_of_the_window_the_transcripts_still_hold(tmp_path):
    transcript(tmp_path / "a.jsonl", [assistant("u1", [("c1", "Bash")]), result("c1", "ok")])
    index = roundtrips.scan(tmp_path)
    analysis = rules.round_trips([record("u1", tools=[("Bash", "h")]), record("pruned")], index)
    assert analysis["records"] == 2
    assert analysis["covered_records"] == 1
    assert analysis["coverage"] == 0.5


def test_a_pruned_transcript_cannot_invent_failures(tmp_path):
    analysis = rules.round_trips([record("gone", tools=[("Bash", "h")])], roundtrips.empty_index())
    assert analysis["coverage"] == 0.0
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 0
    assert analysis["resolved_calls"] == 0
    assert analysis["total_calls"] == 1


def test_the_scan_survives_a_malformed_line(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text('{"type": "assistant"\n' + json.dumps(assistant("u1", [("c1", "Bash")])) + "\n", encoding="utf-8")
    index = roundtrips.scan(path.parent)
    assert index["calls"]["u1"] == [("c1", "Bash")]


def test_a_missing_transcript_root_yields_an_empty_index(tmp_path):
    index = roundtrips.scan(tmp_path / "nope")
    assert index == roundtrips.empty_index()


def test_records_load_from_the_durable_store(tmp_path):
    (tmp_path / "week_2026_08_22.jsonl").write_text(
        json.dumps(record("u1")) + "\n\n" + json.dumps(record("u2")) + "\n", encoding="utf-8"
    )
    loaded = roundtrips.load_records(tmp_path, "week_2026_08_22")
    assert [r["uuid"] for r in loaded] == ["u1", "u2"]
    assert roundtrips.load_records(tmp_path, "week_1999_01_01") == []


def test_a_repeat_that_succeeded_is_not_counted_as_retry_waste(tmp_path):
    transcript(
        tmp_path / "a.jsonl",
        [
            assistant("u1", [("c1", "Bash")]),
            result("c1", "Exit code 1", True),
            assistant("u2", [("c2", "Bash")]),
            result("c2", "ok"),
        ],
    )
    index = roundtrips.scan(tmp_path)
    records = [
        record("u1", tools=[("Bash", "same")], weighted=100.0),
        record("u2", tools=[("Bash", "same")], weighted=40.0, ts="2026-08-22T10:01:00+00:00"),
    ]
    analysis = rules.round_trips(records, index)
    retried = detector(analysis, rules.RETRIED_AFTER_FAILURE)
    assert retried["count"] == 0
    assert retried["weighted_cost"] == 0.0
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 1


def test_retries_can_never_outnumber_the_failed_calls_they_subset(tmp_path):
    transcript(
        tmp_path / "a.jsonl",
        [
            assistant("u1", [("c1", "Bash")]),
            result("c1", "Exit code 1", True),
            assistant("u2", [("c2", "Bash")]),
            result("c2", "ok"),
            assistant("u3", [("c3", "Bash")]),
            result("c3", "ok"),
        ],
    )
    index = roundtrips.scan(tmp_path)
    records = [
        record("u1", tools=[("Bash", "same")]),
        record("u2", tools=[("Bash", "same")], ts="2026-08-22T10:01:00+00:00"),
        record("u3", tools=[("Bash", "same")], ts="2026-08-22T10:02:00+00:00"),
    ]
    analysis = rules.round_trips(records, index)
    assert detector(analysis, rules.RETRIED_AFTER_FAILURE)["count"] == 0
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 1


def test_a_second_failure_of_the_same_input_is_still_counted(tmp_path):
    transcript(
        tmp_path / "a.jsonl",
        [
            assistant("u1", [("c1", "Bash")]),
            result("c1", "Exit code 1", True),
            assistant("u2", [("c2", "Bash")]),
            result("c2", "ok"),
            assistant("u3", [("c3", "Bash")]),
            result("c3", "Exit code 1", True),
        ],
    )
    index = roundtrips.scan(tmp_path)
    records = [
        record("u1", tools=[("Bash", "same")]),
        record("u2", tools=[("Bash", "same")], ts="2026-08-22T10:01:00+00:00"),
        record("u3", tools=[("Bash", "same")], weighted=70.0, ts="2026-08-22T10:02:00+00:00"),
    ]
    analysis = rules.round_trips(records, index)
    retried = detector(analysis, rules.RETRIED_AFTER_FAILURE)
    assert retried["count"] == 1
    assert retried["weighted_cost"] == 70.0
    assert retried["count"] <= detector(analysis, rules.FAILED_CALLS)["count"]
