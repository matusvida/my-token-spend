import hashlib
import json
import math
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as time_of_day
from pathlib import Path
from zoneinfo import ZoneInfo

import context
import rules

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

RESET_SIGNAL_HINTS = (
    "weekly limit",
    "usage limit",
    "rate limit",
    "limit reached",
    "limit resets",
    "limit will reset",
    "reached your limit",
    "out of usage",
)

_DAY = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"

RESET_WEEKDAY_PATTERNS = [
    r"resets?\s+(?:on\s+)?(?:next\s+)?" + _DAY,
    r"reset(?:s|ting)?\s+at\s+[^.,;]{0,40}?\bon\s+" + _DAY,
    r"will\s+reset\s+(?:on\s+)?(?:next\s+)?" + _DAY,
    r"available\s+again\s+(?:on\s+)?(?:next\s+)?" + _DAY,
    r"try\s+again\s+(?:on\s+)?(?:next\s+)?" + _DAY,
    r"resumes?\s+(?:on\s+)?(?:next\s+)?" + _DAY,
    _DAY + r"\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|:00)?",
]


def reset_weekday_candidates(text):
    lowered = (text or "").lower()
    if not any(hint in lowered for hint in RESET_SIGNAL_HINTS):
        return set()
    found = set()
    for pattern in RESET_WEEKDAY_PATTERNS:
        for match in re.finditer(pattern, lowered):
            found.add(match.group(1).capitalize())
    return found


def resolve_reset_weekday(candidates):
    unique = {c for c in candidates if c in WEEKDAYS}
    return unique.pop() if len(unique) == 1 else None


def _entry_text(entry):
    parts = []
    for source in (entry.get("content"), entry.get("summary"), (entry.get("message") or {}).get("content")):
        if isinstance(source, str):
            parts.append(source)
        elif isinstance(source, list):
            parts.extend(b.get("text") or "" for b in source if isinstance(b, dict) and b.get("type") == "text")
    return "\n".join(p for p in parts if p)


STANDARD_OFFSET = timedelta(seconds=-time.timezone)
DAYLIGHT_OFFSET = timedelta(seconds=-time.altzone) if time.daylight else STANDARD_OFFSET


class SystemZone(tzinfo):
    def utcoffset(self, dt):
        return DAYLIGHT_OFFSET if self._is_dst(dt) else STANDARD_OFFSET

    def dst(self, dt):
        return DAYLIGHT_OFFSET - STANDARD_OFFSET if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return time.tzname[1 if self._is_dst(dt) else 0]

    def _is_dst(self, dt):
        if dt is None:
            return False
        stamp = time.mktime(
            (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.weekday(), 1, -1)
        )
        return time.localtime(stamp).tm_isdst > 0


def zone(config):
    name = config.get("timezone")
    return ZoneInfo(name) if name else SystemZone()


def zone_label(config):
    name = config.get("timezone")
    return name or ("local time (%s)" % datetime.now(zone(config)).tzname())


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def window_start(dt_utc, config):
    local = dt_utc.astimezone(zone(config))
    shifted = local - timedelta(hours=config["reset_hour"])
    days_back = (shifted.weekday() - WEEKDAYS.index(config["reset_weekday"])) % 7
    return (shifted - timedelta(days=days_back)).date()


def window_key(start_date):
    return "week_" + start_date.strftime("%Y_%m_%d")


def bucket_records(records, config):
    buckets = defaultdict(list)
    for record in records:
        buckets[window_start(parse_ts(record["ts"]), config).isoformat()].append(record)
    return dict(buckets)


def _tool_hash(name, tool_input):
    payload = json.dumps({"name": name, "input": tool_input}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


AGENT_TOOL = "Agent"


def agent_calls(entry, config):
    if entry.get("type") != "assistant":
        return []
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    limit = config["prompt_label_chars"]
    calls = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") != AGENT_TOOL or not block.get("id"):
            continue
        payload = block.get("input") if isinstance(block.get("input"), dict) else {}
        prompt = payload.get("prompt") or ""
        calls.append(
            {
                "tool_use_id": block["id"],
                "ts": parse_ts(entry["timestamp"]).isoformat(),
                "sessionId": entry.get("sessionId"),
                "parent_uuid": entry.get("uuid"),
                "description": payload.get("description"),
                "subagent_type": payload.get("subagent_type"),
                "model": payload.get("model"),
                "prompt_chars": len(prompt),
                "prompt_head": prompt[:limit] or None,
            }
        )
    return calls


def _optional_int(value):
    return None if value is None else int(value)


def normalize(entry, config):
    if entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None

    usage = message["usage"]
    model = message.get("model")
    weights = config["token_class_weights"]
    model_weight, weight_source = rules.model_weight(model, config)

    counts = {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_create": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
    }
    weighted = model_weight * sum(weights[cls] * value for cls, value in counts.items())
    split = usage.get("cache_creation")
    split = split if isinstance(split, dict) else {}

    tools = []
    text_chars = 0
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tools.append(
                    {
                        "name": block.get("name"),
                        "hash": _tool_hash(block.get("name"), block.get("input")),
                        "tool_use_id": block.get("id"),
                    }
                )
            elif block.get("type") == "text":
                text_chars += len(block.get("text") or "")

    return {
        "ts": parse_ts(entry["timestamp"]).isoformat(),
        "uuid": entry.get("uuid"),
        "sessionId": entry.get("sessionId"),
        "model": model,
        "model_known": weight_source != rules.DEFAULT,
        "effort": entry.get("effort"),
        "isSidechain": bool(entry.get("isSidechain")),
        "agentId": entry.get("agentId"),
        "attributionAgent": entry.get("attributionAgent"),
        "attributionSkill": entry.get("attributionSkill"),
        "mcp_server": entry.get("attributionMcpServer"),
        "mcp_tool": entry.get("attributionMcpTool"),
        "plugin": entry.get("attributionPlugin"),
        "per_turn_effort": entry.get("perTurnEffort"),
        "stop_reason": message.get("stop_reason"),
        "cache_create_5m": _optional_int(split.get("ephemeral_5m_input_tokens")),
        "cache_create_1h": _optional_int(split.get("ephemeral_1h_input_tokens")),
        "compacted": message.get("context_management") is not None,
        "cwd": entry.get("cwd"),
        "gitBranch": entry.get("gitBranch"),
        "version": entry.get("version"),
        "thinking": int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
        "weighted": weighted,
        "tools": tools,
        "text_chars": text_chars,
        "is_api_error": bool(entry.get("isApiErrorMessage")),
        **counts,
    }


def _prompt_text(entry, limit):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
    else:
        return None
    text = text.strip()
    return text[:limit] if text else None


DENIED_MARKERS = ("want to proceed with this tool use", "tool use was rejected")


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text") or ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _tool_outcome(block):
    text = _result_text(block.get("content"))
    is_error = bool(block.get("is_error"))
    return {
        "result_chars": len(text),
        "is_error": is_error,
        "denied": is_error and any(marker in text for marker in DENIED_MARKERS),
    }


def _tool_results(entry):
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
            yield block["tool_use_id"], _tool_outcome(block)


def read_file(path, config, stored):
    stat = path.stat()
    size, mtime = stat.st_size, stat.st_mtime

    if stored and size == stored["size"] and mtime == stored["mtime"]:
        return {
            "records": [],
            "state": dict(stored),
            "malformed": 0,
            "reset_candidates": [],
            "pending_results": {},
            "agent_calls": [],
        }

    resume = bool(stored) and size > stored["size"]
    offset = stored["offset"] if resume else 0
    last_prompt = stored.get("last_prompt") if resume else None
    carried_malformed = stored.get("malformed", 0) if resume else 0
    source_tool_use_id = stored.get("source_tool_use_id") if resume else None
    after_compaction = bool(stored.get("pending_compaction")) if resume else False

    with path.open("rb") as handle:
        handle.seek(offset)
        blob = handle.read()

    consumed = offset
    chunks = blob.split(b"\n")
    if blob.endswith(b"\n"):
        chunks.pop()
    else:
        chunks.pop()

    records = []
    malformed = 0
    reset_candidates = set()
    awaiting = {}
    pending_results = {}
    calls = []
    limit = config["prompt_label_chars"]
    for chunk in chunks:
        consumed += len(chunk) + 1
        line = chunk.strip()
        if not line:
            continue
        text = line.decode("utf-8", "replace")
        try:
            entry = json.loads(text)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(entry, dict):
            malformed += 1
            continue
        if "limit" in text.lower():
            reset_candidates |= reset_weekday_candidates(_entry_text(entry))
        if entry.get("type") == "user":
            prompt = _prompt_text(entry, limit)
            if prompt:
                last_prompt = prompt
            if source_tool_use_id is None and entry.get("sourceToolUseID"):
                source_tool_use_id = entry["sourceToolUseID"]
            if entry.get("isCompactSummary"):
                after_compaction = True
            for call_id, outcome in _tool_results(entry):
                tool = awaiting.pop(call_id, None)
                if tool is None:
                    pending_results[call_id] = outcome
                else:
                    tool.update(outcome)
            continue
        calls.extend(agent_calls(entry, config))
        record = normalize(entry, config)
        if record is not None:
            record["prompt"] = last_prompt
            record["after_compaction"] = after_compaction
            after_compaction = False
            for tool in record["tools"]:
                tool.update({"result_chars": None, "is_error": None, "denied": None})
                if tool["tool_use_id"]:
                    awaiting[tool["tool_use_id"]] = tool
            records.append(record)

    for record in records:
        record["source_tool_use_id"] = source_tool_use_id

    return {
        "records": records,
        "state": {
            "offset": consumed,
            "size": size,
            "mtime": mtime,
            "last_prompt": last_prompt,
            "malformed": carried_malformed + malformed,
            "source_tool_use_id": source_tool_use_id,
            "pending_compaction": after_compaction,
        },
        "malformed": malformed,
        "reset_candidates": sorted(reset_candidates),
        "pending_results": pending_results,
        "agent_calls": calls,
    }


def estimate_ceiling(window_totals, config):
    settings = config["ceiling"]
    if settings["override"] is not None:
        return {
            "estimate": float(settings["override"]),
            "method": "override",
            "approximate": False,
            "cluster_size": 0,
            "windows_considered": len(window_totals),
        }
    if len(window_totals) < settings["min_windows"]:
        return {
            "estimate": None,
            "method": "insufficient-data",
            "approximate": True,
            "cluster_size": 0,
            "windows_considered": len(window_totals),
        }
    ranked = sorted(window_totals.values(), reverse=True)
    size = max(1, math.ceil(len(ranked) * settings["top_cluster_fraction"]))
    cluster = ranked[:size]
    return {
        "estimate": (sum(cluster) / len(cluster)) * settings["headroom"],
        "method": "top-cluster",
        "approximate": True,
        "cluster_size": size,
        "windows_considered": len(window_totals),
    }


TOKEN_FIELDS = ("input", "output", "thinking", "cache_create", "cache_read")


def _empty_bucket():
    return {"turns": 0, "weighted": 0.0, **{field: 0 for field in TOKEN_FIELDS}}


def _accumulate(bucket, record):
    bucket["turns"] += 1
    bucket["weighted"] += record["weighted"]
    for field in TOKEN_FIELDS:
        bucket[field] += record[field]


def _breakdown(records, key_of):
    buckets = {}
    for record in records:
        key = key_of(record)
        if key is None:
            continue
        _accumulate(buckets.setdefault(key, _empty_bucket()), record)
    return [
        {"key": key, **bucket}
        for key, bucket in sorted(buckets.items(), key=lambda kv: (-kv[1]["weighted"], kv[0]))
    ]


def _session_breakdown(records):
    sessions = {}
    for record in records:
        entry = sessions.get(record["sessionId"])
        if entry is None:
            entry = sessions[record["sessionId"]] = {
                **_empty_bucket(),
                "sidechain_turns": 0,
                "sidechain_weighted": 0.0,
                "models": set(),
                "agents": set(),
                "first_ts": record["ts"],
                "last_ts": record["ts"],
                "cwd": record["cwd"],
                "gitBranch": record["gitBranch"],
                "first_prompt": record["prompt"],
            }
        _accumulate(entry, record)
        entry["models"].add(record["model"])
        if record["attributionAgent"]:
            entry["agents"].add(record["attributionAgent"])
        if record["isSidechain"]:
            entry["sidechain_turns"] += 1
            entry["sidechain_weighted"] += record["weighted"]
        if record["ts"] < entry["first_ts"]:
            entry["first_ts"] = record["ts"]
            entry["first_prompt"] = record["prompt"]
        if record["ts"] >= entry["last_ts"]:
            entry["last_ts"] = record["ts"]
            entry["cwd"] = record["cwd"]
            entry["gitBranch"] = record["gitBranch"]

    for entry in sessions.values():
        entry["models"] = sorted(entry["models"])
        entry["agents"] = sorted(entry["agents"])
    return [
        {"key": key, **entry}
        for key, entry in sorted(sessions.items(), key=lambda kv: (-kv[1]["weighted"], kv[0]))
    ]


def _day_breakdown(records, tz):
    buckets = {}
    for record in records:
        key = parse_ts(record["ts"]).astimezone(tz).date().isoformat()
        _accumulate(buckets.setdefault(key, _empty_bucket()), record)
    return [{"date": key, **buckets[key]} for key in sorted(buckets)]


COVERAGE_FIELDS = (
    "mcp_server",
    "mcp_tool",
    "plugin",
    "per_turn_effort",
    "stop_reason",
    "cache_create_5m",
    "cache_create_1h",
    "compacted",
    "after_compaction",
    "source_tool_use_id",
)


def _coverage_entry(present, total):
    return {"present": present, "total": total, "share": (present / total) if total else 0.0}


def field_coverage(records):
    covered = {
        field: _coverage_entry(sum(1 for r in records if r.get(field) is not None), len(records))
        for field in COVERAGE_FIELDS
    }
    calls = [tool for record in records for tool in record.get("tools") or []]
    covered["tool_results"] = _coverage_entry(
        sum(1 for tool in calls if tool.get("result_chars") is not None), len(calls)
    )
    return covered


def aggregate_window(start_date, records, config, parse_stats, ceiling):
    tz = zone(config)
    start_local = datetime.combine(start_date, time_of_day(hour=config["reset_hour"]), tzinfo=tz)
    end_local = start_local + timedelta(days=7)
    now = datetime.now(timezone.utc)
    elapsed_seconds = max(0.0, min((now - start_local).total_seconds(), 7 * 86400.0))
    elapsed_days = elapsed_seconds / 86400.0

    totals = _empty_bucket()
    totals.update({"sessions": 0, "sidechain_turns": 0, "sidechain_weighted": 0.0})
    for record in records:
        _accumulate(totals, record)
        if record["isSidechain"]:
            totals["sidechain_turns"] += 1
            totals["sidechain_weighted"] += record["weighted"]
    totals["sessions"] = len({r["sessionId"] for r in records})

    findings = rules.evaluate(records, config)
    by_rule = {}
    for finding in findings:
        bucket = by_rule.setdefault(finding["rule"], {"count": 0, "weighted_cost": 0.0})
        bucket["count"] += 1
        bucket["weighted_cost"] += finding["weighted_cost"]

    return {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "window": {
            "key": window_key(start_date),
            "start": start_date.isoformat(),
            "end": end_local.date().isoformat(),
            "start_utc": start_local.astimezone(timezone.utc).isoformat(),
            "end_utc": end_local.astimezone(timezone.utc).isoformat(),
            "timezone": zone_label(config),
            "reset_weekday": config["reset_weekday"],
            "reset_hour": config["reset_hour"],
            "is_current": start_local <= now < end_local,
            "elapsed_days": round(elapsed_days, 4),
            "elapsed_fraction": round(elapsed_days / 7.0, 4),
        },
        "weights": {
            "token_class_weights": config["token_class_weights"],
            "model_weights": config["model_weights"],
            "default_model_weight": config["default_model_weight"],
        },
        "totals": totals,
        "by_day": _day_breakdown(records, tz),
        "by_model": _breakdown(records, lambda r: r["model"]),
        "by_effort": _breakdown(records, lambda r: r["effort"]),
        "by_repo": _breakdown(records, lambda r: r["cwd"]),
        "by_branch": _breakdown(records, lambda r: r["gitBranch"]),
        "by_agent": _breakdown(records, lambda r: r["attributionAgent"]),
        "by_skill": _breakdown(records, lambda r: r["attributionSkill"]),
        "by_mcp_server": _breakdown(records, lambda r: r.get("mcp_server")),
        "by_plugin": _breakdown(records, lambda r: r.get("plugin")),
        "by_session": _session_breakdown(records),
        "unknown_models": _breakdown(unpriced_records(records, config), lambda r: r["model"]),
        "field_coverage": field_coverage(records),
        "context": context.window_block(records, config),
        "findings": findings,
        "findings_by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1]["weighted_cost"])),
        "ceiling": ceiling_block(ceiling, totals["weighted"], elapsed_days, now, end_local),
        "parse": dict(parse_stats, records=len(records)),
    }


def ceiling_block(ceiling, weighted, elapsed_days, now, end_local):
    block = dict(ceiling or {"estimate": None, "method": "unknown", "approximate": True})
    burn_rate = weighted / elapsed_days if elapsed_days > 0 else None
    remaining_seconds = max(0.0, (end_local - now).total_seconds())
    remaining_days = remaining_seconds / 86400.0
    estimate = block.get("estimate")
    remaining_budget = max(0.0, estimate - weighted) if estimate else None

    block["burn_rate_per_day"] = round(burn_rate, 2) if burn_rate is not None else None
    block["percent_used"] = round(100.0 * weighted / estimate, 4) if estimate else None
    block["remaining_weighted"] = round(remaining_budget, 2) if remaining_budget is not None else None
    block["remaining_days"] = round(remaining_days, 4)
    block["remaining_hours"] = round(remaining_seconds / 3600.0, 2)

    if remaining_budget is None:
        block["sustainable_rate_basis"] = "no-ceiling"
        block["sustainable_rate_per_day"] = None
    elif remaining_days <= 0:
        block["sustainable_rate_basis"] = "window-closed"
        block["sustainable_rate_per_day"] = None
    elif remaining_days < 1.0:
        block["sustainable_rate_basis"] = "final-day"
        block["sustainable_rate_per_day"] = None
    else:
        block["sustainable_rate_basis"] = "per-day"
        block["sustainable_rate_per_day"] = round(remaining_budget / remaining_days, 2)

    if remaining_budget is not None and remaining_days > 0 and burn_rate:
        exhaustion = now + timedelta(days=remaining_budget / burn_rate)
        before_reset = exhaustion < end_local
        block["projected_exhaustion"] = exhaustion.isoformat() if before_reset else None
        block["exhausts_before_reset"] = before_reset
    else:
        block["projected_exhaustion"] = None
        block["exhausts_before_reset"] = None
    return block


class CollectionError(Exception):
    pass


def unpriced_records(records, config):
    return [r for r in records if rules.model_weight(r["model"], config)[1] == rules.DEFAULT]


def unknown_model_report(windows, config):
    seen = defaultdict(lambda: {"turns": 0, "weighted": 0.0})
    for records in windows.values():
        for record in unpriced_records(records, config):
            seen[record["model"]]["turns"] += 1
            seen[record["model"]]["weighted"] += record["weighted"]
    return [
        {"model": model, "turns": b["turns"], "weighted": b["weighted"], "weight": config["default_model_weight"]}
        for model, b in sorted(seen.items(), key=lambda kv: -kv[1]["weighted"])
    ]


def pricing_drift(windows, config):
    drift = []
    for start, records in sorted(windows.items()):
        stale = sum(1 for r in records if abs(rules.weighted_cost(r, config) - r["weighted"]) > 1e-6)
        if stale:
            drift.append({"window": window_key(date.fromisoformat(start)), "records": stale})
    return drift


def _load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=False, default=str), encoding="utf-8")


def _load_store(path):
    records = {}
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        records[_record_key(record)] = record
    return records


def _record_key(record):
    return record["uuid"] or "%s|%s" % (record["sessionId"], record["ts"])


def _write_store(path, records):
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def load_records(store_dir, window_key):
    return list(_load_store(Path(store_dir) / (window_key + ".jsonl")).values())


def _load_stores(store_dir):
    stores = {}
    for existing in sorted(store_dir.glob("week_*.jsonl")):
        stores[existing.stem.replace("week_", "").replace("_", "-")] = _load_store(existing)
    return stores


def _window_is_closed(start_date, config, now):
    start_local = datetime.combine(start_date, time_of_day(hour=config["reset_hour"]), tzinfo=zone(config))
    return now >= start_local + timedelta(days=7)


def _losses(before, after, config):
    now = datetime.now(timezone.utc)
    losses = []
    for start, prior in sorted(before.items()):
        if not prior or not _window_is_closed(date.fromisoformat(start), config, now):
            continue
        current = after.get(start) or {}
        prior_weighted = sum(r["weighted"] for r in prior.values())
        current_weighted = sum(r["weighted"] for r in current.values())
        dropped = [key for key in prior if key not in current]
        if not dropped and current_weighted >= prior_weighted - 1e-6:
            continue
        losses.append(
            {
                "window": window_key(date.fromisoformat(start)),
                "turns_before": len(prior),
                "turns_after": len(current),
                "weighted_before": prior_weighted,
                "weighted_after": current_weighted,
                "dropped_records": len(dropped),
            }
        )
    return losses


def describe_losses(losses):
    lines = ["refusing to shrink history: %d closed window(s) would lose data" % len(losses)]
    for loss in losses:
        lines.append(
            "  %s: %d -> %d turns, %s -> %s weighted, %d record(s) dropped"
            % (
                loss["window"],
                loss["turns_before"],
                loss["turns_after"],
                _num(loss["weighted_before"]),
                _num(loss["weighted_after"]),
                loss["dropped_records"],
            )
        )
    return "\n".join(lines)


def _prune_window(store_dir, data_dir, reports_dir, key):
    for path in (
        store_dir / (key + ".jsonl"),
        data_dir / (key + ".json"),
        reports_dir / (key + ".md"),
        reports_dir / (key + ".html"),
    ):
        if path.exists():
            path.unlink()


def _finalize(stores, config, store_dir, data_dir, reports_dir, parse_stats, window):
    windows = {
        start: sorted(records.values(), key=lambda r: (r["ts"], r["uuid"] or ""))
        for start, records in sorted(stores.items())
    }
    live = {window_key(date.fromisoformat(start)) for start, records in windows.items() if records}
    for stale in sorted(store_dir.glob("week_*.jsonl")):
        if stale.stem not in live:
            _prune_window(store_dir, data_dir, reports_dir, stale.stem)
    for start, records in windows.items():
        if records:
            _write_store(store_dir / (window_key(date.fromisoformat(start)) + ".jsonl"), records)

    ceiling = estimate_ceiling(
        {start: sum(r["weighted"] for r in records) for start, records in windows.items()}, config
    )
    written = []
    for start, records in windows.items():
        if (window and start != window) or not records:
            continue
        aggregate = aggregate_window(date.fromisoformat(start), records, config, parse_stats, ceiling)
        key = aggregate["window"]["key"]
        _write_json(data_dir / (key + ".json"), aggregate)
        (reports_dir / (key + ".md")).write_text(render_markdown(aggregate), encoding="utf-8")
        written.append(aggregate)
    notices = {
        "unknown_models": unknown_model_report(windows, config),
        "pricing_drift": pricing_drift(windows, config),
    }
    return windows, ceiling, written, notices


def _prepare_dirs(out_dir):
    data_dir, reports_dir = out_dir / "data", out_dir / "reports"
    store_dir = data_dir / "records"
    for directory in (data_dir, reports_dir, store_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return data_dir, reports_dir, store_dir


def recut(config, out_dir, state_path, window=None):
    out_dir, state_path = Path(out_dir), Path(state_path)
    data_dir, reports_dir, store_dir = _prepare_dirs(out_dir)

    known = {}
    for bucket in _load_stores(store_dir).values():
        known.update(bucket)
    if not known:
        raise CollectionError("the record store under %s is empty; run collect first" % store_dir)

    stores = {}
    for key, record in known.items():
        stores.setdefault(window_start(parse_ts(record["ts"]), config).isoformat(), {})[key] = record
    rebucketed = sum(len(bucket) for bucket in stores.values())
    if rebucketed != len(known):
        raise CollectionError("re-cut would change the record count from %d to %d" % (len(known), rebucketed))

    state = _load_json(state_path, {"files": {}})
    tracked = state.get("files") or {}
    parse_stats = {
        "files_scanned": len(tracked),
        "malformed_lines": sum(entry.get("malformed", 0) for entry in tracked.values()),
    }
    windows, ceiling, written, notices = _finalize(stores, config, store_dir, data_dir, reports_dir, parse_stats, window)
    return {
        "windows": written,
        "new_records": 0,
        "total_records": sum(len(r) for r in windows.values()),
        "files_scanned": parse_stats["files_scanned"],
        "malformed_lines": parse_stats["malformed_lines"],
        "ceiling": ceiling,
        "reset": state.get("reset") or {},
        **notices,
    }


def reprice(config, out_dir, state_path):
    out_dir, state_path = Path(out_dir), Path(state_path)
    data_dir, reports_dir, store_dir = _prepare_dirs(out_dir)

    before = _load_stores(store_dir)
    if not any(before.values()):
        raise CollectionError("the record store under %s is empty; run collect first" % store_dir)

    stores = {}
    for start, bucket in before.items():
        stores[start] = {
            key: dict(
                record,
                weighted=rules.weighted_cost(record, config),
                model_known=rules.model_weight(record["model"], config)[1] != rules.DEFAULT,
            )
            for key, record in bucket.items()
        }

    dropped = [loss for loss in _losses(before, stores, config) if loss["dropped_records"]]
    if dropped:
        raise CollectionError(
            "%s\nre-pricing must never lose a record; the store under %s was left untouched."
            % (describe_losses(dropped), store_dir)
        )

    state = _load_json(state_path, {"files": {}})
    tracked = state.get("files") or {}
    parse_stats = {
        "files_scanned": len(tracked),
        "malformed_lines": sum(entry.get("malformed", 0) for entry in tracked.values()),
    }
    windows, ceiling, written, notices = _finalize(
        stores, config, store_dir, data_dir, reports_dir, parse_stats, None
    )
    return {
        "windows": written,
        "new_records": 0,
        "total_records": sum(len(r) for r in windows.values()),
        "files_scanned": parse_stats["files_scanned"],
        "malformed_lines": parse_stats["malformed_lines"],
        "ceiling": ceiling,
        "reset": state.get("reset") or {},
        "repriced": [
            {
                "window": window_key(date.fromisoformat(start)),
                "turns": len(bucket),
                "weighted_before": sum(r["weighted"] for r in before[start].values()),
                "weighted_after": sum(r["weighted"] for r in bucket.values()),
            }
            for start, bucket in sorted(stores.items())
        ],
        **notices,
    }


AGENT_CALL_PREFIX = "agent_calls_"


def load_agent_calls(store_dir):
    known = {}
    for path in sorted(Path(store_dir).glob(AGENT_CALL_PREFIX + "week_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                call = json.loads(line)
            except ValueError:
                continue
            if call.get("tool_use_id"):
                known[call["tool_use_id"]] = call
    return known


def merge_agent_calls(store_dir, fresh, config, discard_stored=False):
    known = {} if discard_stored else load_agent_calls(store_dir)
    for call in fresh:
        known[call["tool_use_id"]] = call
    if not known:
        return known
    buckets = defaultdict(list)
    for call in known.values():
        buckets[window_start(parse_ts(call["ts"]), config)].append(call)
    written = set()
    for start, calls in buckets.items():
        calls.sort(key=lambda call: (call["ts"], call["tool_use_id"]))
        path = Path(store_dir) / (AGENT_CALL_PREFIX + window_key(start) + ".jsonl")
        path.write_text(
            "".join(json.dumps(call, sort_keys=True) + "\n" for call in calls), encoding="utf-8"
        )
        written.add(path)
    for stale in sorted(Path(store_dir).glob(AGENT_CALL_PREFIX + "week_*.jsonl")):
        if stale not in written:
            stale.unlink()
    return known


def _fill_tool_results(stores, pending_results):
    if not pending_results:
        return
    for bucket in stores.values():
        for record in bucket.values():
            for tool in record.get("tools") or []:
                if tool.get("result_chars") is not None:
                    continue
                outcome = pending_results.get(tool.get("tool_use_id"))
                if outcome:
                    tool.update(outcome)


def run(config, root, out_dir, state_path, backfill=False, window=None, rebuild_from_transcripts_only=False):
    root, out_dir, state_path = Path(root), Path(out_dir), Path(state_path)
    data_dir, reports_dir, store_dir = _prepare_dirs(out_dir)
    backfill = backfill or rebuild_from_transcripts_only

    state = {"files": {}} if backfill else _load_json(state_path, {"files": {}})
    reset_state = state.get("reset") if isinstance(state.get("reset"), dict) else {}
    reset_candidates = set() if backfill else set(reset_state.get("candidates") or [])

    files = sorted(p for p in root.rglob("*.jsonl") if p.is_file())
    fresh = []
    fresh_calls = []
    pending_results = {}
    for path in files:
        key = str(path.resolve())
        result = read_file(path, config, state["files"].get(key))
        state["files"][key] = result["state"]
        fresh.extend(result["records"])
        reset_candidates |= set(result["reset_candidates"])
        pending_results.update(result["pending_results"])
        fresh_calls.extend(result["agent_calls"])

    detected = resolve_reset_weekday(reset_candidates)
    state["reset"] = {
        "candidates": sorted(reset_candidates),
        "detected": detected,
        "ambiguous": bool(reset_candidates) and detected is None,
    }

    state["files"] = {k: v for k, v in state["files"].items() if Path(k).exists()}
    malformed_total = sum(entry.get("malformed", 0) for entry in state["files"].values())

    known = _load_stores(store_dir)
    stores = {} if rebuild_from_transcripts_only else {start: dict(b) for start, b in known.items()}
    for record in fresh:
        start = window_start(parse_ts(record["ts"]), config).isoformat()
        stores.setdefault(start, {})[_record_key(record)] = record
    _fill_tool_results(stores, pending_results)

    if not any(stores.values()):
        raise CollectionError(
            "no parseable usage records found under %s (%d transcript files scanned, %d malformed lines)"
            % (root, len(files), malformed_total)
        )

    losses = _losses(known, stores, config)
    if losses and not rebuild_from_transcripts_only:
        raise CollectionError(
            "%s\nthe record store under %s is the durable history and transcripts are pruned by Claude Code.\n"
            "re-run with --rebuild-from-transcripts-only to discard it anyway."
            % (describe_losses(losses), store_dir)
        )

    parse_stats = {"files_scanned": len(files), "malformed_lines": malformed_total}
    windows, ceiling, written, notices = _finalize(stores, config, store_dir, data_dir, reports_dir, parse_stats, window)
    calls = merge_agent_calls(store_dir, fresh_calls, config, discard_stored=rebuild_from_transcripts_only)

    _write_json(state_path, state)
    return {
        "windows": written,
        "new_records": len(fresh),
        "total_records": sum(len(r) for r in windows.values()),
        "files_scanned": len(files),
        "malformed_lines": malformed_total,
        "ceiling": ceiling,
        "reset": state["reset"],
        "losses": losses,
        "agent_calls": len(calls),
        **notices,
    }


def _num(value):
    return "-" if value is None else f"{value:,.0f}"


def _sustainable_text(ceiling):
    basis = ceiling.get("sustainable_rate_basis")
    if basis == "per-day":
        return _num(ceiling["sustainable_rate_per_day"])
    if basis == "final-day":
        return "n/a - under a day to reset, use the remaining figure"
    if basis == "window-closed":
        return "n/a - window closed"
    return "n/a - no ceiling estimate"


def _remaining_text(ceiling):
    remaining = ceiling.get("remaining_weighted")
    if remaining is None:
        return "-"
    hours = ceiling.get("remaining_hours") or 0.0
    if hours <= 0:
        return "%s weighted unused at reset" % _num(remaining)
    return "%s weighted, %.1f h to reset" % (_num(remaining), hours)


def _exhaustion_text(ceiling):
    if ceiling["projected_exhaustion"]:
        return ceiling["projected_exhaustion"]
    if ceiling.get("exhausts_before_reset") is False:
        return "not before reset"
    return "-"


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _breakdown_table(title, entries, limit=10):
    if not entries:
        return ""
    rows = [
        [entry["key"], _num(entry["weighted"]), entry["turns"], _num(entry["cache_read"]), _num(entry["output"])]
        for entry in entries[:limit]
    ]
    return "## %s\n\n%s\n" % (title, _table(["key", "weighted", "turns", "cache read", "output"], rows))


def render_markdown(window):
    meta, totals, ceiling = window["window"], window["totals"], window["ceiling"]
    parts = [
        "# %s" % meta["key"],
        "",
        "%s -> %s (%s, %s reset at %02d:00) - %.2f of 7 days elapsed%s"
        % (
            meta["start"],
            meta["end"],
            meta["timezone"],
            meta["reset_weekday"],
            meta["reset_hour"],
            meta["elapsed_days"],
            "  **current window**" if meta["is_current"] else "",
        ),
        "",
        "Generated %s from %d transcript files (%d malformed lines skipped)."
        % (window["generated_at"], window["parse"]["files_scanned"], window["parse"]["malformed_lines"]),
        "",
        "## Budget",
        "",
        _table(
            ["metric", "value"],
            [
                ["weighted tokens spent", _num(totals["weighted"])],
                [
                    "ceiling estimate (%s%s)" % (ceiling["method"], ", approximate" if ceiling["approximate"] else ""),
                    _num(ceiling["estimate"]),
                ],
                ["percent of ceiling", "-" if ceiling["percent_used"] is None else "%.1f%%" % ceiling["percent_used"]],
                ["burn rate / day", _num(ceiling["burn_rate_per_day"])],
                ["remaining until reset", _remaining_text(ceiling)],
                ["sustainable rate / day", _sustainable_text(ceiling)],
                ["projected exhaustion", _exhaustion_text(ceiling)],
            ],
        ),
        "",
        "## Totals",
        "",
        _table(
            ["metric", "value"],
            [
                ["turns", _num(totals["turns"])],
                ["sessions", _num(totals["sessions"])],
                ["input", _num(totals["input"])],
                ["output", _num(totals["output"])],
                ["thinking", _num(totals["thinking"])],
                ["cache create", _num(totals["cache_create"])],
                ["cache read", _num(totals["cache_read"])],
                ["subagent turns", _num(totals["sidechain_turns"])],
                [
                    "subagent share of weighted",
                    "-" if not totals["weighted"] else "%.1f%%" % (100 * totals["sidechain_weighted"] / totals["weighted"]),
                ],
            ],
        ),
        "",
        "## By day",
        "",
        _table(
            ["date", "weighted", "turns", "cache read", "output"],
            [[d["date"], _num(d["weighted"]), d["turns"], _num(d["cache_read"]), _num(d["output"])] for d in window["by_day"]],
        ),
        "",
    ]

    sessions = window["by_session"][:10]
    if sessions:
        parts += [
            "## Top sessions",
            "",
            _table(
                ["session", "weighted", "turns", "subagent turns", "repo", "branch", "first prompt"],
                [
                    [
                        s["key"][:8],
                        _num(s["weighted"]),
                        s["turns"],
                        s["sidechain_turns"],
                        (s["cwd"] or "-").split("\\")[-1],
                        s["gitBranch"] or "-",
                        (s["first_prompt"] or "-").replace("|", "/").replace("\n", " ")[:60],
                    ]
                    for s in sessions
                ],
            ),
            "",
        ]

    for title, key in (
        ("By repo", "by_repo"),
        ("By model", "by_model"),
        ("By effort", "by_effort"),
        ("By agent type", "by_agent"),
        ("By skill", "by_skill"),
    ):
        block = _breakdown_table(title, window[key])
        if block:
            parts += [block, ""]

    if window["unknown_models"]:
        parts += [
            "## Unknown models (priced at the default weight)",
            "",
            _table(["model", "turns", "weighted"], [[u["key"], u["turns"], _num(u["weighted"])] for u in window["unknown_models"]]),
            "",
        ]

    parts += [
        "## Findings by rule",
        "",
        _table(
            ["rule", "findings", "weighted cost"],
            [[rule, bucket["count"], _num(bucket["weighted_cost"])] for rule, bucket in window["findings_by_rule"].items()],
        )
        if window["findings_by_rule"]
        else "No rule fired in this window.",
        "",
        "## Top findings",
        "",
    ]
    for finding in window["findings"][:20]:
        parts.append(
            "- **%s** (%s) - %s - %s weighted"
            % (finding["rule"], (finding["subject"] or "-")[:8], finding["detail"], _num(finding["weighted_cost"]))
        )
    parts.append("")
    return "\n".join(parts)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


