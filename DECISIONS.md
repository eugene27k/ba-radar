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
