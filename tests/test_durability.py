import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cli
import collect
import paths

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())

OLD_WINDOW = "2025-01-04"
OLD_KEY = "week_2025_01_04"


def assistant_line(uuid, ts, output=1000, session="s1", model="claude-sonnet-5"):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": ts,
            "sessionId": session,
            "isSidechain": False,
            "cwd": "C:\\workspace\\srst",
            "gitBranch": "master",
            "message": {
                "model": model,
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
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    return {"root": tmp_path / "projects", "out": out, "state": out / "state.json", "proj": root}


def run(workspace, config=CONFIG, **kwargs):
    return collect.run(
        config=config,
        root=workspace["root"],
        out_dir=workspace["out"],
        state_path=workspace["state"],
        **kwargs,
    )


def store_uuids(workspace, key):
    path = workspace["out"] / "data" / "records" / (key + ".jsonl")
    return [json.loads(line)["uuid"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def totals(workspace, key):
    return json.loads((workspace["out"] / "data" / (key + ".json")).read_text())["totals"]


def seed(workspace):
    (workspace["proj"] / "pruned.jsonl").write_text(
        "\n".join(assistant_line("p%d" % i, "2025-01-06T10:%02d:00Z" % i) for i in range(3)) + "\n",
        encoding="utf-8",
    )
    (workspace["proj"] / "kept.jsonl").write_text(
        "\n".join(assistant_line("k%d" % i, "2025-01-07T10:%02d:00Z" % i) for i in range(2)) + "\n",
        encoding="utf-8",
    )
    run(workspace)


def test_backfill_keeps_records_whose_transcript_was_pruned(workspace):
    seed(workspace)
    before = totals(workspace, OLD_KEY)
    assert before["turns"] == 5

    (workspace["proj"] / "pruned.jsonl").unlink()
    summary = run(workspace, backfill=True)

    assert store_uuids(workspace, OLD_KEY) == ["p0", "p1", "p2", "k0", "k1"]
    assert totals(workspace, OLD_KEY) == before
    assert summary["files_scanned"] == 1
    assert summary["total_records"] == 5


def test_a_plain_collect_also_survives_a_pruned_transcript(workspace):
    seed(workspace)
    before = totals(workspace, OLD_KEY)
    (workspace["proj"] / "pruned.jsonl").unlink()
    run(workspace)
    assert totals(workspace, OLD_KEY) == before


def test_re_parsing_the_same_transcript_never_duplicates_a_uuid(workspace):
    seed(workspace)
    run(workspace, backfill=True)
    run(workspace, backfill=True)
    uuids = store_uuids(workspace, OLD_KEY)
    assert len(uuids) == len(set(uuids)) == 5
    assert totals(workspace, OLD_KEY)["turns"] == 5


def test_a_backfill_that_would_shrink_a_closed_window_refuses(workspace):
    seed(workspace)
    cheaper = copy.deepcopy(CONFIG)
    cheaper["model_weights"]["claude-sonnet-5"] = 0.5

    with pytest.raises(collect.CollectionError) as error:
        run(workspace, config=cheaper, backfill=True)

    message = str(error.value)
    assert OLD_KEY in message
    assert "--rebuild-from-transcripts-only" in message
    assert totals(workspace, OLD_KEY)["turns"] == 5


def test_the_refusal_names_every_dropped_record(workspace):
    seed(workspace)
    (workspace["proj"] / "pruned.jsonl").unlink()
    known = {OLD_WINDOW: {u: {"weighted": 1.0} for u in ("p0", "p1", "p2", "k0", "k1")}}
    after = {OLD_WINDOW: {u: {"weighted": 1.0} for u in ("k0", "k1")}}
    losses = collect._losses(known, after, CONFIG)
    assert losses[0]["dropped_records"] == 3
    assert losses[0]["turns_before"] == 5 and losses[0]["turns_after"] == 2
    assert "3 record(s) dropped" in collect.describe_losses(losses)


def test_the_current_window_is_not_guarded_against_shrinking(workspace):
    now = datetime.now(timezone.utc)
    start = collect.window_start(now, CONFIG)
    assert collect._losses({start.isoformat(): {"a": {"weighted": 9.0}}}, {}, CONFIG) == []


def test_rebuild_from_transcripts_only_discards_and_reports_the_loss(workspace):
    seed(workspace)
    (workspace["proj"] / "pruned.jsonl").unlink()

    summary = run(workspace, backfill=True, rebuild_from_transcripts_only=True)

    assert store_uuids(workspace, OLD_KEY) == ["k0", "k1"]
    assert totals(workspace, OLD_KEY)["turns"] == 2
    assert summary["losses"][0]["window"] == OLD_KEY
    assert summary["losses"][0]["turns_before"] == 5
    assert summary["losses"][0]["turns_after"] == 2


def test_rebuild_from_transcripts_only_implies_backfill(workspace):
    seed(workspace)
    summary = run(workspace, rebuild_from_transcripts_only=True)
    assert summary["new_records"] == 5
    assert summary["losses"] == []


def test_recut_rebuckets_the_store_without_reading_transcripts(workspace):
    seed(workspace)
    for transcript in workspace["proj"].glob("*.jsonl"):
        transcript.unlink()

    monday = copy.deepcopy(CONFIG)
    monday["reset_weekday"] = "Monday"
    summary = collect.recut(
        config=monday, out_dir=workspace["out"], state_path=workspace["state"], window=None
    )

    assert summary["total_records"] == 5
    assert store_uuids(workspace, "week_2025_01_06") == ["p0", "p1", "p2", "k0", "k1"]
    assert not (workspace["out"] / "data" / "records" / (OLD_KEY + ".jsonl")).exists()
    assert not (workspace["out"] / "data" / (OLD_KEY + ".json")).exists()
    assert totals(workspace, "week_2025_01_06")["turns"] == 5


def test_recut_is_idempotent_and_loses_nothing(workspace):
    seed(workspace)
    first = collect.recut(config=CONFIG, out_dir=workspace["out"], state_path=workspace["state"])
    second = collect.recut(config=CONFIG, out_dir=workspace["out"], state_path=workspace["state"])
    assert first["total_records"] == second["total_records"] == 5
    assert store_uuids(workspace, OLD_KEY) == ["p0", "p1", "p2", "k0", "k1"]


def test_recut_on_an_empty_store_fails_loudly(workspace):
    with pytest.raises(collect.CollectionError):
        collect.recut(config=CONFIG, out_dir=workspace["out"], state_path=workspace["state"])


def test_a_normal_collect_still_reads_only_appended_bytes(workspace):
    seed(workspace)
    with (workspace["proj"] / "kept.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(assistant_line("k9", "2025-01-08T10:00:00Z") + "\n")

    summary = run(workspace)

    assert summary["new_records"] == 1
    assert summary["total_records"] == 6
    assert store_uuids(workspace, OLD_KEY) == ["p0", "p1", "p2", "k0", "k1", "k9"]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    return paths.ensure_home()


def test_setting_the_same_reset_weekday_does_not_advise_re_cutting(home, capsys):
    weekday = cli.load_config(home)["reset_weekday"]
    assert cli.main(["status", "--set-reset-weekday", weekday]) == 0
    out = capsys.readouterr().out
    assert "unchanged" in out
    assert "--recut-windows" not in out


def test_changing_the_reset_weekday_advises_the_safe_re_cut(home, capsys):
    assert cli.main(["status", "--set-reset-weekday", "Thursday"]) == 0
    out = capsys.readouterr().out
    assert "collect --recut-windows" in out
    assert "--backfill" not in out


def test_a_corrected_detection_advises_the_safe_re_cut(home):
    config = cli.load_config(home)
    config["reset_weekday"] = "Saturday"
    cli.save_config(home, config)
    note = cli.apply_detection(
        home, cli.load_config(home), {"candidates": ["Thursday"], "detected": "Thursday", "ambiguous": False}
    )
    assert "collect --recut-windows" in note
    assert "--backfill" not in note


def test_recut_windows_cannot_be_combined_with_a_transcript_re_read(home, capsys):
    assert cli.main(["collect", "--recut-windows", "--backfill"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def cheap_config(weight):
    config = copy.deepcopy(CONFIG)
    config["model_weights"]["claude-sonnet-5"] = weight
    return config


def test_repricing_reads_the_store_and_never_touches_a_transcript(workspace):
    seed(workspace)
    before = totals(workspace, OLD_KEY)
    for path in workspace["proj"].glob("*.jsonl"):
        path.unlink()

    summary = collect.reprice(cheap_config(4.0), workspace["out"], workspace["state"])

    assert store_uuids(workspace, OLD_KEY) == ["p0", "p1", "p2", "k0", "k1"]
    assert totals(workspace, OLD_KEY)["turns"] == before["turns"]
    assert totals(workspace, OLD_KEY)["weighted"] == 4.0 * before["weighted"]
    assert summary["repriced"][0] == {
        "window": OLD_KEY,
        "turns": 5,
        "weighted_before": before["weighted"],
        "weighted_after": 4.0 * before["weighted"],
    }


def test_repricing_upwards_does_not_trip_the_closed_window_shrink_guard(workspace):
    seed(workspace)
    collect.reprice(cheap_config(5.0), workspace["out"], workspace["state"])
    assert totals(workspace, OLD_KEY)["turns"] == 5


def test_repricing_downwards_keeps_every_record(workspace):
    seed(workspace)
    before = totals(workspace, OLD_KEY)
    collect.reprice(cheap_config(0.5), workspace["out"], workspace["state"])
    assert store_uuids(workspace, OLD_KEY) == ["p0", "p1", "p2", "k0", "k1"]
    assert totals(workspace, OLD_KEY)["weighted"] == 0.5 * before["weighted"]


def test_repricing_an_empty_store_fails_loudly(workspace):
    with pytest.raises(collect.CollectionError):
        collect.reprice(CONFIG, workspace["out"], workspace["state"])


def test_a_changed_weight_is_reported_as_pricing_drift_until_repriced(workspace):
    seed(workspace)
    summary = run(workspace, config=cheap_config(4.0))
    assert [d["window"] for d in summary["pricing_drift"]] == [OLD_KEY]
    assert summary["pricing_drift"][0]["records"] == 5

    after = collect.reprice(cheap_config(4.0), workspace["out"], workspace["state"])
    assert after["pricing_drift"] == []


def test_an_unmatched_model_is_named_with_the_weight_it_was_priced_at(workspace):
    (workspace["proj"] / "odd.jsonl").write_text(
        assistant_line("z1", "2025-01-06T10:00:00Z", model="claude-zebra-9") + "\n", encoding="utf-8"
    )
    summary = run(workspace)
    assert summary["unknown_models"] == [
        {"model": "claude-zebra-9", "turns": 1, "weighted": 5010.0, "weight": 1.0}
    ]


def test_a_family_matched_model_is_not_reported_as_unknown(workspace):
    (workspace["proj"] / "odd.jsonl").write_text(
        assistant_line("f1", "2025-01-06T10:00:00Z", model="claude-fable-5-1") + "\n", encoding="utf-8"
    )
    summary = run(workspace)
    assert summary["unknown_models"] == []
    assert json.loads((workspace["out"] / "data" / (OLD_KEY + ".json")).read_text())["unknown_models"] == []


def test_reprice_cannot_be_combined_with_a_transcript_re_read(home, capsys):
    assert cli.main(["collect", "--reprice", "--backfill"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_collect_names_an_unknown_model_and_the_weight_it_used(home, monkeypatch, tmp_path, capsys):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    (root / "a.jsonl").write_text(
        assistant_line("z1", "2025-01-06T10:00:00Z", model="claude-zebra-9") + "\n", encoding="utf-8"
    )
    config = cli.load_config(home)
    config["transcript_root"] = str(tmp_path / "projects")
    cli.save_config(home, config)

    assert cli.main(["collect"]) == 0
    err = capsys.readouterr().err
    assert "UNKNOWN MODEL claude-zebra-9" in err
    assert "default weight 1.0" in err
