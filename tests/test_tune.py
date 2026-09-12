import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cli
import agentfiles
import tune

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
CEILING = 1000000000.0


def agent_file(directory, name, model=None, description="does a thing"):
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---", "name: %s" % name, 'description: "%s"' % description]
    if model:
        lines.append("model: %s" % model)
    lines += ["---", "", "Body of %s." % name, ""]
    path = directory / ("%s.md" % name)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def skill_file(directory, name, description="a skill"):
    target = directory / name
    target.mkdir(parents=True, exist_ok=True)
    path = target / "SKILL.md"
    path.write_text('---\nname: %s\ndescription: "%s"\n---\n\nBody.\n' % (name, description), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path):
    user = tmp_path / "user"
    (user / ".claude" / "agents").mkdir(parents=True)
    (user / ".claude" / "skills").mkdir(parents=True)
    (user / ".claude" / "plugins").mkdir(parents=True)
    return user


def roots_for(home, project_dirs=()):
    return agentfiles.default_roots(
        user_home=home, project_dirs=project_dirs, plugins_home=home / ".claude" / "plugins"
    )


def window(
    start="2026-08-22",
    end="2026-08-29",
    end_utc="2026-08-28T22:00:00+00:00",
    agents=(),
    skills=(),
    total=100000000.0,
    turns=1000,
    by_day=None,
    findings=None,
    ceiling=CEILING,
):
    return {
        "window": {
            "key": "week_" + start.replace("-", "_"),
            "start": start,
            "end": end,
            "start_utc": start + "T00:00:00+00:00",
            "end_utc": end_utc,
        },
        "totals": {"weighted": total, "turns": turns},
        "weights": {
            "model_weights": CONFIG["model_weights"],
            "default_model_weight": CONFIG["default_model_weight"],
            "token_class_weights": CONFIG["token_class_weights"],
        },
        "by_model": [{"key": "claude-opus-5", "weighted": total}],
        "by_repo": [],
        "by_day": by_day or [],
        "by_agent": [{"key": key, "turns": t, "weighted": w} for key, t, w in agents],
        "by_skill": [{"key": key, "turns": t, "weighted": w} for key, t, w in skills],
        "findings": findings or [],
        "findings_by_rule": {},
        "ceiling": {"estimate": ceiling, "method": "top-cluster"},
    }


def record(
    ts, agent=None, skill=None, weighted=1.0, session="s1", agent_id=None, cwd=None, output=10_000
):
    return {
        "uuid": ts + (agent or skill or "x") + session,
        "ts": ts,
        "sessionId": session,
        "agentId": agent_id,
        "attributionAgent": agent,
        "attributionSkill": skill,
        "cwd": cwd,
        "weighted": weighted,
        "input": 0,
        "output": output,
        "thinking": 0,
        "cache_create": 0,
        "cache_read": 0,
        "tools": [],
        "is_api_error": False,
    }


def entry_for(result, name):
    for entry in result["proposals"] + result["setting_proposals"] + result["reported"]:
        if entry["name"] == name:
            return entry
    raise AssertionError("no entry for %s" % name)


def build(windows, home, records=None, **kwargs):
    return tune.build(
        windows, CONFIG, roots=roots_for(home), records_by_window=records or {}, now=NOW, **kwargs
    )


def test_a_name_that_resolves_to_exactly_one_file_is_mapped_to_it(home):
    path = agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    assert agentfiles.resolve_agent("mr-scout", roots_for(home)) == [path]


def test_a_name_that_resolves_to_no_file_resolves_to_nothing(home):
    assert agentfiles.resolve_agent("mr-scout", roots_for(home)) == []


def test_a_name_that_resolves_to_several_files_returns_all_of_them(home, tmp_path):
    project = tmp_path / "repo"
    user_copy = agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    project_copy = agent_file(project / ".claude" / "agents", "mr-scout", model="opus")
    found = agentfiles.resolve_agent("mr-scout", roots_for(home, project_dirs=[str(project)]))
    assert sorted(found) == sorted([user_copy, project_copy])


def test_several_matches_are_listed_and_no_edit_is_proposed(home, tmp_path):
    project = tmp_path / "repo"
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    agent_file(project / ".claude" / "agents", "mr-scout", model="opus")
    result = tune.build(
        [window(agents=[("mr-scout", 100, 40000000.0)])],
        CONFIG,
        roots=roots_for(home, project_dirs=[str(project)]),
        now=NOW,
    )
    entry = entry_for(result, "mr-scout")
    assert entry["status"] == tune.AMBIGUOUS
    assert len(entry["files"]) == 2
    assert entry["patch"] is None
    assert result["proposals"] == []


def test_a_file_is_matched_by_its_frontmatter_name_not_only_its_stem(home):
    directory = home / ".claude" / "agents"
    (directory / "renamed.md").write_text("---\nname: mr-scout\n---\n\nBody.\n", encoding="utf-8")
    assert [p.name for p in agentfiles.resolve_agent("mr-scout", roots_for(home))] == ["renamed.md"]


def test_a_plugin_qualified_name_only_matches_that_plugin(home):
    plugins = home / ".claude" / "plugins"
    install = plugins / "cache" / "market" / "glab" / "1.0.0"
    skill_file(install / "skills", "glab")
    other = plugins / "cache" / "market" / "other" / "1.0.0"
    skill_file(other / "skills", "glab")
    (home / ".claude" / "commands").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "commands" / "glab.md").write_text("my own glab command\n", encoding="utf-8")
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {"plugins": {"glab@market": [{"installPath": str(install)}], "other@market": [{"installPath": str(other)}]}}
        ),
        encoding="utf-8",
    )
    assert [str(p) for p in agentfiles.resolve_skill("glab:glab", roots_for(home))] == [
        str(install / "skills" / "glab" / "SKILL.md")
    ]
    assert [p.name for p in agentfiles.resolve_skill("glab", roots_for(home))] == ["glab.md"]
    assert len(agentfiles.resolve_skill("glab:other", roots_for(home))) == 0


def test_a_skill_name_also_matches_a_slash_command_file(home):
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "review-mr.md").write_text("Review a merge request.\n", encoding="utf-8")
    assert [p.name for p in agentfiles.resolve_skill("review-mr", roots_for(home))] == ["review-mr.md"]


def test_a_reference_file_nested_inside_another_skill_is_not_a_match(home):
    nested = home / ".claude" / "skills" / "linear-mcp-cli" / "references"
    nested.mkdir(parents=True)
    (nested / "comments.md").write_text("reference material\n", encoding="utf-8")
    assert agentfiles.resolve_skill("comments", roots_for(home)) == []


def test_the_default_selection_is_the_last_n_closed_windows():
    windows = [
        window(start="2026-08-01", end_utc="2026-08-07T22:00:00+00:00"),
        window(start="2026-08-08", end_utc="2026-08-14T22:00:00+00:00"),
        window(start="2026-08-15", end_utc="2026-08-21T22:00:00+00:00"),
        window(start="2026-08-22", end_utc="2026-08-28T22:00:00+00:00"),
        window(start="2026-08-29", end_utc="2026-09-04T22:00:00+00:00"),
    ]
    chosen, note = tune.select_windows(windows, count=3, now=NOW)
    assert [w["window"]["key"] for w in chosen] == ["week_2026_08_08", "week_2026_08_15", "week_2026_08_22"]
    assert note is None


def test_the_open_window_is_never_part_of_the_analysed_set():
    windows = [
        window(start="2026-08-22", end_utc="2026-08-28T22:00:00+00:00"),
        window(start="2026-08-29", end_utc="2026-09-04T22:00:00+00:00"),
    ]
    chosen, _ = tune.select_windows(windows, count=4, now=NOW)
    assert [w["window"]["key"] for w in chosen] == ["week_2026_08_22"]
    assert tune.current_window(windows, NOW)["window"]["key"] == "week_2026_08_29"


def test_fewer_closed_windows_than_asked_for_says_so():
    chosen, note = tune.select_windows([window()], count=4, now=NOW)
    assert len(chosen) == 1
    assert "only 1 closed window" in note


def test_an_explicit_window_is_honoured_even_when_it_is_open():
    open_window = window(start="2026-08-29", end_utc="2026-09-04T22:00:00+00:00")
    chosen, note = tune.select_windows([window(), open_window], window="2026-08-29", now=NOW)
    assert [w["window"]["key"] for w in chosen] == ["week_2026_08_29"]
    assert "still open" in note


def test_an_unknown_window_is_an_error_naming_what_exists():
    with pytest.raises(LookupError) as error:
        tune.select_windows([window()], window="2020-01-01")
    assert "week_2026_08_22" in str(error.value)


def test_no_collected_window_is_an_error():
    with pytest.raises(LookupError):
        tune.select_windows([])


def four_windows(agent_costs):
    starts = ["2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"]
    ends = [
        "2026-08-07T22:00:00+00:00",
        "2026-08-14T22:00:00+00:00",
        "2026-08-21T22:00:00+00:00",
        "2026-08-28T22:00:00+00:00",
    ]
    return [
        window(
            start=start,
            end_utc=end,
            agents=[("mr-scout", 100, cost)] if cost else [],
        )
        for start, end, cost in zip(starts, ends, agent_costs)
    ]


def test_the_typical_cost_is_the_median_across_windows_counting_absence_as_zero(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    result = build(four_windows([40000000.0, 0.0, 20000000.0, 10000000.0]), home)
    entry = entry_for(result, "mr-scout")
    assert entry["typical_weighted"] == 15000000.0
    assert entry["peak_weighted"] == 40000000.0
    assert entry["windows_present"] == 3


def test_a_component_seen_in_one_window_of_four_is_a_one_off_and_never_proposes(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    result = build(four_windows([0.0, 0.0, 0.0, 400000000.0]), home)
    entry = entry_for(result, "mr-scout")
    assert entry["status"] == tune.ONE_OFF
    assert "one-off, not a pattern" in tune.render(result)
    assert entry["weighted_saving"] is None
    assert entry["patch"] is None
    assert result["proposals"] == []


def test_a_peak_week_cannot_outrank_a_consistent_component(home):
    windows = four_windows([0.0, 0.0, 0.0, 400000000.0])
    for index, data in enumerate(windows):
        data["by_skill"] = [{"key": "linear-mcp-cli", "turns": 10, "weighted": 20000000.0}]
    skill_file(home / ".claude" / "skills", "linear-mcp-cli")
    result = build(windows, home)
    assert [c["name"] for c in result["centres"]][0] == "linear-mcp-cli"


def test_a_component_present_in_two_of_four_windows_can_propose_from_its_typical_cost(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    result = build(four_windows([40000000.0, 0.0, 0.0, 40000000.0]), home)
    entry = entry_for(result, "mr-scout")
    assert entry["status"] == tune.PROPOSAL
    assert entry["weighted_saving"] == pytest.approx(20000000.0 * 1.0 * 0.8)
    assert entry["weighted_saving"] < entry["peak_weighted"]


def test_a_single_window_analysis_lowers_the_floor_and_says_it_is_fitted_to_one_week(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    result = build([window(agents=[("mr-scout", 100, 40000000.0)])], home)
    assert result["single_window"] is True
    assert result["min_windows_for_proposal"] == 1
    assert result["proposals"][0]["name"] == "mr-scout"
    assert "SINGLE WINDOW" in tune.render(result)


def test_a_builtin_agent_with_no_file_is_proposed_against_the_setting_never_dropped(home):
    windows = four_windows([0, 0, 0, 0])
    for data in windows:
        data["by_agent"] = [{"key": "general-purpose", "turns": 2624, "weighted": 208000000.0}]
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5"}}), encoding="utf-8"
    )
    result = build(windows, home, setting=tune.subagent_model_setting(home))
    entry = entry_for(result, "general-purpose")
    assert entry["status"] == tune.SETTING_PROPOSAL
    assert entry["typical_weighted"] == 208000000.0
    assert entry["files"] == []
    assert entry["setting_env"] == "CLAUDE_CODE_SUBAGENT_MODEL"
    assert entry["setting_value"] == "claude-opus-5"
    assert entry["weighted_saving"] > 0
    assert "208.0M" in tune.render(result)


def test_a_builtin_agent_below_the_saving_floor_still_reports_its_cost_with_no_proposal(home):
    windows = four_windows([0, 0, 0, 0])
    for data in windows:
        data["by_agent"] = [{"key": "general-purpose", "turns": 3, "weighted": 260000.0}]
    result = build(windows, home, setting=tune.subagent_model_setting(home))
    entry = entry_for(result, "general-purpose")
    assert entry["status"] == tune.NO_FILE
    assert entry["typical_weighted"] == 260000.0
    assert entry["weighted_saving"] is None
    assert "no file to edit" in tune.render(result)


def test_an_unknown_non_builtin_agent_says_so_rather_than_guessing(home):
    result = build([window(agents=[("ghost-agent", 10, 9000000.0)])], home)
    assert "no agent definition found" in entry_for(result, "ghost-agent")["note"]


def test_an_agent_already_on_the_cheap_tier_gets_no_proposal_but_keeps_its_cost(home):
    agent_file(home / ".claude" / "agents", "review-verifier", model="sonnet")
    windows = four_windows([0, 0, 0, 0])
    for data in windows:
        data["by_agent"] = [{"key": "review-verifier", "turns": 1323, "weighted": 20700000.0}]
    result = build(windows, home)
    entry = entry_for(result, "review-verifier")
    assert entry["status"] == tune.ALREADY_RIGHT_SIZED
    assert entry["typical_weighted"] == 20700000.0
    assert result["proposals"] == []


def test_an_agent_outside_the_sonnet_class_list_is_shown_without_a_saving(home):
    agent_file(home / ".claude" / "agents", "frontend-developer", model="opus")
    result = build([window(agents=[("frontend-developer", 70, 30000000.0)])], home)
    entry = entry_for(result, "frontend-developer")
    assert entry["status"] == tune.NOT_ASSESSABLE
    assert entry["weighted_saving"] is None


def test_a_saving_below_the_floor_is_reported_but_never_proposed(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    result = tune.build(
        [window(agents=[("mr-scout", 3, 300000.0)])],
        dict(CONFIG, tune={"min_saving": 1000000}),
        roots=roots_for(home),
        now=NOW,
    )
    assert result["proposals"] == []
    assert entry_for(result, "mr-scout")["status"] == tune.OBSERVATION


def test_a_component_below_the_cost_floor_is_counted_but_not_listed(home):
    result = build([window(agents=[("general-purpose", 2, 10.0)])], home)
    assert result["reported"] == []
    assert result["hidden_below_min_cost"] == 1
    assert "1 further component(s)" in tune.render(result)


def test_agents_count_invocations_by_agent_run_and_skills_by_session(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    skill_file(home / ".claude" / "skills", "linear-mcp-cli")
    data = window(agents=[("mr-scout", 4, 40000000.0)], skills=[("linear-mcp-cli", 3, 30000000.0)])
    records = {
        "week_2026_08_22": [
            record("2026-08-22T10:00:00+00:00", agent="mr-scout", agent_id="a1"),
            record("2026-08-22T10:05:00+00:00", agent="mr-scout", agent_id="a1"),
            record("2026-08-22T11:00:00+00:00", agent="mr-scout", agent_id="a2"),
            record("2026-08-22T11:10:00+00:00", agent="mr-scout", agent_id="a2"),
            record("2026-08-22T12:00:00+00:00", skill="linear-mcp-cli", session="s1"),
            record("2026-08-22T12:30:00+00:00", skill="linear-mcp-cli", session="s1"),
            record("2026-08-22T13:00:00+00:00", skill="linear-mcp-cli", session="s2"),
        ]
    }
    result = build([data], home, records=records)
    agent = entry_for(result, "mr-scout")
    skill = entry_for(result, "linear-mcp-cli")
    assert agent["invocations"] == 2
    assert agent["invocation_unit"] == "agent run"
    assert agent["weighted_per_invocation"] == 20000000.0
    assert agent["median_wall_clock_seconds"] == pytest.approx(450.0)
    assert skill["invocations"] == 2
    assert skill["invocation_unit"] == "session"
    assert skill["quiet_invocations"] == 2


def test_a_skill_is_reported_with_its_file_size_and_description_length_and_no_saving(home):
    path = skill_file(home / ".claude" / "skills", "linear-mcp-cli", description="x" * 300)
    result = build([window(skills=[("linear-mcp-cli", 187, 15700000.0)])], home)
    entry = entry_for(result, "linear-mcp-cli")
    assert entry["files"] == [str(path)]
    assert entry["weighted_saving"] is None
    assert entry["description_chars"] == 300
    assert entry["file_bytes"] > 0
    assert "not the cost of loading it" in entry["note"]


def test_a_cost_per_run_far_above_the_others_is_flagged_without_a_saving(home):
    data = window(
        skills=[("cheap-one", 10, 1000000.0), ("cheap-two", 10, 1000000.0), ("heavy", 10, 90000000.0)]
    )
    records = {
        "week_2026_08_22": [
            record("2026-08-22T10:00:00+00:00", skill="cheap-one", session="s1"),
            record("2026-08-22T10:00:00+00:00", skill="cheap-two", session="s2"),
            record("2026-08-22T10:00:00+00:00", skill="heavy", session="s3"),
        ]
    }
    result = build([data], home, records=records)
    assert entry_for(result, "heavy")["out_of_line"] is True
    assert entry_for(result, "cheap-one")["out_of_line"] is False
    assert entry_for(result, "heavy")["weighted_saving"] is None


def test_plugins_are_totalled_from_the_qualified_names(home):
    data = window(skills=[("glab:glab", 10, 5000000.0), ("glab:mr", 5, 1000000.0), ("plain", 1, 100.0)])
    result = build([data], home)
    assert result["plugins"][0]["plugin"] == "glab"
    assert result["plugins"][0]["weighted"] == 6000000.0
    assert result["plugins"][0]["centres"] == 2


def days(values, start="2026-08-22"):
    from datetime import date, timedelta

    first = date.fromisoformat(start)
    return [
        {"date": (first + timedelta(days=index)).isoformat(), "weighted": value, "turns": 1}
        for index, value in enumerate(values)
    ]


def test_the_day_series_fills_days_with_no_spend_and_accumulates():
    data = window(by_day=days([100.0, 0.0, 50.0])[:1] + days([100.0, 0.0, 50.0])[2:], total=150.0)
    result = tune.pacing(data, [], CEILING, tune.settings(CONFIG))
    assert [day["weighted"] for day in result["days"]] == [100.0, 0.0, 50.0, 0.0, 0.0, 0.0, 0.0]
    assert [day["cumulative"] for day in result["days"]][-1] == 150.0
    assert len(result["days"]) == 7


def test_a_window_that_crosses_the_ceiling_names_the_day_it_happened():
    data = window(by_day=days([400.0, 400.0, 400.0, 400.0]), total=1600.0)
    result = tune.pacing(data, [], 1000.0, tune.settings(CONFIG))
    assert result["exhausted_on_day"] == 3
    assert result["exhausted_on"] == "2026-08-24"
    assert result["approached_on_day"] == 3


def test_a_window_that_only_approaches_the_ceiling_is_labelled_as_approaching():
    data = window(by_day=days([500.0, 450.0]), total=950.0)
    result = tune.pacing(data, [], 1000.0, tune.settings(CONFIG))
    assert result["exhausted_on"] is None
    assert result["approached_on_day"] == 2


def test_front_loading_is_measured_against_an_even_week_and_against_active_days():
    data = window(by_day=days([900.0, 50.0, 50.0]), total=1000.0)
    result = tune.pacing(data, [], CEILING, tune.settings(CONFIG))
    assert result["front_load_share"] == pytest.approx(1.0)
    assert result["front_load_ratio"] == pytest.approx(7.0 / 3.0)
    assert result["front_loaded"] is True
    assert result["active_days"] == 3
    assert result["front_load_active_share"] == pytest.approx(0.95)


def test_a_back_loaded_window_is_not_called_front_loaded():
    data = window(by_day=days([0.0, 0.0, 0.0, 0.0, 100.0, 400.0, 500.0]), total=1000.0)
    result = tune.pacing(data, [], CEILING, tune.settings(CONFIG))
    assert result["front_loaded"] is False
    assert result["front_load_share"] == 0.0


def test_the_drivers_of_an_exhausted_window_stop_at_the_day_it_was_exhausted():
    data = window(by_day=days([600.0, 600.0, 600.0]), total=1800.0)
    records = [
        record("2026-08-22T10:00:00+00:00", agent="early", weighted=600.0, cwd="C:\\work\\repo-a"),
        record("2026-08-23T10:00:00+00:00", agent="early", weighted=600.0, cwd="C:\\work\\repo-a"),
        record("2026-08-24T10:00:00+00:00", agent="late", weighted=600.0, cwd="C:\\work\\repo-b"),
    ]
    result = tune.pacing(data, records, 1000.0, tune.settings(CONFIG))
    assert result["exhausted_on"] == "2026-08-23"
    assert result["driver_scope"] == "up to 2026-08-23"
    assert [name for name, _ in result["drivers"]["agent"]] == ["early"]
    assert [name for name, _ in result["drivers"]["repo"]] == ["repo-a"]


def test_a_window_that_never_approached_the_ceiling_reports_drivers_for_the_whole_window():
    data = window(by_day=days([1.0, 1.0]), total=2.0)
    records = [record("2026-08-27T10:00:00+00:00", agent="late", weighted=1.0)]
    result = tune.pacing(data, records, CEILING, tune.settings(CONFIG))
    assert result["driver_scope"] == "over the whole window"
    assert [name for name, _ in result["drivers"]["agent"]] == ["late"]


def test_the_current_window_projects_the_rate_forward_to_the_reset():
    current = window(
        start="2026-08-29", end="2026-09-05", end_utc="2026-09-04T22:00:00+00:00", total=300.0
    )
    pace = tune.current_pacing(current, [], 1000.0, tune.settings(CONFIG), now=NOW)
    assert pace["elapsed_days"] == pytest.approx(2.5)
    assert pace["burn_rate_per_day"] == pytest.approx(120.0)
    assert pace["projected_window_weighted"] == pytest.approx(840.0)
    assert pace["projected_exhaustion"] is None
    assert pace["exhausts_before_reset"] is False


def test_a_current_window_on_track_to_exhaust_names_the_day_and_the_overshoot():
    current = window(
        start="2026-08-29", end="2026-09-05", end_utc="2026-09-04T22:00:00+00:00", total=800.0
    )
    pace = tune.current_pacing(current, [], 1000.0, tune.settings(CONFIG), now=NOW)
    assert pace["exhausts_before_reset"] is True
    assert pace["projected_exhaustion"].startswith("2026-09-01")
    assert pace["overshoot_weighted"] == pytest.approx(1240.0)
    assert pace["sustainable_rate_per_day"] == pytest.approx(200.0 / pace["remaining_days"])
    rendered = tune.render(
        tune.build([window(ceiling=1000.0)], CONFIG, roots=[], records_by_window={}, current=current, now=NOW)
    )
    assert "PROJECTED EXHAUSTION" in rendered
    assert "has to come off" in rendered


def round_trip_window(resolved=2, total=10):
    return {
        "key": "week_2026_08_22",
        "records": 100,
        "weighted": 1000.0,
        "total_calls": total,
        "resolved_calls": resolved,
        "result_coverage": resolved / total if total else 0.0,
        "detectors": [
            {
                "key": "failed_tool_calls",
                "label": "tool calls that came back as an error",
                "count": 2,
                "weighted_cost": 100.0,
                "detail": "",
                "evidence": {},
            }
        ],
    }


def test_the_round_trip_section_states_the_share_of_calls_whose_outcome_is_stored(home):
    rendered = tune.render(build([window()], home, round_trips=[round_trip_window()]))
    assert "tool results recorded for 20.0%" in rendered
    assert "floor for this window" in rendered


def test_a_window_whose_outcomes_are_all_stored_carries_no_floor_warning(home):
    rendered = tune.render(build([window()], home, round_trips=[round_trip_window(resolved=10)]))
    assert "tool results recorded for 100.0%" in rendered
    assert "floor for this window" not in rendered


def test_every_proposal_states_that_quality_is_not_measurable(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus", description="Profiles a merge request")
    result = build([window(agents=[("mr-scout", 399, 20000000.0)])], home)
    entry = result["proposals"][0]
    assert entry["performance_risk"] == "low"
    assert "NOT measurable from this data" in entry["quality_risk"]
    assert entry["buys"] == "Profiles a merge request"


def test_the_report_never_claims_accuracy_is_preserved(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    rendered = tune.render(build([window(agents=[("mr-scout", 399, 20000000.0)])], home)).lower()
    assert "measures cost only" in rendered
    for claim in ("same accuracy", "without losing accuracy", "preserves accuracy", "no loss of quality"):
        assert claim not in rendered


def test_the_output_always_states_the_visibility_limit_and_that_nothing_was_applied(home):
    rendered = tune.render(build([window(agents=[("general-purpose", 2624, 208000000.0)])], home))
    assert "Visibility: tune sees only the agents and skills that actually ran" in rendered
    assert "Nothing was modified" in rendered
    assert "Cost is not waste" in rendered


def test_the_patch_replaces_an_existing_model_line(home):
    path = agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    result = build([window(agents=[("mr-scout", 399, 20000000.0)])], home)
    patch = result["proposals"][0]["patch"]
    assert "-model: opus" in patch and "+model: sonnet" in patch
    assert "model: opus" in path.read_text(encoding="utf-8")


def test_the_patch_adds_a_model_line_when_the_definition_inherits_one(home):
    agent_file(home / ".claude" / "agents", "mr-scout")
    patch = build([window(agents=[("mr-scout", 399, 20000000.0)])], home)["proposals"][0]["patch"]
    assert "+model: sonnet" in patch and "-model:" not in patch


def test_tune_has_no_flag_that_applies_anything():
    parser = cli.build_parser()
    actions = parser._subparsers._group_actions[0].choices["tune"]._actions
    flags = {option for action in actions for option in action.option_strings}
    assert not any("apply" in flag or "write" in flag or "fix" in flag for flag in flags)
    assert flags == {
        "-h",
        "--help",
        "--window",
        "--windows",
        "--min-saving",
        "--min-cost",
        "--json",
    }


def test_neither_tune_module_contains_a_file_writing_call():
    src = Path(__file__).resolve().parents[1] / "src"
    for name in ("tune.py", "rules.py"):
        source = (src / name).read_text(encoding="utf-8")
        for forbidden in ("write_text(", "write_bytes(", "os.replace", "shutil.", "unlink(", "mkdir("):
            assert forbidden not in source, "%s in %s" % (forbidden, name)
        assert "open(" not in source.replace("open(path, encoding=", "READ(")


def live(tmp_path, monkeypatch, home, extra=None):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "data-home"))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    import paths

    data_home = tmp_path / "data-home"
    paths.ensure_home(data_home)
    payload = window(agents=[("mr-scout", 399, 20000000.0)], skills=[("linear-mcp-cli", 187, 15700000.0)])
    (paths.data_dir(data_home) / "week_2026_08_22.json").write_text(json.dumps(payload), encoding="utf-8")
    (paths.data_dir(data_home) / "records" / "week_2026_08_22.jsonl").write_text(
        json.dumps(record("2026-08-22T10:00:00+00:00", agent="mr-scout", agent_id="a1")) + "\n", encoding="utf-8"
    )
    config = json.loads(paths.config_path(data_home).read_text(encoding="utf-8"))
    config["transcript_root"] = str(tmp_path / "transcripts")
    (tmp_path / "transcripts").mkdir(exist_ok=True)
    paths.config_path(data_home).write_text(json.dumps(config), encoding="utf-8")
    fixed = roots_for(home)
    monkeypatch.setattr(agentfiles, "default_roots", lambda **kwargs: fixed)
    return data_home


def test_running_tune_leaves_every_agent_and_skill_file_byte_identical(home, tmp_path, monkeypatch, capsys):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    skill_file(home / ".claude" / "skills", "linear-mcp-cli")
    (home / ".claude" / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    live(tmp_path, monkeypatch, home)
    before = {p: p.read_bytes() for p in sorted((home / ".claude").rglob("*")) if p.is_file()}
    assert cli.main(["tune"]) == 0
    after = {p: p.read_bytes() for p in sorted((home / ".claude").rglob("*")) if p.is_file()}
    assert before == after
    assert "PROPOSALS" in capsys.readouterr().out


def test_the_json_form_carries_the_pacing_centres_and_says_it_applies_nothing(home, tmp_path, monkeypatch, capsys):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    live(tmp_path, monkeypatch, home)
    assert cli.main(["tune", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applies_changes"] is False
    assert payload["windows"][0]["key"] == "week_2026_08_22"
    assert payload["pacing"][0]["key"] == "week_2026_08_22"
    assert any(centre["name"] == "mr-scout" for centre in payload["centres"])
    assert payload["round_trips"] is not None


def test_the_cli_flags_override_the_configured_floors(home, tmp_path, monkeypatch, capsys):
    agent_file(home / ".claude" / "agents", "mr-scout", model="opus")
    live(tmp_path, monkeypatch, home)
    assert cli.main(["tune", "--min-saving", "999999999"]) == 0
    assert "PROPOSALS (0)" in capsys.readouterr().out


def test_tune_writes_nothing_into_the_data_home(home, tmp_path, monkeypatch, capsys):
    data_home = live(tmp_path, monkeypatch, home)
    before = {p: p.stat().st_mtime_ns for p in sorted(data_home.rglob("*")) if p.is_file()}
    assert cli.main(["tune"]) == 0
    after = {p: p.stat().st_mtime_ns for p in sorted(data_home.rglob("*")) if p.is_file()}
    assert before == after
    capsys.readouterr()


def test_the_builtin_agent_list_and_cheap_families_live_in_the_shipped_config():
    assert tune.DEFAULTS["builtin_agents"] == []
    assert tune.DEFAULTS["cheap_model_families"] == []
    assert CONFIG["tune"]["builtin_agents"]
    assert CONFIG["tune"]["cheap_model_families"] == ["sonnet", "haiku"]
    assert tune.settings({})["builtin_agents"] == CONFIG["tune"]["builtin_agents"]
    assert tune.settings({})["cheap_model_families"] == CONFIG["tune"]["cheap_model_families"]


def test_a_user_can_override_the_builtin_agent_list(home):
    result = tune.build(
        [window(agents=[("general-purpose", 10, 9000000.0)])],
        dict(CONFIG, tune=dict(CONFIG["tune"], builtin_agents=[])),
        roots=roots_for(home),
        now=NOW,
    )
    assert "no agent definition found" in entry_for(result, "general-purpose")["note"]


def test_an_agent_is_only_already_right_sized_for_a_configured_cheap_family(home):
    agent_file(home / ".claude" / "agents", "mr-scout", model="sonnet")
    windows = four_windows([0, 0, 0, 0])
    for data in windows:
        data["by_agent"] = [{"key": "mr-scout", "turns": 100, "weighted": 20700000.0}]
    strict = dict(CONFIG, tune=dict(CONFIG["tune"], cheap_model_families=["haiku"]))
    assert entry_for(build(windows, home), "mr-scout")["status"] == tune.ALREADY_RIGHT_SIZED
    result = tune.build(windows, strict, roots=roots_for(home), now=NOW)
    assert entry_for(result, "mr-scout")["status"] == tune.PROPOSAL
