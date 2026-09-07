# BA Radar

Monitors ~50 sources about AI-native development, discards what a business analyst
doesn't need, and delivers a short digest to Telegram every working morning at 08:00
Kyiv time.

The point of the product is not aggregation — that is a commodity. It is the verdict
attached to each item: *does this change my artifacts, my process, or my tooling?* No
existing source writes that down, which is what the model is there to do.

**Status: Stage 2 of PRD §11.** Collection, deduplication and Telegram delivery
(Stage 1) plus relevance scoring, the per-item BA verdict and the Critical / Notable /
Background tiers under the 3/5/7 caps (Stage 2) work end to end. The model layer is
provider-agnostic: Anthropic by default, OpenAI as a config-selected alternative. Not
yet: the `html_diff` and `github_commits_path` collectors (Stage 3), the weekly digest,
calibration tooling (Stage 5). See [DECISIONS.md](DECISIONS.md) for what diverges from
the PRD and why.

---

## Quick start

```bash
uv sync
```

```bash
uv run ba-radar validate-sources
```

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
```

```bash
uv run ba-radar run --dry-run
```

`--dry-run` collects from every active source, scores and analyses what it found, and
prints the digest to stdout. It needs no Telegram credentials and sends nothing — but
since Stage 2 it does need the model provider's key and spends a few cents per run,
because scoring happens during collection (see *How scoring works*). Restore the state
file afterwards with `git checkout -- state/ba_radar.sqlite`.

---

## Setting up the Telegram bot

Needed once, before anything can actually be delivered.

1. **Create the bot.** Message [@BotFather](https://t.me/BotFather) in Telegram, send
   `/newbot`, and follow the prompts. It replies with a token that looks like
   `123456789:AAF...`. That is `TELEGRAM_BOT_TOKEN`.

2. **Start a chat with your new bot** and send it any message. A bot cannot message you
   first — without this step delivery fails with `chat not found`.

3. **Find the chat id.** With the token in hand:

   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
   ```

   Read `result[0].message.chat.id` out of the response. That is `TELEGRAM_CHAT_ID`. It
   is a number, negative for groups.

4. **Add both as repository secrets** under Settings → Secrets and variables → Actions:
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. They are never written to the repo.

Locally, export them instead:

```bash
export TELEGRAM_BOT_TOKEN='123456789:AAF...' TELEGRAM_CHAT_ID='987654321'
```

---

## Setting up the model provider

Scoring and analysis need one model provider. Anthropic is the default; OpenAI is the
alternative. Which one runs is decided in `config/settings.yaml` (`llm.provider`, with
optional per-task overrides under `llm.tasks.<task>.provider`), and only the selected
provider needs a key. Switching is a config edit plus a secret — never a code change.

1. **Get a key.** Anthropic: create one in the [Console](https://platform.claude.com/)
   and note it as `ANTHROPIC_API_KEY`. OpenAI: create one in the platform dashboard and
   note it as `OPENAI_API_KEY`.

2. **Add it as a repository secret.** With the [GitHub CLI](https://cli.github.com/)
   logged in:

   ```bash
   gh secret set ANTHROPIC_API_KEY
   ```

   ```bash
   gh secret set OPENAI_API_KEY
   ```

   Each command prompts for the value; nothing is echoed or committed. The workflows
   pass both names to the `collect` step, so an unset one is simply empty.

3. **Verify it.** Locally, export the key and make one tiny call through the configured
   provider — it prints the provider, the model and the token usage:

   ```bash
   uv run ba-radar llm-check
   ```

   `--task analysis` checks the other task's route when the two differ.

**Choosing models.** Both tasks run on `claude-sonnet-5` by default: the verdict is the
product, and the relevance filter is irreversible (an item scored below the threshold is
never re-scored, because its excerpt is gone). `claude-haiku-4-5` is the cheaper option
for `relevance`. For OpenAI, uncomment the example in `settings.yaml`, put in the model
name from OpenAI's model list, and run `llm-check` — nothing in this repository has been
verified against the OpenAI API yet.

If the key is missing, `collect` and `run` refuse before touching any state: items
collected without a provider could never be scored, and the failure would be silent
until 08:00.

---

## Commands

| Command | What it does |
|---|---|
| `collect` | Fetch every active source, normalise, dedupe, score, analyse, store |
| `preview` | Render the digest to stdout — touches neither the database nor Telegram |
| `llm-check` | One tiny call through the configured provider; prints model and token usage |
| `prepare-digest` | Select today's items and mark them delivered |
| `send-digest` | Send the prepared digest to Telegram |
| `run` | `collect` + `prepare` + `send`; `--dry-run` prints instead of sending |
| `status` | Recent runs, with error counts, delivery confirmation and the model usage line |
| `validate-sources` | Check `config/sources.yaml`; non-zero exit on any bad record |
| `prune` | Apply the retention policy and vacuum the database |
| `gate --hour N` | Decide whether a UTC-scheduled run is the right one for local hour N |

`prepare-digest` and `send-digest` are separate because the workflow commits state to
git **between** them — see *Crash safety* below.

---

## How scoring works

Everything model-related happens inside `collect`, because excerpts exist only in
memory during a run and are never written to the database.

1. **Relevance.** New items are scored in prompt batches (`llm.tasks.relevance.
   batch_size`): the model returns a 0–100 score for the content alone plus practice
   tags. The rubric is `src/ba_radar/llm/prompts/relevance_v1.md`.
2. **Adjustment.** Rules add the strongest source's weight, the indicator bonus and the
   multi-source bonus, and subtract the no-tag penalty (`scoring` in `settings.yaml`).
   Below `scoring.threshold` the item is `FILTERED`: kept for deduplication, never
   delivered.
3. **Analysis.** Items above the threshold, best first and at most `analysis_cap` per
   run, get the verdict: a two-sentence Ukrainian summary, the «BA:» insight, an action
   (`try` / `read` / `note`) and the model's advisory priority. A summary that copies
   twelve or more consecutive words from the source is flagged and rendered without
   the summary.
4. **Tiers.** At `prepare-digest` time the adjusted score is recomputed from the
   current source aggregates — a late cross-source merge still raises the bonus — and
   `scoring.bands` decides Critical / Notable / Background. Each tier is cut at
   `digest.caps`; unselected items roll over for up to `digest.rollover_hours`.

**If the provider fails** (down, rate-limited, or past `llm.phase_deadline_seconds`),
the run is DEGRADED, the affected items stay unscored, and the digest delivers them in
a final «Без аналізу» block capped by `digest.unscored_max_items`: the outage is
visible in the channel and nothing is dropped. An unscored item that a feed shows again
on a later run is scored then. Every run logs an `info` line with the provider, model,
prompt version, counts, tokens and an estimated cost from `llm.pricing`; `status`
shows it.

---

## Adding a source

Edit `config/sources.yaml`. No code change and no redeploy; the next run picks it up.

```yaml
- id: my-source          # unique; a duplicate keeps the first record and logs an error
  name: My Source        # shown as the group heading in the digest
  method: rss            # rss | github_releases | github_commits_path | html_diff | hn_algolia
  url: https://example.com/feed
  category: practitioner # vendor|practitioner|newsletter|research|community|ba_source|regional
  indicator: leading     # leading | lagging | mixed
  weight: 8              # 1..10
  active: true
```

Required fields depend on the method:

| Method | Also requires | Status |
|---|---|---|
| `rss` | `url` | ✅ Increment 1 |
| `github_releases` | `repo` | ✅ Increment 1 |
| `hn_algolia` | `query`, `min_points` | ✅ Increment 1 |
| `github_commits_path` | `repo`, `path` | Stage 3 |
| `html_diff` | `url`, `selector` | Stage 3 |

A record that fails validation is skipped and logged; the rest of the registry still
loads and the run proceeds.

**`hn_algolia` note:** `query` may be a list. Algolia's search is full-text, not boolean
— `"a OR b"` matches the literal word "OR" and returns nothing — so each term is issued
as its own search and the results are merged. Use quoted phrases; bare terms match
loosely. See [DECISIONS.md](DECISIONS.md) D-01.

---

## How scheduling works

GitHub Actions cron is UTC-only, but Kyiv moves between UTC+2 and UTC+3. Each workflow
schedules **both** candidate UTC hours and a gate step decides which one is 08:00 local
today; the other exits immediately.

| Workflow | When | Does |
|---|---|---|
| `digest.yml` | Weekdays, 08:00 Kyiv | Collect, prepare, commit, send, commit |
| `collect.yml` | Sat & Sun, 08:00 Kyiv | Collect and prune only |
| `catchup.yml` | Weekdays, 11:00 Kyiv | Deliver anything the morning run missed |
| `ci.yml` | Push and PR | Lint, types, tests, registry validation |

All state-writing workflows share one concurrency group, so they queue rather than race
on the SQLite file.

**The promise is same-day delivery, not minute accuracy.** Scheduled runs are routinely
5–30 minutes late and are occasionally dropped entirely. A run that starts within the
08:xx hour still delivers; anything later is left to the 11:00 catch-up.

---

## Crash safety

The dangerous moment is between sending to Telegram and recording that it happened. The
workflow commits state **before** the send:

```
collect → prepare-digest → commit → send-digest → commit
```

If the job dies anywhere after `prepare`, the items are already marked delivered, so
tomorrow's digest will not repeat them. The failure mode is a **missed** digest, which
is visible and which `catchup.yml` repairs — never a **duplicate** one, which would
violate the idempotency requirement and erode the daily ritual the product depends on.

Both digest commands are idempotent: `prepare-digest` is a no-op once today's batch
exists, and `send-digest` is a no-op once Telegram has confirmed. That is what makes it
safe for the catch-up to run unconditionally.

An unconfirmed batch is not abandoned at midnight either: the next days' runs keep
resending it for up to `digest.pending_resend_days` (default 3) local days — enough to
survive a weekend-long outage — and it counts as the digest of the day it finally
lands. Only past that window is a batch given up on, so a multi-day Telegram failure
costs the missed days and nothing else. See DECISIONS.md D-15.

---

## State

`state/ba_radar.sqlite` is committed to the repository. That is deliberate: Actions
cache is evicted after 7 days and is explicitly best-effort, and losing the dedup index
means re-delivering everything.

**Excerpts are never persisted.** They exist in memory for the duration of a run and are
discarded. This keeps the committed file small enough to live in git, and means no
copyrighted source text is retained at rest. Model *outputs* are persisted — score,
tags, summary, insight, action, tier — together with the provider, model and prompt
version that produced them, for calibration in Stage 5.

Retention: full rows for 90 days, then pruned to id + URL, which are kept forever so
deduplication never regresses. `prune` runs weekly from `collect.yml`.

---

## Failure alerts

Telegram cannot be the alert channel for a Telegram failure. Three tiers:

1. **Non-zero exit** — GitHub emails the repository owner on workflow failure. This is
   the primary path and needs no extra infrastructure.
2. **A `run-failure` GitHub Issue** — Stage 3.
3. **Telegram** — only for failures where delivery itself worked, e.g. ≥30% of sources
   unreachable.

---

## Development

```bash
uv run pytest
```

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src
```

Tests never touch the network: HTTP is mocked with `respx` against recorded payloads in
`tests/fixtures/`, and model calls go through a fake provider (`tests/fake_llm.py`)
that implements the same protocol as the real adapters. The adapters themselves are
tested against response fixtures validated by the SDKs' own response models.

---

## Layout

```
config/sources.yaml      the source registry — the only file you need to edit
config/settings.yaml     thresholds, caps, model routing; no secrets
src/ba_radar/
  collectors/            one module per collection method + shared async HTTP
  llm/                   provider-agnostic model calls: Anthropic and OpenAI adapters,
                         provider selection, versioned prompts (prompts/*.md)
  pipeline/              scoring, adjusted score, analysis, selection under the caps
  store/                 SQLite schema, migrations, repositories
  render/                digest formatting and 4096-char splitting
  delivery/              Telegram
  normalize.py           canonical URLs, item identity, title keys
  dedupe.py              identity and windowed-title merging
  tasks.py               collect / prepare / send / prune orchestration
state/ba_radar.sqlite    committed state
```
