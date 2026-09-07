"""Repositories over the SQLite store.

All timestamps are stored as ISO 8601 UTC strings. Every datetime crossing this
boundary is normalised to UTC on the way in and returned tz-aware on the way out, so
nothing downstream has to guess.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ba_radar.models import (
    Action,
    Category,
    Indicator,
    Item,
    ItemStatus,
    PracticeTag,
    Priority,
    Run,
    RunKind,
    RunLogEntry,
    RunStatus,
    SourceState,
)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dump_dt(value: datetime | None) -> str | None:
    return to_utc(value).isoformat() if value is not None else None


def _load_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _require_dt(value: str | None) -> datetime:
    parsed = _load_dt(value)
    if parsed is None:
        raise ValueError("expected a timestamp, got NULL")
    return parsed


class ItemRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, item_id: str) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def find_by_title_key(self, title_key: str, since: datetime) -> Item | None:
        """Title match, bounded to a window so recurring titles are not suppressed forever.

        The oldest matching item wins, which makes "later is the duplicate" mean
        "the row already in the database wins" (answers doc Q12).
        """
        row = self.conn.execute(
            "SELECT * FROM items WHERE title_key = ? AND collected_at >= ? "
            "ORDER BY collected_at ASC LIMIT 1",
            (title_key, _dump_dt(since)),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def insert(self, item: Item) -> None:
        self.conn.execute(
            "INSERT INTO items ("
            " id, url, title, title_key, category, indicator, published_at, collected_at,"
            " source_count, max_source_weight, relevance_score, tags, priority,"
            " suggested_priority, summary, ba_insight, action, status, verbatim_flag,"
            " delivered_run_id, delivered_at, pruned,"
            " adjusted_score, scored_at, llm_provider, llm_model, prompt_version"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.id,
                item.url,
                item.title,
                item.title_key,
                item.category.value,
                item.indicator.value,
                _dump_dt(item.published_at),
                _dump_dt(item.collected_at),
                item.source_count,
                item.max_source_weight,
                item.relevance_score,
                json.dumps([t.value for t in item.tags]),
                item.priority.value if item.priority else None,
                item.suggested_priority.value if item.suggested_priority else None,
                item.summary,
                item.ba_insight,
                item.action.value if item.action else None,
                item.status.value,
                int(item.verbatim_flag),
                item.delivered_run_id,
                _dump_dt(item.delivered_at),
                int(item.pruned),
                item.adjusted_score,
                _dump_dt(item.scored_at),
                item.llm_provider,
                item.llm_model,
                item.prompt_version,
            ),
        )
        for source_id in item.source_ids:
            self.link_source(item.id, source_id, item.collected_at)

    def link_source(self, item_id: str, source_id: str, seen_at: datetime) -> bool:
        """Attach a source to an item. Returns True if this was a new link."""
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO item_sources (item_id, source_id, first_seen_at) "
            "VALUES (?, ?, ?)",
            (item_id, source_id, _dump_dt(seen_at)),
        )
        return cursor.rowcount > 0

    def refresh_source_aggregates(self, item_id: str, weight: int) -> None:
        """Recompute source_count and raise max_source_weight after a merge."""
        self.conn.execute(
            "UPDATE items SET"
            " source_count = (SELECT COUNT(*) FROM item_sources WHERE item_id = ?),"
            " max_source_weight = MAX(max_source_weight, ?)"
            " WHERE id = ?",
            (item_id, weight, item_id),
        )

    def apply_score(
        self,
        item_id: str,
        *,
        relevance_score: int,
        tags: list[PracticeTag],
        adjusted_score: int,
        status: ItemStatus,
        scored_at: datetime,
        llm_provider: str,
        llm_model: str,
        prompt_version: str,
    ) -> None:
        """Persist the model's base score and tags plus the adjusted score and the
        provenance of the verdict. `status` is COLLECTED (awaiting analysis) or
        FILTERED (below the threshold)."""
        self.conn.execute(
            "UPDATE items SET relevance_score = ?, tags = ?, adjusted_score = ?, status = ?,"
            " scored_at = ?, llm_provider = ?, llm_model = ?, prompt_version = ?"
            " WHERE id = ?",
            (
                relevance_score,
                json.dumps([t.value for t in tags]),
                adjusted_score,
                status.value,
                _dump_dt(scored_at),
                llm_provider,
                llm_model,
                prompt_version,
                item_id,
            ),
        )

    def apply_analysis(
        self,
        item_id: str,
        *,
        summary: str,
        ba_insight: str,
        action: Action,
        suggested_priority: Priority,
        verbatim_flag: bool,
        priority: Priority,
        prompt_version: str,
    ) -> None:
        """Persist the verdict and move the item to ANALYZED."""
        self.conn.execute(
            "UPDATE items SET summary = ?, ba_insight = ?, action = ?, suggested_priority = ?,"
            " verbatim_flag = ?, priority = ?, prompt_version = ?, status = ?"
            " WHERE id = ?",
            (
                summary,
                ba_insight,
                action.value,
                suggested_priority.value,
                int(verbatim_flag),
                priority.value,
                prompt_version,
                ItemStatus.ANALYZED.value,
                item_id,
            ),
        )

    def undelivered(self, status: ItemStatus) -> list[Item]:
        """Undelivered, unpruned items in one status, oldest collected first.

        The digest selection (`ba_radar.pipeline.select`) ranks and caps these; this
        is the documented replacement point for Increment 1's `select_for_digest`.
        """
        rows = self.conn.execute(
            "SELECT * FROM items"
            " WHERE delivered_run_id IS NULL AND status = ? AND pruned = 0"
            " ORDER BY collected_at ASC, published_at DESC",
            (status.value,),
        ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def count_undelivered(self) -> int:
        """Items that could still be delivered. FILTERED rows are kept only for dedup
        and never count (DECISIONS.md D-23)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE delivered_run_id IS NULL AND pruned = 0"
            " AND status IN (?, ?)",
            (ItemStatus.COLLECTED.value, ItemStatus.ANALYZED.value),
        ).fetchone()
        return int(row["n"])

    def count_scored_since(self, since: datetime | None) -> int:
        """The header's «processed» figure: items scored after `since` (all scored
        items when there is no previous digest). FILTERED items count — they were
        processed, and rejected."""
        if since is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE scored_at IS NOT NULL"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE scored_at > ?", (_dump_dt(since),)
            ).fetchone()
        return int(row["n"])

    def mark_delivered(
        self,
        rows: list[tuple[str, Priority | None, int | None]],
        run_id: int,
        at: datetime,
    ) -> None:
        """Mark a selected batch delivered, persisting the tier and adjusted score each
        item was selected with so a resend renders the same digest."""
        for item_id, priority, adjusted in rows:
            self.conn.execute(
                "UPDATE items SET status = ?, delivered_run_id = ?, delivered_at = ?,"
                " priority = ?, adjusted_score = ? WHERE id = ?",
                (
                    ItemStatus.DELIVERED.value,
                    run_id,
                    _dump_dt(at),
                    priority.value if priority else None,
                    adjusted,
                    item_id,
                ),
            )

    def delivered_for_run(self, run_id: int) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE delivered_run_id = ?"
            " ORDER BY max_source_weight DESC, published_at DESC",
            (run_id,),
        ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def source_ids_for(self, item_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT source_id FROM item_sources WHERE item_id = ? ORDER BY first_seen_at",
            (item_id,),
        ).fetchall()
        return [row["source_id"] for row in rows]

    def prune_before(self, cutoff: datetime) -> int:
        """Drop the payload of old rows, keep the dedup index forever.

        id, url, published_at, collected_at, status and the numeric verdicts survive;
        the prose and the per-source links do not. See DECISIONS.md D-05.
        """
        cutoff_s = _dump_dt(cutoff)
        # No `pruned = 0` filter here: a link row that somehow reappeared on an
        # already-pruned item would otherwise never be cleaned up.
        self.conn.execute(
            "DELETE FROM item_sources WHERE item_id IN"
            " (SELECT id FROM items WHERE collected_at < ?)",
            (cutoff_s,),
        )
        cursor = self.conn.execute(
            "UPDATE items SET"
            " title = '', title_key = '', tags = '[]', summary = NULL, ba_insight = NULL,"
            " suggested_priority = NULL, pruned = 1"
            " WHERE collected_at < ? AND pruned = 0",
            (cutoff_s,),
        )
        return cursor.rowcount

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            title_key=row["title_key"],
            source_count=row["source_count"],
            max_source_weight=row["max_source_weight"],
            category=Category(row["category"]),
            indicator=Indicator(row["indicator"]),
            published_at=_require_dt(row["published_at"]),
            collected_at=_require_dt(row["collected_at"]),
            relevance_score=row["relevance_score"],
            tags=[PracticeTag(t) for t in json.loads(row["tags"])],
            priority=Priority(row["priority"]) if row["priority"] else None,
            suggested_priority=(
                Priority(row["suggested_priority"]) if row["suggested_priority"] else None
            ),
            summary=row["summary"],
            ba_insight=row["ba_insight"],
            action=Action(row["action"]) if row["action"] else None,
            status=ItemStatus(row["status"]),
            verbatim_flag=bool(row["verbatim_flag"]),
            delivered_run_id=row["delivered_run_id"],
            delivered_at=_load_dt(row["delivered_at"]),
            pruned=bool(row["pruned"]),
            adjusted_score=row["adjusted_score"],
            scored_at=_load_dt(row["scored_at"]),
            llm_provider=row["llm_provider"],
            llm_model=row["llm_model"],
            prompt_version=row["prompt_version"],
        )


class RunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(self, kind: RunKind, started_at: datetime) -> Run:
        cursor = self.conn.execute(
            "INSERT INTO runs (kind, started_at, status, log) VALUES (?, ?, ?, '[]')",
            (kind.value, _dump_dt(started_at), RunStatus.RUNNING.value),
        )
        run_id = int(cursor.lastrowid or 0)
        return Run(id=run_id, kind=kind, started_at=to_utc(started_at))

    def save(self, run: Run) -> None:
        if run.id is None:
            raise ValueError("cannot save a run without an id")
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, collected_count = ?,"
            " delivered_count = ?, telegram_confirmed_at = ?, log = ? WHERE id = ?",
            (
                _dump_dt(run.finished_at),
                run.status.value,
                run.collected_count,
                run.delivered_count,
                _dump_dt(run.telegram_confirmed_at),
                json.dumps([entry.model_dump() for entry in run.log], ensure_ascii=False),
                run.id,
            ),
        )

    def get(self, run_id: int) -> Run | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def confirmed_on_local_date(self, kind: RunKind, local_date: date, tz: ZoneInfo) -> Run | None:
        """The run of `kind` whose delivery was confirmed on the given local date, if any.

        Keyed on `telegram_confirmed_at`, not `started_at`: a batch prepared on Friday
        but delivered by Monday's retry counts as Monday's digest. Scoping by local
        date rather than UTC is what makes "one digest per working day" mean the same
        thing on both sides of a DST change.
        """
        start_local = datetime.combine(local_date, datetime.min.time(), tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        row = self.conn.execute(
            "SELECT * FROM runs WHERE kind = ?"
            " AND telegram_confirmed_at >= ? AND telegram_confirmed_at < ?"
            " ORDER BY telegram_confirmed_at DESC LIMIT 1",
            (kind.value, _dump_dt(start_local), _dump_dt(end_local)),
        ).fetchone()
        return self._row_to_run(row) if row else None

    def unconfirmed_since(self, kind: RunKind, start: datetime) -> list[Run]:
        """Runs of `kind` started at or after `start` whose delivery was never confirmed.

        Oldest first, so a backlog of failed batches is resent in the order it was
        selected.
        """
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE kind = ? AND started_at >= ?"
            " AND telegram_confirmed_at IS NULL ORDER BY started_at ASC",
            (kind.value, _dump_dt(start)),
        ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def prune_before(self, cutoff: datetime) -> int:
        cursor = self.conn.execute("DELETE FROM runs WHERE started_at < ?", (_dump_dt(cutoff),))
        return cursor.rowcount

    def latest(self, kind: RunKind) -> Run | None:
        """The most recently started run of `kind`, whatever its outcome."""
        row = self.conn.execute(
            "SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC LIMIT 1", (kind.value,)
        ).fetchone()
        return self._row_to_run(row) if row else None

    def recent(self, limit: int = 20) -> list[Run]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_run(row) for row in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            kind=RunKind(row["kind"]),
            started_at=_require_dt(row["started_at"]),
            finished_at=_load_dt(row["finished_at"]),
            status=RunStatus(row["status"]),
            collected_count=row["collected_count"],
            delivered_count=row["delivered_count"],
            telegram_confirmed_at=_load_dt(row["telegram_confirmed_at"]),
            log=[RunLogEntry.model_validate(e) for e in json.loads(row["log"])],
        )


class SourceStateRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, source_id: str) -> SourceState:
        row = self.conn.execute(
            "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return SourceState(source_id=source_id)
        return SourceState(
            source_id=row["source_id"],
            last_success_at=_load_dt(row["last_success_at"]),
            last_seen_at=_load_dt(row["last_seen_at"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            cursor=row["cursor"],
            block_text=row["block_text"],
            consecutive_failures=row["consecutive_failures"],
            last_error=row["last_error"],
        )

    def save(self, state: SourceState) -> None:
        self.conn.execute(
            "INSERT INTO source_state ("
            " source_id, last_success_at, last_seen_at, etag, last_modified, cursor,"
            " block_text, consecutive_failures, last_error"
            ") VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET"
            " last_success_at = excluded.last_success_at,"
            " last_seen_at = excluded.last_seen_at,"
            " etag = excluded.etag,"
            " last_modified = excluded.last_modified,"
            " cursor = excluded.cursor,"
            " block_text = excluded.block_text,"
            " consecutive_failures = excluded.consecutive_failures,"
            " last_error = excluded.last_error",
            (
                state.source_id,
                _dump_dt(state.last_success_at),
                _dump_dt(state.last_seen_at),
                state.etag,
                state.last_modified,
                state.cursor,
                state.block_text,
                state.consecutive_failures,
                state.last_error,
            ),
        )
