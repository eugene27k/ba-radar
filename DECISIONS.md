# Divergence log

Every place where the code differs from the PRD or from `ba-radar-answers.md`, and why.
Requested in the answers doc: *"I'd rather have an honest divergence log than a PRD that
quietly stops describing the system."*

Entries are append-only. When a decision is folded back into the PRD, mark it FOLDED
rather than deleting it.

---

## D-01 — Algolia cannot express `OR`, so `query` accepts a list

**Status:** open — needs folding into PRD §6
**Source:** PRD §6 example; answers doc Q11

The PRD's example source uses `query: "Claude Code OR spec-driven OR MCP"`. Algolia's
`query` parameter is a **full-text search string, not a boolean expression**, so it
searches for the literal word "OR". Measured on 2026-08-03: that query returns **0
hits** at any threshold over any window, while `"Claude Code"` alone returns 42 and
`"MCP"` returns 106. Shipped as written, the Hacker News source would have been
permanently silent and nothing would have indicated a fault.

**Change:** `query` accepts a string *or* a list of strings. The collector issues one
search per term and merges on `objectID`. A story matching several terms ranks higher
(sorted by points before the per-source cap applies).

Also: bare terms match loosely — `MCP` returned stories about book sales and manifestos.
The registry uses quoted phrases. Measured yield at `min_points: 50`: `"Claude Code"`
~3/week, `"MCP"` ~3/week, `"agentic"` ~3/week, `"spec-driven"` 0.

---

## D-02 — Two mandatory §6 sources cannot use the method implied for them

**Status:** open — needs folding into PRD §6
**Source:** PRD §6 mandatory source list

Verified on 2026-08-03:

- **Anthropic Engineering** publishes no RSS/Atom feed. `/rss.xml`, `/news/rss.xml`,
  `/feed.xml` and `/engineering/rss.xml` all return 404, and the site is not a public
  repo. It therefore requires `html_diff`, which is Stage 3.
- **humanlayer/12-factor-agents** publishes **zero** GitHub releases, so
  `github_releases` would have returned an empty list forever. It is a content repo,
  which is what `github_commits_path` is for — also Stage 3.

Both are in `config/sources.yaml` with `active: false` and a comment explaining why, so
they are switched on by flipping one flag when Stage 3 lands rather than being
rediscovered later.

---

## D-03 — `RunStatus.RUNNING` is a fourth status

**Status:** open — needs folding into PRD req. 4.2.3
**Source:** PRD req. 4.2.3 (three statuses: Успішно / Успішно з помилками / Збій)

The three PRD statuses are all *terminal*. The prepare/send split (answers doc Q14)
needs a way to say "this batch was selected but its outcome is not yet known", and a
crashed run must be distinguishable from a completed one. `RUNNING` is the in-flight
marker; it never appears on a finished run.

---

## D-04 — `max_source_weight`: PRD 2.1.6 assumes one source per item

**Status:** open — needs folding into PRD req. 2.1.6
**Source:** PRD req. 2.1.6 («система підсумовує до оцінки вагу джерела»)

Req. 1.2.4 requires storing the *list* of sources an item appeared in, but req. 2.1.6
says to add "the source weight" — singular. With a merged multi-source item there is no
single weight. The item carries `max_source_weight`, the highest weight of any source it
was seen in, and Stage 2's scoring will read that.

Rationale: an item that appeared in a weight-10 source and a weight-2 source is as
important as its strongest sighting. Summing would double-count popularity, which is
already what the multi-source bonus is for.

---

## D-05 — What "prune to hash + URL" actually keeps

**Status:** open — needs folding into the answers doc
**Source:** answers doc §0 (retention: full rows 90 days → hash + URL forever)

Pruning nulls the payload and keeps the identity. Precisely:

| Kept forever | Dropped at 90 days |
|---|---|
| `id`, `url`, `published_at`, `collected_at` | `title`, `title_key`, `tags` |
| `status`, `relevance_score`, `priority` | `summary`, `ba_insight`, `suggested_priority` |
| | all `item_sources` rows |

Keeping `id` is what preserves dedup forever; keeping the scores costs ~8 bytes and
makes long-run calibration analysis possible during and after Stage 5. `title_key` is
dropped because the title-match window is only 14 days, so it is dead weight after that.

---

## D-06 — Weekday collection runs inside the digest workflow

**Status:** open — needs folding into the answers doc
**Source:** answers doc Q6 ("split collect from digest; collection runs 7 days a week")

Implemented as: `digest.yml` runs `collect` **and** the digest on weekdays;
`collect.yml` runs collection only on Saturday and Sunday. Collection still happens
seven days a week, which is the requirement.

Rationale: two separate weekday workflows would need a time gap between them, and
GitHub Actions delays of 5–30 minutes are routine — the digest could easily start before
collection finished. Sequencing them in one job removes the race entirely.

---

## D-07 — Any unconfirmed run is the pending batch, not just a `RUNNING` one

**Status:** open — clarifies answers doc Q14
**Source:** answers doc Q14

First implemented as "a `RUNNING` digest run today is the prepared batch". A test caught
that this breaks the recovery path it was designed for: a **failed** send sets the run
to `FAILED`, so the catch-up would not recognise it, would create a fresh run, would find
nothing selectable (the items are already marked against the failed run), and the batch
would be orphaned permanently — exactly the outcome Q14 exists to prevent.

The condition is now `telegram_confirmed_at IS NULL`, which covers both "crashed before
send" and "send failed". Covered by
`test_a_failed_send_leaves_a_batch_the_catch_up_can_resend`.

---

## D-08 — Increment 1 has no priority tiers

**Status:** expected — resolved by Stage 2, no PRD change needed

There is no scoring yet, so the 3/5/7 caps in req. 2.3.2–2.3.4 have nothing to sort on.
Increment 1 renders a flat list grouped by source, capped by
`digest.stage1_max_items` (30). The caps are already in `settings.yaml` and unused.

Consequently these requirements are **not** met yet, by design: 2.1, 2.2, 2.3, 3.1.4,
3.1.5, 3.2, and the priority/action/summary fields of §5.3.

---

## D-09 — Retry count is 1 initial attempt + 3 retries

**Status:** open — clarifies PRD req. 1.1.9
**Source:** PRD req. 1.1.9 («не більше 3 повторних спроб... з інтервалом 5 секунд»)

Read as three *retries* after the first attempt, so four requests maximum per source.
A 4xx other than 429 is not retried at all — a 404 feed will still be 404 in five
seconds, and the retry budget is better spent on the sources that might recover.

Additionally, a per-source deadline (`per_source_deadline_seconds`, default 60) caps the
total. Without it, ~50 sources × 4 attempts × (15s timeout + 5s backoff) is unbounded
enough to blow the 10-minute cycle NFR on a bad network day.

---

## D-10 — Hacker News items link to the article, not the discussion

**Status:** open — needs folding into PRD §6
**Source:** not specified in the PRD

An HN story has two URLs: the article it points at, and the HN discussion. The item's
canonical URL is the **article** where one exists, so the same piece arriving via HN and
via RSS collapses into one item — which is the whole point of the dedup design, and what
feeds the multi-source bonus. Text posts (Ask HN / Show HN with no link) use the HN
permalink. The discussion URL is preserved in the excerpt either way.

---

## D-11 — Prompt caching is not worth budgeting for

**Status:** open — needs folding into PRD §9
**Source:** PRD §9 («кешування підказок зменшує вартість вхідних токенів до 90 відсотків»)

Confirmed against current API documentation. Two independent reasons the §9 assumption
does not hold:

1. Haiku 4.5's **minimum cacheable prefix is 4096 tokens**. A relevance rubric will not
   reach that unless deliberately padded.
2. Cache TTL is 5 minutes (1 hour optional), and runs are 24 hours apart, so nothing
   survives between runs.

Caching can only help *within* a single run, across the ~10 batched scoring requests.
The answers doc already accepted removing the assumption from §9; recorded here so the
reasoning is not lost.

---

## D-12 — `ruff` RUF001–003 are disabled

**Status:** expected — no PRD impact

Those rules flag Cyrillic characters and typographic punctuation as "ambiguous unicode".
The digest is written in Ukrainian and the title normaliser has to match real curly
quotes and en/em dashes, so the rules fire on correct code throughout.

---

## D-14 — Rate-limit errors are named, not left as "HTTP 403"

**Status:** addition — no PRD conflict

Found while running the acceptance checks: six GitHub sources failed with a bare
`HTTP 403`, which is not diagnosable from a run log. The cause was the unauthenticated
GitHub quota (60 requests/hour) being exhausted by local testing.

Two additions:

- A 403/429 carrying `x-ratelimit-remaining: 0` is reported as
  `rate limit exhausted (60/hour), resets at HH:MM UTC — set GITHUB_TOKEN`.
- `collect` emits a warning when GitHub-backed sources are active and `GITHUB_TOKEN` is
  unset. Actions always provides that token, so this only fires locally — which is
  exactly where the mystery 403 would otherwise appear.

The retry policy is unchanged: an exhausted quota resets in minutes to an hour, so
retrying inside the same run is pointless.

---

## D-13 — `llm/` and `pipeline/` directories not created yet

**Status:** expected — Stage 2

The approved structure includes `src/ba_radar/llm/` and `src/ba_radar/pipeline/`. They
are not created in Increment 1 because there is nothing to put in them yet, and empty
packages are noise. The model-routing config they will read (`llm.tasks` in
`settings.yaml`) already exists so the shape is fixed.

---

## D-15 — A pending batch is resent for up to 3 days, not only same-day

**Status:** open — extends D-07 / answers doc Q14
**Source:** answers doc Q14

D-07 made "any unconfirmed run today" the pending batch. That still orphaned the batch
at midnight: `prepare` only looked at today's runs, so after a day on which both sends
failed (an expired bot token is the realistic case), the next day selected a fresh
batch and yesterday's items — already marked delivered — were never sent and never
reselectable.

Two changes:

- The pending-batch lookup goes back `digest.pending_resend_days` (default 3) local
  days as well as today. Three days covers a weekend plus a working day to fix the
  token.
- "Already delivered today" is keyed on `telegram_confirmed_at`, not `started_at`:
  Friday's batch delivered by Monday's retry counts as Monday's digest, so Monday's
  catch-up does not send a second one.

Batches older than the window are left alone deliberately — resending week-old news
would displace the day's actual signal. Their items stay marked delivered, which keeps
"never a duplicate" intact; the cost is bounded because every failed send is a non-zero
exit and therefore a GitHub failure email, every day of the outage.

---

## D-16 — The incremental window starts behind the cursor

**Status:** open — amends the literal reading of PRD req. 1.1.4
**Source:** PRD req. 1.1.4 («новіші за час останнього успішного збору»)

The cursor is the newest `published_at` seen. Read literally, "only newer items" means
an entry added to a feed late — backdated, cross-posted, or delivered out of order —
is skipped on every subsequent run and never collected at all.

`_compute_since` therefore starts the window `collection.cursor_overlap_hours`
(default 24) behind the cursor. Re-reading the overlap is free: identity dedup makes a
second sighting of the same canonical URL a no-op. To keep the run counts honest under
routine re-reads, the dedup result distinguishes `unchanged` (the same source seeing
the same item again — not counted) from `merged` (a genuinely new source sighting).
The overlap never triggers the Q15 "window skipped" warning; that fires only when the
cursor itself is older than `max_lookback_hours`.

---

## D-17 — `GITHUB_TOKEN` is passed to the collect step explicitly

**Status:** correction of D-14 — no PRD impact

D-14 assumed "Actions always provides that token". It does, but only as
`secrets.GITHUB_TOKEN` / `github.token` inside workflow expressions — it is **not**
exported into the environment of `run` steps. As written, every scheduled `collect`
ran unauthenticated against the GitHub API: 60 requests an hour per IP, shared with
every other job on the runner's address pool, so the six GitHub-backed sources would
have failed with 403 from the first scheduled run and left it DEGRADED.

The three state-writing workflows now set `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`
on their collect step. That token is rate-limited at 1,000 requests an hour per
repository, and reading public releases needs no permission beyond it. D-14's warning
is unchanged and now fires only locally, as intended.

---

## D-18 — Stage 2 was built from the brief; the PRD and answers doc were unavailable

**Status:** open — every entry below that cites "assumption" needs checking against
the PRD when it is back in reach
**Source:** `docs/STAGE2-BRIEF.md` §0

Neither the PRD nor `ba-radar-answers.md` was available in the session that built
Stage 2 (asked for, not found). The brief plus the requirement numbers already quoted
in this log are the spec. Where the brief said "check the PRD's wording", the
assumption is recorded as its own entry (D-22, D-23, D-24, D-30, D-31). The user
accepted the proposed defaults for the §5 questions wholesale ("apply all").

---

## D-19 — Base score persisted at collect time, adjustment recomputed at prepare time

**Status:** open — needs folding into the answers doc Q7
**Source:** answers doc Q7; PRD 2.1.5–2.1.7, 1.2.5

`relevance_score` holds the model's *base* score for the content alone. The adjusted
score (base + max source weight + indicator bonus + multi-source bonus − no-tag
penalty) is computed twice:

- at collect time, to decide FILTERED vs. analysis, because analysis needs the
  excerpt and the excerpt exists only during that run;
- again at `prepare-digest` time, from the *current* `source_count` and
  `max_source_weight`, to rank and tier — so a cross-source merge that arrives after
  scoring still earns the multi-source bonus. Nothing is ever re-scored.

The value used for a digest is persisted on the item (`adjusted_score`, `priority`) at
selection time, so a resend renders the same tiers (D-15 path) and Stage 5 can see
what the reader actually got.

---

## D-20 — Missing key fails fast; runtime failure degrades and self-heals

**Status:** open — reconciles two clauses of the brief
**Source:** brief §2.6, §3.A, §3.B "provider failure must not lose a day"

Two different failures, two behaviours:

- **No key for the configured provider** is a configuration error. `collect` (and
  `run`, including `--dry-run`) refuses before the database is opened. Collecting
  anyway would leave every item permanently unscored, because the excerpts are gone
  when the run ends; refusing keeps the cursors where they are, and the next run
  with a key collects everything up to `max_lookback_hours`. The failure is a
  non-zero exit, i.e. a GitHub failure email.
- **Provider down, rate-limited, refusing, or past the phase deadline** at runtime is
  a DEGRADED run. Affected items stay `COLLECTED` with a NULL score and are delivered
  in a final «Без аналізу» block (D-21) so the outage is visible in the channel.

Self-healing: the cursor overlap (D-16) re-reads a slice of every feed on every run,
so an unscored row that a feed shows again gets its excerpt back in memory and is
scored then. `DedupeResult.row_for` maps each sighting to its row for this purpose.
Rows outside the re-read window stay unscored until pruned — never silently dropped
from a digest, because the fallback block delivers them first.

---

## D-21 — Rollover age, the unscored block, and what happened to `stage1_max_items`

**Status:** open — assumption
**Source:** brief §3.B, §3.C

- Analysed but unselected items roll over to later digests for
  `digest.rollover_hours` (72, consistent with `max_lookback_hours`), measured from
  `collected_at` — the moment the item entered the pool.
- `digest.stage1_max_items` is renamed `digest.unscored_max_items` and caps the
  «Без аналізу» block. Unscored items are ordered scored-but-unanalysed first (by
  adjusted score), then by source weight and date, under the same rollover age.
- An item scored above the threshold but left without a verdict (analysis cap or
  deadline) stays `COLLECTED` with its score and goes to the unscored block too: no
  verdict, no tier. ANALYZED means "scored *and* analysed".

---

## D-22 — `verbatim_flag`: twelve consecutive words

**Status:** open — assumption, check PRD §8 wording
**Source:** PRD §8 (copyright); brief §3.B

The PRD's wording could not be checked. Implemented: the flag is set when the summary
shares a run of twelve or more consecutive words with the excerpt, compared after
case-folding and stripping punctuation. A flagged item renders with its title, source,
insight, action and tags — never the summary. The insight is always our own text.

---

## D-23 — "Processed" means items scored since the previous digest run started

**Status:** open — assumption, check PRD 3.1.3
**Source:** PRD 3.1.3; brief §3.C

The header's «Опрацьовано матеріалів» counts items whose `scored_at` is after the
start of the most recent digest run of any outcome (all scored items when there is
none). FILTERED items count — they were processed and rejected, which is the point of
the number. `count_undelivered` no longer counts FILTERED rows.

---

## D-24 — The header reads «AIforBA Radar», from config

**Status:** open — assumption
**Source:** brief §3.C

The channel is @AIforBARadar, so the header matches it. It is `digest.header_title` in
`settings.yaml` rather than a constant, so reverting to «BA Radar» is a one-line edit.

---

## D-25 — Priority is rule-derived from bands; the model's suggestion is advisory

**Status:** open — clarifies answers doc Q8
**Source:** answers doc Q8; `scoring.bands`

`priority` = critical ≥ 75, notable ≥ 55, else background, on the adjusted score,
for every item that passed the threshold. `suggested_priority` is stored untouched
for calibration and never influences the tier. If the threshold and the background
band ever disagree, a relevant item still lands in Background rather than in no
block at all.

---

## D-26 — Scored-but-unanalysed items are not ANALYZED

**Status:** open — see D-21
**Source:** brief §3.B statuses

Recorded separately because it is easy to get wrong: `ANALYZED` requires a verdict.
An item with a score and no verdict is a `COLLECTED` item with a score, and it is
delivered through the unscored block, not through a tier.

---

## D-27 — Analysis is one request per item with bounded concurrency; one deadline per phase

**Status:** open — implementation choice
**Source:** brief §3.A phase deadline; §3.B analysis

Scoring is batched (answers doc Q1). Analysis is not: a Ukrainian summary plus
insight per item makes a batch slow, and one malformed reply should cost one item, not
twenty. Requests run `llm.concurrency` at a time. `llm.phase_deadline_seconds` applies
to each phase separately (scoring, then analysis), and a request that would start
after the deadline is skipped rather than started.

---

## D-28 — What the Anthropic adapter does and does not send

**Status:** open — checked against the `/claude-api` skill on 2026-09-07
**Source:** brief §3.A

- No `temperature` / `top_p` / `top_k` (rejected by the 4.7+ family), no assistant
  prefill (rejected), no `budget_tokens` (rejected on Sonnet 5 / Opus 5).
- No `thinking` parameter at all: Sonnet 5 and Opus 5 run adaptive thinking by
  default, Haiku 4.5 runs without; omitting it is the one shape every current model
  accepts. Thinking tokens count against `max_tokens`, so the task budgets are
  generous (16000 for scoring, 8000 for analysis) — only generated tokens are billed.
- `effort` is forwarded inside `output_config` only when a task configures it; Haiku
  4.5 rejects it.
- Structured output via `output_config.format` (`json_schema`); the JSON schema is
  derived from the pydantic reply model and stripped of keywords neither provider's
  strict mode accepts (`minimum`, `maximum`, `minLength`, `pattern`, …); the
  pydantic model still enforces those on the parsed reply.
- SDK retries left at the default (2); explicit `llm.request_timeout_seconds`.
- Prompt caching is not used (D-11).

---

## D-29 — Adapter fixtures are synthesised, not recorded from live calls

**Status:** open — replace with real recordings after the first `llm-check`
**Source:** brief §3.E "recorded raw responses as fixtures"

No provider key was available in the build session. `tests/fixtures/anthropic_message.
json` and `openai_response.json` were constructed from the SDKs' own response models
(`anthropic.types.Message`, `openai.types.responses.Response`) and validated by them,
carrying the same relevance payload so the adapter test can assert both parse to the
same object. The OpenAI request shape was written from the published documentation
and **has not been exercised against the live API**; `llm-check` is the verification.

---

## D-30 — The weekly digest is not in Stage 2

**Status:** open — check PRD §11
**Source:** brief §3.F

Included only if PRD §11 put it in Stage 2, which could not be checked; the config
block `llm.tasks.weekly` stays so the routing shape is fixed.

---

## D-31 — Rubric calibration examples are the maintainer's, from the first digest

**Status:** open — replace in Stage 5
**Source:** brief §5.2

The channel had delivered exactly one digest (nine items) when the rubric was written
and the user did not classify them. `relevance_v1.md` therefore carries the
implementer's classification of those nine, marked provisional, as calibration
examples. Stage 5 replaces them with reader-confirmed examples; that is what the
prompt version pin is for.

---

## D-32 — Verdict provenance is persisted per item

**Status:** addition — no PRD conflict
**Source:** brief §3.F

Migration 002 adds `scored_at`, `adjusted_score`, `llm_provider`, `llm_model` and
`prompt_version` to `items`. `llm_provider` / `llm_model` record the *relevance*
route (the score is what Stage 5 calibrates); `prompt_version` records the relevance
prompt at scoring time and `relevance_vN+analysis_vM` once analysed. Pruning keeps
all five — they cost a few bytes and outlive the prose on purpose (D-05).

---

## D-33 — Cost estimates come from a pricing table in settings

**Status:** addition — no PRD conflict
**Source:** brief §3.A run log

`llm.pricing` maps a model id to USD per million input/output tokens; the run's
`info` log line multiplies it by the measured usage. Prices change and models get
added, so this is config, not code. A model without a row reports `n/a`.
