# my-token-spend — Design, as built

Originally drafted: 2026-08-27, as "Claude Token Burn Guardrail"
Last reconciled with the code: 2026-09-02
Owner: Matúš Vida
Status: **implemented and shipping.** This document describes what the code does now, not what was
planned. Decisions that were made and later replaced are listed under
[Superseded decisions](#superseded-decisions) at the end rather than silently deleted, so the
reasoning stays readable.

## Safety invariants — do not break these

**Read this section before changing anything in `src/`.**

Each item below was, until 2026-09-04, enforced by an executable test. That suite is no longer
shipped with the plugin (see [Testing](#testing)), so these statements are now the only guard. They
are not style preferences and not "current behaviour that happens to work" — each one exists because
breaking it destroys data, misleads the user, or silently edits their machine. A change that
contradicts any of them is a bug, whatever else it improves.

### Data durability — the records store is the truth

1. **The record store is the durable source of truth; transcripts are ephemeral.** Claude Code
   prunes `~/.claude/projects/**/*.jsonl` on its own schedule. `data/records/<window>.jsonl` is
   what survives. Never make a code path that treats a transcript as authoritative over a stored
   record, and never derive a window total by re-reading transcripts when the store has the answer.
2. **`collect --backfill` is union-only. It can never shrink a window.** Backfill re-reads
   transcripts from offset zero and *unions* by `uuid` into the store. Re-parsing the same
   transcript twice must not duplicate a `uuid`; a transcript that has since been pruned must not
   remove the records it originally contributed.
3. **The closed-window shrink guard must refuse before writing.** If a collect would leave a
   *closed* window with fewer records than the store already holds, it raises `CollectionError`,
   names the window, names how many records would be dropped, and points at
   `--rebuild-from-transcripts-only`. The refusal happens *before* anything is written. This is not
   theoretical: the bug this guard exists to prevent already destroyed real data once — a window
   fell from **5,320 turns to 1,566** because pruned transcripts were allowed to overwrite the
   store. The *current* (open) window is deliberately not guarded; only closed windows are frozen.
   One path is deliberately exempt from *half* of this guard: `collect --reprice` exists to move a
   closed window's weighted total, so it **reports** each window's before and after instead of
   refusing on the weighted comparison. The record-count half is still enforced hard, and refusal
   still happens before any write. See invariant 19 — this is a reviewed, deliberate softening, not
   an oversight.
4. **`--rebuild-from-transcripts-only` is the only destructive path, and it stays explicitly
   opt-in.** It is the sole way to make a window shrink. It must report every loss it caused. No
   other flag may acquire this behaviour, and it must never be implied by, or defaulted on from,
   anything else. (It does imply `--backfill`; that direction is safe.)
5. **`--recut-windows` never reads transcripts** — and neither does `--reprice` (invariant 19); a
   flag that only re-derives from the store must never acquire a transcript read. Re-bucketing
   after a `reset_weekday` change re-partitions the existing store and nothing else. It must lose
   no record, be idempotent, and fail loudly on an empty store. It cannot be combined with a
   transcript re-read — the CLI rejects `--recut-windows --backfill` rather than guessing.
6. **A pruned transcript cannot invent findings.** Round-trip analysis runs over the stored records
   and reports the share of tool calls whose outcome was captured; a call stored before outcomes
   were recorded counts as unresolved, never as a success.

### `tune` proposes and never applies

7. **No `tune` code path may write to any agent, skill or `CLAUDE.md` file.** `tune.py` and
   `rules.py` contain no file-writing call at all — no `write_text`, `write_bytes`,
   `os.replace`, `shutil.*`, `unlink`, `mkdir`, and no `open()` except for reading. Running `tune`
   must leave every file under `~/.claude` byte-identical, and must write nothing into the data
   home either.
8. **No auto-apply flag may be added.** `tune` proposes a *patch as text*; the user applies it. Any
   flag whose name contains `apply`, `write` or `fix` is forbidden by construction. The `--json`
   form must keep stating `applies_changes: false`.

### Honesty about what is and is not measured

9. **The report must never claim a change preserves accuracy or quality.** Cost is measured;
   quality is not recorded anywhere in this data. Every proposal carries an explicit
   "NOT measurable from this data" quality risk, and the rendered output must never contain
   "same accuracy", "without losing accuracy", "preserves accuracy" or "no loss of quality".
10. **Every run states its own limits**: that `tune` sees only the components that actually ran,
    that **nothing was modified**, and that **cost is not waste**.
11. **Savings overlap and are never totalled.** One turn can be counted under several
    recommendations. The HTML says so and deliberately shows no total.
12. **No recommendation ever tells the user to use fewer subagents.** Fan-out is the thing being
    measured, not the thing being discouraged; the advice is to right-size a tier or a fan-out,
    never to stop delegating.
13. **The ceiling is an estimate inferred from this machine's own history and is always labelled
    approximate**, unless the user pinned `ceiling.override` by hand.

### Platform and output correctness

14. **Cron rendering must escape every `%` as `\%`; launchd and plain `sh` must not, and the
    Windows renderer must emit no `%` at all.** In a crontab an unescaped `%` terminates the command
    field — an unescaped `$(date +%Y-%m-%d)` truncated the line mid-substitution and collect silently
    never ran. The cron command is also single-quoted so the outer shell expands nothing. The three
    renderers are deliberately asymmetric; "fixing" the inconsistency breaks one of them.
15. **Nothing resolves inside the plugin install directory.** Config, state, data, reports and logs
    all live under the data home (`MY_TOKEN_SPEND_DATA` / `CLAUDE_PLUGIN_DATA` / `~`), never under
    `PLUGIN_ROOT`, which a marketplace update overwrites. `ensure_home` must never overwrite an
    existing `config.json`.
16. **The generated HTML is fully self-contained and escapes untrusted text.** No `href`, `src`,
    `url(` or `@import` — reports are opened from disk and must not phone home. Prompt text comes
    from the user's own transcripts and is HTML-escaped.
17. **No shipped document tells the user to invoke `python3` directly.** The documented entry point
    is `bin/my-token-spend` / `bin/my-token-spend.ps1`, which resolve an interpreter at run time and
    skip the Windows Store `WindowsApps` stub. Naming an interpreter in the docs breaks Windows.
18. **`report.REPORT_FORMAT_VERSION` is bumped in the same commit as any change to what
    `render_html` produces.** Pages carry the version in a `<meta>` stamp and stale pages are
    rebuilt for free; a missed bump silently freezes old pages at the old format.

### Added with the 1.3.0 pricing and retry fixes

19. **`collect --reprice` is store-only and cannot lose a record.** It recomputes each stored
    record's `weighted` from the raw token counts already in the store, never opens a transcript,
    does not take `root`, and does not imply `--rebuild-from-transcripts-only`. It maps the store
    1:1 and still raises `CollectionError` if any record went missing. It is rejected in
    combination with `--backfill`, `--recut-windows`, `--rebuild-from-transcripts-only` and
    `--window`. It rewrites *every* window, because the ceiling estimate is derived across all of
    them. Its one deliberate softening of invariant 3 — reporting rather than refusing a downward
    weighted change on a closed window — is the reviewed price of the flag being usable at all: an
    operation whose purpose is to apply a newly configured price cannot also be forbidden from
    changing the number that price produces. What invariant 3 exists to protect, losing history,
    stays enforced. Making the weighted half refuse again would require adding a separate opt-in
    flag for a downward re-price, not simply re-tightening the check.
20. **The retried-after-failure detector counts only repeats that failed again.** A repeat that
    succeeded is the *recovery*, not the waste; pricing it as waste priced the healthy outcome as
    loss and let the detector's count exceed the failed-call count it is documented as a subset of.
    The label must stay arithmetically true to what is counted.

Both were enforced by tests when they were written, in the same suite that is no longer shipped
(see [Testing](#testing)), so they are prose here for the same reason 1–18 are.

### Added with the 1.5.0 root-cause page

21. **"Why" means what the work was, never why someone chose it.** The root-cause engine and every
    line it renders describe *the work*: how many runs there were, which agent types and skills they
    ran under, in which repo and branch, over what span, with which tools. Intent is not recorded
    anywhere in this data. No generated line may therefore assert a motive, call a decision a
    mistake, call a component unnecessary, or imply a cost was avoidable. **Cost is not waste.**
    Cost and quality risk are always presented together and a saving is never presented as free.
    Clustering states its own confidence and says when a cluster is mixed; prompt labels say when
    they were derived from tools because the stored prompt was too short. A line that cannot be
    grounded in the records is omitted, never softened into a guess.

This one is still enforced by an executable test upstream: every root-cause builder is run over
synthetic records and the assertion fails if any produced line contains a blame word. The test
lives in the unshipped suite (see [Testing](#testing)), so here too the prose is the shipped guard.

### Deliberate divergence from the author's tree — do not "re-sync" it away

The published copy of this plugin **intentionally differs** from the author's working tree: `tests/`
and `pytest.ini` were removed here on 2026-09-04, because nothing in this marketplace ran them and
every sibling plugin in `plugins-incubator/cti/` is prompt-only. The suite is still maintained
upstream, outside this repository.

**Any future sync of this plugin must not re-add `tests/` or `pytest.ini`.** If you are copying the
author's tree over this directory, exclude those two paths. Re-adding them would restore a suite
that nothing executes and reopen the review question that removing them settled.

## Problem

Claude Code weekly token windows are opaque while you are inside them. The only
available tooling, `ccusage`, aggregates by **session**. Sessions routinely span
several weeks, so a session that merely had activity this week reports its entire
multi-week cost against this week. There is no way to answer the question that
matters — *what did I spend since the last reset, and on what* — and therefore no
way to change behaviour before hitting the cap.

Anthropic exposes the true remaining quota only server-side via `/usage`. Nothing
local (`policy-limits.json`, `stats-cache.json`) carries token totals or limits.

## Insight

Every assistant entry in `~/.claude/projects/**/*.jsonl` carries its own
`timestamp` alongside a full `usage` object. Per-message timestamps make exact
window slicing trivial, and the surrounding fields expose dimensions `ccusage`
does not report at all.

Observed record shape (assistant turn):

```
parentUuid, isSidechain, message{model, usage{...}}, requestId, type,
uuid, timestamp, effort, sessionId, userType, entrypoint, cwd, version, gitBranch
```

`usage` contains `input_tokens`, `output_tokens`,
`output_tokens_details.thinking_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, `service_tier`, and a per-iteration breakdown.

Subagent (sidechain) records additionally carry `agentId`, `attributionAgent`,
`sourceToolUseID`, `sourceToolAssistantUUID` and `promptId`, which permit
attributing cost to a specific agent type and to the parent tool call that
spawned it.

## Goals

1. Report spend against the user's **real reset window**. The weekday is configurable and differs
   per person; `Saturday` is only the shipped default and is flagged as unconfirmed until the user
   confirms or the detector corrects it.
2. Attribute spend to sessions, repos, branches, models, effort tiers, and
   subagent types.
3. Explain deltas by **cause**, with a token figure attached to each cause.
4. Cost effectively nothing to run.

## Non-goals

- Team-wide comparison of multiple people's sessions. Marek's original ask; a
  possible later consumer of the same normalized dataset, out of scope here.
- Notifications of any kind. Output is files only; the user reads them on demand.
- Real-time / in-session interruption.

## Architecture

Two layers, deliberately split by cost:

**Daily pass — deterministic, zero tokens.** A Python script run by the OS scheduler — Windows Task
Scheduler, launchd or cron, whichever the machine has. No LLM involvement. A scheduled Claude
session that watched the token budget would itself consume the budget it is watching.

**Weekly pass — one small Claude call.** Runs once per window, the day before
reset, purely to write narrative prose into the HTML report. This is the system's
only token cost.

### Pipeline

```
~/.claude/projects/**/*.jsonl
        │  incremental tail (per-file offset + mtime in state.json)
        ▼
  normalize → one record per assistant message
        ▼
  union into data/records/week_YYYY_MM_DD.jsonl   the durable store, keyed by uuid
        ▼
  bucket into reset windows on the configured weekday
        ▼
  data/week_YYYY_MM_DD.json     canonical aggregate, rewritten idempotently
  reports/week_YYYY_MM_DD.md    human skim, same data
        ▼  once per window, before reset
  reports/week_YYYY_MM_DD.html  cross-week comparison + narrative
```

Everything after the first arrow is recomputed from the record store, not from the transcripts. The
transcripts are only ever an input; see [Window JSON schema](#window-json-schema) for why.

## Components

### `collect.py` — ingest and aggregate

Walks `~/.claude/projects/**/*.jsonl`. For each file it reads only bytes past the
recorded offset, provided size and mtime are consistent with the stored state;
otherwise it re-reads the file from the start. Malformed lines are skipped and
counted, never fatal.

Emits one normalized record per assistant message:

```
ts, sessionId, model, effort, isSidechain, agentId, attributionAgent,
cwd, gitBranch, input, output, thinking, cache_create, cache_read, weighted
```

Aggregates records into the window that contains their timestamp and rewrites
that window's JSON and MD in full. Rewriting rather than appending makes the run
idempotent: running three times a day, or twice in a minute, converges on the
same file.

Interface: `collect.run(...)` and `collect.recut(...)`, reached from the CLI as
`collect [--backfill] [--recut-windows] [--reprice] [--rebuild-from-transcripts-only]
[--window YYYY-MM-DD]`.
Depends on `config.json`, `state.json`, `rules.py`.

Window boundaries are evaluated in the zone named by `config.timezone`. A `null` value — the shipped
default — means the machine's own zone, resolved through a `tzinfo` derived from the platform so
that daylight saving is honoured for historical timestamps rather than frozen at today's offset.
Pinning an IANA name keeps boundaries stable for someone who moves between zones.

### `rules.py` — the reasons engine

Pure functions over a window's normalized records. Each rule returns zero or more
findings, and every finding carries a quantified weighted-token cost. The engine
never speculates; a rule either fires with a number or does not fire.

| Rule | Detects | Cost attributed |
|---|---|---|
| Context bloat tax | `cache_read` climbing monotonically across a session — a long session never cleared | Tokens paid re-reading context above the configured threshold |
| Subagent storm | Sidechain turn count and cost share per parent session | Total sidechain weighted cost for that session |
| Agent-type skew | Weighted cost grouped by `attributionAgent` | Cost per agent type, ranked |
| Model mismatch | Opus/Fable turns with one trivial tool call and short output | Difference between actual cost and the same turn priced at sonnet |
| Redundant reads | Identical tool input hash re-read repeatedly within a session | Cost of the repeat occurrences |
| Loop / retry burn | Repeated near-identical tool calls, failed-then-retried sequences | Cost of the redundant attempts |
| Whale turns | Top N single messages by weighted cost | The turn's own cost, labelled with the triggering user prompt |

Interface: `rules.evaluate(records, config) -> list[Finding]`. Depends on nothing
but `config.json` thresholds — no I/O, so each rule is unit-testable against
synthetic record lists.

### `rootcause.py` — what the work behind a finding was

Pure functions over a window's normalized records, in the same style as `rules.py`: no I/O, no
LLM, no config writes, unit-tested against synthetic record lists. `rules.py` says *what happened
and what it cost*; `rootcause.py` says *what the work was* — the runs, agent types, skills, repos,
branches, timestamps and tool calls those turns actually carry.

Interface: `analyse(window, records, config) -> dict | None`, which returns

```
records      how many stored records the analysis saw
coverage     turns, tool_turns, share      the fraction of turns that record any tool call
labels       observed_max, configured, truncated
becauses     one entry per window["findings"] entry, aligned by index, or None
centres      the ranked cost centres, each split into job clusters
runs         distinct agentId runs in the window
```

`explain()` builds one "because" per finding through a per-rule builder: subagent storm reports run
count, agent mix, median turns per run, job clusters and whether the runs overlapped in time
(derived from each `agentId`'s `ts` range); model mismatch reports which agents and skills the
trivial turns belonged to and their single-tool mix; agent-type skew clusters the agent's runs and
costs each; context bloat reports the session's shape and its re-issued tool inputs; whale turns
splits the turn's price by token class; redundant reads and loop burn report the span, the agents
and the turn count behind the repeat. A builder that cannot ground its line in the records returns
`None`, and the page says so rather than guessing.

**Clustering key.** Runs are grouped by `agentId` (skills by `sessionId`, since a skill has no
invocation id) and clustered on **tool mix, working directory, branch and agent type** — the fields
every turn carries in full. `prompt` is deliberately *not* a clustering input: it is stored
truncated at `prompt_label_chars` and often begins with skill boilerplate. It is used only as a
human label, after known boilerplate prefixes are stripped; when what remains is too short to name
a job the label is derived from the tools, repo and branch instead, and the cluster says which of
the two it is. Every cluster reports a confidence (`high`, `medium`, `low`, `single run`,
`grouping only`) computed from run count, keyword agreement between the member runs' labels and
tool coverage, and a cluster whose runs share tools and repo but not a job label is printed as
`MIXED`.

**Tool coverage is stated, never hidden.** `tools` is populated on roughly half the turns — a
text-only turn records none — so every per-cluster and per-finding tool share is accompanied by the
share of turns that recorded any tool call at all.

### `report.py` — weekly HTML

Reads every `data/week_*.json` plus the target window's `data/records/<key>.jsonl`, and renders one
self-contained HTML page. The reading path is, in order: a **Verdict** block (window position
against the ceiling, burn rate against the sustainable rate, week-over-week delta, and the three
top-ranked actions), the narrative, **Findings** each with its root-cause line inline and its
evidence behind a `<details>`, a **drill-down** that splits the largest agent types and skills into
the jobs their runs actually did, one **Raw breakdowns** section holding every earlier section
collapsed and unchanged (cross-week comparison, delta decomposition, burn detail, where-this-window
went, top sessions, whale turns, headline tiles), then Rule lenses and Recommendations. A spike must
attribute to `+610k: subagent storm in dynamic-pricing`, never to a taller unexplained bar.

The record store is an *optional* input: a window whose `.jsonl` is missing or unreadable still
renders, with the root-cause and drill-down sections stating plainly that they had nothing to read.
Loading records is a file read, not an API call, so the format-rebuild pass stays free.

After rendering, invokes a single headless Claude call with the window's findings
to produce the narrative section, and injects the result. If that call fails the
page still renders, minus the prose.

Interface: `report.main(argv)`, reached from the CLI as
`report [--window YYYY-MM-DD] [--no-narrative] [--all] [--refresh-narrative]`.

### `cli.py` — the single entry point

One `argparse` front end with five subcommands: `collect`, `report`, `status`, `tune`,
`install-schedule`. It also owns the scheduler renderers and interpreter resolution.

`bin/my-token-spend` (POSIX sh) and `bin/my-token-spend.ps1` (PowerShell) are the documented entry
points. They exist because no single interpreter *name* is portable: `python3` is absent on a stock
Windows install, where that name resolves to the Microsoft Store alias stub that exits without
running anything, while `python` is absent on many macOS and Linux installs. Each launcher tries
`python3`, `python`, `py -3` in order, rejects anything under `WindowsApps`, and honours
`MY_TOKEN_SPEND_PYTHON`. `cli.resolve_python()` applies the same rule when it has to write an
absolute interpreter path into a scheduler definition.

### `config.json`

Created on first run by copying `src/config.default.json`, which is the single source of every
default — including the ones that used to be literals in the source: `advice.sonnet_class_agents`,
`tune.builtin_agents`, `tune.cheap_model_families` and `narrative_model`. `advice._settings()` and
`tune.settings()` merge in the order code fallback → shipped config → user config, so a config
written before a key existed still gets the shipped value.

```
transcript_root      where transcripts are read from
timezone             IANA name, or null for this machine's zone
reset_weekday        default Saturday, flagged unconfirmed until the user says
reset_hour           local hour the window rolls over
token_class_weights  per-token-class multipliers
model_weights        per-model multipliers, plus default_model_weight
ceiling              override, and the top-cluster calibration parameters
narrative_model      model the single headless call uses
prompt_label_chars   how much of a user prompt is stored as a label, 400; raising it improves
                     labels on newly collected turns only, never history already stored
rootcause            drill-down share floor, centre and cluster caps, findings shown with a
                     root-cause line, label length
thresholds           per-rule tuning
advice               saving floors and the agent list judged safe to downgrade
tune                 window counts, cost floors, built-in agent names, cheap families
```

Limit or pricing changes are a config edit, never a code change. The full table with defaults is in
the README so the user does not have to read the source to find them.

## The weighted-cost unit

Raw token sums would mislead. Cache reads are roughly an order of magnitude
cheaper than fresh input, cache writes cost about 1.25x, and models differ by up
to 5x. Every record therefore carries:

```
weighted = model_weight * (input
                         + W_cache_create * cache_create
                         + W_cache_read   * cache_read
                         + W_output       * output)
```

All ranking, all deltas and all rule costs are expressed in this unit. Weights
live in `config.json`.

`model_weight` is resolved by `rules.model_weight`, the single implementation the
collector, the rules engine and the advice engine all share. An exact `model_weights`
entry wins; failing that the model's family - `claude-fable-5-1` and
`claude-sonnet-5-20260130` are `fable` and `sonnet` - supplies the weight, so a point
release or a dated build is never silently priced at `default_model_weight`. A family
whose configured members disagree resolves to the weight most of them carry, and a
genuine tie prices unlisted siblings at the default rather than guessing. Anything
that matches nothing is named on stderr at collect time with the weight it was priced
at; `unknown_models` in the window JSON is the record of it, not the alarm.

## Ceiling calibration

No local source of truth exists for the weekly limit, and `/usage` scraping was
rejected as fragile. Instead, the first backfill run computes weighted totals for
every historical window. Windows whose totals cluster at the top of the observed
distribution are treated as windows where the cap was approached, and the ceiling
estimate is derived from that cluster.

The estimate is explicitly approximate and is reported as such in every file.
It is confined to a single function so that a future `/usage` scraper, or a
hand-set value, can replace it without touching anything downstream.

Derived figures: percent of ceiling consumed, current burn rate against the rate
sustainable for the remainder of the window, and projected exhaustion date.

## Layout

The plugin ships code only. Nothing is ever written into the install directory; see
[Storage](#storage).

```
my-token-spend/
  .claude-plugin/plugin.json
  bin/my-token-spend  bin/my-token-spend.ps1
  commands/my-token-spend.md
  skills/my-token-spend/SKILL.md
  src/  cli.py collect.py rules.py rootcause.py advice.py report.py tune.py
        paths.py
        config.default.json
  docs/design.md  README.md
```

## Scheduling

`install-schedule` renders the definition for the detected platform — Windows Task Scheduler,
launchd or cron — or for one named with `--platform`. It is a dry run by default; `--register` is
implemented for Windows only, and refuses elsewhere rather than pretending. Every job pins the
absolute interpreter path from `resolve_python()`, appends UTF-8 logs under the data home, prunes to
`--log-retention` files per job, and records the exit code.

- Daily: `collect` at 08:00, 13:00 and 18:00 local. Incremental, seconds per run.
- Weekly: `report` at 18:00 on the day before the configured reset weekday.

The three renderers share one command string per job but must quote it differently, and the
differences are load-bearing:

| target | quoting | `%` |
|---|---|---|
| plain sh / launchd | the command as built, `$(date +%Y-%m-%d)` intact | bare |
| cron | wrapped in single quotes, embedded `'` written as `'\''`, so the outer shell expands neither `$log` nor `$(date ...)` nor `$?` before the inner `sh -c` sees them | every `%` escaped as `\%` |
| Windows Task Scheduler | a PowerShell `-Command` string; dates come from `Get-Date -Format yyyy-MM-dd` | no `%` at all |

The cron column is not cosmetic. In a crontab an unescaped `%` **terminates the command field** —
everything after it becomes stdin — so a bare `$(date +%Y-%m-%d)` truncates the line mid-expansion
and the job silently never runs. `cli.cron_field()` owns both transformations, and
`tests/test_schedule.py` asserts the asymmetry in both directions: the cron rendering must escape,
the launchd and plain-shell renderings must not.

## Testing

**The test suite is not shipped with this plugin.** `tests/` and `pytest.ini` were removed from the
published copy on 2026-09-04: nothing in this marketplace ran them, so their presence implied a gate
that did not exist. The suite — 411 tests, stdlib + `pytest` only, `pythonpath = src` — is still
maintained in the author's working tree and can be requested from them; there is no separate public
repository for it.

What that suite enforced is therefore recorded as prose in
[Safety invariants](#safety-invariants--do-not-break-these), which is now the binding document. The
rest of this section describes the shape of the suite, so a contributor who obtains it knows what it
covers.

`rules.py` is pure and gets unit tests against synthetic record lists, one per
rule, covering the fire and no-fire cases and the cost arithmetic. `rootcause.py` is tested the same
way: one test per rule's "because" builder, plus the clustering key (a cluster splits on tool mix and
repo, never on prompt text alone), the confidence and MIXED labelling, the derived-label fallback,
the parallel-versus-sequential run detection, the tool-coverage figure, and a guard that no
generated line contains motive or waste language.

`collect.py` gets tests for the incremental-read state machine specifically:
resumption from a recorded offset, detection of a truncated or rotated file, and
idempotence of a repeated run over unchanged input.

Window bucketing gets tests around the reset boundary, including a session whose
messages straddle a rollover — the case that motivates the whole system — and the winter/summer
offset, which is why the boundary tests pin an explicit IANA zone instead of using the default.

The data-safety invariants of [the window schema](#window-json-schema) are tested as behaviour, not
as implementation: a pruned transcript must not shrink a stored window, a repeated parse must not
duplicate a `uuid`, a closed window that would shrink must refuse and name every dropped record,
and a re-cut must lose nothing.

Integration check: backfill over the real transcript corpus, then assert that the
sum of all window totals equals the sum of all normalized records.

## Risks

The transcript JSONL schema is internal to Claude Code and may change without
notice. Mitigation: normalization is confined to one function, unknown fields are
ignored, and a run that finds zero parseable usage records reports that loudly
rather than silently writing an empty window.

The ceiling estimate is inferred, not authoritative, and will drift if Anthropic
changes limits. Mitigation: it is reported as an estimate, isolated behind one
function, and overridable in config.

Transcripts are local to this machine. Sessions run elsewhere are invisible and
the reports will understate spend accordingly.

## Window JSON schema

`data/week_YYYY_MM_DD.json` is the canonical artefact. It is complete enough that
`report.py` never needs to re-read a transcript. `schema_version` is `2`.

```
schema_version   int
generated_at     ISO8601 UTC of the run that wrote the file

window
  key            "week_2026_08_22"
  start / end    local dates of the Saturday reset boundaries (end exclusive)
  start_utc      the same boundaries as UTC instants; timestamps are compared
  end_utc        against these, so a message at 23:30Z on Friday in CEST already
                 belongs to the next window
  timezone       the zone the reset was evaluated in: an IANA name, or
                 "local time (ABBR)" when config.timezone was null
  reset_weekday  "Saturday"
  reset_hour     local hour of the rollover
  is_current     the window containing the run's wall clock
  elapsed_days   0..7, clamped
  elapsed_fraction  elapsed_days / 7

weights          the token-class and per-model weights the file was priced with,
                 copied in so a consumer can re-derive or re-price without config

totals           turns, sessions, input, output, thinking, cache_create,
                 cache_read, weighted, sidechain_turns, sidechain_weighted

by_day           [{date, turns, weighted, input, output, thinking,
                   cache_create, cache_read}]  ascending by local date
by_model         same bucket shape keyed by {key: model}, ranked by weighted desc
by_effort        keyed by effort tier
by_repo          keyed by cwd
by_branch        keyed by gitBranch
by_agent         keyed by attributionAgent   (subagent turns only)
by_skill         keyed by attributionSkill
unknown_models   the subset of by_model whose name matched neither an exact nor a
                 family weight and was therefore priced at default_model_weight

by_session       [{key, turns, weighted, tokens..., sidechain_turns,
                   sidechain_weighted, models[], agents[], first_ts, last_ts,
                   cwd, gitBranch, first_prompt}]
                 cwd/gitBranch are the session's last observed values

findings         rules.evaluate output, ranked by weighted_cost desc
                 [{rule, subject, detail, weighted_cost, evidence{...}}]
                 evidence is rule-specific; whale_turns carries uuid, ts, model,
                 effort, prompt, tools[], cwd, gitBranch, rank
findings_by_rule {rule: {count, weighted_cost}}, ranked by weighted_cost desc

ceiling
  estimate               weighted tokens, or null
  method                 override | top-cluster | insufficient-data
  approximate            false only for an override
  cluster_size           windows averaged to produce the estimate
  windows_considered
  percent_used           null when no estimate exists
  burn_rate_per_day      weighted / elapsed_days
  remaining_weighted
  sustainable_rate_per_day  remaining budget / days left in the window
  projected_exhaustion   ISO8601, or null
  exhausts_before_reset  bool, or null

parse            files_scanned, malformed_lines (cumulative per file, from
                 state.json), records (records in this window)
```

`data/records/week_YYYY_MM_DD.jsonl` holds the normalized records backing each
window, one JSON object per line, keyed by `uuid` and sorted by `(ts, uuid)`.
This store is what makes "incremental parse, full re-aggregate" possible: a run
appends only newly-read records to it, then recomputes the whole window from the
store. It is the durable source of truth, not the transcripts: Claude Code prunes
`~/.claude/projects/**/*.jsonl` over time, so a window's history is no longer
re-derivable once its transcripts are gone. Every run unions freshly parsed
records into the store by `uuid`; `--backfill` re-reads every transcript but
still only unions. A run that would shrink a closed window refuses and names the
loss; `--rebuild-from-transcripts-only` is the explicit opt-in that discards the
store. Re-cutting windows after a reset-weekday change is `--recut-windows`,
which re-buckets the store and never touches transcripts. Re-pricing after a
`model_weights` change is `--reprice`, which recomputes every stored record's
`weighted` from the raw counts it already holds and rewrites every window. It
reads only the store, keeps every record by construction, and is refused if the
record count would change at all. Because a re-price is the one operation whose
whole purpose is to move a closed window's weighted total, it reports each
window's before and after rather than refusing on the shrink guard; the guard's
subject - losing records - is still enforced, and unenforceable only there.
Every run compares the store against the current weights and names the windows
that drifted, so a weight edit cannot sit unapplied unnoticed.

Normalized record fields: `ts, uuid, sessionId, model, model_known, effort,
isSidechain, agentId, attributionAgent, attributionSkill, cwd, gitBranch,
version, input, output, thinking, cache_create, cache_read, weighted,
tools[{name, hash}], text_chars, is_api_error, prompt`.

`state.json` maps absolute transcript path to
`{offset, size, mtime, last_prompt, malformed}`. A file is skipped when size and
mtime are both unchanged, resumed from `offset` when it has strictly grown, and
re-read from byte zero otherwise. A trailing line without a newline is left
unconsumed until it is complete.

## The advice engine

`rules.py` says what happened and what it cost. `advice.py` turns that into things
to do, and it is the only place where a saving figure is *modelled* rather than
measured. It is pure: `recommend(window_data, findings, config) -> list`, no I/O,
and it never mutates its inputs.

### Classification

Every rule belongs to exactly one class, and the class sets the tone of the advice.

| Class | Rules | What it means |
|---|---|---|
| `waste` | model mismatch, redundant reads, loop / retry burn | Cost that bought nothing. Removing it changes what you pay, not what you get. |
| `strategy` | subagent storm, agent-type skew | Cost that bought something real. The advice changes *how* the work is done, never whether it is done. |
| `hygiene` | context bloat, whale turns | Habits rather than decisions. Cheap to change, and the change is a working style. |

The class boundary carries a hard design rule: no `strategy` recommendation is ever
allowed to say "run fewer subagents". Fan-out is what the tool is for. A strategy
recommendation may move workers to a cheaper tier or make each worker's slice
bigger, and that is all.

### Saving arithmetic

One recommendation kind per producer. `cost` below is a finding's `weighted_cost`.

| Kind | Class | From | Weighted saving |
|---|---|---|---|
| `model_downgrade` | waste | model mismatch | the finding's `cost`, verbatim — the rule already priced the same turns at the cheaper model. One per model. |
| `deduplicate_reads` | waste | redundant reads | `sum(cost)` grouped by tool name; one recommendation per tool. |
| `break_retry_loops` | waste | loop / retry burn | `sum(cost)` over every loop finding; one recommendation, labelled with the longest run. |
| `right_size_agent_tier` | strategy | agent-type skew | `cost * expensive_model_share * downgrade_factor`, and only for agents named in `advice.sonnet_class_agents`. |
| `right_size_fan_out` | strategy | subagent storm | `sum over oversized storms of cost * (1 - median_turns / storm_turns)` — the cost of bringing only the above-median storms back to the median. |
| `reset_context` | hygiene | context bloat | `sum(cost)` over every bloated session. |
| `split_whale_turns` | hygiene | whale turns | `sum over oversized whales of (cost - median_cost)` — the excess of the biggest turns over the median whale. |

Two derived quantities, both read from the window's own embedded `weights` so a
historical window is priced the way it was collected:

```
downgrade_factor      = 1 - model_weights[downgrade_model] / max(model_weights)
expensive_model_share = (weighted spent on models priced above the downgrade model)
                        / window total weighted
```

`right_size_agent_tier` multiplies by both because only part of the window ran on an
expensive model, and moving down recovers only the price gap, not the whole cost.

The median rules refuse to fire on too small a sample: `min_storms_for_median` and
`min_whales_for_median` (4 each) findings are required, and a population where
nothing exceeds the median produces nothing.

### Filtering and ranking

A recommendation is dropped entirely below `advice.min_saving` (250,000 weighted).
Survivors get `percent_of_window` and a `score`:

```
score = weighted_saving * confidence_weight     high 1.0, medium 0.6, low 0.3
```

and are ranked by `(-score, kind, subject)`. Ranking by score rather than by raw
saving keeps a large speculative number from outranking a smaller certain one.

Each kind carries a fixed risk and confidence, which is what the score above uses:

| Kind | Performance risk | Confidence |
|---|---|---|
| `model_downgrade` | none | high |
| `deduplicate_reads` | none | medium |
| `break_retry_loops` | none | medium |
| `reset_context` | low | medium |
| `right_size_agent_tier` | low | medium |
| `split_whale_turns` | low | low |
| `right_size_fan_out` | medium | low |

Every recommendation carries `kind, group, subject, title, action, detail,
weighted_saving, performance_risk, confidence, evidence`, plus `percent_of_window`
and `score` once ranked.

**Savings are upper bounds and they overlap.** A turn can be both a whale and an
Opus turn that should have been Sonnet, so the same tokens can appear under two
recommendations. They are alternative framings of the same window, never a shopping
list to add up.

## Cause precedence in the delta decomposition

`findings_by_rule` totals overlap by construction — one subagent turn can be counted
by subagent storm, agent-type skew and whale turns at once. Summing them would
double-count, so the week-over-week decomposition never uses them. It builds a
disjoint grid instead and attributes each cell to **at most one** cause.

### The grid

A cell is `(repo, lane)`:

```
repo = basename(session cwd)
lane = main | subagent
main lane     = session weighted - session sidechain_weighted
subagent lane = session sidechain_weighted
```

Cells partition the window: every weighted token lands in exactly one of them, and
the grid reconciles to `totals.weighted`. Cells holding less than 0.5 weighted are
dropped as noise.

The decomposition takes `delta = after - before` per cell, discards movements under
1.0 weighted, ranks by `|delta|`, keeps the top 8, and folds the remainder into a
single "everything else" row. Because the cells are disjoint, the rows sum to the
total change — which is the property that lets a spike be read as
`+610k: subagent storm in dynamic-pricing` rather than a taller bar.

The cause is looked up in the window that *explains* the movement: the current
window when the cell grew, the previous window when it shrank.

### Which findings may explain a cell

| Rule group | Members | Matching |
|---|---|---|
| Subagent-only | subagent storm, agent-type skew | Never explain the main lane. |
| Global | agent-type skew, model mismatch | Aggregated across the window, so they match a cell when their evidence carries no `cwd`; if it does carry one, it must match the repo. |
| Everything else | context bloat, whale turns, redundant reads, loop / retry burn | Must match the cell's repo through the finding's evidence `cwd`. |

### The precedence order

Candidates are sorted by `(precedence index, -weighted_cost)` and the first wins.
The order is fixed and is deliberately **not** cost-ranked:

```
subagent storm > context bloat > whale turns > model mismatch
               > redundant reads > loop / retry burn > agent-type skew
```

Cost only breaks ties within a rule. A 900-weighted redundant-reads finding
therefore loses to a 50-weighted whale-turns finding in the same cell, because
precedence is decided before cost.

The order runs from the most structural explanation to the most incidental. A
subagent storm is a fact about how the whole cell was run; context bloat and whale
turns describe the shape of a session; model mismatch, redundant reads and loop burn
describe individual turns inside it. Agent-type skew sits last on purpose: it is a
ranking lens that fires in essentially every window, so given any earlier candidate
it would otherwise explain everything and distinguish nothing.

A cell with no matching finding gets no cause and is labelled plainly as
`main-agent work in <repo>` or `subagent work in <repo>`. That is a normal outcome,
not a gap: spend can grow simply because more work was done.


## Report format versioning

`report.py` renders one HTML page per window, but a run only *asks* for one window.
Before this section existed, a change to the rendered format left every other page
frozen at the old shape: when the Recommendations section and the end-of-window
burn-rate fix landed, one page went stale and seven historical windows had no HTML at
all, and the pages had to be regenerated by hand. The markdown never had this problem
because `collect.py` rewrites every window's `.md` on every run.

### The stamp

Every generated page carries five meta tags in `<head>`:

| tag | meaning |
|---|---|
| `report-format-version` | the value of `report.REPORT_FORMAT_VERSION` when the page was rendered |
| `report-window` | the window key, so a page can be identified without its filename |
| `report-window-weighted` | the window's weighted total, to 4 decimals |
| `report-window-turns` | the window's turn count |
| `report-window-closed` | `true` once the window has ended |

Meta tags, not HTML comments: the zero-comment rule applies to the template as much as
to the code, and a meta tag is what a browser and a parser both already understand. A
page with no `report-format-version` tag reads as version `0`.

The narrative is stored a second time, verbatim, in a `data-narrative` attribute on the
narrative `<section>`. That is what makes a rebuild free.

### The rebuild pass

On every run, after the requested window is written, `report.py` reads the stamp of
every window's page and rebuilds any whose stamp is missing or below
`REPORT_FORMAT_VERSION` — including windows that have no page at all. The requested
window is excluded; it was just written. `--all` forces every window through the pass
regardless of its stamp.

The pass is idempotent by construction: a rebuild writes the current version into the
stamp, so the second run of a pair finds nothing to do and says
`format : N, all M pages current`.

### Bumping the version

**Bump `REPORT_FORMAT_VERSION` in the same commit as any change to what
`render_html` produces.** That includes a new or removed section, a changed tile,
label or table column, a fix to a rendered number, and a change to `STYLE` or `SCRIPT`
that alters what the reader sees. It does not include a change that only affects the
console summary, the markdown, or the collector.

If the bump is forgotten, existing pages keep the old rendering and the tool silently
returns to the failure this section exists to prevent. The cost of an unnecessary bump
is one free rebuild of every page; the cost of a missed bump is a stale report nobody
notices. Bump when unsure.

### Narrative and the cost constraint

The narrative section costs one headless Claude call per window. A tool whose purpose
is to reduce token spend cannot fire nine of those three times a day, so:

- **A format rebuild never calls the API.** It reuses the narrative already embedded in
  the stale page. Rebuilding all nine pages costs zero tokens.
- For a page written before the stamp existed, the narrative is recovered by reading
  the prose back out of the rendered `<p>` and `<li>` elements of the narrative section.
  This is a migration path for version-0 pages only; version-1 pages always use the
  attribute.
- `--refresh-narrative` regenerates the prose on rebuilt pages — **one Claude call per
  rebuilt window**. It is never on by default and should be used deliberately, e.g.
  after a change to `build_narrative_prompt`.
- The requested window is the exception: it is re-rendered from fresh data every run and
  gets a fresh narrative as before. It never falls back to its own embedded prose,
  because stale prose beside refreshed numbers is worse than no prose.

Steady-state cost is therefore unchanged: one Claude call per run, the same as before.

### Closed windows are immutable in their data

A closed window's page is rebuilt for format reasons only. Because the stamp records the
window's weighted total and turn count, a rebuild can check them against the window JSON,
and a mismatch on a closed window prints a loud `DATA DRIFT in the closed window ...`
warning on stderr naming both values. The rebuild still proceeds — the format fix is
needed either way — but the discrepancy is surfaced rather than silently overwritten.
Current windows are exempt: their data is expected to move.

### Surfaced flags

| flag | effect |
|---|---|
| `--all` | rebuild every window's page regardless of its stamp |
| `--refresh-narrative` | regenerate prose on rebuilt pages, one Claude call each |

---

# Addendum: plugin packaging

Date: 2026-08-31
Status: implemented

The tool ships to the team as a Claude Code plugin named `my-token-spend`,
distributed through Rohlik's existing `rohlik-skills` marketplace. The two
PowerShell wrappers are removed; nothing platform-specific remains outside the
scheduler installer.

## Distribution

Marketplace: `gitlab.int.rohlikgroup.com/tools/rohlik-skills`, owner Platform &
Infra. Target path `plugins-incubator/cti/my-token-spend/`. The incubator is
unowned in CODEOWNERS, so no approval is required to merge. Promotion to the
curated marketplace happens at three teams or ten uses.

The repository's own `.claude/skills/add-plugin/SKILL.md` is the authoritative
workflow and must be followed rather than reinvented, including its
`validate-marketplace`, `sync-incubator-marketplace` and `generate-catalog`
scripts. `plugins-incubator/_template/` is the starting point.

The name deliberately avoids collision with the existing `ai-finops` plugin,
which covers org-wide cloud LLM spend. This one is personal-scope.

## Layout

```
my-token-spend/
  .claude-plugin/plugin.json
  commands/my-token-spend.md
  skills/my-token-spend/SKILL.md
  src/  collect.py rules.py rootcause.py advice.py report.py tune.py cli.py
  tests/
  README.md
```

Commands and skills invoke `python3 "${CLAUDE_PLUGIN_ROOT}/src/cli.py"`. Exec
form cannot run `.cmd` shims on Windows, so the interpreter is always invoked
directly. `${...}` placeholders are quoted for paths containing spaces.

Subcommands: `collect`, `report`, `status`, `tune`, `install-schedule`.

## Storage

All persistent data lives under `${CLAUDE_PLUGIN_DATA}`
(`~/.claude/plugins/data/{plugin-id}/`): `config.json`, `state.json`, `data/`,
`reports/`, `logs/`. This survives plugin updates but is deleted on full
uninstall unless `--keep-data` is passed. That trade-off is accepted; the README
must state it prominently.

Generated reports contain the user's own work content — prompt labels, branch
names, repository names and URLs. They are never shared, never committed, and
the plugin repository ships code only.

There is no migration path from any earlier layout. A `collect --migrate-from DIR` flag existed
briefly for exactly one pre-plugin install and was removed as dead weight; see
[Superseded decisions](#superseded-decisions).

## Reset day detection

The weekly reset weekday differs per person and no local source records it.
Transcripts were searched across the full corpus and contain no rate-limit or
reset message, because the cap has never been reached on this machine.

Two mechanisms, in order:

1. First run asks once and writes the answer to config.
2. The collector opportunistically watches transcript lines for the rate-limit
   or reset notice Claude Code emits when a user approaches their cap. On a
   confident match it corrects the configured weekday and records the correction
   in the next report.

The message format is unverified, so the detector matches several candidate
patterns and never overrides configuration on a partial or ambiguous match.

## Scheduling

There is no plugin-level scheduler; `monitors/` is a session-scoped watcher, not
cron. Scheduling therefore remains OS-level, behind one cross-platform Python
subcommand that emits Windows Task Scheduler, launchd or cron definitions as
appropriate. It stays dry-run by default and requires an explicit flag to
register anything.

## The tune loop

Findings already carry `attributionAgent` and skill names, which correspond to
real files on disk. `tune` maps findings onto those files and emits concrete,
reviewable diffs.

Example: `review-verifier` accounted for 1,323 turns of adversarial refutation
in one window; the proposal is a one-line `model: sonnet` change to its
frontmatter, with the measured cost attached.

**`tune` proposes; it never applies.** No automatic edits to skills, agent
definitions or CLAUDE.md. The classification of a turn as trivial is an
inference, and acting on a wrong inference degrades quality invisibly, which
costs far more than the tokens saved.

### Cost is not value

Every proposal must present cost alongside what that cost buys, and must never
assert that an expensive component is waste. `review-verifier` consuming 20.7M
weighted tokens may be precisely what keeps bad findings out of merge requests.
The output frames this as "here is the cost and here is what it is doing — you
judge whether it earns its keep", and states the quality risk of each change
explicitly. A proposal whose quality impact cannot be assessed says so rather
than presenting a saving in isolation.

Proposals carry the same `performance_risk` grading as recommendations, and
anything above `none` states what could get worse, not only what gets cheaper.

### Visibility limit

`tune` sees only agents and skills that actually ran in the analysed windows. A
rarely-invoked but expensive component will not surface. This is accepted
behaviour and is stated in the output rather than hidden.

---

# Addendum: the tune loop as built

Date: 2026-08-31
Status: implemented. This is the current description of `tune`; the "tune loop" section of the
packaging addendum above states its intent and its two hard rules, both of which still hold.

The tune loop above described a single window. Implementation widened it on four axes, keeping the
two hard rules (proposes-never-applies, cost-is-not-waste) and adding a third.

## Multi-window by default

`tune` aggregates the last N closed windows, N = 4. A cost centre is ranked by the **median** of its
per-window cost, counting a window it did not appear in as zero, so a single heavy week cannot
promote a component that is usually cheap. A centre seen in fewer than `min_windows_for_proposal`
(2) windows is labelled `one-off, not a pattern` and can never drive a config change. Analysing a
single window is still possible and lowers that floor to 1, but the output says the proposal is
fitted to one week.

## Cost centres

Agents and skills are one kind of thing. Both resolve to files, both carry a per-run cost - per
`agentId` for agents, per session for skills, since a skill has no invocation id in the data - and a
median wall-clock per run. Skills additionally report their file size and description length,
because a broad description is what pulls a file into turns that did not need it; the data cannot
measure how often that happened and the output says so. Skill and plugin names are grouped by their
`plugin:` prefix into a per-plugin total. No skill ever gets a modelled saving: its attributed cost
is the cost of the work done under it, not the cost of loading it.

## Wasted round trips

Measured from the durable record store joined to the transcripts by assistant `uuid`, with the
per-turn tool list aligned to the transcript's `tool_use` blocks by index (verified: 22,440 turns
aligned, 0 mismatched). Shipped detectors, all evidence-backed:

| detector | source | observed |
|---|---|---|
| tool call returned an error | `tool_result.is_error` | 1.3-1.6% of weighted, every window |
| same input re-issued after it failed, and failed again | the above plus the record's tool hash | 0.00-0.16% |
| permission-denied call | error text | 0-2 per window |
| turn the API errored | the collector's `is_api_error` | ~0.00% |

The retry detector counts only repeats that failed again. A repeat that succeeded is the recovery,
not the waste, and counting it both priced the healthy outcome as loss and let the detector's count
exceed the failed-call count it says it is a subset of.

Rejected, with the measurement that rejected them: empty `Grep`/`Glob` results (0.008-0.19% of
weighted, and an empty result is a legitimate negative finding); "long output never referenced"
and "turn that only restates context" (no signal in the data, would require judging meaning);
subagent returned nothing usable (0-3 Task-call failures per window, too rare to rank).

Transcripts are pruned by Claude Code, so each window reports the fraction of its turns the
transcripts still cover. Below 90% the figures are labelled a floor, not a rate comparable to the
other windows.

## Pacing and exhaustion

Per closed window: spend per day against the ceiling estimate, cumulative percentage, the day the
ceiling was reached or approached, front-loading against both the calendar week and the days with
any spend, and the repos, agents and skills that drove the spend up to that day. For the open
window: burn rate, projected end-of-window total, projected exhaustion instant, the sustainable rate
needed to land inside the window, and where the spend sits so far.

## The honesty rule

The third hard rule, equal in weight to the other two: **cost is measurable, quality is not.** No
output may claim a change preserves accuracy. Speed is reported only as turn counts and wall-clock
between timestamps. Every proposal states the quality risk and that the risk is not measurable from
this data.

---

# Superseded decisions

Kept for the reasoning, not as instructions. Nothing in this section describes current behaviour.

| Decision | Status now | Why it changed |
|---|---|---|
| Two PowerShell wrappers (`run.ps1` and a collect wrapper) driving the scripts | **Gone.** `cli.py` plus the two `bin/` launchers | The tool became a plugin others install; nothing platform-specific may sit outside the scheduler renderers. The `-AllReports` / `-RefreshNarrative` wrapper flags are now plain `--all` / `--refresh-narrative` |
| `python3 "${CLAUDE_PLUGIN_ROOT}/src/cli.py"` as the documented entry point | **Gone.** `sh "${CLAUDE_PLUGIN_ROOT}/bin/my-token-spend"`, or the `.ps1` in PowerShell | `python3` is not a name that exists on a stock Windows install; it resolves to the Microsoft Store alias stub, which exits without running anything. Ironically `resolve_python()` already rejected that stub while the docs walked straight into it |
| `collect --migrate-from DIR` | **Removed**, with its `migrate()` implementation and its six tests | It existed to move one pre-plugin install into the plugin data home. That install is gone and no second user of the flag can exist; the code was untested surface area with a copy-into-your-data-home blast radius |
| `install-schedule --register` printing how to unregister the author's older `\ClaudeSpent\` tasks | **Removed** | A leftover of one machine, meaningless to anyone installing the plugin |
| launchd labels under `com.rohlik.my-token-spend` | Now `local.my-token-spend` | The author's employer's reverse-DNS name has no business in the launchd labels of a plugin other people install |
| `timezone: "Europe/Prague"` as the shipped default | Now `null`, meaning the machine's own zone | A default that silently cut every window boundary in one city's zone. Unlike `reset_weekday`, nothing warned about it |
| `advice.sonnet_class_agents`, `tune.builtin_agents`, `tune.cheap_model_families`, and the narrative model, as literals in the source | All in `config.default.json` | The agent list in particular was one person's review-agent roster, hardcoded where no user could override it without editing plugin source |
| `weights[downgrade_model]` looked up directly | `.get(downgrade_model, default_model_weight)` in all three places | A window is priced with the weights frozen at collection, but the downgrade model is read from the *current* config. Changing it raised `KeyError` on every historical page |
| `tune` analysing a single window | Aggregates the last 4 closed windows by default | Advice fitted to one heavy week is advice about that week. The single-window mode survives behind `--window` and says loudly that it is fitted to one week |
| The full tool-result text kept in a second transcript scan | The record's own `result_chars`, `is_error` and `denied` per tool call | The collector already reads every transcript line once and holds the tool ids; a second pass to recover outcomes cost a full rescan and could only see transcripts that had not been pruned |
| `ceiling_estimate` as a single config key | A `ceiling` block: `override`, `top_cluster_fraction`, `min_windows`, `headroom` | The estimate needed its calibration parameters exposed, not just its result |
| Scraping `/usage` for the true remaining quota | Never built | Rejected as fragile at design time, and still is. The ceiling is inferred from the user's own history and labelled an estimate everywhere |

