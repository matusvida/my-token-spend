import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rules

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
START = datetime(2026, 8, 22, tzinfo=timezone.utc)


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat()


def rec(
    ts,
    session="s1",
    model="claude-sonnet-5",
    output=0,
    thinking=0,
    cache_read=0,
    agent=None,
    skill=None,
    agent_id=None,
    uuid="u",
):
    weighted = rules.model_weight(model, CONFIG)[0] * (
        CONFIG["token_class_weights"]["output"] * output
        + CONFIG["token_class_weights"]["cache_read"] * cache_read
    )
    return {
        "ts": ts,
        "uuid": uuid,
        "sessionId": session,
        "model": model,
        "model_known": True,
        "effort": "high",
        "isSidechain": agent_id is not None,
        "agentId": agent_id,
        "attributionAgent": agent,
        "attributionSkill": skill,
        "cwd": "C:\workspace\srst",
        "gitBranch": "master",
        "version": "2.1.227",
        "input": 0,
        "output": output,
        "cache_create": 0,
        "cache_read": cache_read,
        "thinking": thinking,
        "weighted": weighted,
        "tools": [],
        "text_chars": 0,
        "is_api_error": False,
        "prompt": None,
    }


def quota(method="quota-fit", percent=30.0, previous=25.0, closed=True, ceiling=1000000.0, spent=300000.0, extra=None):
    return {
        "window_key": "week_2026_08_22",
        "method": method,
        "closed": closed,
        "ceiling": ceiling,
        "spent": spent,
        "percent_used": percent,
        "previous_percent_used": previous,
        "extra_usage": extra,
    }


def fire(records=(), **kwargs):
    return rules.headroom(list(records), CONFIG, quota(**kwargs))


def test_headroom_fires_when_two_closed_windows_stayed_under_the_cap():
    findings = fire()
    assert len(findings) == 1
    assert findings[0]["rule"] == "headroom"
    assert findings[0]["subject"] == "week_2026_08_22"
    assert findings[0]["weighted_cost"] == 700000.0


def test_headroom_stays_silent_when_the_ceiling_came_from_the_top_cluster():
    assert fire(method="top-cluster") == []


def test_headroom_stays_silent_when_there_is_no_ceiling_yet():
    assert fire(method="insufficient-data") == []


def test_headroom_fires_on_a_configured_override_ceiling():
    assert len(fire(method="override")) == 1


def test_headroom_stays_silent_while_the_window_is_still_open():
    assert fire(closed=False) == []


def test_headroom_stays_silent_when_this_window_reached_the_cap():
    assert fire(percent=72.0) == []


def test_headroom_stays_silent_when_only_the_previous_window_was_quiet():
    assert fire(percent=30.0, previous=88.0) == []


def test_headroom_stays_silent_without_a_previous_window_to_compare():
    assert rules.headroom([], CONFIG, dict(quota(), previous_percent_used=None)) == []


def test_headroom_detail_states_both_percentages_and_the_unused_quota():
    detail = fire()[0]["detail"]
    assert "30%" in detail and "25%" in detail and "700,000" in detail


def test_headroom_never_tells_the_reader_to_spend_the_remainder():
    detail = fire()[0]["detail"].lower()
    assert "use more tokens" not in detail
    assert "spend the remaining quota" not in detail


def test_headroom_names_the_cheap_components_whose_runs_show_judgement():
    records = [
        rec(at(i), agent="deep-reviewer", output=2000, thinking=4000, agent_id="r%d" % i) for i in range(6)
    ] + [rec(at(i), agent="lister", output=20, thinking=0, agent_id="q%d" % i) for i in range(6)]
    components = fire(records)[0]["evidence"]["components"]
    assert [item["name"] for item in components] == ["deep-reviewer"]
    assert components[0]["median_thinking"] == 4000
    assert components[0]["median_output"] == 2000
    assert components[0]["component"] == "agent"


def test_headroom_counts_a_skill_as_a_candidate_too():
    records = [rec(at(i), skill="architect", output=3000, thinking=900) for i in range(4)]
    assert [item["component"] for item in fire(records)[0]["evidence"]["components"]] == ["skill"]


def test_headroom_names_sessions_whose_runs_never_overlapped():
    records = []
    for index in range(3):
        for turn in range(2):
            records.append(
                rec(
                    at(index * 20 + turn * 15),
                    session="serial",
                    agent="worker",
                    agent_id="run%d" % index,
                    output=100,
                    uuid="u%d%d" % (index, turn),
                )
            )
    serial = fire(records)[0]["evidence"]["serial_sessions"]
    assert len(serial) == 1
    assert serial[0]["session"] == "serial"
    assert serial[0]["runs"] == 3
    assert serial[0]["peak_live_runs"] == 1
    assert serial[0]["minutes"] == 45.0


def test_headroom_ignores_a_session_whose_runs_already_overlapped():
    records = []
    for index in range(2):
        for turn in range(2):
            records.append(
                rec(
                    at(turn * 30),
                    session="parallel",
                    agent="worker",
                    agent_id="run%d" % index,
                    output=100,
                    uuid="u%d%d" % (index, turn),
                )
            )
    assert fire(records)[0]["evidence"]["serial_sessions"] == []


def test_headroom_ignores_serial_runs_that_finished_quickly():
    records = [
        rec(at(index * 2 + turn), session="brief", agent="worker", agent_id="run%d" % index, uuid="u%d%d" % (index, turn))
        for index in range(2)
        for turn in range(2)
    ]
    assert fire(records)[0]["evidence"]["serial_sessions"] == []


def test_headroom_carries_the_unused_extra_usage_budget_it_was_given():
    budget = {"is_enabled": True, "monthly_limit": 5000, "used_credits": 0, "currency": "USD", "decimal_places": 2}
    assert fire(extra=budget)[0]["evidence"]["extra_usage"] == budget


import collect
import quota as quota_module

STATS = {"files_scanned": 1, "malformed_lines": 0}
CEILING = {"estimate": 1000000.0, "method": "quota-fit", "approximate": False}


def aggregate(ceiling=CEILING, previous=200000.0, samples=None):
    from datetime import date

    records = [rec(at(i), output=30000, uuid="u%d" % i) for i in range(2)]
    return collect.aggregate_window(
        date(2026, 8, 22),
        records,
        CONFIG,
        STATS,
        ceiling,
        previous_weighted=previous,
        samples=samples,
    )


def test_the_aggregate_of_a_quiet_closed_window_carries_a_headroom_finding():
    window = aggregate()
    finding = [item for item in window["findings"] if item["rule"] == "headroom"]
    assert len(finding) == 1
    assert finding[0]["evidence"]["percent_used"] == 30.0
    assert finding[0]["evidence"]["previous_percent_used"] == 20.0
    assert window["findings_by_rule"]["headroom"]["count"] == 1


def test_the_aggregate_carries_no_headroom_finding_without_a_real_quota():
    window = aggregate(ceiling=dict(CEILING, method="top-cluster"))
    assert [item for item in window["findings"] if item["rule"] == "headroom"] == []


def test_the_aggregate_carries_no_headroom_finding_for_the_first_window_collected():
    window = aggregate(previous=None)
    assert [item for item in window["findings"] if item["rule"] == "headroom"] == []


def test_an_untouched_extra_usage_budget_is_read_off_the_window_samples():
    samples = [
        {
            "ts": "2026-08-24T10:00:00+00:00",
            "extra_usage": {"is_enabled": True, "used_credits": 0, "monthly_limit": 5000, "currency": "USD"},
        }
    ]
    evidence = [item for item in aggregate(samples=samples)["findings"] if item["rule"] == "headroom"][0]
    assert evidence["evidence"]["extra_usage"]["monthly_limit"] == 5000


def test_a_budget_that_was_drawn_on_is_not_reported_as_unused():
    start = datetime(2026, 8, 22, tzinfo=timezone.utc)
    samples = [
        {"ts": "2026-08-24T10:00:00+00:00", "extra_usage": {"is_enabled": True, "used_credits": 0}},
        {"ts": "2026-08-25T10:00:00+00:00", "extra_usage": {"is_enabled": True, "used_credits": 12}},
    ]
    assert quota_module.extra_usage_unused(samples, start, start + timedelta(days=7)) is None


def test_a_disabled_budget_is_not_reported_as_unused():
    start = datetime(2026, 8, 22, tzinfo=timezone.utc)
    samples = [{"ts": "2026-08-24T10:00:00+00:00", "extra_usage": {"is_enabled": False, "used_credits": 0}}]
    assert quota_module.extra_usage_unused(samples, start, start + timedelta(days=7)) is None
