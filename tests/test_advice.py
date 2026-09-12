import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import advice


CONFIG = {
    "model_weights": {"claude-opus-5": 5.0, "claude-sonnet-5": 1.0, "claude-haiku-4-5-20251001": 0.33},
    "default_model_weight": 1.0,
    "thresholds": {
        "context_bloat": {"cache_read_per_turn": 150000, "min_turns": 12},
        "model_mismatch": {
            "downgrade_model": "claude-sonnet-5",
            "max_output_tokens": 250,
            "max_tool_calls": 1,
            "min_total_weighted": 100000,
        },
    },
}


def window(total=1000000.0, by_model=None):
    return {
        "totals": {"weighted": total},
        "by_model": by_model
        if by_model is not None
        else [
            {"key": "claude-opus-5", "weighted": total * 0.8},
            {"key": "claude-sonnet-5", "weighted": total * 0.2},
        ],
        "weights": {
            "model_weights": CONFIG["model_weights"],
            "default_model_weight": 1.0,
            "token_class_weights": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.1, "output": 5.0},
        },
    }


def finding(rule, subject, cost, **evidence):
    return {
        "rule": rule,
        "subject": subject,
        "detail": "%s on %s" % (rule, subject),
        "weighted_cost": float(cost),
        "evidence": evidence,
    }


def storm(subject, turns, cost):
    return finding(
        "subagent_storm",
        subject,
        cost,
        sidechain_turns=turns,
        session_turns=turns + 10,
        cost_share=0.9,
        agents={"general-purpose": cost},
        cwd="C:\\workspace\\alpha",
    )


def whale(subject, cost, prompt="do everything at once"):
    return finding(
        "whale_turns",
        subject,
        cost,
        uuid="u-%s" % subject,
        ts="2026-08-24T10:00:00+00:00",
        model="claude-opus-5",
        effort="high",
        isSidechain=False,
        attributionAgent=None,
        cwd="C:\\workspace\\alpha",
        gitBranch="main",
        prompt=prompt,
        tools=[],
        rank=1,
    )


def only(recommendations, kind):
    return [item for item in recommendations if item["kind"] == kind]


def test_every_rule_maps_to_exactly_one_class():
    assert set(advice.RULE_CLASS) == {
        "model_mismatch",
        "redundant_reads",
        "loop_retry",
        "subagent_storm",
        "agent_type_skew",
        "context_bloat",
        "whale_turns",
    }
    assert advice.RULE_CLASS["model_mismatch"] == advice.WASTE
    assert advice.RULE_CLASS["subagent_storm"] == advice.STRATEGY
    assert advice.RULE_CLASS["agent_type_skew"] == advice.STRATEGY
    assert advice.RULE_CLASS["context_bloat"] == advice.HYGIENE
    assert advice.RULE_CLASS["whale_turns"] == advice.HYGIENE


def test_model_downgrade_saving_is_the_finding_cost_verbatim():
    findings = [finding("model_mismatch", "claude-opus-5", 96200374.0, turns=1628, downgrade_model="claude-sonnet-5")]
    result = only(advice.recommend(window(828033502.8), findings, CONFIG), "model_downgrade")
    assert len(result) == 1
    assert result[0]["weighted_saving"] == 96200374.0
    assert result[0]["percent_of_window"] == pytest.approx(11.618, abs=0.01)
    assert result[0]["performance_risk"] == "low"
    assert "the work is identical" not in result[0]["detail"]
    assert "quality tradeoff" in result[0]["detail"]
    assert "recorded nowhere in this data" in result[0]["detail"]
    assert result[0]["confidence"] == "medium"
    assert result[0]["group"] == advice.WASTE
    assert "claude-sonnet-5" in result[0]["action"]
    assert "1628" in result[0]["detail"]


def test_model_downgrade_fires_once_per_model():
    findings = [
        finding("model_mismatch", "claude-opus-5", 96200374.0, turns=1628, downgrade_model="claude-sonnet-5"),
        finding("model_mismatch", "claude-opus-4-8", 9137234.0, turns=78, downgrade_model="claude-sonnet-5"),
    ]
    result = only(advice.recommend(window(828033502.8), findings, CONFIG), "model_downgrade")
    assert [item["subject"] for item in result] == ["claude-opus-5", "claude-opus-4-8"]
    assert [item["weighted_saving"] for item in result] == [96200374.0, 9137234.0]


def test_model_downgrade_does_not_fire_without_the_rule():
    assert only(advice.recommend(window(), [], CONFIG), "model_downgrade") == []


def test_deduplicate_reads_sums_within_the_tool_only():
    findings = [
        finding("redundant_reads", "s1", 6000000.0, tool="Read", input_hash="a", occurrences=6, cwd="C:\\a"),
        finding("redundant_reads", "s2", 4370201.0, tool="Read", input_hash="b", occurrences=4, cwd="C:\\a"),
        finding("redundant_reads", "s3", 754020.0, tool="Bash", input_hash="c", occurrences=3, cwd="C:\\a"),
    ]
    result = only(advice.recommend(window(828033502.8), findings, CONFIG), "deduplicate_reads")
    assert [item["subject"] for item in result] == ["Read", "Bash"]
    assert result[0]["weighted_saving"] == pytest.approx(10370201.0)
    assert result[1]["weighted_saving"] == pytest.approx(754020.0)
    assert result[0]["performance_risk"] == "none"
    assert "8" in result[0]["detail"]


def test_deduplicate_reads_below_the_reporting_threshold_is_dropped():
    findings = [finding("redundant_reads", "s1", 1000.0, tool="Read", input_hash="a", occurrences=3, cwd="C:\\a")]
    assert only(advice.recommend(window(), findings, CONFIG), "deduplicate_reads") == []


def test_break_retry_loops_sums_the_runs():
    findings = [
        finding("loop_retry", "s1", 400000.0, tool="Bash", input_hash="a", run_length=4, cwd="C:\\a"),
        finding("loop_retry", "s2", 300000.0, tool="Read", input_hash="b", run_length=3, cwd="C:\\a"),
    ]
    result = only(advice.recommend(window(), findings, CONFIG), "break_retry_loops")
    assert len(result) == 1
    assert result[0]["weighted_saving"] == pytest.approx(700000.0)
    assert result[0]["evidence"]["longest_run"] == 4
    assert result[0]["performance_risk"] == "none"


def test_break_retry_loops_does_not_fire_without_the_rule():
    assert only(advice.recommend(window(), [], CONFIG), "break_retry_loops") == []


def test_right_size_agent_tier_uses_the_expensive_model_share_and_the_weight_ratio():
    findings = [finding("agent_type_skew", "review-verifier", 20724466.0, turns=1323, rank=3)]
    result = only(advice.recommend(window(1000000000.0), findings, CONFIG), "right_size_agent_tier")
    assert len(result) == 1
    assert result[0]["weighted_saving"] == pytest.approx(20724466.0 * 0.8 * 0.8)
    assert result[0]["performance_risk"] == "low"
    assert result[0]["confidence"] == "medium"
    assert result[0]["group"] == advice.STRATEGY
    assert "1323" in result[0]["detail"]
    assert "Keep the same fan-out" in result[0]["action"]


def test_right_size_agent_tier_never_touches_judgement_agents():
    findings = [finding("agent_type_skew", "general-purpose", 200031288.0, turns=2441, rank=1)]
    assert only(advice.recommend(window(828033502.8), findings, CONFIG), "right_size_agent_tier") == []


def test_right_size_agent_tier_respects_a_configured_agent_list():
    config = dict(CONFIG, advice={"sonnet_class_agents": ["general-purpose"]})
    findings = [
        finding("agent_type_skew", "general-purpose", 200031288.0, turns=2441, rank=1),
        finding("agent_type_skew", "review-verifier", 20724466.0, turns=1323, rank=3),
    ]
    result = only(advice.recommend(window(828033502.8), findings, config), "right_size_agent_tier")
    assert [item["subject"] for item in result] == ["general-purpose"]


def test_no_recommendation_ever_says_to_use_fewer_subagents():
    findings = [storm("s%d" % index, 100 * (index + 1), 10000000.0 * (index + 1)) for index in range(6)]
    findings.append(finding("agent_type_skew", "review-verifier", 20724466.0, turns=1323, rank=3))
    for item in advice.recommend(window(828033502.8), findings, CONFIG):
        text = (item["title"] + " " + item["action"] + " " + item["detail"]).lower()
        assert "fewer subagent" not in text
        assert "fewer agents" not in text
        assert "stop using subagents" not in text


def test_right_size_fan_out_brings_only_oversized_storms_to_the_median():
    findings = [storm("a", 100, 1000000.0), storm("b", 200, 2000000.0), storm("c", 300, 3000000.0), storm("d", 400, 4000000.0)]
    result = only(advice.recommend(window(100000000.0), findings, CONFIG), "right_size_fan_out")
    assert len(result) == 1
    median = 250.0
    expected = 3000000.0 * (1 - median / 300) + 4000000.0 * (1 - median / 400)
    assert result[0]["weighted_saving"] == pytest.approx(expected)
    assert result[0]["evidence"]["median_sidechain_turns"] == median
    assert result[0]["evidence"]["oversized"] == 2
    assert result[0]["performance_risk"] == "medium"
    assert result[0]["confidence"] == "low"


def test_right_size_fan_out_needs_enough_storms_to_have_a_median():
    findings = [storm("a", 100, 1000000.0), storm("b", 900, 9000000.0)]
    assert only(advice.recommend(window(), findings, CONFIG), "right_size_fan_out") == []


def test_right_size_fan_out_does_not_fire_when_every_storm_is_the_same_size():
    findings = [storm(chr(97 + index), 200, 2000000.0) for index in range(5)]
    assert only(advice.recommend(window(100000000.0), findings, CONFIG), "right_size_fan_out") == []


def test_reset_context_sums_the_sessions_and_names_the_worst():
    findings = [
        finding("context_bloat", "s1", 40827704.0, turns=511, excess_cache_read_tokens=81655408, peak_cache_read=993224, cwd="C:\\a", first_ts="t", last_ts="t"),
        finding("context_bloat", "s2", 29507058.4, turns=120, excess_cache_read_tokens=1000, peak_cache_read=200000, cwd="C:\\b", first_ts="t", last_ts="t"),
    ]
    result = only(advice.recommend(window(828033502.8), findings, CONFIG), "reset_context")
    assert len(result) == 1
    assert result[0]["weighted_saving"] == pytest.approx(70334762.4)
    assert result[0]["group"] == advice.HYGIENE
    assert result[0]["evidence"]["worst_turns"] == 511
    assert "81,655,408" in result[0]["detail"]
    assert "upper bound" in result[0]["detail"]


def test_reset_context_does_not_fire_without_the_rule():
    assert only(advice.recommend(window(), [], CONFIG), "reset_context") == []


def test_split_whale_turns_measures_the_excess_over_the_median_whale():
    findings = [whale("a", 1000000.0), whale("b", 2000000.0), whale("c", 3000000.0), whale("d", 6000000.0)]
    result = only(advice.recommend(window(100000000.0), findings, CONFIG), "split_whale_turns")
    assert len(result) == 1
    median = 2500000.0
    assert result[0]["weighted_saving"] == pytest.approx((3000000.0 - median) + (6000000.0 - median))
    assert result[0]["evidence"]["median_weighted"] == median
    assert result[0]["confidence"] == "low"
    assert result[0]["performance_risk"] == "low"


def test_split_whale_turns_needs_enough_whales_for_a_median():
    assert only(advice.recommend(window(), [whale("a", 9000000.0)], CONFIG), "split_whale_turns") == []


def test_ranking_is_saving_times_confidence():
    findings = [
        finding("model_mismatch", "claude-opus-5", 10000000.0, turns=100, downgrade_model="claude-sonnet-5"),
        finding("context_bloat", "s1", 15000000.0, turns=511, excess_cache_read_tokens=10, peak_cache_read=1, cwd="C:\\a", first_ts="t", last_ts="t"),
    ]
    result = advice.recommend(window(100000000.0), findings, CONFIG)
    assert [item["kind"] for item in result] == ["reset_context", "model_downgrade"]
    assert result[0]["score"] == pytest.approx(9000000.0)
    assert result[1]["score"] == pytest.approx(6000000.0)


def test_every_recommendation_carries_the_full_contract():
    findings = [
        finding("model_mismatch", "claude-opus-5", 96200374.0, turns=1628, downgrade_model="claude-sonnet-5"),
        finding("agent_type_skew", "review-verifier", 20724466.0, turns=1323, rank=3),
        finding("context_bloat", "s1", 40827704.0, turns=511, excess_cache_read_tokens=81655408, peak_cache_read=1, cwd="C:\\a", first_ts="t", last_ts="t"),
    ] + [storm("s%d" % index, 100 * (index + 1), 5000000.0) for index in range(4)] + [
        whale("w%d" % index, 1000000.0 * (index + 1)) for index in range(4)
    ]
    result = advice.recommend(window(828033502.8), findings, CONFIG)
    assert result
    for item in result:
        assert item["group"] in {advice.WASTE, advice.STRATEGY, advice.HYGIENE}
        assert item["performance_risk"] in {"none", "low", "medium"}
        assert item["confidence"] in advice.CONFIDENCE_WEIGHTS
        assert item["weighted_saving"] > 0
        assert 0 < item["percent_of_window"] < 100
        assert item["action"] and item["detail"] and item["title"]
        assert item["score"] == pytest.approx(item["weighted_saving"] * advice.CONFIDENCE_WEIGHTS[item["confidence"]])


def test_recommend_is_pure_and_does_not_mutate_its_inputs():
    findings = [finding("model_mismatch", "claude-opus-5", 96200374.0, turns=1628, downgrade_model="claude-sonnet-5")]
    snapshot = [dict(item) for item in findings]
    data = window(828033502.8)
    before = dict(data["totals"])
    advice.recommend(data, findings, CONFIG)
    assert findings == snapshot
    assert data["totals"] == before


def test_recommend_returns_nothing_when_no_rule_fired():
    assert advice.recommend(window(), [], CONFIG) == []


def test_expensive_model_share_of_a_pure_sonnet_window_is_zero():
    data = window(1000000.0, by_model=[{"key": "claude-sonnet-5", "weighted": 1000000.0}])
    findings = [finding("agent_type_skew", "review-verifier", 20724466.0, turns=1323, rank=3)]
    assert only(advice.recommend(data, findings, CONFIG), "right_size_agent_tier") == []


def test_a_downgrade_model_absent_from_a_historical_windows_weights_does_not_explode():
    config = dict(CONFIG)
    config["thresholds"] = dict(CONFIG["thresholds"])
    config["thresholds"]["model_mismatch"] = dict(
        CONFIG["thresholds"]["model_mismatch"], downgrade_model="claude-sonnet-6"
    )
    data = window(828033502.8)
    findings = [finding("agent_type_skew", "review-verifier", 20724466.0, turns=1323, rank=3)]
    result = only(advice.recommend(data, findings, config), "right_size_agent_tier")

    assert advice._downgrade_factor(data, config) == pytest.approx(1.0 - 1.0 / 5.0)
    assert advice._expensive_model_share(data, config) == pytest.approx(0.8)
    assert [item["subject"] for item in result] == ["review-verifier"]
    assert result[0]["title"].endswith("claude-sonnet-6")


def test_the_fallback_weight_is_the_windows_own_default_not_the_current_config():
    config = dict(CONFIG)
    config["thresholds"] = dict(CONFIG["thresholds"])
    config["thresholds"]["model_mismatch"] = dict(
        CONFIG["thresholds"]["model_mismatch"], downgrade_model="unknown-model"
    )
    data = window(1000000.0)
    data["weights"] = dict(data["weights"], default_model_weight=5.0)

    assert advice._downgrade_factor(data, config) == 0.0
    assert advice._expensive_model_share(data, config) == 0.0


SHIPPED = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text(encoding="utf-8"))


def test_the_sonnet_class_list_lives_in_the_shipped_config_not_in_source():
    assert advice.DEFAULTS["sonnet_class_agents"] == []
    assert SHIPPED["advice"]["sonnet_class_agents"]
    assert advice._settings({})["sonnet_class_agents"] == SHIPPED["advice"]["sonnet_class_agents"]


def test_a_user_override_of_the_sonnet_class_list_still_wins():
    settings = advice._settings({"advice": {"sonnet_class_agents": ["only-this"]}})
    assert settings["sonnet_class_agents"] == ["only-this"]
    assert settings["min_saving"] == SHIPPED["advice"]["min_saving"]
