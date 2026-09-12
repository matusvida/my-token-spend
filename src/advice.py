import paths
import rules

WASTE = "waste"
STRATEGY = "strategy"
HYGIENE = "hygiene"

RULE_CLASS = {
    "model_mismatch": WASTE,
    "redundant_reads": WASTE,
    "loop_retry": WASTE,
    "subagent_storm": STRATEGY,
    "agent_type_skew": STRATEGY,
    "context_bloat": HYGIENE,
    "whale_turns": HYGIENE,
}

CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.6, "low": 0.3}

DEFAULTS = {
    "sonnet_class_agents": [],
    "min_saving": 250000,
    "min_storms_for_median": 4,
    "min_whales_for_median": 4,
}


def _settings(config):
    settings = dict(DEFAULTS)
    settings.update(paths.shipped_config().get("advice") or {})
    settings.update(config.get("advice") or {})
    return settings


model_family = rules.model_family
_model_family = model_family


def _findings_of(findings, rule):
    return [finding for finding in findings if finding["rule"] == rule]


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _downgrade_factor(window_data, config):
    pricing = window_data.get("weights", config)
    downgrade = config["thresholds"]["model_mismatch"]["downgrade_model"]
    cheap = rules.model_weight(downgrade, pricing)[0]
    expensive = max(list(pricing["model_weights"].values()) + [cheap])
    if expensive <= 0:
        return 0.0
    return 1.0 - cheap / expensive


def _expensive_model_share(window_data, config):
    pricing = window_data.get("weights", config)
    downgrade = config["thresholds"]["model_mismatch"]["downgrade_model"]
    cheap = rules.model_weight(downgrade, pricing)[0]
    total = window_data["totals"]["weighted"]
    if not total:
        return 0.0
    expensive = sum(
        bucket["weighted"]
        for bucket in window_data["by_model"]
        if rules.model_weight(bucket["key"], pricing)[0] > cheap
    )
    return expensive / total


def _recommendation(kind, group, subject, title, action, detail, saving, risk, confidence, evidence):
    return {
        "kind": kind,
        "group": group,
        "subject": subject,
        "title": title,
        "action": action,
        "detail": detail,
        "weighted_saving": float(saving),
        "performance_risk": risk,
        "confidence": confidence,
        "evidence": evidence,
    }


def _model_downgrades(findings, config):
    downgrade = config["thresholds"]["model_mismatch"]["downgrade_model"]
    recommendations = []
    for finding in sorted(_findings_of(findings, "model_mismatch"), key=lambda f: -f["weighted_cost"]):
        turns = finding["evidence"]["turns"]
        target = finding["evidence"].get("downgrade_model", downgrade)
        recommendations.append(
            _recommendation(
                "model_downgrade",
                WASTE,
                finding["subject"],
                "Run trivial %s turns on %s" % (finding["subject"], target),
                "Add `\"model\": \"%s\"` to the agent definitions that only fetch, grep or confirm, "
                "and open one-tool sessions with `/model %s`." % (target, _model_family(target)),
                "%d turns produced at most %d output tokens with between 1 and %d tool call(s), at most "
                "%d thinking tokens and no Agent dispatch among them. Priced at %s they cost this much "
                "less. Only the token counts are identical: a tier change is a quality tradeoff, and "
                "whether the cheaper tier reaches the same answers is recorded nowhere in this data. The "
                "rule still cannot see what a turn decided, so a short answer reached after real "
                "judgement is counted here too."
                % (
                    turns,
                    config["thresholds"]["model_mismatch"]["max_output_tokens"],
                    config["thresholds"]["model_mismatch"]["max_tool_calls"],
                    config["thresholds"]["model_mismatch"].get("max_thinking", rules.MAX_THINKING_DEFAULT),
                    target,
                ),
                finding["weighted_cost"],
                "low",
                "medium",
                {"turns": turns, "downgrade_model": target},
            )
        )
    return recommendations


def _deduplicate_reads(findings):
    per_tool = {}
    for finding in _findings_of(findings, "redundant_reads"):
        tool = finding["evidence"]["tool"]
        bucket = per_tool.setdefault(tool, {"cost": 0.0, "findings": 0, "occurrences": 0})
        bucket["cost"] += finding["weighted_cost"]
        bucket["findings"] += 1
        bucket["occurrences"] += finding["evidence"]["occurrences"]
    recommendations = []
    for tool, bucket in sorted(per_tool.items(), key=lambda item: -item[1]["cost"]):
        recommendations.append(
            _recommendation(
                "deduplicate_reads",
                WASTE,
                tool,
                "Stop re-reading the same %s input" % tool,
                "Ask for the file or command output once per session and refer back to it; where a subagent "
                "needs it too, pass the content in its prompt instead of letting it re-run %s." % tool,
                "%d repeat groups, %d calls with a byte-identical input beyond the first."
                % (bucket["findings"], bucket["occurrences"] - bucket["findings"]),
                bucket["cost"],
                "none",
                "medium",
                {"findings": bucket["findings"], "occurrences": bucket["occurrences"]},
            )
        )
    return recommendations


def _break_retry_loops(findings):
    loops = _findings_of(findings, "loop_retry")
    if not loops:
        return []
    cost = sum(finding["weighted_cost"] for finding in loops)
    worst = max(loops, key=lambda finding: finding["weighted_cost"])
    return [
        _recommendation(
            "break_retry_loops",
            WASTE,
            worst["evidence"]["tool"],
            "Break the back-to-back retry loops",
            "When a call fails twice with the same input, change the input or the tool rather than repeating "
            "it; for flaky MCP calls add an explicit fallback in the prompt.",
            "%d run%s of at least %d identical consecutive calls; the longest was %d calls of %s."
            % (
                len(loops),
                "" if len(loops) == 1 else "s",
                worst["evidence"]["run_length"],
                worst["evidence"]["run_length"],
                worst["evidence"]["tool"],
            ),
            cost,
            "none",
            "medium",
            {"runs": len(loops), "longest_run": worst["evidence"]["run_length"]},
        )
    ]


def _right_size_agent_tier(window_data, findings, config, settings):
    sonnet_class = set(settings["sonnet_class_agents"])
    downgrade = config["thresholds"]["model_mismatch"]["downgrade_model"]
    factor = _downgrade_factor(window_data, config)
    share = _expensive_model_share(window_data, config)
    recommendations = []
    for finding in sorted(_findings_of(findings, "agent_type_skew"), key=lambda f: -f["weighted_cost"]):
        agent = finding["subject"]
        if agent not in sonnet_class:
            continue
        saving = finding["weighted_cost"] * share * factor
        recommendations.append(
            _recommendation(
                "right_size_agent_tier",
                STRATEGY,
                agent,
                "Right-size %s to %s" % (agent, downgrade),
                "Set `model: %s` in the %s agent definition. Keep the same fan-out - this changes the tier "
                "of the workers, not how many you run." % (downgrade, agent),
                "%s spent %s weighted tokens over %d turns on mechanical, well-specified work. %.0f%% of the "
                "window ran on a %.0fx model, so moving this agent down saves that share of its cost."
                % (
                    agent,
                    "{:,.0f}".format(finding["weighted_cost"]),
                    finding["evidence"]["turns"],
                    100 * share,
                    1.0 / (1.0 - factor) if factor < 1 else 0,
                ),
                saving,
                "low",
                "medium",
                {
                    "turns": finding["evidence"]["turns"],
                    "agent_weighted": finding["weighted_cost"],
                    "expensive_model_share": share,
                    "downgrade_factor": factor,
                },
            )
        )
    return recommendations


def _right_size_fan_out(findings, settings):
    storms = _findings_of(findings, "subagent_storm")
    if len(storms) < settings["min_storms_for_median"]:
        return []
    median_turns = _median([storm["evidence"]["sidechain_turns"] for storm in storms])
    if median_turns <= 0:
        return []
    oversized = [storm for storm in storms if storm["evidence"]["sidechain_turns"] > median_turns]
    if not oversized:
        return []
    saving = sum(
        storm["weighted_cost"] * (1.0 - median_turns / storm["evidence"]["sidechain_turns"])
        for storm in oversized
    )
    worst = max(oversized, key=lambda storm: storm["evidence"]["sidechain_turns"])
    return [
        _recommendation(
            "right_size_fan_out",
            STRATEGY,
            worst["subject"],
            "Right-size the biggest fan-outs to your own median",
            "Keep the orchestration; batch the unit of work. One agent per merge request rather than one per "
            "file or per check, and hand each agent the slice it needs instead of letting it rediscover it.",
            "%d sessions ran a subagent storm this window; the median was %.0f subagent turns and %d sessions "
            "ran above it, the largest at %d turns. This is the cost of bringing only the oversized ones back "
            "to your own median - the median-sized storms are left untouched."
            % (len(storms), median_turns, len(oversized), worst["evidence"]["sidechain_turns"]),
            saving,
            "medium",
            "low",
            {
                "storms": len(storms),
                "median_sidechain_turns": median_turns,
                "oversized": len(oversized),
                "largest_sidechain_turns": worst["evidence"]["sidechain_turns"],
            },
        )
    ]


def _reset_context(findings, config):
    bloat = _findings_of(findings, "context_bloat")
    if not bloat:
        return []
    threshold = config["thresholds"]["context_bloat"]["cache_read_per_turn"]
    cost = sum(finding["weighted_cost"] for finding in bloat)
    worst = max(bloat, key=lambda finding: finding["weighted_cost"])
    return [
        _recommendation(
            "reset_context",
            HYGIENE,
            worst["subject"],
            "Clear context before it turns into a per-turn tax",
            "Finish a task, then `/clear` and restate the next one in three lines. When a session passes a few "
            "hundred turns, hand the remaining work to a fresh session with a short brief.",
            "%d sessions paid for re-reading context above %s tokens per turn. The worst ran %d turns and "
            "re-read %s tokens above the threshold. This figure is the whole tax above it - an upper bound, "
            "since some of that context was genuinely needed."
            % (
                len(bloat),
                "{:,}".format(threshold),
                worst["evidence"]["turns"],
                "{:,}".format(worst["evidence"]["excess_cache_read_tokens"]),
            ),
            cost,
            "low",
            "medium",
            {
                "sessions": len(bloat),
                "worst_turns": worst["evidence"]["turns"],
                "worst_excess_tokens": worst["evidence"]["excess_cache_read_tokens"],
            },
        )
    ]


def _split_whale_turns(findings, settings):
    whales = _findings_of(findings, "whale_turns")
    if len(whales) < settings["min_whales_for_median"]:
        return []
    median_cost = _median([whale["weighted_cost"] for whale in whales])
    oversized = [whale for whale in whales if whale["weighted_cost"] > median_cost]
    if not oversized:
        return []
    saving = sum(whale["weighted_cost"] - median_cost for whale in oversized)
    worst = max(oversized, key=lambda whale: whale["weighted_cost"])
    return [
        _recommendation(
            "split_whale_turns",
            HYGIENE,
            worst["subject"],
            "Split the handful of turns that carry a whole session",
            "When a prompt asks for several things at once, split it: one ask per turn, and point at the files "
            "instead of asking the model to find them again.",
            "The %d largest turns of the window ranged up to %s weighted tokens against a median of %s among "
            "them. Bringing the %d biggest back to that median is worth this much; the triggering prompt of "
            "the largest was: %s"
            % (
                len(whales),
                "{:,.0f}".format(worst["weighted_cost"]),
                "{:,.0f}".format(median_cost),
                len(oversized),
                (worst["evidence"].get("prompt") or "not captured").strip()[:120],
            ),
            saving,
            "low",
            "low",
            {
                "whales": len(whales),
                "median_weighted": median_cost,
                "oversized": len(oversized),
                "largest_weighted": worst["weighted_cost"],
            },
        )
    ]


def score(recommendation):
    return recommendation["weighted_saving"] * CONFIDENCE_WEIGHTS[recommendation["confidence"]]


def recommend(window_data, findings, config):
    settings = _settings(config)
    total = window_data["totals"]["weighted"]
    recommendations = []
    recommendations.extend(_model_downgrades(findings, config))
    recommendations.extend(_deduplicate_reads(findings))
    recommendations.extend(_break_retry_loops(findings))
    recommendations.extend(_right_size_agent_tier(window_data, findings, config, settings))
    recommendations.extend(_right_size_fan_out(findings, settings))
    recommendations.extend(_reset_context(findings, config))
    recommendations.extend(_split_whale_turns(findings, settings))
    kept = [item for item in recommendations if item["weighted_saving"] >= settings["min_saving"]]
    for item in kept:
        item["percent_of_window"] = 100.0 * item["weighted_saving"] / total if total else 0.0
        item["score"] = score(item)
    kept.sort(key=lambda item: (-item["score"], item["kind"], item["subject"] or ""))
    return kept
