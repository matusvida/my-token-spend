import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import report
import rules
from test_report import analysed, finding, make_window, recommendation, session, storm_window

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())


def anomaly(key="mcp_server_share", subject="claude.ai Linear", score=2.0, chart=None):
    return {
        "key": key,
        "subject": subject,
        "claim": "the %s MCP server cost 92,799,908 weighted tokens over 860 turns" % subject,
        "numbers": {"turns": 860},
        "basis": "5.3% of the window on one server",
        "score": score,
        "action": "Narrow what %s is asked for over its 860 turns." % subject,
        "coverage": {"field": "mcp_server", "present": 14, "total": 100, "share": 0.14},
        "chart": chart or {"kind": "bars", "rows": [{"label": subject, "value": 10.0}]},
    }


def pair(previous_total=200000.0, current_total=600000.0):
    window, records = storm_window()
    previous = make_window(
        start="2026-08-15",
        sessions=[session("s0", previous_total, previous_total / 2, "C:\\workspace\\alpha")],
        findings=[],
        total=previous_total,
        is_current=False,
    )
    return previous, window, records


def render(previous, window, records, **kwargs):
    windows = [previous, window] if previous else [window]
    return report.render_html(windows, window, analysis=analysed(window, records), config=CONFIG, **kwargs)


def section_of(html, heading):
    return html.split("<h2>%s</h2>" % heading)[1].split("</section>")[0]


def test_the_change_block_is_the_second_thing_on_the_page():
    previous, window, records = pair()
    html = render(previous, window, records, recommendations=[recommendation()])
    assert html.index('<section class="card verdict">') < html.index("What changed")
    assert html.index("What changed") < html.index("<h2>Findings</h2>")


def test_the_change_block_leads_with_a_claim_built_from_the_data():
    previous, window, records = pair()
    html = render(previous, window, records)
    block = html.split("What changed")[1].split("</section>")[0]
    assert "movement between the two windows" in block


def test_the_change_block_reconciles_to_the_total():
    previous, window, records = pair()
    block = render(previous, window, records).split("What changed")[1].split("</section>")[0]
    assert "accounted" in block and "total" in block


def test_the_change_chart_colours_its_bars_by_cause():
    previous, window, records = pair()
    block = render(previous, window, records).split("What changed")[1].split("</section>")[0]
    assert 'fill="var(--series-' in block
    assert "subagent storm" in block


def test_the_change_chart_draws_at_most_eight_bars_plus_everything_else():
    previous, window, records = pair()
    sessions = [
        session("s%d" % index, 1000.0 * (index + 1), 0.0, "C:\\workspace\\repo%d" % index)
        for index in range(14)
    ]
    window["by_session"] = sessions
    window["delta"] = None
    block = render(previous, window, records).split("What changed")[1].split("</section>")[0]
    wide = block.split('chart-wrap wide')[1].split('</div>')[0]
    assert wide.count('class="mark"') <= 9
    assert "everything else" in block


def test_a_window_with_no_previous_window_says_so_in_one_line():
    _, window, records = pair()
    window["delta"] = {
        "previous": None,
        "comparable": False,
        "reason": "no previous window to compare",
        "rows": [],
        "total": None,
        "accounted": 0.0,
        "claim": None,
    }
    block = render(None, window, records).split("What changed")[1].split("</section>")[0]
    assert "no previous window to compare" in block
    assert "chart-wrap" not in block


def test_the_anomaly_lane_sits_between_the_change_block_and_the_findings():
    previous, window, records = pair()
    window["anomalies"] = [anomaly()]
    html = render(previous, window, records)
    assert html.index("What changed") < html.index("What looks wrong")
    assert html.index("What looks wrong") < html.index("<h2>Findings</h2>")


def test_an_anomaly_card_carries_its_claim_action_coverage_and_chart():
    previous, window, records = pair()
    window["anomalies"] = [anomaly()]
    block = render(previous, window, records).split("What looks wrong")[1].split("</section>")[0]
    assert "92,799,908" in block
    assert "Narrow what claude.ai Linear is asked for" in block
    assert "14%" in block
    assert "<svg" in block


def test_an_anomaly_table_chart_renders_as_a_table():
    previous, window, records = pair()
    window["anomalies"] = [
        anomaly(chart={"kind": "table", "columns": ["run", "repeats"], "rows": [["poll the deploy", "101"]]})
    ]
    block = render(previous, window, records).split("What looks wrong")[1].split("</section>")[0]
    assert "poll the deploy" in block
    assert "<table" in block


def test_at_most_five_anomaly_cards_render_and_the_rest_are_counted():
    previous, window, records = pair()
    window["anomalies"] = [anomaly(key="k%d" % index, subject="server %d" % index, score=10 - index) for index in range(8)]
    block = render(previous, window, records).split("What looks wrong")[1].split("</section>")[0]
    assert block.count('id="anomaly-') == 5
    assert "3 more" in block
    assert "server 5" not in block


def test_anomaly_cards_render_in_score_order():
    previous, window, records = pair()
    window["anomalies"] = [anomaly(key="low", subject="quiet", score=1.1), anomaly(key="high", subject="loud", score=9.0)]
    block = render(previous, window, records).split("What looks wrong")[1].split("</section>")[0]
    assert block.index("loud") < block.index("quiet")


def test_a_window_with_no_anomaly_prints_one_line():
    previous, window, records = pair()
    window["anomalies"] = []
    block = render(previous, window, records).split("What looks wrong")[1].split("</section>")[0]
    assert "nothing outside the usual pattern this week" in block.lower()
    assert "anomaly" not in block.replace('id="anomalies"', "")


def test_the_removed_sections_are_absent_from_the_page():
    previous, window, records = pair()
    html = render(previous, window, records, recommendations=[recommendation()])
    assert "Effort tiers" not in html
    assert "<h3>Plugins</h3>" not in html
    assert "<h2>Headline tiles</h2>" not in html
    assert "<details><summary>Headline tiles</summary>" not in html
    assert "<h2>Recommendations</h2>" not in html


def test_the_cost_centres_keep_only_jobs_servers_and_skills():
    previous, window, records = pair()
    centres = render(previous, window, records).split("<h2>Cost centres</h2>")[1].split("</section>")[0]
    assert "MCP servers" in centres
    assert "Skills" in centres
    assert "<h3>Repos</h3>" not in centres
    assert "<h3>Models</h3>" not in centres


def test_the_whale_finding_has_no_card_but_keeps_its_raw_table():
    previous, window, records = pair()
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
    html = render(previous, window, records)
    findings = html.split("<h2>Findings</h2>")[1].split("</section>")[0]
    assert "whale" not in findings.lower()
    assert "<details><summary>Whale turns</summary>" in html


def test_the_word_cap_is_eleven_hundred():
    assert report.WORD_CAP == 1100


def advised(kind, title, saving, percent_of_window, subject=None):
    item = recommendation(kind=kind, saving=saving)
    item["title"] = title
    item["percent_of_window"] = percent_of_window
    if subject is not None:
        item["subject"] = subject
    return item


def test_last_weeks_advice_shows_the_figure_then_and_now():
    previous, window, records = pair()
    html = render(previous, window, records, recommendations=[recommendation()])
    block = html.split("<h2>Do these first</h2>")[1].split("</section>")[0]
    assert "Last week" in block


def test_a_recommendation_that_barely_moved_reads_as_unchanged():
    rows = report.advice_movement(
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 20.0)],
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 20.01)],
    )
    assert rows[0]["movement_text"] == "unchanged"


def test_a_recommendation_that_grew_reports_the_points_it_moved():
    rows = report.advice_movement(
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 10.0)],
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 18.0)],
    )
    assert rows[0]["movement"] == 8.0
    assert "+8.0 points" in rows[0]["movement_text"]


def test_a_recommendation_that_stopped_firing_reads_as_zero_now():
    rows = report.advice_movement(
        [advised("reset_context", "Clear the context", 1000.0, 12.0)],
        [],
    )
    assert rows[0]["now"] == 0.0


def test_new_this_week_holds_only_new_or_grown_recommendations():
    previous_items = [advised("model_downgrade", "Downgrade", 1000.0, 10.0)]
    current = [
        advised("model_downgrade", "Downgrade", 1000.0, 11.0),
        advised("reset_context", "Clear the context", 900.0, 9.0),
        advised("deduplicate_reads", "Dedupe", 800.0, 30.0),
    ]
    fresh = report.new_recommendations(previous_items, current)
    assert [item["kind"] for item in fresh] == ["deduplicate_reads", "reset_context"]


def test_a_recommendation_that_grew_by_more_than_five_points_counts_as_new():
    previous_items = [advised("model_downgrade", "Downgrade", 1000.0, 10.0)]
    current = [advised("model_downgrade", "Downgrade", 1000.0, 16.5)]
    assert [item["kind"] for item in report.new_recommendations(previous_items, current)] == ["model_downgrade"]


def test_new_this_week_holds_at_most_two():
    current = [advised("k%d" % index, "title %d" % index, 1000.0, 30.0 - index) for index in range(5)]
    assert len(report.new_recommendations([], current)) == 2


def test_the_page_says_so_when_nothing_is_new_this_week():
    previous, window, records = pair()
    html = render(previous, window, records, recommendations=[])
    block = html.split("<h2>Do these first</h2>")[1].split("</section>")[0]
    assert "Nothing new this week" in block


def test_the_page_order_is_verdict_change_anomalies_findings_centres_advice_raw():
    previous, window, records = pair()
    window["anomalies"] = [anomaly()]
    html = render(previous, window, records, recommendations=[recommendation()])
    order = [
        html.index('<section class="card verdict">'),
        html.index("What changed"),
        html.index("What looks wrong"),
        html.index("<h2>Findings</h2>"),
        html.index("<h2>Cost centres</h2>"),
        html.index("<h2>Do these first</h2>"),
        html.index("<h2>Raw breakdowns</h2>"),
    ]
    assert order == sorted(order)


def test_the_narrative_context_is_plain_text_of_the_claims():
    previous, window, records = pair()
    window["anomalies"] = [anomaly()]
    context = report.narrative_context(window, previous)
    assert "MCP server cost 92,799,908" in context
    assert "<" not in context
    assert "movement between the two windows" in context


def test_the_narrative_context_is_empty_when_there_is_nothing_to_say():
    _, window, _ = pair()
    window["anomalies"] = []
    window["delta"] = {"comparable": False, "reason": "no previous window to compare", "rows": [], "claim": None}
    assert report.narrative_context(window, None) == ""


def test_the_narrative_prompt_is_handed_the_delta_and_anomaly_claims():
    previous, window, records = pair()
    window["anomalies"] = [anomaly()]
    prompt = report.build_narrative_prompt(window, previous, extra_context=report.narrative_context(window, previous))
    assert "What changed this window and what looks wrong" in prompt
    assert "MCP server cost 92,799,908" in prompt


def test_the_failing_tool_action_links_to_the_round_trips_table_on_the_page():
    previous, window, records = pair()
    window["anomalies"] = [
        dict(
            anomaly(key="failing_tool", subject="Bash"),
            action=rules.failing_tool_action("Bash"),
            anchor="round_trips",
        )
    ]
    records = records + [
        dict(records[0], uuid="f%d" % index, tools=[{"name": "Bash", "hash": "h%d" % index,
             "result_chars": 10, "is_error": True, "denied": False}])
        for index in range(6)
    ]
    html = render(previous, window, records)
    lane = html.split("What looks wrong")[1].split("</section>")[0]
    assert 'href="#round_trips"' in lane
    assert 'id="round_trips"' in html
    assert html.index('id="round_trips"') > html.index('href="#round_trips"')


def test_the_failing_tool_action_drops_the_pointer_when_no_table_is_rendered():
    previous, window, records = pair()
    window["anomalies"] = [
        dict(
            anomaly(key="failing_tool", subject="Bash"),
            action=rules.failing_tool_action("Bash"),
            anchor="round_trips",
        )
    ]
    html = report.render_html([previous, window], window, analysis=None, config=CONFIG)
    lane = html.split("What looks wrong")[1].split("</section>")[0]
    assert "round trips table" not in lane
    assert "See which runs the failing Bash calls came from." in lane


def test_two_recommendations_of_one_kind_are_compared_subject_by_subject():
    previous_items = [
        advised("model_downgrade", "Run opus-5 turns on sonnet", 1000.0, 11.5, subject="claude-opus-5"),
        advised("model_downgrade", "Run fable-5-1 turns on sonnet", 100.0, 0.3, subject="claude-fable-5-1"),
    ]
    current = [
        advised("model_downgrade", "Run opus-5 turns on sonnet", 2000.0, 16.7, subject="claude-opus-5"),
    ]
    rows = {row["title"]: row for row in report.advice_movement(previous_items, current)}
    assert rows["Run opus-5 turns on sonnet"]["now"] == 16.7
    assert abs(rows["Run opus-5 turns on sonnet"]["movement"] - 5.2) < 1e-9
    assert rows["Run fable-5-1 turns on sonnet"]["now"] == 0.0
    assert abs(rows["Run fable-5-1 turns on sonnet"]["movement"] + 0.3) < 1e-9


def test_a_recommendation_of_a_kind_seen_before_on_another_subject_is_new():
    previous_items = [advised("model_downgrade", "Run opus-5 turns on sonnet", 1000.0, 11.5, subject="claude-opus-5")]
    current = [advised("model_downgrade", "Run fable turns on sonnet", 900.0, 9.0, subject="claude-fable-5-1")]
    assert [item["subject"] for item in report.new_recommendations(previous_items, current)] == [
        "claude-fable-5-1"
    ]
def test_a_recommendation_carded_as_new_is_not_also_a_movement_row():
    previous_recs = [advised("right_size_fan_out", "Right-size the biggest fan-outs", 1000.0, 9.4)]
    current_recs = [advised("right_size_fan_out", "Right-size the biggest fan-outs", 5000.0, 31.2)]
    assert [item["title"] for item in report.new_recommendations(previous_recs, current_recs)] == [
        "Right-size the biggest fan-outs"
    ]
    block = report._actions_section(current_recs, set(), CONFIG, previous_recs)
    assert block.count("Right-size the biggest fan-outs") == 1
    assert "carded as new below" in block


def test_one_piece_of_advice_is_tracked_across_windows_by_its_own_title():
    previous_items = [advised("reset_context", "Clear the context", 1000.0, 7.2, subject="session-aaa")]
    current = [advised("reset_context", "Clear the context", 3000.0, 15.1, subject="session-bbb")]
    rows = report.advice_movement(previous_items, current)
    assert rows[0]["now"] == 15.1
    assert abs(rows[0]["movement"] - 7.9) < 1e-9


def test_a_movement_row_survives_when_the_recommendation_is_not_carded_as_new():
    previous_recs = [advised("right_size_fan_out", "Right-size the biggest fan-outs", 1000.0, 9.4)]
    current_recs = [advised("right_size_fan_out", "Right-size the biggest fan-outs", 1100.0, 10.4)]
    block = report._actions_section(current_recs, set(), CONFIG, previous_recs)
    assert "Right-size the biggest fan-outs" in block
    assert "Nothing new this week" in block


def test_a_row_that_fell_to_zero_reports_its_movement_not_unchanged():
    rows = report.advice_movement(
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 0.8)],
        [],
    )
    assert rows[0]["then"] == 0.8
    assert rows[0]["now"] == 0.0
    assert rows[0]["movement_text"] == "-0.8 points"


def test_unchanged_is_reserved_for_a_movement_that_rounds_to_zero():
    rows = report.advice_movement(
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 20.0)],
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 20.02)],
    )
    assert rows[0]["movement_text"] == "unchanged"


def test_a_movement_of_one_point_is_printed_not_called_unchanged():
    rows = report.advice_movement(
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 20.0)],
        [advised("model_downgrade", "Run trivial turns on sonnet", 1000.0, 21.0)],
    )
    assert rows[0]["movement_text"] == "+1.0 points"


def priced(window, sessions, priced_sessions, usd=12.5):
    window["cost_usd"] = {
        "usd": usd,
        "sessions": sessions,
        "priced_sessions": priced_sessions,
        "crossing_sessions": 0,
    }
    return window


def test_the_list_price_tile_stays_when_coverage_clears_the_floor():
    previous, window, records = pair()
    priced(window, 80, 60)
    html = render(previous, window, records)
    verdict = html.split('<section class="card verdict">')[1].split("</section>")[0]
    assert "List price" in verdict
    assert "$12.50" in verdict


def test_a_window_under_the_price_floor_shows_unused_quota_instead():
    previous, window, records = pair()
    priced(window, 81, 14)
    window["ceiling"] = dict(window["ceiling"], method="quota-fit", estimate=2000000.0, samples_used=9)
    html = render(previous, window, records)
    verdict = html.split('<section class="card verdict">')[1].split("</section>")[0]
    assert "List price" not in verdict
    assert "unavailable" not in verdict
    assert "Unused quota" in verdict


def test_a_window_with_no_quota_and_no_price_counts_sessions_instead():
    previous, window, records = pair()
    priced(window, 81, 14)
    window["ceiling"] = dict(window["ceiling"], method="top-cluster", estimate=2000000.0)
    html = render(previous, window, records)
    verdict = html.split('<section class="card verdict">')[1].split("</section>")[0]
    assert "List price" not in verdict
    assert "Sessions" in verdict


def test_the_price_coverage_line_moves_into_the_raw_breakdowns():
    previous, window, records = pair()
    priced(window, 81, 14)
    html = render(previous, window, records)
    raw = html.split("<h2>Raw breakdowns</h2>")[1]
    assert "14 of 81 sessions" in raw
    assert "List price" in raw


def failing_records(base, count=6, agent="general-purpose", cwd="/workspace/alpha"):
    return [
        dict(
            base,
            uuid="f%d" % index,
            attributionAgent=agent,
            cwd=cwd,
            tools=[{"name": "Bash", "hash": "h%d" % index, "result_chars": 10,
                    "is_error": True, "denied": False}],
        )
        for index in range(count)
    ]


def test_the_round_trips_table_names_where_each_tool_failed():
    previous, window, records = pair()
    records = records + failing_records(records[0])
    html = render(previous, window, records)
    table = html.split("<h2>Raw breakdowns</h2>")[1].split('id="round_trips"')[1]
    assert "most often in" in table
    assert "general-purpose in alpha" in table


def test_the_failing_tool_action_promises_only_what_the_table_shows():
    previous, window, records = pair()
    window["anomalies"] = [
        dict(anomaly(key="failing_tool", subject="Bash"),
             action=rules.failing_tool_action("Bash"), anchor="round_trips")
    ]
    records = records + failing_records(records[0])
    html = render(previous, window, records)
    lane = html.split("What looks wrong")[1].split("</section>")[0]
    assert "fix the call site" not in lane
    assert "which runs the failing Bash calls came from" in lane


def diverging_rows():
    return [
        {"phrase": "subagent storm in product-promotion-service", "delta": 4.4e8, "tip": "a"},
        {"phrase": "context bloat in dynamic-pricing", "delta": -1.2e8, "tip": "b"},
    ]


def test_the_narrow_delta_chart_stacks_the_label_above_the_bar():
    import charts

    svg = charts.svg_diverging_bars(diverging_rows(), stacked=True)
    assert 'viewBox="0 0 %d' % charts.NARROW_WIDTH in svg
    assert 'text-anchor="end"' not in svg.split("</text>")[0]
    assert svg.count('class="mark"') == 2


def test_the_narrow_bars_start_inside_the_narrow_viewbox():
    import charts
    import re

    svg = charts.svg_diverging_bars(diverging_rows(), stacked=True)
    starts = [float(match) for match in re.findall(r'd="M(-?[\d.]+) ', svg)]
    assert starts
    assert all(0 < start < charts.NARROW_WIDTH for start in starts)


def test_the_change_chart_ships_a_wide_and_a_narrow_rendering():
    previous, window, records = pair()
    block = html_of(render(previous, window, records))
    assert 'class="chart-wrap wide"' in block
    assert 'class="chart-wrap narrow"' in block


def html_of(html):
    return html.split('id="what-changed"')[1].split("</section>")[0]


def test_the_stylesheet_swaps_the_two_renderings_at_narrow_widths():
    previous, window, records = pair()
    html = render(previous, window, records)
    assert ".chart-wrap.narrow { display: none; }" in html
    assert ".chart-wrap.wide { display: none; }" in html
    assert ".chart-wrap.narrow svg.chart { min-width: 0; }" in html


def test_a_stored_anomaly_action_is_reworded_at_render_time():
    previous, window, records = pair()
    window["anomalies"] = [
        dict(
            anomaly(key="failing_tool", subject="Bash"),
            action="Read the failing Bash calls in the round trips table and fix the call site.",
            anchor="round_trips",
        )
    ]
    records = records + failing_records(records[0])
    lane = render(previous, window, records).split("What looks wrong")[1].split("</section>")[0]
    assert "fix the call site" not in lane
    assert "which runs the failing Bash calls came from" in lane
