"""Deduplication (PRD req. 1.2, as amended by answers doc Q12).

Two mechanisms, in order:

1. Identity — the item id is the hash of the canonical URL. A second sighting of the
   same URL is never a second row.
2. Title — within a 14-day window, an item whose normalised title matches an existing
   one is *merged into it* rather than stored separately. There is no "duplicate"
   status; merging is what makes `source_count` meaningful, which is what the
   multi-source bonus reads in Stage 2.

The row already in the database always wins, which makes the outcome independent of
the order sources happen to be collected in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ba_radar.models import Item, ItemStatus, RawItem, Source
from ba_radar.store.repo import ItemRepo


@dataclass(frozen=True)
class Candidate:
    """A raw item that has been canonicalised and is ready for the identity check."""

    raw: RawItem
    source: Source
    canonical_url: str
    item_id: str
    title_key: str
    published_at: datetime


@dataclass
class DedupeResult:
    created: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    # Sightings that added nothing: the same source seeing the same item again. The
    # cursor overlap re-reads a slice of every feed on every run, so these are
    # routine and must not inflate the collected/merged counts.
    unchanged: list[str] = field(default_factory=list)
    # Candidate item id -> the id of the row it ended up in. Identical for created
    # rows and identity merges; differs for a title merge. Stage 2 uses it to hand a
    # re-sighted but still unscored row its fresh excerpt (DECISIONS.md D-20).
    row_for: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.merged)


class Deduplicator:
    def __init__(self, items: ItemRepo, *, title_window_days: int, now: datetime) -> None:
        self.items = items
        self.title_window_start = now - timedelta(days=title_window_days)
        self.now = now

    def ingest(self, candidates: list[Candidate]) -> DedupeResult:
        result = DedupeResult()
        for candidate in candidates:
            outcome, row_id = self._merge_if_known(candidate)
            match outcome:
                case "merged":
                    result.merged.append(row_id)
                case "unchanged":
                    result.unchanged.append(row_id)
                case _:
                    self._create(candidate)
                    result.created.append(candidate.item_id)
            result.row_for[candidate.item_id] = row_id
        return result

    def _merge_if_known(self, candidate: Candidate) -> tuple[str | None, str]:
        """(outcome, row id): outcome is "merged", "unchanged" or None for a new row."""
        existing = self.items.get(candidate.item_id)

        if existing is None and candidate.title_key:
            existing = self.items.find_by_title_key(candidate.title_key, self.title_window_start)

        if existing is None:
            return None, candidate.item_id

        # A pruned row is only an identity tombstone: it suppresses re-delivery, but
        # re-linking sources to it would recreate item_sources rows that D-05 says
        # are dropped, for an item that can never be selected again.
        if existing.pruned:
            return "unchanged", existing.id

        if not self.items.link_source(existing.id, candidate.source.id, self.now):
            return "unchanged", existing.id

        self.items.refresh_source_aggregates(existing.id, candidate.source.weight)
        return "merged", existing.id

    def _create(self, candidate: Candidate) -> None:
        item = Item(
            id=candidate.item_id,
            url=candidate.canonical_url,
            title=candidate.raw.title,
            title_key=candidate.title_key,
            source_ids=[candidate.source.id],
            source_count=1,
            max_source_weight=candidate.source.weight,
            category=candidate.source.category,
            indicator=candidate.source.indicator,
            published_at=candidate.published_at,
            collected_at=self.now,
            status=ItemStatus.COLLECTED,
        )
        self.items.insert(item)
