import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rootcause
import rules

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "src" / "config.default.json").read_text())
W = CONFIG["token_class_weights"]


def rec(
    ts,
    session="s1",
    model="claude-opus-5",
    input=0,
    output=0,
    cache_create=0,
    cache_read=0,
    sidechain=False,
    agent=None,
    skill=None,
    run=None,
    tools=(),
    prompt=None,
    uuid="u",
    cwd="C:\\workspace\\srst",
    branch="master",
    thinking=0,
):
    weighted = rules.model_weight(model, CONFIG)[0] * (
        W["input"] * input
        + W["output"] * output
        + W["cache_create"] * cache_create
        + W["cache_read"] * cache_read
    )
    return {
        "ts": ts,
        "uuid": uuid,
        "sessionId": session,
        "model": model,
        "model_known": True,
        "effort": "high",
        "isSidechain": sidechain,
        "agentId": run,
        "attributionAgent": agent,
        "attributionSkill": skill,
        "cwd": cwd,
        "gitBranch": branch,
        "version": "2.1.227",
        "input": input,
        "output": output,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "thinking": thinking,
        "weighted": weighted,
        "tools": [{"name": n, "hash": h} for n, h in tools],
        "text_chars": 0,
        "is_api_error": False,
        "prompt": prompt,
    }


def window(records, findings, weighted=None):
    total = weighted if weighted is not None else sum(record["weighted"] for record in records)
    return {
        "window": {"key": "week_2026_09_05"},
        "totals": {"weighted": total, "turns": len(records)},
        "findings": findings,
    }


def finding(rule, subject, cost=1000.0, **evidence):
    return {
        "rule": rule,
        "subject": subject,
        "detail": "%s on %s" % (rule, subject),
        "weighted_cost": cost,
        "evidence": evidence,
    }


def ts(minute, day=8, hour=10):
    return "2026-09-%02dT%02d:%02d:00+00:00" % (day, hour, minute)


def test_tool_category_maps_names_and_groups_mcp_by_server():
    assert rootcause.tool_category("Bash") == "shell"
    assert rootcause.tool_category("Grep") == "read"
    assert rootcause.tool_category("Edit") == "write"
    assert rootcause.tool_category("Agent") == "delegate"
    assert rootcause.tool_category("mcp__datadog-mcp__get_datadog_metric") == "mcp:datadog-mcp"
    assert rootcause.tool_category("mcp__dbqt__query") == "mcp:dbqt"
    assert rootcause.tool_category("SomethingNew") == "other"


def test_strip_boilerplate_removes_the_known_skill_prefixes():
    assert rootcause.strip_boilerplate(
        "Base directory for this skill: C:\\x\\y\\skills\\reply-style\n\n# reply-style\n\nThe user reads"
    ) == "The user reads"
    assert rootcause.strip_boilerplate(
        "First, invoke the `rohlik-query` skill for usage guidance. Query the promo table."
    ) == "Query the promo table."
    assert rootcause.strip_boilerplate(
        "Other agents active in this session, addressable via SendMessage({to: name})\nRebase the branch"
    ) == "Rebase the branch"


def test_label_falls_back_when_no_prompt_was_captured():
    assert rootcause.label_of(None) == "no prompt captured"
    assert rootcause.label_of("Base directory for this skill: C:\\x") == "no prompt captured"


def test_label_clips_on_a_word_boundary():
    label = rootcause.label_of("rebase the feature branch onto main and rerun the failing tests", limit=20)
    assert label.endswith("...")
    assert len(label) <= 23
    assert " bran" not in label.rstrip(".")


def test_runs_are_grouped_by_agent_id_and_carry_their_span():
    records = [
        rec(ts(0), sidechain=True, run="r1", agent="general-purpose", tools=(("Bash", "h1"),), output=10),
        rec(ts(30), sidechain=True, run="r1", agent="general-purpose", output=10),
        rec(ts(5), sidechain=True, run="r2", agent="Explore", tools=(("Read", "h2"),), output=10),
    ]
    runs = {run["id"]: run for run in rootcause.group_runs(records)}
    assert set(runs) == {"r1", "r2"}
    assert runs["r1"]["turns"] == 2
    assert runs["r1"]["minutes"] == 30.0
    assert runs["r1"]["dominant"] == "shell"
    assert runs["r2"]["agent"] == "Explore"


def test_turns_without_an_agent_id_form_no_run():
    records = [rec(ts(0), sidechain=True, agent="general-purpose", output=10)]
    assert rootcause.group_runs(records) == []


def test_overlap_detects_parallel_fan_out():
    parallel = [
        {"first_ts": ts(0), "last_ts": ts(30)},
        {"first_ts": ts(10), "last_ts": ts(40)},
        {"first_ts": ts(12), "last_ts": ts(20)},
    ]
    timing = rootcause.overlap(parallel)
    assert timing["parallel"] is True
    assert timing["peak"] == 3
    assert timing["overlapping_runs"] == 3


def test_overlap_reports_sequential_runs_as_not_parallel():
    sequential = [
        {"first_ts": ts(0), "last_ts": ts(10)},
        {"first_ts": ts(20), "last_ts": ts(30)},
    ]
    timing = rootcause.overlap(sequential)
    assert timing["parallel"] is False
    assert timing["peak"] == 1
    assert timing["overlapping_runs"] == 0


def _clustered_records():
    records = []
    for index in range(3):
        for turn in range(4):
            records.append(
                rec(
                    ts(turn, hour=10 + index),
                    sidechain=True,
                    run="bash%d" % index,
                    agent="general-purpose",
                    tools=(("Bash", "b%d%d" % (index, turn)),),
                    output=100,
                    prompt="Rebase the feature branch onto main in the pricing service, careful and faithful",
                    uuid="bash%d%d" % (index, turn),
                )
            )
    for turn in range(4):
        records.append(
            rec(
                ts(turn, hour=20),
                sidechain=True,
                run="dd",
                agent="general-purpose",
                cwd="C:\\workspace\\other",
                tools=(("mcp__datadog-mcp__get_datadog_metric", "d%d" % turn),),
                output=50,
                prompt="Determine what caused the improvement in JVM GC old gen size for the service",
                uuid="dd%d" % turn,
            )
        )
    return records


def test_clusters_split_by_tool_mix_and_repo_not_by_prompt_alone():
    clusters = rootcause.cluster_runs(rootcause.group_runs(_clustered_records()))
    assert len(clusters) == 2
    shell, mcp = clusters
    assert shell["runs"] == 3
    assert shell["dominant"] == "shell"
    assert shell["median_turns"] == 4.0
    assert mcp["runs"] == 1
    assert mcp["dominant"] == "mcp:datadog-mcp"
    assert mcp["repo"] == "other"


def test_a_cluster_of_similar_prompts_is_high_confidence():
    cluster = rootcause.cluster_runs(rootcause.group_runs(_clustered_records()))[0]
    assert cluster["label_source"] == "prompt"
    assert cluster["confidence"] == "high"
    assert cluster["agreement"] == 1.0
    assert cluster["mixed"] is False


def test_a_cluster_whose_runs_disagree_on_the_label_is_reported_as_mixed():
    records = []
    prompts = [
        "rebase the pricing branch onto main and rerun the failing integration tests",
        "write the quarterly promotion summary document for the marketing review",
        "investigate the courier settlement mismatch in the accounting export",
    ]
    for index, prompt in enumerate(prompts):
        for turn in range(3):
            records.append(
                rec(
                    ts(turn, hour=10 + index),
                    sidechain=True,
                    run="r%d" % index,
                    agent="general-purpose",
                    tools=(("Bash", "x%d%d" % (index, turn)),),
                    output=10,
                    prompt=prompt,
                    uuid="u%d%d" % (index, turn),
                )
            )
    cluster = rootcause.cluster_runs(rootcause.group_runs(records))[0]
    assert cluster["runs"] == 3
    assert cluster["mixed"] is True
    assert cluster["confidence"] == "low"
    assert cluster["label_source"] == "derived"
    assert cluster["label"] == "shell commands in srst on master"
    line = rootcause._cluster_line(cluster)
    assert "MIXED" in line
    assert "member labels include" in line
    assert "quarterly promotion summary" in line or "courier settlement" in line


def test_an_unusable_prompt_label_is_replaced_by_a_derived_one():
    records = [
        rec(
            ts(turn),
            sidechain=True,
            run="r1",
            agent="general-purpose",
            tools=(("Bash", "h%d" % turn), ("Write", "w%d" % turn)),
            output=10,
            prompt="Base directory for this skill: C:\\x\\y",
            uuid="u%d" % turn,
        )
        for turn in range(3)
    ]
    cluster = rootcause.cluster_runs(rootcause.group_runs(records))[0]
    assert cluster["label_source"] == "derived"
    assert cluster["label"] == "shell commands with file writes in srst on master"
    assert "label derived from tools" in rootcause._cluster_line(cluster)


def test_coverage_reports_the_tool_recording_share_honestly():
    records = [
        rec(ts(0), tools=(("Bash", "h"),), output=1),
        rec(ts(1), output=1),
        rec(ts(2), output=1),
        rec(ts(3), output=1),
    ]
    assert rootcause.coverage(records) == {"turns": 4, "tool_turns": 1, "share": 0.25}


def test_label_capture_detects_history_stored_below_the_current_setting():
    records = [rec(ts(0), prompt="x" * 160, output=1)]
    assert rootcause.label_capture(records, {"prompt_label_chars": 400}) == {
        "observed_max": 160,
        "configured": 400,
        "truncated": True,
    }
    assert rootcause.label_capture(records, {"prompt_label_chars": 160})["truncated"] is False


def test_storm_because_names_runs_agent_mix_and_parallelism():
    records = _clustered_records()
    findings = [finding("subagent_storm", "s1", 1000.0, cwd="C:\\workspace\\srst")]
    because = rootcause.explain(window(records, findings), records, CONFIG)[0]
    assert "4 subagent runs" in because["text"]
    assert "median 4 turns per run" in because["text"]
    assert "they ran one after another" in because["text"]
    assert any("agent types: general-purpose x16 turns" in point for point in because["points"])
    assert any("job cluster:" in point for point in because["points"])
    assert any("tool calls are recorded on" in point for point in because["points"])


def test_storm_because_states_when_subagent_turns_carry_no_run_id():
    records = [
        rec(ts(turn), sidechain=True, agent="general-purpose", output=100, uuid="u%d" % turn)
        for turn in range(4)
    ]
    findings = [finding("subagent_storm", "s1")]
    because = rootcause.explain(window(records, findings), records, CONFIG)[0]
    assert "carry no agentId" in because["text"]


def test_model_mismatch_because_names_the_tool_mix_and_the_attribution():
    records = [
        rec(ts(turn), model="claude-opus-5", output=10, tools=(("Bash", "h%d" % turn),), agent="general-purpose", sidechain=True, run="r1", uuid="m%d" % turn)
        for turn in range(7)
    ] + [
        rec(ts(20), model="claude-opus-5", output=10, tools=(("Read", "z"),), skill="glab", uuid="m9")
    ]
    findings = [finding("model_mismatch", "claude-opus-5")]
    because = rootcause.explain(window(records, findings), records, CONFIG)[0]
    assert "88% of those turns called a single Bash" in because["text"]
    assert "general-purpose" in because["text"]
    assert any("skills active on those turns: glab 1 turns" in point for point in because["points"])
    assert any("what the turn was for is not recorded" in point for point in because["points"])


def test_skew_because_costs_each_job_cluster():
    records = _clustered_records()
    findings = [finding("agent_type_skew", "general-purpose")]
    because = rootcause.explain(window(records, findings), records, CONFIG)[0]
    assert "general-purpose ran 4 times this window" in because["text"]
    assert "2 job clusters" in because["text"]
    assert len([point for point in because["points"] if point.startswith("job cluster:")]) == 2


def test_bloat_because_names_the_session_shape_and_the_repeated_inputs():
    records = [
        rec(ts(turn), cache_read=200000, output=10, tools=(("Read", "same"),), uuid="b%d" % turn)
        for turn in range(14)
    ]
    findings = [finding("context_bloat", "s1", 1000.0, cwd="C:\\workspace\\srst")]
    because = rootcause.explain(window(records, findings), records, CONFIG)[0]
    assert "14 turns over 0.2 h in srst" in because["text"]
    assert any("Read x14 identical calls" in point for point in because["points"])
    assert any("context read: peak 200,000 tokens" in point for point in because["points"])


def test_whale_because_splits_the_price_by_token_class():
    records = [
        rec(ts(0), uuid="whale", cache_create=100000, output=1000),
        rec(ts(1), uuid="other", output=1),
    ]
    findings = [finding("whale_turns", "s1", 1000.0, uuid="whale")]
    because = rootcause.explain(window(records, findings), records, CONFIG)[0]
    assert "turn 1 of 2 in that session" in because["text"]
    assert "context written to cache" in because["text"]
    assert any(point.startswith("weighted price by token class:") for point in because["points"])


def test_redundant_reads_because_names_the_span_and_the_agents():
    records = [
        rec(ts(turn * 5), tools=(("Read", "same"),), output=10, agent="general-purpose", sidechain=True, run="r1", uuid="r%d" % turn)
        for turn in range(3)
    ]
    findings = [finding("redundant_reads", "s1", 1000.0, tool="Read", input_hash="same", occurrences=3)]
    because = rootcause.explain(window(records, findings), records, CONFIG)[0]
    assert "the same Read input was issued on 3 turns" in because["text"]
    assert any("span: 10.0 minutes" in point for point in because["points"])
    assert any("the input text itself is not stored" in point for point in because["points"])


def test_an_unknown_rule_gets_no_because_line_rather_than_a_guess():
    records = [rec(ts(0), output=1)]
    findings = [finding("something_new", "s1")]
    assert rootcause.explain(window(records, findings), records, CONFIG) == [None]


def test_a_finding_whose_subject_is_absent_from_the_records_gets_no_because():
    records = [rec(ts(0), output=1)]
    findings = [finding("context_bloat", "missing-session")]
    assert rootcause.explain(window(records, findings), records, CONFIG) == [None]


def test_centres_rank_agents_and_skills_above_the_share_floor():
    records = _clustered_records() + [
        rec(ts(turn), skill="prose:reply-style", output=200, uuid="s%d" % turn) for turn in range(5)
    ]
    found = rootcause.centres(window(records, []), records, CONFIG)
    kinds = {(centre["kind"], centre["name"]) for centre in found}
    assert ("agent type", "general-purpose") in kinds
    assert ("skill", "prose:reply-style") in kinds
    agent = [centre for centre in found if centre["name"] == "general-purpose"][0]
    assert agent["runs"] == 4
    assert agent["run_unit"] == "run"
    assert sum(cluster["weighted"] for cluster in agent["clusters"]) == agent["weighted"]
    skill = [centre for centre in found if centre["name"] == "prose:reply-style"][0]
    assert skill["run_unit"] == "session"


def test_a_cheap_cost_centre_is_left_out():
    records = _clustered_records()
    found = rootcause.centres(window(records, [], weighted=1e12), records, CONFIG)
    assert found == []


def test_analyse_returns_nothing_without_records():
    assert rootcause.analyse(window([], []), [], CONFIG) is None


def test_analyse_lines_up_one_entry_per_finding():
    records = _clustered_records()
    findings = [finding("agent_type_skew", "general-purpose"), finding("context_bloat", "missing")]
    analysis = rootcause.analyse(window(records, findings), records, CONFIG)
    assert len(analysis["becauses"]) == len(findings)
    assert analysis["becauses"][1] is None
    assert analysis["runs"] == 4


def test_no_because_line_asserts_a_motive_or_calls_a_cost_wasteful():
    records = _clustered_records() + [
        rec(ts(0), model="claude-opus-5", output=10, tools=(("Bash", "t"),), uuid="mm"),
        rec(ts(1), cache_read=300000, output=10, tools=(("Read", "same"),), uuid="w1"),
    ]
    findings = [
        finding("subagent_storm", "s1"),
        finding("agent_type_skew", "general-purpose"),
        finding("model_mismatch", "claude-opus-5"),
        finding("context_bloat", "s1"),
        finding("whale_turns", "s1", 1.0, uuid="w1"),
        finding("redundant_reads", "s1", 1.0, tool="Read", input_hash="same"),
    ]
    banned = (
        "waste",
        "wasted",
        "unnecessary",
        "should have",
        "shouldn't",
        "mistake",
        "wrong",
        "pointless",
        "avoidable",
        "instead of",
        "because you",
        "because the user",
        "did not need",
        "no need",
    )
    for because in rootcause.explain(window(records, findings), records, CONFIG):
        if because is None:
            continue
        text = " ".join([because["text"]] + because["points"]).lower()
        for phrase in banned:
            assert phrase not in text, "%r appeared in %r" % (phrase, text)


def _dispatch(call_id, description, model="opus", prompt_chars=900):
    return {
        "tool_use_id": call_id,
        "ts": ts(0),
        "sessionId": "s1",
        "parent_uuid": "p1",
        "description": description,
        "subagent_type": "general-purpose",
        "model": model,
        "prompt_chars": prompt_chars,
        "prompt_head": description,
    }


def _dispatched_records(pairs):
    records = []
    for index, (run_id, call_id) in enumerate(pairs):
        for turn in range(3):
            record = rec(
                ts(turn, hour=10 + index),
                sidechain=True,
                run=run_id,
                agent="general-purpose",
                tools=(("Bash", "h%d%d" % (index, turn)),),
                output=10,
                prompt="do a thing",
                uuid="u%d%d" % (index, turn),
            )
            record["source_tool_use_id"] = call_id
            records.append(record)
    return records


def test_a_run_carries_the_description_of_the_call_that_dispatched_it():
    calls = {"toolu_1": _dispatch("toolu_1", "Implement section 2")}
    run = rootcause.group_runs(_dispatched_records([("r0", "toolu_1")]), agent_calls=calls)[0]
    assert run["description"] == "Implement section 2"
    assert run["requested_model"] == "opus"
    assert run["prompt_chars"] == 900


def test_a_run_with_no_matching_dispatch_carries_no_description():
    run = rootcause.group_runs(_dispatched_records([("r0", "toolu_missing")]), agent_calls={})[0]
    assert run["description"] is None
    assert run["requested_model"] is None


def test_runs_with_the_same_description_cluster_together_whatever_their_tools():
    calls = {
        "toolu_1": _dispatch("toolu_1", "Review the merge request"),
        "toolu_2": _dispatch("toolu_2", "Review the merge request"),
    }
    records = _dispatched_records([("r0", "toolu_1"), ("r1", "toolu_2")])
    clusters = rootcause.cluster_runs(rootcause.group_runs(records, agent_calls=calls))
    assert len(clusters) == 1
    assert clusters[0]["label"] == "Review the merge request"
    assert clusters[0]["label_source"] == "description"
    assert clusters[0]["runs"] == 2


def test_a_cluster_whose_runs_all_carry_a_description_is_named():
    calls = {"toolu_1": _dispatch("toolu_1", "Review the merge request")}
    clusters = rootcause.cluster_runs(
        rootcause.group_runs(_dispatched_records([("r0", "toolu_1")]), agent_calls=calls)
    )
    assert clusters[0]["confidence"] == "named"


def test_a_run_without_a_description_still_falls_back_to_the_derived_label():
    calls = {"toolu_1": _dispatch("toolu_1", "Review the merge request")}
    records = _dispatched_records([("r0", "toolu_1"), ("r1", "toolu_missing")])
    clusters = rootcause.cluster_runs(rootcause.group_runs(records, agent_calls=calls))
    labels = sorted(cluster["label"] for cluster in clusters)
    assert "Review the merge request" in labels
    assert any(cluster["label_source"] == "derived" for cluster in clusters)


def test_a_cluster_list_states_how_many_runs_a_description_was_recovered_for():
    calls = {"toolu_1": _dispatch("toolu_1", "Review the merge request")}
    runs = rootcause.group_runs(_dispatched_records([("r0", "toolu_1"), ("r1", "toolu_missing")]), agent_calls=calls)
    assert rootcause.description_coverage(runs) == {"described": 1, "runs": 2, "share": 0.5}
    assert rootcause.description_note(runs) == "descriptions recovered for 50% of runs"
