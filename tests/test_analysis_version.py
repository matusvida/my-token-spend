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
