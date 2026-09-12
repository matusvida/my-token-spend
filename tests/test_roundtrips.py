import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rules


def tool(name, digest, result_chars=0, is_error=False, denied=False, unresolved=False):
    entry = {"name": name, "hash": digest, "tool_use_id": "c_" + digest}
    if unresolved:
        entry.update({"result_chars": None, "is_error": None, "denied": None})
    else:
        entry.update({"result_chars": result_chars, "is_error": is_error, "denied": denied})
    return entry


def record(uuid, session="s1", tools=(), weighted=100.0, ts="2026-08-22T10:00:00+00:00", api_error=False):
    return {
        "uuid": uuid,
        "sessionId": session,
        "ts": ts,
        "weighted": weighted,
        "tools": list(tools),
        "is_api_error": api_error,
    }


def detector(analysis, key):
    for item in analysis["detectors"]:
        if item["key"] == key:
            return item
    raise AssertionError("no detector %s" % key)


def test_a_failed_call_costs_the_share_of_the_turn_that_issued_it():
    analysis = rules.round_trips(
        [record("u1", tools=[tool("Bash", "h1", is_error=True), tool("Read", "h2")], weighted=100.0)]
    )
    failed = detector(analysis, rules.FAILED_CALLS)
    assert failed["count"] == 1
    assert failed["weighted_cost"] == 50.0
    assert failed["evidence"]["by_tool"] == [("Bash", 1)]


def test_a_successful_call_costs_nothing():
    analysis = rules.round_trips([record("u1", tools=[tool("Bash", "h1")])])
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 0
    assert detector(analysis, rules.FAILED_CALLS)["weighted_cost"] == 0.0


def test_the_same_input_failing_twice_is_a_retry():
    records = [
        record("u1", tools=[tool("Bash", "h1", is_error=True)], ts="2026-08-22T10:00:00+00:00"),
        record("u2", tools=[tool("Bash", "h1", is_error=True)], ts="2026-08-22T10:01:00+00:00"),
    ]
    analysis = rules.round_trips(records)
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 2
    retried = detector(analysis, rules.RETRIED_AFTER_FAILURE)
    assert retried["count"] == 1
    assert retried["weighted_cost"] == 100.0


def test_a_repeat_that_succeeded_is_a_recovery_not_a_retry():
    records = [
        record("u1", tools=[tool("Bash", "h1", is_error=True)], ts="2026-08-22T10:00:00+00:00"),
        record("u2", tools=[tool("Bash", "h1")], ts="2026-08-22T10:01:00+00:00"),
    ]
    assert detector(rules.round_trips(records), rules.RETRIED_AFTER_FAILURE)["count"] == 0


def test_a_retry_in_another_session_is_not_a_retry():
    records = [
        record("u1", tools=[tool("Bash", "h1", is_error=True)]),
        record("u2", session="s2", tools=[tool("Bash", "h1", is_error=True)]),
    ]
    assert detector(rules.round_trips(records), rules.RETRIED_AFTER_FAILURE)["count"] == 0


def test_a_permission_denial_is_a_subset_of_the_failures():
    analysis = rules.round_trips([record("u1", tools=[tool("Edit", "h1", is_error=True, denied=True)])])
    assert detector(analysis, rules.PERMISSION_DENIED)["count"] == 1
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 1


def test_api_error_turns_cost_the_whole_turn():
    analysis = rules.round_trips([record("u1", weighted=500.0, api_error=True), record("u2", weighted=1.0)])
    api = detector(analysis, rules.API_ERROR_TURNS)
    assert api["count"] == 1
    assert api["weighted_cost"] == 500.0


def test_a_call_whose_result_was_never_recorded_counts_as_unresolved():
    analysis = rules.round_trips(
        [record("u1", tools=[tool("Bash", "h1", is_error=True), tool("Read", "h2", unresolved=True)])]
    )
    assert analysis["total_calls"] == 2
    assert analysis["resolved_calls"] == 1
    assert analysis["result_coverage"] == 0.5
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 1


def test_records_written_before_results_were_captured_resolve_nothing():
    analysis = rules.round_trips([record("u1", tools=[{"name": "Bash", "hash": "h1"}])])
    assert analysis["resolved_calls"] == 0
    assert analysis["result_coverage"] == 0.0
    assert detector(analysis, rules.FAILED_CALLS)["count"] == 0


def test_a_window_with_no_calls_at_all_reports_full_coverage_of_nothing():
    analysis = rules.round_trips([record("u1")])
    assert analysis["total_calls"] == 0
    assert analysis["result_coverage"] == 0.0


def test_the_detectors_need_no_transcript():
    source = (Path(__file__).resolve().parents[1] / "src" / "rules.py").read_text(encoding="utf-8")
    for forbidden in ("open(", "rglob", "read_text", "Path("):
        assert forbidden not in source
