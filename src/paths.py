import json
import os
import shutil
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PLUGIN_ROOT / "src" / "config.default.json"
STANDALONE_DIR_NAME = "my-token-spend"


_SHIPPED = None


def shipped_config():
    global _SHIPPED
    if _SHIPPED is None:
        try:
            _SHIPPED = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _SHIPPED = {}
    return _SHIPPED


def data_home():
    for name in ("MY_TOKEN_SPEND_DATA", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(name)
        if value and value.strip():
            return Path(value).expanduser()
    return Path.home() / ".claude" / "plugins" / "data" / STANDALONE_DIR_NAME


def data_home_source():
    for name in ("MY_TOKEN_SPEND_DATA", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(name)
        if value and value.strip():
            return name
    return "standalone-default"


def config_path(home=None):
    return (home or data_home()) / "config.json"


def state_path(home=None):
    return (home or data_home()) / "state.json"


def data_dir(home=None):
    return (home or data_home()) / "data"


def reports_dir(home=None):
    return (home or data_home()) / "reports"


def logs_dir(home=None):
    return (home or data_home()) / "logs"


def ensure_home(home=None):
    home = Path(home) if home else data_home()
    for directory in (home, data_dir(home), data_dir(home) / "records", reports_dir(home), logs_dir(home)):
        directory.mkdir(parents=True, exist_ok=True)
    target = config_path(home)
    if not target.exists():
        shutil.copyfile(DEFAULT_CONFIG, target)
    return home


def effective_config_path():
    target = config_path()
    return target if target.exists() else DEFAULT_CONFIG


def load_config(path=None):
    return json.loads(Path(path or effective_config_path()).read_text(encoding="utf-8"))
