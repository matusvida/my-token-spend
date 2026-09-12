import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import advice
import agentfiles
import collect
import paths
import rules

PROPOSAL = "proposal"
SETTING_PROPOSAL = "setting_proposal"
ALREADY_RIGHT_SIZED = "already_right_sized"
NO_FILE = "no_file"
AMBIGUOUS = "ambiguous"
NOT_ASSESSABLE = "not_assessable"
OBSERVATION = "observation"
ONE_OFF = "one_off"

DEFAULTS = {
    "builtin_agents": [],
    "cheap_model_families": [],
    "min_saving": 250000,
    "min_cost": 250000,
    "windows": 4,
    "min_windows_for_proposal": 2,
    "front_load_days": 3,
    "approach_fraction": 0.9,
    "coverage_floor": 0.9,
    "cost_per_use_multiple": 2.0,
    "quiet_invocation_turns": 2,
}

HONESTY = (
    "This report measures COST only. Accuracy and answer quality are not recorded anywhere in this "
    "data, so nothing here can promise a change preserves them. The only speed figures available are "
    "turn counts and wall-clock between timestamps, and they are labelled as such."
)

SUBAGENT_MODEL_ENV = "CLAUDE_CODE_SUBAGENT_MODEL"


def settings(config):
    merged = dict(DEFAULTS)
    for source in (paths.shipped_config(), config):
        for block in ("advice", "tune"):
            merged.update({k: v for k, v in (source.get(block) or {}).items() if k in DEFAULTS})
    return merged


_model_family = advice.model_family








def subagent_model_setting(user_home=None):
    path = (Path(user_home) if user_home else Path.home()) / ".claude" / "settings.json"
    setting = {"env": SUBAGENT_MODEL_ENV, "path": str(path), "readable": False, "value": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return setting
    if not isinstance(payload, dict):
        return setting
    setting["readable"] = True
    environment = payload.get("env")
    if isinstance(environment, dict):
        value = environment.get(SUBAGENT_MODEL_ENV)
        setting["value"] = value if isinstance(value, str) else None
    return setting






def median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def window_is_closed(window_data, now=None):
    now = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(window_data["window"]["end_utc"]) <= now


def load_windows(data_dir):
    windows = []
    for path in sorted(Path(data_dir).glob("week_*.json")):
        try:
            windows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return windows


def select_windows(windows, count=None, window=None, now=None):
    if not windows:
        raise LookupError("no collected windows found; run collect first")
    ordered = sorted(windows, key=lambda data: data["window"]["start"])
    if window:
        key = "week_" + window.replace("-", "_")
        for data in ordered:
            if data["window"]["key"] == key or data["window"]["start"] == window:
                note = None if window_is_closed(data, now) else (
                    "%s is still open, so its per-window figures are partial." % data["window"]["key"]
                )
                return [data], note
        raise LookupError(
            "no window %s; collected windows are %s" % (window, ", ".join(w["window"]["key"] for w in ordered))
        )
    closed = [data for data in ordered if window_is_closed(data, now)]
    if not closed:
        return [ordered[-1]], (
            "no closed window yet, so %s is still open and its per-window figures are partial."
            % ordered[-1]["window"]["key"]
        )
    wanted = count or DEFAULTS["windows"]
    chosen = closed[-wanted:]
    note = None
    if len(chosen) < wanted:
        note = "only %d closed window(s) exist, so the analysis uses all of them." % len(chosen)
    return chosen, note


def current_window(windows, now=None):
    for data in sorted(windows, key=lambda item: item["window"]["start"], reverse=True):
        if not window_is_closed(data, now):
            return data
    return None


def project_dirs(windows, limit=12):
    totals = defaultdict(float)
    for data in windows:
        for bucket in data.get("by_repo") or []:
            if bucket["key"]:
                totals[bucket["key"]] += bucket["weighted"]
    return [key for key, _ in sorted(totals.items(), key=lambda item: -item[1])[:limit]]


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _invocation_stats(records, component, name):
    field = "attributionAgent" if component == "agent" else "attributionSkill"
    key_field = "agentId" if component == "agent" else "sessionId"
    groups = defaultdict(list)
    for record in records:
        if record.get(field) == name:
            groups[record.get(key_field) or record.get("sessionId")].append(record)
    spans = []
    quiet = 0
    for group in groups.values():
        stamps = sorted(_parse_ts(record["ts"]) for record in group)
        spans.append((stamps[-1] - stamps[0]).total_seconds())
        if len(group) <= DEFAULTS["quiet_invocation_turns"]:
            quiet += 1
    return {
        "invocations": len(groups),
        "invocation_unit": "agent run" if component == "agent" else "session",
        "median_wall_clock_seconds": median(spans) if spans else 0.0,
        "quiet_invocations": quiet,
    }


def _trivial_by_agent(records, config, pricing):
    thresholds = config["thresholds"]["model_mismatch"]
    downgrade_weight = rules.model_weight(thresholds["downgrade_model"], pricing)[0]
    totals = defaultdict(lambda: {"weighted": 0.0, "turns": 0})
    for record in records:
        agent = record.get("attributionAgent")
        if not agent or record["output"] > thresholds["max_output_tokens"]:
            continue
        if not 1 <= len(record["tools"]) <= thresholds["max_tool_calls"]:
            continue
        weight = rules.model_weight(record["model"], pricing)[0]
        if weight <= downgrade_weight:
            continue
        bucket = totals[agent]
        bucket["weighted"] += (weight - downgrade_weight) * rules._raw_weighted(record, pricing)
        bucket["turns"] += 1
    return totals


def collect_centres(windows, records_by_window, settings_map, config):
    keys = [data["window"]["key"] for data in windows]
    centres = {}
    for data in windows:
        key = data["window"]["key"]
        records = records_by_window.get(key) or []
        trivial = _trivial_by_agent(records, config, data.get("weights") or config)
        for component, bucket_name in (("agent", "by_agent"), ("skill", "by_skill")):
            for bucket in data.get(bucket_name) or []:
                if not bucket["key"]:
                    continue
                centre = centres.setdefault(
                    (component, bucket["key"]),
                    {
                        "component": component,
                        "name": bucket["key"],
                        "plugin": agentfiles._split_qualified(bucket["key"])[0],
                        "per_window": {},
                        "turns": 0,
                        "total_weighted": 0.0,
                        "invocations": 0,
                        "quiet_invocations": 0,
                        "wall_clocks": [],
                        "trivial_per_window": {},
                        "trivial_turns": 0,
                        "invocation_unit": "agent run" if component == "agent" else "session",
                    },
                )
                centre["per_window"][key] = {"weighted": bucket["weighted"], "turns": bucket["turns"]}
                centre["turns"] += bucket["turns"]
                centre["total_weighted"] += bucket["weighted"]
                cheap = trivial.get(bucket["key"]) if component == "agent" else None
                centre["trivial_per_window"][key] = (cheap or {}).get("weighted", 0.0)
                centre["trivial_turns"] += (cheap or {}).get("turns", 0)
                stats = _invocation_stats(records, component, bucket["key"])
                centre["invocations"] += stats["invocations"]
                centre["quiet_invocations"] += stats["quiet_invocations"]
                if stats["median_wall_clock_seconds"]:
                    centre["wall_clocks"].append(stats["median_wall_clock_seconds"])

    for centre in centres.values():
        series = [centre["per_window"].get(key, {}).get("weighted", 0.0) for key in keys]
        centre["typical_weighted"] = median(series)
        centre["typical_trivial_weighted"] = median(
            [centre["trivial_per_window"].get(key, 0.0) for key in keys]
        )
        centre["peak_weighted"] = max(series)
        centre["windows_present"] = sum(1 for value in series if value > 0)
        centre["windows_analysed"] = len(keys)
        centre["consistent"] = centre["windows_present"] == len(keys)
        centre["one_off"] = centre["windows_present"] <= 1 < len(keys)
        centre["weighted_per_invocation"] = (
            centre["total_weighted"] / centre["invocations"] if centre["invocations"] else None
        )
        centre["median_wall_clock_seconds"] = median(centre["wall_clocks"]) if centre["wall_clocks"] else None
        centre["weighted_per_turn"] = centre["total_weighted"] / centre["turns"] if centre["turns"] else 0.0
    ranked = sorted(centres.values(), key=lambda c: (-c["typical_weighted"], -c["total_weighted"], c["name"]))
    per_use = [c["weighted_per_invocation"] for c in ranked if c["weighted_per_invocation"]]
    reference = median(per_use)
    for centre in ranked:
        centre["cost_per_use_reference"] = reference
        centre["out_of_line"] = bool(
            centre["weighted_per_invocation"]
            and reference
            and centre["weighted_per_invocation"] > settings_map["cost_per_use_multiple"] * reference
        )
    return ranked


def _day_series(window_data):
    start = datetime.fromisoformat(window_data["window"]["start"]).date()
    by_date = {bucket["date"]: bucket for bucket in window_data.get("by_day") or []}
    series = []
    cumulative = 0.0
    for offset in range(7):
        date = (start + timedelta(days=offset)).isoformat()
        bucket = by_date.get(date) or {"weighted": 0.0, "turns": 0}
        cumulative += bucket["weighted"]
        series.append(
            {
                "date": date,
                "day": offset + 1,
                "weighted": bucket["weighted"],
                "turns": bucket["turns"],
                "cumulative": cumulative,
            }
        )
    return series


def _drivers(records, until_date, limit=3):
    lanes = {"repo": defaultdict(float), "agent": defaultdict(float), "skill": defaultdict(float)}
    for record in records:
        if record["ts"][:10] > until_date:
            continue
        if record.get("cwd"):
            lanes["repo"][Path(record["cwd"]).name] += record["weighted"]
        if record.get("attributionAgent"):
            lanes["agent"][record["attributionAgent"]] += record["weighted"]
        if record.get("attributionSkill"):
            lanes["skill"][record["attributionSkill"]] += record["weighted"]
    return {
        lane: sorted(values.items(), key=lambda item: -item[1])[:limit] for lane, values in lanes.items()
    }


def pacing(window_data, records, ceiling, settings_map):
    series = _day_series(window_data)
    total = window_data["totals"]["weighted"]
    front_days = settings_map["front_load_days"]
    front = series[front_days - 1]["cumulative"] if series else 0.0
    even_share = front_days / 7.0
    front_share = front / total if total else 0.0
    active = [day for day in series if day["weighted"] > 0]
    first_half = active[: (len(active) + 1) // 2]
    active_share = sum(day["weighted"] for day in first_half) / total if total else 0.0
    exhausted = None
    approached = None
    if ceiling:
        for day in series:
            if approached is None and day["cumulative"] >= settings_map["approach_fraction"] * ceiling:
                approached = day
            if exhausted is None and day["cumulative"] >= ceiling:
                exhausted = day
    landmark = exhausted or approached
    boundary = (landmark or series[-1])["date"] if series else None
    return {
        "active_days": len(active),
        "front_half_active_days": len(first_half),
        "front_load_active_share": active_share,
        "driver_scope": "up to " + boundary if landmark else "over the whole window",
        "key": window_data["window"]["key"],
        "start": window_data["window"]["start"],
        "total_weighted": total,
        "percent_of_ceiling": 100.0 * total / ceiling if ceiling else None,
        "days": series,
        "front_load_days": front_days,
        "front_load_share": front_share,
        "front_load_ratio": front_share / even_share if even_share else None,
        "front_loaded": bool(front_share > even_share * 1.3),
        "exhausted_on": exhausted["date"] if exhausted else None,
        "exhausted_on_day": exhausted["day"] if exhausted else None,
        "approached_on": approached["date"] if approached else None,
        "approached_on_day": approached["day"] if approached else None,
        "drivers": _drivers(records, boundary) if boundary else {},
        "driver_boundary": boundary,
    }


def current_pacing(window_data, records, ceiling, settings_map, now=None):
    if not window_data:
        return None
    now = now or datetime.now(timezone.utc)
    start = _parse_ts(window_data["window"]["start_utc"])
    end = _parse_ts(window_data["window"]["end_utc"])
    elapsed_days = max(0.0, min(7.0, (now - start).total_seconds() / 86400.0))
    total = window_data["totals"]["weighted"]
    rate = total / elapsed_days if elapsed_days > 0 else None
    projected = rate * 7.0 if rate else None
    remaining_days = max(0.0, (end - now).total_seconds() / 86400.0)
    exhaustion = None
    if rate and ceiling and rate > 0:
        days_to_ceiling = ceiling / rate
        if days_to_ceiling <= 7.0:
            exhaustion = (start + timedelta(days=days_to_ceiling)).astimezone(timezone.utc).isoformat()
    overshoot = projected - ceiling if (projected and ceiling) else None
    centres = _drivers(records, now.date().isoformat(), limit=4)
    return {
        "key": window_data["window"]["key"],
        "elapsed_days": elapsed_days,
        "remaining_days": remaining_days,
        "weighted": total,
        "burn_rate_per_day": rate,
        "projected_window_weighted": projected,
        "percent_of_ceiling_now": 100.0 * total / ceiling if ceiling else None,
        "projected_percent_of_ceiling": 100.0 * projected / ceiling if (projected and ceiling) else None,
        "projected_exhaustion": exhaustion,
        "exhausts_before_reset": bool(exhaustion),
        "overshoot_weighted": overshoot if (overshoot or 0) > 0 else None,
        "sustainable_rate_per_day": (
            (ceiling - total) / remaining_days if (ceiling and remaining_days > 0) else None
        ),
        "drivers": centres,
    }


def _storm_sessions(windows, agent):
    count = 0
    for data in windows:
        for finding in data.get("findings") or []:
            if finding["rule"] == "subagent_storm" and agent in (finding["evidence"].get("agents") or {}):
                count += 1
    return count


def _entry(centre, status, **extra):
    entry = {
        "component": centre["component"],
        "name": centre["name"],
        "plugin": centre["plugin"],
        "status": status,
        "turns": centre["turns"],
        "total_weighted": centre["total_weighted"],
        "typical_weighted": centre["typical_weighted"],
        "peak_weighted": centre["peak_weighted"],
        "windows_present": centre["windows_present"],
        "windows_analysed": centre["windows_analysed"],
        "invocations": centre["invocations"],
        "invocation_unit": centre["invocation_unit"],
        "weighted_per_invocation": centre["weighted_per_invocation"],
        "median_wall_clock_seconds": centre["median_wall_clock_seconds"],
        "quiet_invocations": centre["quiet_invocations"],
        "out_of_line": centre["out_of_line"],
        "per_window": centre["per_window"],
        "files": [],
        "weighted_saving": None,
        "performance_risk": None,
        "confidence": None,
        "quality_risk": None,
        "buys": None,
        "patch": None,
        "note": None,
        "setting_env": None,
        "setting_path": None,
        "setting_value": None,
        "setting_readable": None,
        "trivial_weighted": centre.get("typical_trivial_weighted"),
        "trivial_turns": centre.get("trivial_turns"),
    }
    entry.update(extra)
    return entry


def _builtin_entry(centre, window_data, config, settings_map, setting, buys):
    saving = (
        centre["typical_weighted"]
        * advice._expensive_model_share(window_data, config)
        * advice._downgrade_factor(window_data, config)
    )
    target = _model_family(config["thresholds"]["model_mismatch"]["downgrade_model"])
    current = (
        "currently `%s`" % setting["value"]
        if setting["value"]
        else "not set, so the built-in default applies"
        if setting["readable"]
        else "not readable, so its current value is unknown"
    )
    lever = (
        "model selection for a built-in type is not in a file. It comes from `env.%s` in %s (%s) and from "
        "the `model` argument on the Agent call that starts each run."
        % (SUBAGENT_MODEL_ENV, setting["path"], current)
    )
    if saving < settings_map["min_saving"] or centre["windows_present"] < settings_map["min_windows_for_proposal"]:
        return _entry(
            centre,
            NO_FILE,
            buys=buys,
            note="%s Its spend here did not clear the %s weighted floor across enough of the %d analysed "
            "windows, so no change is proposed."
            % (lever, "{:,.0f}".format(settings_map["min_saving"]), centre["windows_analysed"]),
            performance_risk="unknown",
            quality_risk="Not assessed: without a definition to read there is nothing to judge.",
            setting_env=SUBAGENT_MODEL_ENV,
            setting_path=setting["path"],
            setting_value=setting["value"],
            setting_readable=setting["readable"],
        )
    return _entry(
        centre,
        SETTING_PROPOSAL,
        buys=buys,
        weighted_saving=saving,
        performance_risk="high",
        confidence="medium",
        note="%s Set the env var to `%s` to move every built-in subagent at once, or pass `model` per call "
        "to move only the invocations you have judged can take it. Nothing here edits either." % (lever, target),
        quality_risk="the global default is the widest change this tool can name: it re-tiers every turn "
        "routed to this type, including the ones this data says nothing about. Whether that changes the "
        "answers is NOT measurable from this data.",
        setting_env=SUBAGENT_MODEL_ENV,
        setting_path=setting["path"],
        setting_value=setting["value"],
        setting_readable=setting["readable"],
    )


def _agent_entry(centre, roots, window_data, config, settings_map, windows, setting):
    name = centre["name"]
    matches = agentfiles.resolve_agent(name, roots)
    sessions = _storm_sessions(windows, name)
    buys = (
        "%d subagent turns, and %d session(s) in these windows ran it as a storm" % (centre["turns"], sessions)
        if sessions
        else "%d subagent turns" % centre["turns"]
    )

    if not matches:
        if name in set(settings_map["builtin_agents"]):
            return _builtin_entry(centre, window_data, config, settings_map, setting, buys)
        return _entry(
            centre,
            NO_FILE,
            buys=buys,
            note="no agent definition found in the searched locations, so nothing can be proposed. "
            "It may come from a plugin that is no longer installed, or from a project you were in.",
            performance_risk="unknown",
            quality_risk="Not assessed: without a definition to read there is nothing to judge.",
        )

    if len(matches) > 1:
        return _entry(
            centre,
            AMBIGUOUS,
            files=[str(p) for p in matches],
            buys=buys,
            note="the name resolves to %d files; which one ran is not recorded, so no edit is proposed."
            % len(matches),
            performance_risk="unknown",
            quality_risk="Not assessed: the definition that actually ran is unknown.",
        )

    path = matches[0]
    text = agentfiles._read(path)
    fields, _, _ = agentfiles.read_frontmatter(text)
    declared = fields.get("model")
    description = agentfiles.purpose(fields.get("description"))
    target = config["thresholds"]["model_mismatch"]["downgrade_model"]
    files = [str(path)]

    if declared and _model_family(declared) in set(settings_map["cheap_model_families"]):
        return _entry(
            centre,
            ALREADY_RIGHT_SIZED,
            files=files,
            buys=description or buys,
            note="the definition already asks for `model: %s`, so there is no tier left to give back. "
            "The cost above was still spent on it." % declared,
            performance_risk="none",
            quality_risk="No change proposed, so no quality risk.",
        )

    sonnet_class = set(advice._settings(config)["sonnet_class_agents"])
    if name not in sonnet_class:
        return _entry(
            centre,
            NOT_ASSESSABLE,
            files=files,
            buys=description or buys,
            note="not listed in advice.sonnet_class_agents in config.json, so whether its work survives a "
            "cheaper tier "
            "has not been judged. Cost shown without a saving on purpose.",
            performance_risk="unknown",
            quality_risk="Cannot be assessed from spend data alone.",
        )

    if centre["one_off"] and settings_map["min_windows_for_proposal"] > 1:
        return _entry(
            centre,
            ONE_OFF,
            files=files,
            buys=description or buys,
            note="ran in only 1 of the %d analysed windows, so it is a one-off, not a pattern, and it "
            "does not drive a config change." % centre["windows_analysed"],
            performance_risk="none",
            quality_risk="No change proposed, so no quality risk.",
        )

    if centre["windows_present"] < settings_map["min_windows_for_proposal"]:
        return _entry(
            centre,
            ONE_OFF,
            files=files,
            buys=description or buys,
            note="present in %d of %d windows, below the %d-window floor for a config change."
            % (centre["windows_present"], centre["windows_analysed"], settings_map["min_windows_for_proposal"]),
            performance_risk="none",
            quality_risk="No change proposed, so no quality risk.",
        )

    factor = advice._downgrade_factor(window_data, config)
    share = advice._expensive_model_share(window_data, config)
    saving = centre["typical_weighted"] * share * factor
    if saving < settings_map["min_saving"]:
        return _entry(
            centre,
            OBSERVATION,
            files=files,
            buys=description or buys,
            weighted_saving=saving,
            note="a downgrade would typically save less than the %s weighted floor, so it is reported, "
            "not proposed." % "{:,.0f}".format(settings_map["min_saving"]),
            performance_risk="none",
            quality_risk="No change proposed, so no quality risk.",
        )

    return _entry(
        centre,
        PROPOSAL,
        files=files,
        buys=description or buys,
        weighted_saving=saving,
        performance_risk="low",
        confidence="medium",
        quality_risk="you lose %s-level judgement on: %s. Whether that changes the answers is NOT "
        "measurable from this data."
        % (_model_family(declared) if declared else "the inherited model", description or "this agent's work"),
        note="set `model: %s` in the frontmatter. Same fan-out, cheaper workers."
        % _model_family(target),
        patch=agentfiles.unified_patch(path, text, _model_family(target), agentfiles.display_path(path, roots)),
    )


def _skill_entry(centre, roots):
    name = centre["name"]
    matches = agentfiles.resolve_skill(name, roots)
    if not matches:
        return _entry(
            centre,
            NO_FILE,
            note="no SKILL.md or command file found in the searched locations, so nothing can be proposed.",
            performance_risk="unknown",
            quality_risk="Not assessed: no file to read.",
        )
    status = AMBIGUOUS if len(matches) > 1 else NOT_ASSESSABLE
    fields = agentfiles.read_frontmatter(agentfiles._read(matches[0]))[0]
    description = fields.get("description") or ""
    body = len(agentfiles._read(matches[0]))
    if len(matches) > 1:
        note = "the name resolves to %d files; which one ran is not recorded." % len(matches)
    else:
        note = (
            "cost is the turns attributed to this skill, not the cost of loading it. Its file is %s bytes "
            "and its description is %d characters.%s"
            % (
                "{:,}".format(body),
                len(description),
                " That description is broad enough to pull the file in on turns that did not need it, "
                "which this data cannot measure - read it and judge." if len(description) > 200 else "",
            )
        )
    return _entry(
        centre,
        status,
        files=[str(p) for p in matches],
        buys=agentfiles.purpose(description),
        note=note,
        performance_risk="unknown",
        quality_risk="Cannot be assessed from spend data alone.",
        description_chars=len(description),
        file_bytes=body,
    )


def _rule_totals(windows, rule):
    series = [
        (data.get("findings_by_rule") or {}).get(rule, {"count": 0, "weighted_cost": 0.0}) for data in windows
    ]
    return {
        "count": sum(item["count"] for item in series),
        "typical_weighted": median([item["weighted_cost"] for item in series]),
        "total_weighted": sum(item["weighted_cost"] for item in series),
        "windows_present": sum(1 for item in series if item["count"]),
    }


def _reconciliation(windows, current, entries, proposals, setting_proposals, config, open_window=None, now=None):
    rule = "model_mismatch"
    thresholds = config["thresholds"][rule]
    analysed = _rule_totals(windows, rule)
    bucket = ((current or {}).get("findings_by_rule") or {}).get(rule) or {}
    unpatchable = [e for e in entries if e["status"] in (NO_FILE, AMBIGUOUS) and not e["setting_env"]]
    file_total = sum(e["weighted_saving"] for e in proposals)
    setting_total = sum(e["weighted_saving"] for e in setting_proposals)
    unpatchable_weighted = sum(e["typical_weighted"] for e in unpatchable)
    open_key = (open_window or {}).get("window", {}).get("key")
    analysed_open = [d["window"]["key"] for d in windows if not window_is_closed(d, now)]
    reasons = []
    if current:
        reasons.append(
            "window scope: the report covers the open window %s; tune analyses the %d closed window(s) "
            "before it, where the same rule found typically %s per window."
            % (current["window"]["key"], len(windows), _short(analysed["typical_weighted"]))
        )
    elif open_key and open_key in analysed_open:
        reasons.append(
            "window scope: the selected window %s is the still-open one the report also reads, so the "
            "figures here are partial; across the %d window(s) analysed the same rule found typically %s "
            "per window." % (open_key, len(windows), _short(analysed["typical_weighted"]))
        )
    elif open_key:
        reasons.append(
            "window scope: the open window %s was not loaded on this invocation, so the report's live "
            "figure is not available to compare; across the %d window(s) analysed the same rule found "
            "typically %s per window." % (open_key, len(windows), _short(analysed["typical_weighted"]))
        )
    else:
        reasons.append(
            "window scope: there is no open window here, so the report and tune cover the same %d closed "
            "window(s); the same rule found typically %s per window."
            % (len(windows), _short(analysed["typical_weighted"]))
        )
    reasons.append(
        "formula: the rule re-prices individual turns that produced under %d output tokens with at most %d "
        "tool call. A proposal here prices a whole component instead - its typical window cost x the share "
        "of the window that ran above %s x the price gap - and only where a file or a setting can carry "
        "the change."
        % (thresholds["max_output_tokens"], thresholds["max_tool_calls"], thresholds["downgrade_model"])
    )
    if unpatchable:
        reasons.append(
            "no patchable target: %d cost centre(s) worth typically %s per window resolve to no file and no "
            "setting this tool can name, so their share of the rule's figure cannot become a proposal."
            % (len(unpatchable), _short(unpatchable_weighted))
        )
    return {
        "rule": rule,
        "current_window": (current or {}).get("window", {}).get("key"),
        "current_window_weighted": bucket.get("weighted_cost"),
        "open_window": open_key,
        "open_window_loaded": bool(current),
        "open_window_selected": bool(open_key and open_key in analysed_open),
        "analysed_typical_weighted": analysed["typical_weighted"],
        "analysed_total_weighted": analysed["total_weighted"],
        "file_proposal_total": file_total,
        "setting_proposal_total": setting_total,
        "proposal_total": file_total + setting_total,
        "unpatchable_centres": len(unpatchable),
        "unpatchable_typical_weighted": unpatchable_weighted,
        "reasons": reasons,
    }


def build(
    windows,
    config,
    roots=None,
    records_by_window=None,
    round_trips=None,
    current=None,
    open_window=None,
    now=None,
    setting=None,
):
    settings_map = settings(config)
    settings_map["min_windows_for_proposal"] = min(settings_map["min_windows_for_proposal"], len(windows))
    now = now or datetime.now(timezone.utc)
    open_window = open_window if open_window is not None else current
    records_by_window = records_by_window or {}
    roots = roots if roots is not None else agentfiles.default_roots(project_dirs=project_dirs(windows))
    setting = setting if setting is not None else subagent_model_setting()
    latest = windows[-1]
    ceiling = (latest.get("ceiling") or {}).get("estimate")

    centres = collect_centres(windows, records_by_window, settings_map, config)
    entries = []
    for centre in centres:
        if centre["component"] == "agent":
            entries.append(_agent_entry(centre, roots, latest, config, settings_map, windows, setting))
        else:
            entries.append(_skill_entry(centre, roots))

    proposals = sorted(
        (e for e in entries if e["status"] == PROPOSAL), key=lambda e: (-e["weighted_saving"], e["name"])
    )
    setting_proposals = sorted(
        (e for e in entries if e["status"] == SETTING_PROPOSAL),
        key=lambda e: (-e["weighted_saving"], e["name"]),
    )
    proposed = (PROPOSAL, SETTING_PROPOSAL)
    reported = sorted(
        (
            e
            for e in entries
            if e["status"] not in proposed
            and max(e["typical_weighted"], e["peak_weighted"]) >= settings_map["min_cost"]
        ),
        key=lambda e: (-e["typical_weighted"], -e["peak_weighted"], e["name"]),
    )
    hidden = [
        e
        for e in entries
        if e["status"] not in proposed
        and max(e["typical_weighted"], e["peak_weighted"]) < settings_map["min_cost"]
    ]

    plugins = defaultdict(lambda: {"weighted": 0.0, "turns": 0, "centres": 0})
    for centre in centres:
        if centre["plugin"]:
            bucket = plugins[centre["plugin"]]
            bucket["weighted"] += centre["total_weighted"]
            bucket["turns"] += centre["turns"]
            bucket["centres"] += 1

    return {
        "generated_at": now.isoformat(),
        "windows": [
            {
                "key": data["window"]["key"],
                "start": data["window"]["start"],
                "end": data["window"]["end"],
                "weighted": data["totals"]["weighted"],
                "turns": data["totals"]["turns"],
                "closed": window_is_closed(data, now),
            }
            for data in windows
        ],
        "ceiling": ceiling,
        "ceiling_method": (latest.get("ceiling") or {}).get("method"),
        "ceiling_detail": {
            key: (latest.get("ceiling") or {}).get(key) for key in ("samples_used", "band_pct")
        },
        "pacing": [pacing(data, records_by_window.get(data["window"]["key"]) or [], ceiling, settings_map) for data in windows],
        "current": current_pacing(
            current, records_by_window.get((current or {}).get("window", {}).get("key")) or [], ceiling, settings_map, now
        )
        if current
        else None,
        "centres": centres,
        "plugins": sorted(
            ({"plugin": name, **values} for name, values in plugins.items()),
            key=lambda item: -item["weighted"],
        ),
        "round_trips": round_trips,
        "rule_round_trips": {
            "redundant_reads": _rule_totals(windows, "redundant_reads"),
            "loop_retry": _rule_totals(windows, "loop_retry"),
        },
        "saving_basis": {
            "downgrade_model": config["thresholds"]["model_mismatch"]["downgrade_model"],
            "expensive_model_share": advice._expensive_model_share(latest, config),
            "downgrade_factor": advice._downgrade_factor(latest, config),
        },
        "min_saving": settings_map["min_saving"],
        "min_cost": settings_map["min_cost"],
        "min_windows_for_proposal": settings_map["min_windows_for_proposal"],
        "single_window": len(windows) == 1,
        "proposals": proposals,
        "setting_proposals": setting_proposals,
        "subagent_model_setting": setting,
        "reconciliation": _reconciliation(
            windows, current, entries, proposals, setting_proposals, config, open_window=open_window, now=now
        ),
        "reported": reported,
        "hidden_below_min_cost": len(hidden),
        "roots": [str(root["path"]) for root in roots],
        "applies_changes": False,
    }


def _num(value):
    return "{:,.0f}".format(value) if value is not None else "n/a"


def _short(value):
    if value is None:
        return "n/a"
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= cut:
            return "%.1f%s" % (value / cut, suffix)
    return "%.0f" % value


def _duration(seconds):
    if not seconds:
        return "n/a"
    if seconds < 90:
        return "%.0fs" % seconds
    if seconds < 5400:
        return "%.0fm" % (seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)


def _percent(value):
    return "n/a" if value is None else "%.0f%%" % value


STATUS_LABEL = {
    PROPOSAL: "PROPOSAL",
    SETTING_PROPOSAL: "SETTING PROPOSAL",
    ALREADY_RIGHT_SIZED: "already right-sized",
    NO_FILE: "no file to edit",
    AMBIGUOUS: "ambiguous",
    NOT_ASSESSABLE: "no proposal",
    OBSERVATION: "below the saving floor",
    ONE_OFF: "one-off, not a pattern",
}


def _bar(fraction, width=24):
    filled = max(0, min(width, int(round(fraction * width))))
    return "#" * filled + "." * (width - filled)


def _render_pacing(result, lines):
    ceiling = result["ceiling"]
    lines.append("1. PACING AND EXHAUSTION")
    block = {"method": result["ceiling_method"], **(result.get("ceiling_detail") or {})}
    phrase = collect.ceiling_phrase(block)
    lines.append("   ceiling %s weighted - %s." % (_num(ceiling), phrase))
    for window in result["pacing"]:
        lines.append("")
        lines.append(
            "   %s  %s weighted  %s of the ceiling%s"
            % (
                window["key"],
                _short(window["total_weighted"]),
                _percent(window["percent_of_ceiling"]),
                "   FRONT-LOADED" if window["front_loaded"] else "",
            )
        )
        for day in window["days"]:
            fraction = (day["cumulative"] / ceiling) if ceiling else 0.0
            lines.append(
                "     d%d %s  %8s  cum %8s  %s %s"
                % (
                    day["day"],
                    day["date"],
                    _short(day["weighted"]),
                    _short(day["cumulative"]),
                    _bar(fraction),
                    _percent(100 * fraction) if ceiling else "",
                )
            )
        lines.append(
            "     first %d calendar days carried %s of the window (an even week would be %s, ratio %.2fx); "
            "on the %d day(s) with any spend, the first %d carried %s"
            % (
                window["front_load_days"],
                _percent(100 * window["front_load_share"]),
                _percent(100 * window["front_load_days"] / 7.0),
                window["front_load_ratio"] or 0.0,
                window["active_days"],
                window["front_half_active_days"],
                _percent(100 * window["front_load_active_share"]),
            )
        )
        if window["exhausted_on"]:
            lines.append(
                "     reached the ceiling on day %d (%s)"
                % (window["exhausted_on_day"], window["exhausted_on"])
            )
        elif window["approached_on"]:
            lines.append(
                "     came within 10%% of the ceiling on day %d (%s)"
                % (window["approached_on_day"], window["approached_on"])
            )
        else:
            lines.append("     never approached the ceiling")
        drivers = window.get("drivers") or {}
        for lane in ("repo", "agent", "skill"):
            items = drivers.get(lane) or []
            if items:
                lines.append(
                    "     %s, top %s: %s"
                    % (
                        window["driver_scope"],
                        lane,
                        ", ".join("%s %s" % (name, _short(value)) for name, value in items),
                    )
                )

    current = result.get("current")
    lines.append("")
    if not current:
        lines.append("   No open window to pace: every collected window has closed.")
        return
    lines.append("   CURRENT WINDOW %s" % current["key"])
    lines.append(
        "     %.1f days elapsed, %.1f left. %s weighted so far, %s of the ceiling."
        % (
            current["elapsed_days"],
            current["remaining_days"],
            _short(current["weighted"]),
            _percent(current["percent_of_ceiling_now"]),
        )
    )
    lines.append(
        "     burn rate %s/day. At this rate the window ends at %s, %s of the ceiling."
        % (
            _short(current["burn_rate_per_day"]),
            _short(current["projected_window_weighted"]),
            _percent(current["projected_percent_of_ceiling"]),
        )
    )
    if current["projected_exhaustion"]:
        lines.append(
            "     PROJECTED EXHAUSTION %s, before the reset. To land inside the window, %s weighted has to "
            "come off the remaining days - sustainable rate from now is %s/day against the current %s/day."
            % (
                current["projected_exhaustion"][:16].replace("T", " "),
                _short(current["overshoot_weighted"]),
                _short(current["sustainable_rate_per_day"]),
                _short(current["burn_rate_per_day"]),
            )
        )
    else:
        lines.append("     No exhaustion is projected before the reset at the current rate.")
    drivers = current.get("drivers") or {}
    for lane in ("repo", "agent", "skill"):
        items = drivers.get(lane) or []
        if items:
            lines.append(
                "     the spend so far sits in %s: %s"
                % (lane, ", ".join("%s %s" % (name, _short(value)) for name, value in items))
            )
    lines.append(
        "     Which of that to change is a judgement about what the work is worth. This tool cannot make "
        "it: it can see what the work cost and nothing about what it produced."
    )


def _render_centres(result, lines):
    lines.append("2. CONSISTENT COST CENTRES across %d window(s)" % len(result["windows"]))
    lines.append(
        "   ranked by TYPICAL (median) cost per window, not by peak, so one heavy week cannot promote a "
        "component that is usually cheap. 'seen' is how many of the analysed windows it ran in."
    )
    lines.append("")
    lines.append(
        "   %-34s %-6s %8s %8s %6s %11s %9s"
        % ("component", "kind", "typical", "peak", "seen", "per-run", "wall/run")
    )
    for centre in result["centres"][:18]:
        if centre["typical_weighted"] < result["min_cost"] and centre["total_weighted"] < result["min_cost"]:
            continue
        flags = []
        if centre["one_off"]:
            flags.append("one-off")
        if centre["out_of_line"]:
            flags.append("cost/run out of line")
        if centre["quiet_invocations"]:
            flags.append("%d quiet run(s)" % centre["quiet_invocations"])
        lines.append(
            "   %-34s %-6s %8s %8s %4d/%-1d %11s %9s %s"
            % (
                centre["name"][:34],
                centre["component"],
                _short(centre["typical_weighted"]),
                _short(centre["peak_weighted"]),
                centre["windows_present"],
                centre["windows_analysed"],
                _short(centre["weighted_per_invocation"]),
                _duration(centre["median_wall_clock_seconds"]),
                " ".join(flags),
            )
        )
    lines.append("")
    lines.append(
        "   per-run is per agent run for agents and per session for skills - a skill has no invocation id "
        "in this data, so its denominator is coarser and its per-run figure is not comparable to an "
        "agent's. wall/run is median wall-clock between the first and last turn of a run: it is the only "
        "speed number in this data, and it measures elapsed time, not work done."
    )
    lines.append(
        "   'quiet run' = a run of at most %d attributed turns: loaded, barely acted on. It is a proxy for "
        "a component being pulled in on turns that did not need it, not proof of one." % DEFAULTS["quiet_invocation_turns"]
    )
    if result["plugins"]:
        lines.append("")
        lines.append("   by plugin:")
        for plugin in result["plugins"][:6]:
            lines.append(
                "     %-24s %8s over %d turns in %d component(s)"
                % (plugin["plugin"], _short(plugin["weighted"]), plugin["turns"], plugin["centres"])
            )


def _render_round_trips(result, lines):
    lines.append("3. WASTED ROUND TRIPS")
    for window in result.get("round_trips") or []:
        coverage = window["result_coverage"]
        lines.append("")
        lines.append(
            "   %s  tool results recorded for %.1f%% of %d call(s) over %d turns"
            % (window["key"], 100 * coverage, window["total_calls"], window["records"])
        )
        if coverage < 0.9:
            lines.append(
                "     the figures below are a floor for this window, not a rate comparable to the others: "
                "the rest of the calls were stored before outcomes were captured."
            )
        for detector in window["detectors"]:
            if not detector["count"]:
                continue
            lines.append(
                "     %-46s %5d  %8s  %.2f%% of the window"
                % (
                    detector["label"][:46],
                    detector["count"],
                    _short(detector["weighted_cost"]),
                    100 * detector["weighted_cost"] / window["weighted"] if window["weighted"] else 0.0,
                )
            )
            for tool, count in (detector["evidence"].get("by_tool") or [])[:3]:
                lines.append("         %-28s %d" % (tool, count))
    rules = result["rule_round_trips"]
    lines.append("")
    lines.append("   from the collector's own rules, over the same windows:")
    lines.append(
        "     same input read again in a session      %5d finding(s), typically %8s per window"
        % (rules["redundant_reads"]["count"], _short(rules["redundant_reads"]["typical_weighted"]))
    )
    lines.append(
        "     identical calls back to back            %5d finding(s), typically %8s per window"
        % (rules["loop_retry"]["count"], _short(rules["loop_retry"]["typical_weighted"]))
    )
    lines.append("")
    lines.append(
        "   These are the only round-trip signals this data supports. A failed call is a measured event, "
        "not a judgement: some of them are how work legitimately proceeds - a probe that was expected to "
        "fail, a command that reported a real problem."
    )


def _render_entry(entry, indent="  "):
    lines = ["%s (%s)" % (entry["name"], entry["component"])]
    for path in entry["files"]:
        lines.append("%sfile: %s" % (indent, path))
    lines.append(
        "%stypically %s weighted per window, seen in %d of %d windows, %s turns over %d %s(s)."
        % (
            indent,
            _short(entry["typical_weighted"]),
            entry["windows_present"],
            entry["windows_analysed"],
            "{:,}".format(entry["turns"]),
            entry["invocations"],
            entry["invocation_unit"],
        )
    )
    if entry["buys"]:
        lines.append("%sWhat it buys: %s" % (indent, entry["buys"]))
    if entry["status"] in (PROPOSAL, SETTING_PROPOSAL):
        lines.append("%sProposal: %s" % (indent, entry["note"]))
        lines.append(
            "%sEst. saving %s per window     Quality risk (%s): %s"
            % (indent, _short(entry["weighted_saving"]), entry["performance_risk"], entry["quality_risk"])
        )
        if entry["status"] == SETTING_PROPOSAL:
            lines.append(
                "%sBlast radius: the global default moves all %s of this agent's typical window spend, "
                "while only %s of that is what the trivial-turn rule can identify as recoverable (%s such "
                "turn(s) in these windows) - a saving, not a slice of the spend."
                % (
                    indent,
                    _short(entry["typical_weighted"]),
                    _short(entry["trivial_weighted"]),
                    "{:,}".format(entry["trivial_turns"] or 0),
                )
            )
    else:
        lines.append("%s%s: %s" % (indent, STATUS_LABEL[entry["status"]], entry["note"]))
        if entry["quality_risk"]:
            lines.append("%sQuality risk: %s" % (indent, entry["quality_risk"]))
    if entry["patch"]:
        lines.append("")
        lines.extend(indent + line for line in entry["patch"].splitlines())
    return lines


def _render_setting_proposals(result, lines):
    setting = result["subagent_model_setting"]
    lines.append("5. SETTING-LEVEL PROPOSALS (%d)" % len(result["setting_proposals"]))
    lines.append(
        "   built-in agent types have no definition file. Their tier comes from `env.%s` in %s and from the "
        "`model` argument on each Agent call, so a proposal here names a setting instead of a patch."
        % (setting["env"], setting["path"])
    )
    lines.append(
        "   current value: %s"
        % (
            "`%s`" % setting["value"]
            if setting["value"]
            else "unset, so the built-in default applies"
            if setting["readable"]
            else "unreadable - the file is missing, or is not a JSON object"
        )
    )
    if not result["setting_proposals"]:
        lines.append("   none above the %s weighted floor." % _num(result["min_saving"]))
        return
    lines.append(
        "   every entry below points at the SAME global default, so setting it moves all of them at once. "
        "The `model` argument on an individual Agent call is the narrower lever."
    )
    for entry in result["setting_proposals"]:
        lines.append("")
        lines.extend(_render_entry(entry))


def _render_reconciliation(result, lines):
    data = result["reconciliation"]
    lines.append("6. RECONCILIATION WITH THE WEEKLY REPORT")
    if data["current_window_weighted"] is None:
        if data["open_window_selected"]:
            lines.append(
                "   the selected window %s is the open one the weekly report also reads, so its figures here "
                "are partial and no report-versus-tune gap is computed." % data["open_window"]
            )
        elif data["open_window"] and not data["open_window_loaded"]:
            lines.append(
                "   the open window %s was not loaded on this invocation, so the weekly report's %s figure "
                "is not compared here." % (data["open_window"], data["rule"])
            )
        elif not data["open_window"]:
            lines.append(
                "   there is no open window, so the weekly report and tune read the same windows and there "
                "is no gap to explain."
            )
        else:
            lines.append(
                "   the weekly report's %s lens found nothing in the open window %s, so there is no gap to "
                "explain." % (data["rule"], data["open_window"])
            )
    else:
        lines.append(
            "   the report's %s lens reads %s for %s. The proposals above total %s (%s file-level, %s "
            "setting-level). Both are correct on their own basis; the gap is not an error."
            % (
                data["rule"],
                _short(data["current_window_weighted"]),
                data["current_window"],
                _short(data["proposal_total"]),
                _short(data["file_proposal_total"]),
                _short(data["setting_proposal_total"]),
            )
        )
    for reason in data["reasons"]:
        lines.append("     %s" % reason)
    lines.append(
        "   Every figure here is an upper bound on its own basis and they overlap, so the totals above are "
        "sums of upper bounds rather than a number to bank. The distance between them is scope and formula, "
        "not one of them being wrong."
    )


def render(result):
    keys = ", ".join(window["key"] for window in result["windows"])
    lines = [
        "my-token-spend tune - %d window(s): %s" % (len(result["windows"]), keys),
        HONESTY,
        "",
    ]
    open_windows = [w["key"] for w in result["windows"] if not w["closed"]]
    if open_windows:
        lines.append(
            "%s is not finished, so its per-window figures are partial and understate a full week."
            % ", ".join(open_windows)
        )
        lines.append("")

    _render_pacing(result, lines)
    lines.append("")
    _render_centres(result, lines)
    lines.append("")
    _render_round_trips(result, lines)
    lines.append("")

    basis = result["saving_basis"]
    lines.append("4. FILE-LEVEL PROPOSALS (%d)" % len(result["proposals"]))
    lines.append(
        "   saving basis: the component's TYPICAL cost per window x %.0f%% of the window that ran above %s "
        "x the %.0f%% price gap. Upper bounds; they overlap rather than adding up. Nothing below %d of %d "
        "windows can produce a proposal."
        % (
            100 * basis["expensive_model_share"],
            basis["downgrade_model"],
            100 * basis["downgrade_factor"],
            result["min_windows_for_proposal"],
            len(result["windows"]),
        )
    )
    if result["single_window"]:
        lines.append(
            "   SINGLE WINDOW: a proposal below is fitted to one week and has not been shown to be a "
            "pattern. Re-run without --window, or with --windows 4, before acting on it."
        )
    if not result["proposals"]:
        lines.append("   none above the %s weighted floor." % _num(result["min_saving"]))
    for entry in result["proposals"]:
        lines.append("")
        lines.extend(_render_entry(entry))

    lines.append("")
    _render_setting_proposals(result, lines)
    lines.append("")
    _render_reconciliation(result, lines)

    lines.append("")
    lines.append("7. COST WITHOUT A PROPOSAL (%d)" % len(result["reported"]))
    for entry in result["reported"]:
        lines.append("")
        lines.extend(_render_entry(entry))

    lines.extend(
        [
            "",
            "%d further component(s) typically cost less than the %s weighted floor and are not listed."
            % (result["hidden_below_min_cost"], _num(result["min_cost"])),
            "",
            "LIMITS",
            "  Visibility: tune sees only the agents and skills that actually ran in the analysed windows. "
            "A rarely-invoked but expensive component does not surface here.",
            "  Cost is not waste: every figure above is what a component cost, not what it wasted. An "
            "expensive component may be exactly what is keeping the work correct.",
            "  " + HONESTY,
            "  Nothing was modified. tune only proposes; read any patch or setting above and apply it "
            "yourself. It reads settings.json to report the current value and never writes it.",
        ]
    )
    return "\n".join(lines) + "\n"
