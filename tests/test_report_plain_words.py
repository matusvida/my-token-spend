import html as html_module
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import report
from test_report_diagnosis import anomaly, pair, render

INTERNAL_NAMES = (
    "attributionSkill",
    "attributionAgent",
    "attributionMcpServer",
    "attributionPlugin",
    "mcp_server",
    "mcp_tool",
    "result_chars",
    "subagent_type",
    "isSidechain",
    "sessionId",
    "tool_use_id",
    "is_api_error",
    "weighted_cost",
    "percent_of_window",
    "totalCostUSD",
    "cost-state",
    "sidechain_weighted",
    "model_known",
)

COVERAGE_FIELDS = (
    ("mcp_server_share", "claude.ai Linear", "mcp_server"),
    ("reply_skill_headless", "prose:reply-style", "attributionSkill"),
    ("unattributed_subagents", "subagents", "attributionAgent"),
    ("failing_tool", "Bash", "tool result status"),
    ("context_growth_tool", "Bash", "tool result sizes"),
    ("repeated_tool_input", "Bash", "tools"),
)


def visible_text(html):
    stripped = re.sub(r"<svg.*?</svg>", " ", html, flags=re.S)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return html_module.unescape(stripped)


def fixture_page():
    previous, window, records = pair()
    window["anomalies"] = [
        dict(
            anomaly(key=key, subject=subject),
            coverage={"field": field, "present": 15, "total": 100, "share": 0.15},
        )
        for key, subject, field in COVERAGE_FIELDS
    ]
    return render(previous, window, records)


def test_no_internal_field_name_reaches_the_reader():
    text = visible_text(fixture_page())
    assert [name for name in INTERNAL_NAMES if name in text] == []


def test_every_coverage_footer_reads_as_plain_words():
    for _, _, field in COVERAGE_FIELDS:
        note = report._coverage_note({"field": field, "present": 15, "total": 100, "share": 0.15})
        assert note.endswith("recorded on 15% of turns.")
        assert "_" not in note
        assert note == note.lower() or note.startswith("MCP")


def test_the_unattributed_tile_note_names_no_field():
    note = report.unattributed_note(900.0, 1000.0)
    assert "subagent_type" not in note
    assert "no agent type" in note


def test_a_footer_saying_full_coverage_is_not_printed_at_all():
    assert report._coverage_note({"field": "attributionSkill", "share": 1.0}) == ""
    card = report._anomaly_card(
        dict(
            anomaly(key="mcp_server_share", subject="claude.ai Linear"),
            coverage={"field": "mcp_server", "present": 10, "total": 10, "share": 1.0},
        )
    )
    assert "chart-note" not in card
