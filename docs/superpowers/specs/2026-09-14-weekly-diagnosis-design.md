# my-token-spend: from cost ledger to weekly diagnosis

Status: draft for implementation. Follows the 2026-09-12 spec, which is implemented and shipped as 1.6.0. Baseline for comparison: the critic review in the session scratchpad and `~/.claude/plugins/data/my-token-spend/reports_old/`.

## Purpose

The 1.6.0 page is an accurate ledger. It leads with three habits that never change between weeks, hides the one section that explains why a week differed, and its `tune` command produces nothing the author would change. The three facts a reader did not know sat in collapsed tables. This design turns the page into a weekly diagnosis: what changed, what looks wrong, what to do about it, and whether last week's advice worked.

Every guarantee stays: the durable store never shrinks, `tune` never writes, overlapping findings are never summed, no sentence asserts a motive, no external references, visible words under the cap.

## Section 1: defects

### List price attribution

Two pages show $191.94 for 1.7B weighted and $230.56 for 332M weighted. The window sum today takes each session's final `cost-state` total and books it to the window of the session's first record, so a session that spans a boundary lands whole in the earlier window and a session that started in a past window contributes nothing to the current one.

Fix: `cost-state` entries are cumulative per session, so successive entries give per-interval deltas. Book each delta to the window of its own timestamp. A session's first entry books whole to its timestamp's window. Test: a session spanning two windows splits its USD by the same proportion as its weighted tokens, within the coarseness of the entries. The tile keeps its basis line and gains "N sessions, M crossing a window boundary".

### Narrative

Three defects: it silently prints a grey line when the `claude` CLI is missing, its output shows mojibake from a cp1252 decode on Windows, and it truncates mid-sentence.

Fix: decode the subprocess output as UTF-8 with `errors="replace"` and set `PYTHONIOENCODING` and `--output-format text`. Cap the prompt at three sentences and refuse the result if it exceeds four sentences or 90 words, then retry once with a shorter cap. When the CLI is missing, the heading is not rendered; a one-line notice sits in the header meta: "narrative off: `claude` not on PATH for the scheduled run". The narrative prompt receives the delta decomposition and the anomaly lane from Sections 2 and 3, not the raw totals, so it explains the change rather than restating the ledger.

### Context series footer

The context-bloat chart says 5,233 turns and plots about 300 points. The footer states "5,233 turns binned to 300 points, each point the max of its bin".

### Whale rows

Three rows at 6,166,060 weighted seconds apart from one task notification read as triple counting. Whale turns that share a session, a prompt head and a cost within 1% collapse into one row "3 near-identical turns, 6.17M each" with the count. The whale chart itself is removed from the findings (Section 4); the collapsed table stays in raw breakdowns.

## Section 2: what changed, at the top

The week-over-week decomposition moves from a collapsed raw breakdown to the second block on the page, directly under the Verdict tiles. It is the only section that answers "why is this week different" and it reconciles to the total.

Rendering: one diverging bar chart of the delta by repo × lane (main vs subagent), colored by cause from the existing disjoint `CAUSE_PRECEDENCE`, largest absolute delta first, at most 8 bars plus "everything else" with its count. The accounting line stays: "+800.8M accounted, +800.8M total". A one-sentence claim above the chart names the top cause and its share of the swing, generated from the data, not from the narrative.

For the first window on record, or when the previous window has fewer than 100 turns, the block says "no previous window to compare" and takes one line.

## Section 3: anomalies, ranked by strangeness

A new lane between "What changed" and the findings. It answers "what looks wrong", separated from "what costs a lot". Each anomaly is one card: a claim with its measured numbers, the chart or table that shows it, and the concrete action. Sorted by a strangeness score, not by weighted cost, so a cheap loop outranks an expensive but normal storm.

Detectors, each in `rules.py` with its own threshold in config under `anomalies`:

- **Skill loaded where nothing reads the reply.** A skill whose description targets the user's chat reply (configured list `anomalies.reply_skills`, default `prose:reply-style`, `prose:bro`) attributed to turns in sessions whose `entrypoint` or `cwd` marks them as headless (the scheduled review loop is detected by `cwd` under a configured `anomalies.headless_cwds` list, plus any session whose first user entry has `promptSource: scheduled` where present). Action: name the skill file and the project whose settings can exclude it. Score: share of the skill's spend inside headless sessions.
- **Identical tool input repeated in a short span.** Same tool + input hash more than `anomalies.repeat_min` (default 20) times within `anomalies.repeat_minutes` (default 15) in one run. Action: name the run by its dispatch description and the tool. Score: repeats per minute. The existing `loop_retry` rule stays for the cost figure; this detector is the behaviour lens.
- **Tool result size dominating context growth.** One tool accounts for more than `anomalies.growth_share` (default 60%) of a session's context growth and the median result exceeds `anomalies.result_kb` (default 8 KB). Action: name the tool and the median size; for Bash, suggest the orchestrator role's output limits. Score: share × median size.
- **Failed calls concentrated in one tool.** More than `anomalies.fail_min` (default 50) failures in a window where one tool holds over 70% of them. Action: name the tool. Score: failure count.
- **Unattributed subagent share above threshold.** More than `anomalies.unattributed_share` (default 50%) of subagent spend has no agent type. Action: "these runs were dispatched without `subagent_type`, or by a plugin whose agents are not on disk". Score: the share.
- **MCP server above a share of the window.** One server above `anomalies.mcp_share` (default 4%). Action: name the server and its turn count. Score: the share.

At most 5 cards render; the rest are counted in a footer. A window with no anomaly prints one line: "nothing outside the usual pattern this week". Each card carries the coverage of the field it depends on, as every ranking does today.

## Section 4: cut the page

Removed from the rendered page entirely:

- The effort tiers ranking.
- The models, plugins and repos rankings from the cost-centre lanes. Models stays in raw breakdowns; plugins duplicate skills; repos duplicate the delta chart.
- The whale-turns finding card and chart. The rule stays for the collapsed table.
- The `split_whale_turns` recommendation kind.
- The headline tiles inside raw breakdowns.
- The Recommendations section that restates the "Do these first" cards. The group view survives only for headroom, when it fires.

The visible word cap drops from 1,500 to 1,100. A real window must render under it with a full narrative.

## Section 5: recommendations become deltas

"Do these first" stops being a fixed habit list. It has two parts:

**Last week's advice.** For each recommendation that appeared on the previous window's page, one row: the title, its figure then, its figure now, and the movement in percentage points of the window. The rows are read from the previous window's stored aggregate, not from the HTML. When the movement is within ±2 points, the row says "unchanged". Nothing on this list claims the user followed the advice; it shows whether the measured pattern moved.

**New this week.** At most two cards, only for recommendations that did not appear on the previous page or whose figure grew by more than 5 points of the window. When nothing qualifies, the block says so in one line.

Recommendations stay priced and caveated as today. The three fixed habits still exist as recommendation kinds; they simply stop rendering when they are unchanged from last week.

## Section 6: tune asks instead of refusing

`tune` today prints 28 "cost without a proposal" entries and one 0.02% setting proposal. The one real lever is hidden in a refusal: general-purpose is not on the `sonnet_class_agents` whitelist, so its tier is never judged.

Changes:

- A new first section, **Decisions this data needs from you**: every agent type above `min_cost` that is on neither `sonnet_class_agents` nor `opus_class_agents`, with its typical window cost, run count, median thinking per turn and median output per turn, and the exact `config.json` line to add it to either list. One line per agent, sorted by cost. The section states: "classify these and the next run prices them".
- Setting-level proposals below `min_saving` share of the window (default 1%) are folded into one line with a count instead of a full card.
- "Cost without a proposal" shrinks to skills and agents above 3% of a window; the rest is a count.
- A **What moved since last run** section mirrors Section 5: for each proposal from the previous `tune` run (stored in `data/tune_last.json`, written by tune as the only file it writes, in the data home, never a config or agent file; the no-write tests are extended to allow exactly this path), the figure then and now. The no-write invariant becomes "tune writes nothing outside `data/tune_last.json`".
- The skill entries drop the file-size line; a byte count is not evidence of anything.

## Section 7: quota stability

The fitted quota switched on mid-review and moved the heaviest week from 121% to 52%. That is the feature working, but a reader sees the same week under two denominators.

- Once `quota-fit` has produced a ceiling, the report stamps every page with the ceiling and method used and the Verdict says "ceiling changed since this page was last rendered: 1.4B estimated to 3.3B fitted" for one render after a change.
- The `top-cluster` estimate is labelled "estimate, likely low" whenever a quota sample exists at all, since a single sample already bounds the real ceiling from below: spent ÷ utilization is a floor.
- `percent_used` on closed windows uses the fitted ceiling retroactively, never the estimate, once the fit exists. The page says so in the tile subtitle.

## Data flow

`collect` is unchanged except for USD delta booking and the new anomaly detectors running at aggregation. The window aggregate gains `anomalies`, `delta` (already computed, now stored), and `recommendations_previous` resolved at render time from the previous window file. `report` renders in the new order: Verdict, What changed, Anomalies, Findings (without whales), Cost centres (jobs, MCP servers, skills), Do these first (last week's advice, new this week), Raw breakdowns. `tune` reads and writes `data/tune_last.json`.

## Testing

Unit tests per section: USD split across a boundary; narrative decode, length refusal and missing-CLI header line; series footer text; whale row collapse; delta chart ordering and the "no previous window" line; every anomaly detector's fire and no-fire boundaries and its action text naming a file, tool or run; the removed sections absent from the page; the 1,100-word cap on the real-shaped fixture; the advice comparison with an unchanged, a grown and a new recommendation; tune's decisions section listing exactly the unclassified agents; tune's single write path; the ceiling-change notice rendering once.

Acceptance on real data: a copy of the data home, `collect` then `report --all`, then a read of week_2026_09_05 answering the critic's three questions: is the top of the page the delta, does the anomaly lane surface the headless skill, the Bash growth and the 101-repeat loop, and does `tune` open with the classification ask.

## Out of scope

- A real billing figure. USD remains the list-price estimate.
- Measuring whether a tier change changed answer quality.
- Any write by `tune` outside its own state file.
