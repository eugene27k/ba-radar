"""The model phases of a `collect` run: score, adjust, filter, analyse, persist.

Called by `tasks.collect` after deduplication, while the excerpts are still in memory.
Only model *outputs* are persisted — the columns already exist on `items`; the
excerpt never reaches the database (answers doc §0).

Each phase has its own transaction so a scoring pass survives an analysis failure,
and each has its own deadline so a hanging provider cannot blow the cycle budget.
Any failure or skipped item makes the run DEGRADED; the caller decides what that
means for the digest (the «Без аналізу» block, DECISIONS.md D-20).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from ba_radar.llm import ProviderPool, estimate_cost, load_prompt
from ba_radar.models import ItemStatus, RunLogEntry
from ba_radar.pipeline.adjust import adjusted_score, is_relevant, priority_for
from ba_radar.pipeline.analyze import AnalysisItem, analyze_items, shares_verbatim_run
from ba_radar.pipeline.score import ScoringItem, score_items
from ba_radar.settings import Settings
from ba_radar.store import ItemRepo, transaction

RELEVANCE = "relevance"
ANALYSIS = "analysis"


@dataclass
class EnrichSummary:
    attempted: int = 0
    scored: int = 0
    filtered: int = 0
    analyzed: int = 0
    unscored: int = 0  # attempted but left without a base score
    unanalyzed: int = 0  # scored above the threshold but left without a verdict
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float | None = None
    degraded: bool = False
    route: str = ""  # "anthropic/claude-sonnet-5" (relevance) — for the run log
    prompt_versions: str = ""
    log: list[RunLogEntry] = field(default_factory=list)

    @property
    def info_message(self) -> str:
        cost = f"${self.estimated_cost:.4f}" if self.estimated_cost is not None else "n/a"
        return (
            f"llm {self.route} [{self.prompt_versions}]: attempted {self.attempted}, "
            f"scored {self.scored} (filtered {self.filtered}, analyzed {self.analyzed}, "
            f"unanalyzed {self.unanalyzed}), unscored {self.unscored}; "
            f"{self.requests} request(s), tokens {self.input_tokens} in / "
            f"{self.output_tokens} out, est. cost {cost}"
        )


async def enrich(
    conn: sqlite3.Connection,
    settings: Settings,
    pool: ProviderPool,
    items: list[ScoringItem],
    *,
    now: datetime,
) -> EnrichSummary:
    summary = EnrichSummary(attempted=len(items))
    relevance_prompt = load_prompt(RELEVANCE)
    analysis_prompt = load_prompt(ANALYSIS)
    summary.prompt_versions = f"{relevance_prompt.name}+{analysis_prompt.name}"

    scorer, relevance_route = pool.for_task(RELEVANCE)
    summary.route = f"{relevance_route.provider}/{relevance_route.model}"
    if not items:
        summary.log.append(RunLogEntry(level="info", message=summary.info_message))
        return summary

    repo = ItemRepo(conn)
    llm = settings.llm

    # --- phase 1: relevance -------------------------------------------------------
    scoring = await score_items(
        items,
        scorer,
        relevance_route,
        relevance_prompt,
        deadline_seconds=llm.phase_deadline_seconds,
        concurrency=llm.concurrency,
    )
    summary.requests += scoring.requests
    summary.input_tokens += scoring.input_tokens
    summary.output_tokens += scoring.output_tokens
    summary.unscored = scoring.unscored
    for error in scoring.errors:
        summary.log.append(RunLogEntry(level="error", message=error))
    if scoring.unscored:
        summary.degraded = True

    candidates: list[AnalysisItem] = []
    by_id = {entry.item.id: entry for entry in items}
    with transaction(conn):
        for item_id, verdict in scoring.verdicts.items():
            entry = by_id[item_id]
            item = entry.item
            adjusted = adjusted_score(
                verdict.relevance,
                max_source_weight=item.max_source_weight,
                indicator=item.indicator,
                source_count=item.source_count,
                has_tags=bool(verdict.tags),
                scoring=settings.scoring,
            )
            relevant = is_relevant(adjusted, settings.scoring)
            repo.apply_score(
                item_id,
                relevance_score=verdict.relevance,
                tags=verdict.tags,
                adjusted_score=adjusted,
                status=ItemStatus.COLLECTED if relevant else ItemStatus.FILTERED,
                scored_at=now,
                llm_provider=relevance_route.provider,
                llm_model=relevance_route.model,
                prompt_version=relevance_prompt.name,
            )
            summary.scored += 1
            if relevant:
                candidates.append(
                    AnalysisItem(
                        item=item,
                        excerpt=entry.excerpt,
                        source_names=entry.source_names,
                        base_score=verdict.relevance,
                        adjusted_score=adjusted,
                        tags=tuple(verdict.tags),
                    )
                )
            else:
                summary.filtered += 1

    # --- phase 2: analysis --------------------------------------------------------
    candidates.sort(key=lambda entry: (-entry.adjusted_score, -entry.item.max_source_weight))
    over_cap = candidates[settings.scoring.analysis_cap :]
    candidates = candidates[: settings.scoring.analysis_cap]
    if over_cap:
        summary.log.append(
            RunLogEntry(
                level="warning",
                message=(
                    f"analysis cap reached: {len(over_cap)} relevant item(s) beyond "
                    f"scoring.analysis_cap={settings.scoring.analysis_cap} left unanalysed"
                ),
            )
        )

    analyst, analysis_route = pool.for_task(ANALYSIS)
    analysis = await analyze_items(
        candidates,
        analyst,
        analysis_route,
        analysis_prompt,
        deadline_seconds=llm.phase_deadline_seconds,
        concurrency=llm.concurrency,
    )
    summary.requests += analysis.requests
    summary.input_tokens += analysis.input_tokens
    summary.output_tokens += analysis.output_tokens
    for error in analysis.errors:
        summary.log.append(RunLogEntry(level="error", message=error))
    if analysis.skipped or analysis.failed:
        summary.degraded = True

    with transaction(conn):
        for candidate in candidates:
            analysed = analysis.verdicts.get(candidate.item.id)
            if analysed is None:
                continue
            flagged = shares_verbatim_run(analysed.summary, candidate.excerpt)
            repo.apply_analysis(
                candidate.item.id,
                summary=analysed.summary,
                ba_insight=analysed.ba_insight,
                action=analysed.action,
                suggested_priority=analysed.suggested_priority,
                verbatim_flag=flagged,
                priority=priority_for(candidate.adjusted_score, settings.scoring.bands),
                prompt_version=f"{relevance_prompt.name}+{analysis_prompt.name}",
            )
            summary.analyzed += 1
    summary.unanalyzed = len(candidates) - summary.analyzed + len(over_cap)

    # A cost estimate needs one price per model; both tasks usually share one.
    cost = 0.0
    priced = True
    for route, tokens_in, tokens_out in (
        (relevance_route, scoring.input_tokens, scoring.output_tokens),
        (analysis_route, analysis.input_tokens, analysis.output_tokens),
    ):
        estimate = estimate_cost(llm, route.model, tokens_in, tokens_out)
        if estimate is None:
            priced = False
            break
        cost += estimate
    summary.estimated_cost = cost if priced else None

    summary.log.append(RunLogEntry(level="info", message=summary.info_message))
    return summary
