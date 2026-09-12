# my-token-spend: real quota, job-level attribution, one chart per finding

Status: draft for review. Baseline data for before/after comparison: `~/.claude/plugins/data/my-token-spend/reports_old/`.

## Purpose

The plugin answers "what did I spend and how much" well. It answers "why" only from shape: session length, cache size, turn counts, repeated hashes. Its only recommendation lever is "move this agent type to Sonnet". This design adds the three things the goal needs and the current data cannot give:

1. A real weekly quota and reset instant, from Anthropic, instead of a ceiling inferred from the user's own heaviest weeks.
2. Job-level attribution: what a subagent run was for, and which tool results made a session expensive.
3. A report where every claim stands on one chart or table, plus an under-spend recommendation for users who leave quota unused.

Everything the current code guarantees stays: the durable record store never shrinks, `tune` never writes, overlapping findings are never summed, no sentence asserts a motive, the HTML has no external references.

## Section 1: real quota and reset instant

### Source

Claude Code stores its OAuth token in `~/.claude/.credentials.json` under `claudeAiOauth.accessToken` (Windows and Linux) or in the macOS Keychain under the service name `Claude Code-credentials`. `GET https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20` returns, verified on 2026-09-12:

- `seven_day.utilization` (percent) and `seven_day.resets_at` (UTC instant). This is the weekly quota the plugin has been estimating.
- `five_hour.utilization` and `resets_at`.
- `seven_day_opus`, `seven_day_sonnet`: per-model weekly buckets, null on this plan, present on others.
- `extra_usage`: org overage budget with `monthly_limit`, `used_credits`, `currency`, `decimal_places`.
- `limits[]`: the same data with `severity`.

### Behavior

New module `quota.py`, called from `collect.run` after records are written:

1. Read the token. If missing or `expiresAt` is in the past, skip with one log line. Never refresh the token: Claude Code owns the refresh rotation and a second refresher would invalidate its session.
2. Call the endpoint with a 10 s timeout. On any non-200 or network error, skip with one log line. The collector must finish without the network.
3. Append one sample to `data/quota_samples.jsonl`: `{ts, seven_day_pct, seven_day_resets_at, five_hour_pct, five_hour_resets_at, per_model: {...}, extra_usage: {...}, weighted_so_far}` where `weighted_so_far` is the weighted total of the current window at `ts`.

`cli` gets `quota` as a standalone command that prints the latest sample and the derived ceiling, for debugging.

### Window boundary

The reset instant replaces the configured weekday. `collect.window_start(ts)` becomes:

1. Load every distinct `seven_day_resets_at` from the samples, sorted.
2. The window containing `ts` starts at the latest known reset instant less than or equal to `ts`. If `ts` is before the first known instant or after the last, step by exactly 7 days from the nearest known instant.
3. If no sample exists, fall back to the current `reset_weekday` + `reset_hour` config, converted to a UTC instant.

Window keys stay `week_YYYY_MM_DD` of the local start date, so file names and old reports still line up. Windows are cut in UTC. Display converts to the configured timezone. The current cut at Saturday 00:00 Prague is 5 hours off the real Saturday 03:00 UTC boundary and drifts by an hour across DST; the UTC instant fixes both.

The first collect after this change detects that the stored boundary differs from the real one and prints the `--recut-windows` instruction, using the existing recut path. `reset_weekday_confirmed`, `reset_weekday_source`, and the transcript-prose weekday detector in `collect.reset_weekday_candidates` are removed. The config fields `reset_weekday` and `reset_hour` stay as the no-sample fallback.

If the reset instant shifts, for example after a week without usage, the new instant appears as a new distinct value in the samples and step 2 uses it. The recut path handles the re-slicing.

### Ceiling

`collect.estimate_ceiling` gains a first method, `quota-fit`:

- Take samples from the current and the previous two windows whose `seven_day_pct >= 10` and whose `weighted_so_far > 0`.
- Ceiling = least-squares fit of `weighted_so_far = ceiling * pct / 100` through the origin. Report the residual spread as the confidence band.
- Requires at least 3 samples. Otherwise fall back to `override`, then to `top-cluster`.

The report names the method in the hero block: "of your weekly quota (fitted from 14 usage samples, ±4%)" for `quota-fit`, and keeps "estimated ceiling, from your own heavy weeks" for `top-cluster`. `percent_used` under `quota-fit` is the latest `seven_day_pct` directly when a sample is less than 6 hours old, because Anthropic's number beats a fit of it.

The 10% floor exists because at 1% utilization a rounding step of one percent is a 100% relative error.

### Failure modes stated in the report

- No token or no network for the whole window: hero block says "quota unknown this window, ceiling estimated from your own heavy weeks" and the under-spend rule stays silent.
- Token belongs to a different account than the transcripts: not detectable. Documented as a known limit in `docs/design.md`.

## Section 2: capture what the transcripts already say

### New record fields

`collect.normalize` adds, from the assistant entry:

- `mcp_server`, `mcp_tool` from `attributionMcpServer`, `attributionMcpTool`.
- `plugin` from `attributionPlugin`.
- `per_turn_effort` from `perTurnEffort`.
- `stop_reason` from `message.stop_reason`.
- `cache_create_5m`, `cache_create_1h` from `usage.cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`. `cache_create` stays as their sum. Weighting of the two classes stays identical until Section 5 shows a price gap.
- `compacted` (boolean) when `message.context_management` is present.

From user entries in the same session, joined by `tool_use_id`:

- `tools[].result_chars`: length of the `tool_result` content text, summed over blocks.
- `tools[].is_error`: the `tool_result.is_error` flag.
- `tools[].denied`: the existing permission-denial text match.

This makes `roundtrips.scan` unnecessary. The four round-trip detectors move to `rules.py` and run over records. `tune --no-round-trips` and the coverage warning go away because the data is always present for new records.

Compaction boundaries: a user entry with `isCompactSummary: true` marks the next assistant turn's record `after_compaction: true`.

### Agent calls

When an assistant entry carries a `tool_use` block named `Agent`, `normalize` also appends to `data/records/agent_calls_week_*.jsonl`:

`{tool_use_id, ts, sessionId, parent_uuid, description, subagent_type, model, prompt_chars, prompt_head}` with `prompt_head` clipped to `prompt_label_chars`.

`sourceToolUseID` on subagent transcripts points at Skill calls inside the subagent, never at the dispatching Agent call (0 of 623 files matched on this machine), so it cannot carry the join. The join is on the dispatch prompt instead: session id plus the first 200 normalized characters of the subagent's first user prompt, teammate envelope stripped, matched against the stored `prompt_head`; ties go to the latest call at or before the run started. Measured: 487 of 623 subagent files join uniquely.

`rootcause.group_runs` attaches the joined call to the run, so a run carries the orchestrator's `description`, its requested `model`, and its prompt size. `cluster_runs` keys on `description` first. Only runs without a joined call fall back to the derived tool-mix label. Cluster confidence gains a top level, `named`, meaning every member carries an orchestrator description. The report states coverage on every cluster list: "descriptions recovered for N% of runs". Old records without a matching call stay in the derived-label path.

### Schema and durability

`schema_version` of the record store goes up. Old records keep working; every new lens states the share of turns that carry its field. `collect --rebuild-from-transcripts-only` re-captures the new fields for transcripts still on disk. The shrink guard stays unchanged.

## Section 3: context growth attributed to its cause

`context_bloat` today fires on `cache_read` per turn, which is the consequence. The new derivation, in a new module `context.py` run at aggregation:

1. Order a session's records by `ts`. Growth at turn *n* is `cache_read[n] + cache_create[n] - cache_read[n-1] - cache_create[n-1]`, floored at 0, reset to 0 at `after_compaction`.
2. Attribute growth at turn *n* to the tool results of turn *n-1* in proportion to `result_chars`, to the user prompt text length when there were no tools, and to `unattributed` otherwise.
3. Aggregate per session: growth by tool name, top 10 single results by chars with the tool name and turn timestamp, number of compactions, and the carry tax (weighted cache-read cost above the threshold, as today).

Finding text becomes: "session re-read 360M tokens above the 150K threshold; 61% of the context growth came from 214 Bash results, the largest 412 KB at 14:02". The `<details>` list names the top 10 results. Tool input text is still not stored, so the sentence names the tool and the size, never the command.

The 150K threshold stays configurable and is drawn as a line on the chart.

## Section 4: report rebuilt around one justification per finding

### Budget

Visible text outside `<details>` is capped at 1,500 words. A test counts the words of the rendered page with `<details>` bodies removed and fails above the cap. The current page is about 6,800.

### Structure

1. **Verdict.** Four tiles: weekly quota used (real percent when known, method named), weighted spent, list-price USD from `cost-state`, unattributed share of subagent spend. Below them a 7-day burn line against the quota, with the reset instant marked.
2. **Do these first.** At most three cards. Each card: one sentence of claim with the threshold in it, one number for the upper-bound saving or headroom, the risk and confidence badges, and a link to its finding's chart. The existing "upper bounds, never summed, cost is not waste" notice stays, once.
3. **Why this window looked like this.** The narrative stays, capped at 120 words, and now receives the agent descriptions and the context-growth breakdown in its prompt so it can name jobs instead of hashes.
4. **Findings.** Each finding is a card with its claim, its threshold, and exactly one chart or table that is the evidence. Everything else moves into `<details>`.
5. **Cost centres.** One ranked bar chart per lane: agent description clusters, MCP servers, plugins, skills, repos, models. Every bar is labeled with what it is, and the chart footer states the coverage of the field it groups by.
6. **Raw breakdowns.** Unchanged content, all inside `<details>`.
7. **Recommendations.** Unchanged grouping, plus the new Headroom group from Section 5.

### One chart per rule

- `context_bloat`: per-turn context size over the session, threshold line, compaction markers, bars colored by the tool that grew it.
- `subagent_storm`: run timeline, one bar per run from first to last turn, height by turns, labeled with the agent description, overlap visible.
- `model_mismatch`: scatter of output tokens against tool calls, colored by model, point size by thinking tokens. The "trivial" region is drawn as a box so the reader can judge it.
- `agent_type_skew`: horizontal bars per description cluster within the type, with the unattributed residual as its own bar.
- `whale_turns`: the top 10 turns as bars decomposed into cache read, cache create, output.
- `redundant_reads` and `loop_retry`: table of tool, repeat count, first and last occurrence, cost share.
- Round-trip rules: table of tool, failures, retries, denials, cost.

Charts stay hand-rolled inline SVG, as today. The design system for color, axes and stat tiles follows the `dataviz` skill; light and dark both render.

### USD figure

`collect` reads `cost-state` entries. The last one per session is the session total. Per window, sum sessions whose first record falls in the window. Show it as "list price, as `/cost` shows it; not what the subscription bills". Per-model `costUSD` over token counts gives the implied price per token class per model. `collect --calibrate-weights` prints the implied relative weights next to the configured ones and changes nothing.

## Section 5: under-spend

### Rule `headroom`

Fires only when the ceiling method is `quota-fit` or `override`. Never on `top-cluster`, because a user measured against their own habit is always near 100%.

Trigger: the window closed with `percent_used < headroom.max_pct` (default 60) and the same held for the previous window. Two windows, so one quiet week never fires it.

Cost field: unused weighted quota, `ceiling - spent`.

### Recommendations, kind `headroom`

Each names the work that is currently running on a cheaper tier and prices the upgrade in the same weighted unit as savings, so the two directions read the same way:

- `upgrade_tier`: agent definition files and skills that declare `model: sonnet` or `haiku` and whose runs show judgment work, measured as median thinking tokens per turn above `headroom.min_thinking` (default 500) or median output above `headroom.min_output` (default 800). Cost of the upgrade = typical window spend × (opus weight / current weight − 1). Only recommended when it fits inside the unused quota.
- `widen_fan_out`: sessions where runs waited on each other, measured as a peak of one live run while the queue of runs in the session was longer, and the total run time exceeds `headroom.min_serial_minutes` (default 20). The recommendation names the parallel cap the orchestrator role file sets, if one is found by the same file search `tune` uses.
- `extra_usage_unused`: when `extra_usage.is_enabled` and `used_credits == 0` for the whole window, one line stating the unused budget in its currency. No action attached; the reader decides.

### Invariants, mirrored from the existing ones

- No recommendation says "use more tokens" or "spend the remaining quota". Every headroom card names the specific work it would upgrade.
- The rule stays silent when the quota is unknown.
- `tune` proposes `model: opus` frontmatter patches for `upgrade_tier` targets through the same `sonnet_class_agents`-style whitelist, inverted: a new `opus_class_agents` list in config names the agents where judgment matters. Files not in either list get `NOT_ASSESSABLE`, as today.

Tests: a headroom recommendation never appears with `top-cluster`; every headroom card names a file or a session; `tune` still writes nothing.

## Section 6: rule and tune fixes

- `model_mismatch`: a turn is trivial only if `output <= 250`, exactly one tool call, `thinking <= model_mismatch.max_thinking` (default 200), and the one tool is not `Agent`. Dispatch turns are the orchestrator working, not waste. Confidence drops from high to medium because the rule still cannot see what the turn decided.
- `SETTING_PROPOSAL` for built-in agent types passes the same `sonnet_class_agents` gate as file-level proposals. Today the widest change `tune` can name is the only ungated one.
- `_tool_share` in `rules.py` divides a turn's cost evenly over its tool calls. It changes to divide by `result_chars` when present, so a redundant 400 KB read carries its own weight and a redundant `ls` does not.
- `roundtrips.py` is deleted after its detectors move to `rules.py`.
- `_skill_entry` in `tune.py` drops the sentence that infers over-triggering from description length. A character count is not evidence.

## Data flow after the change

`collect` tails transcripts, writes records with the new fields, writes agent calls, polls the quota endpoint, appends a sample, and cuts windows from the known reset instants. Aggregation runs `rules`, `context`, and `rootcause` over the records, joins runs to agent calls, and fits the ceiling from the samples. `report` renders the capped page with one chart per finding. `tune` reads the aggregates, the agent files, and the samples, and prints proposals in both directions.

## Testing

Unit tests stay in `C:\workspace\my-token-spend\tests`, outside the shared repo, as decided in MR 559. New tests per section: window cut from reset instants including the 7-day step and the fallback; quota-fit with the 10% floor and the 3-sample minimum; the endpoint client against recorded fixtures for 200, 401, timeout and missing token; `sourceToolUseID` join and coverage statement; growth attribution including the compaction reset; the 1,500-word cap; each new chart's presence per rule; every headroom invariant; the model_mismatch dispatch exclusion; the setting-proposal gate.

Acceptance check on real data: run `collect --rebuild-from-transcripts-only`, regenerate `week_2026_09_05`, and compare against `reports_old/week_2026_09_05.html`: the top finding must name a job by its orchestrator description, the hero percent must name its method, and the page must be under the word cap.

## Out of scope

- Billing figures. The USD shown is Claude Code's list-price estimate.
- Quality measurement of any tier change. The report keeps saying it cannot see this.
- Storing tool input text. Hashes stay; sizes and outcomes are new.
- Any write by `tune`.
