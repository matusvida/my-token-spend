import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import report


WEIGHTS = {
    "token_class_weights": {"input": 1.0, "cache_create": 1.25, "cache_read": 0.1, "output": 5.0},
    "model_weights": {"claude-opus-5": 5.0, "claude-sonnet-5": 1.0},
    "default_model_weight": 1.0,
}


def bucket(key, weighted, turns=10):
    return {
        "key": key,
        "turns": turns,
        "weighted": weighted,
        "input": 1,
        "output": 1,
        "thinking": 0,
        "cache_create": 1,
        "cache_read": 1,
    }


def session(key, weighted, sidechain, cwd, branch="main", prompt="do the thing", turns=10):
    return {
        "key": key,
        "turns": turns,
        "weighted": weighted,
        "input": 1,
        "output": 1,
        "thinking": 0,
        "cache_create": 1,
        "cache_read": 1,
        "sidechain_turns": turns // 2,
        "sidechain_weighted": sidechain,
        "models": ["claude-opus-5"],
        "agents": ["general-purpose"],
        "first_ts": "2026-08-24T10:00:00+00:00",
        "last_ts": "2026-08-25T10:00:00+00:00",
        "cwd": cwd,
        "gitBranch": branch,
        "first_prompt": prompt,
    }


def finding(rule, subject, cost, **evidence):
    return {
        "rule": rule,
        "subject": subject,
        "detail": "%s on %s" % (rule, subject),
        "weighted_cost": cost,
        "evidence": evidence,
    }


def make_window(start="2026-08-22", sessions=None, findings=None, is_current=True, weights=None, total=None):
    sessions = sessions if sessions is not None else [session("s1", 1000.0, 400.0, "C:\\workspace\\alpha")]
    findings = findings if findings is not None else []
    weighted = total if total is not None else sum(item["weighted"] for item in sessions)
    sidechain = sum(item["sidechain_weighted"] for item in sessions)
    end = "2026-08-29" if start == "2026-08-22" else "2026-08-22"
    by_rule = {}
    for item in findings:
        entry = by_rule.setdefault(item["rule"], {"count": 0, "weighted_cost": 0.0})
        entry["count"] += 1
        entry["weighted_cost"] += item["weighted_cost"]
    return {
        "schema_version": 1,
        "generated_at": "2026-08-27T19:36:20.305811+00:00",
        "window": {
            "key": "week_" + start.replace("-", "_"),
            "start": start,
            "end": end,
            "start_utc": start + "T00:00:00+00:00",
            "end_utc": end + "T00:00:00+00:00",
            "timezone": "Europe/Prague",
            "reset_weekday": "Saturday",
            "reset_hour": 0,
            "is_current": is_current,
            "elapsed_days": 5.9,
            "elapsed_fraction": 0.84,
        },
        "weights": weights or WEIGHTS,
        "totals": {
            "turns": 100,
            "weighted": weighted,
            "input": 10,
            "output": 20,
            "thinking": 5,
            "cache_create": 30,
            "cache_read": 400,
            "sessions": len(sessions),
            "sidechain_turns": 40,
            "sidechain_weighted": sidechain,
        },
        "by_day": [
            dict(bucket("2026-08-24", weighted / 2), date="2026-08-24"),
            dict(bucket("2026-08-25", weighted / 2), date="2026-08-25"),
        ],
        "by_model": [bucket("claude-opus-5", weighted)],
        "by_effort": [bucket("medium", weighted)],
        "by_repo": [bucket("C:\\workspace\\alpha", weighted)],
        "by_branch": [bucket("main", weighted)],
        "by_agent": [bucket("general-purpose", sidechain / 2 if sidechain else 0.0)],
        "by_skill": [bucket("review-mr", weighted / 4)],
        "unknown_models": [],
        "by_session": sessions,
        "findings": findings,
        "findings_by_rule": by_rule,
        "ceiling": {
            "estimate": 10000.0,
            "method": "top-cluster",
            "approximate": True,
            "cluster_size": 2,
            "windows_considered": 2,
            "percent_used": 100.0 * weighted / 10000.0,
            "burn_rate_per_day": weighted / 5.9,
            "remaining_weighted": 10000.0 - weighted,
            "sustainable_rate_per_day": (10000.0 - weighted) / 1.1,
            "projected_exhaustion": "2026-08-31T00:00:00+00:00",
            "exhausts_before_reset": False,
        },
        "parse": {"files_scanned": 12, "malformed_lines": 0, "records": 100},
    }


def write_windows(directory, windows):
    os.makedirs(directory, exist_ok=True)
    for window in windows:
        with open(os.path.join(directory, "%s.json" % window["window"]["key"]), "w", encoding="utf-8") as handle:
            json.dump(window, handle)
    return directory


def test_compact_scales_and_signs():
    assert report.compact(999) == "999"
    assert report.compact(1500) == "1.5K"
    assert report.compact(801_397_016) == "801.4M"
    assert report.compact(1_239_325_382) == "1.2B"
    assert report.compact(-2_500_000) == "-2.5M"
    assert report.signed_compact(2_500_000) == "+2.5M"
    assert report.signed_compact(-2_500_000) == "-2.5M"


def test_exact_and_percent():
    assert report.exact(801_397_016.49) == "801,397,016"
    assert report.percent(64.664) == "64.7%"


def test_nice_ticks_cover_the_maximum():
    ticks = report.nice_ticks(812_850_236)
    assert ticks[0] == 0
    assert ticks[-1] >= 812_850_236
    assert ticks == sorted(ticks)
    assert report.nice_ticks(0) == [0.0, 1.0]


def test_repo_label_uses_the_leaf_directory():
    assert report.repo_label("C:\\workspace\\dynamic-pricing\\srst-service") == "srst-service"
    assert report.repo_label("/home/x/proj/") == "proj"
    assert report.repo_label(None) == "unknown"


def test_cell_grid_is_disjoint_and_reconciles_to_the_window_total():
    window = make_window(
        sessions=[
            session("a", 1000.0, 600.0, "C:\\workspace\\alpha"),
            session("b", 500.0, 100.0, "C:\\workspace\\alpha"),
            session("c", 300.0, 0.0, "C:\\workspace\\beta"),
        ]
    )
    grid = report.cell_grid(window)
    assert grid[("alpha", "subagent")] == 700.0
    assert grid[("alpha", "main")] == 800.0
    assert grid[("beta", "main")] == 300.0
    assert ("beta", "subagent") not in grid
    assert sum(grid.values()) == pytest.approx(window["totals"]["weighted"])


def test_decompose_delta_sums_to_the_total_change():
    previous = make_window(
        start="2026-08-15",
        is_current=False,
        sessions=[
            session("a", 1000.0, 600.0, "C:\\workspace\\alpha"),
            session("c", 300.0, 0.0, "C:\\workspace\\beta"),
        ],
    )
    current = make_window(
        sessions=[
            session("d", 2000.0, 1800.0, "C:\\workspace\\alpha"),
            session("e", 100.0, 0.0, "C:\\workspace\\gamma"),
        ]
    )
    rows = report.decompose_delta(previous, current, top_n=99)
    assert sum(row["delta"] for row in rows) == pytest.approx(report.total_delta(previous, current))


def test_decompose_delta_folds_the_tail_into_one_residual_row():
    sessions = [session("s%d" % index, 100.0 * (index + 1), 0.0, "C:\\workspace\\r%d" % index) for index in range(6)]
    current = make_window(sessions=sessions)
    previous = make_window(start="2026-08-15", is_current=False, sessions=[session("old", 50.0, 0.0, "C:\\workspace\\r0")])
    rows = report.decompose_delta(previous, current, top_n=2)
    assert len(rows) == 3
    assert rows[-1]["repo"] == "everything else"
    assert sum(row["delta"] for row in rows) == pytest.approx(report.total_delta(previous, current))


def test_cause_precedence_prefers_the_storm_on_the_subagent_lane():
    window = make_window(
        findings=[
            finding("agent_type_skew", "general-purpose", 900.0),
            finding("subagent_storm", "a", 400.0, cwd="C:\\workspace\\alpha"),
        ]
    )
    chosen = report.choose_cause(window, "alpha", "subagent")
    assert chosen["rule"] == "subagent_storm"


def test_subagent_only_rules_never_explain_the_main_lane():
    window = make_window(
        findings=[
            finding("subagent_storm", "a", 900.0, cwd="C:\\workspace\\alpha"),
            finding("agent_type_skew", "general-purpose", 800.0),
            finding("context_bloat", "a", 100.0, cwd="C:\\workspace\\alpha"),
        ]
    )
    chosen = report.choose_cause(window, "alpha", "main")
    assert chosen["rule"] == "context_bloat"


def test_cause_precedence_orders_the_main_lane_rules():
    window = make_window(
        findings=[
            finding("loop_retry", "a", 5.0, cwd="C:\\workspace\\alpha"),
            finding("whale_turns", "a", 50.0, cwd="C:\\workspace\\alpha"),
            finding("redundant_reads", "a", 900.0, cwd="C:\\workspace\\alpha"),
        ]
    )
    assert report.choose_cause(window, "alpha", "main")["rule"] == "whale_turns"


def test_cause_ignores_findings_from_another_repo():
    window = make_window(findings=[finding("context_bloat", "a", 900.0, cwd="C:\\workspace\\beta")])
    assert report.choose_cause(window, "alpha", "main") is None
    assert report.cause_phrase(None, "alpha", "main") == "main-agent work in alpha"


def test_each_cell_takes_exactly_one_cause():
    window = make_window(
        sessions=[session("a", 1000.0, 600.0, "C:\\workspace\\alpha")],
        findings=[
            finding("subagent_storm", "a", 600.0, cwd="C:\\workspace\\alpha"),
            finding("agent_type_skew", "general-purpose", 600.0),
            finding("whale_turns", "a", 400.0, cwd="C:\\workspace\\alpha"),
        ],
    )
    previous = make_window(start="2026-08-15", is_current=False, sessions=[])
    rows = report.decompose_delta(previous, window, top_n=99)
    for row in rows:
        assert row["cause"] in report.CAUSE_PRECEDENCE or row["cause"] is None
    assert sum(1 for row in rows if row["lane"] == "subagent") == 1


def test_weights_divergence_flags_repricing():
    same = [make_window(start="2026-08-15", is_current=False), make_window()]
    assert report.weights_divergence(same) == []
    stale_weights = json.loads(json.dumps(WEIGHTS))
    stale_weights["model_weights"]["claude-opus-5"] = 1.0
    mixed = [make_window(start="2026-08-15", is_current=False, weights=stale_weights), make_window()]
    assert report.weights_divergence(mixed) == ["week_2026_08_15"]


def test_weights_notice_says_so_when_windows_disagree():
    stale_weights = json.loads(json.dumps(WEIGHTS))
    stale_weights["model_weights"]["claude-opus-5"] = 1.0
    windows = [make_window(start="2026-08-15", is_current=False, weights=stale_weights), make_window()]
    html = report.render_html(windows, windows[-1])
    assert "not</strong> directly comparable" in html
    assert "week_2026_08_15" in html


def test_weights_notice_confirms_comparability_when_they_agree():
    windows = [make_window(start="2026-08-15", is_current=False), make_window()]
    assert "directly comparable" in report.render_html(windows, windows[-1])


def test_select_target_defaults_to_the_current_window():
    windows = [make_window(start="2026-08-15", is_current=False), make_window()]
    assert report.select_target(windows, None)["window"]["key"] == "week_2026_08_22"
    assert report.select_target(windows, "2026-08-15")["window"]["key"] == "week_2026_08_15"


def test_select_target_rejects_an_unknown_window():
    with pytest.raises(SystemExit):
        report.select_target([make_window()], "2026-01-01")


def test_agent_rows_add_the_unattributed_residual():
    window = make_window(sessions=[session("a", 1000.0, 600.0, "C:\\workspace\\alpha")])
    rows = report.agent_rows(window)
    assert sum(row["weighted"] for row in rows) == pytest.approx(600.0)
    assert any(row["key"] == "unattributed subagent turns" for row in rows)


def test_render_html_has_no_external_references():
    windows = [make_window(start="2026-08-15", is_current=False), make_window()]
    html = report.render_html(windows, windows[-1])
    assert not re.search(r'src\s*=', html)
    assert all(href.startswith("#") for href in re.findall(r'href\s*=\s*"([^"]*)"', html))
    assert "url(" not in html
    assert "@import" not in html
    assert "<script>" in html and "</script>" in html
    assert "<style>" in html


def test_render_html_escapes_untrusted_prompt_text():
    hostile = session("a", 1000.0, 400.0, "C:\\workspace\\alpha", prompt="<script>alert('x')</script>")
    windows = [make_window(sessions=[hostile])]
    html = report.render_html(windows, windows[-1])
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html


def test_render_html_carries_the_headline_numbers_and_theme_blocks():
    windows = [make_window(start="2026-08-15", is_current=False), make_window()]
    html = report.render_html(windows, windows[-1])
    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="dark"]' in html
    assert "week_2026_08_22" in html
    assert "percent of ceiling" in html.lower()


def test_render_html_without_a_previous_window_still_renders():
    windows = [make_window()]
    html = report.render_html(windows, windows[0])
    assert "nothing to compare against" in html


def test_render_html_states_the_precedence_and_the_overlap_warning():
    windows = [make_window(start="2026-08-15", is_current=False), make_window()]
    html = report.render_html(windows, windows[-1])
    assert "subagent storm &gt; context bloat" in html
    assert "overlap on purpose" in html


def test_narrative_is_injected_when_present():
    windows = [make_window()]
    html = report.render_html(windows, windows[0], narrative="Subagents ate the week.\n\n- fewer reviewers")
    assert "Subagents ate the week." in html
    assert "<li>fewer reviewers</li>" in html


def test_narrative_absence_is_stated_not_crashed():
    windows = [make_window()]
    assert "No narrative was generated" in report.render_html(windows, windows[0], narrative=None)


def test_build_narrative_prompt_is_small_and_warns_about_summing():
    previous = make_window(start="2026-08-15", is_current=False)
    current = make_window(findings=[finding("subagent_storm", "a", 600.0, cwd="C:\\workspace\\alpha")])
    prompt = report.build_narrative_prompt(current, previous, report.decompose_delta(previous, current))
    assert "week_2026_08_22" in prompt
    assert "Never sum the overlapping findings" in prompt
    assert "never suggest using fewer subagents" in prompt
    assert len(prompt) < 4000


def test_fetch_narrative_reports_a_missing_cli(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: None)
    text, error = report.fetch_narrative("hi")
    assert text is None
    assert "not on PATH" in error


def test_fetch_narrative_reports_a_failing_call(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "claude")

    def explode(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(report.subprocess, "run", explode)
    text, error = report.fetch_narrative("hi")
    assert text is None
    assert error


def test_fetch_narrative_returns_stdout(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "claude")

    class Result:
        returncode = 0
        stdout = "  it was the subagents  "
        stderr = ""

    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: Result())
    text, error = report.fetch_narrative("hi")
    assert text == "it was the subagents"
    assert error is None


def test_fetch_narrative_treats_an_empty_answer_as_failure(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "claude")

    class Result:
        returncode = 0
        stdout = "\n"
        stderr = ""

    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: Result())
    assert report.fetch_narrative("hi") == (None, "empty response")


def test_console_summary_leads_with_the_budget_and_top_causes():
    windows = [
        make_window(start="2026-08-15", is_current=False),
        make_window(findings=[finding("subagent_storm", "a", 600.0, cwd="C:\\workspace\\alpha")]),
    ]
    summary = report.console_summary(windows, windows[-1])
    assert "percent of ceiling" in summary
    assert "burn rate" in summary
    assert "subagent storm" in summary
    assert "never add them together" in summary


def test_main_writes_the_report_without_a_narrative(tmp_path, capsys):
    data_dir = write_windows(
        str(tmp_path / "data"),
        [make_window(start="2026-08-15", is_current=False), make_window()],
    )
    report_dir = str(tmp_path / "reports")
    exit_code = report.main(["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir])
    assert exit_code == 0
    written = os.path.join(report_dir, "week_2026_08_22.html")
    assert os.path.exists(written)
    assert "percent of ceiling" in capsys.readouterr().out


def test_main_survives_a_broken_narrative_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, "fetch_narrative", lambda prompt, timeout=180, model=None: (None, "claude blew up"))
    data_dir = write_windows(str(tmp_path / "data"), [make_window()])
    report_dir = str(tmp_path / "reports")
    assert report.main(["--data-dir", data_dir, "--report-dir", report_dir]) == 0
    captured = capsys.readouterr()
    assert "narrative skipped: claude blew up" in captured.err
    assert os.path.exists(os.path.join(report_dir, "week_2026_08_22.html"))


def test_main_fails_loudly_on_an_empty_data_dir(tmp_path):
    with pytest.raises(SystemExit):
        report.main(["--no-narrative", "--data-dir", str(tmp_path), "--report-dir", str(tmp_path)])


import paths

REAL_DATA = str(paths.data_dir())


@pytest.mark.skipif(not os.path.isdir(REAL_DATA), reason="no collected data on this machine")
def test_real_windows_reconcile_and_render():
    windows = report.load_windows(REAL_DATA)
    if not windows:
        pytest.skip("no window files")
    for window in windows:
        grid = report.cell_grid(window)
        assert sum(grid.values()) == pytest.approx(window["totals"]["weighted"], rel=1e-9)
    target = report.select_target(windows, None)
    html = report.render_html(windows, target)
    assert not re.search(r'(href|src)\s*=', html)
    assert target["window"]["key"] in html


def recommendation(kind="model_downgrade", group="waste", saving=96200374.0, risk="none", confidence="high"):
    return {
        "kind": kind,
        "group": group,
        "subject": "claude-opus-5",
        "title": "Run trivial claude-opus-5 turns on claude-sonnet-5",
        "action": 'Add `"model": "claude-sonnet-5"` to the agent definitions.',
        "detail": "1628 trivial turns, identical work.",
        "weighted_saving": saving,
        "percent_of_window": 11.6,
        "performance_risk": risk,
        "confidence": confidence,
        "score": saving,
        "evidence": {},
    }


def test_recommendations_section_renders_all_three_groups():
    windows = [make_window()]
    html = report.render_html(
        windows,
        windows[0],
        recommendations=[
            recommendation(),
            recommendation("right_size_agent_tier", "strategy", 15584734.0, "low", "medium"),
            recommendation("reset_context", "hygiene", 70334762.0, "low", "medium"),
        ],
    )
    assert "Waste - cut it" in html
    assert "tune it, never cut it" in html
    assert "Hygiene - cheap habits" in html
    assert "no performance risk" in html
    assert "high confidence" in html


def test_recommendations_section_refuses_to_show_a_total():
    windows = [make_window()]
    html = report.render_html(windows, windows[0], recommendations=[recommendation()])
    assert "never added into a total" in html
    assert "total savings" not in html.lower()


def test_recommendations_section_states_its_absence():
    windows = [make_window()]
    html = report.render_html(windows, windows[0], recommendations=[])
    assert "No rule produced a recommendation" in html


def test_render_html_still_works_without_recommendations():
    windows = [make_window()]
    assert "Recommendations" in report.render_html(windows, windows[0])


def test_recommendation_action_backticks_become_inline_code():
    windows = [make_window()]
    html = report.render_html(windows, windows[0], recommendations=[recommendation()])
    assert "<code>&quot;model&quot;: &quot;claude-sonnet-5&quot;</code>" in html


def test_narrative_prompt_carries_the_recommendations_and_the_orchestration_guardrail():
    current = make_window()
    prompt = report.build_narrative_prompt(current, None, [], [recommendation()])
    assert "Run trivial claude-opus-5 turns" in prompt
    assert "never suggest using fewer subagents" in prompt


def test_console_summary_leads_with_the_top_savings():
    windows = [make_window()]
    summary = report.console_summary(windows, windows[0], [recommendation()])
    assert "top savings" in summary
    assert "96.2M" in summary


def ceiling_fixture(basis, **overrides):
    base = {
        "estimate": 1000.0,
        "method": "top-cluster",
        "approximate": True,
        "percent_used": 80.0,
        "burn_rate_per_day": 130.1,
        "remaining_weighted": 200.0,
        "remaining_days": 0.0833,
        "remaining_hours": 2.0,
        "sustainable_rate_basis": basis,
        "sustainable_rate_per_day": None,
        "projected_exhaustion": None,
        "exhausts_before_reset": False,
    }
    base.update(overrides)
    return base


def test_final_day_reports_budget_left_instead_of_a_per_day_rate():
    title, value, sub = report.budget_tile(ceiling_fixture("final-day"))
    assert title == "Budget left"
    assert value == "200"
    assert sub == "2.0 h to reset"


def test_final_day_qualifier_never_quotes_a_sustainable_rate():
    qualifier = report.budget_qualifier(ceiling_fixture("final-day"))
    assert "sustainable" not in qualifier
    assert qualifier == "200 left, 2.0 h to reset"


def test_closed_window_reports_unused_budget_not_a_rate():
    title, value, sub = report.budget_tile(ceiling_fixture("window-closed", remaining_hours=0.0, remaining_days=0.0))
    assert title == "Budget left at reset"
    assert sub == "window closed"
    assert "sustainable" not in report.budget_qualifier(ceiling_fixture("window-closed"))


def test_per_day_basis_still_reports_the_rate():
    ceiling = ceiling_fixture("per-day", sustainable_rate_per_day=133.33, remaining_hours=36.0, remaining_days=1.5)
    title, value, _ = report.budget_tile(ceiling)
    assert title == "Sustainable rate"
    assert value == "133 / day"


def test_exhaustion_label_is_suppressed_when_it_falls_after_the_reset():
    assert report.exhaustion_label(ceiling_fixture("final-day")) == "not before reset"


def test_exhaustion_label_is_absent_for_a_closed_window():
    assert report.exhaustion_label(ceiling_fixture("window-closed", exhausts_before_reset=None)) == "-"


def test_a_ceiling_without_a_basis_field_still_renders():
    legacy = {"burn_rate_per_day": 10.0, "sustainable_rate_per_day": 500.0, "remaining_weighted": 100.0}
    assert report.budget_tile(legacy)[0] == "Sustainable rate"
    assert "sustainable" in report.budget_qualifier(legacy)


def build_reports(tmp_path, windows, argv_extra=()):
    data_dir = write_windows(str(tmp_path / "data"), windows)
    report_dir = str(tmp_path / "reports")
    argv = ["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir] + list(argv_extra)
    assert report.main(argv) == 0
    return report_dir


def downgrade_stamp(path, version=0):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    current = '<meta name="report-format-version" content="%d">' % report.REPORT_FORMAT_VERSION
    replacement = '<meta name="report-format-version" content="%d">' % version if version else ""
    replaced = text.replace(current, replacement, 1)
    assert replaced != text
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(replaced)


def two_windows():
    return [make_window(start="2026-08-15", is_current=False), make_window()]


def stale_page_with_narrative(tmp_path, windows, narrative):
    report_dir = str(tmp_path / "reports")
    data_dir = write_windows(str(tmp_path / "data"), windows)
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "week_2026_08_15.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[0], narrative=narrative))
    downgrade_stamp(path)
    return data_dir, report_dir, path


def test_rendered_html_carries_the_format_stamp():
    windows = two_windows()
    page = report.render_html(windows, windows[-1])
    assert '<meta name="report-format-version" content="%d">' % report.REPORT_FORMAT_VERSION in page
    assert '<meta name="report-window" content="week_2026_08_22">' in page
    assert '<meta name="report-window-closed" content="false">' in page


def test_read_stamp_round_trips_version_totals_and_narrative(tmp_path):
    windows = two_windows()
    path = str(tmp_path / "page.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[0], narrative="Opus ate the week.\n\n- batch the reviewers"))
    stamp = report.read_stamp(path)
    assert stamp["format_version"] == report.REPORT_FORMAT_VERSION
    assert stamp["closed"] is True
    assert stamp["turns"] == windows[0]["totals"]["turns"]
    assert abs(stamp["weighted"] - windows[0]["totals"]["weighted"]) < 1e-6
    assert "Opus ate the week." in stamp["narrative"]
    assert "batch the reviewers" in stamp["narrative"]


def test_read_stamp_of_a_missing_page_is_none(tmp_path):
    assert report.read_stamp(str(tmp_path / "nope.html")) is None


def test_read_stamp_of_an_unstamped_page_reports_version_zero(tmp_path):
    path = str(tmp_path / "legacy.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("<!doctype html><html><head><title>old</title></head><body></body></html>")
    stamp = report.read_stamp(path)
    assert stamp["format_version"] == 0
    assert stamp["narrative"] is None


def test_a_page_behind_the_current_version_is_stale(tmp_path):
    windows = two_windows()
    report_dir = build_reports(tmp_path, windows)
    downgrade_stamp(os.path.join(report_dir, "week_2026_08_15.html"))
    assert [w["window"]["key"] for w, _ in report.stale_windows(windows, report_dir)] == ["week_2026_08_15"]


def test_a_window_with_no_page_at_all_is_stale(tmp_path):
    windows = two_windows()
    report_dir = build_reports(tmp_path, [windows[-1]])
    assert [w["window"]["key"] for w, _ in report.stale_windows(windows, report_dir)] == ["week_2026_08_15"]


def test_current_pages_are_not_stale(tmp_path):
    windows = two_windows()
    assert report.stale_windows(windows, build_reports(tmp_path, windows)) == []


def test_main_rebuilds_only_the_page_that_is_behind(tmp_path, capsys):
    windows = two_windows()
    data_dir = write_windows(str(tmp_path / "data"), windows)
    report_dir = str(tmp_path / "reports")
    argv = ["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]
    assert report.main(argv) == 0
    capsys.readouterr()

    stale_page = os.path.join(report_dir, "week_2026_08_15.html")
    downgrade_stamp(stale_page)
    assert report.main(argv) == 0
    assert "rebuilt 1 stale page(s) [week_2026_08_15]" in capsys.readouterr().out
    assert report.read_stamp(stale_page)["format_version"] == report.REPORT_FORMAT_VERSION


def test_a_second_run_rebuilds_nothing(tmp_path, capsys):
    windows = two_windows()
    data_dir = write_windows(str(tmp_path / "data"), windows)
    report_dir = str(tmp_path / "reports")
    argv = ["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]
    report.main(argv)
    capsys.readouterr()
    report.main(argv)
    assert "all 2 pages current" in capsys.readouterr().out


def test_all_forces_a_rebuild_of_every_other_window(tmp_path, capsys):
    windows = two_windows()
    data_dir = write_windows(str(tmp_path / "data"), windows)
    report_dir = str(tmp_path / "reports")
    argv = ["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]
    report.main(argv)
    capsys.readouterr()
    report.main(argv + ["--all"])
    assert "rebuilt 1 stale page(s) [week_2026_08_15]" in capsys.readouterr().out


def test_a_rebuild_reuses_the_embedded_narrative_without_calling_claude(tmp_path, monkeypatch, capsys):
    windows = two_windows()
    data_dir, report_dir, stale_page = stale_page_with_narrative(tmp_path, windows, "Subagents ate the week.")
    calls = []

    def spy(prompt, timeout=180, model=None):
        calls.append(prompt)
        return "fresh", None

    monkeypatch.setattr(report, "fetch_narrative", spy)
    assert report.main(["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]) == 0
    capsys.readouterr()
    assert calls == []
    rebuilt = report.read_stamp(stale_page)
    assert rebuilt["format_version"] == report.REPORT_FORMAT_VERSION
    assert rebuilt["narrative"] == "Subagents ate the week."


def test_refresh_narrative_regenerates_prose_on_a_rebuild(tmp_path, monkeypatch):
    windows = two_windows()
    data_dir, report_dir, stale_page = stale_page_with_narrative(tmp_path, windows, "Old prose.")
    calls = []

    def spy(prompt, timeout=180, model=None):
        calls.append(prompt)
        return "Rewritten prose.", None

    monkeypatch.setattr(report, "fetch_narrative", spy)
    argv = ["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir, "--refresh-narrative"]
    assert report.main(argv) == 0
    assert len(calls) == 1
    assert report.read_stamp(stale_page)["narrative"] == "Rewritten prose."


def test_a_failed_narrative_refresh_falls_back_to_the_embedded_one(tmp_path, monkeypatch, capsys):
    windows = two_windows()
    data_dir, report_dir, stale_page = stale_page_with_narrative(tmp_path, windows, "Old prose.")
    monkeypatch.setattr(report, "fetch_narrative", lambda prompt, timeout=180, model=None: (None, "claude blew up"))
    argv = ["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir, "--refresh-narrative"]
    assert report.main(argv) == 0
    assert "narrative skipped for week_2026_08_15: claude blew up" in capsys.readouterr().err
    assert report.read_stamp(stale_page)["narrative"] == "Old prose."


def test_frozen_data_drift_is_silent_when_a_closed_window_is_unchanged(tmp_path):
    windows = two_windows()
    path = str(tmp_path / "page.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[0]))
    assert report.frozen_data_drift(windows[0], report.read_stamp(path)) is None


def test_frozen_data_drift_reports_a_changed_closed_window(tmp_path):
    windows = two_windows()
    path = str(tmp_path / "page.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[0]))
    moved = make_window(start="2026-08-15", is_current=False, total=windows[0]["totals"]["weighted"] + 5000.0)
    moved["totals"]["turns"] = windows[0]["totals"]["turns"] + 7
    drift = report.frozen_data_drift(moved, report.read_stamp(path))
    assert "weighted" in drift
    assert "turns" in drift


def test_a_current_window_never_reports_drift(tmp_path):
    windows = two_windows()
    path = str(tmp_path / "page.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[-1]))
    moved = make_window(total=windows[-1]["totals"]["weighted"] + 9999.0)
    assert report.frozen_data_drift(moved, report.read_stamp(path)) is None


def test_main_shouts_when_a_closed_window_rebuild_changes_the_numbers(tmp_path, capsys):
    windows = two_windows()
    data_dir = write_windows(str(tmp_path / "data"), windows)
    report_dir = str(tmp_path / "reports")
    argv = ["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]
    report.main(argv)
    capsys.readouterr()

    moved = make_window(start="2026-08-15", is_current=False, total=windows[0]["totals"]["weighted"] + 5000.0)
    write_windows(data_dir, [moved])
    report.main(argv + ["--all"])
    assert "DATA DRIFT in the closed window week_2026_08_15" in capsys.readouterr().err


@pytest.mark.skipif(not os.path.isdir(REAL_DATA), reason="no collected data on this machine")
def test_every_real_page_carries_the_current_stamp():
    real_reports = os.path.join(os.path.dirname(REAL_DATA), "reports")
    pages = sorted(f for f in os.listdir(real_reports)) if os.path.isdir(real_reports) else []
    pages = [f for f in pages if f.endswith(".html")]
    if not pages:
        pytest.skip("no rendered reports")
    behind = [
        page
        for page in pages
        if (report.read_stamp(os.path.join(real_reports, page)) or {}).get("format_version", 0)
        < report.REPORT_FORMAT_VERSION
    ]
    assert behind == []


def strip_narrative_attribute(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    replaced = re.sub(r'<section class="card narrative" data-narrative="[^"]*"', '<section class="card narrative"', text)
    assert replaced != text
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(replaced)


def test_a_legacy_page_still_yields_its_prose(tmp_path):
    windows = two_windows()
    path = str(tmp_path / "legacy.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[0], narrative="Opus ate the week.\n\n- batch the reviewers"))
    strip_narrative_attribute(path)
    downgrade_stamp(path)
    stamp = report.read_stamp(path)
    assert stamp["format_version"] == 0
    assert stamp["narrative"] == "Opus ate the week.\n\n- batch the reviewers"


def test_the_attribute_wins_over_the_rendered_body(tmp_path):
    windows = two_windows()
    path = str(tmp_path / "page.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[0], narrative="Canonical prose."))
    assert report.read_stamp(path)["narrative"] == "Canonical prose."


def test_a_legacy_page_without_a_narrative_reports_none(tmp_path):
    windows = two_windows()
    path = str(tmp_path / "page.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html(windows, windows[0], narrative=None))
    strip_narrative_attribute(path)
    assert report.read_stamp(path)["narrative"] is None


def record(ts, uuid, session="s1", weighted=1000.0, tools=(), prompt=None, agent=None, skill=None, run=None, **extra):
    payload = {
        "ts": ts,
        "uuid": uuid,
        "sessionId": session,
        "model": "claude-opus-5",
        "model_known": True,
        "effort": "high",
        "isSidechain": bool(agent or run),
        "agentId": run,
        "attributionAgent": agent,
        "attributionSkill": skill,
        "cwd": "C:\\workspace\\alpha",
        "gitBranch": "main",
        "version": "2.1.227",
        "input": 1,
        "output": 10,
        "thinking": 0,
        "cache_create": 0,
        "cache_read": 200000,
        "weighted": weighted,
        "tools": [{"name": name, "hash": digest} for name, digest in tools],
        "text_chars": 0,
        "is_api_error": False,
        "prompt": prompt,
    }
    payload.update(extra)
    return payload


def storm_records(prompt="rebase the pricing branch onto main and rerun the failing tests"):
    records = []
    for run in range(4):
        for turn in range(6):
            records.append(
                record(
                    "2026-08-24T%02d:%02d:00+00:00" % (10 + run, turn),
                    "u%d%d" % (run, turn),
                    weighted=25000.0,
                    tools=(("Bash", "b%d%d" % (run, turn)),),
                    prompt=prompt,
                    agent="general-purpose",
                    run="r%d" % run,
                )
            )
    return records


def write_records(data_dir, window, records):
    directory = os.path.join(data_dir, "records")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "%s.jsonl" % window["window"]["key"])
    with open(path, "w", encoding="utf-8") as handle:
        for item in records:
            handle.write(json.dumps(item) + "\n")
    return path


CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())


def analysed(window, records, calls=None):
    import evidence
    import rootcause

    analysis = rootcause.analyse(window, records, CONFIG, calls)
    if analysis is not None:
        analysis["evidence"] = evidence.build(window, records, CONFIG, calls)
    return analysis


def storm_window(records=None, subject="s1", start="2026-08-22", is_current=True):
    records = storm_records() if records is None else records
    sessions = [session("s1", 600000.0, 600000.0, "C:\\workspace\\alpha")]
    findings = [
        finding("subagent_storm", subject, 600000.0, cwd="C:\\workspace\\alpha"),
        finding("agent_type_skew", "general-purpose", 600000.0, turns=24, rank=1),
    ]
    window = make_window(
        start=start, sessions=sessions, findings=findings, total=600000.0, is_current=is_current
    )
    return window, records


def test_the_page_reads_verdict_then_actions_then_findings_then_centres_then_raw():
    window, records = storm_window()
    windows = [window]
    html = report.render_html(windows, window, recommendations=[recommendation()], analysis=analysed(window, records))
    order = [
        html.index('<section class="card verdict">'),
        html.index("<h2>Do these first</h2>"),
        html.index("<h2>Why this week looked like this</h2>"),
        html.index("<h2>Findings</h2>"),
        html.index("<h2>Cost centres</h2>"),
        html.index("<h2>Raw breakdowns</h2>"),
        html.index("<h2>Recommendations</h2>"),
    ]
    assert order == sorted(order)


def test_the_verdict_carries_four_tiles_and_the_burn_line():
    window, records = storm_window()
    window["cost_usd"] = {"usd": 12.5, "sessions": 1, "priced_sessions": 1, "share": 1.0,
                          "label": "list price, as /cost shows it; not what the subscription bills"}
    html = report.render_html([window], window, recommendations=[recommendation()], analysis=analysed(window, records))
    verdict = html.split('<section class="card verdict">')[1].split("</section>")[0]
    assert "Ceiling used" in verdict
    assert "quota unknown this window, ceiling estimated from your own heavy weeks" in verdict
    assert "Weighted spent" in verdict
    assert "$12.50" in verdict
    assert "not what the subscription bills" in verdict
    assert "Unattributed subagent spend" in verdict
    assert "quota 10.0K" in verdict
    assert "reset 2026-08-29" in verdict


def test_the_actions_name_a_threshold_and_link_to_their_chart():
    window, records = storm_window()
    html = report.render_html([window], window, recommendations=[recommendation()], analysis=analysed(window, records))
    actions = html.split("<h2>Do these first</h2>")[1].split("</section>")[0]
    assert "Run trivial claude-opus-5 turns on claude-sonnet-5" in actions
    assert "turns under 250 output tokens with at most 1 tool call" in actions
    assert "Cost is not waste" in actions


def test_the_actions_list_at_most_three():
    window, records = storm_window()
    many = [recommendation(kind="k%d" % index, saving=1000.0 * (10 - index)) for index in range(5)]
    for index, item in enumerate(many):
        item["title"] = "action number %d" % index
    html = report.render_html([window], window, recommendations=many, analysis=analysed(window, records))
    actions = html.split("<h2>Do these first</h2>")[1].split("</section>")[0]
    assert actions.count("action number") == 3
    assert "action number 3" not in actions


def test_the_actions_state_when_nothing_cleared_the_threshold():
    window, records = storm_window()
    html = report.render_html([window], window, recommendations=[], analysis=analysed(window, records))
    assert "No rule cleared the reporting threshold this window" in html


RAW_PANELS = (
    "Every window, main agent vs subagents",
    "Week-over-week change, decomposed by cause",
    "Burn inside this window",
    "Where this window went",
    "Top sessions",
    "Whale turns",
    "Headline tiles",
)


def test_every_old_section_survives_inside_a_collapsed_raw_panel():
    window, records = storm_window()
    window["findings"].append(
        finding(
            "whale_turns",
            "s1",
            500.0,
            uuid="u00",
            ts="2026-08-24T10:00:00+00:00",
            model="claude-opus-5",
            prompt="a prompt",
            tools=["Bash"],
            cwd="C:\\workspace\\alpha",
        )
    )
    windows = [make_window(start="2026-08-15", is_current=False), window]
    html = report.render_html(windows, window, analysis=analysed(window, records))
    raw = html.split('<section class="card raw">')[1]
    for panel in RAW_PANELS:
        assert "<details><summary>%s</summary>" % panel in raw
    assert "<details open" not in html
    assert raw.index("Top sessions") < raw.index("Labelled with the first user prompt")


def test_the_raw_panels_are_the_only_home_of_the_old_sections():
    window, records = storm_window()
    html = report.render_html([window], window, analysis=analysed(window, records))
    head = html.split('<section class="card raw">')[0]
    assert "Where this window went" not in head
    assert "Weighted tokens per local day" not in head
    assert "Labelled with the first user prompt" not in head


def test_a_finding_carries_its_root_cause_line_and_its_evidence():
    window, records = storm_window()
    html = report.render_html([window], window, analysis=analysed(window, records))
    findings = html.split("<h2>Findings</h2>")[1].split("<h2>Cost centres</h2>")[0]
    assert "The work behind it: 4 subagent runs" in findings
    assert "median 6 turns per run" in findings
    assert "<details><summary>Work behind it</summary>" in findings
    assert "job cluster:" in findings
    assert "tool calls are recorded on" in findings


def test_the_findings_section_refuses_to_claim_intent():
    window, records = storm_window()
    html = report.render_html([window], window, analysis=analysed(window, records))
    findings = html.split("<h2>Findings</h2>")[1].split("<h2>Cost centres</h2>")[0]
    assert "never summed" in findings
    assert "nothing here says why anyone chose the work" in findings


def test_a_missing_record_store_is_stated_not_faked():
    window, _ = storm_window()
    html = report.render_html([window], window, analysis=None)
    assert "No records are stored for this window" in html
    assert "cannot be broken into jobs" in html
    assert "not readable" not in html
    assert "could not be read" not in html


def test_the_drill_down_costs_each_job_of_a_vague_cost_centre():
    window, records = storm_window()
    html = report.render_html([window], window, analysis=analysed(window, records))
    drill = html.split("<h2>What the big cost centres did</h2>")[1]
    assert "agent type general-purpose" in drill
    assert "rebase the pricing branch onto main and rerun the failing tests" in drill
    assert "job cluster" in drill
    assert "24 turns across 4 runs" in drill


def test_the_drill_down_states_the_clustering_key_and_the_tool_coverage():
    window, records = storm_window()
    html = report.render_html([window], window, analysis=analysed(window, records))
    assert "Clusters are formed on tool mix, working directory, branch and agent type" in html
    assert "The prompt is used only as a human label" in html
    assert "Tool calls are recorded on 100.0% of this window" in html


def test_the_drill_down_names_a_cluster_it_could_not_label_from_the_prompt():
    window, records = storm_window(records=storm_records(prompt="Base directory for this skill: C:\\x\\y"))
    html = report.render_html([window], window, analysis=analysed(window, records))
    assert "shell commands in alpha on main" in html
    assert "label derived from tools, repo and branch" in html


def test_a_truncated_label_history_is_disclosed():
    window, records = storm_window()
    analysis = analysed(window, records)
    analysis["labels"] = {"observed_max": 160, "configured": 400, "truncated": True}
    html = report.render_html([window], window, analysis=analysis)
    assert "cannot improve labels already stored" in html


def test_a_prompt_reaching_the_root_cause_labels_is_escaped():
    hostile = "rebase <script>alert(1)</script> the pricing branch again right now"
    window, records = storm_window(records=storm_records(prompt=hostile))
    html = report.render_html([window], window, analysis=analysed(window, records))
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html


def test_main_reads_the_record_store_for_the_root_cause_section(tmp_path):
    window, records = storm_window()
    windows = [window]
    data_dir = write_windows(str(tmp_path / "data"), windows)
    write_records(data_dir, window, records)
    report_dir = str(tmp_path / "reports")
    assert report.main(["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]) == 0
    with open(os.path.join(report_dir, "week_2026_08_22.html"), encoding="utf-8") as handle:
        page = handle.read()
    assert "The work behind it: 4 subagent runs" in page


def test_a_window_with_no_record_file_still_renders_through_main(tmp_path):
    window, _ = storm_window()
    data_dir = write_windows(str(tmp_path / "data"), [window])
    report_dir = str(tmp_path / "reports")
    assert report.main(["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]) == 0
    with open(os.path.join(report_dir, "week_2026_08_22.html"), encoding="utf-8") as handle:
        page = handle.read()
    assert "No records are stored for this window" in page
    assert "not readable" not in page


def test_load_records_returns_nothing_for_a_missing_store(tmp_path, capsys):
    window, _ = storm_window()
    assert report.load_records(window, str(tmp_path)) == ([], 0, None)
    analysis, store = report.analysis_for(window, CONFIG, str(tmp_path))
    assert analysis is None
    assert store == {"records": 0, "skipped": 0, "read_error": None, "analysis_error": None}
    assert capsys.readouterr().err == ""


def test_load_records_returns_nothing_for_an_empty_store(tmp_path, capsys):
    window, _ = storm_window()
    write_records(str(tmp_path), window, [])
    assert report.load_records(window, str(tmp_path)) == ([], 0, None)
    analysis, store = report.analysis_for(window, CONFIG, str(tmp_path))
    assert analysis is None
    assert store["records"] == 0 and store["skipped"] == 0
    assert capsys.readouterr().err == ""
    html = report.render_html([window], window, analysis=analysis, store=store)
    assert "No records are stored for this window" in html


def test_a_malformed_line_is_skipped_and_the_later_records_still_load(tmp_path, capsys):
    window, records = storm_window()
    path = write_records(str(tmp_path), window, records)
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    lines.insert(12, '{"uuid": "broken", ')
    Path(path).write_text("".join(line + chr(10) for line in lines), encoding="utf-8")

    loaded, skipped, read_error = report.load_records(window, str(tmp_path))
    assert len(loaded) == len(records)
    assert [item["uuid"] for item in loaded] == [item["uuid"] for item in records]
    assert skipped == 1
    assert read_error is None
    err = capsys.readouterr().err
    assert "line 13: skipping unparseable record" in err
    assert "1 of 25 line(s) skipped as unparseable" in err


def test_a_skipped_line_makes_the_page_state_the_shortfall(tmp_path):
    window, records = storm_window()
    path = write_records(str(tmp_path), window, records)
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    lines.insert(12, "not json at all")
    Path(path).write_text("".join(line + chr(10) for line in lines), encoding="utf-8")

    analysis, store = report.analysis_for(window, CONFIG, str(tmp_path))
    assert analysis is not None
    assert store["skipped"] == 1 and store["records"] == 24
    html = report.render_html([window], window, analysis=analysis, store=store)
    assert "record store holds 25 line(s), of which 1 could not be parsed" in html
    assert "is computed over the 24 record(s) that loaded" in html
    assert "Tool calls are recorded on 100.0% of the turns this page could load" in html


def test_a_healthy_store_states_no_shortfall(tmp_path):
    window, records = storm_window()
    write_records(str(tmp_path), window, records)
    analysis, store = report.analysis_for(window, CONFIG, str(tmp_path))
    html = report.render_html([window], window, analysis=analysis, store=store)
    assert "could not be parsed" not in html
    assert "Tool calls are recorded on 100.0% of this window" in html


def test_an_unreadable_store_says_so_and_reports_the_error(tmp_path, capsys, monkeypatch):
    window, _ = storm_window()
    write_records(str(tmp_path), window, [])

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(report, "open", refuse, raising=False)
    records, skipped, read_error = report.load_records(window, str(tmp_path))
    assert records == [] and skipped == 0
    assert isinstance(read_error, PermissionError)
    assert "could not be read" in capsys.readouterr().err

    monkeypatch.setattr(report, "open", refuse, raising=False)
    analysis, store = report.analysis_for(window, CONFIG, str(tmp_path))
    capsys.readouterr()
    assert analysis is None
    assert store["read_error"] is not None
    html = report.render_html([window], window, analysis=analysis, store=store)
    assert "The record store for this window could not be read" in html
    assert "Permission denied" in html


@pytest.mark.parametrize("error", [KeyError("centres"), TypeError("bad operand"), ValueError("bad share")])
def test_a_failing_analysis_is_reported_and_never_blamed_on_the_store(tmp_path, capsys, monkeypatch, error):
    window, records = storm_window()
    write_records(str(tmp_path), window, records)

    def explode(*args, **kwargs):
        raise error

    monkeypatch.setattr(report.rootcause, "analyse", explode)
    analysis, store = report.analysis_for(window, CONFIG, str(tmp_path))
    err = capsys.readouterr().err
    assert analysis is None
    assert store["records"] == 24
    assert store["read_error"] is None
    assert type(error).__name__ in store["analysis_error"]
    assert "root-cause analysis failed for week_2026_08_22 over 24 readable record(s)" in err
    assert type(error).__name__ in err
    assert "Traceback (most recent call last)" in err

    html = report.render_html([window], window, analysis=analysis, store=store)
    assert "root-cause analysis of this window failed with %s" % type(error).__name__ in html
    assert "a defect in the analysis and not a problem with your records" in html
    assert "not readable" not in html
    assert "could not be read" not in html
    assert "No records are stored for this window" not in html


def test_a_failing_analysis_through_main_still_renders_and_diagnoses(tmp_path, monkeypatch, capsys):
    window, records = storm_window()
    data_dir = write_windows(str(tmp_path / "data"), [window])
    write_records(data_dir, window, records)
    report_dir = str(tmp_path / "reports")
    monkeypatch.setattr(report.rootcause, "analyse", lambda *a, **k: (_ for _ in ()).throw(KeyError("centres")))
    assert report.main(["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]) == 0
    err = capsys.readouterr().err
    assert "root-cause analysis failed for week_2026_08_22" in err
    assert "KeyError" in err
    with open(os.path.join(report_dir, "week_2026_08_22.html"), encoding="utf-8") as handle:
        page = handle.read()
    assert "root-cause analysis of this window failed with KeyError" in page
    assert "not readable" not in page


def test_a_rebuild_gets_root_causes_and_still_calls_no_api(tmp_path, monkeypatch, capsys):
    window, records = storm_window(start="2026-08-15", is_current=False)
    windows = [window, make_window()]
    data_dir, report_dir, stale_page = stale_page_with_narrative(tmp_path, windows, "Subagents ate the week.")
    write_records(data_dir, windows[0], records)
    calls = []
    monkeypatch.setattr(report, "fetch_narrative", lambda *a, **k: (calls.append(a) or ("x", None)))
    assert report.main(["--no-narrative", "--data-dir", data_dir, "--report-dir", report_dir]) == 0
    capsys.readouterr()
    assert calls == []
    with open(stale_page, encoding="utf-8") as handle:
        rebuilt = handle.read()
    assert "The work behind it: 4 subagent runs" in rebuilt
    assert report.read_stamp(stale_page)["narrative"] == "Subagents ate the week."


def test_a_cluster_list_states_how_many_runs_a_description_was_recovered_for():
    import rootcause

    window, records = storm_window()
    for index, record in enumerate(records):
        if index >= 12:
            record["prompt"] = "a different job entirely: audit the courier settlement export"
    calls = {
        "toolu_1": {
            "tool_use_id": "toolu_1",
            "ts": "2026-08-24T09:00:00+00:00",
            "sessionId": records[0]["sessionId"],
            "description": "Rebase the pricing branch",
            "model": "opus",
            "prompt_chars": 400,
            "prompt_head": records[0]["prompt"],
        }
    }
    analysis = rootcause.analyse(window, records, CONFIG, calls)
    html = report.render_html([window], window, analysis=analysis)
    drill = html.split("<h2>What the big cost centres did</h2>")[1]
    assert "Rebase the pricing branch" in drill
    assert "descriptions recovered for 50% of runs" in drill
