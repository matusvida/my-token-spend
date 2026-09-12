import json
from pathlib import Path

DENIED_MARKERS = ("want to proceed with this tool use", "tool use was rejected")

KIND_CHARS = 44


def empty_index():
    return {"calls": {}, "results": {}, "assistant_uuids": set()}


def _head(text, limit=60):
    return " ".join((text or "").strip().split("\n")[0].split())[:limit]


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
