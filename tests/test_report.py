import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cost
import report
import rules


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
        "analysis_version": rules.ANALYSIS_VERSION,
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


def test_the_context_chart_footer_states_how_the_turns_were_binned():
    chart = {
        "kind": "context_series",
        "series": [["2026-08-25T14:00:00+00:00", 200000, 1000, "Bash"]] * 300,
        "series_points": 5233,
        "turns": 5233,
        "threshold": 150000,
        "compactions": [],
        "by_tool": [{"tool": "Bash", "tokens": 4667251, "results": 2129}],
        "top_results": [],
        "coverage": {},
    }
    html = report._context_chart_html(chart)
    assert "5,233 turns binned to 300 points, each point the max of its bin" in html


def test_near_identical_whale_turns_collapse_into_one_counted_row():
    prompt = "<task-notification> task 41ab finished"
    findings = [
        finding("whale_turns", "s1", 6166060.0, ts="2026-09-06T05:40:09+00:00", prompt=prompt, model="opus"),
        finding("whale_turns", "s1", 6166060.0, ts="2026-09-06T05:40:10+00:00", prompt=prompt, model="opus"),
        finding("whale_turns", "s1", 6135000.0, ts="2026-09-06T05:40:12+00:00", prompt=prompt, model="opus"),
        finding("whale_turns", "s2", 5396616.0, ts="2026-09-06T09:53:26+00:00", prompt="build the report", model="opus"),
    ]
    html = report._whales_section(make_window(findings=findings))
    body = html.split("<tbody>")[1]
    assert body.count("<tr>") == 2
    assert "3 near-identical" in body
    assert "6,166,060" in body


def test_whale_turns_at_a_different_cost_stay_separate_rows():
    prompt = "<task-notification> task 41ab finished"
    findings = [
        finding("whale_turns", "s1", 6166060.0, ts="2026-09-06T05:40:09+00:00", prompt=prompt, model="opus"),
        finding("whale_turns", "s1", 4000000.0, ts="2026-09-06T05:40:10+00:00", prompt=prompt, model="opus"),
    ]
    body = report._whales_section(make_window(findings=findings)).split("<tbody>")[1]
    assert body.count("<tr>") == 2
    assert "near-identical" not in body


def _ceiling(estimate, method, **extra):
    return dict(
        {"estimate": estimate, "method": method, "approximate": True, "percent_used": 52.4,
         "samples_used": 9, "band_pct": 3.0, "cluster_size": 2, "windows_considered": 4},
        **extra
    )


def test_a_closed_window_says_the_fitted_ceiling_was_applied_retroactively():
    window = make_window(is_current=False)
    window["ceiling"] = _ceiling(3.3e9, "quota-fit")
    verdict = report.render_html([window], window).split('<section class="card verdict">')[1]
    assert "applied to this closed window once the fit existed" in verdict


def test_an_open_window_does_not_claim_a_retroactive_ceiling():
    window = make_window()
    window["ceiling"] = _ceiling(3.3e9, "quota-fit")
    verdict = report.render_html([window], window).split('<section class="card verdict">')[1]
    assert "applied to this closed window" not in verdict


def test_the_ceiling_change_notice_renders_once(tmp_path):
    window = make_window()
    window["ceiling"] = _ceiling(1.43e9, "top-cluster")
    report.write_report([window], window, None, str(tmp_path))
    window["ceiling"] = _ceiling(3.3e9, "quota-fit")
    path = report.write_report([window], window, None, str(tmp_path))
    changed = Path(path).read_text(encoding="utf-8")
    assert "ceiling changed since this page was last rendered: 1.4B estimated to 3.3B fitted" in changed
    again = Path(report.write_report([window], window, None, str(tmp_path))).read_text(encoding="utf-8")
    assert "ceiling changed since this page was last rendered" not in again


def test_narrative_is_injected_when_present():
    windows = [make_window()]
    html = report.render_html(windows, windows[0], narrative="Subagents ate the week.\n\n- fewer reviewers")
    assert "Subagents ate the week." in html
    assert "<li>fewer reviewers</li>" in html


def test_narrative_absence_renders_no_heading_and_one_header_line():
    windows = [make_window()]
    page = report.render_html(windows, windows[0], narrative=None, narrative_note="claude CLI not on PATH")
    assert "Why this week looked like this" not in page
    header = page.split("<header>")[1].split("</header>")[0]
    assert "narrative off: claude not on PATH for the scheduled run" in header
    assert page.count("narrative off:") == 1


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
        stdout = b"  it was the subagents  "
        stderr = b""

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


def test_fetch_narrative_decodes_utf8_output_and_asks_for_plain_text(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "claude")
    seen = {}

    class Result:
        returncode = 0
        stdout = "opus — the week".encode("utf-8")
        stderr = b""

    def spy(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(report.subprocess, "run", spy)
    text, error = report.fetch_narrative("hi")
    assert error is None
    assert text == "opus — the week"
    assert "--output-format" in seen["command"] and "text" in seen["command"]
    assert seen["kwargs"]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert not seen["kwargs"].get("text")


def test_fetch_narrative_replaces_undecodable_bytes_rather_than_failing(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "claude")

    class Result:
        returncode = 0
        stdout = "opus ".encode("utf-8") + bytes([0x97]) + " the week".encode("utf-8")
        stderr = b""

    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: Result())
    text, error = report.fetch_narrative("hi")
    assert error is None
    assert text.startswith("opus ")


def test_the_narrative_prompt_caps_the_answer_at_three_sentences():
    current = make_window()
    prompt = report.build_narrative_prompt(current, None, [])
    assert "at most 3 sentences" in prompt
    assert "at most 2 sentences" in report.build_narrative_prompt(current, None, [], sentences=2)


def test_the_narrative_prompt_forbids_a_preamble_around_the_paragraph():
    prompt = report.build_narrative_prompt(make_window(), None, [])
    assert "no preamble, no word or sentence count" in prompt


def test_the_narrative_prompt_carries_the_extra_context_it_is_handed():
    current = make_window()
    prompt = report.build_narrative_prompt(current, None, [], extra_context="the Linear MCP doubled")
    assert "the Linear MCP doubled" in prompt


def test_an_over_long_narrative_is_refused_and_asked_again_with_a_shorter_cap(monkeypatch):
    windows = [make_window()]
    answers = [" ".join(["word"] * 100), "It was the subagents."]
    asked = []

    def fake(prompt, timeout=180, model=None):
        asked.append(prompt)
        return answers[len(asked) - 1], None

    monkeypatch.setattr(report, "fetch_narrative", fake)
    text, error = report.narrative_for(windows, windows[0], CONFIG, recommendations=[])
    assert text == "It was the subagents."
    assert error is None
    assert len(asked) == 2
    assert "at most 2 sentences" in asked[1]


def test_a_narrative_still_over_the_cap_after_the_retry_is_dropped(monkeypatch):
    windows = [make_window()]
    monkeypatch.setattr(report, "fetch_narrative", lambda prompt, timeout=180, model=None: ("One. Two. Three. Four. Five.", None))
    text, error = report.narrative_for(windows, windows[0], CONFIG, recommendations=[])
    assert text is None
    assert "sentences" in error


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
        html.index("<h2>Findings</h2>"),
        html.index("<h2>Cost centres</h2>"),
        html.index("<h2>Raw breakdowns</h2>"),
        html.index("<h2>Recommendations</h2>"),
    ]
    assert order == sorted(order)


def test_the_list_price_tile_states_its_session_count_and_boundary_crossers():
    window, records = storm_window()
    window["cost_usd"] = {
        "usd": 12.5,
        "sessions": 4,
        "priced_sessions": 3,
        "crossing_sessions": 1,
        "share": 0.75,
        "label": cost.LABEL,
    }
    html = report.render_html([window], window, recommendations=[recommendation()], analysis=analysed(window, records))
    verdict = html.split('<section class="card verdict">')[1].split("</section>")[0]
    assert "3 sessions, 1 crossing a window boundary" in verdict


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
    assert "estimated ceiling 10.0K" in verdict
    assert "reset 2026-08-29" in verdict


def test_the_actions_name_a_threshold_and_link_to_their_chart():
    window, records = storm_window()
    html = report.render_html([window], window, recommendations=[recommendation()], analysis=analysed(window, records))
    actions = html.split("<h2>Do these first</h2>")[1].split("</section>")[0]
    assert "Run trivial claude-opus-5 turns on claude-sonnet-5" in actions
    assert "under 250 output tokens, at most 1 tool call" in actions
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
    assert "say what the work was, never why" in findings


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


def test_labels_that_share_a_prefix_lose_it_so_the_tail_distinguishes_them():
    import charts

    shortened = charts.shorten_labels(
        [
            "shell commands with file writes in product-promotion-service on cti-12375-promo",
            "shell commands with file writes in workspace on HEAD",
            "shell commands with file writes in srst-service on CTI-12629",
        ],
        charts.LABEL_CHARS,
    )
    assert len(set(shortened)) == 3
    assert all(label.startswith("...") for label in shortened)
    assert shortened[1] == "...workspace on HEAD"


def test_labels_that_share_a_suffix_lose_it_so_the_head_distinguishes_them():
    import charts

    shortened = charts.shorten_labels(
        [
            "shell commands with file writes in product-promotion-service on cti-12375-promo",
            "file reads and searches in product-promotion-service on cti-12375-promo",
            "file writes plus dbqt MCP calls in product-promotion-service on cti-12375-promo",
        ],
        charts.LABEL_CHARS,
    )
    assert len(set(shortened)) == 3
    assert shortened[1] == "file reads and searches..."


def test_labels_sharing_nothing_are_left_alone_apart_from_the_cap():
    import charts

    assert charts.shorten_labels(["Collapse the call", "Quantify the fan-out"], 46) == [
        "Collapse the call",
        "Quantify the fan-out",
    ]


def _timeline(minutes, span_days):
    runs = []
    for index in range(4):
        day = 22 + index * span_days
        runs.append(
            {
                "label": "job number %d" % index,
                "named": True,
                "first_ts": "2026-08-%02dT09:00:00+00:00" % day,
                "last_ts": "2026-08-%02dT09:%02d:00+00:00" % (day, minutes),
                "turns": 6 + index,
                "weighted": 1000.0 * (index + 1),
                "agent": "general-purpose",
            }
        )
    return {"kind": "run_timeline", "runs": runs, "hidden": 0, "overlap": {}, "descriptions": {"described": 4, "runs": 4}}


def test_the_run_timeline_draws_hour_ticks_when_the_runs_are_visible_on_the_clock():
    import charts

    chart = _timeline(minutes=50, span_days=0)
    assert charts.timeline_mode(chart) == "clock"
    svg = charts.svg_run_timeline(chart)
    assert svg.count('class="axis-label"') >= 3
    assert "09:10" in svg or "09:15" in svg or "09:30" in svg


def test_a_timeline_whose_runs_are_all_slivers_falls_back_to_runs_by_turns():
    import charts

    chart = _timeline(minutes=2, span_days=3)
    assert charts.timeline_mode(chart) == "turns"
    svg = charts.svg_run_timeline(chart)
    assert svg.index("job number 3") < svg.index("job number 0")
    html = report._timeline_chart_html(chart)
    assert "Runs by turns, largest first" in html
    assert "too thin to place on the clock" in html


def test_a_legend_names_only_the_token_classes_the_bars_actually_draw():
    import evidence

    chart = {
        "kind": "whale_bars",
        "series": list(evidence.WHALE_SERIES),
        "rows": [
            {"label": "05:40", "model": "claude-opus-5", "agent": "main agent", "weighted": 100.0,
             "parts": [0.0, 100.0, 0.0, 0.0]}
        ],
    }
    html = report._whale_chart_html(chart)
    assert "context written to cache" in html
    assert "context re-read from cache" not in html
    assert "fresh input" not in html


def test_a_timeline_whose_runs_are_all_named_shows_no_derived_swatch():
    chart = _timeline(minutes=50, span_days=0)
    html = report._timeline_chart_html(chart)
    assert "label derived from tools" not in html
    assert "named by the orchestrator" not in html
    chart["runs"][0]["named"] = False
    both = report._timeline_chart_html(chart)
    assert "label derived from tools" in both
    assert "named by the orchestrator" in both


def test_the_mismatch_strip_plots_output_against_thinking_with_the_rule_box_in_the_corner():
    import charts

    chart = {
        "kind": "scatter",
        "points": [
            {"tools": 1, "output": 120, "thinking": 40, "model": "claude-opus-5"},
            {"tools": 1, "output": 900, "thinking": 700, "model": "claude-sonnet-5"},
        ],
        "models": ["claude-opus-5", "claude-sonnet-5"],
        "box": {"output": 250, "tools": 1, "thinking": 200},
        "axis_max": 1000,
        "thinking_max": 800,
        "above_axis": 0,
        "plotted": 2,
        "total": 2,
    }
    svg = charts.svg_scatter(chart)
    assert "counted: 250 output, 200 thinking, 1 tool call" in svg
    assert "output tokens, thinking up the side" in svg
    assert "tool calls on the turn" not in svg
    assert svg.count('class="dot"') == 2
    corner = svg.index("counted: 250 output")
    assert 'text-anchor="end"' in svg[corner - 80 : corner]


def test_the_burn_chart_zero_fills_every_calendar_day_from_the_window_start():
    window = make_window(start="2026-08-22", total=600000.0)
    window["by_day"] = [
        {"date": "2026-08-24", "weighted": 200000.0, "turns": 4, "cache_read": 10, "output": 5},
        {"date": "2026-08-26", "weighted": 400000.0, "turns": 6, "cache_read": 20, "output": 7},
    ]
    days = report.calendar_days(window)
    assert [day["date"] for day in days] == [
        "2026-08-22",
        "2026-08-23",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
    ]
    assert days[0]["weighted"] == 0.0
    assert days[3]["weighted"] == 0.0
    assert report._burn_chart(window).count("weighted spent so far") == 5


def test_the_mismatch_chart_survives_a_config_without_a_thinking_threshold():
    import copy

    import evidence
    import rules

    config = copy.deepcopy(CONFIG)
    del config["thresholds"]["model_mismatch"]["max_thinking"]
    records = [
        record("2026-08-22T10:0%d:00+00:00" % index, "u%d" % index, tools=[{"name": "Read", "hash": "h"}])
        for index in range(3)
    ]
    for entry in records:
        entry.update({"output": 100, "thinking": 20, "model": "claude-opus-5"})
    chart = evidence._mismatch_chart(records, config)
    assert chart["box"]["thinking"] == rules.MAX_THINKING_DEFAULT


def test_runs_sharing_one_derived_label_are_told_apart_by_their_start_clock():
    import charts

    runs = [
        {
            "label": "file reads and searches in pps on cti-12375",
            "named": False,
            "first_ts": "2026-09-08T1%d:00:00+00:00" % index,
            "last_ts": "2026-09-08T1%d:40:00+00:00" % index,
            "turns": 5,
            "weighted": 100.0,
            "agent": "general-purpose",
        }
        for index in range(3)
    ]
    labels = charts.run_labels(runs)
    assert len(set(labels)) == 3
    assert labels[0].endswith(" 10:00")
    runs[0]["label"] = "a different job entirely"
    assert charts.run_labels(runs)[0] == "a different job entirely"


def test_a_long_bars_value_label_is_drawn_inside_the_bar():
    import charts

    rows = [
        {"label": "biggest", "value": 1000.0, "tip": "t"},
        {"label": "middling", "value": 300.0, "tip": "t"},
        {"label": "tiny", "value": 20.0, "tip": "t"},
    ]
    svg = charts.svg_ranked_bars(rows, label_width=280)
    labels = re.findall(r'<text class="value-label( inside)?" x="([0-9.]+)"', svg)
    assert [item[0] for item in labels] == [" inside", " inside", ""]
    assert float(labels[0][1]) == float(labels[1][1]) == 290.0
    assert float(labels[2][1]) < 290.0 + charts.INSIDE_LABEL_MIN


def test_whale_labels_carry_seconds_and_a_turn_number_when_the_minute_repeats():
    import evidence

    records = [
        record("2026-09-05T05:40:%02d+00:00" % (9 + index), "w%d" % index, weighted=100.0)
        for index in range(3)
    ]
    findings = [finding("whale_turns", "s1", 100.0, uuid="w%d" % index) for index in range(3)]
    chart = evidence._whale_chart(records, findings, CONFIG)
    assert [row["label"] for row in chart["rows"]] == ["05:40:09", "05:40:10", "05:40:11"]
    assert [row["sublabel"] for row in chart["rows"]] == ["#1", "#2", "#3"]
    html = report._whale_chart_html(chart)
    assert "05:40:09" in html and "#1" in html


def test_whale_labels_stay_at_minute_resolution_when_nothing_collides():
    import evidence

    records = [
        record("2026-09-05T0%d:40:00+00:00" % (5 + index), "w%d" % index, weighted=100.0)
        for index in range(2)
    ]
    findings = [finding("whale_turns", "s1", 100.0, uuid="w%d" % index) for index in range(2)]
    chart = evidence._whale_chart(records, findings, CONFIG)
    assert [row["label"] for row in chart["rows"]] == ["05:40", "06:40"]
    assert [row.get("sublabel") for row in chart["rows"]] == [None, None]
    assert "05:40:00" in report._whale_chart_html(chart)


def test_the_burn_axis_tops_out_near_the_window_it_draws():
    import charts

    svg = charts.svg_burn(["09-05"], [887_015_163.0], 1_283_915_540.0, "reset", "estimated ceiling")
    ticks = [float(value) * 1e9 for value in re.findall(r'class="tick"[^>]*>([0-9.]+)B<', svg)]
    assert max(ticks) >= 1_283_915_540.0
    assert max(ticks) <= 1_283_915_540.0 * 1.1
    assert "estimated ceiling 1.3B" in svg


def test_the_burn_axis_still_clears_a_series_above_the_ceiling():
    import charts

    svg = charts.svg_burn(["09-05"], [1_739_908_484.0], 1_283_915_540.0, "reset")
    ticks = [float(value) * 1e9 for value in re.findall(r'class="tick"[^>]*>([0-9.]+)B<', svg)]
    assert max(ticks) >= 1_739_908_484.0
    assert max(ticks) <= 1_739_908_484.0 * 1.1


def test_the_context_axis_is_labelled_by_turn_order():
    import charts

    chart = {
        "kind": "context_series",
        "series": [
            ["2026-09-05T09:16:00+00:00", 100000, 10, "Bash"],
            ["2026-09-05T10:24:00+00:00", 150000, 20, "Bash"],
            ["2026-09-05T16:53:00+00:00", 200000, 30, "Bash"],
        ],
        "threshold": 150000,
        "compactions": [],
        "by_tool": [{"tool": "Bash", "tokens": 60, "results": 3}],
        "top_results": [],
        "coverage": {},
    }
    svg = charts.svg_context_series(chart)
    assert "turn 1 (09:16)" in svg
    assert "turn 3 (16:53)" in svg
    html = report._context_chart_html(chart)
    assert "x is turn order, not a clock" in html


def test_a_timeline_whose_median_run_is_a_sliver_falls_back_to_runs_by_turns():
    import charts

    runs = [
        {
            "label": "short job %d" % index,
            "named": True,
            "first_ts": "2026-09-05T1%d:00:00+00:00" % index,
            "last_ts": "2026-09-05T1%d:20:00+00:00" % index,
            "turns": 3 + index,
            "weighted": 100.0,
            "agent": "general-purpose",
        }
        for index in range(5)
    ]
    runs.append(
        {
            "label": "the overnight run",
            "named": True,
            "first_ts": "2026-09-05T19:43:00+00:00",
            "last_ts": "2026-09-08T07:40:00+00:00",
            "turns": 400,
            "weighted": 900.0,
            "agent": "general-purpose",
        }
    )
    chart = {
        "kind": "run_timeline",
        "runs": runs,
        "hidden": 0,
        "overlap": {},
        "descriptions": {"described": 6, "runs": 6},
    }
    assert charts.timeline_mode(chart) == "turns"
    svg = charts.svg_run_timeline(chart)
    assert svg.index("the overnight run") < svg.index("short job 4")
    html = report._timeline_chart_html(chart)
    assert "most runs are too thin to place on the clock" in html
    runs[5]["last_ts"] = "2026-09-05T21:43:00+00:00"
    assert charts.timeline_mode(chart) == "clock"


def test_the_run_timeline_marks_day_boundaries_when_it_spans_days():
    import charts

    runs = [
        {
            "label": "job %d" % index,
            "named": True,
            "first_ts": "2026-09-0%dT02:00:00+00:00" % (5 + index),
            "last_ts": "2026-09-0%dT20:00:00+00:00" % (5 + index),
            "turns": 5,
            "weighted": 100.0,
            "agent": "general-purpose",
        }
        for index in range(3)
    ]
    chart = {
        "kind": "run_timeline",
        "runs": runs,
        "hidden": 0,
        "overlap": {},
        "descriptions": {"described": 3, "runs": 3},
    }
    assert charts.timeline_mode(chart) == "clock"
    svg = charts.svg_run_timeline(chart)
    assert svg.count('class="grid day"') == 2


def test_the_scatter_draws_the_minority_model_last():
    import charts

    points = [{"tools": 1, "output": 100, "thinking": 10, "model": "claude-opus-5"} for _ in range(5)]
    points.append({"tools": 1, "output": 900, "thinking": 700, "model": "claude-sonnet-5"})
    points.append({"tools": 1, "output": 120, "thinking": 20, "model": "claude-opus-5"})
    chart = {
        "kind": "scatter",
        "points": points,
        "models": ["claude-opus-5", "claude-sonnet-5"],
        "box": {"output": 250, "tools": 1, "thinking": 200},
        "axis_max": 1000,
        "thinking_max": 800,
        "above_axis": 0,
        "plotted": len(points),
        "total": len(points),
    }
    svg = charts.svg_scatter(chart)
    assert svg.index("claude-sonnet-5\n") > svg.rindex("claude-opus-5\n")


def test_the_scatter_region_edges_sit_exactly_on_the_thresholds():
    import charts

    chart = {
        "kind": "scatter",
        "points": [{"tools": 1, "output": 250, "thinking": 200, "model": "claude-opus-5"}],
        "models": ["claude-opus-5"],
        "box": {"output": 250, "tools": 1, "thinking": 200},
        "axis_max": 1000,
        "thinking_max": 800,
        "above_axis": 0,
        "plotted": 1,
        "total": 1,
    }
    svg = charts.svg_scatter(chart)
    region = re.search(r'<rect class="region" x="([0-9.]+)" y="([0-9.]+)" width="([0-9.]+)" height="([0-9.]+)"', svg)
    dot = re.search(r'<circle class="dot" cx="([0-9.]+)" cy="([0-9.]+)"', svg)
    assert abs(float(region.group(1)) + float(region.group(3)) - float(dot.group(1))) < 0.01
    assert abs(float(region.group(2)) - float(dot.group(2))) < 0.01
    assert abs(float(region.group(2)) + float(region.group(4)) - (320 - charts.MARGIN["bottom"])) < 0.01
    assert float(region.group(1)) == charts.MARGIN["left"]


def test_the_burn_endpoint_label_stays_on_the_canvas_when_the_series_fills_the_axis():
    import charts

    svg = charts.svg_burn(["09-05"], [1_739_908_484.0], 1_283_915_540.0, "reset")
    dot = float(re.search(r'class="end-dot" cx="[0-9.]+" cy="(-?[0-9.]+)"', svg).group(1))
    label = float(re.search(r'<text class="value-label" x="[0-9.]+" y="(-?[0-9.]+)"', svg).group(1))
    assert label - charts.VALUE_LABEL_ASCENT >= 0.0
    assert label > dot


def test_the_burn_endpoint_label_sits_above_the_dot_when_there_is_room():
    import charts

    svg = charts.svg_burn(["09-05"], [400_000_000.0], 1_283_915_540.0, "reset")
    dot = float(re.search(r'class="end-dot" cx="[0-9.]+" cy="(-?[0-9.]+)"', svg).group(1))
    label = float(re.search(r'<text class="value-label" x="[0-9.]+" y="(-?[0-9.]+)"', svg).group(1))
    assert label < dot


def test_a_single_repeat_group_is_not_reported_in_the_plural():
    import advice

    findings = [finding("redundant_reads", "Bash", 3_300_000.0)]
    findings[0]["evidence"] = {"tool": "Bash", "occurrences": 9}
    detail = advice._deduplicate_reads(findings)[0]["detail"]
    assert detail.startswith("1 repeat group,")


def test_an_equal_tool_call_range_collapses_to_exactly_one():
    import text

    assert text.bounded(1, 1, "tool call") == "exactly 1 tool call"
    assert text.bounded(1, 3, "tool call") == "between 1 and 3 tool calls"
    assert text.bounded(2, 2, "tool call") == "exactly 2 tool calls"


def test_the_mismatch_detail_collapses_a_degenerate_tool_call_range():
    import copy

    config = copy.deepcopy(CONFIG)
    config["thresholds"]["model_mismatch"]["max_tool_calls"] = 1
    records = [
        record("2026-08-22T10:0%d:00+00:00" % index, "u%d" % index, tools=[{"name": "Read", "hash": "h"}])
        for index in range(3)
    ]
    for entry in records:
        entry.update({"output": 100, "thinking": 20, "model": "claude-opus-5"})
    import rules

    detail = rules.model_mismatch(records, config)[0]["detail"]
    assert "exactly 1 tool call," in detail
    assert "tool call(s)" not in detail
def _bloat_chart():
    tools = ["Read", "Bash", "Grep", "Write", "Agent", "Edit", "Glob"]
    series = [
        ["2026-08-22T09:%02d:00+00:00" % index, 100000 + index, 10, leader]
        for index, leader in enumerate(tools + [None, "unattributed"])
    ]
    return {
        "kind": "context_series",
        "series": series,
        "threshold": 150000,
        "compactions": [],
        "by_tool": [{"tool": name, "tokens": 100 - index, "results": 1} for index, name in enumerate(tools)],
        "top_results": [],
    }


def test_the_context_legend_names_every_colour_the_chart_draws():
    import charts

    chart = _bloat_chart()
    drawn = set(re.findall(r'class="mark"[^>]*fill="var\((--[a-z0-9-]+)\)"', charts.svg_context_series(chart)))
    html = report._context_chart_html(chart)
    legended = set(re.findall(r'class="swatch" style="background:var\((--[a-z0-9-]+)\)"', html))
    assert legended == drawn
    assert charts.OTHER in drawn
    assert "other or unattributed" in html


def test_the_context_legend_leaves_out_a_tool_that_never_led_a_turn():
    import charts

    chart = _bloat_chart()
    chart["series"] = [point for point in chart["series"] if point[3] != "Grep"]
    html = report._context_chart_html(chart)
    legended = set(re.findall(r'class="swatch" style="background:var\((--[a-z0-9-]+)\)"', html))
    assert legended == set(re.findall(r'class="mark"[^>]*fill="var\((--[a-z0-9-]+)\)"', charts.svg_context_series(chart)))
    assert "Grep" not in re.search(r'<div class="legend">.*?</div>', html, re.S).group(0)


def test_clusters_sharing_one_label_are_told_apart_in_the_deep_dive_table():
    centre = {
        "weighted": 500.0,
        "clusters": [
            {
                "label": "Multi-Expert MR Review Design reference",
                "mixed": False,
                "runs": 1,
                "turns": 10,
                "weighted": 100.0 * (index + 1),
                "median_turns": 10.0,
                "tools": {},
                "confidence": "single run",
                "first_ts": "2026-08-22T1%d:00:00+00:00" % index,
            }
            for index in range(3)
        ],
    }
    names = [row[0] for row in report._cluster_rows(centre)]
    assert len(set(names)) == 3
    assert names[0].endswith(" 10:00")


def test_a_cluster_with_a_label_of_its_own_keeps_it_whole():
    centre = {
        "weighted": 200.0,
        "clusters": [
            {
                "label": label,
                "mixed": False,
                "runs": 1,
                "turns": 10,
                "weighted": 100.0,
                "median_turns": 10.0,
                "tools": {},
                "confidence": "single run",
                "first_ts": "2026-08-22T10:00:00+00:00",
            }
            for label in ("Collapse the call", "Quantify the fan-out")
        ],
    }
    assert [row[0] for row in report._cluster_rows(centre)] == ["Collapse the call", "Quantify the fan-out"]


def test_two_recommendations_clipping_alike_keep_their_differing_tails():
    import charts

    titles = [
        "Right-size the Explore agent",
        "Stop re-reading the same Bash input",
        "Stop re-reading the same Read input",
    ]
    assert charts.distinct_labels(titles, 26) == [
        "Right-size the Explore...",
        "...Bash input",
        "...Read input",
    ]


def _whale_bars(parts):
    import evidence

    return {
        "kind": "whale_bars",
        "rows": [
            {
                "label": "05:4%d" % index,
                "turn": index + 1,
                "model": "claude-opus-5",
                "agent": "main agent",
                "weighted": sum(row),
                "parts": list(row),
            }
            for index, row in enumerate(parts)
        ],
        "series": list(evidence.WHALE_SERIES),
    }


def _swatches(html):
    box = re.search(r'<div class="legend">.*?</div>\s*<div class="chart-wrap"', html, re.S)
    return re.findall(r'class="swatch" style="background:var\((--[a-z0-9-]+)\)"', box.group(0) if box else "")


def test_the_whale_legend_drops_a_class_that_is_all_but_invisible():
    chart = _whale_bars([[0.0, 900_000.0, 1_000.0, 20.0], [0.0, 900_000.0, 1_000.0, 20.0]])
    html = report._whale_chart_html(chart)
    assert _swatches(html) == ["--series-1"]
    assert "Smaller classes omitted." in html


def test_the_whale_legend_keeps_every_class_that_is_readable():
    chart = _whale_bars([[0.0, 600_000.0, 300_000.0, 100_000.0]])
    html = report._whale_chart_html(chart)
    assert _swatches(html) == ["--series-1", "--series-2", "--series-3"]
    assert "Smaller classes omitted." not in html


def test_a_lone_mismatched_turn_is_not_reported_in_the_plural():
    import copy
    import rules

    config = copy.deepcopy(CONFIG)
    records = [
        record("2026-08-22T10:00:00+00:00", "u0", weighted=9e8, tools=[{"name": "Read", "hash": "h"}])
    ]
    records[0].update({"output": 100, "thinking": 20, "model": "claude-opus-5", "cache_read": 2_000_000})
    detail = rules.model_mismatch(records, config)[0]["detail"]
    assert detail.startswith("1 claude-opus-5 turn produced")
    assert "it would have cost" in detail


def test_identical_labels_are_not_eaten_by_their_own_shared_affixes():
    import charts

    labels = ["# Multi-Expert MR Review Design reference: specs/mr-review"] * 3
    assert charts.shorten_labels(labels, 40) == [charts.clip(labels[0], 40)] * 3
    stamps = ["2026-08-22T1%d:09:00+00:00" % index for index in range(3)]
    assert charts.distinct_labels(labels, 40, stamps) == [
        charts.clip(labels[0], 40) + " 1%d:09" % index for index in range(3)
    ]


def test_the_burn_ceiling_label_sits_clear_of_the_endpoint_value():
    import charts

    svg = charts.svg_burn(["09-05"], [1_739_908_484.0], 1_283_915_540.0, "reset", "estimated ceiling")
    reference = re.search(r'<text class="reference-label" x="([0-9.]+)"[^>]*text-anchor="(\w+)">estimated', svg)
    assert reference.group(2) == "start"
    assert float(reference.group(1)) < charts.PLOT_WIDTH / 2


def test_the_whale_legend_drops_a_class_that_clears_the_share_but_not_a_pixel():
    chart = _whale_bars([[0.0, 6_010_198.0, 34_500.0, 10.0]] * 5)
    html = report._whale_chart_html(chart)
    assert _swatches(html) == ["--series-1"]
    assert "Smaller classes omitted." in html
