"""Deduplication (PRD req. 1.2, answers doc Q12)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from ba_radar.dedupe import Candidate, Deduplicator
from ba_radar.models import RawItem
from ba_radar.normalize import canonical_url, item_id, title_key
from ba_radar.settings import Settings
from ba_radar.store import ItemRepo

from .conftest import make_source

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
CFG = Settings.load().normalize


def candidate(url: str, title: str, source_id: str = "s1", weight: int = 5) -> Candidate:
    source = make_source(source_id, name=f"Source {source_id}", weight=weight)
    canonical = canonical_url(url, CFG)
    return Candidate(
        raw=RawItem(url=url, title=title, published_at=NOW),
        source=source,
        canonical_url=canonical,
        item_id=item_id(canonical),
        title_key=title_key(title, source.name),
        published_at=NOW,
    )


def test_same_url_is_never_a_second_row(conn: sqlite3.Connection) -> None:
    """req. 1.2.2 — an item already in the store is not added again."""
    repo = ItemRepo(conn)
    deduper = Deduplicator(repo, title_window_days=14, now=NOW)

    first = deduper.ingest([candidate("https://example.com/post", "A post")])
    second = deduper.ingest([candidate("https://example.com/post", "A post")])

    assert len(first.created) == 1
    assert len(second.created) == 0
    assert len(second.merged) == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_tracking_variant_of_the_same_url_merges(conn: sqlite3.Connection) -> None:
    repo = ItemRepo(conn)
    deduper = Deduplicator(repo, title_window_days=14, now=NOW)

    deduper.ingest([candidate("https://example.com/post", "A post")])
    result = deduper.ingest([candidate("https://www.example.com/post/?utm_source=nl", "A post")])

    assert len(result.merged) == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_same_title_from_a_different_source_merges_and_counts(
    conn: sqlite3.Connection,
) -> None:
    """The merge is what makes source_count meaningful for the Stage 2 bonus."""
    repo = ItemRepo(conn)
    deduper = Deduplicator(repo, title_window_days=14, now=NOW)

    deduper.ingest([candidate("https://a.example/x", "Spec-driven agents", "s1", weight=4)])
    deduper.ingest([candidate("https://b.example/y", "Spec-driven agents", "s2", weight=9)])
    deduper.ingest([candidate("https://c.example/z", "Spec-driven agents", "s3", weight=2)])

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    row = conn.execute("SELECT source_count, max_source_weight FROM items").fetchone()
    assert row["source_count"] == 3
    # PRD 2.1.6 adds "the source weight"; with several sources we take the highest.
    assert row["max_source_weight"] == 9


def test_the_existing_row_wins_so_order_does_not_matter(conn: sqlite3.Connection) -> None:
    repo = ItemRepo(conn)
    deduper = Deduplicator(repo, title_window_days=14, now=NOW)

    deduper.ingest([candidate("https://first.example/x", "Shared headline")])
    deduper.ingest([candidate("https://second.example/y", "Shared headline", "s2")])

    url = conn.execute("SELECT url FROM items").fetchone()["url"]
    assert url == "https://first.example/x"


def test_title_match_expires_outside_the_window(conn: sqlite3.Connection) -> None:
    """Otherwise a recurring title like 'Weekly Update' is suppressed forever."""
    repo = ItemRepo(conn)

    old = Deduplicator(repo, title_window_days=14, now=NOW - timedelta(days=30))
    old.ingest([candidate("https://example.com/weekly-1", "Weekly Update")])

    recent = Deduplicator(repo, title_window_days=14, now=NOW)
    result = recent.ingest([candidate("https://example.com/weekly-2", "Weekly Update")])

    assert len(result.created) == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2


def test_no_duplicate_status_is_ever_written(conn: sqlite3.Connection) -> None:
    """Merging replaces the PRD's implied «Дублікат» status, which does not exist."""
    repo = ItemRepo(conn)
    deduper = Deduplicator(repo, title_window_days=14, now=NOW)
    deduper.ingest([candidate("https://a.example/x", "Same")])
    deduper.ingest([candidate("https://b.example/y", "Same", "s2")])

    statuses = {r["status"] for r in conn.execute("SELECT status FROM items")}
    assert statuses == {"collected"}


def test_distinct_titles_and_urls_stay_distinct(conn: sqlite3.Connection) -> None:
    repo = ItemRepo(conn)
    deduper = Deduplicator(repo, title_window_days=14, now=NOW)
    result = deduper.ingest(
        [
            candidate("https://example.com/a", "First article"),
            candidate("https://example.com/b", "Second article"),
        ]
    )
    assert len(result.created) == 2
