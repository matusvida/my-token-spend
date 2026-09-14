import re
from collections import Counter, defaultdict
from datetime import datetime

import context
import text


ANALYSIS_VERSION = 2


FAILED_CALLS = "failed_tool_calls"
RETRIED_AFTER_FAILURE = "retried_after_failure"
PERMISSION_DENIED = "permission_denied"
API_ERROR_TURNS = "api_error_turns"


EXACT = "exact"
FAMILY = "family"
DEFAULT = "default"


def model_family(model):
    parts = [part for part in str(model).split("-") if not part.isdigit()]
    return parts[1] if len(parts) > 1 and parts[0] == "claude" else str(model)


def family_weights(pricing):
    grouped = defaultdict(lambda: defaultdict(int))
    for name, weight in (pricing.get("model_weights") or {}).items():
        grouped[model_family(name)][weight] += 1
    resolved = {}
    for family, counts in grouped.items():
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            resolved[family] = ranked[0][0]
    return resolved


def model_weight(model, pricing):
    weights = pricing.get("model_weights") or {}
    if model in weights:
        return weights[model], EXACT
    family = family_weights(pricing).get(model_family(model))
    if family is not None:
        return family, FAMILY
    return pricing["default_model_weight"], DEFAULT


def _model_weight(record, config):
    return model_weight(record["model"], config)[0]


def _raw_weighted(record, config):
    weights = config["token_class_weights"]
    return (
        weights["input"] * record["input"]
        + weights["output"] * record["output"]
        + weights["cache_create"] * record["cache_create"]
        + weights["cache_read"] * record["cache_read"]
    )


def weighted_cost(record, config):
    return _model_weight(record, config) * _raw_weighted(record, config)


def _tool_shares(record):
    tools = record["tools"]
    if not tools:
        return []
    sizes = [tool.get("result_chars") for tool in tools]
    if any(size is None for size in sizes) or sum(sizes) <= 0:
        share = record["weighted"] / len(tools)
        return [(tool, share) for tool in tools]
    total = float(sum(sizes))
    return [(tool, record["weighted"] * size / total) for tool, size in zip(tools, sizes)]


def _finding(rule, subject, detail, weighted_cost, evidence):
    return {
        "rule": rule,
        "subject": subject,
        "detail": detail,
        "weighted_cost": float(weighted_cost),
        "evidence": evidence,
    }


def _by_session(records):
    sessions = defaultdict(list)
    for record in records:
        sessions[record["sessionId"]].append(record)
    for session in sessions.values():
        session.sort(key=lambda r: r["ts"])
    return sessions


def context_bloat(records, config):
    settings = config["thresholds"]["context_bloat"]
    threshold = settings["cache_read_per_turn"]
    findings = []
    for session_id, session in _by_session(records).items():
        if len(session) < settings["min_turns"]:
            continue
        summary = context.summarize_session(session, config)
        if not summary["excess_tokens"]:
            continue
        findings.append(
            _finding(
                "context_bloat",
                session_id,
                context.detail(summary, threshold, config),
                summary["carry_tax"],
                {
                    "turns": summary["turns"],
                    "excess_cache_read_tokens": summary["excess_tokens"],
                    "peak_cache_read": summary["peak_cache_read"],
                    "cwd": summary["cwd"],
                    "first_ts": summary["first_ts"],
                    "last_ts": summary["last_ts"],
                    "growth_total": round(summary["growth_total"]),
                    "growth_by_tool": [
                        {"tool": entry["tool"], "tokens": round(entry["tokens"]), "results": entry["results"]}
                        for entry in summary["growth_by_tool"][:5]
                    ],
                    "top_results": summary["top_results"],
                    "compactions": summary["compactions"],
                    "attributed_share": summary["attributed_share"],
                    "tool_results_coverage": summary["tool_results_coverage"],
                },
            )
        )
    return findings


def subagent_storm(records, config):
    settings = config["thresholds"]["subagent_storm"]
    findings = []
    for session_id, session in _by_session(records).items():
        sidechain = [r for r in session if r["isSidechain"]]
        if len(sidechain) < settings["min_sidechain_turns"]:
            continue
        total = sum(r["weighted"] for r in session)
        cost = sum(r["weighted"] for r in sidechain)
        if not total or cost / total < settings["min_cost_share"]:
            continue
        agents = defaultdict(float)
        for record in sidechain:
            agents[record["attributionAgent"] or "unknown"] += record["weighted"]
        findings.append(
            _finding(
                "subagent_storm",
                session_id,
                "%d subagent turns account for %.0f%% of the session"
                % (len(sidechain), 100 * cost / total),
                cost,
                {
                    "sidechain_turns": len(sidechain),
                    "session_turns": len(session),
                    "cost_share": cost / total,
                    "agents": dict(sorted(agents.items(), key=lambda kv: -kv[1])),
                    "cwd": session[-1]["cwd"],
                },
            )
        )
    return findings


def agent_type_skew(records, config):
    settings = config["thresholds"]["agent_type_skew"]
    totals = defaultdict(float)
    turns = defaultdict(int)
    for record in records:
        agent = record["attributionAgent"]
        if not agent:
            continue
        totals[agent] += record["weighted"]
        turns[agent] += 1
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[: settings["top_n"]]
    return [
        _finding(
            "agent_type_skew",
            agent,
            "agent type %s cost %s weighted tokens across %d turns" % (agent, f"{cost:,.0f}", turns[agent]),
            cost,
            {"turns": turns[agent], "rank": rank},
        )
        for rank, (agent, cost) in enumerate(ranked, start=1)
        if cost >= settings["min_weighted"]
    ]


MAX_THINKING_DEFAULT = 200
DISPATCH_TOOL = "Agent"


def trivial_turn(record, settings):
    if record["output"] > settings["max_output_tokens"]:
        return False
    if not 1 <= len(record["tools"]) <= settings["max_tool_calls"]:
        return False
    if (record.get("thinking") or 0) > settings.get("max_thinking", MAX_THINKING_DEFAULT):
        return False
    return all(tool["name"] != DISPATCH_TOOL for tool in record["tools"])


def model_mismatch(records, config):
    settings = config["thresholds"]["model_mismatch"]
    downgrade = settings["downgrade_model"]
    downgrade_weight = model_weight(downgrade, config)[0]
    per_model = defaultdict(lambda: {"cost": 0.0, "turns": 0})
    for record in records:
        weight = _model_weight(record, config)
        if weight <= downgrade_weight:
            continue
        if not trivial_turn(record, settings):
            continue
        bucket = per_model[record["model"]]
        bucket["cost"] += (weight - downgrade_weight) * _raw_weighted(record, config)
        bucket["turns"] += 1
    return [
        _finding(
            "model_mismatch",
            model,
            "%s produced at most %d output tokens with %s, at most %d "
            "thinking tokens and no %s dispatch among them; %s would have cost %s weighted tokens less "
            "on %s"
            % (
                text.plural(bucket["turns"], "%s turn" % model),
                settings["max_output_tokens"],
                text.bounded(1, settings["max_tool_calls"], "tool call"),
                settings.get("max_thinking", MAX_THINKING_DEFAULT),
                DISPATCH_TOOL,
                "it" if bucket["turns"] == 1 else "they",
                f"{bucket['cost']:,.0f}",
                downgrade,
            ),
            bucket["cost"],
            {"turns": bucket["turns"], "downgrade_model": downgrade},
        )
        for model, bucket in sorted(per_model.items(), key=lambda kv: -kv[1]["cost"])
        if bucket["cost"] >= settings["min_total_weighted"]
    ]


def redundant_reads(records, config):
    settings = config["thresholds"]["redundant_reads"]
    watched = set(settings["tools"])
    findings = []
    for session_id, session in _by_session(records).items():
        groups = defaultdict(list)
        for record in session:
            for tool, share in _tool_shares(record):
                if tool["name"] in watched:
                    groups[(tool["name"], tool["hash"])].append((record, share))
        for (name, digest), occurrences in groups.items():
            if len(occurrences) < settings["min_repeats"]:
                continue
            cost = sum(share for _, share in occurrences[1:])
            findings.append(
                _finding(
                    "redundant_reads",
                    session_id,
                    "%s called with identical input %d times" % (name, len(occurrences)),
                    cost,
                    {
                        "tool": name,
                        "input_hash": digest,
                        "occurrences": len(occurrences),
                        "cwd": session[-1]["cwd"],
                    },
                )
            )
    return findings


def loop_retry(records, config):
    settings = config["thresholds"]["loop_retry"]
    findings = []
    for session_id, session in _by_session(records).items():
        sequence = []
        for record in session:
            for tool, share in _tool_shares(record):
                sequence.append((tool["name"], tool["hash"], share))
        start = 0
        while start < len(sequence):
            end = start + 1
            while end < len(sequence) and sequence[end][1] == sequence[start][1]:
                end += 1
            run = sequence[start:end]
            if len(run) >= settings["min_repeats"]:
                findings.append(
                    _finding(
                        "loop_retry",
                        session_id,
                        "%s repeated %d times back to back" % (run[0][0], len(run)),
                        sum(share for _, _, share in run[1:]),
                        {
                            "tool": run[0][0],
                            "input_hash": run[0][1],
                            "run_length": len(run),
                            "cwd": session[-1]["cwd"],
                        },
                    )
                )
            start = end
    return findings


def whale_turns(records, config):
    top_n = config["thresholds"]["whale_turns"]["top_n"]
    ranked = sorted(
        (r for r in records if r["weighted"] > 0), key=lambda r: (-r["weighted"], r["ts"], r["uuid"] or "")
    )[:top_n]
    return [
        _finding(
            "whale_turns",
            record["sessionId"],
            "single %s turn cost %s weighted tokens" % (record["model"], f"{record['weighted']:,.0f}"),
            record["weighted"],
            {
                "uuid": record["uuid"],
                "ts": record["ts"],
                "model": record["model"],
                "effort": record["effort"],
                "isSidechain": record["isSidechain"],
                "attributionAgent": record["attributionAgent"],
                "cwd": record["cwd"],
                "gitBranch": record["gitBranch"],
                "prompt": record["prompt"],
                "tools": [t["name"] for t in record["tools"]],
                "rank": rank,
            },
        )
        for rank, record in enumerate(ranked, start=1)
    ]


HEADROOM_DEFAULTS = {"max_pct": 60, "min_thinking": 500, "min_output": 800, "min_serial_minutes": 20}
QUOTA_METHODS = ("quota-fit", "override")


def headroom_settings(config):
    merged = dict(HEADROOM_DEFAULTS)
    merged.update(config.get("headroom") or {})
    return merged


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _stamp(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _judgement_components(records, settings):
    groups = defaultdict(list)
    for record in records:
        if record.get("attributionAgent"):
            groups[("agent", record["attributionAgent"])].append(record)
        if record.get("attributionSkill"):
            groups[("skill", record["attributionSkill"])].append(record)
    components = []
    for (component, name), group in groups.items():
        thinking = _median([record.get("thinking") or 0 for record in group])
        output = _median([record["output"] for record in group])
        if thinking <= settings["min_thinking"] and output <= settings["min_output"]:
            continue
        components.append(
            {
                "component": component,
                "name": name,
                "turns": len(group),
                "weighted": sum(record["weighted"] for record in group),
                "median_thinking": thinking,
                "median_output": output,
            }
        )
    components.sort(key=lambda item: (-item["weighted"], item["name"]))
    return components


def _runs_of(session):
    runs = defaultdict(list)
    for record in session:
        if record.get("agentId"):
            runs[record["agentId"]].append(record)
    spans = []
    for group in runs.values():
        stamps = sorted(_stamp(record["ts"]) for record in group)
        spans.append(
            {
                "start": stamps[0],
                "end": stamps[-1],
                "turns": len(group),
                "weighted": sum(record["weighted"] for record in group),
            }
        )
    return sorted(spans, key=lambda span: span["start"])


def _peak_live(spans):
    events = [(span["end"], -1) for span in spans] + [(span["start"], 1) for span in spans]
    events.sort()
    live = peak = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    return peak


def _serial_sessions(records, settings):
    sessions = []
    for session_id, session in _by_session(records).items():
        spans = _runs_of(session)
        if len(spans) < 2 or _peak_live(spans) > 1:
            continue
        minutes = sum((span["end"] - span["start"]).total_seconds() for span in spans) / 60.0
        if minutes < settings["min_serial_minutes"]:
            continue
        sessions.append(
            {
                "session": session_id,
                "runs": len(spans),
                "peak_live_runs": 1,
                "minutes": round(minutes, 2),
                "weighted": sum(span["weighted"] for span in spans),
                "median_run_weighted": _median([span["weighted"] for span in spans]),
                "cwd": session[-1]["cwd"],
            }
        )
    sessions.sort(key=lambda item: (-item["minutes"], item["session"]))
    return sessions


def headroom(records, config, quota):
    settings = headroom_settings(config)
    if quota.get("method") not in QUOTA_METHODS or not quota.get("closed"):
        return []
    ceiling = quota.get("ceiling")
    percent, previous = quota.get("percent_used"), quota.get("previous_percent_used")
    if not ceiling or percent is None or previous is None:
        return []
    if percent >= settings["max_pct"] or previous >= settings["max_pct"]:
        return []
    spent = quota.get("spent") or 0.0
    unused = max(0.0, ceiling - spent)
    return [
        _finding(
            "headroom",
            quota.get("window_key"),
            "the window closed at %.0f%% of the weekly quota and the one before it at %.0f%%, so %s "
            "weighted tokens of quota expired unused" % (percent, previous, f"{unused:,.0f}"),
            unused,
            {
                "method": quota.get("method"),
                "ceiling": ceiling,
                "spent": spent,
                "percent_used": percent,
                "previous_percent_used": previous,
                "max_pct": settings["max_pct"],
                "unused_weighted": unused,
                "components": _judgement_components(records, settings),
                "serial_sessions": _serial_sessions(records, settings),
                "extra_usage": quota.get("extra_usage"),
            },
        )
    ]


RULES = (
    context_bloat,
    subagent_storm,
    agent_type_skew,
    model_mismatch,
    redundant_reads,
    loop_retry,
    whale_turns,
)


def evaluate(records, config, quota=None):
    findings = []
    for rule in RULES:
        findings.extend(rule(records, config))
    if quota is not None:
        findings.extend(headroom(records, config, quota))
    findings.sort(key=lambda f: (-f["weighted_cost"], f["rule"], f["subject"] or "", f["detail"]))
    return findings


def _ordered_sessions(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["sessionId"]].append(record)
    for session in grouped.values():
        session.sort(key=lambda r: (r["ts"], r["uuid"] or ""))
    return grouped


def _detector(key, label, count, cost, detail, evidence):
    return {
        "key": key,
        "label": label,
        "count": count,
        "weighted_cost": float(cost),
        "detail": detail,
        "evidence": evidence,
    }


def round_trips(records):
    total_weighted = sum(record["weighted"] for record in records)
    total_calls = 0
    resolved_calls = 0

    failed = 0
    failed_cost = 0.0
    denied = 0
    denied_cost = 0.0
    retried = 0
    retried_cost = 0.0
    api_errors = 0
    api_error_cost = 0.0
    by_tool = Counter()
    per_tool = defaultdict(lambda: {"failures": 0, "retries": 0, "denied": 0, "weighted": 0.0})

    for session in _ordered_sessions(records).values():
        flat = []
        for record in session:
            if record.get("is_api_error"):
                api_errors += 1
                api_error_cost += record["weighted"]
            tools = record["tools"] or []
            share = record["weighted"] / len(tools) if tools else 0.0
            for tool in tools:
                total_calls += 1
                if tool.get("is_error") is not None:
                    resolved_calls += 1
                flat.append((tool["name"], tool["hash"], share, tool))

        first_failure = {}
        for position, (name, digest, share, tool) in enumerate(flat):
            if not tool.get("is_error"):
                continue
            failed += 1
            failed_cost += share
            by_tool[name] += 1
            first_failure.setdefault((name, digest), position)
            per_tool[name]["failures"] += 1
            per_tool[name]["weighted"] += share
            if tool.get("denied"):
                denied += 1
                denied_cost += share
                per_tool[name]["denied"] += 1
        for position, (name, digest, share, tool) in enumerate(flat):
            origin = first_failure.get((name, digest))
            if origin is not None and position > origin and tool.get("is_error"):
                retried += 1
                retried_cost += share
                per_tool[name]["retries"] += 1

    detectors = [
        _detector(
            FAILED_CALLS,
            "tool calls that came back as an error",
            failed,
            failed_cost,
            "the share of the turn that issued a call whose result was an error. The context cost of "
            "reading the error back is not counted, so this is a floor.",
            {"by_tool": by_tool.most_common(5)},
        ),
        _detector(
            RETRIED_AFTER_FAILURE,
            "calls that failed again on the same input",
            retried,
            retried_cost,
            "a subset of the calls above: the same tool with the same input, re-issued after it failed "
            "once in the same session, and failed again. A repeat that succeeded is the recovery, not "
            "waste, and is not counted here.",
            {},
        ),
        _detector(
            PERMISSION_DENIED,
            "calls stopped by a permission decision",
            denied,
            denied_cost,
            "a subset of the failed calls: the tool never ran.",
            {},
        ),
        _detector(
            API_ERROR_TURNS,
            "turns the API itself returned as an error",
            api_errors,
            api_error_cost,
            "counted from the collector's own is_api_error flag.",
            {},
        ),
    ]
    return {
        "records": len(records),
        "total_calls": total_calls,
        "resolved_calls": resolved_calls,
        "result_coverage": (resolved_calls / total_calls) if total_calls else 0.0,
        "by_tool": sorted(
            ({"tool": name, **counts} for name, counts in per_tool.items()),
            key=lambda row: (-row["weighted"], row["tool"]),
        ),
        "weighted": total_weighted,
        "detectors": detectors,
    }


ANOMALY_DEFAULTS = {
    "reply_skills": ["prose:reply-style", "prose:bro"],
    "headless_cwds": ["daily-improvement-review"],
    "headless_entrypoints": ["sdk-cli"],
    "headless_prompt_sources": ["scheduled", "sdk"],
    "reply_headless_share": 0.25,
    "repeat_min": 20,
    "repeat_minutes": 15,
    "growth_share": 0.6,
    "result_kb": 8,
    "fail_min": 50,
    "fail_dominant_share": 0.7,
    "unattributed_share": 0.5,
    "mcp_share": 0.04,
    "max_cards": 5,
    "min_growth_turns": 12,
    "growth_min_results": 20,
    "growth_total_mb": 4.0,
    "run_label_chars": 70,
}

REPLY_SKILL_HEADLESS = "reply_skill_headless"
REPEATED_TOOL_INPUT = "repeated_tool_input"
CONTEXT_GROWTH_TOOL = "context_growth_tool"
FAILING_TOOL = "failing_tool"
ROUND_TRIPS_ANCHOR = "round_trips"
UNATTRIBUTED_SUBAGENTS = "unattributed_subagents"
MCP_SERVER_SHARE = "mcp_server_share"


def anomaly_settings(config):
    merged = dict(ANOMALY_DEFAULTS)
    merged.update(config.get("anomalies") or {})
    return merged


def _field_coverage(records, field, subset=None):
    pool = records if subset is None else subset
    present = sum(1 for record in pool if record.get(field))
    return {
        "field": field,
        "present": present,
        "total": len(pool),
        "share": (present / len(pool)) if pool else 0.0,
    }


def _anomaly(key, subject, claim, numbers, basis, score, action, coverage, chart, anchor=None):
    return {
        "key": key,
        "subject": subject,
        "claim": claim,
        "numbers": numbers,
        "basis": basis,
        "score": float(score),
        "action": action,
        "coverage": coverage,
        "chart": chart,
        "anchor": anchor,
    }


def _bars(rows):
    return {"kind": "bars", "rows": rows}


def _table_chart(columns, rows):
    return {"kind": "table", "columns": columns, "rows": rows}


def _path_segments(path):
    return [part for part in str(path or "").replace("\\", "/").lower().split("/") if part]


def _modal(values):
    counts = Counter(value for value in values if value)
    return counts.most_common(1)[0][0] if counts else None


def headless_sessions(records, settings):
    wanted = {name.lower() for name in settings["headless_cwds"]}
    entrypoints = {name.lower() for name in settings["headless_entrypoints"]}
    sources = {name.lower() for name in settings["headless_prompt_sources"]}
    headless = {}
    for session_id, session in _by_session(records).items():
        cwd = _modal(record.get("cwd") for record in session)
        by_cwd = bool(wanted & set(_path_segments(cwd)))
        by_entry = any(str(record.get("entrypoint") or "").lower() in entrypoints for record in session)
        by_source = any(str(record.get("prompt_source") or "").lower() in sources for record in session)
        if by_cwd or by_entry or by_source:
            headless[session_id] = cwd
    return headless


def skill_file_of(name):
    parts = str(name).split(":")
    if len(parts) == 2:
        return "%s/skills/%s/SKILL.md" % (parts[0], parts[1])
    return "skills/%s/SKILL.md" % name


def _reply_skill_headless(records, settings):
    watched = set(settings["reply_skills"])
    if not watched:
        return []
    headless = headless_sessions(records, settings)
    totals = defaultdict(lambda: {"total": 0.0, "inside": 0.0, "turns": 0, "inside_turns": 0, "cwds": []})
    for record in records:
        skill = record.get("attributionSkill")
        if skill not in watched:
            continue
        bucket = totals[skill]
        bucket["total"] += record["weighted"]
        bucket["turns"] += 1
        if record["sessionId"] in headless:
            bucket["inside"] += record["weighted"]
            bucket["inside_turns"] += 1
            bucket["cwds"].append(headless[record["sessionId"]])
    findings = []
    for skill, bucket in totals.items():
        if not bucket["total"]:
            continue
        share = bucket["inside"] / bucket["total"]
        if share < settings["reply_headless_share"]:
            continue
        project = _modal(bucket["cwds"]) or "the headless project"
        findings.append(
            _anomaly(
                REPLY_SKILL_HEADLESS,
                skill,
                "%s cost %s weighted tokens, %.0f%% of it in sessions with nobody reading the reply"
                % (skill, f"{bucket['total']:,.0f}", 100 * share),
                {
                    "weighted": bucket["total"],
                    "headless_weighted": bucket["inside"],
                    "headless_share": share,
                    "turns": bucket["turns"],
                    "headless_turns": bucket["inside_turns"],
                    "project": project,
                },
                "%.0f%% of the skill's spend sits in headless sessions" % (100 * share),
                share / settings["reply_headless_share"],
                "Turn its plugin off for %s in that project's .claude/settings.json; the skill file is %s."
                % (project, skill_file_of(skill)),
                _field_coverage(records, "attributionSkill"),
                _bars(
                    [
                        {"label": "headless sessions", "value": bucket["inside"]},
                        {"label": "sessions with a reader", "value": bucket["total"] - bucket["inside"]},
                    ]
                ),
            )
        )
    return findings


SUMMARY_ATTR = re.compile(r'summary="([^"]{3,})"')


def _prompt_label(prompt, limit):
    body = text.repair_mojibake(str(prompt or "").strip())
    if not body:
        return None
    match = SUMMARY_ATTR.search(body)
    if match:
        return match.group(1)[:limit]
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("<"):
            return line[:limit]
    return None


def _run_label(session, settings):
    counts = Counter()
    for record in session:
        label = _prompt_label(record.get("prompt"), settings["run_label_chars"])
        if label:
            counts[label] += 1
    return counts.most_common(1)[0][0] if counts else "a run with no recorded dispatch description"


def _runs(records):
    runs = defaultdict(list)
    for record in records:
        runs[record.get("agentId") or record["sessionId"]].append(record)
    for run in runs.values():
        run.sort(key=lambda record: record["ts"])
    return runs


def _repeated_tool_input(records, settings):
    minimum, minutes = settings["repeat_min"], settings["repeat_minutes"]
    findings = []
    for run in _runs(records).values():
        groups = defaultdict(list)
        for record in run:
            for call in record.get("tools") or []:
                groups[(call["name"], call["hash"])].append(record["ts"])
        for (name, digest), stamps in groups.items():
            stamps.sort()
            best = None
            left = 0
            for right in range(len(stamps)):
                while (_stamp(stamps[right]) - _stamp(stamps[left])).total_seconds() > minutes * 60:
                    left += 1
                span = right - left + 1
                if best is None or span > best[0]:
                    best = (span, stamps[left], stamps[right])
            if best is None or best[0] <= minimum:
                continue
            span_minutes = max(1.0 / 60, (_stamp(best[2]) - _stamp(best[1])).total_seconds() / 60.0)
            per_minute = best[0] / span_minutes
            label = _run_label(run, settings)
            findings.append(
                _anomaly(
                    REPEATED_TOOL_INPUT,
                    name,
                    "%s was called with the same input %d times in %.0f minutes inside one run"
                    % (name, best[0], span_minutes),
                    {
                        "tool": name,
                        "repeats": best[0],
                        "minutes": round(span_minutes, 1),
                        "per_minute": round(per_minute, 2),
                        "input_hash": digest,
                        "run": label,
                        "first_ts": best[1],
                        "last_ts": best[2],
                    },
                    "%.1f identical calls a minute" % per_minute,
                    best[0] / float(minimum),
                    'Give the run dispatched as "%s" a stop condition on %s.' % (label, name),
                    _field_coverage(records, "tools"),
                    _table_chart(
                        ["run", "tool", "repeats", "minutes", "per minute", "from", "to"],
                        [
                            [
                                label,
                                name,
                                "%d" % best[0],
                                "%.0f" % span_minutes,
                                "%.1f" % per_minute,
                                best[1][11:19],
                                best[2][11:19],
                            ]
                        ],
                    ),
                )
            )
    findings.sort(key=lambda item: -item["score"])
    return findings[:1]


def _result_bytes(session, name):
    return [
        call["result_chars"]
        for record in session
        for call in record.get("tools") or []
        if call["name"] == name and call.get("result_chars")
    ]


def _context_growth_tool(records, config, settings):
    minimum_kb = settings["result_kb"]
    findings = []
    for session in _by_session(records).values():
        if len(session) < settings["min_growth_turns"]:
            continue
        summary = context.summarize_session(session, config)
        total = summary["growth_total"]
        if not total:
            continue
        for entry in summary["growth_by_tool"][:1]:
            share = entry["tokens"] / total
            sizes = _result_bytes(session, entry["tool"])
            median_bytes = _median(sizes)
            total_mb = sum(sizes) / 1048576.0
            if entry["results"] < settings["growth_min_results"] or share < settings["growth_share"]:
                continue
            size_over = median_bytes / (minimum_kb * 1024.0)
            volume_over = total_mb / settings["growth_total_mb"]
            if max(size_over, volume_over) < 1.0:
                continue
            median_kb = median_bytes / 1024.0
            extra = (
                " Its output limits live in the dispatching agent definition."
                if entry["tool"] == "Bash"
                else ""
            )
            findings.append(
                _anomaly(
                    CONTEXT_GROWTH_TOOL,
                    entry["tool"],
                    "%s results are %.0f%% of the context this session re-read: %d results, %.1f MB, "
                    "median %.0f KB" % (entry["tool"], 100 * share, entry["results"], total_mb, median_kb),
                    {
                        "tool": entry["tool"],
                        "session": summary["session"],
                        "growth_share": share,
                        "growth_tokens": entry["tokens"],
                        "results": entry["results"],
                        "median_result_bytes": median_bytes,
                        "total_result_mb": total_mb,
                    },
                    "%.0f%% of the growth, %.1f MB over %d results" % (100 * share, total_mb, entry["results"]),
                    (share / settings["growth_share"]) * max(size_over, volume_over),
                    "Cut what %s hands back: pipe it through head, or write a file and read the slice.%s"
                    % (entry["tool"], extra),
                    dict(summary["tool_results_coverage"], field="tool result sizes"),
                    _bars(
                        [
                            {"label": item["tool"], "value": item["tokens"]}
                            for item in summary["growth_by_tool"][:4]
                        ]
                    ),
                )
            )
    findings.sort(key=lambda item: -item["score"])
    return findings[:1]


def _failing_tool(records, settings):
    failures = Counter()
    calls = 0
    resolved = 0
    for record in records:
        for call in record.get("tools") or []:
            calls += 1
            if call.get("is_error") is not None:
                resolved += 1
            if call.get("is_error"):
                failures[call["name"]] += 1
    total = sum(failures.values())
    if total <= settings["fail_min"] or not failures:
        return []
    name, count = failures.most_common(1)[0]
    share = count / total
    if share <= settings["fail_dominant_share"]:
        return []
    return [
        _anomaly(
            FAILING_TOOL,
            name,
            "%d tool calls came back as an error, %d of them (%.0f%%) on %s"
            % (total, count, 100 * share, name),
            {"tool": name, "failures": count, "window_failures": total, "share": share},
            "%d failures on one tool" % count,
            count / float(settings["fail_min"]),
            "Read the failing %s calls in the round trips table and fix the call site." % name,
            {
                "field": "tool result status",
                "present": resolved,
                "total": calls,
                "share": (resolved / calls) if calls else 0.0,
            },
            _bars([{"label": tool_name, "value": hits} for tool_name, hits in failures.most_common(4)]),
            anchor=ROUND_TRIPS_ANCHOR,
        )
    ]


def _unattributed_subagents(records, settings):
    sidechain = [record for record in records if record["isSidechain"]]
    total = sum(record["weighted"] for record in sidechain)
    if not total:
        return []
    unnamed = sum(record["weighted"] for record in sidechain if not record.get("attributionAgent"))
    turns = sum(1 for record in sidechain if not record.get("attributionAgent"))
    share = unnamed / total
    if share <= settings["unattributed_share"]:
        return []
    return [
        _anomaly(
            UNATTRIBUTED_SUBAGENTS,
            "subagent lane",
            "%.0f%% of subagent spend (%s weighted tokens over %d turns) carries no agent type"
            % (100 * share, f"{unnamed:,.0f}", turns),
            {
                "unattributed_weighted": unnamed,
                "sidechain_weighted": total,
                "share": share,
                "turns": turns,
            },
            "%.0f%% of the subagent lane has no agent type" % (100 * share),
            share / settings["unattributed_share"],
            "These runs were dispatched without subagent_type, or by a plugin whose agents are not on "
            "disk. Name the type at the dispatch site.",
            _field_coverage(records, "attributionAgent", sidechain),
            _bars(
                [
                    {"label": "no agent type", "value": unnamed},
                    {"label": "named agent type", "value": total - unnamed},
                ]
            ),
        )
    ]


def _mcp_server_share(records, settings):
    total = sum(record["weighted"] for record in records)
    if not total:
        return []
    servers = defaultdict(lambda: {"weighted": 0.0, "turns": 0})
    for record in records:
        server = record.get("mcp_server")
        if not server:
            continue
        servers[server]["weighted"] += record["weighted"]
        servers[server]["turns"] += 1
    ranked = sorted(servers.items(), key=lambda kv: -kv[1]["weighted"])
    findings = []
    for server, bucket in ranked:
        share = bucket["weighted"] / total
        if share <= settings["mcp_share"]:
            continue
        findings.append(
            _anomaly(
                MCP_SERVER_SHARE,
                server,
                "the %s MCP server cost %s weighted over %d turns, %.1f%% of the window"
                % (server, f"{bucket['weighted']:,.0f}", bucket["turns"], 100 * share),
                {"server": server, "weighted": bucket["weighted"], "turns": bucket["turns"], "share": share},
                "%.1f%% of the window on one server" % (100 * share),
                share / settings["mcp_share"],
                "Narrow what %s is asked for over its %d turns." % (server, bucket["turns"]),
                _field_coverage(records, "mcp_server"),
                _bars([{"label": name, "value": entry["weighted"]} for name, entry in ranked[:4]]),
            )
        )
    return findings


def anomalies(records, config):
    settings = anomaly_settings(config)
    found = []
    found.extend(_reply_skill_headless(records, settings))
    found.extend(_repeated_tool_input(records, settings))
    found.extend(_context_growth_tool(records, config, settings))
    found.extend(_failing_tool(records, settings))
    found.extend(_unattributed_subagents(records, settings))
    found.extend(_mcp_server_share(records, settings))
    found.sort(key=lambda item: (-item["score"], item["key"], item["subject"] or ""))
    return found
