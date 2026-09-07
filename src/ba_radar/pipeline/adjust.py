"""The adjusted score and the rule-derived priority (answers doc Q7, Q8).

    adjusted = base
             + max_source_weight              (PRD 2.1.6 as read in DECISIONS.md D-04)
             + indicator_bonus[indicator]     (answers doc Q18)
             + multi_source_bonus             if source_count >= multi_source_min (PRD 1.2.5)
             - no_tag_penalty                 if the model assigned no tag (PRD 2.1.7)

Below `scoring.threshold` the item is FILTERED. The tier comes from `scoring.bands`
on the adjusted score; the model's `suggested_priority` is stored for calibration
only (DECISIONS.md D-25).

Pure functions on purpose: the base score is persisted at collect time and the
adjustment is recomputed at prepare time from the current source aggregates, so a
merge that arrives after scoring still raises the bonus (DECISIONS.md D-19).
"""

from __future__ import annotations

from ba_radar.models import Indicator, Item, Priority
from ba_radar.settings import ScoreBands, ScoringSettings


def adjusted_score(
    base: int,
    *,
    max_source_weight: int,
    indicator: Indicator,
    source_count: int,
    has_tags: bool,
    scoring: ScoringSettings,
) -> int:
    bonus = {
        Indicator.LEADING: scoring.indicator_bonus.leading,
        Indicator.MIXED: scoring.indicator_bonus.mixed,
        Indicator.LAGGING: scoring.indicator_bonus.lagging,
    }[indicator]
    total = base + max_source_weight + bonus
    if source_count >= scoring.multi_source_min:
        total += scoring.multi_source_bonus
    if not has_tags:
        total -= scoring.no_tag_penalty
    return total


def adjusted_for_item(item: Item, scoring: ScoringSettings) -> int | None:
    """The adjusted score from an item's persisted base score and current aggregates."""
    if item.relevance_score is None:
        return None
    return adjusted_score(
        item.relevance_score,
        max_source_weight=item.max_source_weight,
        indicator=item.indicator,
        source_count=item.source_count,
        has_tags=bool(item.tags),
        scoring=scoring,
    )


def is_relevant(adjusted: int, scoring: ScoringSettings) -> bool:
    return adjusted >= scoring.threshold


def priority_for(adjusted: int, bands: ScoreBands) -> Priority:
    """Tier for an item that passed the threshold.

    Anything at or above the threshold but below `bands.background` (only possible if
    the two are configured inconsistently) still renders as Background rather than
    vanishing: a relevant item must land in some block.
    """
    if adjusted >= bands.critical:
        return Priority.CRITICAL
    if adjusted >= bands.notable:
        return Priority.NOTABLE
    return Priority.BACKGROUND
