---
name: my-token-spend
description: Use when the user asks where their Claude Code tokens went, why a weekly limit is being approached or was exhausted early, what is burning their quota this window, wants a token spend report, or wants the agents and skills that burned the window right-sized.
---

# My Token Spend

Answers "what did I spend since the last reset, and on what" from the local transcript corpus.
`ccusage` aggregates by session, and sessions span weeks, so it cannot answer this. This tool
slices per message timestamp into the user's real reset window instead.

Everything runs through one entry point:

```
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" <subcommand>
```

`collect` ingests transcripts, `report` renders the HTML page, `status` shows what has been
collected and where it lives, `tune` paces the windows and maps agent and skill spend onto the
definition files on disk, `install-schedule` emits the OS scheduler definitions.

## Gotchas

- **Never name the interpreter yourself.** The launcher above resolves it. `python3` does not exist
  on a stock Windows install - the name resolves to the Microsoft Store alias stub, which exits
  without running anything - and `python` does not exist on many macOS and Linux installs. If only
  PowerShell is available, use
  `powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend.ps1"`.
  Keep `"${CLAUDE_PLUGIN_ROOT}"` quoted; the path can contain spaces.
- **Windows are cut at the reset instant Anthropic reports**, sampled once per collect. If a run
  prints `WINDOW BOUNDARY CHANGED`, run `collect --recut-windows` to re-slice the stored records.
  `quota sample skipped` means the endpoint or the token was unavailable; the run still finished and
  the cut fell back to the configured weekday.
- **`data/records/` is the durable history, not the transcripts.** Claude Code prunes transcripts, so
  never run `collect --rebuild-from-transcripts-only`; it drops every record whose transcript is gone.
- `UNKNOWN MODEL` means a model id matched nothing in `model_weights` and was priced at the default
  weight; relay the name. `PRICING DRIFT` means the stored records no longer match the configured
  weights: `collect --reprice` re-prices them from the store alone, then `report --all`.
- **Reports contain the user's own work content** - prompt labels, branch names, repo paths.
  Never paste them into a shared channel, an artifact, or a commit. Hand over the file path. The
  aggregate savings figures are not work content: always relay the top levers with their weighted
  figures and risk levels. Leaving a number out is a missing answer, not privacy.
- **Costs are in a weighted unit, not raw tokens.** Cache reads are ~0.1x, output ~5x, and models
  differ up to 5x. Do not compare a weighted figure to a raw token count from anywhere else.
- **Check which ceiling method the report names.** `quota-fit` and `override` are a real quota, so
  the percentage is a percentage of the quota. `top-cluster` is inferred from the user's own heavy
  weeks, so present it as an estimate of their own habit, never as a limit. `quota` prints the
  latest sample and the method.
- **`tune` opens with the decisions it needs from the user - relay that section first.** It lists
  every agent type that is on neither `advice.sonnet_class_agents` nor `advice.opus_class_agents`
  with its cost, run count and median thinking and output per turn, and the `config.json` line that
  classifies it. Nothing else in the run prices those agents until the user answers, so give them
  that section in full before any proposal. Do not edit `config.json` for them unless they ask.
- **`tune` proposes; it never applies.** It prints a patch per file-level proposal and changes
  nothing. Do not apply one for the user unless they ask for that file to be changed, and never edit
  an agent definition, a skill or a CLAUDE.md as a side effect of running it. The only file it writes
  is `data/tune_last.json` in the data home, which is how the next run reports what moved.
- **Built-in agent types are proposed against a setting, not a file.** `general-purpose`, `Explore`
  and the rest take their tier from `env.CLAUDE_CODE_SUBAGENT_MODEL` in `~/.claude/settings.json`
  and from the per-call `model` argument. `tune` reads that file to report the current value and
  never writes it; the global default re-tiers every built-in subagent at once, so relay it as the
  widest-blast-radius change the tool can name.
- **Cost is not waste, and quality is not measured.** Every `tune` figure is what a component cost,
  not what it wasted, and it only sees what actually ran in the analysed windows. Never relay a
  saving without the quality risk beside it, and never tell the user a change is free or preserves
  accuracy - this data cannot show that.
- **`tune` aggregates the last four closed windows by default.** A component seen in one window is a
  one-off and must not drive a config change; `--window` analyses a single week and says so.
- **`report` makes one headless Claude call** for the narrative. `--no-narrative` skips it. A
  format rebuild of old pages never calls the API.
- **The page opens with a Verdict block and then findings, each carrying a root-cause line** derived
  on this machine from the stored records - no extra API call. Those lines say what the work was:
  how many runs, which agent types and skills, in which repo, with which tools. They never say why
  anyone chose it, because intent is not in this data. Relay them the same way: never turn "this is
  what the work was" into "this was a mistake".

## Storage

All state lives under `${CLAUDE_PLUGIN_DATA}`: `config.json`, `state.json`, `data/`, `reports/`,
`logs/`. Nothing is ever written into the plugin install directory. Run `status` to print the
resolved location.
