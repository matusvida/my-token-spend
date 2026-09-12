import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import evidence
import report
import rootcause

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_report_budget import CONFIG, real_shaped  # noqa: E402


def headroom_finding(unused=1_800_000_000.0):
    return {
        "rule": "headroom",
        "subject": "week_2026_08_22",
        "detail": "the window closed at 30%% of the weekly quota and the one before it at 27%%, so "
        "%s weighted tokens of quota expired unused" % "{:,.0f}".format(unused),
        "weighted_cost": unused,
        "evidence": {
            "method": "quota-fit",
            "ceiling": 2_500_000_000.0,
            "spent": 700_000_000.0,
            "percent_used": 30.0,
            "previous_percent_used": 27.0,
            "max_pct": 60,
            "unused_weighted": unused,
            "components": [],
            "serial_sessions": [],
            "extra_usage": None,
        },
    }


def headroom_recommendation(unused=1_800_000_000.0):
    return {
        "kind": "widen_fan_out",
        "group": "headroom",
        "subject": "session-0001",
        "title": "Let session 0001 run its work side by side",
        "action": "Raise the parallel cap in the orchestrator role file.",
        "detail": "5 runs never overlapped across 119 minutes of run time.",
        "weighted_saving": 0.0,
        "weighted_headroom": 12_083_534.0,
        "percent_of_window": 1.4,
        "performance_risk": "none",
        "confidence": "medium",
        "score": 0.0,
        "evidence": {},
    }


def windowed(with_headroom=True):
    window, records, calls = real_shaped()
    if with_headroom:
        window["findings"].append(headroom_finding())
        entry = window["findings_by_rule"].setdefault("headroom", {"count": 0, "weighted_cost": 0.0})
        entry["count"] += 1
        entry["weighted_cost"] += 1_800_000_000.0
    return window, records, calls


def rendered(window, records, calls, recommendations=None):
    analysis = rootcause.analyse(window, records, CONFIG, calls)
    analysis["evidence"] = evidence.build(window, records, CONFIG, calls)
    store = {"records": len(records), "skipped": 0, "read_error": None, "analysis_error": None}
    return report.render_html(
        [window], window, analysis=analysis, store=store, config=CONFIG, recommendations=recommendations or []
    )


def test_the_headroom_rule_gets_no_finding_card():
    window, records, calls = windowed()
    html = rendered(window, records, calls)
    findings = html.split("<h2>Findings</h2>")[1].split("<h2>Cost centres</h2>")[0]
    assert "expired unused" not in findings
    assert 'id="headroom"' not in findings


def test_under_spend_never_leads_the_cost_rankings():
    window, records, calls = windowed()
    summary = report.console_summary([window], window)
    causes = summary.split("top causes")[1]
    assert "headroom" not in causes
    html = rendered(window, records, calls)
    lenses = html.split("<h3>Largest individual findings</h3>")[0].split("Rule lenses")[1]
    assert "headroom" not in lenses


def test_the_verdict_states_the_unused_quota_and_links_to_its_group():
    window, records, calls = windowed()
    html = rendered(window, records, calls, [headroom_recommendation()])
    verdict = html.split('<section class="card verdict">')[1].split("</section>")[0]
    assert "1.8B unused of your quota" in verdict
    assert "two windows running" in verdict
    assert 'href="#rec-headroom"' in verdict
    assert 'id="rec-headroom"' in html


def test_the_verdict_says_nothing_about_headroom_when_the_rule_is_silent():
    window, records, calls = windowed(with_headroom=False)
    html = rendered(window, records, calls)
    verdict = html.split('<section class="card verdict">')[1].split("</section>")[0]
    assert "unused of your quota" not in verdict
    assert "rec-headroom" not in html


def test_a_headroom_card_shows_its_headroom_not_a_saving():
    window, records, calls = windowed()
    html = rendered(window, records, calls, [headroom_recommendation()])
    group = html.split('id="rec-headroom"')[1]
    assert "12.1M" in group
    assert "headroom, 1.4% of the window" in group
    assert ">0<" not in group


def test_the_recommendations_table_names_the_basis_of_every_figure():
    window, records, calls = windowed()
    from test_report import recommendation

    html = rendered(window, records, calls, [recommendation(), headroom_recommendation()])
    rows = html.split("<summary>Numbers</summary>")[-1]
    assert "<th>basis</th>" in rows
    assert "<td>headroom</td>" in rows
    assert "<td>saving</td>" in rows


def test_the_delta_decomposition_ignores_the_headroom_finding():
    window, records, calls = windowed()
    window["by_repo"].append({"key": None, "turns": 1, "weighted": 5.0, "input": 0, "output": 0,
                              "thinking": 0, "cache_create": 0, "cache_read": 0})
    previous = json.loads(json.dumps(window))
    previous["window"]["key"] = "week_2026_08_15"
    previous["window"]["start"] = "2026-08-15"
    rows = report.decompose_delta(previous, window)
    assert all(row["cause"] != "headroom" for row in rows)


def test_a_headroom_action_card_names_its_threshold_and_its_group():
    window, records, calls = windowed()
    item = dict(headroom_recommendation(), score=1e12)
    html = rendered(window, records, calls, [item])
    actions = html.split("<h2>Do these first</h2>")[1].split("</section>")[0]
    assert "under 60% of the quota in this window and the one before" in actions
    assert 'href="#rec-headroom">see the headroom group' in actions
    assert "headroom, 1.4% of the window" in actions
    assert "12.1M" in actions


def test_a_page_carrying_headroom_still_fits_the_word_cap():
    from test_report_budget import WORD_CAP, _recommendations

    window, records, calls = windowed()
    recommendations = _recommendations(window) + [headroom_recommendation()]
    analysis = rootcause.analyse(window, records, CONFIG, calls)
    analysis["evidence"] = evidence.build(window, records, CONFIG, calls)
    store = {"records": len(records), "skipped": 0, "read_error": None, "analysis_error": None}
    html = report.render_html(
        [window],
        window,
        narrative=" ".join(["prose"] * 400),
        recommendations=recommendations,
        analysis=analysis,
        store=store,
        config=CONFIG,
    )
    words = report.visible_words(html)
    assert words <= WORD_CAP, "visible words: %d" % words
