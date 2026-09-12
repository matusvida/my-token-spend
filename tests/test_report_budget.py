import json
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect
import evidence
import report
import rootcause

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
WORD_CAP = 1500

TOOLS = ["Bash", "Read", "Grep", "Edit", "Write", "Agent", "Skill", "WebFetch", "Glob"]
AGENTS = ["general-purpose", "Explore", "code-analyst", "mr-scout", "review-verifier"]
REPOS = ["C:\\workspace\\alpha", "C:\\workspace\\beta", "C:\\workspace\\gamma"]
MCP = [None, None, "claude.ai Linear", "dbqt", "datadog-mcp"]
PLUGINS = [None, None, "prose", "datadog", "my-token-spend"]
SKILLS = [None, None, "review-mr", "dataviz", "glab"]
DESCRIPTIONS = [
    "Collapse the pricing service to one master-data call",
    "Quantify the fan-out amplification",
    "Audit the promo dataset on test21",
    "Map the pricing seam to the export",
    "Implement the ladder discount",
    "Rebase the pricing branch",
    "Chase the failing settlement export",
    "Draft the migration plan",
    "Review the courier payout diff",
    "Trace the duplicated master-data reads",
    "Split the nightly recalculation",
    "Explain the cache-read spike",
    "Rewrite the tune proposal text",
    "Check the QA rollout",
    "Summarise the incident timeline",
]


PROMPTS = [
    "%s, and then rerun the affected checks before handing the branch back" % description
    for description in DESCRIPTIONS
]


def _record(rng, index, session, ts, sidechain, run, agent, cache_read, tools):
    model = "claude-opus-5" if rng.random() < 0.7 else "claude-sonnet-5"
    output = rng.randrange(20, 2400)
    entry = {
        "ts": ts,
        "uuid": "u%05d" % index,
        "sessionId": session,
        "model": model,
        "model_known": True,
        "effort": "high",
        "isSidechain": sidechain,
        "agentId": run,
        "attributionAgent": agent,
        "attributionSkill": rng.choice(SKILLS),
        "mcp_server": rng.choice(MCP),
        "mcp_tool": None,
        "plugin": rng.choice(PLUGINS),
        "per_turn_effort": None,
        "stop_reason": "end_turn",
        "cache_create_5m": 100,
        "cache_create_1h": None,
        "compacted": False,
        "after_compaction": False,
        "source_tool_use_id": None,
        "cwd": rng.choice(REPOS),
        "gitBranch": "master",
        "version": "2.1.227",
        "input": rng.randrange(10, 900),
        "output": output,
        "cache_create": rng.randrange(1000, 40000),
        "cache_read": cache_read,
        "thinking": rng.randrange(0, 1800),
        "tools": tools,
        "text_chars": 400,
        "is_api_error": rng.random() < 0.01,
        "prompt": PROMPTS[index % len(PROMPTS)],
    }
    weights = CONFIG["token_class_weights"]
    model_weight = 5.0 if model == "claude-opus-5" else 1.0
    entry["weighted"] = model_weight * (
        weights["input"] * entry["input"]
        + weights["output"] * entry["output"]
        + weights["cache_create"] * entry["cache_create"]
        + weights["cache_read"] * entry["cache_read"]
    )
    return entry


def real_shaped():
    rng = random.Random(11)
    records = []
    calls = {}
    index = 0
    minute = 0
    for session_number in range(4):
        session = "session-%04d" % session_number
        cache_read = 40000
        for turn in range(150):
            minute += 1
            sidechain = turn % 3 != 0
            run = "run-%d-%02d" % (session_number, turn // 9) if sidechain else None
            agent = AGENTS[(turn // 9) % len(AGENTS)] if sidechain else None
            cache_read = min(900000, cache_read + rng.randrange(0, 9000))
            tools = []
            for slot in range(rng.randrange(0, 3)):
                name = rng.choice(TOOLS)
                tools.append(
                    {
                        "name": name,
                        "hash": "h%02d" % rng.randrange(0, 40),
                        "tool_use_id": "call-%05d-%d" % (index, slot),
                        "result_chars": rng.randrange(10, 90000),
                        "is_error": rng.random() < 0.05,
                        "denied": rng.random() < 0.01,
                    }
                )
            ts = "2026-08-%02dT%02d:%02d:00+00:00" % (24 + minute // 1440, (minute // 60) % 24, minute % 60)
            records.append(_record(rng, index, session, ts, sidechain, run, agent, cache_read, tools))
            index += 1
    for number, description in enumerate(DESCRIPTIONS):
        calls["toolu_%02d" % number] = {
            "tool_use_id": "toolu_%02d" % number,
            "ts": "2026-08-24T00:00:00+00:00",
            "sessionId": "session-%04d" % (number % 4),
            "description": description,
            "subagent_type": "general-purpose",
            "model": "claude-opus-5",
            "prompt_chars": 500,
            "prompt_head": PROMPTS[number % len(PROMPTS)],
        }
    for record in records:
        if record["agentId"]:
            number = int(record["agentId"].split("-")[-1])
            record["prompt"] = PROMPTS[number % len(PROMPTS)]
    stats = {"files_scanned": 40, "malformed_lines": 0}
    ceiling = {"estimate": 2.5e9, "method": "quota-fit", "approximate": True, "cluster_size": 0,
               "windows_considered": 3, "samples_used": 9, "band_pct": 3.0,
               "latest_pct": 61.0, "latest_pct_is_fresh": False}
    window = collect.aggregate_window(date(2026, 8, 22), records, CONFIG, stats, ceiling)
    window["cost_usd"] = {"usd": 482.17, "sessions": 4, "priced_sessions": 4, "share": 1.0,
                          "label": "list price, as /cost shows it; not what the subscription bills"}
    return window, records, calls


def rendered(window, records, calls, **kwargs):
    analysis = rootcause.analyse(window, records, CONFIG, calls)
    analysis["evidence"] = evidence.build(window, records, CONFIG, calls)
    store = {"records": len(records), "skipped": 0, "read_error": None, "analysis_error": None}
    return report.render_html([window], window, analysis=analysis, store=store, config=CONFIG, **kwargs)


def test_the_fixture_is_shaped_like_a_real_window():
    window, records, calls = real_shaped()
    assert len(records) == 600
    assert {finding["rule"] for finding in window["findings"]} >= {
        "context_bloat",
        "subagent_storm",
        "agent_type_skew",
        "model_mismatch",
        "whale_turns",
    }
    assert window["by_mcp_server"] and window["by_plugin"]
    assert window["context"]["sessions"]


def test_the_visible_page_stays_under_the_word_cap():
    window, records, calls = real_shaped()
    html = rendered(
        window,
        records,
        calls,
        recommendations=_recommendations(window),
        narrative=" ".join(["prose"] * 400),
    )
    words = report.visible_words(html)
    assert words <= WORD_CAP, "visible words: %d" % words


def test_a_long_narrative_is_clipped_to_its_cap():
    window, records, calls = real_shaped()
    html = rendered(window, records, calls, narrative=" ".join("word%d" % index for index in range(400)))
    body = html.split("<h2>Why this week looked like this</h2>")[1].split("</section>")[0]
    assert "word119" in body
    assert "word120" not in body
    assert report.NARRATIVE_WORDS == 120


def _recommendations(window):
    import advice

    return advice.recommend(window, window["findings"], CONFIG)


def test_details_bodies_do_not_count_but_their_summaries_do():
    page = "<main><p>one two</p><details><summary>three</summary><p>four five six</p></details></main>"
    assert report.visible_words(page) == 3


def test_nested_details_are_stripped_whole():
    page = "<main><p>one</p><details><summary>two</summary><details><summary>three</summary>" "<p>four</p></details></details></main>"
    assert report.visible_words(page) == 3


def test_every_rule_puts_its_own_evidence_on_the_page():
    window, records, calls = real_shaped()
    html = rendered(window, records, calls)
    findings = html.split("<h2>Findings</h2>")[1].split("<h2>Cost centres</h2>")[0]
    assert 'id="context_bloat-0"' in findings
    assert 'id="subagent_storm-0"' in findings
    assert 'id="agent_type_skew-0"' in findings
    assert 'id="model_mismatch"' in findings
    assert 'id="whale_turns"' in findings
    assert 'id="round_trips"' in findings
    assert "threshold" in findings
    assert "counted: 250 output, 200 thinking, 1 tool call" in findings
    assert "trivial" not in findings
    assert "tool calls on the turn" not in findings
    assert "colour is the tool that grew it" in findings
    assert "x is turn order, not a clock" in findings


def test_the_cost_centres_rank_six_lanes_and_state_their_coverage():
    window, records, calls = real_shaped()
    html = rendered(window, records, calls)
    centres = html.split("<h2>Cost centres</h2>")[1].split("<h2>Raw breakdowns</h2>")[0]
    for lane in (
        "Jobs, by dispatch description where recovered",
        "MCP servers",
        "Plugins",
        "Skills",
        "Repos",
        "Models",
    ):
        assert lane in centres
    assert "descriptions recovered for" in centres
    assert centres.count("recorded on") >= 5
    assert "smaller job clusters" not in centres
    assert "more general-purpose runs in" in centres
    assert "label derived from tools" in centres


def test_the_top_finding_names_a_job_by_its_description():
    window, records, calls = real_shaped()
    html = rendered(window, records, calls)
    assert any(description in html for description in DESCRIPTIONS)


def test_the_page_keeps_no_external_reference():
    import re

    window, records, calls = real_shaped()
    html = rendered(window, records, calls, recommendations=_recommendations(window))
    assert not re.search(r"src\s*=", html)
    assert all(href.startswith("#") for href in re.findall(r'href\s*=\s*"([^"]*)"', html))
    for marker in ("<img", "<link", "@import", "url("):
        assert marker not in html


def test_both_themes_are_defined():
    window, records, calls = real_shaped()
    html = rendered(window, records, calls)
    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="dark"]' in html
    assert '[data-theme="light"]' in html
    for slot in range(1, 9):
        assert html.count("--series-%d:" % slot) == 3


def test_the_headroom_group_renders_only_when_a_headroom_recommendation_exists():
    window, records, calls = real_shaped()
    plain = rendered(window, records, calls, recommendations=_recommendations(window))
    assert "Headroom" not in plain
    item = dict(_recommendations(window)[0])
    item.update({"group": "headroom", "kind": "upgrade_tier", "title": "Upgrade the review agents"})
    with_headroom = rendered(window, records, calls, recommendations=[item])
    assert "Headroom - quota you did not use" in with_headroom
    assert "Upgrade the review agents" in with_headroom


def test_the_burn_reference_line_is_named_by_the_ceiling_method():
    window, records, calls = real_shaped()
    measured = rendered(window, records, calls)
    assert ">quota 2.5B<" in measured
    assert "estimated ceiling 2.5B" not in measured
    window["ceiling"]["method"] = "top-cluster"
    estimated = rendered(window, records, calls)
    assert "estimated ceiling 2.5B" in estimated
    assert ">quota 2.5B<" not in estimated


def test_the_effort_ranking_states_what_its_share_is_measured_on():
    window, records, calls = real_shaped()
    html = rendered(window, records, calls)
    block = html.split("<h3>Effort tiers</h3>")[1].split("<h3>")[0]
    assert "recorded on 100% of turns." in block
    assert window["field_coverage"]["effort"]["share"] == 1.0


def test_no_ranking_asserts_a_share_with_an_empty_subtitle():
    import re

    window, records, calls = real_shaped()
    html = rendered(window, records, calls)
    assert '<p class="sub"></p>' not in html
    assert re.search(r"<h3>Skills</h3><p class=\"sub\">Recorded on \d+% of turns\.</p>", html)
    assert "<h3>Models</h3><p class=\"sub\">Recorded on every turn.</p>" in html
