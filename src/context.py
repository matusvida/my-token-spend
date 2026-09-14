from collections import defaultdict

import rules

PROMPT = "prompt"
UNATTRIBUTED = "unattributed"
TOP_RESULTS = 10
MAX_SESSIONS = 10
SERIES_POINTS = 300


def _usage(record):
    return (record.get("cache_read") or 0) + (record.get("cache_create") or 0)


def _carries_context(record):
    return not record.get("is_api_error") and _usage(record) > 0


def _is_reset(record):
    return bool(record.get("after_compaction")) or bool(record.get("compacted"))


def _threads(session):
    threads = defaultdict(list)
    for record in session:
        threads[record.get("agentId")].append(record)
    for thread in threads.values():
        thread.sort(key=lambda r: r["ts"])
    return threads


def _thread_growth(thread):
    previous = None
    for record in thread:
        if not _carries_context(record):
            continue
        if previous is None or _is_reset(record):
            growth = 0.0
        else:
            growth = float(max(0, _usage(record) - _usage(previous)))
        yield record, previous, growth
        previous = record


def _attribute(previous, growth):
    if growth <= 0 or previous is None:
        return {}, []
    tools = previous.get("tools") or []
    if not tools:
        return {PROMPT: growth}, []
    sizes = [tool.get("result_chars") for tool in tools]
    if any(size is None for size in sizes) or sum(sizes) <= 0:
        return {UNATTRIBUTED: growth}, []
    total = float(sum(sizes))
    buckets = defaultdict(float)
    results = []
    for tool, size in zip(tools, sizes):
        name = tool.get("name") or "unknown"
        share = growth * size / total
        buckets[name] += share
        results.append({"tool": name, "chars": size, "ts": previous["ts"], "growth": share})
    return dict(buckets), results


def summarize_session(session, config):
    settings = config["thresholds"]["context_bloat"]
    threshold = settings["cache_read_per_turn"]
    cache_read_weight = config["token_class_weights"]["cache_read"]

    by_tool = defaultdict(float)
    counted = defaultdict(int)
    largest = {}
    prompt_growth = 0.0
    unattributed = 0.0
    results = []
    series = []
    total_growth = 0.0

    for thread in _threads(session).values():
        for record, previous, growth in _thread_growth(thread):
            total_growth += growth
            buckets, attributed = _attribute(previous, growth)
            for name, share in buckets.items():
                if name == PROMPT:
                    prompt_growth += share
                elif name == UNATTRIBUTED:
                    unattributed += share
                else:
                    by_tool[name] += share
            for entry in attributed:
                counted[entry["tool"]] += 1
                best = largest.get(entry["tool"])
                if best is None or entry["chars"] > best["chars"]:
                    largest[entry["tool"]] = {"chars": entry["chars"], "ts": entry["ts"]}
            results.extend(attributed)
            leader = max(buckets, key=buckets.get) if buckets else None
            series.append([record["ts"], _usage(record), round(growth), leader])

    series.sort(key=lambda point: point[0])
    series_points = len(series)
    series = _downsample(series, SERIES_POINTS)
    ordered = sorted(session, key=lambda r: r["ts"])
    excess_tokens = 0
    carry_tax = 0.0
    for record in ordered:
        over = max(0, record["cache_read"] - threshold)
        if over:
            excess_tokens += over
            carry_tax += rules._model_weight(record, config) * cache_read_weight * over

    calls = [tool for record in session for tool in record.get("tools") or []]
    present = sum(1 for tool in calls if tool.get("result_chars") is not None)
    attributed_growth = sum(by_tool.values()) + prompt_growth

    return {
        "session": ordered[0]["sessionId"] if ordered else None,
        "turns": len(session),
        "growth_total": total_growth,
        "growth_by_tool": [
            {"tool": name, "tokens": tokens, "results": counted[name]}
            for name, tokens in sorted(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "prompt_growth": prompt_growth,
        "unattributed_growth": unattributed,
        "attributed_share": (attributed_growth / total_growth) if total_growth else 0.0,
        "top_results": sorted(results, key=lambda entry: (-entry["chars"], entry["ts"]))[:TOP_RESULTS],
        "largest_by_tool": largest,
        "compactions": sum(1 for record in session if _is_reset(record)),
        "compaction_ts": [record["ts"] for record in ordered if _is_reset(record)],
        "carry_tax": carry_tax,
        "excess_tokens": excess_tokens,
        "peak_cache_read": max((r["cache_read"] for r in session), default=0),
        "first_ts": ordered[0]["ts"] if ordered else None,
        "last_ts": ordered[-1]["ts"] if ordered else None,
        "cwd": ordered[-1]["cwd"] if ordered else None,
        "tool_results_coverage": {
            "present": present,
            "total": len(calls),
            "share": (present / len(calls)) if calls else 0.0,
        },
        "series": series,
        "series_points": series_points,
    }


def _downsample(series, limit):
    if len(series) <= limit:
        return series
    stride = len(series) / float(limit)
    binned = []
    for index in range(limit):
        bucket = series[int(index * stride) : int((index + 1) * stride)] or []
        if not bucket:
            continue
        weights = defaultdict(float)
        for point in bucket:
            if point[3]:
                weights[point[3]] += point[2]
        leader = max(weights, key=weights.get) if weights else None
        offset = max(range(len(bucket)), key=lambda position: bucket[position][1])
        peak = bucket[offset]
        binned.append(
            [peak[0], peak[1], sum(point[2] for point in bucket), leader, int(index * stride) + offset]
        )
    return binned


def _size(chars):
    if chars < 1024:
        return "%d B" % chars
    if chars < 1024 * 1024:
        return "%.0f KB" % (chars / 1024.0)
    return "%.1f MB" % (chars / (1024.0 * 1024.0))


def _clock(ts, config):
    import collect

    return collect.parse_ts(ts).astimezone(collect.zone(config)).strftime("%H:%M")


def detail(summary, threshold, config):
    text = "session of %d turns re-read %s tokens above the %s-token threshold" % (
        summary["turns"],
        "{:,}".format(summary["excess_tokens"]),
        "{:,}".format(threshold),
    )
    leader = summary["growth_by_tool"][0] if summary["growth_by_tool"] else None
    if leader and summary["growth_total"]:
        biggest = summary["largest_by_tool"].get(leader["tool"])
        text += "; %.0f%% of the context growth came from %d %s result%s" % (
            100.0 * leader["tokens"] / summary["growth_total"],
            leader["results"],
            leader["tool"],
            "" if leader["results"] == 1 else "s",
        )
        if biggest:
            text += ", the largest %s at %s" % (_size(biggest["chars"]), _clock(biggest["ts"], config))
    coverage = summary["tool_results_coverage"]
    if coverage["total"] and coverage["present"] < coverage["total"]:
        missing = coverage["total"] - coverage["present"]
        text += "; tool results were not recorded on %d of %d calls, so that much growth stays unattributed" % (
            missing,
            coverage["total"],
        )
    return text


def _by_session(records):
    sessions = defaultdict(list)
    for record in records:
        sessions[record["sessionId"]].append(record)
    return sessions


def window_block(records, config):
    settings = config["thresholds"]["context_bloat"]
    summaries = []
    for session in _by_session(records).values():
        if len(session) < settings["min_turns"]:
            continue
        summary = summarize_session(session, config)
        if summary["excess_tokens"]:
            summaries.append(summary)
    summaries.sort(key=lambda entry: (-entry["carry_tax"], entry["session"]))

    calls = [tool for record in records for tool in record.get("tools") or []]
    present = sum(1 for tool in calls if tool.get("result_chars") is not None)
    share = (present / len(calls)) if calls else 0.0
    return {
        "threshold": settings["cache_read_per_turn"],
        "sessions": summaries[:MAX_SESSIONS],
        "coverage": {"present": present, "total": len(calls), "share": share},
        "statement": "tool results are recorded on %d of %d tool calls (%.0f%%); growth from calls without a "
        "recorded result size is counted as unattributed" % (present, len(calls), 100.0 * share),
    }
