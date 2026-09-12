import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import paths


def test_plugin_data_env_wins_over_the_standalone_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MY_TOKEN_SPEND_DATA", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    assert paths.data_home() == tmp_path / "plugin"
    assert paths.data_home_source() == "CLAUDE_PLUGIN_DATA"


def test_explicit_override_wins_over_the_plugin_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "explicit"))
    assert paths.data_home() == tmp_path / "explicit"
    assert paths.data_home_source() == "MY_TOKEN_SPEND_DATA"


def test_standalone_context_falls_back_under_the_claude_root(monkeypatch):
    monkeypatch.delenv("MY_TOKEN_SPEND_DATA", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert paths.data_home() == Path.home() / ".claude" / "plugins" / "data" / "my-token-spend"
    assert paths.data_home_source() == "standalone-default"


def test_a_blank_env_var_is_not_a_data_home(monkeypatch):
    monkeypatch.delenv("MY_TOKEN_SPEND_DATA", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "   ")
    assert paths.data_home_source() == "standalone-default"


def test_ensure_home_creates_every_directory_and_seeds_the_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    home = paths.ensure_home()
    assert (home / "data" / "records").is_dir()
    assert (home / "reports").is_dir()
    assert (home / "logs").is_dir()
    seeded = json.loads(paths.config_path(home).read_text(encoding="utf-8"))
    assert seeded["reset_weekday"] in (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    )


def test_ensure_home_never_overwrites_an_existing_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    home = paths.ensure_home()
    paths.config_path(home).write_text(json.dumps({"reset_weekday": "Tuesday"}), encoding="utf-8")
    paths.ensure_home()
    assert json.loads(paths.config_path(home).read_text(encoding="utf-8")) == {"reset_weekday": "Tuesday"}


def test_nothing_resolves_inside_the_plugin_install_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    for resolved in (paths.config_path(), paths.state_path(), paths.data_dir(), paths.reports_dir(), paths.logs_dir()):
        assert paths.PLUGIN_ROOT not in resolved.parents


def test_effective_config_falls_back_to_the_shipped_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "empty"))
    assert paths.effective_config_path() == paths.DEFAULT_CONFIG
    assert paths.load_config()["reset_weekday"] == "Saturday"
