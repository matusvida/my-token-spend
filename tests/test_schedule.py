import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cli
import paths

PYTHON = "/usr/bin/python3"
SCRIPT = "/opt/plugin/src/cli.py"
LOGS = str(paths.logs_dir())


@pytest.fixture
def jobs():
    return cli._jobs(PYTHON, SCRIPT, LOGS, 30, "Friday")


def test_platform_detection_maps_each_os(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    assert cli.schedule_platform() == "windows"
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    assert cli.schedule_platform() == "launchd"
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    assert cli.schedule_platform() == "cron"


def test_an_explicit_platform_overrides_detection(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    assert cli.schedule_platform("launchd") == "launchd"
    assert cli.schedule_platform("cron") == "cron"


def test_the_report_job_runs_the_day_before_the_reset():
    assert cli.report_weekday({"reset_weekday": "Saturday"}) == "Friday"
    assert cli.report_weekday({"reset_weekday": "Monday"}) == "Sunday"
    assert cli.report_weekday({"reset_weekday": "Sunday"}) == "Saturday"


def test_windows_triggers_are_a_comma_separated_powershell_array(jobs):
    script = cli.render_windows_schedule(jobs)
    assert "$triggers = @((New-ScheduledTaskTrigger -Daily -At '08:00'), " \
           "(New-ScheduledTaskTrigger -Daily -At '13:00'), (New-ScheduledTaskTrigger -Daily -At '18:00'))" in script
    assert "$triggers = @((New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At '18:00'))" in script


def test_windows_schedule_registers_both_jobs_under_its_own_folder(jobs):
    script = cli.render_windows_schedule(jobs)
    assert script.count("Register-ScheduledTask") == 2
    assert "-TaskPath '\\MyTokenSpend\\' -TaskName 'Collect'" in script
    assert "-TaskPath '\\MyTokenSpend\\' -TaskName 'Report'" in script
    assert "ClaudeSpent" not in script


def test_windows_schedule_uses_an_absolute_interpreter_and_prunes_logs(jobs):
    script = cli.render_windows_schedule(jobs)
    assert PYTHON in script
    assert SCRIPT in script
    assert "-Encoding utf8" in script
    assert "Select-Object -Skip 30 | Remove-Item" in script


def test_launchd_emits_one_plist_per_job_with_the_right_calendar(jobs):
    plist = cli.render_launchd(jobs)
    assert plist.count("<?xml") == 2
    assert "local.my-token-spend.collect" in plist
    assert "local.my-token-spend.report" in plist
    for hour in (8, 13, 18):
        assert "<key>Hour</key><integer>%d</integer>" % hour in plist
    assert "<key>Weekday</key><integer>5</integer>" in plist
    assert "2>&1" not in plist


def test_cron_emits_a_marked_block_with_correct_specs(jobs):
    block = cli.render_cron(jobs)
    assert block.startswith("# my-token-spend (managed block)")
    assert block.rstrip().endswith("# my-token-spend (end)")
    assert "0 8,13,18 * * *" in block
    assert "0 18 * * 5" in block
    assert PYTHON in block


def test_posix_jobs_redirect_and_prune_their_logs(jobs):
    for job in jobs:
        command = job["posix_command"]
        assert "mkdir -p '%s'" % LOGS in command
        assert 'echo "=== exit $? ==="' in command
        assert "tail -n +31 | xargs -r rm -f" in command


def test_render_schedule_dispatches_on_the_target(jobs):
    assert cli.render_schedule("windows", jobs) == cli.render_windows_schedule(jobs)
    assert cli.render_schedule("launchd", jobs) == cli.render_launchd(jobs)
    assert cli.render_schedule("cron", jobs) == cli.render_cron(jobs)


def test_a_non_saturday_reset_moves_every_platforms_report_job():
    jobs = cli._jobs(PYTHON, SCRIPT, LOGS, 30, "Tuesday")
    assert "-DaysOfWeek Tuesday" in cli.render_windows_schedule(jobs)
    assert "<key>Weekday</key><integer>2</integer>" in cli.render_launchd(jobs)
    assert "0 18 * * 2" in cli.render_cron(jobs)


def test_a_dry_run_registers_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "register_windows", lambda jobs: pytest.fail("dry run must not register"))
    assert cli.main(["install-schedule", "--platform", "cron"]) == 0
    assert "This was a dry run" in capsys.readouterr().out


def test_register_is_refused_on_non_windows_targets(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MY_TOKEN_SPEND_DATA", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "register_windows", lambda jobs: pytest.fail("must not register"))
    assert cli.main(["install-schedule", "--platform", "launchd", "--register"]) == 1
    assert "only implemented for Windows" in capsys.readouterr().err


def test_registering_replaces_tasks_of_the_same_name(jobs):
    assert cli.render_windows_schedule(jobs).count("-Force | Out-Null") == 2


def _cron_command_field(cron_line):
    field = cron_line.split(" ", 5)[5]
    out = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and index + 1 < len(field) and field[index + 1] == "%":
            out.append("%")
            index += 2
            continue
        if char == "%":
            break
        out.append(char)
        index += 1
    return "".join(out)


def test_cron_escapes_every_percent_so_the_command_field_is_not_terminated(jobs):
    block = cli.render_cron(jobs)
    assert r"+\%Y-\%m-\%d" in block
    for line in block.splitlines():
        if line.startswith("#"):
            continue
        assert "%" in line
        assert not re.search(r"(?<!\\)%", line)


def test_cron_survives_crons_own_percent_handling_intact(jobs):
    for line in cli.render_cron(jobs).splitlines():
        if line.startswith("#"):
            continue
        assert _cron_command_field(line).endswith("xargs -r rm -f'")
        assert "$(date +%Y-%m-%d)" in _cron_command_field(line)


def test_cron_single_quotes_the_command_so_the_outer_shell_expands_nothing(jobs):
    for line in cli.render_cron(jobs).splitlines():
        if line.startswith("#"):
            continue
        command = line.split("/bin/sh -c ", 1)[1]
        assert command.startswith("'") and command.endswith("'")
        assert '"' not in command.split("log=")[0]
        assert '\\"' not in command


def test_cron_field_escapes_embedded_single_quotes():
    assert cli.cron_field("a'b") == r"'a'\''b'"
    assert cli.cron_field("100%") == r"'100\%'"


def test_launchd_and_plain_shell_keep_a_bare_percent(jobs):
    plist = cli.render_launchd(jobs)
    assert "+%Y-%m-%d" in plist
    assert "\\%" not in plist
    for job in jobs:
        assert "+%Y-%m-%d" in job["posix_command"]
        assert "\\%" not in job["posix_command"]


def test_the_windows_rendering_uses_no_percent_at_all(jobs):
    script = cli.render_windows_schedule(jobs)
    assert "Get-Date -Format yyyy-MM-dd" in script
    assert "%" not in script
