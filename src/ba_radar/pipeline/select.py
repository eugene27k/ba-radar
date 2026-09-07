"""Digest selection under the tier caps (PRD 2.3.2–2.3.4).

Runs at `prepare-digest` time on persisted data. The adjusted score is recomputed
here from the base score and the *current* source aggregates, so an item that was
merged after scoring still benefits from the multi-source bonus (DECISIONS.md D-19).

Two pools:

* ANALYZED items — sorted into Critical / Notable / Background by `scoring.bands`,
  best adjusted score first inside a tier, cut at `digest.caps`. Unselected items
  roll over to later digests until they are `digest.rollover_hours` old.
* COLLECTED items — anything a failed or timed-out provider left without a verdict.
  They go to the final «Без аналізу» block, capped by `digest.unscored_max_items`,
  so a provider outage is visible in the channel and nothing is dropped silently
  (DECISIONS.md D-20, D-21).

FILTERED items are never in either pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ba_radar.models import Item, ItemStatus, Priority
from ba_radar.pipeline.adjust import adjusted_for_item, priority_for
from ba_radar.settings import Settings

TIER_ORDER: tuple[Priority, ...] = (Priority.CRITICAL, Priority.NOTABLE, Priority.BACKGROUND)


@dataclass(frozen=True)
class Ranked:
    item: Item
    adjusted: int | None
    priority: Priority | None  # None -> the unscored block


@dataclass
class DigestSelection:
    tiers: dict[Priority, list[Ranked]] = field(
        default_factory=lambda: {tier: [] for tier in TIER_ORDER}
    )
    unscored: list[Ranked] = field(default_factory=list)

    @property
    def ordered(self) -> list[Ranked]:
        """Display order: tier blocks first, the unscored block last."""
        rows: list[Ranked] = []
        for tier in TIER_ORDER:
            rows.extend(self.tiers[tier])
        rows.extend(self.unscored)
        return rows

    @property
    def total(self) -> int:
        return len(self.ordered)


def _rank_key(row: Ranked) -> tuple[int, int, float]:
    return (
        -(row.adjusted if row.adjusted is not None else -1),
        -row.item.max_source_weight,
        -row.item.published_at.timestamp(),
    )


def select_digest(
    analyzed: list[Item], unscored: list[Item], settings: Settings, *, now: datetime
) -> DigestSelection:
    cutoff = now - timedelta(hours=settings.digest.rollover_hours)
    selection = DigestSelection()

    ranked: dict[Priority, list[Ranked]] = {tier: [] for tier in TIER_ORDER}
    for item in analyzed:
        if item.status != ItemStatus.ANALYZED or item.collected_at < cutoff:
            continue
        adjusted = adjusted_for_item(item, settings.scoring)
        if adjusted is None:
            continue
        tier = priority_for(adjusted, settings.scoring.bands)
        ranked[tier].append(Ranked(item=item, adjusted=adjusted, priority=tier))

    caps = settings.digest.caps
    for tier, cap in (
        (Priority.CRITICAL, caps.critical),
        (Priority.NOTABLE, caps.notable),
        (Priority.BACKGROUND, caps.background),
    ):
        rows = sorted(ranked[tier], key=_rank_key)
        selection.tiers[tier] = rows[:cap]

    fallback = [
        Ranked(item=item, adjusted=adjusted_for_item(item, settings.scoring), priority=None)
        for item in unscored
        if item.status == ItemStatus.COLLECTED and item.collected_at >= cutoff
    ]
    fallback.sort(key=_rank_key)
    selection.unscored = fallback[: settings.digest.unscored_max_items]
    return selection


def display_order(items: list[Item], settings: Settings) -> list[Ranked]:
    """Order a delivered batch for rendering: by persisted tier, then adjusted score.

    Used when resending a prepared batch, where the tier and adjusted score were
    persisted at prepare time and must not be recomputed.
    """
    rows = [
        Ranked(item=item, adjusted=item.adjusted_score, priority=item.priority) for item in items
    ]
    rank = {tier: index for index, tier in enumerate(TIER_ORDER)}
    return sorted(
        rows,
        key=lambda row: (
            rank.get(row.priority, len(TIER_ORDER)) if row.priority else len(TIER_ORDER),
            *_rank_key(row),
        ),
    )
