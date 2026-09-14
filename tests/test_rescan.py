import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collect

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())

WINDOW = "2025-01-04"
KEY = "week_2025_01_04"


def assistant_line(uuid, ts, session="s1", tools=(), extra=None):
    content = [
        {"type": "tool_use", "id": call_id, "name": name, "input": payload}
        for call_id, name, payload in tools
    ]
    entry = {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": session,
        "isSidechain": False,
        "cwd": "C:\\workspace\\srst",
        "gitBranch": "master",
        "message": {
            "model": "claude-sonnet-5",
            "content": content,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    entry.update(extra or {})
    return json.dumps(entry)


def tool_result_line(call_id, text):
    return json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}]},
        }
    )


def cost_line(session, usd):
    return json.dumps({"type": "cost-state", "sessionId": session, "totalCostUSD": usd, "modelUsage": {}})


@pytest.fixture
def workspace(tmp_path):
    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    return {"root": tmp_path / "projects", "out": out, "state": out / "state.json", "proj": proj}


def run(workspace, config=CONFIG, **kwargs):
    return collect.run(
        config=config,
        root=workspace["root"],
        out_dir=workspace["out"],
        state_path=workspace["state"],
        **kwargs,
    )


def store_path(workspace):
    return workspace["out"] / "data" / "records" / (KEY + ".jsonl")


def stored_records(workspace):
    return [json.loads(line) for line in store_path(workspace).read_text(encoding="utf-8").splitlines() if line.strip()]


def totals(workspace):
    return json.loads((workspace["out"] / "data" / (KEY + ".json")).read_text())["totals"]


def state_of(workspace):
    return json.loads(workspace["state"].read_text(encoding="utf-8"))


AGENT_INPUT = {"description": "Implement section 2", "subagent_type": "general-purpose", "prompt": "p" * 300}


def seed(workspace):
    (workspace["proj"] / "pruned.jsonl").write_text(
        "\n".join(assistant_line("p%d" % i, "2025-01-06T10:%02d:00Z" % i) for i in range(3)) + "\n",
        encoding="utf-8",
    )
    (workspace["proj"] / "kept.jsonl").write_text(
        "\n".join(
            [
                assistant_line(
                    "k0",
                    "2025-01-07T10:00:00Z",
                    tools=(("toolu_a", "Agent", AGENT_INPUT),),
                    extra={"attributionMcpServer": "gitlab"},
                ),
                tool_result_line("toolu_a", "done" * 10),
                assistant_line("k1", "2025-01-07T10:01:00Z"),
                cost_line("s1", 12.5),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return run(workspace)


def strip_new_fields(workspace):
    records = stored_records(workspace)
    for record in records:
        record.pop("mcp_server", None)
        record.pop("text_chars", None)
        for tool in record.get("tools") or []:
            tool.pop("result_chars", None)
            tool.pop("is_error", None)
            tool.pop("denied", None)
    store_path(workspace).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8"
    )
    for path in workspace["out"].glob("data/records/agent_calls_*.jsonl"):
        path.unlink()
    (workspace["out"] / "data" / "session_costs.json").write_text("{}", encoding="utf-8")


def test_rescan_backfills_fields_missing_from_an_older_store(workspace):
    seed(workspace)
    strip_new_fields(workspace)
    assert all("mcp_server" not in r for r in stored_records(workspace))

    summary = run(workspace, rescan=True)

    by_uuid = {record["uuid"]: record for record in stored_records(workspace)}
    assert by_uuid["k0"]["mcp_server"] == "gitlab"
    assert by_uuid["k0"]["tools"][0]["result_chars"] == 40
    assert summary["rescan"]["records_updated"] == 5
    assert summary["rescan"]["records_added"] == 0


def test_rescan_restores_the_agent_call_and_cost_stores(workspace):
    seed(workspace)
    strip_new_fields(workspace)

    summary = run(workspace, rescan=True)

    calls = collect.load_agent_calls(workspace["out"] / "data" / "records")
    assert list(calls) == ["toolu_a"]
    assert calls["toolu_a"]["description"] == "Implement section 2"
    assert summary["rescan"]["agent_calls_added"] == 1
    assert summary["rescan"]["session_costs_added"] == 1
    costs = json.loads((workspace["out"] / "data" / "session_costs.json").read_text())
    assert costs["s1"]["usd"] == 12.5


def test_a_second_rescan_adds_no_duplicate_agent_call(workspace):
    seed(workspace)
    run(workspace, rescan=True)
    summary = run(workspace, rescan=True)
    calls = collect.load_agent_calls(workspace["out"] / "data" / "records")
    assert list(calls) == ["toolu_a"]
    assert summary["rescan"]["agent_calls_added"] == 0


def test_rescan_never_shrinks_a_window_when_the_weights_changed(workspace):
    seed(workspace)
    before = totals(workspace)
    cheaper = copy.deepcopy(CONFIG)
    cheaper["model_weights"]["claude-sonnet-5"] = 0.5

    run(workspace, config=cheaper, rescan=True)

    assert totals(workspace)["weighted"] == before["weighted"]
    assert totals(workspace)["turns"] == before["turns"]


def test_rescan_with_reprice_does_move_the_weighted_total(workspace):
    seed(workspace)
    before = totals(workspace)
    richer = copy.deepcopy(CONFIG)
    richer["model_weights"]["claude-sonnet-5"] = CONFIG["model_weights"]["claude-sonnet-5"] * 2

    run(workspace, config=richer, rescan=True, reprice=True)

    assert totals(workspace)["weighted"] > before["weighted"]


def test_rescan_keeps_records_whose_transcript_was_pruned(workspace):
    seed(workspace)
    before = totals(workspace)
    (workspace["proj"] / "pruned.jsonl").unlink()

    summary = run(workspace, rescan=True)

    assert [r["uuid"] for r in stored_records(workspace)] == ["p0", "p1", "p2", "k0", "k1"]
    assert totals(workspace) == before
    assert summary["rescan"] == {
        "records_updated": 0,
        "records_added": 0,
        "records_unchanged": 2,
        "agent_calls_added": 0,
        "session_costs_added": 0,
    }


def test_rescan_resets_the_stored_offsets_to_the_end_of_each_file(workspace):
    seed(workspace)
    run(workspace, rescan=True)
    files = state_of(workspace)["files"]
    for key, entry in files.items():
        assert entry["offset"] == Path(key).stat().st_size


def test_a_schema_bump_recommends_a_rescan_on_the_next_collect(workspace):
    seed(workspace)
    state = state_of(workspace)
    state["schema_version"] = collect.SCHEMA_VERSION - 1
    workspace["state"].write_text(json.dumps(state), encoding="utf-8")

    assert run(workspace)["rescan_recommended"] == collect.SCHEMA_VERSION


def test_the_rescan_recommendation_is_printed_only_once(workspace):
    seed(workspace)
    state = state_of(workspace)
    state["schema_version"] = collect.SCHEMA_VERSION - 1
    workspace["state"].write_text(json.dumps(state), encoding="utf-8")

    run(workspace)
    assert run(workspace)["rescan_recommended"] is None


def test_a_first_ever_collect_does_not_recommend_a_rescan(workspace):
    assert seed(workspace)["rescan_recommended"] is None


def test_a_rescan_itself_clears_the_recommendation(workspace):
    seed(workspace)
    state = state_of(workspace)
    state["schema_version"] = collect.SCHEMA_VERSION - 1
    workspace["state"].write_text(json.dumps(state), encoding="utf-8")

    assert run(workspace, rescan=True)["rescan_recommended"] is None
    assert run(workspace)["rescan_recommended"] is None


@pytest.fixture
def home(monkeypatch, tmp_path):
    import paths

    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    return paths.ensure_home()


def _cli_workspace(home, tmp_path):
    import cli

    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    config = cli.load_config(home)
    config["transcript_root"] = str(tmp_path / "projects")
    cli.save_config(home, config)
    return {"root": tmp_path / "projects", "out": home, "state": home / "state.json", "proj": proj}


def test_the_cli_prints_the_upgrade_notice_only_on_the_first_collect(home, tmp_path, capsys):
    import cli

    workspace = _cli_workspace(home, tmp_path)
    seed(workspace)
    state = state_of(workspace)
    state["schema_version"] = collect.SCHEMA_VERSION - 1
    workspace["state"].write_text(json.dumps(state), encoding="utf-8")
    capsys.readouterr()

    assert cli.main(["collect"]) == 0
    first = capsys.readouterr().err
    assert "NEW FIELDS AVAILABLE" in first
    assert "collect --rescan" in first

    assert cli.main(["collect"]) == 0
    assert "NEW FIELDS AVAILABLE" not in capsys.readouterr().err


def test_the_cli_prints_one_summary_line_per_rescanned_store(home, tmp_path, capsys):
    import cli

    workspace = _cli_workspace(home, tmp_path)
    seed(workspace)
    strip_new_fields(workspace)
    capsys.readouterr()

    assert cli.main(["collect", "--rescan"]) == 0
    out = capsys.readouterr().out
    assert "rescan records      : 5 gained fields, 0 added" in out
    assert "rescan agent calls  : 1 added" in out
    assert "rescan session costs: 1 added" in out


def test_rescan_cannot_be_combined_with_another_transcript_re_read(home, capsys):
    import cli

    assert cli.main(["collect", "--rescan", "--backfill"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def age_the_cost_store(workspace):
    path = workspace["out"] / "data" / "session_costs.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    for entry in stored.values():
        entry["points"] = []
    path.write_text(json.dumps(stored), encoding="utf-8")
    state = state_of(workspace)
    for entry in state["files"].values():
        if entry.get("cost"):
            entry["cost"].pop("ts", None)
    workspace["state"].write_text(json.dumps(state), encoding="utf-8")


def cost_points(workspace):
    path = workspace["out"] / "data" / "session_costs.json"
    return {k: v.get("points") or [] for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


def test_a_rescan_rebuilds_the_cost_points_a_pre_upgrade_store_never_stored(workspace):
    seed(workspace)
    assert cost_points(workspace)["s1"]
    age_the_cost_store(workspace)
    assert cost_points(workspace)["s1"] == []

    run(workspace)
    assert cost_points(workspace)["s1"] == [], "an incremental collect re-reads nothing"

    run(workspace, rescan=True)
    points = cost_points(workspace)["s1"]
    assert len(points) == 1
    assert points[0][1] == 12.5
