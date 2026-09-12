import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cli
import collect
import paths

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())


@pytest.mark.parametrize(
    "text",
    [
        "You've reached your weekly limit. Your limit resets Monday at 9am.",
        "Weekly limit reached - usage will reset on Monday.",
        "Your usage limit will reset next Monday.",
        "Weekly limit reached. Claude will be available again on Monday.",
        "You are out of usage for now; try again Monday.",
        "Rate limit hit. Access resumes Monday at 02:00.",
    ],
)
def test_each_candidate_phrasing_yields_the_same_weekday(text):
    assert collect.reset_weekday_candidates(text) == {"Monday"}
    assert collect.resolve_reset_weekday(collect.reset_weekday_candidates(text)) == "Monday"


def test_a_weekday_without_a_limit_signal_is_ignored():
    assert collect.reset_weekday_candidates("Let's ship this on Monday at 9am.") == set()


def test_two_different_weekdays_are_ambiguous_and_resolve_to_nothing():
    text = "Your weekly limit resets Monday, though the previous notice said it resets Thursday."
    assert collect.reset_weekday_candidates(text) == {"Monday", "Thursday"}
    assert collect.resolve_reset_weekday(collect.reset_weekday_candidates(text)) is None


def test_no_candidates_resolve_to_nothing():
    assert collect.resolve_reset_weekday(set()) is None


def test_junk_candidates_are_discarded():
    assert collect.resolve_reset_weekday({"Caturday"}) is None
    assert collect.resolve_reset_weekday({"Caturday", "Friday"}) == "Friday"


def transcript(tmp_path, *texts):
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    lines = []
    for text in texts:
        lines.append(json.dumps({"type": "system", "content": text}))
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "timestamp": "2026-08-25T10:00:00.000Z",
                "sessionId": "s1",
                "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 10}},
            }
        )
    )
    (root / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_run_reports_a_confident_detection(tmp_path):
    root = transcript(tmp_path, "Your weekly limit resets Thursday at 08:00.")
    summary = collect.run(CONFIG, root, tmp_path / "out", tmp_path / "out" / "state.json")
    assert summary["reset"]["detected"] == "Thursday"
    assert summary["reset"]["ambiguous"] is False


def test_run_reports_ambiguity_without_picking_a_winner(tmp_path):
    root = transcript(
        tmp_path,
        "Your weekly limit resets Thursday at 08:00.",
        "Your weekly limit resets Sunday at 08:00.",
    )
    summary = collect.run(CONFIG, root, tmp_path / "out", tmp_path / "out" / "state.json")
    assert summary["reset"]["detected"] is None
    assert summary["reset"]["ambiguous"] is True
    assert summary["reset"]["candidates"] == ["Sunday", "Thursday"]


def test_candidates_survive_an_incremental_run_that_rereads_nothing(tmp_path):
    root = transcript(tmp_path, "Your weekly limit resets Thursday at 08:00.")
    state = tmp_path / "out" / "state.json"
    collect.run(CONFIG, root, tmp_path / "out", state)
    again = collect.run(CONFIG, root, tmp_path / "out", state)
    assert again["reset"]["detected"] == "Thursday"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    return paths.ensure_home()


def test_an_ambiguous_detection_never_touches_the_config(home):
    config = cli.load_config(home)
    note = cli.apply_detection(home, config, {"candidates": ["Monday", "Friday"], "detected": None, "ambiguous": True})
    assert "changed nothing" in note
    assert cli.load_config(home)["reset_weekday"] == config["reset_weekday"]
    assert cli.load_config(home)["reset_weekday_confirmed"] is False


def test_a_confident_detection_corrects_the_config_and_records_it(home):
    config = cli.load_config(home)
    config["reset_weekday"] = "Saturday"
    cli.save_config(home, config)
    note = cli.apply_detection(home, cli.load_config(home), {"candidates": ["Thursday"], "detected": "Thursday", "ambiguous": False})
    assert "Saturday to Thursday" in note
    stored = cli.load_config(home)
    assert stored["reset_weekday"] == "Thursday"
    assert stored["reset_weekday_source"] == "detected"
    assert stored["reset_weekday_corrections"][0]["from"] == "Saturday"
    assert stored["reset_weekday_corrections"][0]["to"] == "Thursday"


def test_a_detection_matching_the_config_changes_nothing(home):
    config = cli.load_config(home)
    assert cli.apply_detection(home, config, {"detected": config["reset_weekday"], "ambiguous": False}) is None
    assert "reset_weekday_corrections" not in cli.load_config(home)


def test_the_unconfirmed_banner_disappears_once_the_user_answers(home):
    assert "RESET DAY NOT CONFIRMED" in cli.unconfirmed_banner(cli.load_config(home))
    cli.main(["status", "--set-reset-weekday", "wednesday"])
    stored = cli.load_config(home)
    assert stored["reset_weekday"] == "Wednesday"
    assert stored["reset_weekday_source"] == "user"
    assert cli.unconfirmed_banner(stored) is None


def test_an_invalid_weekday_is_rejected(home):
    assert cli.main(["status", "--set-reset-weekday", "Caturday"]) == 1
    assert cli.load_config(home)["reset_weekday_confirmed"] is False
