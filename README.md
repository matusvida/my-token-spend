# my-token-spend

Reports **your own** Claude Code token spend against **your real weekly reset window**, and says
what to change.

`ccusage` aggregates by session. Sessions routinely run for weeks, so a session that merely had
activity this week reports its whole multi-week cost against this week. This tool ignores sessions
as a unit and slices on the per-message timestamps in `~/.claude/projects/**/*.jsonl` instead, so
the question it answers is the one that matters: *what did I spend since the last reset, and on
what.*

It attributes spend to sessions, repos, branches, models, effort tiers and subagent types; explains
each week-over-week movement by a single named cause with a token figure attached; and turns that
into ranked, costed recommendations. Everything but one small narrative call per report is
deterministic and costs no tokens.

---

## ⚠️ Read this before you install

**Generated reports contain your own work content.** Prompt labels, branch names, repository paths
and session identifiers are written into the markdown and HTML pages verbatim. They are meant for
you alone. Do not commit them, paste them into a shared channel, publish them as an artifact, or
attach them to a ticket. This repository ships code only — no report has ever been committed to it,
and nothing in the plugin uploads or syncs your data anywhere.

**Uninstalling deletes your history.** All data lives under `${CLAUDE_PLUGIN_DATA}`, which Claude
Code removes when the plugin is uninstalled from every scope. Years of windows go with it. To keep
them:

```bash
claude plugin uninstall my-token-spend --keep-data
```

Data survives ordinary plugin *updates* — only a full uninstall wipes it.

---

## Install

This plugin lives in `plugins-incubator/`, which is a separate marketplace from the curated
`rohlik-skills` one. Add both: the incubator is a local directory inside the `rohlik-skills`
clone, so `rohlik-skills` is what actually fetches new commits.

```bash
claude plugin marketplace add https://gitlab.int.rohlikgroup.com/tools/rohlik-skills.git
claude plugin marketplace add ~/.claude/plugins/marketplaces/rohlik-skills/plugins-incubator
claude plugin install my-token-spend@rohlik-skills-incubator
```

To get a later version, update `rohlik-skills` before the incubator. In the other order the
incubator re-reads a directory that nothing has pulled into, and the new version looks missing.

```bash
claude plugin marketplace update rohlik-skills
claude plugin marketplace update rohlik-skills-incubator
```

Requires Python 3.11+ on `PATH`. Standard library only — nothing to `pip install`.

## Use

Inside a Claude Code session, `/my-token-spend` covers the normal flow. Directly:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" collect     # ingest transcripts (seconds, incremental)
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" report      # render the HTML page for this window
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" status      # where data lives and what is in it
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" tune        # pace the windows, find patterns, propose edits
```

In PowerShell, with no `sh` available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:CLAUDE_PLUGIN_ROOT/bin/my-token-spend.ps1" status
```

Both launchers pick the interpreter for you — `python3`, then `python`, then `py -3`, skipping the
Microsoft Store alias stub under `WindowsApps`, which exits without running anything. So you never
have to know which name your OS uses. `MY_TOKEN_SPEND_PYTHON` overrides the choice with an absolute
path. `python src/cli.py` from a clone works too, if you know your own interpreter's name.

| Subcommand | Useful flags |
|---|---|
| `collect` | `--backfill` re-read every transcript · `--recut-windows` re-bucket the stored records after the window boundary moves · `--reprice` re-price the stored records after a `model_weights` change · `--rebuild-from-transcripts-only` (destructive) · `--window YYYY-MM-DD` |
| `report` | `--no-narrative` skip the Claude call · `--all` rebuild every page · `--refresh-narrative` |
| `status` | `--set-reset-weekday DAY` |
| `quota` | none |
| `tune` | `--windows N` · `--window YYYY-MM-DD` · `--min-saving N` · `--min-cost N` · `--json` |
| `install-schedule` | `--register` · `--platform windows\|launchd\|cron` · `--log-retention N` |

## `tune` - pacing, patterns, and file-level proposals

`tune` answers one question: can this cost come down without the work getting worse. It aggregates
the last four **closed** windows by default, because advice fitted to a single heavy week is advice
about that week, not about how you work.

It measures cost. It cannot measure accuracy or answer quality - nothing in the transcript data
records whether an answer was right - and it says so wherever a proposal touches something whose
value it cannot see. The only speed figures it has are turn counts and wall-clock between
timestamps, and they are labelled as such.

Seven sections, in the order a reader needs them:

1. **Pacing and exhaustion** - per closed window, spend per day against the estimated ceiling, how
   front-loaded the burn was (against both the calendar week and the days you actually worked), the
   day the ceiling was reached or approached, and which repos, agents and skills drove the spend up
   to that day. Then the current window: burn rate, projected end-of-window total, projected
   exhaustion date if there is one, the sustainable rate needed to land inside the window, and where
   the spend has gone so far.
2. **Consistent cost centres** - agents and skills ranked by *typical* (median) cost per window, not
   by peak, with cost per run and median wall-clock per run. Anything seen in one window of four is
   labelled `one-off, not a pattern` and can never drive a config change.
3. **Wasted round trips** - measured, not inferred: tool calls that came back as an error, the
   subset re-issued with a byte-identical input after already failing, calls stopped by a permission
   decision, and turns the API itself errored. Each window states how much of it the transcripts
   still cover, because Claude Code prunes them and a partly-covered window yields a floor, not a
   rate.
4. **File-level proposals** - the resolved file, the cost, what the cost buys, the quality risk, and
   a unified diff to read and apply yourself.
5. **Setting-level proposals** - built-in agent types have no definition file, so their proposal
   names `env.CLAUDE_CODE_SUBAGENT_MODEL` in `settings.json` instead of a patch, states the current
   value, and spells out the blast radius: the setting is global, so every entry moves together.
6. **Reconciliation with the weekly report** - why the report's `model_mismatch` figure and the
   proposal totals differ, in terms of window scope, formula, and cost centres with no patchable
   target. Both are correct on their own basis; the gap is not an error.
7. **Cost without a proposal** - everything expensive that this tool will not offer a change for,
   with the reason.

```
backend-reviewer (agent)
  file: C:\Users\you\.claude\agents\backend-reviewer.md
  typically 23.7M weighted per window, seen in 2 of 4 windows, 334 turns over 7 agent run(s).
  What it buys: the server-side application layer of a merge request
  Proposal: set `model: sonnet` in the frontmatter. Same fan-out, cheaper workers.
  Est. saving 17.9M per window     Quality risk (low): you lose opus-level judgement on: ...
                                   Whether that changes the answers is NOT measurable from this data.

  --- a/.claude/agents/backend-reviewer.md
  +++ b/.claude/agents/backend-reviewer.md
  @@ -4,3 +4,3 @@
   tools: Read, Grep, Glob, Bash, Write, Skill
  -model: opus
  +model: sonnet
   ---
```

Three rules it keeps:

- **It proposes, it never applies.** There is no flag that edits a file. Whether a turn was trivial
  is an inference, and acting on a wrong inference degrades quality invisibly, which costs far more
  than the tokens saved.
- **Cost is not waste.** Every entry shows what the cost buys and states the quality risk. Anything
  whose impact cannot be judged from spend data is listed under *cost without a proposal* with no
  saving attached. Built-in agent types with no file - `general-purpose`, `Explore` - are never
  dropped, and that is usually where the largest single figure sits: they get a setting-level
  proposal against the global `CLAUDE_CODE_SUBAGENT_MODEL` default, with the blast radius stated,
  rather than a patch. An agent whose definition already asks for the cheaper tier is shown as
  already right-sized, with its cost still visible.
- **It only sees what ran.** A rarely-invoked but expensive component does not surface, and the
  output says so.

`--windows N` changes how many closed windows are aggregated; `--window YYYY-MM-DD` analyses exactly
one and says loudly that a single-window proposal is fitted to one week. `--json` emits the whole
result, and `--min-saving` / `--min-cost` move the two floors.

## The reset window

The weekly quota resets at an instant Anthropic decides, and `GET /api/oauth/usage` reports it.
Every `collect` reads the OAuth token Claude Code already stores, calls that endpoint with a ten
second timeout, and appends one sample to `data/quota_samples.jsonl`. The token is never refreshed:
Claude Code owns the refresh rotation and a second refresher would invalidate its session.

Windows are then cut at those instants, in UTC, and displayed in your timezone. `quota` shows the
latest sample and the ceiling fitted from the samples:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" quota
```

When the first sample moves a boundary under records already stored, `collect` says so:

```
WINDOW BOUNDARY CHANGED: 412 stored record(s) across week_2026_09_05 belong to a different window
under the reset instant Anthropic reports; re-cut every window with: collect --recut-windows
```

The re-cut re-buckets the stored records rather than re-reading transcripts, so no history can be
lost.

Without a token or a network the run still finishes, prints one `quota sample skipped` line, and
falls back to `reset_weekday` + `reset_hour`, which you set with:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" status --set-reset-weekday Saturday
```

The token and your transcripts can belong to different accounts. Nothing local can tell, so the
quota would be one account's and the spend another's.

## Why the record store is the source of truth

Claude Code prunes `~/.claude/projects/**/*.jsonl` over time, so a window's transcripts stop
existing long before you stop caring about the window. `data/records/week_*.jsonl` is therefore
the durable history, and every run unions freshly parsed records into it by `uuid` - `--backfill`
included. A run that would shrink a closed window refuses and names exactly what it would drop;
`--rebuild-from-transcripts-only` is the explicit opt-in that keeps only what today's transcripts
still contain. `--reprice` is the one other store-only path: it rewrites each record's weighted
cost from the raw token counts it already holds, so it can never drop one. Nothing prunes the
store; back it up if you back up anything.

## Scheduling

There is no plugin-level scheduler, so this stays at the OS level behind one subcommand. It is a
**dry run by default** and prints the exact definition it would install:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" install-schedule                      # show the plan
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" install-schedule --register           # Windows only
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" install-schedule --platform launchd   # macOS plists
sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend" install-schedule --platform cron      # crontab block
```

`collect` runs daily at 08:00, 13:00 and 18:00; `report` runs the day before your reset at 18:00.
Only Windows registers automatically — launchd and cron print definitions for you to install, with
the commands to do it. Every job pins the absolute interpreter path, appends UTF-8 logs to
`${CLAUDE_PLUGIN_DATA}/logs/`, prunes to the last 30 files per job, and records its exit code.

## Where your data lives

Everything persistent is under `${CLAUDE_PLUGIN_DATA}` — `~/.claude/plugins/data/{plugin-id}/`:

```
config.json     thresholds, model weights, your reset weekday
state.json      per-transcript read offsets, so collection stays incremental
data/           week_YYYY_MM_DD.json, the canonical aggregate per window
data/records/   the normalized records backing each window
reports/        week_YYYY_MM_DD.md and .html
logs/           scheduled-run logs
```

Nothing is ever written into the plugin's install directory. Run `status` to print the resolved
location.

Run outside a plugin context — from a clone — `${CLAUDE_PLUGIN_DATA}` is unset and everything
falls back to `~/.claude/plugins/data/my-token-spend/`. Set `MY_TOKEN_SPEND_DATA` to override
either case; it wins over both.

## Configuration

`config.json` in the data home is a copy of `src/config.default.json`, made on first run. Every
default lives there rather than in the code, so nothing needs a source edit:

| Key | Default | What it decides |
|---|---|---|
| `transcript_root` | `~/.claude/projects` | where transcripts are read from |
| `timezone` | `null` | the zone window boundaries are cut in. `null` means this machine's own zone, daylight saving included. Set an IANA name (`Europe/Prague`, `America/New_York`) to pin it — needed if you work across zones and want stable boundaries |
| `reset_weekday` / `reset_hour` | `Saturday` / `0` | where the weekly window starts when no quota sample has ever landed. The real reset instant wins whenever one has; see above |
| `ceiling.quota_fit` | `min_pct 10`, `min_samples 3`, `windows 3`, `fresh_hours 6` | the fit of your ceiling against reported utilization: the utilization floor a sample must clear, how many samples and how many windows it draws on, and how new a sample must be to be used as the percentage directly |
| `token_class_weights`, `model_weights`, `default_model_weight` | see file | the weighted-cost unit |
| `model_weights` keys | exact model ids | matched exactly first, then by model family, so `claude-fable-5-1` and `claude-sonnet-5-20260130` price as fable and sonnet without an entry of their own |
| `narrative_model` | `sonnet` | model the one narrative call per report uses |
| `prompt_label_chars` | `400` | how much of a prompt is stored as a label. Raising it improves labels on turns collected from then on; it cannot improve history already stored, and the report says so when it detects shorter labels. A config written before this default changed keeps its own value — raise it by hand to benefit |
| `rootcause.min_centre_share` | `0.03` | the share of the window a cost centre must reach to get its own drill-down |
| `rootcause.max_centres`, `rootcause.max_clusters` | `6`, `8` | how many cost centres are drilled into, and how many job clusters each shows before the tail is folded into one row |
| `rootcause.max_findings` | `12` | how many findings get a root-cause line on the page |
| `ceiling.override` | `null` | pin the weekly ceiling instead of inferring it |
| `thresholds.*` | see file | when each rule fires |
| `thresholds.model_mismatch.downgrade_model` | `claude-sonnet-5` | the cheaper model every saving is priced against |
| `advice.sonnet_class_agents` | a review-agent list | agent types whose work is judged to survive the cheaper tier. **Replace this with your own agents** — an agent not listed is reported with its cost and no saving, never silently downgraded |
| `advice.min_saving`, `tune.min_saving`, `tune.min_cost` | `250000` | floors a proposal must clear |
| `tune.windows`, `tune.min_windows_for_proposal` | `4`, `2` | how many closed windows are aggregated, and how many a component must appear in |
| `tune.builtin_agents` | Claude Code's own agent types | names that get a setting-level proposal against `CLAUDE_CODE_SUBAGENT_MODEL` rather than "definition not found" |
| `tune.cheap_model_families` | `sonnet`, `haiku` | model families counted as already right-sized |

A model id is priced by exact match first, then by its family: `claude-fable-5-1` gets the
`claude-fable-5` weight, and `claude-sonnet-5-20260130` gets the `claude-sonnet-5` weight, so a
point release or a dated build is never quietly priced at `default_model_weight`. A family whose
configured members disagree on a weight is resolved to the weight most of them carry, and a genuine
tie prices its unlisted siblings at the default. An id that matches nothing is named on stderr on
every collect, with the weight it was priced at, and listed under `unknown_models` in the window
JSON.

Stored records keep the price they were collected at, so editing `model_weights` does not re-price
the past on its own. Every collect compares the stored records against the current weights and says
which windows drifted; `collect --reprice` then recomputes each stored record's weighted cost from
its raw token counts and rewrites every window. It reads the record store only, never a transcript,
and never drops a record. Follow it with `report --all` to re-render the pages, which costs no
tokens. Changing `downgrade_model` to a name with no configured weight is safe: the family weight,
or `default_model_weight`, is used instead.

## Reading the page

The HTML page is meant to be read top to bottom and abandoned early:

1. **Verdict** — where the window sits against the estimated ceiling, the burn rate against the
   sustainable rate, the change from last week, and the three top-ranked actions. For most visits
   this is the whole page.
2. **Findings** — every rule that fired, ranked by cost, each with one line saying what the work
   behind it was: how many runs, which agent types and skills, in which repo and branch, over what
   span, with which tools. The supporting numbers are one click away under *Evidence*.
3. **What the big cost centres did** — a label like `general-purpose` is a dispatch name, not a
   job. Each large agent type and skill is split into the jobs its runs actually did, with a cost
   per job and a stated confidence.
4. **Raw breakdowns** — everything the page used to open with, unchanged and collapsed:
   cross-week comparison, delta decomposition, daily burn, the per-dimension breakdowns, top
   sessions, whale turns and the headline tiles.
5. **Rule lenses** and **Recommendations**, as before.

The root-cause lines are derived on your machine from the stored records, with no LLM involved. They
say **what the work was, never why anyone chose it** — intent is not in this data, so nothing on the
page claims a decision was wrong, a component unnecessary or a cost avoidable. Clusters are formed
on tool mix, working directory, branch and agent type, which every turn carries in full; the prompt
is used only as a human label because it is stored truncated and often starts with skill
boilerplate. Tool calls are recorded on roughly half of all turns, so every tool share is printed
next to the coverage it was computed from.

## Reading the numbers

Costs are in a **weighted** unit, not raw tokens: cache reads are ~0.1x fresh input, cache writes
1.25x, output 5x, and models differ by up to 5x. Weights live in `config.json` and are copied into
every window file, so a historical window stays priced the way it was collected. A weighted figure
is not comparable to a token count from anywhere else.

The **ceiling** is inferred from the top cluster of your own historical windows, because no local
source of truth for the weekly limit exists. It is an estimate, is labelled as one everywhere, and
can be pinned by hand with `ceiling.override` in `config.json`.

**Savings are upper bounds and they overlap.** One turn can be both a whale and an Opus turn that
should have been Sonnet, so the same tokens appear under two recommendations. They are alternative
framings of one window, never a list to add up.

## Development

The plugin ships prompt and source files only. It has no runtime dependency beyond the Python
standard library, and nothing needs to be built or installed to run it.

A pytest suite (411 tests, stdlib + `pytest` only) exists but is **maintained outside the published
plugin**, in the author's working tree. Nothing in this marketplace runs it, so shipping it here
would only look like a gate that does not exist. If you are changing the source and want the suite,
ask the plugin author (see `author` in `.claude-plugin/plugin.json`) for the current copy — there is
no separate public repository for it.

The reasoning the suite pinned is not lost with it: the invariants that must not be broken are
written down in [docs/design.md](docs/design.md#safety-invariants--do-not-break-these), and any
change to this plugin should be read against that section first.

Bump `report.REPORT_FORMAT_VERSION` in the same commit as any change to what `render_html`
produces. Pages carry that version in a `<meta>` stamp, and every run rebuilds pages below it —
free, because a rebuild reuses the narrative already embedded in the stale page and never calls the
API. A missed bump silently freezes old pages at the old format.
