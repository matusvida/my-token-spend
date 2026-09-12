---
description: Where your tokens went this reset window.
argument-hint: "[collect|report|status|tune]"
allowed-tools: Bash, Read, AskUserQuestion
---

Run the my-token-spend CLI with the arguments the user gave in `$ARGUMENTS`, defaulting to
`collect` followed by `report` when they gave none.

The entry point is always:

```
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" <subcommand> [flags]
```

The launcher finds the interpreter itself, so never substitute `python3`, `python` or `py` for it:
on Windows the name `python3` resolves to the Microsoft Store alias stub, which exits without
running anything. If only PowerShell is available, the equivalent is
`powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend.ps1" <subcommand>`.

Subcommands:

- `collect [--backfill] [--recut-windows] [--reprice] [--rebuild-from-transcripts-only] [--window YYYY-MM-DD]`
- `report [--window YYYY-MM-DD] [--no-narrative] [--all] [--refresh-narrative]`
- `status [--set-reset-weekday DAY]`
- `tune [--windows N] [--window YYYY-MM-DD] [--min-saving N] [--min-cost N] [--json]`
- `install-schedule [--register] [--platform windows|launchd|cron] [--log-retention N]`

If any command prints `RESET DAY NOT CONFIRMED`, ask the user with AskUserQuestion which weekday
their Claude usage limit resets on, then run
`sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" status --set-reset-weekday <Day>` followed by
`collect --recut-windows` if it reports the weekday changed. Do not guess the weekday, and never
run `collect --rebuild-from-transcripts-only` - it discards history whose transcripts Claude Code
has already pruned.

If a command prints `UNKNOWN MODEL`, a model id in the transcripts matches nothing in
`model_weights` and was priced at the default weight; tell the user the name and the weight rather
than leaving it in the window JSON. If it prints `PRICING DRIFT`, the stored records were priced
with weights that no longer match the config: `collect --reprice` re-prices them from the stored
token counts without reading a transcript, and `report --all` re-renders the pages for free.

`tune` paces the last four closed windows against the estimated ceiling, ranks agents and skills by
their typical cost per window, measures failed and repeated tool round trips, and maps all of it
onto the definition files on disk with a patch per proposal. Built-in agent types have no file, so
their spend is proposed against `env.CLAUDE_CODE_SUBAGENT_MODEL` in `~/.claude/settings.json` and
the per-call `model` argument instead; `tune` reads that file and never writes it. It proposes only.
Never apply a patch it prints on the user's behalf
unless they ask for that specific file to be changed, and never edit an agent, skill, CLAUDE.md or
settings.json as a side effect of running it. A setting-level proposal is the widest change it can
name - relay its risk level with its figure. When relaying its output, keep the cost and the quality risk
together: a component that costs a lot is not thereby waste, and nothing in this data shows whether
a cheaper configuration answers as well. Never present a saving as free.

Report the path of the written HTML file back to the user, and with it the top three quantified
levers the run produced: each one's title, its weighted figure and its risk level, largest first.
Those aggregates are the answer the user asked for; never compress them into a half-sentence, and
never drop the largest one. If a lever is too broad to state safely in one line, say so and give
the figure anyway.

Everything else in the report stays in the file. Do not read prompt labels, session ids, branch
names, repo paths, file lists or any other work content into the conversation unless the user asks
for them - that content is theirs, the totals are not the same thing.
