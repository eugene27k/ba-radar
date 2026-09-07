"""Scoring, analysis and prioritisation (Stage 2).

`enrich` runs inside `collect` while the excerpts exist; `select_digest` runs at
`prepare-digest` time on persisted verdicts. `adjust` holds the arithmetic both share.
"""

from ba_radar.pipeline.adjust import adjusted_for_item, adjusted_score, is_relevant, priority_for
from ba_radar.pipeline.analyze import AnalysisVerdict, shares_verbatim_run
from ba_radar.pipeline.enrich import EnrichSummary, enrich
from ba_radar.pipeline.score import RelevanceBatch, RelevanceVerdict, ScoringItem
from ba_radar.pipeline.select import (
    TIER_ORDER,
    DigestSelection,
    Ranked,
    display_order,
    select_digest,
)

__all__ = [
    "TIER_ORDER",
    "AnalysisVerdict",
    "DigestSelection",
    "EnrichSummary",
    "Ranked",
    "RelevanceBatch",
    "RelevanceVerdict",
    "ScoringItem",
    "adjusted_for_item",
    "adjusted_score",
    "display_order",
    "enrich",
    "is_relevant",
    "priority_for",
    "select_digest",
    "shares_verbatim_run",
]
