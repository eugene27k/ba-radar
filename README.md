# BA Radar

Monitors ~50 sources about AI-native development, discards what a business analyst
doesn't need, and delivers a short digest to Telegram every working morning at 08:00
Kyiv time.

The point of the product is not aggregation — that is a commodity. It is the verdict
attached to each item: *does this change my artifacts, my process, or my tooling?* No
existing source writes that down, which is what the model is there to do.

**Status: Increment 1 (Stage 1 of PRD §11).** Collection, deduplication and Telegram
delivery work end to end. There is no relevance scoring or model analysis yet, so the
digest is a plain grouped list — Stage 2 adds the filter, the per-item verdict and the
priority tiers. See [DECISIONS.md](DECISIONS.md) for what diverges from the PRD and why.

---

## Quick start

```bash
uv sync
```

```bash
uv run ba-radar validate-sources
```

```bash
uv run ba-radar run --dry-run
```

`--dry-run` collects from every active source and prints the digest to stdout. It needs
no Telegram credentials and sends nothing.

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

## Commands

| Command | What it does |
|---|---|
| `collect` | Fetch every active source, normalise, dedupe, store |
| `preview` | Render the digest to stdout — touches neither the database nor Telegram |
| `prepare-digest` | Select today's items and mark them delivered |
| `send-digest` | Send the prepared digest to Telegram |
| `run` | `collect` + `prepare` + `send`; `--dry-run` prints instead of sending |
| `status` | Recent runs, with error counts and delivery confirmation |
| `validate-sources` | Check `config/sources.yaml`; non-zero exit on any bad record |
| `prune` | Apply the retention policy and vacuum the database |
| `gate --hour N` | Decide whether a UTC-scheduled run is the right one for local hour N |

`prepare-digest` and `send-digest` are separate because the workflow commits state to
git **between** them — see *Crash safety* below.

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
copyrighted source text is retained at rest.

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
`tests/fixtures/`.

---

## Layout

```
config/sources.yaml      the source registry — the only file you need to edit
config/settings.yaml     thresholds, caps, model routing; no secrets
src/ba_radar/
  collectors/            one module per collection method + shared async HTTP
  store/                 SQLite schema, migrations, repositories
  render/                digest formatting and 4096-char splitting
  delivery/              Telegram
  normalize.py           canonical URLs, item identity, title keys
  dedupe.py              identity and windowed-title merging
  tasks.py               collect / prepare / send / prune orchestration
state/ba_radar.sqlite    committed state
```

Stage 2 adds `src/ba_radar/llm/` (provider-agnostic model calls, versioned prompts) and
`src/ba_radar/pipeline/` (scoring, analysis, prioritisation).
