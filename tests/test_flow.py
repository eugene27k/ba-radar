"""End-to-end flow: collect -> prepare -> send, with the network mocked.

These are the acceptance checks for Increment 1 expressed as tests, so a regression in
the crash-safety or idempotency behaviour fails CI rather than being discovered at
08:00 some morning.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ba_radar import tasks
from ba_radar.delivery import TelegramError
from ba_radar.models import RunKind, RunStatus, SourceState
from ba_radar.settings import Secrets, Settings
from ba_radar.store import RunRepo, connect

from .conftest import fixture_text

MONDAY = datetime(2026, 8, 3, 5, 5, tzinfo=UTC)  # 08:05 Kyiv

TWO_SOURCES = """
- id: blog
  name: Example Practitioner
  method: rss
  url: https://example.com/feed
  category: practitioner
  indicator: leading
  weight: 9
  active: true

- id: newsletter
  name: Example Newsletter
  method: rss
  url: https://news.example.com/feed
  category: newsletter
  indicator: mixed
  weight: 5
  active: true
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    settings = Settings.load()
    settings.db_path = tmp_path / "state.sqlite"
    settings.sources_path = tmp_path / "sources.yaml"
    settings.sources_path.write_text(TWO_SOURCES, encoding="utf-8")
    settings.collection.retry_interval_seconds = 0
    return settings


def mock_feeds(*, blog: int = 200, newsletter: int = 200) -> None:
    respx.get("https://example.com/feed").mock(
        return_value=httpx.Response(blog, text=fixture_text("blog.atom") if blog == 200 else "")
    )
    respx.get("https://news.example.com/feed").mock(
        return_value=httpx.Response(
            newsletter, text=fixture_text("newsletter.rss") if newsletter == 200 else ""
        )
    )


def mock_telegram() -> respx.Route:
    return respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )


SECRETS = Secrets(telegram_bot_token="test-token", telegram_chat_id="123")


@respx.mock
async def test_collect_is_idempotent_across_runs(cfg: Settings) -> None:
    """Acceptance 1 — a second run delivers nothing new (§8 idempotency)."""
    mock_feeds()

    first = await tasks.collect(cfg, now=MONDAY)
    second = await tasks.collect(cfg, now=MONDAY + timedelta(minutes=1))

    assert first.items_created > 0
    assert second.items_created == 0


@respx.mock
async def test_cross_source_duplicate_is_merged_not_repeated(cfg: Settings) -> None:
    """Both fixtures carry 'Spec-driven development with agents' at different URLs."""
    mock_feeds()
    await tasks.collect(cfg, now=MONDAY)

    conn = connect(cfg.db_path)
    rows = conn.execute(
        "SELECT source_count, max_source_weight FROM items WHERE title LIKE 'Spec-driven%'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["source_count"] == 2
    assert rows[0]["max_source_weight"] == 9


@respx.mock
async def test_a_dead_source_does_not_stop_the_others(cfg: Settings) -> None:
    """Acceptance 2 — req. 1.1.8: log the failure, carry on."""
    mock_feeds(blog=500)

    summary = await tasks.collect(cfg, now=MONDAY)

    assert summary.sources_failed == 1
    assert summary.items_created > 0  # the newsletter still landed
    assert any(entry.level == "error" and entry.source_id == "blog" for entry in summary.log)


@respx.mock
async def test_enough_failures_downgrade_the_run_status(cfg: Settings) -> None:
    """req. 4.2.4 — at or above 30% of sources unavailable the run is degraded."""
    mock_feeds(blog=500, newsletter=500)

    summary = await tasks.collect(cfg, now=MONDAY)

    assert summary.sources_failed == 2
    assert summary.status == RunStatus.DEGRADED


@respx.mock
async def test_registry_errors_are_recorded_on_the_run(cfg: Settings) -> None:
    """Acceptance 3 and 4 — bad records are skipped and surfaced in the run log."""
    cfg.sources_path.write_text(
        TWO_SOURCES
        + """
- id: blog
  name: Duplicate Id
  method: rss
  url: https://dupe.example.com/feed
  category: vendor
  indicator: leading
  weight: 1
  active: true

- id: incomplete
  name: Missing Its URL
  method: rss
  category: vendor
  indicator: leading
  weight: 1
  active: true
""",
        encoding="utf-8",
    )
    mock_feeds()

    summary = await tasks.collect(cfg, now=MONDAY)
    messages = [entry.message for entry in summary.log]

    assert any("duplicate id" in m for m in messages)
    assert any("incomplete" in m and "url" in m for m in messages)
    # The two good sources were still collected.
    assert summary.items_created > 0


@respx.mock
async def test_prepare_then_send_marks_the_run_confirmed(cfg: Settings) -> None:
    mock_feeds()
    mock_telegram()

    await tasks.collect(cfg, now=MONDAY)
    prepared = tasks.prepare_digest(cfg, now=MONDAY)
    assert prepared.state == "prepared"
    assert prepared.selected > 0

    sent = await tasks.send_digest(cfg, SECRETS, now=MONDAY)
    assert sent.state == "sent"

    conn = connect(cfg.db_path)
    run = RunRepo(conn).get(sent.run_id or 0)
    conn.close()
    assert run is not None
    assert run.telegram_confirmed_at is not None
    assert run.status == RunStatus.SUCCESS


@respx.mock
async def test_a_second_send_on_the_same_day_is_a_no_op(cfg: Settings) -> None:
    """The idempotency NFR: re-running must not produce a second digest."""
    mock_feeds()
    route = mock_telegram()

    await tasks.collect(cfg, now=MONDAY)
    tasks.prepare_digest(cfg, now=MONDAY)
    await tasks.send_digest(cfg, SECRETS, now=MONDAY)
    calls_after_first = route.call_count

    again = tasks.prepare_digest(cfg, now=MONDAY + timedelta(hours=3))
    assert again.state == "already_delivered"

    resent = await tasks.send_digest(cfg, SECRETS, now=MONDAY + timedelta(hours=3))
    assert resent.state == "already_delivered"
    assert route.call_count == calls_after_first


@respx.mock
async def test_a_failed_send_leaves_a_batch_the_catch_up_can_resend(cfg: Settings) -> None:
    """Answers doc Q14 — the crash window is a missed digest, never a duplicate one."""
    mock_feeds()
    respx.post(url__regex=r".*/sendMessage").mock(return_value=httpx.Response(500))

    await tasks.collect(cfg, now=MONDAY)
    prepared = tasks.prepare_digest(cfg, now=MONDAY)

    with pytest.raises(TelegramError):
        await tasks.send_digest(cfg, SECRETS, now=MONDAY)

    conn = connect(cfg.db_path)
    run = RunRepo(conn).get(prepared.run_id or 0)
    conn.close()
    assert run is not None
    assert run.status == RunStatus.FAILED  # req. 4.2.5
    assert run.telegram_confirmed_at is None

    # The catch-up run: same commands, now succeeding.
    respx.post(url__regex=r".*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
    )
    retried = tasks.prepare_digest(cfg, now=MONDAY + timedelta(hours=3))
    assert retried.state == "already_prepared"
    assert retried.run_id == prepared.run_id  # the same batch, not a fresh selection

    sent = await tasks.send_digest(cfg, SECRETS, now=MONDAY + timedelta(hours=3))
    assert sent.state == "sent"


@respx.mock
async def test_a_batch_that_failed_yesterday_is_resent_today(cfg: Settings) -> None:
    """DECISIONS D-15 — a multi-day Telegram outage costs the missed days only.

    Before the resend window existed, the next day selected a fresh batch and
    yesterday's items — already marked delivered — were orphaned permanently.
    """
    mock_feeds()
    respx.post(url__regex=r".*/sendMessage").mock(return_value=httpx.Response(500))

    await tasks.collect(cfg, now=MONDAY)
    prepared = tasks.prepare_digest(cfg, now=MONDAY)
    with pytest.raises(TelegramError):
        await tasks.send_digest(cfg, SECRETS, now=MONDAY)

    tuesday = MONDAY + timedelta(days=1)
    retried = tasks.prepare_digest(cfg, now=tuesday)
    assert retried.state == "already_prepared"
    assert retried.run_id == prepared.run_id

    respx.post(url__regex=r".*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    )
    sent = await tasks.send_digest(cfg, SECRETS, now=tuesday)
    assert sent.state == "sent"
    assert sent.run_id == prepared.run_id

    # The late delivery counts as Tuesday's digest: the same-day catch-up must not
    # prepare and send a second one.
    later = tasks.prepare_digest(cfg, now=tuesday + timedelta(hours=3))
    assert later.state == "already_delivered"


@respx.mock
async def test_a_batch_older_than_the_resend_window_is_left_alone(cfg: Settings) -> None:
    """Resending week-old news would displace the day's signal; the cutoff is deliberate."""
    mock_feeds()
    await tasks.collect(cfg, now=MONDAY)
    stale = tasks.prepare_digest(cfg, now=MONDAY)  # its send never happens

    later = MONDAY + timedelta(days=cfg.digest.pending_resend_days + 2)
    fresh = tasks.prepare_digest(cfg, now=later)

    assert fresh.state == "prepared"
    assert fresh.run_id != stale.run_id


@respx.mock
async def test_send_without_prepare_is_a_no_op(cfg: Settings) -> None:
    mock_telegram()
    result = await tasks.send_digest(cfg, SECRETS, now=MONDAY)
    assert result.state == "nothing_prepared"


@respx.mock
async def test_an_empty_day_still_sends_a_message(cfg: Settings) -> None:
    """req. 3.1.9 — silence must not be mistaken for a broken pipeline."""
    mock_feeds()
    route = mock_telegram()

    tasks.prepare_digest(cfg, now=MONDAY)  # nothing collected
    sent = await tasks.send_digest(cfg, SECRETS, now=MONDAY)

    assert sent.state == "sent"
    assert route.call_count == 1
    assert "Нових релевантних матеріалів" in route.calls.last.request.content.decode()


@respx.mock
async def test_delivered_items_are_excluded_from_the_next_digest(cfg: Settings) -> None:
    mock_feeds()
    mock_telegram()

    await tasks.collect(cfg, now=MONDAY)
    first = tasks.prepare_digest(cfg, now=MONDAY)
    await tasks.send_digest(cfg, SECRETS, now=MONDAY)

    tuesday = MONDAY + timedelta(days=1)
    second = tasks.prepare_digest(cfg, now=tuesday)

    assert first.selected > 0
    assert second.selected == 0  # everything already went out


def test_first_ever_run_looks_back_48_hours(cfg: Settings) -> None:
    """req. 1.1.5 — with no cursor, collect the last 48 hours."""
    since, warning = tasks._compute_since(SourceState(source_id="blog"), MONDAY, cfg)
    assert since == MONDAY - timedelta(hours=48)
    assert warning is None


def test_a_fresh_cursor_is_reread_with_an_overlap(cfg: Settings) -> None:
    """DECISIONS D-16 — the window starts behind the cursor so an entry added to the
    feed late, with a published date older than the newest seen, is still collected."""
    cursor = MONDAY - timedelta(hours=6)
    since, warning = tasks._compute_since(
        SourceState(source_id="blog", last_seen_at=cursor), MONDAY, cfg
    )
    assert since == cursor - timedelta(hours=cfg.collection.cursor_overlap_hours)
    assert warning is None


def test_the_overlap_alone_does_not_count_as_a_skipped_window(cfg: Settings) -> None:
    """The Q15 warning is for data that may have been missed; the overlap zone was
    already collected, so clamping it at the floor is silent."""
    cursor = MONDAY - timedelta(hours=60)  # fresh, but cursor - overlap < the 72h floor
    since, warning = tasks._compute_since(
        SourceState(source_id="blog", last_seen_at=cursor), MONDAY, cfg
    )
    assert since == MONDAY - timedelta(hours=cfg.collection.max_lookback_hours)
    assert warning is None


def test_a_stale_cursor_is_clamped_and_the_skip_is_recorded(cfg: Settings) -> None:
    """Answers doc Q15 — after an outage, backfill is capped at 72 hours.

    Without the clamp, a five-day outage would flood a digest capped at 15 items with
    stale news and displace the day's actual signal.
    """
    since, warning = tasks._compute_since(
        SourceState(source_id="blog", last_seen_at=MONDAY - timedelta(days=10)), MONDAY, cfg
    )
    assert since == MONDAY - timedelta(hours=72)
    assert warning is not None
    assert "skipped" in warning


async def test_prune_keeps_the_dedup_index_but_drops_the_payload(cfg: Settings) -> None:
    """Answers doc §0 — full rows for 90 days, then id + url forever."""
    with respx.mock:
        mock_feeds()
        await tasks.collect(cfg, now=MONDAY - timedelta(days=200))

    conn: sqlite3.Connection = connect(cfg.db_path)
    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    assert before > 0

    summary = tasks.prune(cfg, now=MONDAY)
    assert summary.items_pruned == before

    conn = connect(cfg.db_path)
    rows = conn.execute("SELECT id, url, title, pruned FROM items").fetchall()
    links = conn.execute("SELECT COUNT(*) FROM item_sources").fetchone()[0]
    conn.close()

    assert len(rows) == before  # the ids survive, so dedup still works
    assert all(row["pruned"] == 1 for row in rows)
    assert all(row["url"] for row in rows)
    assert all(row["title"] == "" for row in rows)
    assert links == 0


async def test_delivery_confirmations_are_scoped_to_the_local_day(cfg: Settings) -> None:
    """Digest identity is a Kyiv calendar day, so DST cannot produce two per day."""
    conn = connect(cfg.db_path)
    runs = RunRepo(conn)

    early = runs.start(RunKind.DIGEST, datetime(2026, 8, 3, 5, 5, tzinfo=UTC))
    early.telegram_confirmed_at = datetime(2026, 8, 3, 5, 6, tzinfo=UTC)  # 08:06 Kyiv Mon
    runs.save(early)
    late = runs.start(RunKind.DIGEST, datetime(2026, 8, 3, 22, 30, tzinfo=UTC))
    late.telegram_confirmed_at = datetime(2026, 8, 3, 22, 31, tzinfo=UTC)  # 01:31 Kyiv Tue
    runs.save(late)

    from zoneinfo import ZoneInfo

    kyiv = ZoneInfo("Europe/Kyiv")
    monday = runs.confirmed_on_local_date(RunKind.DIGEST, datetime(2026, 8, 3).date(), kyiv)
    tuesday = runs.confirmed_on_local_date(RunKind.DIGEST, datetime(2026, 8, 4).date(), kyiv)
    conn.close()

    assert monday is not None and monday.id == early.id
    assert tuesday is not None and tuesday.id == late.id
