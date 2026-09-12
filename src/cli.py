import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import paths

SUBCOMMANDS = ("collect", "report", "status", "tune", "install-schedule")

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

TASK_FOLDER = "\\MyTokenSpend\\"
LAUNCHD_PREFIX = "local.my-token-spend"
CRON_MARKER = "# my-token-spend"

INSTALL_HINTS = {
    "launchd": "Save each plist at the path above, then:\n"
    "  launchctl unload ~/Library/LaunchAgents/local.my-token-spend.*.plist 2>/dev/null\n"
    "  launchctl load  ~/Library/LaunchAgents/local.my-token-spend.collect.plist\n"
    "  launchctl load  ~/Library/LaunchAgents/local.my-token-spend.report.plist",
    "cron": "Replace the marked block in your crontab, keeping the marker lines:\n"
    "  crontab -l > /tmp/ct; $EDITOR /tmp/ct; crontab /tmp/ct",
}

COLLECT_TIMES = [(8, 0), (13, 0), (18, 0)]
REPORT_TIME = (18, 0)


def report_weekday(config):
    return WEEKDAYS[(WEEKDAYS.index(config["reset_weekday"]) - 1) % 7]


class CliError(Exception):
    pass


def resolve_python():
    candidate = os.environ.get("MY_TOKEN_SPEND_PYTHON")
    if candidate:
        if not Path(candidate).exists():
            raise CliError("MY_TOKEN_SPEND_PYTHON points at %s, which does not exist" % candidate)
        return str(Path(candidate).resolve())
    if sys.executable and "WindowsApps" not in sys.executable:
        return str(Path(sys.executable).resolve())
    for name in ("python3", "python", "py"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found:
            return str(Path(found).resolve())
    raise CliError("no usable python interpreter found; set MY_TOKEN_SPEND_PYTHON to an absolute path")


def cli_script():
    return str(Path(__file__).resolve())


def load_config(home):
    return json.loads(paths.config_path(home).read_text(encoding="utf-8"))


def save_config(home, config):
    paths.config_path(home).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def reset_confirmed(config):
    return bool(config.get("reset_weekday_confirmed"))


def unconfirmed_banner(config):
    if reset_confirmed(config):
        return None
    return (
        "RESET DAY NOT CONFIRMED: windows are being cut on %s, which is only a default. "
        'Set the real one with: status --set-reset-weekday <Monday..Sunday>' % config["reset_weekday"]
    )


def apply_detection(home, config, detection):
    if not detection:
        return None
    detected = detection.get("detected")
    if detection.get("ambiguous"):
        return (
            "reset detector saw conflicting weekdays %s in transcripts and changed nothing"
            % ", ".join(detection.get("candidates") or [])
        )
    if not detected or detected == config["reset_weekday"]:
        return None
    previous = config["reset_weekday"]
    config["reset_weekday"] = detected
    config["reset_weekday_confirmed"] = True
    config["reset_weekday_source"] = "detected"
    history = config.setdefault("reset_weekday_corrections", [])
    history.append(
        {"at": datetime.now(timezone.utc).isoformat(), "from": previous, "to": detected, "source": "transcript"}
    )
    save_config(home, config)
    return (
        "reset detector corrected the reset weekday from %s to %s; re-cut every window with: collect --recut-windows"
        % (previous, detected)
    )


def window_totals(data_dir):
    totals = {}
    for path in sorted(Path(data_dir).glob("week_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        totals[payload["window"]["key"]] = {
            "weighted": round(payload["totals"]["weighted"], 4),
            "turns": payload["totals"]["turns"],
            "sessions": payload["totals"]["sessions"],
        }
    return totals


def cmd_collect(args):
    import collect

    home = paths.ensure_home()
    config = load_config(home)
    if args.recut_windows and (args.backfill or args.rebuild_from_transcripts_only):
        raise CliError("--recut-windows re-buckets the stored records and cannot be combined with a transcript re-read")
    if args.reprice and (args.backfill or args.rebuild_from_transcripts_only or args.recut_windows or args.window):
        raise CliError("--reprice re-prices every stored record and cannot be combined with any other collect flag")

    try:
        if args.reprice:
            summary = collect.reprice(
                config=config,
                out_dir=home,
                state_path=paths.state_path(home),
            )
        elif args.recut_windows:
            summary = collect.recut(
                config=config,
                out_dir=home,
                state_path=paths.state_path(home),
                window=args.window,
            )
        else:
            root = Path(config["transcript_root"]).expanduser()
            if not root.exists():
                raise CliError("transcript root %s does not exist" % root)
            summary = collect.run(
                config=config,
                root=root,
                out_dir=home,
                state_path=paths.state_path(home),
                backfill=args.backfill,
                window=args.window,
                rebuild_from_transcripts_only=args.rebuild_from_transcripts_only,
            )
    except collect.CollectionError as error:
        raise CliError(str(error))

    for loss in summary.get("losses") or []:
        print(
            "DISCARDED %s: %d -> %d turns, %s -> %s weighted"
            % (
                loss["window"],
                loss["turns_before"],
                loss["turns_after"],
                collect._num(loss["weighted_before"]),
                collect._num(loss["weighted_after"]),
            ),
            file=sys.stderr,
        )

    for entry in summary.get("repriced") or []:
        print(
            "REPRICED %s: %s -> %s weighted over %d turns"
            % (
                entry["window"],
                collect._num(entry["weighted_before"]),
                collect._num(entry["weighted_after"]),
                entry["turns"],
            ),
            file=sys.stderr,
        )
    if summary.get("repriced"):
        print("re-render the pages the new prices belong to with: report --all", file=sys.stderr)
    for unknown in summary.get("unknown_models") or []:
        print(
            "UNKNOWN MODEL %s: %d turns priced at the default weight %s because no exact or family entry "
            "matches it in model_weights; add one in %s"
            % (unknown["model"], unknown["turns"], unknown["weight"], paths.config_path(home)),
            file=sys.stderr,
        )
    for drift in summary.get("pricing_drift") or []:
        print(
            "PRICING DRIFT %s: %d stored record(s) were priced with weights that differ from the current "
            "config; re-price the store with: collect --reprice"
            % (drift["window"], drift["records"]),
            file=sys.stderr,
        )

    print("data home : %s  (%s)" % (home, paths.data_home_source()))
    print(
        "scanned %d files, %d new records, %d records total, %d malformed lines"
        % (summary["files_scanned"], summary["new_records"], summary["total_records"], summary["malformed_lines"])
    )
    print("ceiling: %s (%s)" % (collect._num(summary["ceiling"]["estimate"]), summary["ceiling"]["method"]))
    for aggregate in summary["windows"]:
        print(
            "  %s  %s weighted  %d turns  %d sessions"
            % (
                aggregate["window"]["key"],
                collect._num(aggregate["totals"]["weighted"]),
                aggregate["totals"]["turns"],
                aggregate["totals"]["sessions"],
            )
        )

    note = apply_detection(home, config, summary.get("reset"))
    if note:
        print(note, file=sys.stderr)
    banner = unconfirmed_banner(load_config(home))
    if banner:
        print(banner, file=sys.stderr)
    return 0


def cmd_report(args):
    import report

    home = paths.ensure_home()
    argv = ["--data-dir", str(paths.data_dir(home)), "--report-dir", str(paths.reports_dir(home)),
            "--config", str(paths.config_path(home))]
    if args.window:
        argv += ["--window", args.window]
    if args.no_narrative:
        argv.append("--no-narrative")
    if args.all:
        argv.append("--all")
    if args.refresh_narrative:
        argv.append("--refresh-narrative")
    return report.main(argv)


def cmd_status(args):
    home = paths.ensure_home()
    config = load_config(home)

    if args.set_reset_weekday:
        weekday = args.set_reset_weekday.strip().capitalize()
        if weekday not in WEEKDAYS:
            raise CliError("--set-reset-weekday must be one of %s" % ", ".join(WEEKDAYS))
        changed = weekday != config["reset_weekday"]
        config["reset_weekday"] = weekday
        config["reset_weekday_confirmed"] = True
        config["reset_weekday_source"] = "user"
        save_config(home, config)
        print("reset weekday set to %s" % weekday)
        if changed:
            print("re-cut every window with: collect --recut-windows")
        else:
            print("window boundaries are unchanged, so nothing needs re-cutting")
        return 0

    totals = window_totals(paths.data_dir(home))
    print("data home        : %s  (%s)" % (home, paths.data_home_source()))
    print("config           : %s" % paths.config_path(home))
    print("reset weekday    : %s (%s)" % (config["reset_weekday"], config.get("reset_weekday_source", "default")))
    print("reset confirmed  : %s" % reset_confirmed(config))
    for correction in config.get("reset_weekday_corrections") or []:
        print("  corrected %s -> %s at %s" % (correction["from"], correction["to"], correction["at"]))
    state = paths.state_path(home)
    print("state            : %s (%s)" % (state, "present" if state.exists() else "missing"))
    if state.exists():
        stored = json.loads(state.read_text(encoding="utf-8"))
        print("transcripts seen : %d" % len(stored.get("files") or {}))
        detection = stored.get("reset") or {}
        if detection.get("candidates"):
            print("reset signals    : %s (ambiguous=%s)" % (", ".join(detection["candidates"]), detection.get("ambiguous")))
    print("windows          : %d" % len(totals))
    for key, value in totals.items():
        print("  %s  %15.4f weighted  %6d turns  %4d sessions" % (key, value["weighted"], value["turns"], value["sessions"]))
    reports = sorted(paths.reports_dir(home).glob("week_*.html"))
    print("html reports     : %d" % len(reports))
    if reports:
        print("latest report    : %s" % reports[-1])
    banner = unconfirmed_banner(config)
    if banner:
        print(banner, file=sys.stderr)
    return 0


def cmd_tune(args):
    import roundtrips
    import rules
    import tune

    home = paths.ensure_home()
    config = load_config(home)
    for name, value in (("min_saving", args.min_saving), ("min_cost", args.min_cost), ("windows", args.windows)):
        if value is not None:
            config.setdefault("tune", {})[name] = value

    windows = tune.load_windows(paths.data_dir(home))
    try:
        analysed, warning = tune.select_windows(windows, count=tune.settings(config)["windows"], window=args.window)
    except LookupError as error:
        raise CliError(str(error))

    store = paths.data_dir(home) / "records"
    open_window = tune.current_window(windows)
    current = None if args.window else open_window
    wanted = list(analysed) + ([current] if current else [])
    records_by_window = {
        data["window"]["key"]: roundtrips.load_records(store, data["window"]["key"]) for data in wanted
    }

    trips = None
    if not args.no_round_trips:
        index = roundtrips.scan(Path(config["transcript_root"]).expanduser())
        trips = []
        for data in analysed:
            key = data["window"]["key"]
            trips.append(dict(rules.round_trips(records_by_window.get(key) or [], index), key=key))

    result = tune.build(
        analysed,
        config,
        records_by_window=records_by_window,
        round_trips=trips,
        current=current,
        open_window=open_window,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(tune.render(result), end="")
    if warning:
        print(warning, file=sys.stderr)
    return 0


def _log_retention_snippet_posix(log_dir, prefix, retention):
    return "ls -1t '%s'/%s-*.log 2>/dev/null | tail -n +%d | xargs -r rm -f" % (log_dir, prefix, retention + 1)


def _posix_command(python, script, subcommand, log_dir, prefix, retention):
    return (
        "mkdir -p '%s'; "
        "log=\"%s/%s-$(date +%%Y-%%m-%%d).log\"; "
        "{ echo \"=== $(date -Iseconds) start ===\"; "
        "'%s' '%s' %s 2>&1; "
        "echo \"=== exit $? ===\"; } >> \"$log\" 2>&1; "
        "%s"
        % (log_dir, log_dir, prefix, python, script, subcommand, _log_retention_snippet_posix(log_dir, prefix, retention))
    )


def _windows_argument(python, script, subcommand, log_dir, prefix, retention):
    statements = [
        "New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % log_dir,
        "$log = Join-Path '%s' ('%s-' + (Get-Date -Format yyyy-MM-dd) + '.log')" % (log_dir, prefix),
        "('=== ' + (Get-Date -Format o) + ' start ===') | Out-File -LiteralPath $log -Append -Encoding utf8",
        "& '%s' '%s' %s *>&1 | ForEach-Object { $_.ToString() } | Out-File -LiteralPath $log -Append -Encoding utf8"
        % (python, script, subcommand),
        "('=== exit ' + $LASTEXITCODE + ' ===') | Out-File -LiteralPath $log -Append -Encoding utf8",
        "Get-ChildItem -LiteralPath '%s' -Filter '%s-*.log' | Sort-Object LastWriteTime -Descending | "
        "Select-Object -Skip %d | Remove-Item -Force -ErrorAction SilentlyContinue" % (log_dir, prefix, retention),
    ]
    return '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command "%s"' % "; ".join(statements)


def _jobs(python, script, log_dir, retention, weekday):
    return [
        {
            "name": "Collect",
            "prefix": "collect",
            "subcommand": "collect",
            "description": "my-token-spend: incremental transcript collection into the current reset window.",
            "schedule": "daily at 08:00, 13:00 and 18:00 local",
            "weekday": None,
            "windows_argument": _windows_argument(python, script, "collect", log_dir, "collect", retention),
            "posix_command": _posix_command(python, script, "collect", log_dir, "collect", retention),
        },
        {
            "name": "Report",
            "prefix": "report",
            "subcommand": "report",
            "description": "my-token-spend: weekly HTML report, the day before the reset.",
            "schedule": "weekly on %s at %02d:%02d local" % (weekday, REPORT_TIME[0], REPORT_TIME[1]),
            "weekday": weekday,
            "windows_argument": _windows_argument(python, script, "report", log_dir, "report", retention),
            "posix_command": _posix_command(python, script, "report", log_dir, "report", retention),
        },
    ]


def _windows_triggers(job):
    if job["name"] == "Collect":
        return ", ".join("(New-ScheduledTaskTrigger -Daily -At '%02d:%02d')" % t for t in COLLECT_TIMES)
    return "(New-ScheduledTaskTrigger -Weekly -DaysOfWeek %s -At '%02d:%02d')" % (
        job["weekday"],
        REPORT_TIME[0],
        REPORT_TIME[1],
    )


def render_windows_schedule(jobs):
    lines = []
    for job in jobs:
        lines.extend(
            [
                "# %s%s" % (TASK_FOLDER, job["name"]),
                "$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '%s'"
                % job["windows_argument"].replace("'", "''"),
                "$triggers = @(%s)" % _windows_triggers(job),
                '$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" '
                "-LogonType Interactive -RunLevel Limited",
                "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
                "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)",
                "Register-ScheduledTask -TaskPath '%s' -TaskName '%s' -Action $action -Trigger $triggers "
                "-Principal $principal -Settings $settings -Description '%s' -Force | Out-Null"
                % (TASK_FOLDER, job["name"], job["description"]),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def render_launchd(jobs):
    blocks = []
    for job in jobs:
        label = "%s.%s" % (LAUNCHD_PREFIX, job["prefix"])
        if job["name"] == "Collect":
            intervals = "".join(
                "      <dict><key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>\n" % t
                for t in COLLECT_TIMES
            )
            calendar = "    <key>StartCalendarInterval</key>\n    <array>\n%s    </array>\n" % intervals
        else:
            calendar = (
                "    <key>StartCalendarInterval</key>\n"
                "    <dict><key>Weekday</key><integer>%d</integer>"
                "<key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>\n"
                % (WEEKDAYS.index(job["weekday"]) + 1, REPORT_TIME[0], REPORT_TIME[1])
            )
        blocks.append(
            "~/Library/LaunchAgents/%s.plist\n"
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "  <dict>\n"
            "    <key>Label</key><string>%s</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            "      <string>/bin/sh</string>\n"
            "      <string>-c</string>\n"
            "      <string>%s</string>\n"
            "    </array>\n"
            "%s"
            "    <key>RunAtLoad</key><false/>\n"
            "  </dict>\n"
            "</plist>\n" % (label, label, job["posix_command"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), calendar)
        )
    return "\n".join(blocks)


def cron_field(command):
    quoted = "'" + command.replace("'", r"'\''") + "'"
    return quoted.replace("%", r"\%")


def render_cron(jobs):
    lines = [CRON_MARKER + " (managed block)"]
    for job in jobs:
        if job["name"] == "Collect":
            spec = "%d %s * * *" % (COLLECT_TIMES[0][1], ",".join(str(h) for h, _ in COLLECT_TIMES))
        else:
            spec = "%d %d * * %d" % (REPORT_TIME[1], REPORT_TIME[0], WEEKDAYS.index(job["weekday"]) + 1)
        lines.append("%s /bin/sh -c %s" % (spec, cron_field(job["posix_command"])))
    lines.append(CRON_MARKER + " (end)")
    return "\n".join(lines) + "\n"


def schedule_platform(override=None):
    if override:
        return override
    name = platform.system().lower()
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "launchd"
    return "cron"


def render_schedule(target, jobs):
    if target == "windows":
        return render_windows_schedule(jobs)
    if target == "launchd":
        return render_launchd(jobs)
    return render_cron(jobs)


def register_windows(jobs):
    script = render_windows_schedule(jobs)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CliError("registering scheduled tasks failed (exit %d):\n%s%s" % (result.returncode, result.stdout, result.stderr))
    return result.stdout.strip()


def cmd_install_schedule(args):
    home = paths.ensure_home()
    python = resolve_python()
    config = load_config(home)
    log_dir = str(paths.logs_dir(home))
    jobs = _jobs(python, cli_script(), log_dir, args.log_retention, report_weekday(config))
    target = schedule_platform(args.platform)

    print("my-token-spend - scheduled job plan")
    print("  platform    : %s" % target)
    print("  interpreter : %s" % python)
    print("  entry point : %s" % cli_script())
    print("  data home   : %s  (%s)" % (home, paths.data_home_source()))
    print("  log dir     : %s  (kept: %d files per job)" % (log_dir, args.log_retention))
    print("  reset day   : %s%s" % (config["reset_weekday"], "" if reset_confirmed(config) else " (UNCONFIRMED)"))
    for job in jobs:
        print("  %-8s %s" % (job["name"] + ":", job["schedule"]))
    print("")
    print(render_schedule(target, jobs))

    if not args.register:
        print("Nothing was registered. This was a dry run.")
        if target == "windows":
            print("Register with: install-schedule --register")
        else:
            print(INSTALL_HINTS[target])
            print("--register is Windows-only; install the definitions above yourself.")
        if args.platform and args.platform != schedule_platform():
            print("Rendered for %s from a %s machine, so the paths above are this machine's." % (target, schedule_platform()))
        return 0

    if target != "windows":
        raise CliError("--register is only implemented for Windows Task Scheduler; install the definition above by hand")
    output = register_windows(jobs)
    print("registered under %s" % TASK_FOLDER)
    if output:
        print(output)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="my-token-spend", description="Claude Code token spend, per reset window.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="ingest transcripts into the current reset window")
    collect_parser.add_argument("--backfill", action="store_true", help="ignore stored offsets and re-read every transcript")
    collect_parser.add_argument(
        "--recut-windows",
        action="store_true",
        help="re-bucket the stored records into windows under the current reset weekday, without reading transcripts",
    )
    collect_parser.add_argument(
        "--rebuild-from-transcripts-only",
        action="store_true",
        help="DESTRUCTIVE: discard the stored records and keep only what today's transcripts still contain",
    )
    collect_parser.add_argument(
        "--reprice",
        action="store_true",
        help="re-price every stored record with the current weights and rewrite every window; reads the store only",
    )
    collect_parser.add_argument("--window", metavar="YYYY-MM-DD", help="write only the window starting on this date")
    collect_parser.set_defaults(func=cmd_collect)

    report_parser = subparsers.add_parser("report", help="render the weekly HTML report")
    report_parser.add_argument("--window", metavar="YYYY-MM-DD", help="window start date; defaults to the current window")
    report_parser.add_argument("--no-narrative", action="store_true", help="skip the headless Claude narrative call")
    report_parser.add_argument("--all", action="store_true", help="force-rebuild every window's page")
    report_parser.add_argument("--refresh-narrative", action="store_true", help="regenerate prose on rebuilt pages")
    report_parser.set_defaults(func=cmd_report)

    status_parser = subparsers.add_parser("status", help="show where data lives and what has been collected")
    status_parser.add_argument("--set-reset-weekday", metavar="DAY", help="record the weekday the quota resets on")
    status_parser.set_defaults(func=cmd_status)

    tune_parser = subparsers.add_parser("tune", help="pace the window and map agent and skill spend onto the files on disk")
    tune_parser.add_argument("--window", metavar="YYYY-MM-DD", help="analyse only this window instead of the most recent closed ones")
    tune_parser.add_argument("--windows", type=int, metavar="N", help="how many closed windows to aggregate (default 4)")
    tune_parser.add_argument("--no-round-trips", action="store_true", help="skip the transcript scan for wasted round trips")
    tune_parser.add_argument("--min-saving", type=float, metavar="N", help="weighted floor a proposal must clear")
    tune_parser.add_argument("--min-cost", type=float, metavar="N", help="weighted floor for reporting a component with no proposal")
    tune_parser.add_argument("--json", action="store_true", help="emit the machine-readable form instead of prose")
    tune_parser.set_defaults(func=cmd_tune)

    schedule_parser = subparsers.add_parser("install-schedule", help="emit or register OS scheduler definitions")
    schedule_parser.add_argument("--register", action="store_true", help="actually register the jobs (Windows only)")
    schedule_parser.add_argument("--platform", choices=["windows", "launchd", "cron"], help="render for another platform")
    schedule_parser.add_argument("--log-retention", type=int, default=30, help="log files kept per job")
    schedule_parser.set_defaults(func=cmd_install_schedule)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as error:
        print("my-token-spend: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
