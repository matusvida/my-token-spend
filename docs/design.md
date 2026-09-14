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

7. **`tune` writes nothing outside `data/tune_last.json`.** That one file in the data home holds the
   proposals and figures of the last run, so the next run can say what moved. `rules.py`,
   `agentfiles.py` and `advice.py` contain no file-writing call at all — no `write_text`,
   `write_bytes`, `os.replace`, `shutil.*`, `unlink`, `mkdir`, and no `open()` except for reading —
   and `tune.py` contains exactly one, inside `save_state`, targeting that path. Running `tune` must
   leave every file under `~/.claude` byte-identical, and must touch no other file in the data home:
   never a config, an agent definition, a skill or a window aggregate.
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
13. **The ceiling names its own method wherever it is shown.** `quota-fit` is fitted against the
    utilization Anthropic reports, `override` is what the user pinned, and `top-cluster` is inferred
    from this machine's own heavy weeks and is labelled an estimate, never a quota.

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

### Added with the rescan upgrade path

22. **`collect --rescan` backfills fields and can neither lose a record nor move a total.** A plain
    collect reads only the bytes past each stored offset, so after a schema change the fields it
    added exist only on the records parsed since. `--rescan` re-reads every transcript from byte
    zero and **upserts** into the store by `uuid`: a record that already exists keeps its identity
    and gains whatever fields the fresh parse carries, a record whose transcript was pruned is left
    untouched, and the priced fields `weighted` and `model_known` are carried over from the stored
    copy unless `--reprice` is given alongside. It merges rather than replaces per tool call too,
    so an outcome already joined onto a stored call is never overwritten with a null. The agent-call
    and cost-state stores are merged by their own keys under the same no-shrink rule, and
    `state.json` offsets are reset to each file's new end. It is the upgrade path
    `--rebuild-from-transcripts-only` must never be used for: that flag discards the store, and on a
    real install most of the history is no longer in any transcript.
23. **The rescan recommendation is stated once.** `state.json` carries the `schema_version` the
    store was last written under. When a collect finds a store that predates the current version, it
    names `--rescan` on stderr and records the new version, so the notice does not repeat. A first
    collect into an empty store says nothing.

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

1. Report spend against the user's **real reset window**, cut at the reset instant the usage
   endpoint reports. `reset_weekday` and `reset_hour` are the fallback for a machine that has never
   reached the endpoint.
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
`collect [--backfill] [--rescan] [--recut-windows] [--reprice]
[--rebuild-from-transcripts-only] [--window YYYY-MM-DD]`.
Depends on `config.json`, `state.json`, `rules.py`, `context.py`, `cost.py`.

Window boundaries are evaluated in the zone named by `config.timezone`. A `null` value — the shipped
default — means the machine's own zone, resolved through a `tzinfo` derived from the platform so
that daylight saving is honoured for historical timestamps rather than frozen at today's offset.
Pinning an IANA name keeps boundaries stable for someone who moves between zones.

### `cost.py` — the list-price USD figure

Claude Code writes a `cost-state` entry into the transcript after every turn, carrying the session's
running `totalCostUSD` and per-model token counts. `collect` keeps every such entry it reads, stamped
with the timestamp of the entry before it, and merges them into `data/session_costs.json`, which is
durable and never shrinks: a pruned transcript keeps its price. Each session holds the cumulative
points it has been seen at, plus its highest total for the price calibration.

Because the entries are cumulative, successive points give the USD spent in the interval between
them. A window's USD is the sum of the intervals whose own timestamp falls inside it, so a session
that straddles a reset splits its price between the two windows in the proportion its work actually
fell — within the coarseness of the entries, which is one per CLI exit or resume. A session's first
point books whole to its own window; a session stored before this booking existed, with no points,
falls back to the window holding its first record. A store written before the points existed carries
no timestamp on its per-file state, so the points come back only on `collect --rescan`; the schema
version is what makes the upgrade notice ask for that run.

The tile states how many of the window's sessions were priced and how many of them crossed a
boundary. Below `report.USD_MIN_COVERAGE` (half the window's sessions) the tile is not a price tile
at all — a total drawn from a tenth of the sessions is not comparable with one drawn from all of
them, and two such figures side by side read as an inversion that is really a coverage gap. The slot
carries a number the data supports instead: unused quota where a fitted quota or a config override
exists, otherwise the session count with the subagent turns beside it. The coverage itself moves to
one line in the raw breakdowns, naming how many of the window's sessions carry a cost entry and why
the rest do not: Claude Code records a session's cost only when the session itself writes one, so
sessions still open, resumed into another file, or closed without writing one carry no price. The figure is labelled *list price, as `/cost` shows it; not
what the subscription bills* everywhere it is shown — it is Claude Code's own estimate of what the
same tokens would have cost on the API, not a billing figure.

`collect --calibrate-weights` fits, per model, the four token-class prices that best explain the
stored session totals (least squares over `input`, `output`, `cache_create`, `cache_read`), divides
them by the input price, and prints the result next to the configured `token_class_weights`. It needs
at least eight sessions per model and refuses a fit whose residual exceeds 5% of the spend or whose
prices are not all positive, because sessions with near-identical token mixes cannot separate four
classes. It writes nothing; changing the weights stays a human decision.

### `quota.py` — the real quota and its reset instant

Reads the OAuth token Claude Code stores, calls `oauth/usage` with a 10 s timeout,
and appends one sample per collect. It never refreshes the token and never writes
anything on a failure. See [Reset instant](#reset-instant).

The poll carries the HTTP status back to the collector. On a 429 the collector stamps
`quota_throttled_at` into `state.json` and skips the poll entirely for the next
`collect.THROTTLE_MINUTES` (30), so a daily cron plus a handful of manual runs never hammer a
rate-limited endpoint. Any other outcome clears the stamp.

### `rules.py` — the reasons engine

Pure functions over a window's normalized records. Each rule returns zero or more
findings, and every finding carries a quantified weighted-token cost. The engine
never speculates; a rule either fires with a number or does not fire.

| Rule | Detects | Cost attributed |
|---|---|---|
| Context bloat tax | `cache_read` climbing monotonically across a session — a long session never cleared. The finding text names what grew the context, from `context.py` | Tokens paid re-reading context above the configured threshold |
| Subagent storm | Sidechain turn count and cost share per parent session | Total sidechain weighted cost for that session |
| Agent-type skew | Weighted cost grouped by `attributionAgent` | Cost per agent type, ranked |
| Model mismatch | Opus/Fable turns that are trivial on all four counts: output at or below `max_output_tokens`, between 1 and `max_tool_calls` tool calls, thinking at or below `max_thinking`, and no `Agent` call among them | Difference between actual cost and the same turn priced at sonnet |
| Redundant reads | Identical tool input hash re-read repeatedly within a session | Cost of the repeat occurrences, each call charged its share of the turn by `result_chars` |
| Loop / retry burn | Repeated near-identical tool calls, failed-then-retried sequences | Cost of the redundant attempts, charged the same way |
| Whale turns | Top N single messages by weighted cost | The turn's own cost, labelled with the triggering user prompt. Near-identical turns in one session collapse to one counted row, and the table's figure is one of those turns rather than their sum |
| Headroom | A closed window that ended under `headroom.max_pct` of a **known** quota, with the window before it under the same figure | The quota the window left unused, `ceiling - spent` |

A turn's cost is split across its tool calls in proportion to `result_chars`, so
a repeated 400 KB read carries its own weight and a repeated `ls` does not. When
any call on the turn has no recorded result size, or every result was empty, the
split falls back to an even one.

An `Agent` call is the orchestrator dispatching work, not a turn that should have
run cheaper, and a turn that thought for hundreds of tokens before a short answer
is judgement rather than a lookup. Both are excluded, and the finding text states
all four conditions so the reader can judge the definition rather than trust it.

The headroom rule is the one rule that needs more than the records. It fires only
when the ceiling came from `quota-fit` or `override`, never from `top-cluster`: a
user measured against their own heaviest weeks always looks near 100%, so unused
quota would be an artefact of the measurement. It needs two consecutive quiet
windows, so one quiet week never fires it, and it stays silent while a window is
still open. Its evidence carries the components whose runs look like judgement
work (median thinking above `headroom.min_thinking` or median output above
`headroom.min_output`), the sessions whose subagent runs never overlapped for at
least `headroom.min_serial_minutes`, and the window's untouched extra-usage budget.

Interface: `rules.evaluate(records, config, quota=None) -> list[Finding]`. Depends
on nothing but `config.json` thresholds — no I/O, so each rule is unit-testable
against synthetic record lists. The optional `quota` block is what `collect`
passes for the headroom rule: the window key, the ceiling method, whether the
window has closed, the ceiling, the spend, this window's and the previous
window's percentage, and the extra-usage budget if every sample in the window
reported it enabled and undrawn.

### `agentfiles.py` — where an agent or a skill is defined

The root search (project `.claude`, user `.claude`, installed plugins, then
marketplace trees as a fallback), the frontmatter reader, the name resolvers and
the unified-diff builder. `tune` maps a cost centre onto its file through it, and
`advice` prices a tier change through the same search, so both name the same file
for the same component. It also answers "what parallel cap is written down", by
scanning each root's own top-level `*.md` role files as well as its agent and
skill files for a line that caps a number of things running in parallel; a file
whose name mentions an orchestrator is read first.

### `context.py` — what grew the context

Pure functions over a window's records, no I/O. Context growth at a turn is
`(cache_read + cache_create)` minus the same sum on the previous turn of the
**same thread**, floored at zero. A thread is `(sessionId, agentId)`: subagent
turns carry the parent's `sessionId`, so interleaving them by timestamp would
read every return to the parent as fresh growth. Growth resets to zero on a turn
marked `after_compaction` or `compacted`, and turns with no usage at all — API
errors — are skipped rather than treated as a reset, so the next real turn is
measured against the last real one.

Growth at a turn is attributed to the *previous* turn's tool results in
proportion to their `result_chars`. A previous turn with no tool calls sends the
whole growth to `prompt`; a previous turn whose calls do not all carry a
`result_chars`, or whose results were all empty, sends it to `unattributed`. The
prompt bucket is all-or-nothing: the stored `prompt` is the session's last user
prompt clipped to `prompt_label_chars`, so there is no per-turn prompt length to
divide by.

Per session it reports growth by tool name, the ten largest single results with
their tool and timestamp, the number of compactions, the carry tax (the weighted
`cache_read` above the threshold), and the share of tool calls that carry a
`result_chars` at all. Old records carry none, so every
sentence built from this data states that coverage.

Interface: `context.summarize_session(session, config)`,
`context.window_block(records, config)` for the window JSON, and
`context.detail(summary, threshold, config)` for the `context_bloat` finding
text. The text names the tool and the size of a result, never its input: tool
input text is not stored.

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

**Whale rows are collapsed before they are drawn.** Three rows at the same weighted cost, seconds
apart, under one task notification, read as triple counting rather than as parallel tool calls each
billed the shared cache-create. Whale findings that share a session, the first
`report.WHALE_PROMPT_HEAD` (80) characters of their prompt, and a cost within `report.WHALE_SAME_COST`
(1%) of the group's leader become one row carrying the count. Nothing is dropped: the count is on the
row and the cost shown is per turn.

**Clustering key.** Runs are grouped by `agentId` (skills by `sessionId`, since a skill has no
invocation id). A run is joined to the `Agent` call that dispatched it on the dispatch prompt: the
session id plus the first 40 characters of the run's first stored prompt, whitespace-collapsed and
stripped of a leading `<teammate-message>` envelope, matched against the same normalisation of the
call's `prompt_head` and then confirmed over whatever prefix both sides actually carry. The two
texts are cut to different lengths — both start at `prompt_label_chars`, but the envelope is
stripped from only the stored side, and a run whose prompt arrived wrapped therefore keeps fewer
characters of it — so the confirmation compares `min(len)` characters rather than demanding equal
strings. Nothing on a subagent turn carries the dispatching call's id, so the prompt is
the only link the transcripts offer; a prompt shorter than 40 normalised characters is not specific
enough to join on and joins nothing. When one prompt was dispatched more than once in a session, the
latest call at or before the run's first turn wins, which is the retry rather than the original. The
join gives the run the description the orchestrator wrote, the model it asked for and the size of
the prompt it was given. Measured here: 98 of 123 runs in `week_2026_09_05`. Runs that carry a description cluster on the description alone; the rest
cluster on **tool mix, working directory, branch and agent type** — the fields every turn carries in
full. `prompt` is deliberately *not* a clustering input: it is stored
truncated at `prompt_label_chars` and often begins with skill boilerplate. It is used only as a
human label, after known boilerplate prefixes are stripped and after `text.repair_mojibake` undoes
a double UTF-8 encoding in the stored bytes; when what remains is too short to name
a job the label is derived from the tools, repo and branch instead, and the cluster says which of
the two it is. Rule names reach the reader through `RULE_LABELS` everywhere, the narrative prompt
included, so internal ids like `context_bloat` never surface in prose. Every cluster reports a confidence (`named`, `high`, `medium`, `low`, `single run`,
`grouping only`) computed from run count, keyword agreement between the member runs' labels and
tool coverage; `named` is the top level and means every member run carries an orchestrator
description, so the label is quoted rather than inferred. A cluster whose runs share tools and repo
but not a job label is printed as `MIXED`. Not every run can be joined to its dispatch, so every
cluster list states the share of runs a description was recovered for.

**Cluster cap.** A description names one job, so most description clusters hold a single run and a
window produces far more clusters than the eight the tool-mix labels used to produce. Clusters are
ranked by weighted cost and the first `rootcause.max_clusters` (12) are kept; everything below folds
into one tail bar, and `tail_note` states how many clusters and how many runs that bar holds, so the
collapsed remainder is never silently dropped.

**Tool coverage is stated, never hidden.** `tools` is populated on roughly half the turns — a
text-only turn records none — so every per-cluster and per-finding tool share is accompanied by the
share of turns that recorded any tool call at all.

### `report.py` — weekly HTML

Reads every `data/week_*.json` plus the target window's `data/records/<key>.jsonl`, and renders one
self-contained HTML page. The reading path is, in order:

1. **Verdict** — four tiles (quota or ceiling used, weighted spent, list-price USD or its stand-in,
   the share of subagent spend no agent type claims). The first names the fitted value and the
   sample count behind it, so a reader who remembers last week's percentage can see the denominator
   that moved. The third is a price only where coverage allows one. The last is a warning, not a
   stat: above `report.UNATTRIBUTED_WARN_SHARE` (25%) it carries a warning rule and says the runs
   were dispatched with no agent type, so nothing further down the page can name them. The tiles sit
   over a seven-day burn line against the
   ceiling with the reset instant marked. The reference line is labelled *quota* only when the
   ceiling came from a usage sample or a config override, and *estimated ceiling* otherwise, so it
   never contradicts the tile above it. The axis runs over every calendar day from the window start,
   zero-filled, because `by_day` holds only days with spend.
2. **What changed** — the week-over-week decomposition, read from the window's stored `delta`
   block. One diverging bar per repo × lane slice, each bar coloured by the cause the disjoint
   `CAUSE_PRECEDENCE` assigned it, largest absolute movement first, at most eight bars plus an
   *everything else* bar carrying the count. Above the chart sits a sentence generated from the
   rows, naming the largest movement and its share of the gross movement; under it the accounting
   line, *"+800.8M accounted, +800.8M total"*. The chart ships twice: the wide rendering puts each
   label left of the baseline, and a second one stacks the label above its bar in a narrow viewBox,
   swapped in by stylesheet under 640px so the bars stay on screen on a phone. When there is no earlier window, or the earlier one
   holds fewer than `delta.MIN_PREVIOUS_TURNS` (100) turns, the block is one line saying so.
3. **Why this week looked like this** — the narrative, capped at four sentences and 90 words, and
   handed the delta claim and the anomaly claims as its context. When no narrative exists neither the
   heading nor the section is rendered. The header meta line always states the outcome: *narrative
   written*, *narrative trimmed to N words*, *narrative reused*, or *narrative off: <reason>* — never
   a bare absence.
4. **What looks wrong** — the anomaly lane, read from the window's stored `anomalies`. At most
   `report.ANOMALY_CARDS` (5) cards, ranked by score, and a line counting the rest. Each card is a
   claim with its measured numbers, one chart or table, the concrete action, and the coverage of the
   field the detector read, named in plain words rather than by its record key and printed only when
   some turns are missing it. Actions whose wording depends only on the anomaly's own subject are
   built while rendering, not read from the stored string, so a rewording reaches every existing
   window. A window with no anomaly prints one line.
5. **Findings** — one card per finding group worth at least `report.FINDING_CARD_SHARE` (1%) of the
   window, carrying exactly one chart or table as its evidence. Everything else, the root-cause line
   included, sits behind a `<details>`; smaller findings are counted in a line and keep their rows in
   the rule lenses.
6. **Cost centres** — one ranked bar chart per lane: jobs by dispatch description where recovered,
   MCP servers, skills. Each footer states the coverage of the field its lane groups by. Clusters
   carrying a recovered label hold the head of the jobs ranking; every other cluster groups by agent
   type and repo into a bar reading *"31 more general-purpose runs in product-promotion-service"*,
   drawn in the derived colour the legend names. The lane draws the six largest such groups and its
   footer says how many it left out.
7. **Do these first** — two blocks. *Last week's advice* is one row per recommendation the previous
   window produced: the title, its share of that window, its share of this one, and the movement in
   points, reading *unchanged* only when the movement rounds to 0.0 points
   (`report.UNCHANGED_POINTS`). The rows are read by running
   `advice.recommend` over the previous window's stored aggregate, never off the rendered page, and
   the four largest movements are drawn. *New this week* holds at most `report.NEW_CARDS` (2) cards,
   for recommendations that were absent last week or grew by more than `report.GROWN_POINTS` (5)
   points; when nothing qualifies it is one line. A recommendation carded as new is dropped from the
   movement table, so no piece of advice appears twice in the section. Both halves are keyed on the
   row's own **title**: one window emits several `model_downgrade` items, one per model, and keying
   on kind alone collapsed them onto one row and printed one model's figure against another model's
   name. Subject alone does not work either, because two rules name a session and a session id is
   new every week. The overlap notice appears here, once.
8. **Raw breakdowns** — the remaining lenses, collapsed. The round-trips table lives here, on a
   `<details>` carrying `id="round_trips"`, because the `failing tool` anomaly card's action sends
   the reader to it. An anomaly whose action names a table declares the anchor; the card links the
   phrase when that anchor is on the page and drops the pointer when it is not, so the action can
   never name a table the reader cannot reach.
9. **Quota you did not use** — the `headroom` group, rendered only when a recommendation of that
   kind exists, carrying its `id="rec-headroom"` anchor.

**Under-spend is not a cause of spend.** The `headroom` finding's `weighted_cost` is the quota that
expired unused, so it would lead every cost ranking on the page. It is excluded from the findings
cards, the rule-lens tables, the delta decomposition's cause precedence and the console summary's top
causes, and rendered instead as one line under the Verdict tiles — *"1.8B unused of your quota, two
windows running"* — linking to `#rec-headroom`. A headroom recommendation carries its figure in
`weighted_headroom`, not `weighted_saving`, so `report.figure_of` returns the value and its basis, the
cards read "headroom, N% of the window" rather than a share of savings, and the recommendations table
has a `basis` column.

A spike must attribute to a named job, never to a taller unexplained bar.

**One chart per rule.** `context_bloat` draws the session's context over time, coloured by the tool
whose results grew it, with the threshold line and the compaction markers. `subagent_storm` draws a
run timeline labelled with the orchestrator's descriptions, so overlap is visible. `model_mismatch`
is a strip of output tokens against thinking tokens, coloured by model, with the counted region drawn
as a box and its criteria stated in the top-right corner. Tool count is not an axis: the rule admits
exactly one tool call, so plotting it would imply a spread that does not exist.
`agent_type_skew` is a bar per job cluster with the no-run-id residual as its own bar.
`redundant_reads`, `loop_retry` and the round-trip detectors get tables. `whale_turns` keeps its rule
and its collapsed table in the raw breakdowns, but has no finding card: ten near-equal bars conveyed
nothing a sentence did not.

**A chart that draws a sample says so.** A session's context series is one point per turn that
carried context, downsampled to `context.SERIES_POINTS` (300) bins; each bin keeps its largest point
and the growth of the whole bin, so the peak a reader judges the threshold line against is real. The
footer states the binning — *5,233 turns binned to 300 points, each point the max of its bin* — so
the headline turn count can be checked against what the chart actually plots. The three axis labels
name the **turn** a bin stands for, not the bin's index, and carry the date as well as the clock when
the session spans more than one day, so a four-day session cannot read as one morning.

**A legend names what is drawn, and only what is readable.** Every colour a chart paints carries a
legend entry: the context-bloat chart colours the five largest tools and folds the rest, including
turns led by no single tool, into one grey *other or unattributed* entry. Nothing is listed that has
no mark, and a whale-turn class is dropped from the legend when it holds under
`report.WHALE_LEGEND_SHARE` (0.5%) of the drawn total or its tallest segment cannot reach
`report.WHALE_LEGEND_PIXELS` (2) plot units; either way it renders as a hairline nobody can find,
and the footer then says smaller classes were omitted. The marks themselves are never removed.

**No two rows carry the same label.** `charts.distinct_labels` elides the prefix and suffix shared by
every label, and where that still leaves duplicates it re-elides within the colliding group alone and
falls back to appending each row's start clock. Run timelines, the deep-dive cluster tables and the
collapsed recommendations overview all go through it.

**Word budget.** Visible prose outside `<details>` is capped at 1,100 words, measured by
`report.visible_words`, which drops every `<details>` body but keeps its `<summary>`, and drops the
text inside the SVG charts: axis ticks and bar labels are chart furniture, and counting them charged
the page for drawing the evidence the design asks for. A test renders a real-shaped fixture window
through `collect.aggregate_window` and fails above the cap. Four things hold the budget on a busy
window: the narrative is clipped to `NARRATIVE_WORDS` at render time as well as asked for in the
prompt, findings below 1% of the window fall back to the rule lenses, the anomaly lane and the
new-advice cards are capped at five and two, and every chart caps its visible rows — the collapsed
remainder is always stated, never dropped.

`evidence.py` holds the per-card chart payloads and the lane rankings, and `charts.py` the SVG
geometry; `report.py` is left with page assembly. `evidence.build` is attached to the root-cause
analysis under the `evidence` key, so a window with no readable records degrades the same way the
rest of the page does.

The record store is an *optional* input: a window whose `.jsonl` is missing or unreadable still
renders, with the root-cause and drill-down sections stating plainly that they had nothing to read.
Loading records is a file read, not an API call, so the format-rebuild pass stays free.

After rendering, invokes a single headless Claude call with the window's findings
to produce the narrative section, and injects the result. If that call fails the
page still renders, minus the prose.

The call asks for `--output-format text` and reads the child's bytes itself, decoding UTF-8 with
`errors="replace"` and setting `PYTHONIOENCODING`, so an em dash in the answer cannot arrive as
mojibake on Windows. The prompt asks for at most `NARRATIVE_SENTENCES` (3) sentences and
`NARRATIVE_ASK_WORDS` (75) words, deliberately under the refusal threshold, since a model asked for
exactly the limit lands a word or two over it; it is also told to answer with the paragraph alone,
because a preamble stating its own word count is what pushed a compliant answer past the cap. An answer of
`NARRATIVE_WORDS` (90) words or fewer is taken verbatim. Between 91 and `NARRATIVE_TRIM_WORDS` (160)
words it is trimmed to the last whole sentence that still fits inside 90 words and at most
`NARRATIVE_MAX_SENTENCES` (4) sentences, and the header says *narrative trimmed to N words*; the
sentence splitter breaks only on terminal punctuation followed by a capital, so an abbreviation such
as *e.g.* is never a cut point and prose is never cut mid-sentence. An answer past 160 words, or one
with no whole sentence inside the cap, is refused and asked once more under a two-sentence cap; a
second such answer is dropped and the page says so rather than printing a wall of prose. `build_narrative_prompt` takes an optional `extra_context`
string, appended through `narrative_context_lines`, so the delta decomposition and the anomaly lane
can be handed to the prompt instead of the raw totals.

Interface: `report.main(argv)`, reached from the CLI as
`report [--window YYYY-MM-DD] [--no-narrative] [--all] [--refresh-narrative]`.

### `cli.py` — the single entry point

One `argparse` front end with six subcommands: `collect`, `report`, `status`, `quota`, `tune`,
`install-schedule`. It also owns the scheduler renderers and interpreter resolution. `quota` prints
the latest usage sample and the ceiling derived from it, for debugging.

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
reset_weekday        fallback weekday, used only until a quota sample lands
reset_hour           fallback local hour the window rolls over
token_class_weights  per-token-class multipliers
model_weights        per-model multipliers, plus default_model_weight
ceiling              override, the quota-fit parameters, and the top-cluster calibration
narrative_model      model the single headless call uses
prompt_label_chars   how much of a user prompt is stored as a label, 400; raising it improves
                     labels on newly collected turns only, never history already stored
rootcause            drill-down share floor, centre and cluster caps, findings shown with a
                     root-cause line, label length
thresholds           per-rule tuning
headroom             the under-spend cap and the thresholds that define judgement work
advice               saving floors, the agent list judged safe to downgrade, and the agent list
                     judged to need the top tier
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

Three methods, tried in order, all behind one function.

`quota-fit` is the first. Each quota sample pairs a reported utilization with the
weighted spend of its window at that instant, so the two are a line through the
origin and the ceiling is its slope: a least-squares fit of
`weighted_so_far = ceiling * pct / 100`. The residual spread is reported as the
confidence band. Samples below `min_pct` are dropped, because at one percent
utilization a one-percent rounding step is a 100% relative error, and fewer than
`min_samples` points is no fit at all. Only the current and the previous
`windows - 1` windows count, so a limit change ages out. When the newest sample
is younger than `fresh_hours`, `percent_used` is that sample's own figure rather
than the fit of it: Anthropic's number beats a fit of Anthropic's numbers.

`override` is next, whatever the user pinned by hand.

`top-cluster` is the fallback for a machine that has never reached the endpoint.
The first backfill run computes weighted totals for every historical window,
windows whose totals cluster at the top of the observed distribution are treated
as windows where the cap was approached, and the ceiling is derived from that
cluster. It is an estimate of a habit, not a quota, and every place that shows
it says so. When any quota sample exists at all, the estimate is labelled
*estimate, likely low* and states the floor that sample already implies: a single
utilization reading bounds the real ceiling from below at `spent / utilization`,
so an estimate under that floor is known to be too small before anything else is
measured.

**One ceiling, applied everywhere, and a page that says when it moved.** The
ceiling is computed once per `collect` run over every window, so a closed window
picks up a fitted ceiling retroactively the moment the fit exists, and its tile
says so rather than leaving the reader with two denominators for one week. The
rendered page carries the ceiling and its method in `report-ceiling` and
`report-ceiling-method` meta tags; the next render compares against them and the
Verdict states the move once, as *ceiling changed since this page was last
rendered: 1.4B estimated to 3.3B fitted*. The following render stamps the new
value, so the notice does not repeat.

Derived figures: percent consumed, current burn rate against the rate sustainable
for the remainder of the window, and projected exhaustion date.

## Layout

The plugin ships code only. Nothing is ever written into the install directory; see
[Storage](#storage).

```
my-token-spend/
  .claude-plugin/plugin.json
  bin/my-token-spend  bin/my-token-spend.ps1
  commands/my-token-spend.md
  skills/my-token-spend/SKILL.md
  src/  cli.py collect.py rules.py context.py cost.py rootcause.py advice.py evidence.py
        report.py charts.py tune.py quota.py paths.py
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

The `oauth/usage` endpoint is undocumented and can change shape or disappear.
Mitigation: every failure is a skip with one log line, the collector finishes
without it, and both the window cut and the ceiling fall back to what the plugin
inferred before.

The token is read but never refreshed, because Claude Code owns the refresh
rotation and a second refresher would invalidate its session. An expired token
is a skip until Claude Code's next run renews it.

The token and the transcripts can belong to different accounts. Nothing local
distinguishes them, so the quota would be the token owner's while the spend is
this machine's. This is not detectable and is not mitigated.

Transcripts are local to this machine. Sessions run elsewhere are invisible and
the reports will understate spend accordingly.

## Window JSON schema

`data/week_YYYY_MM_DD.json` is the canonical artefact. It is complete enough that
`report.py` never needs to re-read a transcript. `schema_version` is `2` and
`analysis_version` is `rules.ANALYSIS_VERSION`.

```
schema_version   int
analysis_version int, the reasons-engine revision the findings were written by
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
by_mcp_server    keyed by the MCP server the turn was attributed to
by_plugin        keyed by the plugin the turn was attributed to
field_coverage   {field: {present, total, share}} for every field added after
                 schema_version 1, plus tool_results as a share of tool calls
                 rather than of turns. Records written before a field existed
                 carry none of it, so a lens over it is read against its own
                 coverage and never against the window's turn count
unknown_models   the subset of by_model whose name matched neither an exact nor a
                 family weight and was therefore priced at default_model_weight

by_session       [{key, turns, weighted, tokens..., sidechain_turns,
                   sidechain_weighted, models[], agents[], first_ts, last_ts,
                   cwd, gitBranch, first_prompt}]
                 cwd/gitBranch are the session's last observed values

context          context.window_block output: the context-growth breakdown the
                 report renders
                 {threshold, coverage{present, total, share}, statement,
                  sessions[{session, turns, growth_total, growth_by_tool[],
                   prompt_growth, unattributed_growth, attributed_share,
                   top_results[{tool, chars, ts, growth}], compactions,
                   carry_tax, excess_tokens, peak_cache_read, first_ts, last_ts,
                   cwd, tool_results_coverage, largest_by_tool{},
                   compaction_ts[], series[[ts, context, growth, tool]]}]}
                 series is binned down to 300 points - each bin sums its growth,
                 so the bars still add up to growth_total - and compaction_ts
                 carries the markers so they survive the binning
                 only sessions that carry a context_bloat finding, the ten
                 heaviest by carry tax

findings         rules.evaluate output, ranked by weighted_cost desc
                 [{rule, subject, detail, weighted_cost, evidence{...}}]
                 evidence is rule-specific; whale_turns carries uuid, ts, model,
                 effort, prompt, tools[], cwd, gitBranch, rank
findings_by_rule {rule: {count, weighted_cost}}, ranked by weighted_cost desc

ceiling
  estimate               weighted tokens, or null
  method                 quota-fit | override | top-cluster | insufficient-data
  approximate            false only for an override
  cluster_size           windows averaged to produce the estimate
  windows_considered
  samples_used           quota samples behind a quota-fit
  band_pct               residual spread of the fit, as a percent
  latest_pct             the newest reported utilization
  latest_pct_is_fresh    true when that sample is younger than fresh_hours
  percent_used           null when no estimate exists
  percent_used_source    quota-sample | ceiling-estimate
  floor                  spent / utilization from the best quota sample, or null
  floor_spent            the weighted spend behind that floor
  floor_pct              the utilization behind that floor
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
still only unions. `--rescan` also re-reads every transcript from byte zero, and
merges field by field into each stored record instead of replacing it, so a
record collected under an older schema gains the fields it was missing while its
`weighted` stays exactly as priced. That makes it the upgrade path after a schema
change. A run that would shrink a closed window refuses and names the
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
isSidechain, agentId, attributionAgent, attributionSkill, mcp_server, mcp_tool,
plugin, per_turn_effort, stop_reason, compacted, after_compaction,
source_tool_use_id, cwd, gitBranch, version, input, output, thinking,
cache_create, cache_create_5m, cache_create_1h, cache_read, weighted,
tools[{name, hash, tool_use_id, result_chars, is_error, denied}], text_chars,
is_api_error, prompt`.

`cache_create_5m` and `cache_create_1h` split `cache_create`, which stays their
sum and stays priced at one flat weight. `after_compaction` marks the turn that
follows a compact summary. `source_tool_use_id` is the transcript's first
`sourceToolUseID`: the tool call whose result produced that user entry, stamped
on every turn of the file. It is **not** the Agent call that spawned a subagent.
Measured over 623 subagent transcripts here, not one of their `sourceToolUseID`
values is an `Agent` tool_use id; every one observed points at a `Skill` call
inside the subagent itself. The field is stored because it is real, and nothing
joins on it.

A tool call's outcome is joined onto it from the `tool_result` block that names
its `tool_use_id`, within the same transcript file and in the same pass. Results
arrive in the user entry after the call, so a pass that ends between the two
stores the call with `result_chars`, `is_error` and `denied` all null and hands
the unmatched result to the next pass, which fills it into the stored record.
A null outcome therefore means *not yet known*, never *succeeded*: every figure
derived from outcomes states the share of calls that carry one. Records written
before this field existed carry none, and stay unresolved until `--rescan`
re-reads the transcripts that are still there.

`data/records/agent_calls_week_YYYY_MM_DD.jsonl` holds one line per `Agent` tool
call, keyed by `tool_use_id`: `{tool_use_id, ts, sessionId, parent_uuid,
description, subagent_type, model, prompt_chars, prompt_head}`. `parent_uuid` is
the uuid of the assistant turn that issued the call. These files are merged by
`tool_use_id` on every run and never shrink, under the same rule as the records,
and are bucketed by the dispatch timestamp. They are always read as a whole set,
so a subagent whose turns land in the window after its dispatch still joins.

`state.json` maps absolute transcript path to
`{offset, size, mtime, last_prompt, malformed, source_tool_use_id,
pending_compaction}` under `files`, and carries the `schema_version` the store
was last written under alongside it. A file is skipped when size and mtime are
both unchanged, resumed from `offset` when it has strictly grown, and re-read
from byte zero otherwise; `--rescan` re-reads every file whatever its offset says
and leaves the offsets at each file's new end. A trailing line without a newline is left unconsumed until it is
complete.

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
| `headroom` | headroom | Quota that expired unused. These spend rather than save, and each one names the specific work it would move. |

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
| `upgrade_tier` | headroom | headroom | Not a saving. `weighted_headroom = component_weighted * (opus_weight / current_weight - 1)`, for a component whose definition file declares `model: sonnet` or `haiku` and whose runs show the judgement profile. Offered only when it fits inside the unused quota. |
| `widen_fan_out` | headroom | headroom | Not a saving. `weighted_headroom` is the median weighted cost of one run in the session, which is what one more parallel lane of comparable work costs. Offered only when it fits inside the unused quota. |
| `extra_usage_unused` | headroom | headroom | Neither a saving nor a price. One line stating the untouched overage budget in its own currency, with no action attached. |

A headroom recommendation carries `weighted_saving = 0.0` and puts its figure in
`weighted_headroom`, because presenting a spend in the saving column would be a
lie in the one place the reader trusts most. `percent_of_window` is computed from
whichever of the two is set. No headroom text tells the reader to use more tokens
or to spend the remaining quota; every one that carries an action names the file
or the session it would change.

Two derived quantities, both read from the window's own embedded `weights` so a
historical window is priced the way it was collected:

```
downgrade_factor      = 1 - model_weights[downgrade_model] / max(model_weights)
expensive_model_share = (weighted spent on models priced above the downgrade model)
                        / window total weighted
```

`right_size_agent_tier` multiplies by both because only part of the window ran on an
expensive model, and moving down recovers only the price gap, not the whole cost.

The median rule refuses to fire on too small a sample: `min_storms_for_median` (4)
findings are required, and a population where nothing exceeds the median produces
nothing.

### Filtering and ranking

A recommendation is dropped entirely below `advice.min_saving` (250,000 weighted).
That floor is about savings, so headroom recommendations do not pass through it;
their own gate is that the move fits inside the quota the window left unused.
Survivors get `percent_of_window` and a `score`:

```
score = weighted_saving * confidence_weight     high 1.0, medium 0.6, low 0.3
```

and are ranked by `(-score, kind, subject)`. Ranking by score rather than by raw
saving keeps a large speculative number from outranking a smaller certain one.

Each kind carries a fixed risk and confidence, which is what the score above uses:

| Kind | Performance risk | Confidence |
|---|---|---|
| `model_downgrade` | none | medium |
| `deduplicate_reads` | none | medium |
| `break_retry_loops` | none | medium |
| `reset_context` | low | medium |
| `right_size_agent_tier` | low | medium |
| `right_size_fan_out` | medium | low |
| `upgrade_tier` | none | low |
| `widen_fan_out` | medium | low |
| `extra_usage_unused` | none | low |

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


## Reasons-engine versioning

A rule finding's detail sentence and its evidence are rendered by `rules.evaluate` at
collect time and frozen into `data/week_*.json`. `report.py` only re-renders what it
finds there. So changing a rule's wording, its threshold, or the shape of the aggregate
leaves every already-written window showing the sentence the old code produced, with no
sign that it is out of date.

`rules.ANALYSIS_VERSION` is the revision of that engine, and `aggregate_window` copies it
into every window as `analysis_version`.

### Bumping the version

**Bump `rules.ANALYSIS_VERSION` in the same commit as any change to the text a rule
emits, to a threshold that decides whether a rule fires or how it is worded, or to the
shape of the window aggregate.** It does not cover a change that only affects rendering,
because `report.py` re-renders from the stored aggregate on every run.

A missed bump leaves closed windows explaining themselves in the old language. The cost
of an unnecessary bump is one re-aggregation per window, which reads nothing but the
record store.

### The re-analysis pass

Every plain `collect`, after the incremental ingest, re-aggregates any window whose
stored `analysis_version` is missing or below `rules.ANALYSIS_VERSION` and prints
`re-analysed N window(s) after a rule change`. A window stamped *above* the code's
version is left alone: the store was written by a newer build and downgrading it would
lose information.

The pass reads only the durable record store. It never writes a record and never changes
a weighted total — re-pricing is `--reprice`'s job and its semantics are untouched. A
window the run was already going to write (the usual case, since `collect` without
`--window` re-aggregates the whole store) is simply counted; `--window` narrows the
normal write but not this pass, so a stale window outside the requested one is still
brought current.

`report` refuses to render while any window is stale rather than printing sentences from
a rule revision that no longer exists, and names `collect` as the fix.


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

- **A format rebuild reuses the prose it already has.** `data/narratives.json` holds one
  entry per window — the prose and the note describing the last outcome — and the stale
  page's `data-narrative` attribute is the fallback when the sidecar has no entry yet.
  Rebuilding pages that already have prose costs zero tokens.
- **A window that has never been asked gets one call, once.** A closed window with no
  stored narrative and no recorded attempt is asked on the next render even without
  `--refresh-narrative`; the outcome, prose or refusal, is written to the sidecar, so the
  call is never repeated. `--no-narrative` suppresses it.
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
  src/  collect.py rules.py context.py rootcause.py advice.py report.py tune.py cli.py
  tests/
  README.md
```

Commands and skills invoke `python3 "${CLAUDE_PLUGIN_ROOT}/src/cli.py"`. Exec
form cannot run `.cmd` shims on Windows, so the interpreter is always invoked
directly. `${...}` placeholders are quoted for paths containing spaces.

Subcommands: `collect`, `report`, `status`, `quota`, `tune`, `install-schedule`.

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

## Reset instant

`GET https://api.anthropic.com/api/oauth/usage`, with the OAuth token Claude Code
stores in `~/.claude/.credentials.json` or in the macOS Keychain under
`Claude Code-credentials`, reports `seven_day.utilization` as a percent and
`seven_day.resets_at` as a UTC instant. The collector appends one sample per run
to `data/quota_samples.jsonl`, tagged with the window spend at that moment.

Windows are cut at those instants. The window containing a moment starts at the
latest known instant at or before it, stepping by exactly seven days beyond the
sampled range in either direction, and ends at the next known instant or seven
days later, whichever comes first. `resets_at` jitters by fractions of a second
between calls, so instants are rounded to the minute before they are compared.
Window keys stay the local date of the start instant, so old reports still line
up. With no sample at all the cut falls back to `reset_weekday` + `reset_hour`.

When the first sample moves a boundary under records already stored, `collect`
names the affected windows and asks for `collect --recut-windows`.

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


## Anomalies

The findings rank a window by weighted cost. That buries behaviour: a ten-minute loop that issued the
same shell command 101 times is worth 1.9M and lands below an expensive but entirely ordinary storm.
`rules.anomalies` is the second lens, and it ranks by strangeness instead.

Six detectors, each with its thresholds under `anomalies` in `config.json`:

| detector | fires when | score basis |
|---|---|---|
| `reply_skill_headless` | a skill from `reply_skills` spends more than `reply_headless_share` of its weighted tokens inside headless sessions | that share |
| `repeated_tool_input` | one tool and input hash repeat more than `repeat_min` times within `repeat_minutes` inside one run | repeats a minute |
| `context_growth_tool` | one tool is more than `growth_share` of a session's context growth over at least `growth_min_results` results, and its median result clears `result_kb` or its total clears `growth_total_mb` | share times whichever gate fired |
| `failing_tool` | more than `fail_min` failed calls in the window with one tool above `fail_dominant_share` of them | the failure count |
| `unattributed_subagents` | more than `unattributed_share` of subagent weighted spend carries no agent type | that share |
| `mcp_server_share` | one MCP server is above `mcp_share` of the window | that share |

**The reply-skill action names a file that exists.** `rules.reply_skill_action` resolves the skill
through `agentfiles.resolve_skill` over the roots `collect` builds once per run, so the card prints
the installed plugin cache path rather than a guessed fragment. When the file sits inside an
installed plugin, `agentfiles.plugin_identifier` recovers the `<plugin>@<marketplace>` id and the
action names the documented lever: `"<id>": false` under `enabledPlugins` in the project's
`.claude/settings.json`. With nothing resolved it says to exclude the plugin for that project and
invents neither a key nor a path.

**A session is headless when nothing reads the reply.** `rules.headless_sessions` marks a session by
its modal `cwd` sitting under one of `headless_cwds`, by any turn whose `entrypoint` is in
`headless_entrypoints`, or by a first user entry whose `promptSource` is in
`headless_prompt_sources`. `collect.normalize` keeps `entrypoint` per turn and `collect.read_file`
carries the session's first `promptSource` onto every record of the file, the way it already carries
the spawning tool call.

**The score is a multiple of the detector's own threshold.** The six quantities are shares, counts
and rates, and no common unit exists between them. Dividing each by the threshold it had to clear
gives one dimensionless number that means the same thing for every detector: how far outside its own
normal this window sits. Each card also carries the quantity in its own words, so the ranking never
hides what it measured.

**Context growth fires on volume as well as size.** The size gate alone misses the shape that
actually fills a context: on the heaviest window on record, `Bash` is 67% of one session's growth
over 2,129 results and 6.2 MB, at a median of 1 KB each. A tool that hands back megabytes in small
pieces is the same problem as one that hands back a few large ones.

**Every card names something on disk.** A skill file and the project whose settings can exclude it, a
run by the description it was dispatched with, a tool, a server. A card that could only say *this
looks odd* would be a stat, not an action.

## Cost centres

Agents and skills are one kind of thing. Both resolve to files, both carry a per-run cost - per
`agentId` for agents, per session for skills, since a skill has no invocation id in the data - and a
median wall-clock per run. Skills additionally report their description length as a figure, with no
conclusion drawn from it: a character count is not evidence that a skill loaded on turns that did
not need it. A byte count says even less, so the file size is not reported at all. Skill and plugin
names are grouped by their
`plugin:` prefix into a per-plugin total. No skill ever gets a modelled saving: its attributed cost
is the cost of the work done under it, not the cost of loading it.

## Proposals in both directions

A file-level proposal moves a component down a tier and is gated by
`advice.sonnet_class_agents`. The setting-level proposal for a built-in agent type, which moves
every built-in subagent at once through `env.CLAUDE_CODE_SUBAGENT_MODEL`, passes the same gate: it
is the widest change tune can name, so it is the last one that should be ungated. A built-in that is
not on the list shows its cost with no proposal, as an unlisted file-level component does.

The inverse proposal moves a component **up** a tier. It requires all of: the definition declares a
cheap model, the name is listed in `advice.opus_class_agents`, an analysed window fired the headroom
rule, the component cleared the same `min_windows_for_proposal` floor a downgrade must clear, and
the priced increase fits inside the unused quota. It renders as its own section that states it
spends rather than saves, and it emits the same kind of unified diff a downgrade does. A component
on neither list is `NOT_ASSESSABLE` with its cost shown and no verdict either way.

## The decisions section

A component on neither class list is the one thing `tune` cannot resolve on its own, and it used to
surface as a refusal buried among dozens of cost entries. It is now the first section of the output.
Every agent type above `min_cost` that is on neither list is listed with its typical window cost, its
run count, its median thinking per turn and its median output per turn — the four figures a reader
needs to judge whether the work is judgement work — and with the exact `config.json` line that adds
it to either list. The section closes by saying that classifying them lets the next run price them.
The two per-turn medians come from the stored records, so an agent that ran in no loaded window
shows zeroes rather than a guess.

Two counts keep the rest of the output short. A setting-level proposal worth less than
`min_saving_share` (1%) of a typical analysed window folds into one counted line instead of a card:
its blast radius argument is longer than its figure is large. "Cost without a proposal" lists the
components whose **typical or peak** window cost reaches `min_reported_share` (3%) of a typical
window, and states how many cleared the weighted floor but reached that share in neither. An entry
that qualified on its peak prints the peak beside its typical figure, so a listed component is never
smaller than the stated filter.

## What moved since last run

`tune` stores each run's proposals and their figures in `data/tune_last.json` and compares the next
run against it: then, now, and whether an entry is new or no longer proposed. On the first run the
section is one line. A moved figure is a moved estimate over different windows, not a measured
saving, and the section says so.

## Wasted round trips

Measured from the durable record store alone. Each tool call carries the outcome of its own
`tool_result`, joined at collection time by `tool_use_id`, so no transcript is read here and a
pruned transcript cannot change the figures. A call whose outcome was never captured counts as
unresolved, and the section states the share of calls that carry one. Shipped detectors, all
evidence-backed:

| detector | source | observed |
|---|---|---|
| tool call returned an error | the record's own `is_error` flag | 1.3-1.6% of weighted, every window |
| same input re-issued after it failed, and failed again | the above plus the record's tool hash | 0.00-0.16% |
| permission-denied call | the record's own `denied` flag, set from the error text at collection | 0-2 per window |
| turn the API errored | the collector's `is_api_error` | ~0.00% |

The card headline and cost are the window totals from the failed-call detector, not the subtotal of
the tools the table happens to show; when the table is truncated its footer says *"top 4 of N
tools"*. Each row also names the lane most of that tool's failures came from — the agent or skill and
the repo, with the count — because the error text and the call's input are not stored, so the lane is
as close to a call site as the record store reaches, and the card's action promises no more than that. The failed-again calls are a subset already inside that figure, so the sentence reads
*including*, never *and*, and the two are never added.

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
| Scraping `/usage` for the true remaining quota | The `oauth/usage` endpoint, sampled once per collect | Scraping the web page was rejected as fragile and still is. The endpoint the CLI itself calls is not, and it reports both the utilization and the reset instant the plugin had been inferring |
| Guessing the reset weekday from limit-notice wording in transcripts | Removed, with `reset_weekday_confirmed` and `reset_weekday_source` | The corpus contained no such notice, so the detector never fired, and the endpoint answers the question outright |

