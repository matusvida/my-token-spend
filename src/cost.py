import json
from collections import defaultdict
from pathlib import Path

STORE_NAME = "session_costs.json"
LABEL = "list price, as /cost shows it; not what the subscription bills"
MIN_SESSIONS = 8
MAX_INSTABILITY = 0.3
CLASSES = ("input", "output", "cache_create", "cache_read")
SOURCE_FIELDS = {
    "input": "inputTokens",
    "output": "outputTokens",
    "cache_create": "cacheCreationInputTokens",
    "cache_read": "cacheReadInputTokens",
}


def capture(entry):
    if entry.get("type") != "cost-state":
        return None
    session = entry.get("sessionId")
    total = entry.get("totalCostUSD")
    if not session or total is None:
        return None
    models = {}
    for name, usage in (entry.get("modelUsage") or {}).items():
        if not isinstance(usage, dict):
            continue
        counts = {cls: int(usage.get(SOURCE_FIELDS[cls]) or 0) for cls in CLASSES}
        counts["usd"] = float(usage.get("costUSD") or 0.0)
        models[name] = counts
    return {"session": session, "usd": float(total), "models": models}


def store_path(data_dir):
    return Path(data_dir) / STORE_NAME


def load(data_dir):
    try:
        with store_path(data_dir).open(encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def merge(data_dir, fresh, discard_stored=False):
    known = {} if discard_stored else load(data_dir)
    for entry in fresh:
        if entry:
            known[entry["session"]] = entry
    store_path(data_dir).write_text(json.dumps(known, sort_keys=True), encoding="utf-8")
    return known


def sessions_by_window(windows):
    first = {}
    for start, records in windows.items():
        for record in records:
            session = record["sessionId"]
            seen = first.get(session)
            if seen is None or (record["ts"], start) < seen:
                first[session] = (record["ts"], start)
    owned = defaultdict(set)
    for session, (_, start) in first.items():
        owned[start].add(session)
    return owned


def window_block(session_ids, costs):
    session_ids = list(session_ids)
    priced = [costs[session] for session in session_ids if session in costs]
    return {
        "usd": round(sum(entry["usd"] for entry in priced), 4) if priced else None,
        "sessions": len(session_ids),
        "priced_sessions": len(priced),
        "share": (len(priced) / len(session_ids)) if session_ids else 0.0,
        "label": LABEL,
    }


def _solve(matrix, vector):
    size = len(vector)
    rows = [list(matrix[index]) + [vector[index]] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for row in range(size):
            if row == column:
                continue
            factor = rows[row][column] / rows[column][column]
            for cell in range(column, size + 1):
                rows[row][cell] -= factor * rows[column][cell]
    return [rows[index][size] / rows[index][index] for index in range(size)]


def _fit_prices(samples):
    size = len(CLASSES)
    matrix = [[0.0] * size for _ in range(size)]
    vector = [0.0] * size
    for counts, usd in samples:
        for row in range(size):
            vector[row] += counts[CLASSES[row]] * usd
            for column in range(size):
                matrix[row][column] += counts[CLASSES[row]] * counts[CLASSES[column]]
    solution = _solve(matrix, vector)
    return None if solution is None else dict(zip(CLASSES, solution))


def _instability(samples):
    half = len(samples) // 2
    first, second = _fit_prices(samples[:half]), _fit_prices(samples[half:])
    if first is None or second is None:
        return None
    return max(
        abs(first[cls] - second[cls]) / max(abs(first[cls]), abs(second[cls]), 1e-18) for cls in CLASSES
    )


def _samples_by_model(costs):
    grouped = defaultdict(list)
    for entry in costs.values():
        for model, counts in (entry.get("models") or {}).items():
            if counts.get("usd"):
                grouped[model].append(({cls: counts.get(cls) or 0 for cls in CLASSES}, float(counts["usd"])))
    return grouped


def calibrate(costs, config):
    import rules

    configured = config["token_class_weights"]
    rows = []
    for model, samples in sorted(_samples_by_model(costs).items()):
        row = {
            "model": model,
            "sessions": len(samples),
            "configured": dict(configured),
            "configured_model_weight": rules.model_weight(model, config)[0],
            "implied": None,
            "implied_model_weight": None,
            "reason": None,
        }
        if len(samples) < MIN_SESSIONS:
            row["reason"] = "%d sessions, fewer than the %d needed to separate four token classes" % (
                len(samples),
                MIN_SESSIONS,
            )
        else:
            prices = _fit_prices(samples)
            if prices is None or prices["input"] <= 0:
                row["reason"] = "these sessions put no positive price on fresh input, so nothing can be "                    "normalised against it"
            else:
                row["implied"] = {cls: prices[cls] / prices["input"] for cls in CLASSES}
                row["price_per_input_token"] = prices["input"]
                row["instability"] = _instability(samples)
                if any(weight <= 0 for weight in row["implied"].values()):
                    row["reason"] = "unstable: the fit prices at least one token class at or below zero"
                elif row["instability"] is None or row["instability"] > MAX_INSTABILITY:
                    row["reason"] = (
                        "unstable: the two halves of the sample disagree by %s, so the token mixes in "
                        "these sessions are too alike to separate the four classes"
                        % ("more than half" if row["instability"] is None else "%.0f%%" % (100.0 * row["instability"]))
                    )
        rows.append(row)
    reference = min(
        (row["price_per_input_token"] for row in rows if row.get("price_per_input_token")), default=None
    )
    for row in rows:
        if reference and row.get("price_per_input_token"):
            row["implied_model_weight"] = row["price_per_input_token"] / reference
    return rows


def _weight_cells(weights):
    return ["%.3f" % weights[cls] for cls in CLASSES]


def calibrate_report(rows):
    if not rows:
        return "no cost-state entries are stored, so no price can be implied"
    lines = [
        "implied relative weights next to the configured ones; nothing is written",
        "  %-32s %-9s %-8s %-8s %-8s %-8s %s" % ("model", "source", *CLASSES, "model weight"),
    ]
    for row in rows:
        lines.append(
            "  %-32s %-9s %-8s %-8s %-8s %-8s %s"
            % (row["model"][:32], "configured", *_weight_cells(row["configured"]), "%.3f" % row["configured_model_weight"])
        )
        if row["implied"] is None:
            lines.append("  %-32s %-9s %s" % ("", "implied", row["reason"]))
            continue
        lines.append(
            "  %-32s %-9s %-8s %-8s %-8s %-8s %s"
            % (
                "",
                "implied",
                *_weight_cells(row["implied"]),
                "%.3f" % row["implied_model_weight"] if row["implied_model_weight"] else "-",
            )
        )
        lines.append("  %-32s %-9s %d sessions of list-price totals" % ("", "from", row["sessions"]))
        if row["reason"]:
            lines.append("  %-32s %-9s %s" % ("", "warning", row["reason"]))
    return "\n".join(lines)
