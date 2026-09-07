# BA Radar — Stage 2 brief

Relevance scoring, the per-item BA verdict, priority tiers, and a provider-agnostic
LLM layer (Anthropic by default, OpenAI optional, chosen in config).

This brief is written for a fresh Claude Code session that has no memory of how
Increment 1 was built. Read it fully before touching code. Talk to the user in
Ukrainian; keep code, comments, commit messages and repository docs in English, as the
repository already does.

---

## 0. Where things stand

Increment 1 (Stage 1) is **deployed and working**:

- Repository: `eugene27k/ba-radar` on GitHub, default branch `main`, CI green.
- GitHub Actions run the schedule (`.github/workflows/`): weekday digest at 08:00 Kyiv,
  catch-up at 11:00, weekend collection. State (`state/ba_radar.sqlite`) is committed
  back by the workflows themselves.
- Delivery goes to the public Telegram channel `@AIforBARadar` via the bot
  `@AIforBA_Radar_bot`. Secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` exist.
- 106 tests, `ruff`, `ruff format` and `mypy --strict` are clean. Keep them that way.
- The digest is currently a flat list grouped by source. There is **no model call
  anywhere** yet. `src/ba_radar/llm/` and `src/ba_radar/pipeline/` do not exist
  (DECISIONS.md D-13); the `llm` and `scoring` blocks in `config/settings.yaml` exist
  but nothing reads them.

Read, in this order, before planning:

1. `README.md` — product thesis, commands, crash safety, layout.
2. `DECISIONS.md` — every divergence from the PRD so far. D-04, D-05, D-08, D-11,
   D-13, D-15, D-16 and D-17 bear directly on this stage.
3. `config/settings.yaml` — the `scoring`, `digest` and `llm` blocks are the shape
   Stage 2 must fill.
4. `src/ba_radar/models.py` — `Item` already carries `relevance_score`, `tags`,
   `priority`, `suggested_priority`, `summary`, `ba_insight`, `action`,
   `verbatim_flag`, and the statuses `FILTERED` / `ANALYZED`. `PracticeTag`,
   `Priority`, `Action` enums exist. Do not invent parallel fields.
5. `src/ba_radar/tasks.py`, `src/ba_radar/store/repo.py` (`select_for_digest` is the
   documented replacement point), `src/ba_radar/render/digest.py`,
   `src/ba_radar/labels.py`, `tests/test_flow.py`.

**The PRD and `ba-radar-answers.md` are referenced throughout but are not in the
repository.** First thing in the session: ask the user to paste them or point to them.
If they cannot be found, work from this brief plus the requirement numbers quoted in
DECISIONS.md, and record every assumption you make as a DECISIONS.md entry.

---

## 1. Goal

Turn the flat list into the product the README promises: for every delivered item, a
verdict a business analyst can act on — *does this change my artifacts, my process, or
my tooling, and how?* — with a priority tier, a one-line summary, practice tags and a
recommended action, rendered as Critical / Notable / Background blocks under the 3/5/7
caps. Aggregation stays; judgement is added.

Second goal: the model layer is provider-agnostic. The user picks the provider and
model per task in `settings.yaml`; Anthropic is the default, OpenAI is the optional
alternative. Switching providers must be a config edit plus a secret, never a code
change.

---

## 2. Hard constraints (do not trade these away)

1. **Excerpts are never persisted** (answers doc §0; `RawItem` docstring). They exist
   in memory only during `collect`. Therefore scoring and analysis **run inside
   `collect`**, while `RawItem.excerpt` is available. Model *outputs* (score, tags,
   summary, insight, action, suggested priority) are persisted — the columns already
   exist. Do not add an excerpt column and do not write source text to the database.
2. **Delivery invariants stay exactly as they are**: the prepare/send split, one digest
   per local day keyed on `telegram_confirmed_at`, the pending-batch resend window
   (D-15), idempotent `prepare-digest` and `send-digest`. Stage 2 changes *which*
   items are selected and *how* they render, nothing about when or how often.
3. **Tests never touch the network.** `respx` mocks `httpx` only, and the Anthropic
   SDK 1.x runs on `httpx2`, so provider tests use a fake provider that implements the
   protocol, plus recorded raw responses as fixtures for the adapter tests. No live
   calls in CI. Never pass the collectors' `httpx` client into an SDK client.
4. **English keys everywhere; Ukrainian only at render time** via `labels.py`
   (answers doc Q19). Model output uses the English enum values too.
5. **Quality first, cost second.** At this volume (10–20 items a day) the difference
   between Haiku and Sonnet/Opus for the verdict is a few dollars a month. Never pick
   the model for price without asking the user. Do not build prompt-caching plumbing
   (D-11 explains why it cannot help across runs) unless you measure a benefit.
6. **Secrets only from the environment**: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`. The
   three state-writing workflows must pass them to the `collect` step `env`, exactly
   as `GITHUB_TOKEN` is passed today (D-17). Never log a key; redact it from error
   text the way `delivery/telegram.py` redacts the bot token.
7. **Engineering bar**: mypy strict, ruff, 100-column lines, acceptance behaviour
   expressed as flow tests, `DECISIONS.md` append-only entries for every divergence
   or assumption, README kept truthful (its *Status* paragraph must say Stage 2 when
   you are done). Work on a branch `feat/stage2`, open a PR to `main`, CI must pass.
   Local runs mutate `state/ba_radar.sqlite`; restore it with
   `git checkout -- state/ba_radar.sqlite` before committing.

---

## 3. Scope

### A. `src/ba_radar/llm/` — the provider layer

- `LLMRequest`: task name, system prompt, user content, JSON schema for the structured
  reply, `max_tokens`, model; `LLMResponse`: the parsed and pydantic-validated object,
  token usage in and out, model and provider names.
- `Provider` protocol with `async def complete(request) -> LLMResponse`.
- `anthropic.py`: the official `anthropic` SDK, `AsyncAnthropic`, structured output via
  the current API shape. **Load the `/claude-api` skill before writing this file** and
  follow it for model IDs (`claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5`),
  structured outputs, thinking configuration per model, and what the 4.6+ models
  reject (assistant prefill, sampling parameters, `budget_tokens`). Do not send
  `temperature`. Leave SDK retries at their defaults; set an explicit timeout.
- `openai.py`: the official `openai` SDK, `AsyncOpenAI`, structured output with the
  same JSON schema. **Read OpenAI's current documentation before writing it** and do
  not guess model names: the model is a config value, and `settings.yaml` ships a
  commented example the user fills in. Nothing in this repository has been verified
  against the OpenAI API yet.
- Both adapters translate one provider-neutral prompt + schema; a test feeds each
  adapter a recorded raw response and asserts they parse to the same object.
- Selection: `llm.provider` (global default) and optional per-task overrides
  `llm.tasks.<task>.provider` / `.model`. Only the selected provider's key is required;
  fail fast with a clear message at the start of `collect` if it is missing. Install
  both SDKs as regular dependencies — optional extras would make the workflow's
  `uv sync --frozen` depend on which provider is configured.
- Prompts are versioned files: `src/ba_radar/llm/prompts/relevance_v1.md`,
  `analysis_v1.md`. The version in use is written to the run log.
- A `ba-radar llm-check` command: one tiny call through the configured provider,
  prints provider, model, and token usage. This is how the user verifies a key.
- Every run logs an `info` entry (add the level to `RunLogEntry`): provider, model,
  prompt version, items scored / analyzed / filtered, tokens in and out, estimated
  cost. `ba-radar status` should surface it.
- A phase deadline (`llm.phase_deadline_seconds`) so a slow or hanging provider cannot
  blow the 10-minute cycle NFR: past the deadline, remaining items stay unscored and
  the run is DEGRADED.

### B. `src/ba_radar/pipeline/` — scoring, analysis, prioritisation

- **Relevance scoring** (`score.py`): batched requests of `llm.tasks.relevance.
  batch_size` items — prompt batching, not the Batches API (answers doc Q1). Per item
  in: title, source name and category, indicator, published date, excerpt. Per item
  out: `relevance` 0–100, `tags` (a subset of `PracticeTag`), a one-line rationale
  (log only, not persisted). The rubric lives in the prompt file and encodes the
  product thesis: what matters to a BA working in AI-native development
  (specifications, context engineering, agent orchestration, evals, tool releases,
  role change, evidence and critique, security and quality). Draft it with the user —
  see the questions in §5 — and mark it for Stage 5 calibration.
- **Adjusted score** (answers doc Q7; PRD 2.1.5–2.1.7, 1.2.5, Q18):
  `base + max_source_weight (D-04) + indicator_bonus + multi_source_bonus (if
  source_count ≥ multi_source_min) − no_tag_penalty (if no tags)`, all from
  `settings.scoring`. Below `threshold` → status `FILTERED`: kept for dedup, never
  delivered, pruned normally.
- **Where the adjustment is computed — decide and record.** Recommended: persist the
  model's *base* score and tags at collect time; compute the adjusted score, the band
  and the tier at `prepare-digest` time. Merges that arrive after scoring then still
  raise `source_count` and the bonus, and a merged item is never re-scored (its
  excerpt is gone anyway). Record the choice as a DECISIONS.md entry.
- **Analysis** (`analyze.py`): for items at or above the threshold, best adjusted
  score first, at most `scoring.analysis_cap` per run. Output per item: `summary`
  (Ukrainian, at most two sentences, a paraphrase — never a quote), `ba_insight`
  (Ukrainian, one or two sentences answering the thesis question concretely),
  `action` (`try` / `read` / `note`), `suggested_priority` (advisory only, answers
  doc Q8), and `verbatim_flag`. The **final `priority` is rule-derived** from
  `scoring.bands` (critical ≥ 75, notable ≥ 55, background ≥ 40); the model's
  suggestion is stored for calibration only.
- `verbatim_flag` (PRD §8, copyright): check the PRD's wording. If it is absent,
  implement and record: flag the item when the summary shares a contiguous run of
  twelve or more words with the excerpt, and render a flagged item with its title and
  insight only.
- **Statuses**: `COLLECTED → FILTERED` (below threshold) or `ANALYZED` (scored and
  analysed) `→ DELIVERED`. `select_for_digest` selects `ANALYZED` items by tier under
  the caps (`digest.caps`, PRD 2.3.2–2.3.4), highest adjusted score first inside a
  tier. Unselected `ANALYZED` items roll over to the next day; decide on a maximum
  age for rollover (72 hours is consistent with `max_lookback_hours`) and record it.
- **Provider failure must not lose a day.** If scoring fails for a run (no key,
  provider down, deadline), the run is DEGRADED, the items stay `COLLECTED` with a
  NULL score, and the digest renders them in a final «Без аналізу» block capped by
  a renamed `stage1_max_items` (call it `digest.unscored_max_items`). The failure is
  visible in the channel and nothing is silently dropped. A better design is welcome;
  record whichever you ship.

### C. Rendering (PRD 3.1.4, 3.1.5, §5.3)

- Blocks in tier order with Ukrainian headings from `labels.PRIORITY_UK`. Per item:
  linked title, date, source label, summary, the insight prefixed «BA:», the action
  label from `ACTION_UK`, tags from `PRACTICE_TAG_UK`.
- Header keeps the date and the processed count; define "processed" for Stage 2
  (items scored since the previous digest is the natural reading) and make
  `count_undelivered` stop counting `FILTERED` rows.
- The 4096-character splitting carries over unchanged: never split inside an item,
  repeat the block heading on a continuation, keep the linkless fallback for an
  oversized line. The empty-day message is unchanged (req. 3.1.9).
- Ask the user whether the header should read «AIforBA Radar» to match the channel.

### D. Configuration and secrets

- Extend `llm` in `settings.yaml`: `provider`, `phase_deadline_seconds`, per-task
  `provider` / `model` / `max_tokens` / `batch_size` / `effort`, plus a commented
  OpenAI example. Keep `scoring` as is unless the PRD says otherwise.
- `Secrets` gains `openai_api_key`; `ANTHROPIC_API_KEY` already exists as an optional
  field. The provider layer reads keys through `Secrets`, never `os.environ` directly.
- Workflows: add both keys to the `collect` step `env` in `digest.yml`, `catchup.yml`
  and `collect.yml`. README gets a "Setting up the model provider" section mirroring
  the Telegram one, including the `gh secret set` commands.

### E. Tests (extend, do not replace)

- Fake provider implementing the protocol, scriptable per task, counting calls.
- Adapter tests from recorded raw responses (one per provider), no network.
- Unit tests: adjusted-score arithmetic and every bonus/penalty branch; bands; caps;
  rollover age; `FILTERED` never selected; unscored items land in the fallback block.
- Flow tests with the fake provider: collect → score → analyse → prepare → send;
  the degraded path when the provider raises or times out; provider selection from
  config; a missing key fails fast before any state is touched.
- Rendering tests for the tiered layout and the split invariants.
- Run `uv run ba-radar run --dry-run` with a real key once before opening the PR —
  note that Stage 2 makes dry-run spend a few cents, because scoring happens in
  `collect`. Restore the state file afterwards.

### F. Out of scope for this stage

- The weekly digest (`RunKind.WEEKLY`, `llm.tasks.weekly`) unless the PRD's stage
  plan (§11) puts it in Stage 2 — check and confirm with the user.
- `html_diff` and `github_commits_path` collectors (Stage 3).
- Calibration tooling and score analysis (Stage 5). Persisting `llm_provider`,
  `llm_model` and `prompt_version` per item in a small migration is cheap and helps
  Stage 5; do it if it costs less than an hour.
- Registry expansion: the README promises ~50 sources and the registry has 14. That
  is a content task for the user, not this stage — but mention it.

---

## 4. Suggested order of work

1. Ask for the PRD and answers doc; ask the §5 questions; propose a short plan and
   get a yes before writing code.
2. Provider layer with the fake provider and adapter tests. `llm-check` command.
3. Scoring + adjusted score + statuses, tests.
4. Analysis + rule-derived priority, tests.
5. Selection under caps + rendering, tests. Update `preview` and `--dry-run`.
6. Config, secrets, workflows, README, DECISIONS.md entries.
7. Full local verification; PR; after merge, a forced `digest.yml` dispatch to see a
   real Stage 2 digest in the channel.

Small commits with clear messages. End each with the same trailer the repository's
recent commits use.

---

## 5. Questions to ask the user before writing prompts

1. Which provider and model for `relevance` and for `analysis`? Default proposal:
   Anthropic, `claude-sonnet-5` for analysis (the verdict is the product), Haiku or
   Sonnet for relevance. OpenAI model names come from the user.
2. Three examples each of what they would call *critical*, *notable* and
   *background* from the last two weeks of the channel — this becomes the rubric's
   calibration examples.
3. Which practice tags matter most to their day-to-day work, and are any missing from
   `PracticeTag`?
4. Header text: «BA Radar» or «AIforBA Radar»?
5. Should the weekly digest be in this stage (only if the PRD says so)?
