import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH_LAUNCHER = ROOT / "bin" / "my-token-spend"
PS_LAUNCHER = ROOT / "bin" / "my-token-spend.ps1"
DOCS = [ROOT / "README.md", ROOT / "commands" / "my-token-spend.md", ROOT / "skills" / "my-token-spend" / "SKILL.md"]


def env(tmp_path, **extra):
    merged = dict(os.environ, MY_TOKEN_SPEND_DATA=str(tmp_path / "home"))
    merged.update(extra)
    return merged


def test_both_launchers_are_shipped():
    assert SH_LAUNCHER.is_file()
    assert PS_LAUNCHER.is_file()
    assert SH_LAUNCHER.read_text(encoding="utf-8").startswith("#!/bin/sh")


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on this machine")
def test_the_posix_launcher_runs_the_cli_without_naming_an_interpreter(tmp_path):
    result = subprocess.run(
        [shutil.which("sh"), str(SH_LAUNCHER), "status"],
        capture_output=True,
        text=True,
        env=env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "data home" in result.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on this machine")
def test_the_posix_launcher_honours_an_explicit_interpreter(tmp_path):
    result = subprocess.run(
        [shutil.which("sh"), str(SH_LAUNCHER), "status"],
        capture_output=True,
        text=True,
        env=env(tmp_path, MY_TOKEN_SPEND_PYTHON=sys.executable),
    )
    assert result.returncode == 0, result.stderr
    assert "data home" in result.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on this machine")
def test_the_posix_launcher_skips_a_windowsapps_stub(tmp_path):
    stub_dir = tmp_path / "WindowsApps"
    stub_dir.mkdir(parents=True)
    stub = stub_dir / "python3"
    stub.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    stub.chmod(0o755)
    result = subprocess.run(
        [shutil.which("sh"), str(SH_LAUNCHER), "status"],
        capture_output=True,
        text=True,
        env=env(tmp_path, PATH=str(stub_dir) + os.pathsep + os.environ.get("PATH", "")),
    )
    assert result.returncode == 0, result.stderr
    assert "data home" in result.stdout


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="no PowerShell on this machine")
def test_the_powershell_launcher_runs_the_cli(tmp_path):
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS_LAUNCHER), "status"],
        capture_output=True,
        text=True,
        env=env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "data home" in result.stdout


def test_no_document_tells_the_user_to_invoke_python3_directly():
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        assert "python3 " not in text, path.name
        assert "src/cli.py" not in text or "bin/my-token-spend" in text
