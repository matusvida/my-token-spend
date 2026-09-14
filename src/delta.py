import os


CAUSE_PRECEDENCE = [
    "subagent_storm",
    "context_bloat",
    "whale_turns",
    "model_mismatch",
    "redundant_reads",
    "loop_retry",
    "agent_type_skew",
]

HEADROOM_RULE = "headroom"

SUBAGENT_ONLY_RULES = {"subagent_storm", "agent_type_skew"}
GLOBAL_RULES = {"agent_type_skew", "model_mismatch"}

RULE_LABELS = {
    "subagent_storm": "subagent storm",
    "agent_type_skew": "agent-type skew",
    "model_mismatch": "model mismatch",
    "context_bloat": "context bloat",
    "whale_turns": "whale turns",
    "redundant_reads": "redundant reads",
    "loop_retry": "loop / retry burn",
}


def repo_label(cwd):
    if not cwd:
        return "unknown"
    return os.path.basename(cwd.rstrip("\\/")) or cwd


def cell_grid(window):
    grid = {}
    for session in window["by_session"]:
        repo = repo_label(session.get("cwd"))
        sidechain = session.get("sidechain_weighted", 0.0)
        main = session["weighted"] - sidechain
        grid[(repo, "main")] = grid.get((repo, "main"), 0.0) + main
        grid[(repo, "subagent")] = grid.get((repo, "subagent"), 0.0) + sidechain
    return {key: value for key, value in grid.items() if abs(value) > 0.5}


def _finding_matches_cell(finding, repo, lane):
    rule = finding["rule"]
    if rule in SUBAGENT_ONLY_RULES and lane != "subagent":
        return False
    if rule in GLOBAL_RULES:
        return finding["evidence"].get("cwd") in (None, "") or repo_label(finding["evidence"].get("cwd")) == repo
    return repo_label(finding["evidence"].get("cwd")) == repo


def spend_findings(window):
    return [f for f in window["findings"] if f["rule"] != HEADROOM_RULE]


def headroom_finding(window):
    for finding in window["findings"]:
        if finding["rule"] == HEADROOM_RULE:
            return finding
    return None


def choose_cause(window, repo, lane):
    candidates = [f for f in spend_findings(window) if _finding_matches_cell(f, repo, lane)]
    if not candidates:
        return None
    candidates.sort(key=lambda f: (CAUSE_PRECEDENCE.index(f["rule"]), -f["weighted_cost"]))
    return candidates[0]


def cause_phrase(finding, repo, lane):
    if finding is None:
        return "%s work in %s" % ("subagent" if lane == "subagent" else "main-agent", repo)
    return "%s in %s" % (RULE_LABELS[finding["rule"]], repo)


def decompose_delta(previous, current, top_n=8):
    before = cell_grid(previous) if previous else {}
    after = cell_grid(current)
    rows = []
    for key in set(before) | set(after):
        repo, lane = key
        delta = after.get(key, 0.0) - before.get(key, 0.0)
        if abs(delta) < 1.0:
            continue
        source = current if delta > 0 else previous
        finding = choose_cause(source, repo, lane) if source else None
        rows.append(
            {
                "repo": repo,
                "lane": lane,
                "delta": delta,
                "before": before.get(key, 0.0),
                "after": after.get(key, 0.0),
                "cause": None if finding is None else finding["rule"],
                "phrase": cause_phrase(finding, repo, lane),
            }
        )
    rows.sort(key=lambda row: -abs(row["delta"]))
    head = rows[:top_n]
    tail = rows[top_n:]
    if tail:
        head.append(
            {
                "repo": "everything else",
                "lane": "",
                "delta": sum(row["delta"] for row in tail),
                "before": sum(row["before"] for row in tail),
                "after": sum(row["after"] for row in tail),
                "cause": None,
                "phrase": "%d smaller movements" % len(tail),
            }
        )
    return head


def total_delta(previous, current):
    if previous is None:
        return None
    return current["totals"]["weighted"] - previous["totals"]["weighted"]


