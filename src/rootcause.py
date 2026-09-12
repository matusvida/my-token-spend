import re
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median

import rules


SHELL = "shell"
READ = "read"
WRITE = "write"
WEB = "web"
SKILL = "skill"
DELEGATE = "delegate"
TRACKING = "tracking"
OTHER = "other"
NO_TOOLS = "no recorded tool calls"

TOOL_CATEGORIES = {
    "Bash": SHELL,
    "BashOutput": SHELL,
    "KillShell": SHELL,
    "Read": READ,
    "Grep": READ,
    "Glob": READ,
    "NotebookRead": READ,
    "Write": WRITE,
    "Edit": WRITE,
    "MultiEdit": WRITE,
    "NotebookEdit": WRITE,
    "WebFetch": WEB,
    "WebSearch": WEB,
    "Skill": SKILL,
    "SlashCommand": SKILL,
    "ToolSearch": SKILL,
    "Agent": DELEGATE,
    "Task": DELEGATE,
    "SendMessage": DELEGATE,
    "TodoWrite": TRACKING,
    "Monitor": TRACKING,
}

CATEGORY_WORDS = {
    SHELL: "shell commands",
    READ: "file reads and searches",
    WRITE: "file writes",
    WEB: "web fetches",
    SKILL: "skill loads",
    DELEGATE: "further delegation",
    TRACKING: "task tracking",
    OTHER: "assorted tools",
    NO_TOOLS: NO_TOOLS,
}

BOILERPLATE = (
    re.compile(r"^\s*base directory for this skill\s*:[^\n]*", re.I),
    re.compile(r"^\s*first,?\s+invoke\s+the\s+[`\"']?[\w:.\-]+[`\"']?\s+skill[^.\n]*[.\n]?", re.I),
    re.compile(r"^\s*(?:please\s+)?(?:read|load|use)\s+the\s+[`\"']?[\w:.\-]+[`\"']?\s+skill\s+(?:first|for [^.\n]*)[.\n]?", re.I),
    re.compile(r"^\s*you are (?:a|an|the)\s+[^.\n]{0,60}[.\n]", re.I),
    re.compile(r"^\s*other agents active in this session[^\n]*", re.I),
    re.compile(r"^\s*if you intend to call multiple tools[^\n]*", re.I),
    re.compile(r"^\s*#{1,6}\s*[\w:.\-]+\s*$", re.M),
    re.compile(r"^\s*<[^>\n]{1,80}>\s*"),
)

RUN_ID_KEY = "agentId"


def tool_category(name):
    if str(name).startswith("mcp__"):
        parts = str(name).split("__")
        return "mcp:" + (parts[1] if len(parts) > 1 and parts[1] else "unknown")
    return TOOL_CATEGORIES.get(name, OTHER)


def category_words(category):
    if category.startswith("mcp:"):
        return "%s MCP calls" % category[4:]
    return CATEGORY_WORDS.get(category, category)


def strip_boilerplate(prompt):
    text = str(prompt or "")
    for _ in range(6):
        before = text
        for pattern in BOILERPLATE:
            text = pattern.sub(" ", text, count=1)
        text = text.strip(" \t\r\n-:#*")
        if text == before:
            break
    return re.sub(r"\s+", " ", text).strip()


def label_of(prompt, limit=90):
    text = strip_boilerplate(prompt)
    if not text:
        return "no prompt captured"
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(" ,.;:") + "..."


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _span_minutes(first, last):
    start, end = _parse_ts(first), _parse_ts(last)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 60.0)


def _modal(values):
    counted = Counter(value for value in values if value)
    if not counted:
        return None, False
    ranked = sorted(counted.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ranked[0][0], len(ranked) > 1


def repo_of(cwd):
    if not cwd:
        return "unknown repo"
    return re.split(r"[\\/]", str(cwd).rstrip("\\/"))[-1] or str(cwd)


def tool_counts(records):
    counted = Counter()
    for record in records:
        for tool in record.get("tools") or []:
            counted[tool["name"]] += 1
    return counted


def category_counts(tools):
    counted = Counter()
    for name, count in tools.items():
        counted[tool_category(name)] += count
    return counted


def dominant_category(categories):
    if not categories:
        return NO_TOOLS
    return sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def coverage(records):
    total = len(records)
    with_tools = sum(1 for record in records if record.get("tools"))
    return {
        "turns": total,
        "tool_turns": with_tools,
        "share": (with_tools / total) if total else 0.0,
    }


def label_capture(records, config):
    lengths = [len(record.get("prompt") or "") for record in records if record.get("prompt")]
    observed = max(lengths) if lengths else 0
    configured = int(config.get("prompt_label_chars") or 0)
    return {
        "observed_max": observed,
        "configured": configured,
        "truncated": bool(observed and configured and observed < configured),
    }


def group_runs(records, key=RUN_ID_KEY, label_chars=90, agent_calls=None):
    grouped = defaultdict(list)
    for record in records:
        identity = record.get(key)
        if identity:
            grouped[identity].append(record)
    dispatches = dispatch_index(agent_calls)
    runs = [_describe_run(identity, turns, label_chars, dispatches) for identity, turns in grouped.items()]
    runs.sort(key=lambda run: (-run["weighted"], run["id"]))
    return runs


def description_coverage(runs):
    described = sum(1 for run in runs if run.get("description"))
    return {"described": described, "runs": len(runs), "share": (described / len(runs)) if runs else 0.0}


def description_note_of(counted):
    return "descriptions recovered for %s of runs" % _share(counted["described"], counted["runs"])


def description_note(runs):
    return description_note_of(description_coverage(runs))


TEAMMATE_TAG = re.compile(r"^\s*<teammate-message[^>]*>\s*", re.S)
DISPATCH_KEY_CHARS = 200
DISPATCH_KEY_MIN = 40


def _dispatch_key(session, prompt):
    text = " ".join(TEAMMATE_TAG.sub("", str(prompt or "")).split())[:DISPATCH_KEY_CHARS]
    return (session, text) if len(text) >= DISPATCH_KEY_MIN else None


def dispatch_index(agent_calls):
    index = defaultdict(list)
    for call in (agent_calls or {}).values():
        key = _dispatch_key(call.get("sessionId"), call.get("prompt_head"))
        if key:
            index[key].append(call)
    for calls in index.values():
        calls.sort(key=lambda call: call.get("ts") or "")
    return index


def _dispatch_of(turns, index):
    key = _dispatch_key(turns[0].get("sessionId"), turns[0].get("prompt"))
    candidates = index.get(key) if key else None
    if not candidates:
        return {}
    earlier = [call for call in candidates if (call.get("ts") or "") <= turns[0]["ts"]]
    return earlier[-1] if earlier else candidates[0]


def _describe_run(identity, turns, label_chars, dispatches=None):
    turns = sorted(turns, key=lambda record: (record["ts"], record.get("uuid") or ""))
    tools = tool_counts(turns)
    categories = category_counts(tools)
    cwd, cwd_mixed = _modal(record.get("cwd") for record in turns)
    branch, branch_mixed = _modal(record.get("gitBranch") for record in turns)
    agent, agent_mixed = _modal(record.get("attributionAgent") for record in turns)
    skill, _ = _modal(record.get("attributionSkill") for record in turns)
    session, _ = _modal(record.get("sessionId") for record in turns)
    dispatch = _dispatch_of(turns, dispatches or {})
    return {
        "id": identity,
        "description": dispatch.get("description"),
        "requested_model": dispatch.get("model"),
        "prompt_chars": dispatch.get("prompt_chars"),
        "agent": agent,
        "skill": skill,
        "session": session,
        "turns": len(turns),
        "weighted": sum(record["weighted"] for record in turns),
        "output": sum(record["output"] for record in turns),
        "first_ts": turns[0]["ts"],
        "last_ts": turns[-1]["ts"],
        "minutes": _span_minutes(turns[0]["ts"], turns[-1]["ts"]),
        "cwd": cwd,
        "repo": repo_of(cwd),
        "branch": branch,
        "mixed_context": bool(cwd_mixed or branch_mixed or agent_mixed),
        "tools": tools,
        "categories": categories,
        "dominant": dominant_category(categories),
        "tool_turns": sum(1 for record in turns if record.get("tools")),
        "models": sorted({record["model"] for record in turns}),
        "label": label_of(turns[0].get("prompt"), label_chars),
        "errors": sum(1 for record in turns if record.get("is_api_error")),
    }


STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "this", "that", "it",
    "is", "are", "be", "by", "at", "as", "from", "into", "no", "not", "you", "your", "we", "i",
    "do", "does", "did", "so", "then", "than", "there", "here", "what", "when", "which", "if",
    "every", "all", "any", "only", "also", "must", "should", "will", "can", "read", "write",
}


def _keywords(label):
    words = re.findall(r"[a-z0-9][a-z0-9\-_]{2,}", str(label).lower())
    return {word for word in words if word not in STOPWORDS}


def _agreement(runs, reference):
    others = [run for run in runs if run["id"] != reference["id"]]
    if not others:
        return 1.0
    keys = _keywords(reference["label"])
    if not keys:
        return 0.0
    shared = sum(1 for run in others if len(keys & _keywords(run["label"])) >= 2)
    return shared / len(others)


DESCRIPTION = "description"


def _cluster_key(run):
    if run.get("description"):
        return (DESCRIPTION, run["description"])
    mcp = tuple(sorted(name for name in run["categories"] if name.startswith("mcp:")))
    return (
        run["agent"] or "unattributed",
        run["repo"],
        run["branch"] or "unknown branch",
        run["dominant"],
        WRITE in run["categories"],
        mcp,
    )


def label_usable(label):
    return label != "no prompt captured" and len(_keywords(label)) >= 3


def derived_label(run):
    servers = sorted(
        name[4:] for name in run["categories"] if name.startswith("mcp:") and name != run["dominant"]
    )
    return "%s%s%s in %s on %s" % (
        category_words(run["dominant"]),
        " with file writes" if WRITE in run["categories"] else "",
        " plus %s MCP calls" % ", ".join(servers) if servers else "",
        run["repo"],
        run["branch"] or "unknown branch",
    )


def _cluster_label(reference, label_source):
    if label_source == DESCRIPTION:
        return reference["description"]
    if label_source == "prompt":
        return reference["label"]
    return derived_label(reference)


def _confidence(members, agreement, mixed, tool_share, label_source):
    if label_source == DESCRIPTION and all(run.get("description") for run in members):
        return "named"
    if len(members) == 1:
        return "single run"
    if label_source == "derived":
        return "grouping only"
    if mixed:
        return "low"
    if agreement >= 0.6 and tool_share >= 0.3:
        return "high"
    if agreement >= 0.3:
        return "medium"
    return "low"


def cluster_runs(runs, max_clusters=8):
    grouped = defaultdict(list)
    for run in runs:
        grouped[_cluster_key(run)].append(run)
    clusters = []
    for key, members in grouped.items():
        members.sort(key=lambda run: (-run["weighted"], run["id"]))
        reference = members[0]
        named = key[0] == DESCRIPTION
        usable = label_usable(reference["label"])
        agreement = _agreement(members, reference) if usable else 0.0
        mixed = not named and usable and agreement < 0.3 and len(members) > 1
        label_source = DESCRIPTION if named else ("prompt" if usable and not mixed else "derived")
        turns = sum(run["turns"] for run in members)
        tool_turns = sum(run["tool_turns"] for run in members)
        tools = Counter()
        for run in members:
            tools.update(run["tools"])
        clusters.append(
            {
                "key": key,
                "agent": reference["agent"] or "unattributed",
                "repo": reference["repo"],
                "branch": reference["branch"] or "unknown branch",
                "dominant": reference["dominant"],
                "writes": WRITE in reference["categories"],
                "mcp": sorted(name for name in reference["categories"] if name.startswith("mcp:")),
                "label": _cluster_label(reference, label_source),
                "label_source": label_source,
                "runs": len(members),
                "described_runs": sum(1 for run in members if run.get("description")),
                "turns": turns,
                "median_turns": float(median([run["turns"] for run in members])),
                "weighted": sum(run["weighted"] for run in members),
                "minutes": sum(run["minutes"] or 0.0 for run in members),
                "tools": tools,
                "tool_turns": tool_turns,
                "tool_share": (tool_turns / turns) if turns else 0.0,
                "agreement": agreement,
                "mixed": mixed,
                "confidence": _confidence(
                    members,
                    agreement,
                    mixed,
                    (tool_turns / turns) if turns else 0.0,
                    label_source if named else ("prompt" if usable else "derived"),
                ),
                "members": members,
                "other_labels": [run["label"] for run in members[1:4]],
            }
        )
    clusters.sort(key=lambda cluster: (-cluster["weighted"], cluster["label"]))
    if len(clusters) <= max_clusters:
        return clusters
    head = clusters[:max_clusters]
    tail = clusters[max_clusters:]
    head.append(
        {
            "key": None,
            "agent": None,
            "repo": None,
            "branch": None,
            "dominant": None,
            "writes": False,
            "mcp": [],
            "label": "%d smaller job clusters" % len(tail),
            "label_source": "derived",
            "runs": sum(cluster["runs"] for cluster in tail),
            "described_runs": sum(cluster["described_runs"] for cluster in tail),
            "turns": sum(cluster["turns"] for cluster in tail),
            "median_turns": 0.0,
            "weighted": sum(cluster["weighted"] for cluster in tail),
            "minutes": sum(cluster["minutes"] for cluster in tail),
            "tools": Counter(),
            "tool_turns": sum(cluster["tool_turns"] for cluster in tail),
            "tool_share": 0.0,
            "agreement": 0.0,
            "mixed": True,
            "confidence": "not clustered",
            "members": [],
            "other_labels": [],
        }
    )
    return head


def overlap(runs):
    spans = []
    for run in runs:
        start, end = _parse_ts(run["first_ts"]), _parse_ts(run["last_ts"])
        if start is not None and end is not None:
            spans.append((start, end))
    if not spans:
        return {"peak": 0, "overlapping_runs": 0, "parallel": False, "spans": 0}
    events = []
    for start, end in spans:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda event: (event[0], -event[1]))
    live = 0
    peak = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    overlapping = 0
    for index, (start, end) in enumerate(spans):
        for other, (other_start, other_end) in enumerate(spans):
            if index != other and start <= other_end and other_start <= end:
                overlapping += 1
                break
    return {"peak": peak, "overlapping_runs": overlapping, "parallel": peak > 1, "spans": len(spans)}


def _plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _num(value):
    return "{:,}".format(int(round(float(value))))


def _share(part, whole):
    return "%.0f%%" % (100.0 * part / whole) if whole else "-"


def _agent_mix(records):
    weighted = defaultdict(float)
    turns = Counter()
    for record in records:
        name = record.get("attributionAgent") or ("unattributed subagent" if record.get("isSidechain") else "main agent")
        weighted[name] += record["weighted"]
        turns[name] += 1
    return sorted(
        ({"name": name, "weighted": cost, "turns": turns[name]} for name, cost in weighted.items()),
        key=lambda entry: (-entry["weighted"], entry["name"]),
    )


def _skill_mix(records):
    weighted = defaultdict(float)
    turns = Counter()
    for record in records:
        name = record.get("attributionSkill")
        if not name:
            continue
        weighted[name] += record["weighted"]
        turns[name] += 1
    return sorted(
        ({"name": name, "weighted": cost, "turns": turns[name]} for name, cost in weighted.items()),
        key=lambda entry: (-entry["weighted"], entry["name"]),
    )


def member_labels(cluster, limit=2):
    usable = [label for label in cluster["other_labels"] if label_usable(label)]
    return " | ".join(usable[:limit]) if usable else "none the prompt labels can name"


def cluster_display(cluster, limit=70):
    if not cluster["mixed"]:
        return cluster["label"]
    return "%s - mixed, e.g. %s" % (cluster["label"], member_labels(cluster, 1)[:limit])


def _cluster_line(cluster):
    parts = [
        "%s - %s, %s weighted, %s"
        % (
            cluster["label"],
            _plural(cluster["runs"], "run"),
            _num(cluster["weighted"]),
            _plural(cluster["turns"], "turn"),
        )
    ]
    if cluster.get("label_source") != "derived":
        if cluster["dominant"]:
            parts.append("mostly %s" % category_words(cluster["dominant"]))
        if cluster["repo"] and cluster["repo"] != "unknown repo":
            parts.append("in %s" % cluster["repo"])
    parts.append("cluster confidence %s" % cluster["confidence"])
    if cluster["mixed"]:
        parts.append(
            "MIXED: these runs share tools, repo and branch but not a common job label, so the label above is "
            "derived from the tools; member labels include %s" % member_labels(cluster, 2)
        )
    elif cluster.get("label_source") == "derived":
        parts.append(
            "label derived from tools, repo and branch because the stored prompt label is too short to name the job"
        )
    return "; ".join(parts)


def trivial_turns(records, config, model=None):
    settings = config["thresholds"]["model_mismatch"]
    downgrade_weight = rules.model_weight(settings["downgrade_model"], config)[0]
    chosen = []
    for record in records:
        if model is not None and record["model"] != model:
            continue
        if rules.model_weight(record["model"], config)[0] <= downgrade_weight:
            continue
        if record["output"] > settings["max_output_tokens"]:
            continue
        if not 1 <= len(record.get("tools") or []) <= settings["max_tool_calls"]:
            continue
        chosen.append(record)
    return chosen


def repeated_tool_inputs(records, min_repeats=2):
    counted = Counter()
    for record in records:
        for tool in record.get("tools") or []:
            counted[(tool["name"], tool.get("hash"))] += 1
    repeats = [
        {"tool": name, "hash": digest, "occurrences": count}
        for (name, digest), count in counted.items()
        if count >= min_repeats
    ]
    repeats.sort(key=lambda entry: (-entry["occurrences"], entry["tool"], str(entry["hash"])))
    return repeats


def weight_components(record, config):
    weights = config["token_class_weights"]
    model_weight = rules.model_weight(record["model"], config)[0]
    parts = {
        "fresh input": model_weight * weights["input"] * record["input"],
        "context written to cache": model_weight * weights["cache_create"] * record["cache_create"],
        "context re-read from cache": model_weight * weights["cache_read"] * record["cache_read"],
        "output": model_weight * weights["output"] * record["output"],
    }
    return sorted(parts.items(), key=lambda kv: (-kv[1], kv[0]))


def _because(text, points, confidence="derived from the stored records"):
    return {"text": text, "points": [point for point in points if point], "confidence": confidence}


def _sessions(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["sessionId"]].append(record)
    for session in grouped.values():
        session.sort(key=lambda record: (record["ts"], record.get("uuid") or ""))
    return grouped


def _storm_because(finding, index):
    session = index["sessions"].get(finding["subject"]) or []
    sidechain = [record for record in session if record.get("isSidechain")]
    if not sidechain:
        return None
    runs = group_runs(sidechain, agent_calls=index["agent_calls"])
    ungrouped = [record for record in sidechain if not record.get(RUN_ID_KEY)]
    timing = overlap(runs)
    mix = _agent_mix(sidechain)
    clusters = cluster_runs(runs, max_clusters=4)
    if runs:
        shape = (
            "up to %d ran at the same time" % timing["peak"]
            if timing["parallel"]
            else "they ran one after another"
        )
        text = "The work behind it: %s across %s, median %s per run, and %s." % (
            _plural(len(runs), "subagent run"),
            _plural(len({run["agent"] or "unattributed" for run in runs}), "agent type"),
            _plural(int(median([run["turns"] for run in runs])), "turn"),
            shape,
        )
    else:
        text = "The work behind it: %s of subagent turns carry no agentId, so no run boundaries can be derived." % _num(
            len(sidechain)
        )
    points = [
        "agent types: "
        + ", ".join(
            "%s x%d turns (%s weighted)" % (entry["name"], entry["turns"], _num(entry["weighted"]))
            for entry in mix[:5]
        ),
    ]
    if runs:
        turn_counts = [run["turns"] for run in runs]
        points.append(
            "turns per run: median %d, range %d-%d over %s"
            % (int(median(turn_counts)), min(turn_counts), max(turn_counts), _plural(len(runs), "run"))
        )
        points.append(
            "timing: peak %s live at once, %d of %d runs overlapped another run"
            % (_plural(timing["peak"], "run"), timing["overlapping_runs"], len(runs))
        )
        points.extend("job cluster: " + _cluster_line(cluster) for cluster in clusters)
        points.append(description_note(runs))
    if ungrouped:
        points.append(
            "%s of these subagent turns carry no agentId (%s weighted), so they are not inside any run above"
            % (_num(len(ungrouped)), _num(sum(record["weighted"] for record in ungrouped)))
        )
    points.append(
        "tool calls are recorded on %s of this session's turns, so tool shares describe that subset only"
        % _share(coverage(session)["tool_turns"], len(session))
    )
    return _because(text, points)


def _model_mismatch_because(finding, index):
    turns = trivial_turns(index["records"], index["config"], model=finding["subject"])
    if not turns:
        return None
    tools = tool_counts(turns)
    top_tool, top_count = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    mix = _agent_mix(turns)
    skills = _skill_mix(turns)
    repos = Counter(repo_of(record.get("cwd")) for record in turns)
    text = "The work behind it: %s of those turns called a single %s, and the largest share ran under %s." % (
        _share(top_count, len(turns)),
        top_tool,
        mix[0]["name"] if mix else "no recorded attribution",
    )
    points = [
        "where they ran: "
        + ", ".join("%s %d turns" % (entry["name"], entry["turns"]) for entry in mix[:5]),
        "single tool called: "
        + ", ".join(
            "%s %s of turns" % (name, _share(count, len(turns)))
            for name, count in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        ),
        "repos: " + ", ".join("%s %d" % (name, count) for name, count in repos.most_common(4)),
        "output per turn: median %d tokens, thinking median %d tokens"
        % (
            int(median([record["output"] for record in turns])),
            int(median([record["thinking"] for record in turns])),
        ),
    ]
    if skills:
        points.append(
            "skills active on those turns: "
            + ", ".join("%s %d turns" % (entry["name"], entry["turns"]) for entry in skills[:4])
        )
    points.append(
        "a turn is counted here on output size and tool count only; what the turn was for is not recorded"
    )
    return _because(text, points)


def _skew_because(finding, index):
    agent = finding["subject"]
    turns = [record for record in index["records"] if record.get("attributionAgent") == agent]
    if not turns:
        return None
    runs = group_runs(turns, agent_calls=index["agent_calls"])
    clusters = cluster_runs(runs, max_clusters=index["max_clusters"])
    ungrouped = [record for record in turns if not record.get(RUN_ID_KEY)]
    if clusters:
        largest = clusters[0]
        text = "The work behind it: %s ran %s this window, and the runs group into %s; the largest is %s (%s, %s weighted)." % (
            agent,
            _plural(len(runs), "time"),
            _plural(len(clusters), "job cluster"),
            largest["label"],
            _plural(largest["runs"], "run"),
            _num(largest["weighted"]),
        )
    else:
        text = "The work behind it: %s of %s turns carry no agentId, so no run boundaries can be derived." % (
            _num(len(ungrouped)),
            agent,
        )
    points = ["job cluster: " + _cluster_line(cluster) for cluster in clusters]
    points.append(description_note(runs))
    if ungrouped:
        points.append(
            "%s turns attributed to %s carry no agentId (%s weighted) and are not in any cluster"
            % (_num(len(ungrouped)), agent, _num(sum(record["weighted"] for record in ungrouped)))
        )
    points.append(
        "tool calls are recorded on %s of these turns" % _share(coverage(turns)["tool_turns"], len(turns))
    )
    return _because(text, points)


def _bloat_because(finding, index):
    session = index["sessions"].get(finding["subject"]) or []
    if not session:
        return None
    hours = (_span_minutes(session[0]["ts"], session[-1]["ts"]) or 0.0) / 60.0
    heavy = sorted(session, key=lambda record: -record["cache_read"])[:20]
    tools = tool_counts(heavy)
    repeats = repeated_tool_inputs(session, min_repeats=2)
    text = "The work behind it: %s turns over %.1f h in %s, with the heaviest-context turns calling %s." % (
        _num(len(session)),
        hours,
        repo_of(session[-1].get("cwd")),
        ", ".join("%s x%d" % (name, count) for name, count in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
        or "no recorded tools",
    )
    points = [
        "context read: peak %s tokens on one turn, median %s across the session"
        % (
            _num(max(record["cache_read"] for record in session)),
            _num(median([record["cache_read"] for record in session])),
        ),
        "re-issued tool inputs: "
        + (
            ", ".join(
                "%s x%d identical calls" % (entry["tool"], entry["occurrences"]) for entry in repeats[:5]
            )
            or "none recorded"
        ),
        "tools on the 20 heaviest-context turns: "
        + (
            ", ".join(
                "%s x%d" % (name, count)
                for name, count in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
            )
            or "none recorded"
        ),
        "models in the session: " + ", ".join(sorted({record["model"] for record in session})),
        "tool calls are recorded on %s of this session's turns"
        % _share(coverage(session)["tool_turns"], len(session)),
    ]
    return _because(text, points)


def _whale_because(finding, index):
    record = index["by_uuid"].get(finding["evidence"].get("uuid"))
    if record is None:
        return None
    session = index["sessions"].get(record["sessionId"]) or []
    position = 1 + sum(1 for other in session if other["ts"] < record["ts"])
    tools = [tool["name"] for tool in record.get("tools") or []]
    components = weight_components(record, index["config"])
    text = "The work behind it: turn %d of %d in that session, under %s in %s; %s of its weighted price is %s." % (
        position,
        len(session),
        record.get("attributionAgent") or record.get("attributionSkill") or "the main agent",
        repo_of(record.get("cwd")),
        _share(components[0][1], sum(value for _, value in components)),
        components[0][0],
    )
    points = [
        "weighted price by token class: "
        + ", ".join(
            "%s %s (%s)" % (name, _num(value), _share(value, sum(other for _, other in components)))
            for name, value in components
            if value
        ),
        "tokens: %s input, %s cache create, %s cache read, %s output, %s thinking"
        % (
            _num(record["input"]),
            _num(record["cache_create"]),
            _num(record["cache_read"]),
            _num(record["output"]),
            _num(record["thinking"]),
        ),
        "tools called on that turn: " + (", ".join(tools) or "none recorded"),
        "model %s at %s effort on branch %s"
        % (record["model"], record.get("effort") or "unknown", record.get("gitBranch") or "unknown"),
        "run id: %s" % (record.get(RUN_ID_KEY) or "none - not a subagent run"),
        "session label: %s" % label_of((session[0].get("prompt") if session else None), 110),
    ]
    return _because(text, points)


def _repeat_because(finding, index):
    session = index["sessions"].get(finding["subject"]) or []
    digest = finding["evidence"].get("input_hash")
    tool = finding["evidence"].get("tool")
    hits = [
        record
        for record in session
        if any(item["name"] == tool and item.get("hash") == digest for item in record.get("tools") or [])
    ]
    if not hits:
        return None
    mix = _agent_mix(hits)
    text = "The work behind it: the same %s input was issued on %s in %s between %s and %s." % (
        tool,
        _plural(len(hits), "turn"),
        repo_of(hits[-1].get("cwd")),
        hits[0]["ts"][11:16],
        hits[-1]["ts"][11:16],
    )
    points = [
        "issued under: "
        + ", ".join("%s %d turns" % (entry["name"], entry["turns"]) for entry in mix[:4]),
        "span: %.1f minutes" % (_span_minutes(hits[0]["ts"], hits[-1]["ts"]) or 0.0),
        "other tools on the same turns: "
        + (
            ", ".join(
                "%s x%d" % (name, count)
                for name, count in sorted(tool_counts(hits).items(), key=lambda kv: (-kv[1], kv[0]))[:5]
                if name != tool
            )
            or "none"
        ),
        "the stored hash identifies identical input; the input text itself is not stored",
    ]
    return _because(text, points)


BECAUSE_BUILDERS = {
    "subagent_storm": _storm_because,
    "model_mismatch": _model_mismatch_because,
    "agent_type_skew": _skew_because,
    "context_bloat": _bloat_because,
    "whale_turns": _whale_because,
    "redundant_reads": _repeat_because,
    "loop_retry": _repeat_because,
}


def _index(records, config, max_clusters, agent_calls=None):
    return {
        "records": records,
        "config": config,
        "sessions": _sessions(records),
        "by_uuid": {record["uuid"]: record for record in records if record.get("uuid")},
        "max_clusters": max_clusters,
        "agent_calls": agent_calls or {},
    }


def _settings(config):
    shipped = {
        "min_centre_share": 0.03,
        "max_centres": 6,
        "max_clusters": 8,
        "max_findings": 12,
        "label_chars": 90,
    }
    shipped.update(config.get("rootcause") or {})
    return shipped


def explain(window, records, config, agent_calls=None):
    settings = _settings(config)
    index = _index(records, config, settings["max_clusters"], agent_calls)
    out = []
    for finding in window["findings"]:
        builder = BECAUSE_BUILDERS.get(finding["rule"])
        try:
            out.append(builder(finding, index) if builder else None)
        except (KeyError, TypeError, ValueError, IndexError):
            out.append(None)
    return out


def centres(window, records, config, agent_calls=None):
    settings = _settings(config)
    total = window["totals"]["weighted"] or 0.0
    floor = total * settings["min_centre_share"]
    found = []
    agents = defaultdict(list)
    skills = defaultdict(list)
    for record in records:
        if record.get("attributionAgent"):
            agents[record["attributionAgent"]].append(record)
        if record.get("attributionSkill"):
            skills[record["attributionSkill"]].append(record)
    for kind, groups, key in (("agent type", agents, RUN_ID_KEY), ("skill", skills, "sessionId")):
        for name, turns in groups.items():
            weighted = sum(record["weighted"] for record in turns)
            if weighted < floor:
                continue
            runs = group_runs(turns, key=key, label_chars=settings["label_chars"], agent_calls=agent_calls)
            ungrouped = [record for record in turns if not record.get(key)]
            found.append(
                {
                    "kind": kind,
                    "name": name,
                    "weighted": weighted,
                    "share": (weighted / total) if total else 0.0,
                    "turns": len(turns),
                    "runs": len(runs),
                    "run_unit": "run" if key == RUN_ID_KEY else "session",
                    "clusters": cluster_runs(runs, max_clusters=settings["max_clusters"]),
                    "coverage": coverage(turns),
                    "descriptions": description_coverage(runs),
                    "ungrouped_turns": len(ungrouped),
                    "ungrouped_weighted": sum(record["weighted"] for record in ungrouped),
                    "tools": tool_counts(turns),
                    "models": sorted({record["model"] for record in turns}),
                }
            )
    found.sort(key=lambda centre: (-centre["weighted"], centre["name"]))
    return found[: settings["max_centres"]]


def analyse(window, records, config, agent_calls=None):
    if not records:
        return None
    runs = group_runs(records, agent_calls=agent_calls)
    return {
        "records": len(records),
        "coverage": coverage(records),
        "labels": label_capture(records, config),
        "max_findings": _settings(config)["max_findings"],
        "becauses": explain(window, records, config, agent_calls),
        "centres": centres(window, records, config, agent_calls),
        "runs": len(runs),
        "descriptions": description_coverage(runs),
    }
