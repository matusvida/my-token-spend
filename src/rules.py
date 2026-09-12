from collections import Counter, defaultdict


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


def _tool_share(record):
    tools = record["tools"]
    return record["weighted"] / len(tools) if tools else 0.0


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
    cache_read_weight = config["token_class_weights"]["cache_read"]
    findings = []
    for session_id, session in _by_session(records).items():
        if len(session) < settings["min_turns"]:
            continue
        excess_tokens = 0
        cost = 0.0
        for record in session:
            over = max(0, record["cache_read"] - threshold)
            if over:
                excess_tokens += over
                cost += _model_weight(record, config) * cache_read_weight * over
        if not excess_tokens:
            continue
        findings.append(
            _finding(
                "context_bloat",
                session_id,
                "session of %d turns re-read %s tokens of context above the %s-token threshold"
                % (len(session), f"{excess_tokens:,}", f"{threshold:,}"),
                cost,
                {
                    "turns": len(session),
                    "excess_cache_read_tokens": excess_tokens,
                    "peak_cache_read": max(r["cache_read"] for r in session),
                    "cwd": session[-1]["cwd"],
                    "first_ts": session[0]["ts"],
                    "last_ts": session[-1]["ts"],
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


def model_mismatch(records, config):
    settings = config["thresholds"]["model_mismatch"]
    downgrade = settings["downgrade_model"]
    downgrade_weight = model_weight(downgrade, config)[0]
    per_model = defaultdict(lambda: {"cost": 0.0, "turns": 0})
    for record in records:
        weight = _model_weight(record, config)
        if weight <= downgrade_weight:
            continue
        if record["output"] > settings["max_output_tokens"]:
            continue
        if not 1 <= len(record["tools"]) <= settings["max_tool_calls"]:
            continue
        bucket = per_model[record["model"]]
        bucket["cost"] += (weight - downgrade_weight) * _raw_weighted(record, config)
        bucket["turns"] += 1
    return [
        _finding(
            "model_mismatch",
            model,
            "%d trivial %s turns would have cost %s weighted tokens less on %s"
            % (bucket["turns"], model, f"{bucket['cost']:,.0f}", downgrade),
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
            share = _tool_share(record)
            for tool in record["tools"]:
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
            share = _tool_share(record)
            for tool in record["tools"]:
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


RULES = (
    context_bloat,
    subagent_storm,
    agent_type_skew,
    model_mismatch,
    redundant_reads,
    loop_retry,
    whale_turns,
)


def evaluate(records, config):
    findings = []
    for rule in RULES:
        findings.extend(rule(records, config))
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
            if tool.get("denied"):
                denied += 1
                denied_cost += share
        for position, (name, digest, share, tool) in enumerate(flat):
            origin = first_failure.get((name, digest))
            if origin is not None and position > origin and tool.get("is_error"):
                retried += 1
                retried_cost += share

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
        "weighted": total_weighted,
        "detectors": detectors,
    }
