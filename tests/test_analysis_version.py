import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect
import rules

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
STATS = {"files_scanned": 1, "files_read": 1, "malformed_lines": 0}


def rec(ts="2026-08-25T10:00:00+00:00", output=100):
    weights = CONFIG["token_class_weights"]
    weight = CONFIG["model_weights"]["claude-sonnet-5"]
    return {
        "ts": ts,
        "uuid": "u1",
        "sessionId": "s1",
        "model": "claude-sonnet-5",
        "model_known": True,
        "effort": "high",
        "isSidechain": False,
        "agentId": None,
        "attributionAgent": None,
        "attributionSkill": None,
        "cwd": "C:\a",
        "gitBranch": "master",
        "version": "2.1.227",
        "input": 0,
        "output": output,
        "cache_create": 0,
        "cache_read": 0,
        "thinking": 0,
        "weighted": weight * weights["output"] * output,
        "tools": [],
        "text_chars": 0,
        "is_api_error": False,
        "prompt": "do the thing",
    }


def test_a_window_aggregate_is_stamped_with_the_analysis_version():
    window = collect.aggregate_window(date(2026, 8, 22), [rec()], CONFIG, STATS, None)
    assert window["analysis_version"] == rules.ANALYSIS_VERSION


def test_the_analysis_version_is_a_positive_integer():
    assert isinstance(rules.ANALYSIS_VERSION, int)
    assert rules.ANALYSIS_VERSION >= 1


def assistant_line(uuid, ts, output=1000, session="s1"):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": session,
            "isSidechain": False,
            "effort": "high",
            "cwd": "C:/workspace/srst",
            "gitBranch": "master",
            "version": "2.1.227",
            "message": {
                "model": "claude-sonnet-5",
                "content": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": output,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


@pytest.fixture
def workspace(tmp_path):
    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    (proj / "a.jsonl").write_text(
        "\n".join(
            [
                assistant_line("u1", "2026-08-25T10:00:00Z"),
                assistant_line("u2", "2026-08-26T10:00:00Z", output=2000),
                assistant_line("u3", "2026-09-01T10:00:00Z", output=3000, session="s2"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    return {"root": tmp_path / "projects", "out": out, "state": out / "state.json"}


def run(workspace, **kwargs):
    return collect.run(
        config=CONFIG,
        root=workspace["root"],
        out_dir=workspace["out"],
        state_path=workspace["state"],
        **kwargs,
    )


def age_the_stamp(workspace, key, version=None):
    path = workspace["out"] / "data" / (key + ".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if version is None:
        payload.pop("analysis_version", None)
    else:
        payload["analysis_version"] = version
    payload["findings"] = [{"rule": "stale", "detail": "a sentence from the old rules", "weighted_cost": 1.0}]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def stamps(workspace):
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8")).get("analysis_version")
        for path in sorted((workspace["out"] / "data").glob("week_*.json"))
    }


def test_a_plain_collect_re_analyses_a_window_whose_stamp_is_missing(workspace):
    run(workspace)
    age_the_stamp(workspace, "week_2026_08_22")
    summary = run(workspace)
    assert summary["reanalysed"] == ["week_2026_08_22"]
    assert stamps(workspace)["week_2026_08_22"] == rules.ANALYSIS_VERSION


def test_a_plain_collect_re_analyses_a_window_whose_stamp_is_older(workspace):
    run(workspace)
    age_the_stamp(workspace, "week_2026_08_29", version=rules.ANALYSIS_VERSION - 1)
    summary = run(workspace)
    assert summary["reanalysed"] == ["week_2026_08_29"]
    payload = json.loads((workspace["out"] / "data" / "week_2026_08_29.json").read_text(encoding="utf-8"))
    assert all(item["rule"] != "stale" for item in payload["findings"])


def test_a_newer_stamp_than_the_code_is_left_alone(workspace):
    run(workspace)
    age_the_stamp(workspace, "week_2026_08_22", version=rules.ANALYSIS_VERSION + 1)
    assert run(workspace)["reanalysed"] == []


def test_a_second_collect_re_analyses_nothing(workspace):
    run(workspace)
    age_the_stamp(workspace, "week_2026_08_22")
    assert run(workspace)["reanalysed"] == ["week_2026_08_22"]
    assert run(workspace)["reanalysed"] == []


def test_a_stale_window_outside_the_requested_one_is_still_re_analysed(workspace):
    run(workspace)
    age_the_stamp(workspace, "week_2026_08_22")
    summary = run(workspace, window="2026-08-29")
    assert summary["reanalysed"] == ["week_2026_08_22"]
    assert [w["window"]["key"] for w in summary["windows"]] == ["week_2026_08_29"]
    assert stamps(workspace)["week_2026_08_22"] == rules.ANALYSIS_VERSION


def records_bytes(workspace):
    return {
        path.name: path.read_bytes()
        for path in sorted((workspace["out"] / "data" / "records").glob("*.jsonl"))
    }


def weighted_totals(workspace):
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))["totals"]["weighted"]
        for path in sorted((workspace["out"] / "data").glob("week_*.json"))
    }


def test_re_analysis_touches_neither_the_records_nor_the_weighted_totals(workspace):
    run(workspace)
    before_records, before_totals = records_bytes(workspace), weighted_totals(workspace)
    age_the_stamp(workspace, "week_2026_08_22")
    age_the_stamp(workspace, "week_2026_08_29")
    assert run(workspace)["reanalysed"] == ["week_2026_08_22", "week_2026_08_29"]
    assert records_bytes(workspace) == before_records
    assert weighted_totals(workspace) == before_totals


def test_the_collect_console_names_the_re_analysed_windows(workspace, capsys, monkeypatch):
    import cli

    run(workspace)
    age_the_stamp(workspace, "week_2026_08_22")
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(workspace["out"]))
    monkeypatch.setattr(cli.paths, "ensure_home", lambda: workspace["out"])
    monkeypatch.setattr(cli, "load_config", lambda home: dict(CONFIG, transcript_root=str(workspace["root"])))
    args = argparse.Namespace(
        calibrate_weights=False, recut_windows=False, rescan=False, reprice=False,
        backfill=False, rebuild_from_transcripts_only=False, window=None,
    )
    assert cli.cmd_collect(args) == 0
    assert "re-analysed 1 window after a rule change" in capsys.readouterr().out


def test_the_collect_console_stays_silent_when_nothing_is_stale(workspace, capsys, monkeypatch):
    import cli

    run(workspace)
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(workspace["out"]))
    monkeypatch.setattr(cli.paths, "ensure_home", lambda: workspace["out"])
    monkeypatch.setattr(cli, "load_config", lambda home: dict(CONFIG, transcript_root=str(workspace["root"])))
    args = argparse.Namespace(
        calibrate_weights=False, recut_windows=False, rescan=False, reprice=False,
        backfill=False, rebuild_from_transcripts_only=False, window=None,
    )
    assert cli.cmd_collect(args) == 0
    assert "re-analysed" not in capsys.readouterr().out
