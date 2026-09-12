import rootcause
import rules

SCATTER_POINTS = 400
TIMELINE_RUNS = 6
TABLE_ROWS = 4
LANE_ROWS = 3
JOBS_TAIL_ROWS = 6

RULE_GROUPS = {"context_bloat": 1, "subagent_storm": 1, "agent_type_skew": 1}
ROUND_TRIPS = "round_trips"
HEADROOM = "headroom"


def threshold_text(rule, config):
    if rule == ROUND_TRIPS:
        return "any tool call whose result came back an error"
    settings = config["thresholds"]
    if rule == "context_bloat":
        block = settings["context_bloat"]
        return "%d+ turn sessions, above %s cache-read tokens a turn" % (
            block["min_turns"],
            "{:,}".format(block["cache_read_per_turn"]),
        )
    if rule == "subagent_storm":
        block = settings["subagent_storm"]
        return "%d+ subagent turns and %.0f%%+ of the session" % (
            block["min_sidechain_turns"],
            100.0 * block["min_cost_share"],
        )
    if rule == "agent_type_skew":
        block = settings["agent_type_skew"]
        return "the %d costliest agent types above %s weighted" % (
            block["top_n"],
            "{:,}".format(block["min_weighted"]),
        )
    if rule == "model_mismatch":
        block = settings["model_mismatch"]
        return "under %d output tokens, at most %d tool call, above %s weighted" % (
            block["max_output_tokens"],
            block["max_tool_calls"],
            "{:,}".format(block["min_total_weighted"]),
        )
    if rule == "redundant_reads":
        block = settings["redundant_reads"]
        return "%d+ identical inputs to %s in one session" % (
            block["min_repeats"],
            ", ".join(block["tools"]),
        )
    if rule == "loop_retry":
        return "%d+ identical calls back to back" % settings["loop_retry"]["min_repeats"]
    if rule == "whale_turns":
        return "the %d costliest single turns" % settings["whale_turns"]["top_n"]
    return ""


def _session_context(window, session_id):
    for entry in (window.get("context") or {}).get("sessions") or []:
        if entry.get("session") == session_id:
            return entry
    return None


def _context_chart(window, finding):
    summary = _session_context(window, finding["subject"])
    if not summary or not summary.get("series"):
        return None
    return {
        "kind": "context_series",
        "series": summary["series"],
        "threshold": (window.get("context") or {}).get("threshold"),
        "compactions": summary.get("compaction_ts") or [],
        "by_tool": summary.get("growth_by_tool") or [],
        "top_results": summary.get("top_results") or [],
        "coverage": summary.get("tool_results_coverage") or {},
    }


def _run_label(run):
    return run.get("description") or rootcause.derived_label(run)


def _storm_chart(records, finding, agent_calls):
    turns = [r for r in records if r["sessionId"] == finding["subject"] and r.get("isSidechain")]
    runs = rootcause.group_runs(turns, agent_calls=agent_calls)
    if not runs:
        return None
    shown = sorted(runs, key=lambda run: -run["weighted"])[:TIMELINE_RUNS]
    shown.sort(key=lambda run: run["first_ts"])
    return {
        "kind": "run_timeline",
        "runs": [
            {
                "label": _run_label(run),
                "named": bool(run.get("description")),
                "first_ts": run["first_ts"],
                "last_ts": run["last_ts"],
                "turns": run["turns"],
                "weighted": run["weighted"],
                "agent": run["agent"] or "unattributed",
            }
            for run in shown
        ],
        "hidden": len(runs) - len(shown),
        "overlap": rootcause.overlap(runs),
        "descriptions": rootcause.description_coverage(runs),
    }


def _mismatch_chart(records, config):
    settings = config["thresholds"]["model_mismatch"]
    turns = [record for record in records if record.get("tools")]
    if not turns:
        return None
    turns.sort(key=lambda record: (record["ts"], record.get("uuid") or ""))
    stride = max(1, len(turns) // SCATTER_POINTS)
    sampled = turns[::stride][:SCATTER_POINTS]
    ranked = sorted({record["model"] for record in turns})
    weight = {model: rules.model_weight(model, config)[0] for model in ranked}
    expensive = sorted(ranked, key=lambda model: (-weight[model], model))[:3]
    outputs = sorted(record["output"] for record in sampled)
    axis_max = max(outputs[int(0.95 * (len(outputs) - 1))], 4 * settings["max_output_tokens"])
    max_thinking = settings.get("max_thinking", rules.MAX_THINKING_DEFAULT)
    thinking_values = sorted((record.get("thinking") or 0) for record in sampled)
    thinking_max = max(thinking_values[int(0.95 * (len(thinking_values) - 1))], 4 * max_thinking)
    points = [
        {
            "tools": len(record["tools"]),
            "output": record["output"],
            "thinking": min(record.get("thinking") or 0, thinking_max),
            "model": record["model"] if record["model"] in expensive else "other models",
        }
        for record in sampled
        if record["output"] <= axis_max
    ]
    return {
        "kind": "scatter",
        "points": points,
        "models": expensive + (["other models"] if len(ranked) > len(expensive) else []),
        "box": {
            "output": settings["max_output_tokens"],
            "tools": settings["max_tool_calls"],
            "thinking": max_thinking,
        },
        "axis_max": axis_max,
        "thinking_max": thinking_max,
        "above_axis": len(sampled) - len(points),
        "plotted": len(points),
        "total": len(turns),
    }


def _skew_chart(records, finding, config, agent_calls):
    turns = [record for record in records if record.get("attributionAgent") == finding["subject"]]
    runs = rootcause.group_runs(turns, agent_calls=agent_calls)
    if not runs:
        return None
    clusters = rootcause.cluster_runs(runs, max_clusters=rootcause.settings(config)["max_clusters"])
    rows = [
        {
            "label": cluster["label"],
            "weighted": cluster["weighted"],
            "runs": cluster["runs"],
            "confidence": cluster["confidence"],
            "derived": cluster["label_source"] == "derived",
            "tail": cluster["tail"],
        }
        for cluster in clusters
    ]
    residual = sum(record["weighted"] for record in turns if not record.get(rootcause.RUN_ID_KEY))
    if residual > 0:
        rows.append(
            {
                "label": "turns with no run id",
                "weighted": residual,
                "runs": 0,
                "confidence": "residual",
                "derived": True,
                "tail": False,
            }
        )
    return {
        "kind": "cluster_bars",
        "rows": rows,
        "tail_note": rootcause.tail_note(clusters),
        "descriptions": rootcause.description_coverage(runs),
    }


WHALE_SERIES = ("context re-read from cache", "context written to cache", "output", "fresh input")


def _turn_numbers(records):
    ordered = {}
    for record in sorted(records, key=lambda item: (item.get("ts") or "", item.get("uuid") or "")):
        ordered.setdefault(record["sessionId"], []).append(record.get("uuid"))
    return {uuid: index + 1 for uuids in ordered.values() for index, uuid in enumerate(uuids)}


def _whale_chart(records, findings, config):
    by_uuid = {record["uuid"]: record for record in records if record.get("uuid")}
    numbers = _turn_numbers(records)
    rows = []
    for finding in findings:
        record = by_uuid.get(finding["evidence"].get("uuid"))
        if record is None:
            continue
        parts = dict(rootcause.weight_components(record, config))
        stamp = record["ts"] or ""
        rows.append(
            {
                "label": stamp[11:16],
                "second_label": stamp[11:19],
                "turn": numbers.get(record["uuid"]),
                "model": record["model"],
                "agent": record.get("attributionAgent") or "main agent",
                "weighted": finding["weighted_cost"],
                "parts": [parts[name] for name in WHALE_SERIES],
            }
        )
    if not rows:
        return None
    if len({row["label"] for row in rows}) < len(rows):
        for row in rows:
            row["label"] = row["second_label"]
            row["sublabel"] = "#%d" % row["turn"] if row["turn"] else None
    return {"kind": "whale_bars", "rows": rows, "series": list(WHALE_SERIES)}


def _repeat_chart(records, findings):
    sessions = {}
    for record in records:
        sessions.setdefault(record["sessionId"], []).append(record)
    for turns in sessions.values():
        turns.sort(key=lambda record: (record["ts"], record.get("uuid") or ""))
    rows = []
    for finding in sorted(findings, key=lambda item: -item["weighted_cost"])[:TABLE_ROWS]:
        tool = finding["evidence"].get("tool")
        digest = finding["evidence"].get("input_hash")
        hits = [
            record
            for record in sessions.get(finding["subject"]) or []
            if any(item["name"] == tool and item.get("hash") == digest for item in record.get("tools") or [])
        ]
        rows.append(
            {
                "tool": tool,
                "session": finding["subject"],
                "repeats": finding["evidence"].get("occurrences")
                or finding["evidence"].get("run_length")
                or len(hits),
                "first": (hits[0]["ts"] if hits else "")[11:16],
                "last": (hits[-1]["ts"] if hits else "")[11:16],
                "repo": rootcause.repo_of(finding["evidence"].get("cwd")),
                "weighted": finding["weighted_cost"],
            }
        )
    return {"kind": "repeat_table", "rows": rows, "hidden": max(0, len(findings) - len(rows))}


def _detector_of(analysis, key):
    for item in analysis["detectors"]:
        if item["key"] == key:
            return item
    return {"count": 0, "weighted_cost": 0.0}


def round_trip_chart(analysis):
    if not analysis.get("by_tool"):
        return None
    failed = _detector_of(analysis, rules.FAILED_CALLS)
    return {
        "kind": "round_trip_table",
        "rows": analysis["by_tool"][:TABLE_ROWS],
        "tools": len(analysis["by_tool"]),
        "failures": failed["count"],
        "weighted": failed["weighted_cost"],
        "detectors": analysis["detectors"],
        "coverage": analysis["result_coverage"],
        "calls": analysis["total_calls"],
    }


def _card(rule, findings, chart, config, anchor, index, hidden=0):
    return {
        "id": anchor,
        "rule": rule,
        "findings": findings,
        "index": index,
        "hidden": hidden,
        "weighted_cost": sum(finding["weighted_cost"] for finding in findings),
        "threshold": threshold_text(rule, config),
        "chart": chart,
    }


def cards(window, records, config, agent_calls=None):
    by_rule = {}
    for index, finding in enumerate(window["findings"]):
        if finding["rule"] == HEADROOM:
            continue
        by_rule.setdefault(finding["rule"], []).append((index, finding))
    built = []
    for rule, entries in by_rule.items():
        entries.sort(key=lambda pair: -pair[1]["weighted_cost"])
        if rule in RULE_GROUPS:
            for position, (index, finding) in enumerate(entries[: RULE_GROUPS[rule]]):
                if rule == "context_bloat":
                    chart = _context_chart(window, finding)
                elif rule == "subagent_storm":
                    chart = _storm_chart(records, finding, agent_calls)
                else:
                    chart = _skew_chart(records, finding, config, agent_calls)
                built.append(
                    _card(
                        rule,
                        [finding],
                        chart,
                        config,
                        "%s-%d" % (rule, position),
                        index,
                        max(0, len(entries) - RULE_GROUPS[rule]) if position == 0 else 0,
                    )
                )
            continue
        findings = [finding for _, finding in entries]
        if rule == "model_mismatch":
            chart = _mismatch_chart(records, config)
        elif rule == "whale_turns":
            chart = _whale_chart(records, findings, config)
        else:
            chart = _repeat_chart(records, findings)
        built.append(_card(rule, findings, chart, config, rule, entries[0][0]))
    built.sort(key=lambda card: (-card["weighted_cost"], card["id"]))
    return built


def _coverage(records, field):
    present = sum(1 for record in records if record.get(field))
    return {"present": present, "total": len(records), "share": (present / len(records)) if records else 0.0}


def _share_text(coverage):
    return "%.0f%% of turns" % (100.0 * (coverage or {}).get("share", 0.0))


def _lane(name, entries, note, limit=LANE_ROWS):
    rows = [
        {"label": entry["key"], "weighted": entry["weighted"], "turns": entry.get("turns")}
        for entry in sorted(entries, key=lambda entry: -entry["weighted"])[:limit]
    ]
    return {"name": name, "rows": rows, "note": note}


def lanes(window, records, config, agent_calls=None):
    coverage = window.get("field_coverage") or {}
    runs = rootcause.group_runs(records, agent_calls=agent_calls)
    clusters = rootcause.cluster_runs(runs, max_clusters=rootcause.settings(config)["max_clusters"])
    tail = rootcause.tail_note(clusters)
    named = [cluster for cluster in clusters if not cluster["tail"]]
    grouped = [cluster for cluster in clusters if cluster["tail"]]
    shown = named + grouped[:JOBS_TAIL_ROWS]
    note = rootcause.description_note_of(rootcause.description_coverage(runs)) + ("; " + tail if tail else "")
    if len(grouped) > JOBS_TAIL_ROWS:
        note += ", %d groups not drawn" % (len(grouped) - JOBS_TAIL_ROWS)
    built = [
        {
            "name": "Jobs, by dispatch description where recovered",
            "rows": [
                {
                    "label": cluster["label"],
                    "weighted": cluster["weighted"],
                    "turns": cluster["turns"],
                    "derived": cluster["label_source"] == "derived",
                }
                for cluster in shown
            ],
            "note": note,
        }
    ]
    for name, entries, note in (
        (
            "MCP servers",
            window.get("by_mcp_server") or [],
            "recorded on %s" % _share_text(coverage.get("mcp_server")),
        ),
        (
            "Plugins",
            window.get("by_plugin") or [],
            "recorded on %s" % _share_text(coverage.get("plugin")),
        ),
        (
            "Skills",
            window.get("by_skill") or [],
            "recorded on %s" % _share_text(_coverage(records, "attributionSkill")),
        ),
        (
            "Repos",
            window.get("by_repo") or [],
            "recorded on %s" % _share_text(_coverage(records, "cwd")),
        ),
        ("Models", window.get("by_model") or [], "recorded on every turn"),
    ):
        built.append(_lane(name, entries, note))
    return built


def build(window, records, config, agent_calls=None):
    analysis = rules.round_trips(records)
    return {
        "cards": cards(window, records, config, agent_calls),
        "lanes": lanes(window, records, config, agent_calls),
        "round_trips": analysis,
        "round_trip_chart": round_trip_chart(analysis),
    }
