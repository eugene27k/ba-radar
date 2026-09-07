"""Task orchestration: collect, prepare, send, prune.

`prepare` and `send` are separate entry points on purpose. The workflow commits the
SQLite state to git *between* them, so a crash after the Telegram send can never
produce a second digest tomorrow — the worst case is a digest that was marked
delivered but never sent, which is visible and which the catch-up run repairs
(answers doc Q14).

Stage 2 adds the model phases to `collect` — scoring and analysis run there because
the excerpts exist only in memory during collection (answers doc §0) — and replaces
the flat selection with tiered selection under the caps at `prepare` time. When and
how often digests go out is unchanged.
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
from ba_radar.dedupe import Candidate, DedupeResult, Deduplicator
from ba_radar.delivery import TelegramClient
from ba_radar.llm import ProviderPool
from ba_radar.models import (
    ItemStatus,
    Run,
    RunKind,
    RunLogEntry,
    RunStatus,
    Source,
    SourceMethod,
    SourceState,
)
from ba_radar.normalize import canonical_url, item_id, title_key
from ba_radar.pipeline import (
    DigestSelection,
    EnrichSummary,
    Ranked,
    ScoringItem,
    display_order,
    enrich,
    select_digest,
)
from ba_radar.registry import load_registry
from ba_radar.render import DigestEntry, render_digest
from ba_radar.settings import LLMSecrets, Secrets, Settings, github_token
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
    llm: EnrichSummary | None = None


@dataclass
class PrepareSummary:
    run_id: int | None
    state: str  # "prepared" | "already_prepared" | "already_delivered"
    selected: int = 0
    pool: int = 0  # undelivered analysed + unscored items considered
    processed: int = 0  # the header figure: items scored since the previous digest


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


def _scoring_set(
    items_repo: ItemRepo,
    candidates: list[Candidate],
    deduped: DedupeResult,
    index: dict[str, Source],
) -> list[ScoringItem]:
    """The rows this run must score, each with the freshest excerpt seen for it.

    New rows always qualify. A row seen again that is still unscored — because a
    previous run's provider failed or timed out — qualifies too: its excerpt is back
    in memory, so this is the one moment it can be repaired (DECISIONS.md D-20).
    Everything else (scored, delivered, pruned) is left alone; nothing is re-scored.
    """
    excerpt_for: dict[str, str] = {}
    for candidate in candidates:
        row_id = deduped.row_for.get(candidate.item_id)
        if row_id is None:
            continue
        excerpt = candidate.raw.excerpt
        if len(excerpt) > len(excerpt_for.get(row_id, "")):
            excerpt_for[row_id] = excerpt

    seen: list[str] = []
    for row_id in [*deduped.created, *deduped.merged, *deduped.unchanged]:
        if row_id not in seen:
            seen.append(row_id)

    result: list[ScoringItem] = []
    for row_id in seen:
        item = items_repo.get(row_id)
        if (
            item is None
            or item.pruned
            or item.delivered_run_id is not None
            or item.status != ItemStatus.COLLECTED
            or item.relevance_score is not None
        ):
            continue
        names = tuple(index[sid].name for sid in items_repo.source_ids_for(item.id) if sid in index)
        result.append(
            ScoringItem(item=item, excerpt=excerpt_for.get(row_id, ""), source_names=names)
        )
    return result


async def collect(
    settings: Settings, *, now: datetime | None = None, llm: ProviderPool | None = None
) -> CollectSummary:
    now = to_utc(now or datetime.now(UTC))

    # Fail fast on a missing provider key, before the database is even opened: items
    # collected without a provider could never be scored, because their excerpts are
    # gone once this run ends (DECISIONS.md D-20).
    if llm is None:
        llm = ProviderPool.for_tasks(settings.llm, LLMSecrets.from_env())

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
        # 60 requests an hour unauthenticated. The workflows pass GITHUB_TOKEN to the
        # collect step explicitly (D-17), so this fires locally — where it is
        # otherwise diagnosed as a mystery 403.
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

    # Model phases, while the excerpts are still in memory.
    index = {source.id: source for source in registry.sources}
    to_score = _scoring_set(items_repo, candidates, deduped, index)
    enrichment = await enrich(conn, settings, llm, to_score, now=now)
    log.extend(enrichment.log)

    status = RunStatus.SUCCESS
    if active and failed / len(active) >= DEGRADED_FAILURE_RATIO:
        status = RunStatus.DEGRADED  # req. 4.2.4
    elif enrichment.degraded or any(entry.level == "error" for entry in log):
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
        llm=enrichment,
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
    conn: sqlite3.Connection, settings: Settings, rows: list[Ranked]
) -> list[DigestEntry]:
    """Digest entries in the given display order, labelled with the strongest source."""
    items_repo = ItemRepo(conn)
    index = _source_index(settings)

    entries: list[DigestEntry] = []
    for row in rows:
        item = row.item
        sources = [index[sid] for sid in items_repo.source_ids_for(item.id) if sid in index]
        best = max(sources, key=lambda s: s.weight) if sources else None
        entries.append(
            DigestEntry(
                title=item.title,
                url=item.url,
                source_label=best.name if best else "—",
                published_at=item.published_at,
                priority=row.priority,
                summary=item.summary,
                ba_insight=item.ba_insight,
                action=item.action,
                tags=tuple(item.tags),
                verbatim_flag=item.verbatim_flag,
            )
        )
    return entries


def _processed_since(runs_repo: RunRepo) -> datetime | None:
    """Start of the window the header's «processed» count covers (D-23)."""
    previous = runs_repo.latest(RunKind.DIGEST)
    return previous.started_at if previous else None


def _select(
    conn: sqlite3.Connection, settings: Settings, now: datetime
) -> tuple[DigestSelection, int, int]:
    """(selection, pool size, processed count) — shared by prepare and preview."""
    items_repo = ItemRepo(conn)
    runs_repo = RunRepo(conn)
    analyzed = items_repo.undelivered(ItemStatus.ANALYZED)
    unscored = items_repo.undelivered(ItemStatus.COLLECTED)
    selection = select_digest(analyzed, unscored, settings, now=now)
    processed = items_repo.count_scored_since(_processed_since(runs_repo))
    return selection, len(analyzed) + len(unscored), processed


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

    selection, pool, processed = _select(conn, settings, now)
    rows = selection.ordered

    with transaction(conn):
        run = runs_repo.start(RunKind.DIGEST, now)
        assert run.id is not None
        items_repo.mark_delivered(
            [(row.item.id, row.priority, row.adjusted) for row in rows], run.id, now
        )
        run.collected_count = processed
        run.delivered_count = len(rows)
        runs_repo.save(run)

    run_id = run.id
    conn.close()
    return PrepareSummary(
        run_id=run_id, state="prepared", selected=len(rows), pool=pool, processed=processed
    )


def preview_digest(settings: Settings, *, now: datetime | None = None) -> list[str]:
    """Render what would be sent, touching nothing. Used by `--dry-run`."""
    now = to_utc(now or datetime.now(UTC))
    tz = ZoneInfo(settings.schedule.timezone)

    conn = connect(settings.db_path)
    selection, _pool, processed = _select(conn, settings, now)
    entries = _build_entries(conn, settings, selection.ordered)
    conn.close()

    return render_digest(
        entries,
        digest_date=now.astimezone(tz).date(),
        processed_count=processed,
        max_chars=settings.digest.telegram_max_chars,
        title=settings.digest.header_title,
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

    # Tier and adjusted score were persisted at prepare time; a resend must not
    # recompute them, or a merge in between could reshuffle the digest.
    items = items_repo.delivered_for_run(pending.id)
    entries = _build_entries(conn, settings, display_order(items, settings))
    messages = render_digest(
        entries,
        digest_date=today,
        processed_count=pending.collected_count,
        max_chars=settings.digest.telegram_max_chars,
        title=settings.digest.header_title,
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
