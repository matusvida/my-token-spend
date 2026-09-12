import json
from collections import Counter, defaultdict
from pathlib import Path

DENIED_MARKERS = ("want to proceed with this tool use", "tool use was rejected")

KIND_CHARS = 44

FAILED_CALLS = "failed_tool_calls"
RETRIED_AFTER_FAILURE = "retried_after_failure"
PERMISSION_DENIED = "permission_denied"
API_ERROR_TURNS = "api_error_turns"


def empty_index():
    return {"calls": {}, "results": {}, "assistant_uuids": set()}


def _outcome(block):
    if not block.get("is_error"):
        return (False, "", False)
    text = block.get("content")
    if not isinstance(text, str):
        text = json.dumps(text)
    return (True, _head(text, KIND_CHARS), any(marker in text for marker in DENIED_MARKERS))


def scan(root):
    index = empty_index()
    root = Path(root).expanduser()
    if not root.is_dir():
        return index
    for path in sorted(root.rglob("*.jsonl")):
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"type"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                kind = entry.get("type")
                content = (entry.get("message") or {}).get("content")
                if kind == "assistant":
                    index["assistant_uuids"].add(entry.get("uuid"))
                    if isinstance(content, list):
                        calls = [
                            (block.get("id"), block.get("name"))
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "tool_use"
                        ]
                        if calls:
                            index["calls"][entry.get("uuid")] = calls
                elif kind == "user" and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            index["results"][block.get("tool_use_id")] = _outcome(block)
    return index


def _sessions(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["sessionId"]].append(record)
    for session in grouped.values():
        session.sort(key=lambda r: (r["ts"], r["uuid"] or ""))
    return grouped


def _head(text, limit=60):
    return " ".join((text or "").strip().split("\n")[0].split())[:limit]


def _detector(key, label, count, cost, detail, evidence):
    return {
        "key": key,
        "label": label,
        "count": count,
        "weighted_cost": float(cost),
        "detail": detail,
        "evidence": evidence,
    }


def analyse(records, index):
    total_weighted = sum(record["weighted"] for record in records)
    covered = sum(1 for record in records if record["uuid"] in index["assistant_uuids"])
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
    kinds = Counter()

    for session in _sessions(records).values():
        flat = []
        for record in session:
            if record.get("is_api_error"):
                api_errors += 1
                api_error_cost += record["weighted"]
            calls = index["calls"].get(record["uuid"]) or []
            tools = record["tools"] or []
            share = record["weighted"] / len(tools) if tools else 0.0
            for position, tool in enumerate(tools):
                total_calls += 1
                call_id = calls[position][0] if position < len(calls) else None
                outcome = index["results"].get(call_id) if call_id else None
                if outcome is not None:
                    resolved_calls += 1
                flat.append((tool["name"], tool["hash"], share, outcome))

        first_failure = {}
        for position, (name, digest, share, outcome) in enumerate(flat):
            if not outcome or not outcome[0]:
                continue
            failed += 1
            failed_cost += share
            by_tool[name] += 1
            kinds[outcome[1]] += 1
            first_failure.setdefault((name, digest), position)
            if outcome[2]:
                denied += 1
                denied_cost += share
        for position, (name, digest, share, outcome) in enumerate(flat):
            origin = first_failure.get((name, digest))
            if origin is not None and position > origin and outcome and outcome[0]:
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
            {"by_tool": by_tool.most_common(5), "kinds": kinds.most_common(4)},
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
            "counted from the collector's own is_api_error flag, so it needs no transcript.",
            {},
        ),
    ]
    return {
        "records": len(records),
        "covered_records": covered,
        "coverage": covered / len(records) if records else 0.0,
        "total_calls": total_calls,
        "resolved_calls": resolved_calls,
        "weighted": total_weighted,
        "detectors": detectors,
    }


def load_records(store_dir, window_key):
    path = Path(store_dir) / (window_key + ".jsonl")
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records
