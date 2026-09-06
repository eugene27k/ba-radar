"""Task orchestration: collect, prepare, send, prune.

`prepare` and `send` are separate entry points on purpose. The workflow commits the
SQLite state to git *between* them, so a crash after the Telegram send can never
produce a second digest tomorrow — the worst case is a digest that was marked
delivered but never sent, which is visible and which the catch-up run repairs
(answers doc Q14).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ba_radar.collectors import FetchContext, FetchResult, HttpFetcher, get_collector
from ba_radar.dedupe import Candidate, Deduplicator
from ba_radar.delivery import TelegramClient
from ba_radar.models import (
    Run,
    RunKind,
    RunLogEntry,
    RunStatus,
    Source,
    SourceMethod,
    SourceState,
)
from ba_radar.normalize import canonical_url, item_id, title_key
from ba_radar.registry import load_registry
from ba_radar.render import DigestEntry, render_digest
from ba_radar.settings import Secrets, Settings, github_token
from ba_radar.store import ItemRepo, RunRepo, SourceStateRepo, connect, transaction, vacuum
from ba_radar.store.repo import to_utc

DEGRADED_FAILURE_RATIO = 0.30  # req. 4.2.4


@dataclass
class CollectSummary:
    run_id: int
    status: RunStatus
    sources_attempted: int
    sources_failed: int
    items_created: int
    items_merged: int
    log: list[RunLogEntry] = field(default_factory=list)


@dataclass
class PrepareSummary:
    run_id: int | None
    state: str  # "prepared" | "already_prepared" | "already_delivered"
    selected: int = 0
    pool: int = 0


@dataclass
class SendSummary:
    run_id: int | None
    state: str  # "sent" | "already_delivered" | "nothing_prepared"
    messages: int = 0
    delivered: int = 0


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


def _compute_since(
    state: SourceState, now: datetime, settings: Settings
) -> tuple[datetime, str | None]:
    """Incremental window for one source (req. 1.1.4 / 1.1.5, answers doc Q15).

    The window starts `cursor_overlap_hours` *behind* the cursor: the cursor is the
    newest published date seen, so an entry added to the feed late with an older
    published date would otherwise be skipped forever. Re-reading the overlap is free
    — identity dedup makes a second sighting a no-op. See DECISIONS.md D-16.
    """
    collection = settings.collection

    if state.last_seen_at is None:
        return now - timedelta(hours=collection.default_lookback_hours), None

    floor = now - timedelta(hours=collection.max_lookback_hours)
    if state.last_seen_at < floor:
        skipped = floor - state.last_seen_at
        hours = skipped.total_seconds() / 3600
        return floor, (
            f"cursor was {hours:.0f}h older than the {collection.max_lookback_hours}h "
            f"limit; that window was skipped"
        )
    # No warning when only the overlap dips below the floor — that window was
    # already collected, nothing is being skipped.
    since = state.last_seen_at - timedelta(hours=collection.cursor_overlap_hours)
    return max(since, floor), None


async def _fetch_source(
    source: Source,
    state: SourceState,
    fetcher: HttpFetcher,
    settings: Settings,
    now: datetime,
) -> tuple[Source, FetchResult | None, list[RunLogEntry]]:
    log: list[RunLogEntry] = []
    collector = get_collector(source.method)
    if collector is None:
        log.append(
            RunLogEntry(
                level="error",
                source_id=source.id,
                message=f"no collector registered for method '{source.method}'",
            )
        )
        return source, None, log

    since, clamp_warning = _compute_since(state, now, settings)
    if clamp_warning:
        log.append(RunLogEntry(level="warning", source_id=source.id, message=clamp_warning))

    ctx = FetchContext(
        fetcher=fetcher,
        settings=settings.collection,
        since=since,
        now=now,
        deadline=time.monotonic() + settings.collection.per_source_deadline_seconds,
        github_token=github_token(),
    )

    try:
        result = await collector.fetch(source, state, ctx)
    except Exception as exc:  # req. 1.1.8 — log and carry on with the other sources
        log.append(
            RunLogEntry(
                level="error",
                source_id=source.id,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return source, None, log

    for warning in result.warnings:
        log.append(RunLogEntry(level="warning", source_id=source.id, message=warning))
    return source, result, log


async def collect(settings: Settings, *, now: datetime | None = None) -> CollectSummary:
    now = to_utc(now or datetime.now(UTC))
    conn = connect(settings.db_path)
    items_repo = ItemRepo(conn)
    runs_repo = RunRepo(conn)
    state_repo = SourceStateRepo(conn)

    registry = load_registry(settings.sources_path)
    log = [RunLogEntry(level="error", message=msg) for msg in registry.errors]
    active = registry.active

    run = runs_repo.start(RunKind.COLLECT, now)
    assert run.id is not None

    github_methods = {SourceMethod.GITHUB_RELEASES, SourceMethod.GITHUB_COMMITS_PATH}
    github_sources = sum(1 for s in active if s.method in github_methods)
    if github_sources and not github_token():
        # 60 requests an hour unauthenticated. Actions always provides GITHUB_TOKEN, so
        # this fires locally — where it is otherwise diagnosed as a mystery 403.
        log.append(
            RunLogEntry(
                level="warning",
                message=(
                    f"GITHUB_TOKEN is not set and {github_sources} source(s) use the "
                    f"GitHub API; the unauthenticated quota is 60 requests/hour"
                ),
            )
        )

    https_hosts = frozenset(
        host
        for source in registry.sources
        if source.url
        and source.url.startswith("https://")
        and (host := urlsplit(source.url).hostname)
    )

    failed = 0
    candidates: list[Candidate] = []

    async with HttpFetcher(settings.collection) as fetcher:
        outcomes = await asyncio.gather(
            *(
                _fetch_source(source, state_repo.get(source.id), fetcher, settings, now)
                for source in active
            )
        )

        for source, result, entries in outcomes:
            log.extend(entries)
            if result is None:
                failed += 1
                previous = state_repo.get(source.id)
                state_repo.save(
                    previous.model_copy(
                        update={
                            "consecutive_failures": previous.consecutive_failures + 1,
                            "last_error": next(
                                (e.message for e in entries if e.level == "error"), None
                            ),
                        }
                    )
                )
                continue

            if result.state is not None:
                state_repo.save(result.state)

            keep_fragment = source.method == SourceMethod.HTML_DIFF
            for raw in result.items:
                url = raw.url
                if _needs_redirect_resolution(url, settings):
                    url = await fetcher.resolve_redirect(
                        url,
                        settings.normalize.redirect_timeout_seconds,
                        settings.normalize.redirect_max_hops,
                    )
                canonical = canonical_url(
                    url,
                    settings.normalize,
                    keep_fragment=keep_fragment,
                    https_hosts=https_hosts,
                )
                candidates.append(
                    Candidate(
                        raw=raw,
                        source=source,
                        canonical_url=canonical,
                        item_id=item_id(canonical),
                        title_key=title_key(raw.title, source.name),
                        published_at=to_utc(raw.published_at or now),
                    )
                )

    # Global cap (answers doc Q20). Best sources survive a flood.
    cap = settings.collection.global_item_cap
    if len(candidates) > cap:
        candidates.sort(key=lambda c: (-c.source.weight, -c.published_at.timestamp()))
        log.append(
            RunLogEntry(
                level="warning",
                message=(
                    f"global cap reached: kept {cap} of {len(candidates)} candidates, "
                    f"lowest-weight sources dropped"
                ),
            )
        )
        candidates = candidates[:cap]

    with transaction(conn):
        deduper = Deduplicator(
            items_repo, title_window_days=settings.dedupe.title_window_days, now=now
        )
        deduped = deduper.ingest(candidates)

    status = RunStatus.SUCCESS
    if active and failed / len(active) >= DEGRADED_FAILURE_RATIO:
        status = RunStatus.DEGRADED  # req. 4.2.4
    elif any(entry.level == "error" for entry in log):
        status = RunStatus.DEGRADED

    run.finished_at = datetime.now(UTC)
    run.status = status
    run.collected_count = deduped.total
    run.log = log
    runs_repo.save(run)
    conn.close()

    return CollectSummary(
        run_id=run.id,
        status=status,
        sources_attempted=len(active),
        sources_failed=failed,
        items_created=len(deduped.created),
        items_merged=len(deduped.merged),
        log=log,
    )


def _needs_redirect_resolution(url: str, settings: Settings) -> bool:
    from ba_radar.normalize import should_resolve_redirect

    return should_resolve_redirect(url, settings.normalize)


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


def _find_pending_batch(
    runs_repo: RunRepo, today: date, tz: ZoneInfo, resend_days: int
) -> Run | None:
    """The oldest digest batch selected but never confirmed delivered, if any.

    Looks back `resend_days` local days as well as at today: a batch whose send kept
    failing is retried on following days instead of being orphaned, so a multi-day
    Telegram outage costs only the missed days (DECISIONS.md D-15). Anything older is
    left alone — resending week-old news would displace the day's actual signal.
    """
    window_start = datetime.combine(
        today - timedelta(days=resend_days), datetime.min.time(), tzinfo=tz
    )
    return next(iter(runs_repo.unconfirmed_since(RunKind.DIGEST, window_start)), None)


def _source_index(settings: Settings) -> dict[str, Source]:
    return {source.id: source for source in load_registry(settings.sources_path).sources}


def _build_entries(
    conn: sqlite3.Connection, settings: Settings, item_ids_in_order: list[str]
) -> list[DigestEntry]:
    items_repo = ItemRepo(conn)
    index = _source_index(settings)

    rows: list[tuple[int, str, DigestEntry]] = []
    for item_id_value in item_ids_in_order:
        item = items_repo.get(item_id_value)
        if item is None:
            continue
        sources = [index[sid] for sid in items_repo.source_ids_for(item.id) if sid in index]
        best = max(sources, key=lambda s: s.weight) if sources else None
        label = best.name if best else "—"
        weight = best.weight if best else 0
        rows.append(
            (
                weight,
                label,
                DigestEntry(
                    title=item.title,
                    url=item.url,
                    source_label=label,
                    published_at=item.published_at,
                ),
            )
        )

    # Group by source: strongest sources first, items newest-first inside each group.
    rows.sort(key=lambda row: (-row[0], row[1], -row[2].published_at.timestamp()))
    return [row[2] for row in rows]


def prepare_digest(settings: Settings, *, now: datetime | None = None) -> PrepareSummary:
    """Select today's items and mark them delivered, in one transaction.

    Atomic on purpose: a run left in RUNNING state always has its items marked, so
    `send_digest` can trust that a RUNNING digest run is a complete, sendable batch.
    """
    now = to_utc(now or datetime.now(UTC))
    tz = ZoneInfo(settings.schedule.timezone)
    today = now.astimezone(tz).date()

    conn = connect(settings.db_path)
    runs_repo = RunRepo(conn)
    items_repo = ItemRepo(conn)

    delivered = runs_repo.confirmed_on_local_date(RunKind.DIGEST, today, tz)
    if delivered is not None:
        conn.close()
        return PrepareSummary(run_id=delivered.id, state="already_delivered")

    # Any unconfirmed run in the resend window is a batch that has been selected but
    # not delivered — whether it crashed before the send (RUNNING) or the send itself
    # failed (FAILED), today or on a recent day. It must be resent as-is: its items
    # are already marked against that run, so selecting a fresh batch would orphan
    # them permanently.
    pending = _find_pending_batch(runs_repo, today, tz, settings.digest.pending_resend_days)
    if pending is not None:
        conn.close()
        return PrepareSummary(
            run_id=pending.id, state="already_prepared", selected=pending.delivered_count
        )

    pool = items_repo.count_undelivered()
    selected = items_repo.select_for_digest(settings.digest.stage1_max_items)

    with transaction(conn):
        run = runs_repo.start(RunKind.DIGEST, now)
        assert run.id is not None
        items_repo.mark_delivered([item.id for item in selected], run.id, now)
        run.collected_count = pool
        run.delivered_count = len(selected)
        runs_repo.save(run)

    run_id = run.id
    conn.close()
    return PrepareSummary(run_id=run_id, state="prepared", selected=len(selected), pool=pool)


def preview_digest(settings: Settings, *, now: datetime | None = None) -> list[str]:
    """Render what would be sent, touching nothing. Used by `--dry-run`."""
    now = to_utc(now or datetime.now(UTC))
    tz = ZoneInfo(settings.schedule.timezone)

    conn = connect(settings.db_path)
    items_repo = ItemRepo(conn)
    pool = items_repo.count_undelivered()
    selected = items_repo.select_for_digest(settings.digest.stage1_max_items)
    entries = _build_entries(conn, settings, [item.id for item in selected])
    conn.close()

    return render_digest(
        entries,
        digest_date=now.astimezone(tz).date(),
        processed_count=pool,
        max_chars=settings.digest.telegram_max_chars,
    )


async def send_digest(
    settings: Settings,
    secrets: Secrets,
    *,
    now: datetime | None = None,
) -> SendSummary:
    now = to_utc(now or datetime.now(UTC))
    tz = ZoneInfo(settings.schedule.timezone)
    today = now.astimezone(tz).date()

    conn = connect(settings.db_path)
    runs_repo = RunRepo(conn)
    items_repo = ItemRepo(conn)

    delivered = runs_repo.confirmed_on_local_date(RunKind.DIGEST, today, tz)
    if delivered is not None:
        conn.close()
        return SendSummary(run_id=delivered.id, state="already_delivered")

    pending = _find_pending_batch(runs_repo, today, tz, settings.digest.pending_resend_days)
    if pending is None or pending.id is None:
        conn.close()
        return SendSummary(run_id=None, state="nothing_prepared")

    items = items_repo.delivered_for_run(pending.id)
    entries = _build_entries(conn, settings, [item.id for item in items])
    messages = render_digest(
        entries,
        digest_date=today,
        processed_count=pending.collected_count,
        max_chars=settings.digest.telegram_max_chars,
    )

    try:
        async with TelegramClient(secrets.telegram_bot_token, secrets.telegram_chat_id) as telegram:
            await telegram.send_messages(messages)
    except Exception as exc:
        pending.status = RunStatus.FAILED  # req. 4.2.5
        pending.finished_at = now
        pending.log = [
            *pending.log,
            RunLogEntry(level="error", message=f"telegram delivery failed: {exc}"),
        ]
        runs_repo.save(pending)
        conn.close()
        raise

    # Stamped with this run's `now`, not a fresh clock read: `confirmed_on_local_date`
    # keys "one digest per day" on this value, so it must fall on the local day the
    # send ran under.
    pending.telegram_confirmed_at = now
    pending.finished_at = now
    pending.status = RunStatus.SUCCESS
    runs_repo.save(pending)
    conn.close()

    return SendSummary(
        run_id=pending.id,
        state="sent",
        messages=len(messages),
        delivered=len(items),
    )


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


@dataclass
class PruneSummary:
    items_pruned: int
    runs_deleted: int


def prune(settings: Settings, *, now: datetime | None = None) -> PruneSummary:
    now = to_utc(now or datetime.now(UTC))
    conn = connect(settings.db_path)
    items_repo = ItemRepo(conn)
    runs_repo = RunRepo(conn)

    with transaction(conn):
        items_pruned = items_repo.prune_before(
            now - timedelta(days=settings.retention.full_rows_days)
        )
        runs_deleted = runs_repo.prune_before(now - timedelta(days=settings.retention.runs_days))

    vacuum(conn)
    conn.close()
    return PruneSummary(items_pruned=items_pruned, runs_deleted=runs_deleted)
