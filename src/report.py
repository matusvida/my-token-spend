import argparse
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import date, timedelta

import advice
import charts
import collect
import cost
import evidence
import paths
import rootcause
import text
from charts import (
    clip,
    compact,
    esc,
    exact,
    legend,
    meter,
    nice_ticks,
    percent,
    signed_compact,
    svg_burn,
    svg_context_series,
    svg_diverging_bars,
    svg_lines,
    svg_ranked_bars,
    svg_run_timeline,
    svg_scatter,
    svg_stacked_columns,
    table,
    table_view,
)


def default_data_dir():
    return str(paths.data_dir())


def default_report_dir():
    return str(paths.reports_dir())


def default_config_path():
    return str(paths.effective_config_path())


REPORT_FORMAT_VERSION = 3

DEFAULT_NARRATIVE_MODEL = "sonnet"

MALFORMED_LINES_NAMED = 5

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


def load_config(path=None):
    path = path or default_config_path()
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def records_path(window, data_dir=None):
    data_dir = data_dir or default_data_dir()
    return os.path.join(data_dir, "records", "%s.jsonl" % window["window"]["key"])


def load_records(window, data_dir=None):
    path = records_path(window, data_dir)
    records = []
    skipped = 0
    read_error = None
    try:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError as error:
                    skipped += 1
                    if skipped <= MALFORMED_LINES_NAMED:
                        print(
                            "record store %s line %d: skipping unparseable record: %s" % (path, number, error),
                            file=sys.stderr,
                        )
    except FileNotFoundError:
        pass
    except OSError as error:
        read_error = error
        print("record store %s could not be read: %s" % (path, error), file=sys.stderr)
    if skipped:
        print(
            "record store %s: %d of %d line(s) skipped as unparseable; figures derived from records are short by that many"
            % (path, skipped, len(records) + skipped),
            file=sys.stderr,
        )
    return records, skipped, read_error


def empty_store():
    return {"records": 0, "skipped": 0, "read_error": None, "analysis_error": None}


def analysis_for(window, config, data_dir=None):
    records, skipped, read_error = load_records(window, data_dir)
    store = {"records": len(records), "skipped": skipped, "read_error": read_error, "analysis_error": None}
    if read_error is not None:
        return None, store
    try:
        calls = collect.load_agent_calls(os.path.join(data_dir or default_data_dir(), "records"))
        analysis = rootcause.analyse(window, records, config, calls)
        if analysis is not None:
            analysis["evidence"] = evidence.build(window, records, config, calls)
        return analysis, store
    except (KeyError, TypeError, ValueError) as error:
        store["analysis_error"] = "%s: %s" % (type(error).__name__, error)
        print(
            "root-cause analysis failed for %s over %d readable record(s): %s"
            % (window["window"]["key"], len(records), store["analysis_error"]),
            file=sys.stderr,
        )
        traceback.print_exc()
        return None, store


def load_windows(data_dir=None):
    data_dir = data_dir or default_data_dir()
    windows = []
    for path in sorted(glob.glob(os.path.join(data_dir, "week_*.json"))):
        with open(path, encoding="utf-8") as handle:
            windows.append(json.load(handle))
    windows.sort(key=lambda w: w["window"]["start"])
    return windows


def window_key(date_str):
    return "week_" + date_str.replace("-", "_")


def select_target(windows, requested):
    if requested:
        key = window_key(requested)
        for window in windows:
            if window["window"]["key"] == key:
                return window
        raise SystemExit("no window file for %s (looked for %s.json)" % (requested, key))
    for window in reversed(windows):
        if window["window"]["is_current"]:
            return window
    return windows[-1]


def weights_divergence(windows):
    reference = windows[-1]["weights"]
    return [w["window"]["key"] for w in windows if w["weights"] != reference]


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


def agent_rows(window):
    rows = [dict(row) for row in window["by_agent"]]
    named = sum(row["weighted"] for row in rows)
    residual = window["totals"]["sidechain_weighted"] - named
    if residual > 1.0:
        rows.append({"key": "unattributed subagent turns", "weighted": residual, "turns": None})
    rows.sort(key=lambda row: -row["weighted"])
    return rows


def lane_totals(window):
    sidechain = window["totals"]["sidechain_weighted"]
    return window["totals"]["weighted"] - sidechain, sidechain


def session_label(session):
    prompt = (session.get("first_prompt") or "").strip().replace("\n", " ")
    if not prompt:
        return "no prompt captured"
    if len(prompt) > 90:
        prompt = prompt[:87].rstrip() + "..."
    return prompt


def sustainable_basis(ceiling):
    basis = ceiling.get("sustainable_rate_basis")
    if basis:
        return basis
    return "per-day" if ceiling.get("sustainable_rate_per_day") else "window-closed"


def budget_tile(ceiling):
    basis = sustainable_basis(ceiling)
    if basis == "per-day":
        return (
            "Sustainable rate",
            "%s / day" % compact(ceiling.get("sustainable_rate_per_day") or 0),
            "to finish the window at the ceiling",
        )
    remaining = ceiling.get("remaining_weighted")
    value = compact(remaining) if remaining is not None else "unknown"
    if basis == "final-day":
        return ("Budget left", value, "%.1f h to reset" % (ceiling.get("remaining_hours") or 0.0))
    if basis == "window-closed":
        return ("Budget left at reset", value, "window closed")
    return ("Budget left", "unknown", "no ceiling estimate")


def budget_qualifier(ceiling):
    basis = sustainable_basis(ceiling)
    remaining = ceiling.get("remaining_weighted")
    if basis == "per-day":
        return "sustainable %s / day" % compact(ceiling.get("sustainable_rate_per_day") or 0)
    if basis == "final-day":
        return "%s left, %.1f h to reset" % (compact(remaining or 0), ceiling.get("remaining_hours") or 0.0)
    if basis == "window-closed":
        return "window closed, %s unused" % compact(remaining or 0)
    return "no ceiling estimate"


def budget_sentence(ceiling):
    return "Burn rate %s/day (%s)." % (compact(ceiling.get("burn_rate_per_day") or 0), budget_qualifier(ceiling))


def exhaustion_label(ceiling):
    if ceiling.get("projected_exhaustion"):
        return ceiling["projected_exhaustion"][:10]
    if ceiling.get("exhausts_before_reset") is False:
        return "not before reset"
    return "-"


STYLE = """
:root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --page: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --series-3: #1baf7a;
  --series-4: #eda100;
  --series-5: #e87ba4;
  --series-6: #008300;
  --series-7: #4a3aa7;
  --series-8: #e34948;
  --series-other: #898781;
  --marker: #d03b3b;
  --delta-up: #d03b3b;
  --delta-down: #2a78d6;
  --meter-track: #cde2fb;
  --good: #0ca30c;
  --warning: #fab219;
  --serious: #ec835a;
  --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --page: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
    --series-5: #d55181;
    --series-6: #008300;
    --series-7: #9085e9;
    --series-8: #e66767;
    --series-other: #898781;
    --marker: #e66767;
    --delta-up: #d03b3b;
    --delta-down: #3987e5;
    --meter-track: #184f95;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --page: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --series-1: #3987e5;
  --series-2: #d95926;
  --series-3: #199e70;
  --series-4: #c98500;
  --series-5: #d55181;
  --series-6: #008300;
  --series-7: #9085e9;
  --series-8: #e66767;
  --series-other: #898781;
  --marker: #e66767;
  --delta-up: #d03b3b;
  --delta-down: #3987e5;
  --meter-track: #184f95;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 20px 96px;
  background: var(--page);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 15px;
  line-height: 1.5;
}
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 0 0 4px; }
h3 { font-size: 14px; margin: 20px 0 6px; color: var(--text-secondary); font-weight: 600; }
p { margin: 0 0 12px; }
.sub { color: var(--text-secondary); font-size: 13px; }
.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 22px;
  margin: 18px 0;
}
.hero-value { font-size: 56px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.05; }
.hero-unit { font-size: 14px; color: var(--text-secondary); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-top: 18px; }
.tile { border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.tile-label { font-size: 12px; color: var(--text-secondary); }
.tile-value { font-size: 22px; font-weight: 600; margin-top: 2px; }
.tile-note { font-size: 12px; color: var(--muted); margin-top: 2px; }
.meter { margin-top: 18px; }
.meter-track { height: 14px; border-radius: 7px; background: var(--meter-track); overflow: hidden; }
.meter-fill { height: 100%; background: var(--series-1); border-radius: 7px; }
.meter-caption { font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
.notice {
  border-left: 3px solid var(--warning);
  padding: 10px 14px;
  background: var(--surface-1);
  border-radius: 0 8px 8px 0;
  font-size: 13px;
  color: var(--text-secondary);
  margin: 12px 0;
}
.notice.ok { border-left-color: var(--good); }
.chart-wrap { overflow-x: auto; }
svg.chart { width: 100%; min-width: 620px; height: auto; display: block; }
.grid { stroke: var(--grid); stroke-width: 1; }
.grid.day { stroke: var(--baseline); stroke-width: 1.5; }
.baseline { stroke: var(--baseline); stroke-width: 1; }
.reference { stroke: var(--baseline); stroke-width: 1; }
.reference-label, .region-label {
  fill: var(--muted); font-size: 11px;
  stroke: var(--surface-1); stroke-width: 3px; paint-order: stroke fill;
}
.tick, .axis-label, .axis-sublabel, .row-label, .value-label {
  fill: var(--text-secondary); font-size: 12px; font-variant-numeric: tabular-nums;
}
.axis-label { fill: var(--muted); }
.axis-sublabel { fill: var(--muted); font-size: 11px; }
.axis-label.strong { fill: var(--text-primary); font-weight: 600; }
.row-label { fill: var(--text-primary); font-variant-numeric: normal; }
.value-label { fill: var(--text-primary); font-weight: 600; }
.value-label.inside { fill: var(--surface-1); }
.mark { cursor: default; }
.mark:hover, .mark:focus { opacity: 0.82; outline: none; }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.end-dot { stroke: var(--surface-1); stroke-width: 2; }
.hit { fill: transparent; }
.hit:hover, .hit:focus { fill: var(--grid); fill-opacity: 0.45; outline: none; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin: 4px 0 10px; font-size: 13px; color: var(--text-secondary); }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; }
td { font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
details.table-view { margin-top: 12px; }
details.table-view summary { cursor: pointer; font-size: 13px; color: var(--text-secondary); }
.narrative p { max-width: 72ch; }
.narrative li { max-width: 72ch; }
.rec-group { margin-top: 26px; }
.rec-group-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.rec-group-head h3 { margin: 0; color: var(--text-primary); font-size: 15px; }
.rec-group-note { font-size: 13px; color: var(--text-secondary); }
.rec {
  border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-top: 12px;
  display: grid; grid-template-columns: minmax(0, 1fr) 190px; gap: 16px;
}
.rec-title { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
.rec-action { font-size: 14px; margin-bottom: 6px; }
.rec-detail { font-size: 13px; color: var(--text-secondary); }
.rec-figures { text-align: right; }
.rec-saving { font-size: 24px; font-weight: 600; line-height: 1.1; }
.rec-share { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
.badge {
  display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  color: var(--text-secondary); margin-left: 8px;
}
.badge .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.risk-none .dot { background: var(--good); }
.risk-low .dot { background: var(--warning); }
.risk-medium .dot { background: var(--serious); }
code { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 0.92em; }
.verdict .hero-value { margin-top: 8px; }
ol.actions { margin: 6px 0 0; padding-left: 22px; }
ol.actions li { margin-bottom: 10px; }
.finding {
  border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-top: 12px;
}
.finding-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.finding-rule { font-weight: 600; }
.finding-subject { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.finding-cost { margin-left: auto; font-weight: 600; font-variant-numeric: tabular-nums; }
.finding-detail { font-size: 13px; color: var(--text-secondary); margin-top: 2px; }
.because { margin-top: 8px; max-width: 88ch; }
.because.muted-line { color: var(--muted); font-size: 13px; }
.finding details { margin-top: 6px; }
.finding summary, .raw summary { cursor: pointer; font-size: 13px; color: var(--text-secondary); }
.finding details ul { margin: 8px 0 0; padding-left: 20px; font-size: 13px; color: var(--text-secondary); }
.finding details li { margin-bottom: 4px; max-width: 96ch; }
.raw > details { border-top: 1px solid var(--grid); padding: 10px 0; }
.raw > details > summary { font-size: 14px; color: var(--text-primary); font-weight: 600; }
.raw > details > section.card {
  margin: 8px 0 0; border: 0; padding: 0; background: transparent; border-radius: 0;
}
.raw > details > section.card > h2 { font-size: 14px; color: var(--text-secondary); margin: 4px 0 6px; }

.marker { stroke: var(--marker); stroke-width: 1.5; stroke-dasharray: 3 3; }
.dot { stroke: var(--surface-1); stroke-width: 1.5; }
.region { fill: var(--grid); fill-opacity: 0.5; stroke: var(--baseline); stroke-dasharray: 4 3; }
.finding { scroll-margin-top: 14px; }
.finding-threshold { font-size: 12px; color: var(--muted); margin: 2px 0 10px; }
.chart-note { font-size: 12px; color: var(--muted); margin: 6px 0 0; }
.lane { margin-top: 24px; }
a { color: var(--series-1); }

#tooltip {
  position: fixed; pointer-events: none; opacity: 0; transition: opacity 90ms ease;
  background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
  font-size: 12px; line-height: 1.4; box-shadow: 0 6px 20px rgba(0,0,0,0.16);
  max-width: 320px; z-index: 20; white-space: pre-line;
}
"""

SCRIPT = """
(function () {
  var tip = document.getElementById('tooltip');
  function show(target, x, y) {
    tip.textContent = target.getAttribute('data-tip') || '';
    tip.style.opacity = '1';
    var box = tip.getBoundingClientRect();
    var left = Math.min(x + 14, window.innerWidth - box.width - 12);
    var top = Math.max(8, y - box.height - 14);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function hide() { tip.style.opacity = '0'; }
  document.addEventListener('pointermove', function (event) {
    var target = event.target.closest('[data-tip]');
    if (target) { show(target, event.clientX, event.clientY); } else { hide(); }
  });
  document.addEventListener('focusin', function (event) {
    var target = event.target.closest('[data-tip]');
    if (!target) { hide(); return; }
    var box = target.getBoundingClientRect();
    show(target, box.left + box.width / 2, box.top);
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('scroll', hide, true);
})();
"""


def _weights_notice(windows):
    diverging = weights_divergence(windows)
    if not diverging:
        return (
            '<div class="notice ok">All %d windows were priced with identical weights, '
            "so the cross-week bars are directly comparable.</div>" % len(windows)
        )
    return (
        '<div class="notice">These windows were priced with <strong>different</strong> weights than the '
        "latest one: %s. Cross-week totals below are <strong>not</strong> directly comparable until "
        "<code>collect.py --backfill</code> re-prices them.</div>" % esc(", ".join(diverging))
    )


def _cross_week_section(windows, target, ceiling):
    series = [
        {"name": "main agent", "color": "--series-1"},
        {"name": "subagents", "color": "--series-2"},
    ]
    categories = []
    rows = []
    for window in windows:
        main, sidechain = lane_totals(window)
        is_target = window["window"]["key"] == target["window"]["key"]
        label = window["window"]["start"][5:].replace("-", "/")
        categories.append(
            {
                "label": label,
                "sublabel": "this window" if is_target else None,
                "emphasis": is_target,
                "parts": [main, sidechain],
                "tips": [
                    "%s\nmain agent: %s weighted" % (window["window"]["key"], exact(main)),
                    "%s\nsubagents: %s weighted (%s of the window)"
                    % (
                        window["window"]["key"],
                        exact(sidechain),
                        percent(100.0 * sidechain / window["totals"]["weighted"]) if window["totals"]["weighted"] else "0%",
                    ),
                ],
            }
        )
        rows.append(
            [
                window["window"]["key"],
                exact(window["totals"]["weighted"]),
                exact(main),
                exact(sidechain),
                percent(100.0 * sidechain / window["totals"]["weighted"]) if window["totals"]["weighted"] else "-",
                exact(window["totals"]["turns"]),
                exact(window["totals"]["sessions"]),
            ]
        )
    return (
        '<section class="card"><h2>Every window, main agent vs subagents</h2>'
        '<p class="sub">Weighted tokens per reset window. The horizontal rule is the ceiling.</p>'
        "%s%s<div class=\"chart-wrap\">%s</div>%s</section>"
        % (
            legend(series),
            "",
            svg_stacked_columns(categories, series, reference=ceiling),
            table_view(
                ["window", "weighted", "main agent", "subagents", "subagent share", "turns", "sessions"],
                rows,
            ),
        )
    )


def _delta_section(previous, current, rows):
    if previous is None:
        return (
            '<section class="card"><h2>Week-over-week change</h2>'
            '<p class="sub">No earlier window on disk, so there is nothing to compare against.</p></section>'
        )
    delta = total_delta(previous, current)
    accounted = sum(row["delta"] for row in rows)
    chart_rows = []
    table_rows = []
    for row in rows:
        lane = row["lane"] or "mixed"
        chart_rows.append(
            {
                "delta": row["delta"],
                "phrase": clip("%s: %s" % (signed_compact(row["delta"]), row["phrase"]), 52),
                "tip": "%s (%s lane)\n%s -> %s weighted\ncause: %s"
                % (
                    row["repo"],
                    lane,
                    exact(row["before"]),
                    exact(row["after"]),
                    RULE_LABELS.get(row["cause"], "no rule fired on this slice"),
                ),
            }
        )
        table_rows.append(
            [
                row["repo"],
                lane,
                RULE_LABELS.get(row["cause"], "-"),
                exact(row["before"]),
                exact(row["after"]),
                ("+" if row["delta"] >= 0 else "") + exact(row["delta"]),
            ]
        )
    return (
        '<section class="card"><h2>%s vs %s: %s weighted, decomposed by cause</h2>'
        '<p class="sub">Each row is one repo &times; lane slice of the window. The slices are disjoint and '
        "sum to the total change (%s accounted, %s total), so no cost is counted twice.</p>"
        '<div class="chart-wrap">%s</div>'
        '<p class="sub">Cause labels follow a fixed precedence &mdash; %s &mdash; and each slice takes '
        "<strong>one</strong> cause only. Rule findings overlap by design, so they are never summed here.</p>"
        "%s</section>"
        % (
            esc(current["window"]["key"]),
            esc(previous["window"]["key"]),
            esc(signed_compact(delta)),
            esc(compact(accounted)),
            esc(compact(delta)),
            svg_diverging_bars(chart_rows),
            esc(" > ".join(RULE_LABELS[rule] for rule in CAUSE_PRECEDENCE)),
            table_view(
                ["repo", "lane", "cause", "previous window", "this window", "change"],
                table_rows,
            ),
        )
    )


def calendar_days(window):
    stored = {day["date"]: day for day in window["by_day"]}
    if not stored:
        return []
    start = date.fromisoformat(window["window"]["start"])
    last = date.fromisoformat(max(stored))
    days = []
    cursor = start
    while cursor <= last:
        key = cursor.isoformat()
        days.append(stored.get(key) or {"date": key, "weighted": 0.0, "turns": 0, "cache_read": 0, "output": 0})
        cursor += timedelta(days=1)
    return days


def _daily_section(window, ceiling):
    days = calendar_days(window)
    if not days:
        return ""
    labels = [day["date"][5:] for day in days]
    daily = [
        {
            "label": day["date"],
            "value": day["weighted"],
            "tip": "%s\n%s weighted\n%s turns\n%s cache read"
            % (day["date"], exact(day["weighted"]), exact(day["turns"]), exact(day["cache_read"])),
        }
        for day in days
    ]
    cumulative = []
    running = 0.0
    for day in days:
        running += day["weighted"]
        cumulative.append(running)
    estimate = ceiling.get("estimate")
    series = [{"name": "cumulative spend", "color": "--series-1"}]
    if estimate:
        pace = [estimate * (index + 1) / 7.0 for index in range(len(days))]
        series.append({"name": "even pace to the ceiling", "color": "--series-2"})
        values = [cumulative, pace]
    else:
        values = [cumulative]
    for serie, value_list in zip(series, values):
        serie["values"] = value_list
    return (
        '<section class="card"><h2>Burn inside this window</h2>'
        '<p class="sub">Weighted tokens per local day.</p>'
        '<div class="chart-wrap">%s</div>'
        "<h3>Cumulative spend against an even pace to the ceiling</h3>%s"
        '<div class="chart-wrap">%s</div>%s</section>'
        % (
            svg_ranked_bars(daily, label_width=140, row_height=34),
            legend(series),
            svg_lines(labels, series),
            table_view(
                ["date", "weighted", "cumulative", "turns", "output", "cache read"],
                [
                    [
                        day["date"],
                        exact(day["weighted"]),
                        exact(cumulative[index]),
                        exact(day["turns"]),
                        exact(day["output"]),
                        exact(day["cache_read"]),
                    ]
                    for index, day in enumerate(days)
                ],
            ),
        )
    )


def _breakdown(title, note, rows, total, key_label="key"):
    if not rows:
        return ""
    bars = [
        {
            "label": row["label"],
            "value": row["weighted"],
            "tip": "%s\n%s weighted (%s of the window)%s"
            % (
                row["label"],
                exact(row["weighted"]),
                percent(100.0 * row["weighted"] / total) if total else "0%",
                "\n%s turns" % exact(row["turns"]) if row.get("turns") else "",
            ),
        }
        for row in rows
    ]
    return (
        "<h3>%s</h3><p class=\"sub\">%s</p><div class=\"chart-wrap\">%s</div>%s"
        % (
            esc(title),
            esc(note),
            svg_ranked_bars(bars, label_width=300),
            table_view(
                [key_label, "weighted", "share", "turns"],
                [
                    [
                        row["label"],
                        exact(row["weighted"]),
                        percent(100.0 * row["weighted"] / total) if total else "-",
                        exact(row["turns"]) if row.get("turns") else "-",
                    ]
                    for row in rows
                ],
                "Numbers",
            ),
        )
    )


def _recorded_on(window, field):
    block = (window.get("field_coverage") or {}).get(field)
    if not block:
        return ""
    return "recorded on %.0f%% of turns." % (100.0 * block["share"])


def _turn_share(window, entries):
    turns = window["totals"]["turns"]
    named = sum(entry.get("turns") or 0 for entry in entries)
    return "Recorded on %.0f%% of turns." % (100.0 * named / turns) if turns else ""


def _rows_from(entries, limit=8, label_key="key"):
    ordered = sorted(entries, key=lambda entry: -entry["weighted"])[:limit]
    return [
        {"label": entry[label_key], "weighted": entry["weighted"], "turns": entry.get("turns")}
        for entry in ordered
    ]


def _composition_section(window):
    total = window["totals"]["weighted"]
    main, sidechain = lane_totals(window)
    lane_rows = [
        {"label": "main agent", "weighted": main, "turns": window["totals"]["turns"] - window["totals"]["sidechain_turns"]},
        {"label": "subagents", "weighted": sidechain, "turns": window["totals"]["sidechain_turns"]},
    ]
    agents = [
        {"label": row["key"], "weighted": row["weighted"], "turns": row.get("turns")}
        for row in agent_rows(window)[:8]
    ]
    repos = [
        {"label": repo_label(row["key"]), "weighted": row["weighted"], "turns": row.get("turns")}
        for row in sorted(window["by_repo"], key=lambda row: -row["weighted"])[:8]
    ]
    return (
        '<section class="card"><h2>Where this window went</h2>'
        "%s%s%s%s%s%s</section>"
        % (
            _breakdown("Main agent vs subagents", "Sidechain turns priced with the same weights.", lane_rows, total, "lane"),
            _breakdown("Subagent types", "Subagent turns only; turns with no attribution are pooled.", agents, total, "agent type"),
            _breakdown("Repos", "By the working directory recorded on each turn.", repos, total, "repo"),
            _breakdown("Models", "Recorded on every turn.", _rows_from(window["by_model"]), total, "model"),
            _breakdown("Effort tiers", _recorded_on(window, "effort"), _rows_from(window["by_effort"]), total, "effort"),
            _breakdown("Skills", _turn_share(window, window["by_skill"]), _rows_from(window["by_skill"], limit=6), total, "skill"),
        )
    )


def _sessions_section(window):
    sessions = sorted(window["by_session"], key=lambda session: -session["weighted"])[:12]
    rows = [
        [
            session["key"][:8],
            exact(session["weighted"]),
            exact(session["turns"]),
            exact(session.get("sidechain_turns", 0)),
            repo_label(session.get("cwd")),
            session.get("gitBranch") or "-",
            session_label(session),
        ]
        for session in sessions
    ]
    return (
        '<section class="card"><h2>Top sessions</h2>'
        '<p class="sub">Labelled with the first user prompt of the session; repo and branch are the last values '
        "the session reported.</p><div class=\"table-wrap\">%s</div></section>"
        % table(
            ["session", "weighted", "turns", "subagent turns", "repo", "branch", "prompt"],
            rows,
        )
    )


def _whales_section(window):
    whales = [f for f in window["findings"] if f["rule"] == "whale_turns"]
    if not whales:
        return ""
    rows = [
        [
            exact(finding["weighted_cost"]),
            finding["evidence"].get("model", "-"),
            finding["evidence"].get("effort") or "-",
            "subagent" if finding["evidence"].get("isSidechain") else "main",
            finding["evidence"].get("attributionAgent") or "-",
            repo_label(finding["evidence"].get("cwd")),
            (finding["evidence"].get("ts") or "")[:19].replace("T", " "),
            (finding["evidence"].get("prompt") or "-")[:80],
        ]
        for finding in whales
    ]
    return (
        '<section class="card"><h2>Whale turns</h2>'
        '<p class="sub">The single most expensive assistant turns in this window, with the prompt that '
        "triggered them.</p><div class=\"table-wrap\">%s</div></section>"
        % table(
            ["weighted", "model", "effort", "lane", "agent", "repo", "when (UTC)", "prompt"],
            rows,
        )
    )


def _findings_section(window):
    by_rule = {rule: stats for rule, stats in window["findings_by_rule"].items() if rule != HEADROOM_RULE}
    rows = [
        [
            RULE_LABELS.get(rule, rule),
            exact(stats["count"]),
            exact(stats["weighted_cost"]),
            percent(100.0 * stats["weighted_cost"] / window["totals"]["weighted"]),
        ]
        for rule, stats in sorted(by_rule.items(), key=lambda item: -item[1]["weighted_cost"])
    ]
    top = sorted(spend_findings(window), key=lambda finding: -finding["weighted_cost"])[:12]
    detail_rows = [
        [
            RULE_LABELS.get(finding["rule"], finding["rule"]),
            finding["subject"][:8] if len(finding["subject"]) > 20 else finding["subject"],
            finding["detail"],
            exact(finding["weighted_cost"]),
        ]
        for finding in top
    ]
    return (
        '<section class="card"><h2>Rule lenses</h2>'
        '<div class="notice">These totals <strong>overlap on purpose</strong> &mdash; one turn can be a whale, '
        "part of a subagent storm and part of an agent-type skew at once. Read them as separate lenses; "
        "they are never added together, and the delta decomposition above uses disjoint slices instead.</div>"
        '<div class="table-wrap">%s</div><h3>Largest individual findings</h3><div class="table-wrap">%s</div></section>'
        % (
            table(["rule", "findings", "weighted cost", "of window"], rows),
            table(["rule", "subject", "detail", "weighted"], detail_rows),
        )
    )


WORD_CAP = 1500

TAG = re.compile(r"(<[^>]+>)")


def visible_words(page):
    body = re.search(r"<main>(.*)</main>", page, re.S)
    text = body.group(1) if body else page
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S)
    kept = []
    depth = 0
    summary = False
    for token in TAG.split(text):
        if token.startswith("<") and token.endswith(">"):
            name = token[1:-1].split()[0].lower() if len(token) > 2 else ""
            if name == "details":
                depth += 1
            elif name == "/details":
                depth = max(0, depth - 1)
            elif name == "summary":
                summary = True
            elif name == "/summary":
                summary = False
            continue
        if depth == 0 or summary:
            kept.append(token)
    return len(html.unescape(" ".join(kept)).split())


GROUP_TITLES = [
    ("waste", "Waste - cut it", "Same result, fewer tokens."),
    (
        "strategy",
        "Strategy cost - tune it, never cut it",
        "Right-size the workers, never run fewer agents.",
    ),
    ("hygiene", "Hygiene - cheap habits", "How a session is opened and fed."),
    (
        "headroom",
        "Headroom - quota you did not use",
        "Work running below the tier it needs.",
    ),
]

GROUP_CARDS = 2

RISK_WORDS = {"none": "no performance risk", "low": "low performance risk", "medium": "medium performance risk"}


def figure_of(item):
    headroom = item.get("weighted_headroom")
    if headroom is None:
        return item["weighted_saving"], "saving"
    return headroom, "headroom"


def _share_line(item):
    value, basis = figure_of(item)
    share = percent(item["percent_of_window"])
    return share + " of the window" if basis == "saving" else "headroom, %s of the window" % share


def _recommendation_card(item):
    return (
        '<div class="rec"><div><div class="rec-title">%s</div>'
        '<details><summary>What to change</summary>'
        '<div class="rec-action">%s</div><div class="rec-detail">%s</div></details></div>'
        '<div class="rec-figures"><div class="rec-saving">%s</div>'
        '<div class="rec-share">%s</div>'
        '<div class="badge risk-%s"><span class="dot"></span>%s</div>'
        '<div class="badge">%s confidence</div></div></div>'
        % (
            esc(item["title"]),
            _inline_code(item["action"]),
            esc(item["detail"]),
            esc(compact(figure_of(item)[0])),
            esc(_share_line(item)),
            esc(item["performance_risk"]),
            esc(RISK_WORDS[item["performance_risk"]]),
            esc(item["confidence"]),
        )
    )


def _inline_code(text):
    parts = esc(text).split("`")
    return "".join(part if index % 2 == 0 else "<code>%s</code>" % part for index, part in enumerate(parts))


def _protected_agents(window, recommendations):
    downgraded = {item["subject"] for item in recommendations if item["kind"] == "right_size_agent_tier"}
    named = [row for row in agent_rows(window) if row.get("turns")]
    protected = [row for row in named[:4] if row["key"] not in downgraded]
    if not protected:
        return ""
    names = ", ".join(row["key"] for row in protected)
    return (
        '<p class="sub">Left alone as judgement work: %s.</p>' % esc(names)
    )


RECOMMENDATION_LABEL_CHARS = 26


def _recommendations_section(window, recommendations):
    if not recommendations:
        return (
            '<section class="card"><h2>Recommendations</h2>'
            '<p class="sub">No rule produced a recommendation above the reporting threshold this window.</p></section>'
        )
    ranked = sorted(recommendations, key=lambda entry: -figure_of(entry)[0])
    bars = [
        {
            "label": label,
            "value": figure_of(item)[0],
            "tip": "%s\n%s weighted %s (%s of the window)\n%s, %s confidence"
            % (
                item["title"],
                exact(figure_of(item)[0]),
                figure_of(item)[1],
                percent(item["percent_of_window"]),
                RISK_WORDS[item["performance_risk"]],
                item["confidence"],
            ),
        }
        for item, label in zip(
            ranked, charts.distinct_labels([item["title"] for item in ranked], RECOMMENDATION_LABEL_CHARS)
        )
    ]
    groups = []
    for key, title, note in GROUP_TITLES:
        members = [item for item in recommendations if item["group"] == key]
        if not members:
            continue
        shown, rest = members[:GROUP_CARDS], members[GROUP_CARDS:]
        cards = "".join(_recommendation_card(item) for item in shown)
        if rest:
            cards += "<details><summary>%s</summary>%s</details>" % (
                esc("%d smaller" % len(rest)),
                "".join(_recommendation_card(item) for item in rest),
            )
        extra = _protected_agents(window, recommendations) if key == "strategy" else ""
        groups.append(
            '<div class="rec-group" id="rec-%s"><div class="rec-group-head"><h3>%s</h3>'
            '<span class="rec-group-note">%s</span></div>%s%s</div>'
            % (esc(key), esc(title), esc(note), extra, cards)
        )
    return (
        '<section class="card"><h2>Recommendations</h2>'
        '<p class="sub">One rule per figure; they overlap and are <strong>never added into a '
        "total</strong>.</p>"
        '<details><summary>Ranked overview</summary><div class="chart-wrap">%s</div></details>%s%s</section>'
        % (
            svg_ranked_bars(bars, label_width=330),
            "".join(groups),
            table_view(
                ["recommendation", "class", "basis", "weighted", "of window", "risk", "confidence", "action"],
                [
                    [
                        item["title"],
                        item["group"],
                        figure_of(item)[1],
                        exact(figure_of(item)[0]),
                        percent(item["percent_of_window"]),
                        item["performance_risk"],
                        item["confidence"],
                        item["action"],
                    ]
                    for item in recommendations
                ],
                "Numbers",
            ),
        )
    )


def _headline_section(window, previous):
    totals = window["totals"]
    ceiling = window["ceiling"]
    main, sidechain = lane_totals(window)
    delta = total_delta(previous, window)
    tiles = [
        (
            "Percent of %s" % ("quota" if collect.quota_is_known(ceiling) else "ceiling"),
            percent(ceiling["percent_used"]) if ceiling.get("percent_used") is not None else "unknown",
            collect.ceiling_method_text(ceiling),
        ),
        ("Burn rate", "%s / day" % compact(ceiling.get("burn_rate_per_day") or 0), "over %.2f elapsed days" % window["window"]["elapsed_days"]),
        budget_tile(ceiling),
        (
            "Projected exhaustion",
            exhaustion_label(ceiling),
            "reset at %s" % window["window"]["end"],
        ),
        ("Turns", exact(totals["turns"]), "%s sessions" % exact(totals["sessions"])),
        (
            "Subagent share",
            percent(100.0 * sidechain / totals["weighted"]) if totals["weighted"] else "-",
            "%s subagent turns" % exact(totals["sidechain_turns"]),
        ),
        (
            "vs previous window",
            signed_compact(delta) if delta is not None else "-",
            previous["window"]["key"] if previous else "no earlier window",
        ),
        ("Cache read", compact(totals["cache_read"]), "%s output tokens" % compact(totals["output"])),
    ]
    tile_html = "".join(
        '<div class="tile"><div class="tile-label">%s</div><div class="tile-value">%s</div>'
        '<div class="tile-note">%s</div></div>' % (esc(label), esc(value), esc(note))
        for label, value, note in tiles
    )
    meter_html = ""
    if ceiling.get("percent_used") is not None:
        meter_html = meter(
            ceiling["percent_used"] / 100.0,
            "%s of %s weighted tokens (%s), %s remaining"
            % (
                percent(ceiling["percent_used"]),
                compact(ceiling["estimate"]),
                collect.ceiling_phrase(ceiling),
                compact(ceiling.get("remaining_weighted") or 0),
            ),
        )
    return (
        '<section class="card"><div class="hero-value">%s</div>'
        '<div class="hero-unit">weighted tokens spent in %s (%s to %s, %s)</div>'
        "%s<div class=\"tiles\">%s</div></section>"
        % (
            esc(compact(totals["weighted"])),
            esc(window["window"]["key"]),
            esc(window["window"]["start"]),
            esc(window["window"]["end"]),
            esc(window["window"]["timezone"]),
            meter_html,
            tile_html,
        )
    )


def _finding_subject(finding):
    subject = finding.get("subject") or "-"
    return subject[:8] if len(subject) > 20 else subject


def _sentence_case(text):
    return text[:1].upper() + text[1:]


def _store_reason(store, detail=False):
    if store["analysis_error"]:
        reason = "the root-cause analysis of this window failed with %s" % esc(store["analysis_error"])
        if detail:
            reason += (
                ", which is a defect in the analysis and not a problem with your records; the error and its "
                "traceback are on stderr"
            )
        return reason
    if store["read_error"] is not None:
        return "the record store for this window could not be read (%s)" % esc(str(store["read_error"]))
    return "no records are stored for this window"


def _because_block(because, analysis, store):
    if because:
        points = "".join("<li>%s</li>" % esc(point) for point in because["points"])
        return (
            '<details><summary>Work behind it</summary>'
            '<div class="because">%s</div><ul>%s</ul></details>' % (esc(because["text"]), points)
        )
    if analysis is None:
        return (
            '<div class="because muted-line">No root-cause line: %s, so only the rule&rsquo;s own figures are '
            "shown.</div>" % _store_reason(store)
        )
    return (
        '<div class="because muted-line">No root-cause line: the stored records carry nothing further '
        "about this finding.</div>"
    )


def _tile(label, value, note):
    return (
        '<div class="tile"><div class="tile-label">%s</div><div class="tile-value">%s</div>'
        '<div class="tile-note">%s</div></div>' % (esc(label), esc(value), esc(note))
    )


def unattributed_subagent(window):
    named = sum(row["weighted"] for row in window["by_agent"])
    sidechain = window["totals"]["sidechain_weighted"]
    return max(0.0, sidechain - named), sidechain


def usd_value(window):
    usd = (window.get("cost_usd") or {}).get("usd")
    return "unknown" if usd is None else "$%.2f" % usd


def reset_label(window):
    return "reset %s UTC" % window["window"]["end_utc"][:16].replace("T", " ")


def _burn_chart(window):
    days = calendar_days(window)
    if not days:
        return ""
    cumulative = []
    running = 0.0
    for day in days:
        running += day["weighted"]
        cumulative.append(running)
    return '<div class="chart-wrap">%s</div>' % svg_burn(
        [day["date"][5:] for day in days],
        cumulative,
        window["ceiling"].get("estimate"),
        reset_label(window),
        "quota" if collect.quota_is_known(window["ceiling"]) else "estimated ceiling",
    )


def _verdict_section(window, previous, recommendations):
    ceiling = window["ceiling"]
    totals = window["totals"]
    unattributed, sidechain = unattributed_subagent(window)
    cost_block = window.get("cost_usd") or {}
    tiles = [
        _tile(
            "%s used" % ("Quota" if collect.quota_is_known(ceiling) else "Ceiling"),
            percent(ceiling["percent_used"]) if ceiling.get("percent_used") is not None else "unknown",
            collect.ceiling_method_text(ceiling)
            if collect.quota_is_known(ceiling)
            else "quota unknown this window, ceiling estimated from your own heavy weeks",
        ),
        _tile(
            "Weighted spent",
            compact(totals["weighted"]),
            "%s turns, %s sessions" % (exact(totals["turns"]), exact(totals["sessions"])),
        ),
        _tile("List price", usd_value(window), cost_block.get("label") or cost.LABEL),
        _tile(
            "Unattributed subagent spend",
            percent(100.0 * unattributed / sidechain) if sidechain else "-",
            "of %s weighted on subagents" % compact(sidechain),
        ),
    ]
    return (
        '<section class="card verdict"><h2>%s</h2>'
        '<p class="sub">%s to %s, %s.</p>'
        '<div class="tiles">%s</div>%s%s</section>'
        % (
            esc(window["window"]["key"]),
            esc(window["window"]["start"]),
            esc(window["window"]["end"]),
            esc(window["window"]["timezone"]),
            "".join(tiles),
            _headroom_line(window, recommendations),
            _burn_chart(window),
        )
    )


def _headroom_line(window, recommendations):
    finding = headroom_finding(window)
    if finding is None:
        return ""
    unused = esc(compact(finding["evidence"]["unused_weighted"]))
    if any(item["group"] == "headroom" for item in recommendations or []):
        return (
            '<p class="sub"><a href="#rec-headroom">%s unused of your quota, two windows running</a></p>'
            % unused
        )
    return '<p class="sub">%s unused of your quota, two windows running.</p>' % unused


REC_RULES = {
    "model_downgrade": "model_mismatch",
    "deduplicate_reads": "redundant_reads",
    "break_retry_loops": "loop_retry",
    "right_size_agent_tier": "agent_type_skew-0",
    "right_size_fan_out": "subagent_storm-0",
    "reset_context": "context_bloat-0",
    "split_whale_turns": "whale_turns",
}

OVERLAP_NOTICE = (
    '<div class="notice">Savings are upper bounds from single rules, they overlap, and they are never '
    "added together. Cost is not waste: every figure is the price of work that was done, and the quality "
    "effect of changing it is not measurable from this data.</div>"
)


def _headroom_threshold(config):
    return "under %d%% of the quota in this window and the one before" % (
        (config.get("headroom") or {}).get("max_pct", 60)
    )


def _action_card(item, anchors, config):
    if item["group"] == "headroom":
        anchor, threshold = "rec-headroom", _headroom_threshold(config)
        link = '<a href="#rec-headroom">see the headroom group</a>'
    else:
        anchor = REC_RULES.get(item["kind"])
        rule = (anchor or "").split("-")[0]
        threshold = evidence.threshold_text(rule, config) if rule else ""
        link = (
            '<a href="#%s">see the chart</a>' % esc(anchor)
            if anchor in anchors
            else "no chart: the rule behind it did not fire on this window"
        )
    return (
        '<div class="rec"><div><div class="rec-title">%s</div>'
        '<div class="rec-action">Counted at %s.</div><div class="rec-detail">%s</div></div>'
        '<div class="rec-figures"><div class="rec-saving">%s</div>'
        '<div class="rec-share">%s</div>'
        '<div class="badge risk-%s"><span class="dot"></span>%s</div>'
        '<div class="badge">%s confidence</div></div></div>'
        % (
            esc(item["title"]),
            esc(threshold or "the rule threshold"),
            link,
            esc(compact(figure_of(item)[0])),
            esc(_share_line(item)),
            esc(item["performance_risk"]),
            esc(RISK_WORDS[item["performance_risk"]]),
            esc(item["confidence"]),
        )
    )


def _actions_section(recommendations, anchors, config):
    if not recommendations:
        return (
            '<section class="card"><h2>Do these first</h2>'
            '<p class="sub">No rule cleared the reporting threshold this window.</p>%s</section>'
            % OVERLAP_NOTICE
        )
    cards = "".join(_action_card(item, anchors, config) for item in recommendations[:3])
    return '<section class="card"><h2>Do these first</h2>%s%s</section>' % (cards, OVERLAP_NOTICE)


def _legend_of(names, present=None):
    colours = charts.color_map(names)
    shown = [name for name in names if present is None or name in present]
    return legend([{"name": name, "color": colours[name]} for name in shown]) if shown else ""


def _marked_legend(entries):
    shown = [{"name": name, "color": colour} for name, colour, present in entries if present]
    return legend(shown) if len(shown) > 1 else ""


def _context_chart_html(chart):
    note = "x is turn order, not a clock; colour is the tool that grew it"
    if chart.get("compactions"):
        note += "; dashed = %s" % _plural(len(chart["compactions"]), "compaction")
    note += "."
    rows = [
        [entry["tool"], exact(entry["tokens"]), exact(entry["results"])]
        for entry in chart["by_tool"]
    ]
    return "%s<div class=\"chart-wrap\">%s</div><p class=\"chart-note\">%s</p>%s" % (
        legend(charts.context_legend(chart)),
        svg_context_series(chart),
        esc(note),
        table_view(["tool", "context tokens it grew", "results"], rows, "Numbers"),
    )


def _timeline_chart_html(chart):
    if charts.timeline_mode(chart) == "turns":
        note = "Runs by turns, largest first; most runs are too thin to place on the clock; %s." % (
            rootcause.description_note_of(chart["descriptions"])
        )
    else:
        note = "Runs on the clock, thickness by turns; %s." % (
            rootcause.description_note_of(chart["descriptions"])
        )
    if chart["hidden"]:
        note += " %s not drawn." % _plural(chart["hidden"], "smaller run")
    rows = [
        [run["label"], run["agent"], run["first_ts"][11:16], run["last_ts"][11:16], exact(run["turns"]), exact(run["weighted"])]
        for run in chart["runs"]
    ]
    return "%s<div class=\"chart-wrap\">%s</div><p class=\"chart-note\">%s</p>%s" % (
        _marked_legend(
            [
                ("named by the orchestrator", "--series-1", any(run["named"] for run in chart["runs"])),
                ("label derived from tools", charts.OTHER, any(not run["named"] for run in chart["runs"])),
            ]
        ),
        svg_run_timeline(chart),
        esc(note),
        table_view(["run", "agent type", "from", "to", "turns", "weighted"], rows, "Numbers"),
    )


def _scatter_chart_html(chart):
    note = "%s of %s tool-calling turns, no jitter." % (
        exact(chart["plotted"]),
        exact(chart["total"]),
    )
    if chart.get("above_axis"):
        note += " %s above the axis not drawn." % _plural(chart["above_axis"], "turn")
    return "%s<div class=\"chart-wrap\">%s</div><p class=\"chart-note\">%s</p>" % (
        _legend_of(chart["models"], {point["model"] for point in chart["points"]}),
        svg_scatter(chart),
        esc(note),
    )


CLUSTER_BARS = 4


def _cluster_bar_rows(rows):
    head = [row for row in rows if not row["tail"] and row["confidence"] != "residual"][:CLUSTER_BARS]
    return head + [row for row in rows if row["tail"] or row["confidence"] == "residual"]


def _cluster_chart_html(chart):
    drawn = _cluster_bar_rows(chart["rows"])
    short = charts.shorten_labels([row["label"] for row in drawn], charts.LABEL_CHARS)
    rows = [
        {
            "label": label,
            "value": row["weighted"],
            "color": charts.OTHER if row["confidence"] == "residual" or row["derived"] else "--series-1",
            "tip": "%s\n%s weighted\n%s, %s"
            % (row["label"], exact(row["weighted"]), _plural(row["runs"], "run"), row["confidence"]),
        }
        for row, label in zip(drawn, short)
    ]
    note = rootcause.description_note_of(chart["descriptions"]) + "."
    if chart.get("tail_note"):
        note += " " + chart["tail_note"] + "."
    return "<div class=\"chart-wrap\">%s</div><p class=\"chart-note\">%s</p>%s" % (
        svg_ranked_bars(rows, label_width=320),
        esc(note),
        table_view(
            ["job", "runs", "weighted", "confidence"],
            [[row["label"], exact(row["runs"]), exact(row["weighted"]), row["confidence"]] for row in chart["rows"]],
            "Numbers",
        ),
    )


def _whale_chart_html(chart):
    drawn = [
        index
        for index, _ in enumerate(chart["series"])
        if any(row["parts"][index] > 0 for row in chart["rows"])
    ]
    names = [chart["series"][index] for index in drawn]
    series = [{"name": name, "color": charts.CATEGORICAL[index]} for index, name in enumerate(names)]
    categories = [
        {
            "label": row["label"],
            "sublabel": row.get("sublabel"),
            "parts": [row["parts"][index] for index in drawn],
            "tips": [
                "%s at %s\n%s: %s weighted" % (row["model"], row["label"], name, exact(row["parts"][index]))
                for name, index in zip(names, drawn)
            ],
        }
        for row in chart["rows"]
    ]
    return "%s<div class=\"chart-wrap\">%s</div><p class=\"chart-note\">%s</p>%s" % (
        legend(series),
        svg_stacked_columns(categories, series),
        esc("One bar per turn."),
        table_view(
            ["when", "turn", "model", "agent", "weighted"],
            [
                [
                    row.get("second_label") or row["label"],
                    exact(row["turn"]) if row.get("turn") else "",
                    row["model"],
                    row["agent"],
                    exact(row["weighted"]),
                ]
                for row in chart["rows"]
            ],
            "Numbers",
        ),
    )


def _repeat_chart_html(chart):
    rows = [
        [row["tool"], exact(row["repeats"]), row["first"], row["last"], exact(row["weighted"])]
        for row in chart["rows"]
    ]
    note = "The hash identifies identical input; the text is not stored."
    if chart["hidden"]:
        note += " %d more below." % chart["hidden"]
    return '<div class="table-wrap">%s</div><p class="chart-note">%s</p>' % (
        table(["tool", "repeats", "first", "last", "weighted"], rows),
        esc(note),
    )


def _round_trip_chart_html(chart):
    rows = [
        [row["tool"], exact(row["failures"]), exact(row["retries"]), exact(row["denied"]), exact(row["weighted"])]
        for row in chart["rows"]
    ]
    note = "An outcome is recorded on %s of %s calls%s." % (
        percent(100.0 * chart["coverage"]),
        exact(chart["calls"]),
        "" if chart["coverage"] >= 1.0 else "; the rest uncounted",
    )
    if chart["tools"] > len(chart["rows"]):
        note += " Top %d of %d tools." % (len(chart["rows"]), chart["tools"])
    return '<div class="table-wrap">%s</div><p class="chart-note">%s</p>' % (
        table(["tool", "failed", "failed again", "denied", "weighted"], rows),
        esc(note),
    )


CHART_HTML = {
    "context_series": _context_chart_html,
    "run_timeline": _timeline_chart_html,
    "scatter": _scatter_chart_html,
    "cluster_bars": _cluster_chart_html,
    "whale_bars": _whale_chart_html,
    "repeat_table": _repeat_chart_html,
    "round_trip_table": _round_trip_chart_html,
}


def _plural(count, word):
    return text.plural(count, word)


def _claim(card, window):
    findings = card["findings"]
    if len(findings) == 1:
        return findings[0]["detail"]
    total = sum(finding["weighted_cost"] for finding in findings)
    share = percent(100.0 * total / window["totals"]["weighted"]) if window["totals"]["weighted"] else "-"
    if card["rule"] == "whale_turns":
        return "the %s costliest turns cost %s weighted, %s of the window" % (
            len(findings),
            exact(total),
            share,
        )
    if card["rule"] == "model_mismatch":
        return findings[0]["detail"]
    return "%s cost %s weighted, %s of the window" % (
        _plural(len(findings), "repeat group"),
        exact(total),
        share,
    )


def _card_subject(card):
    findings = card["findings"]
    if len(findings) != 1:
        return "%s findings" % len(findings)
    return _finding_subject(findings[0])


def _finding_card(card, window, analysis, store):
    chart = card["chart"]
    body = CHART_HTML[chart["kind"]](chart) if chart else (
        '<p class="sub">The stored records carry nothing further about this finding, so it has no chart.</p>'
    )
    becauses = analysis.get("becauses") or []
    because = becauses[card["index"]] if card["index"] < len(becauses) else None
    tail = (
        '<p class="chart-note">Another %s in the raw breakdowns.</p>'
        % _plural(card["hidden"], RULE_LABELS.get(card["rule"], card["rule"]) + " finding")
        if card["hidden"]
        else ""
    )
    return (
        '<div class="finding" id="%s"><div class="finding-head"><span class="finding-rule">%s</span>'
        '<span class="finding-subject">%s</span><span class="finding-cost">%s</span></div>'
        '<div class="finding-detail">%s</div>'
        '<div class="finding-threshold">Counted at %s.</div>%s%s%s</div>'
        % (
            esc(card["id"]),
            esc(RULE_LABELS.get(card["rule"], card["rule"])),
            esc(_card_subject(card)),
            esc(compact(card["weighted_cost"])),
            esc(_sentence_case(_claim(card, window))),
            esc(card["threshold"]),
            body,
            tail,
            _because_block(because, analysis, store),
        )
    )


def _findings_cards_section(window, analysis, store):
    if analysis is None or not analysis.get("evidence"):
        return (
            '<section class="card"><h2>Findings</h2><p class="sub">%s, so no finding can be charted.</p></section>'
            % _sentence_case(_store_reason(store, detail=True))
        )
    built = list(analysis["evidence"]["cards"])
    chart = analysis["evidence"].get("round_trip_chart")
    if chart:
        built.append(
            {
                "id": evidence.ROUND_TRIPS,
                "rule": evidence.ROUND_TRIPS,
                "findings": [],
                "index": -1,
                "hidden": 0,
                "weighted_cost": sum(item["weighted_cost"] for item in analysis["evidence"]["round_trips"]["detectors"]),
                "threshold": evidence.threshold_text(evidence.ROUND_TRIPS, {}),
                "chart": chart,
            }
        )
    cards = []
    for card in built:
        if card["rule"] == evidence.ROUND_TRIPS:
            cards.append(_round_trip_card(card))
            continue
        cards.append(_finding_card(card, window, analysis, store))
    return (
        '<section class="card"><h2>Findings</h2>'
        '<div class="notice">One chart or table is the evidence. Lenses overlap, are never summed, and '
        "say what the work was, never why.</div>"
        "%s</section>" % "".join(cards)
    )


def _round_trip_card(card):
    return (
        '<div class="finding" id="%s"><div class="finding-head"><span class="finding-rule">round trips</span>'
        '<span class="finding-subject">%s failed</span><span class="finding-cost">%s</span></div>'
        '<div class="finding-detail">Tool calls that came back as an error, including the calls that failed '
        "again on the same input.</div>"
        '<div class="finding-threshold">Counted at %s.</div>%s</div>'
        % (
            esc(card["id"]),
            esc(exact(card["chart"]["failures"])),
            esc(compact(card["chart"]["weighted"])),
            esc(card["threshold"]),
            _round_trip_chart_html(card["chart"]),
        )
    )


def _lane_block(lane, total):
    marks = _marked_legend(
        [
            ("named by the orchestrator", "--series-1", any(not row.get("derived") for row in lane["rows"])),
            ("label derived from tools", charts.OTHER, any(row.get("derived") for row in lane["rows"])),
        ]
    )
    short = charts.shorten_labels([row["label"] for row in lane["rows"]], 40)
    rows = [
        {
            "label": label,
            "value": row["weighted"],
            "color": charts.OTHER if row.get("derived") else "--series-1",
            "tip": "%s\n%s weighted (%s of the window)"
            % (row["label"], exact(row["weighted"]), percent(100.0 * row["weighted"] / total) if total else "-"),
        }
        for row, label in zip(lane["rows"], short)
    ]
    if not rows:
        return '<div class="lane"><h3>%s</h3><p class="chart-note">%s</p></div>' % (
            esc(lane["name"]),
            esc("Nothing in this window records it. %s" % lane["note"]),
        )
    return '<div class="lane"><h3>%s</h3>%s<div class="chart-wrap">%s</div><p class="chart-note">%s</p>%s</div>' % (
        esc(lane["name"]),
        marks,
        svg_ranked_bars(rows, label_width=320),
        esc(lane["note"] + "."),
        table_view(
            ["name", "weighted", "turns"],
            [[row["label"], exact(row["weighted"]), exact(row["turns"]) if row.get("turns") else "-"] for row in lane["rows"]],
            "Numbers",
        ),
    )


def _lanes_section(window, analysis, store):
    if analysis is None or not analysis.get("evidence"):
        return (
            '<section class="card"><h2>Cost centres</h2><p class="sub">%s, so the window cannot be split '
            "into centres.</p></section>" % _sentence_case(_store_reason(store, detail=True))
        )
    total = window["totals"]["weighted"]
    return (
        '<section class="card"><h2>Cost centres</h2>'
        '<p class="sub">Six rankings of the same window, each footer stating its coverage.</p>%s%s</section>'
        % (
            _shortfall_notice(store),
            "".join(_lane_block(lane, total) for lane in analysis["evidence"]["lanes"]),
        )
    )


CLUSTER_LABEL_CHARS = 100


def _cluster_rows(centre):
    clusters = centre["clusters"]
    names = charts.distinct_labels(
        [rootcause.cluster_display(cluster) for cluster in clusters],
        CLUSTER_LABEL_CHARS,
        [cluster.get("first_ts") for cluster in clusters],
    )
    rows = []
    for cluster, name in zip(clusters, names):
        tools = ", ".join(
            "%s %d" % (name, count)
            for name, count in sorted(cluster["tools"].items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        )
        rows.append(
            [
                name,
                exact(cluster["runs"]),
                exact(cluster["turns"]),
                exact(cluster["weighted"]),
                percent(100.0 * cluster["weighted"] / centre["weighted"]) if centre["weighted"] else "-",
                "%.0f" % cluster["median_turns"],
                tools or "none recorded",
                cluster["confidence"],
            ]
        )
    return rows


def _centre_block(centre):
    notes = [
        "%s turns across %s %s%s"
        % (
            exact(centre["turns"]),
            exact(centre["runs"]),
            centre["run_unit"],
            "" if centre["runs"] == 1 else "s",
        ),
        "tool calls recorded on %s of those turns"
        % percent(100.0 * centre["coverage"]["share"]),
        "models: %s" % ", ".join(centre["models"]),
    ]
    if centre.get("descriptions"):
        notes.append(rootcause.description_note_of(centre["descriptions"]))
    if centre["ungrouped_turns"]:
        notes.append(
            "%s turns (%s weighted) carry no %s id and are outside every cluster"
            % (
                exact(centre["ungrouped_turns"]),
                compact(centre["ungrouped_weighted"]),
                centre["run_unit"],
            )
        )
    return (
        "<h3>%s &mdash; %s weighted, %s of the window</h3>"
        '<p class="sub">%s</p><div class="table-wrap">%s</div>'
        % (
            esc("%s %s" % (centre["kind"], centre["name"])),
            esc(compact(centre["weighted"])),
            esc(percent(100.0 * centre["share"])),
            esc("; ".join(notes)),
            table(
                ["job cluster", centre["run_unit"] + "s", "turns", "weighted", "of centre", "median turns", "top tools", "confidence"],
                _cluster_rows(centre),
            ),
        )
    )


def _shortfall_notice(store):
    if not store["skipped"]:
        return ""
    held = store["records"] + store["skipped"]
    return (
        '<div class="notice">This window&rsquo;s record store holds %s line(s), of which %s could not be parsed '
        "and were skipped. Every figure on this page derived from the records &mdash; tool coverage, cluster and "
        "tool shares, cost-centre shares and their weights &mdash; is computed over the %s record(s) that loaded, "
        "not over the whole window, and is short by that many. The skipped lines are reported on stderr. The "
        "transcript-level counts in the header come from collection and are unaffected.</div>"
        % (esc(exact(held)), esc(exact(store["skipped"])), esc(exact(store["records"])))
    )


def _drilldown_section(analysis, store):
    if analysis is None:
        return (
            '<section class="card"><h2>What the big cost centres did</h2>'
            '<p class="sub">%s, so its cost centres cannot be broken into jobs. Only the aggregates below are '
            "available.</p>%s</section>"
            % (_sentence_case(_store_reason(store, detail=True)), _shortfall_notice(store))
        )
    centres = analysis["centres"]
    if not centres:
        return (
            '<section class="card"><h2>What the big cost centres did</h2>'
            '<p class="sub">No agent type or skill in this window clears the reporting share, so there is '
            "nothing vague enough to drill into.</p></section>"
        )
    labels = analysis["labels"]
    shipped_label_chars = paths.shipped_config().get("prompt_label_chars") or 0
    label_note = ""
    if labels["configured"] and shipped_label_chars > labels["configured"]:
        label_note = (
            '<div class="notice">This install captures prompt labels at %d characters; the shipped default is '
            "now %d. Raise <code>prompt_label_chars</code> in your <code>config.json</code> to get fuller "
            "labels on turns collected from now on. It cannot improve labels already stored.</div>"
            % (labels["configured"], shipped_label_chars)
        )
    elif labels["truncated"]:
        label_note = (
            '<div class="notice">Prompt labels in this window were stored truncated at %d characters, below '
            "the current <code>prompt_label_chars</code> setting of %d. Raising that setting cannot improve "
            "labels already stored, so some clusters below are named from their tools, repo and branch "
            "instead.</div>" % (labels["observed_max"], labels["configured"])
        )
    return (
        '<section class="card"><h2>What the big cost centres did</h2>'
        '<p class="sub">A name like <code>general-purpose</code> is a dispatch label, not a job. Each centre '
        "below is split into the jobs its runs actually did.</p>"
        '<div class="notice">Clusters are formed on tool mix, working directory, branch and agent type, which '
        "every turn carries. The prompt is used only as a human label, after known skill boilerplate is "
        "stripped; where it is too short to name a job the label is derived from the tools instead and the "
        "cluster says so. Tool calls are recorded on %s of %s &mdash; text-only turns carry none &mdash; so "
        "tool shares describe that subset.</div>"
        "%s%s%s</section>"
        % (
            esc(percent(100.0 * analysis["coverage"]["share"])),
            "the turns this page could load" if store["skipped"] else "this window&rsquo;s turns",
            _shortfall_notice(store),
            label_note,
            "".join(_centre_block(centre) for centre in centres),
        )
    )


def _raw_section(parts):
    blocks = "".join(
        "<details><summary>%s</summary>%s</details>" % (esc(title), body) for title, body in parts if body
    )
    return (
        '<section class="card raw"><h2>Raw breakdowns</h2>'
        '<p class="sub">Separate lenses on the same window, overlapping by design.</p>%s</section>' % blocks
    )


NARRATIVE_WORDS = 120


def _clip_words(text, limit):
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]) + "..."


def _narrative_section(narrative):
    narrative = _clip_words(narrative, NARRATIVE_WORDS) if narrative else narrative
    if not narrative:
        return (
            '<p class="sub narrative-missing" data-narrative="">No narrative: writing one needs the '
            "<code>claude</code> CLI on PATH.</p>"
        )
    blocks = []
    bullets = []
    for line in narrative.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            if bullets:
                blocks.append("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(item) for item in bullets))
                bullets = []
            continue
        if stripped.startswith(("- ", "* ")):
            bullets.append(stripped[2:])
            continue
        if bullets:
            blocks.append("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(item) for item in bullets))
            bullets = []
        blocks.append("<p>%s</p>" % esc(stripped.lstrip("#").strip()))
    if bullets:
        blocks.append("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(item) for item in bullets))
    return (
        '<section class="card narrative" data-narrative="%s"><h2>Why this week looked like this</h2>%s'
        '<p class="sub">Written by one headless Claude call.</p></section>'
        % (esc(narrative.strip()), "".join(blocks))
    )


def format_stamp(window):
    totals = window["totals"]
    return (
        '<meta name="report-format-version" content="%d">'
        '<meta name="report-window" content="%s">'
        '<meta name="report-window-weighted" content="%.4f">'
        '<meta name="report-window-turns" content="%d">'
        '<meta name="report-window-closed" content="%s">'
        % (
            REPORT_FORMAT_VERSION,
            esc(window["window"]["key"]),
            float(totals["weighted"]),
            int(round(totals["turns"])),
            "false" if window["window"].get("is_current") else "true",
        )
    )


def _meta_value(text, name):
    match = re.search(r'<meta name="%s" content="([^"]*)">' % re.escape(name), text)
    return html.unescape(match.group(1)) if match else None


def _number(text, cast):
    try:
        return cast(text)
    except (TypeError, ValueError):
        return None


def _legacy_narrative(text):
    match = re.search(r'<section class="card narrative"[^>]*>(.*?)</section>', text, re.S)
    if not match:
        return None
    lines = []
    for name, attrs, content in re.findall(r"<(p|li)([^>]*)>(.*?)</\1>", match.group(1), re.S):
        if 'class="sub"' in attrs:
            continue
        plain = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
        if not plain:
            continue
        if name == "li":
            lines.append("- " + plain)
        else:
            lines.extend([plain, ""])
    return "\n".join(lines).strip() or None


def read_stamp(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    match = re.search(r'<section class="card narrative" data-narrative="([^"]*)"', text)
    narrative = html.unescape(match.group(1)).strip() if match else (_legacy_narrative(text) or "")
    return {
        "format_version": _number(_meta_value(text, "report-format-version"), int) or 0,
        "weighted": _number(_meta_value(text, "report-window-weighted"), float),
        "turns": _number(_meta_value(text, "report-window-turns"), int),
        "closed": _meta_value(text, "report-window-closed") == "true",
        "narrative": narrative or None,
    }


def report_path(window, report_dir=None):
    report_dir = report_dir or default_report_dir()
    return os.path.join(report_dir, "%s.html" % window["window"]["key"])


def stale_windows(windows, report_dir=None, force_all=False, skip_keys=()):
    report_dir = report_dir or default_report_dir()
    stale = []
    for window in windows:
        if window["window"]["key"] in skip_keys:
            continue
        stamp = read_stamp(report_path(window, report_dir))
        if force_all or stamp is None or stamp["format_version"] < REPORT_FORMAT_VERSION:
            stale.append((window, stamp))
    return stale


def frozen_data_drift(window, stamp):
    if stamp is None or not stamp["closed"] or window["window"].get("is_current"):
        return None
    drift = []
    weighted = float(window["totals"]["weighted"])
    if stamp["weighted"] is not None and abs(stamp["weighted"] - weighted) > max(1.0, abs(weighted) * 1e-9):
        drift.append("weighted %.4f -> %.4f" % (stamp["weighted"], weighted))
    turns = int(round(window["totals"]["turns"]))
    if stamp["turns"] is not None and stamp["turns"] != turns:
        drift.append("turns %d -> %d" % (stamp["turns"], turns))
    return ", ".join(drift) or None


def rebuild_stale(
    windows,
    config,
    report_dir=None,
    force_all=False,
    skip_keys=(),
    refresh_narrative=False,
    data_dir=None,
):
    report_dir = report_dir or default_report_dir()
    rebuilt = []
    for window, stamp in stale_windows(windows, report_dir, force_all, skip_keys):
        drift = frozen_data_drift(window, stamp)
        if drift:
            print(
                "DATA DRIFT in the closed window %s: %s - the format rebuild is overwriting a page whose "
                "numbers should have been frozen. Investigate before trusting either version."
                % (window["window"]["key"], drift),
                file=sys.stderr,
            )
        narrative = stamp["narrative"] if stamp else None
        recommendations = advice.recommend(window, window["findings"], config)
        analysis, store = analysis_for(window, config, data_dir)
        if refresh_narrative:
            fresh, error = narrative_for(windows, window, config, recommendations, analysis)
            if error:
                print("narrative skipped for %s: %s" % (window["window"]["key"], error), file=sys.stderr)
            narrative = fresh or narrative
        write_report(windows, window, narrative, report_dir, recommendations, analysis, store, config)
        rebuilt.append(
            {
                "key": window["window"]["key"],
                "from_version": stamp["format_version"] if stamp else None,
                "narrative_reused": bool(narrative) and not refresh_narrative,
                "drift": drift,
            }
        )
    return rebuilt


def render_html(windows, target, narrative=None, recommendations=None, analysis=None, store=None, config=None):
    store = store or empty_store()
    config = config or paths.shipped_config()
    previous = None
    for index, window in enumerate(windows):
        if window["window"]["key"] == target["window"]["key"] and index:
            previous = windows[index - 1]
    rows = decompose_delta(previous, target) if previous else []
    if recommendations is None:
        recommendations = []
    parse = target["parse"]
    raw_parts = [
        ("Every window, main agent vs subagents", _cross_week_section(windows, target, target["ceiling"].get("estimate"))),
        ("Week-over-week change, decomposed by cause", _delta_section(previous, target, rows)),
        ("Burn inside this window", _daily_section(target, target["ceiling"])),
        ("Where this window went", _composition_section(target)),
        ("Top sessions", _sessions_section(target)),
        ("Whale turns", _whales_section(target)),
        ("Headline tiles", _headline_section(target, previous)),
        ("Rule lenses", _findings_section(target)),
        ("What the big cost centres did", _drilldown_section(analysis, store)),
    ]
    anchors = {card["id"] for card in ((analysis or {}).get("evidence") or {}).get("cards") or []}
    body = "".join(
        [
            "<header><h1>Claude token guardrail &mdash; %s</h1>" % esc(target["window"]["key"]),
            '<p class="sub">%s &middot; %s files, %s records, %s malformed. Weighted tokens, not raw.</p>'
            "</header>"
            % (
                esc(target["generated_at"][:19].replace("T", " ")),
                esc(exact(parse["files_scanned"])),
                esc(exact(parse["records"])),
                esc(exact(parse["malformed_lines"])),
            ),
            _weights_notice(windows),
            _verdict_section(target, previous, recommendations),
            _actions_section(recommendations, anchors, config),
            _narrative_section(narrative),
            _findings_cards_section(target, analysis, store),
            _lanes_section(target, analysis, store),
            _raw_section(raw_parts),
            _recommendations_section(target, recommendations),
        ]
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">%s'
        "<title>Token guardrail &mdash; %s</title><style>%s</style></head><body>"
        '<main>%s</main><div id="tooltip" role="status"></div><script>%s</script></body></html>'
        % (format_stamp(target), esc(target["window"]["key"]), STYLE, body, SCRIPT)
    )


def build_narrative_prompt(target, previous, rows, recommendations=None, analysis=None):
    lines = [
        "You are writing two short paragraphs for a personal Claude Code token-usage report.",
        "Window %s (%s to %s), %s weighted tokens, %s of the ceiling."
        % (
            target["window"]["key"],
            target["window"]["start"],
            target["window"]["end"],
            exact(target["totals"]["weighted"]),
            percent(target["ceiling"]["percent_used"]) if target["ceiling"].get("percent_used") is not None else "unknown share",
        ),
        budget_sentence(target["ceiling"]),
    ]
    if previous:
        lines.append(
            "Change vs %s: %s weighted." % (previous["window"]["key"], signed_compact(total_delta(previous, target)))
        )
        lines.append("Biggest disjoint movements (repo x lane, one cause each):")
        for row in rows[:6]:
            lines.append(
                "- %s %s (%s lane): %s"
                % (row["repo"], signed_compact(row["delta"]), row["lane"] or "mixed", RULE_LABELS.get(row["cause"], "no rule fired"))
            )
    lines.append("Recommendations already derived from the rules, ranked:")
    for finding in sorted(target["findings"], key=lambda f: -f["weighted_cost"])[:6]:
        lines.append("- %s: %s (%s weighted)" % (finding["rule"], finding["detail"], compact(finding["weighted_cost"])))
    jobs = (((analysis or {}).get("evidence") or {}).get("lanes") or [{}])[0].get("rows") or []
    if jobs:
        lines.append("Jobs this window, named by the description the orchestrator dispatched them with:")
        for job in jobs[:6]:
            lines.append("- %s: %s weighted" % (job["label"], compact(job["weighted"])))
    for session in ((target.get("context") or {}).get("sessions") or [])[:2]:
        growth = ", ".join(
            "%s %s from %d results" % (compact(entry["tokens"]), entry["tool"], entry["results"])
            for entry in (session.get("growth_by_tool") or [])[:4]
        )
        lines.append(
            "- context growth in session %s: %s total, %s, %d compaction(s)"
            % (session["session"][:8], compact(session["growth_total"]), growth or "unattributed", session["compactions"])
        )
    lines.append("Top sessions:")
    for session in sorted(target["by_session"], key=lambda s: -s["weighted"])[:3]:
        lines.append(
            "- %s in %s on %s: %s weighted, %s turns, %s subagent turns, first prompt: %s"
            % (
                session["key"][:8],
                repo_label(session.get("cwd")),
                session.get("gitBranch") or "unknown branch",
                compact(session["weighted"]),
                session["turns"],
                session.get("sidechain_turns", 0),
                session_label(session),
            )
        )
    for item in (recommendations or [])[:5]:
        lines.append(
            "- [%s] %s: %s weighted (%s of the window), %s performance risk, %s confidence"
            % (
                item["group"],
                item["title"],
                compact(item["weighted_saving"]),
                percent(item["percent_of_window"]),
                item["performance_risk"],
                item["confidence"],
            )
        )
    lines.append(
        "Write plain prose, no headings, no markdown emphasis, at most 120 words: what drove this window, and "
        "then ground the advice in the recommendations listed above, leading with the largest one. Never sum "
        "the overlapping findings, and never suggest using fewer subagents - heavy orchestration is the "
        "intended workflow; right-size the workers and the batch size instead. Name the jobs by the "
        "descriptions above rather than by session hashes."
    )
    return "\n".join(lines)


def fetch_narrative(prompt, timeout=180, model=DEFAULT_NARRATIVE_MODEL):
    executable = shutil.which("claude")
    if not executable:
        return None, "claude CLI not on PATH"
    try:
        result = subprocess.run(
            [executable, "-p", prompt, "--model", model or DEFAULT_NARRATIVE_MODEL],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)
    if result.returncode != 0:
        return None, (result.stderr or "").strip()[:300] or "exit code %d" % result.returncode
    text = (result.stdout or "").strip()
    return (text, None) if text else (None, "empty response")


def write_report(windows, target, narrative, report_dir=None, recommendations=None, analysis=None, store=None, config=None):
    report_dir = report_dir or default_report_dir()
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "%s.html" % target["window"]["key"])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_html(windows, target, narrative, recommendations, analysis, store, config))
    return path


def console_summary(windows, target, recommendations=None):
    previous = None
    for index, window in enumerate(windows):
        if window["window"]["key"] == target["window"]["key"] and index:
            previous = windows[index - 1]
    ceiling = target["ceiling"]
    lines = [
        "%s  %s weighted  %s turns  %s sessions"
        % (
            target["window"]["key"],
            exact(target["totals"]["weighted"]),
            exact(target["totals"]["turns"]),
            exact(target["totals"]["sessions"]),
        ),
        "  percent of ceiling : %s (%s estimate %s)"
        % (
            percent(ceiling["percent_used"]) if ceiling.get("percent_used") is not None else "unknown",
            ceiling.get("method", "-"),
            exact(ceiling["estimate"]) if ceiling.get("estimate") else "-",
        ),
        "  burn rate          : %s / day (%s)"
        % (compact(ceiling.get("burn_rate_per_day") or 0), budget_qualifier(ceiling)),
    ]
    if previous:
        lines.append(
            "  vs %s      : %s weighted" % (previous["window"]["key"], signed_compact(total_delta(previous, target)))
        )
    lines.append("  top savings        :")
    for item in (recommendations or [])[:3]:
        lines.append(
            "    %-46s %s weighted (%s, %s risk)"
            % (
                item["title"][:46],
                compact(figure_of(item)[0]),
                percent(item["percent_of_window"]),
                item["performance_risk"],
            )
        )
    lines.append("  top causes         :")
    causes = {rule: stats for rule, stats in target["findings_by_rule"].items() if rule != HEADROOM_RULE}
    for rule, stats in sorted(causes.items(), key=lambda item: -item[1]["weighted_cost"])[:3]:
        lines.append(
            "    %-18s %s weighted across %d findings"
            % (RULE_LABELS.get(rule, rule), compact(stats["weighted_cost"]), stats["count"])
        )
    lines.append("  (rule lenses overlap - never add them together)")
    return "\n".join(lines)


def narrative_for(windows, target, config, recommendations=None, analysis=None):
    if recommendations is None:
        recommendations = advice.recommend(target, target["findings"], config)
    index = [w["window"]["key"] for w in windows].index(target["window"]["key"])
    previous = windows[index - 1] if index else None
    rows = decompose_delta(previous, target) if previous else []
    return fetch_narrative(
        build_narrative_prompt(target, previous, rows, recommendations, analysis),
        model=config.get("narrative_model"),
    )


def rebuild_summary(rebuilt, windows):
    if not rebuilt:
        return "  format             : %d, all %d pages current" % (REPORT_FORMAT_VERSION, len(windows))
    reused = sum(1 for item in rebuilt if item["narrative_reused"])
    return "  format             : %d, rebuilt %d stale page(s) [%s], narrative reused on %d" % (
        REPORT_FORMAT_VERSION,
        len(rebuilt),
        ", ".join(item["key"] for item in rebuilt),
        reused,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the weekly Claude token guardrail report.")
    parser.add_argument("--window", help="window start date, YYYY-MM-DD; defaults to the current window")
    parser.add_argument("--no-narrative", action="store_true", help="skip the headless Claude narrative call")
    parser.add_argument("--all", action="store_true", help="force-rebuild every window's HTML regardless of its format stamp")
    parser.add_argument(
        "--refresh-narrative",
        action="store_true",
        help="regenerate the prose on rebuilt pages too - one Claude call per rebuilt window",
    )
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    data_dir = args.data_dir or default_data_dir()
    report_dir = args.report_dir or default_report_dir()
    windows = load_windows(data_dir)
    if not windows:
        raise SystemExit("no window files in %s - run collect first" % data_dir)
    target = select_target(windows, args.window)

    config = load_config(args.config)
    recommendations = advice.recommend(target, target["findings"], config)

    analysis, store = analysis_for(target, config, data_dir)
    narrative = None
    if not args.no_narrative:
        narrative, error = narrative_for(windows, target, config, recommendations, analysis)
        if error:
            print("narrative skipped: %s" % error, file=sys.stderr)

    path = write_report(windows, target, narrative, report_dir, recommendations, analysis, store, config)
    rebuilt = rebuild_stale(
        windows,
        config,
        report_dir,
        force_all=args.all,
        skip_keys={target["window"]["key"]},
        refresh_narrative=args.refresh_narrative,
        data_dir=data_dir,
    )
    print(console_summary(windows, target, recommendations))
    print(rebuild_summary(rebuilt, windows))
    print("  report             : %s" % os.path.abspath(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
