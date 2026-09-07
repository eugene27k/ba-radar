"""Scoring arithmetic, bands, caps, rollover and the verbatim check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ba_radar.llm import ProviderError, TaskRoute, load_prompt
from ba_radar.models import Category, Indicator, Item, ItemStatus, PracticeTag, Priority
from ba_radar.pipeline import (
    adjusted_score,
    display_order,
    is_relevant,
    priority_for,
    select_digest,
    shares_verbatim_run,
)
from ba_radar.pipeline.analyze import AnalysisItem, analyze_items
from ba_radar.pipeline.score import RelevanceVerdict, ScoringItem, score_items
from ba_radar.settings import Settings

from .fake_llm import FakeProvider

NOW = datetime(2026, 8, 3, 5, 5, tzinfo=UTC)


@pytest.fixture
def cfg() -> Settings:
    return Settings.load()


def make_item(
    item_id: str,
    *,
    score: int | None = 60,
    status: ItemStatus = ItemStatus.ANALYZED,
    weight: int = 5,
    indicator: Indicator = Indicator.MIXED,
    sources: int = 1,
    tags: list[PracticeTag] | None = None,
    collected_at: datetime = NOW,
    published_at: datetime = NOW,
    priority: Priority | None = None,
    adjusted: int | None = None,
) -> Item:
    return Item(
        id=item_id,
        url=f"https://e.com/{item_id}",
        title=f"Item {item_id}",
        title_key=item_id,
        source_count=sources,
        max_source_weight=weight,
        category=Category.PRACTITIONER,
        indicator=indicator,
        published_at=published_at,
        collected_at=collected_at,
        relevance_score=score,
        tags=tags if tags is not None else [PracticeTag.EVALS],
        status=status,
        priority=priority,
        adjusted_score=adjusted,
    )


# ---------------------------------------------------------------------------
# adjusted score and bands
# ---------------------------------------------------------------------------


def test_adjusted_score_adds_weight_and_indicator(cfg: Settings) -> None:
    scoring = cfg.scoring
    common = {"source_count": 1, "has_tags": True, "scoring": scoring}
    assert adjusted_score(50, max_source_weight=10, indicator=Indicator.LEADING, **common) == 65
    assert adjusted_score(50, max_source_weight=10, indicator=Indicator.MIXED, **common) == 60
    assert adjusted_score(50, max_source_weight=10, indicator=Indicator.LAGGING, **common) == 55
    assert adjusted_score(50, max_source_weight=1, indicator=Indicator.MIXED, **common) == 51


def test_multi_source_bonus_applies_at_the_minimum_count(cfg: Settings) -> None:
    scoring = cfg.scoring
    common = {"max_source_weight": 5, "indicator": Indicator.MIXED, "has_tags": True}
    below = adjusted_score(50, source_count=scoring.multi_source_min - 1, scoring=scoring, **common)
    at = adjusted_score(50, source_count=scoring.multi_source_min, scoring=scoring, **common)
    assert at - below == scoring.multi_source_bonus


def test_no_tag_penalty_applies_only_without_tags(cfg: Settings) -> None:
    scoring = cfg.scoring
    common = {"max_source_weight": 5, "indicator": Indicator.MIXED, "source_count": 1}
    tagged = adjusted_score(50, has_tags=True, scoring=scoring, **common)
    untagged = adjusted_score(50, has_tags=False, scoring=scoring, **common)
    assert tagged - untagged == scoring.no_tag_penalty


def test_threshold_and_bands(cfg: Settings) -> None:
    scoring = cfg.scoring
    assert is_relevant(scoring.threshold, scoring)
    assert not is_relevant(scoring.threshold - 1, scoring)
    bands = scoring.bands
    assert priority_for(bands.critical, bands) == Priority.CRITICAL
    assert priority_for(bands.critical - 1, bands) == Priority.NOTABLE
    assert priority_for(bands.notable, bands) == Priority.NOTABLE
    assert priority_for(bands.notable - 1, bands) == Priority.BACKGROUND
    # A relevant item below the background band still lands in a block.
    assert priority_for(bands.background - 1, bands) == Priority.BACKGROUND


def test_relevance_verdict_clamps_scores_and_drops_unknown_tags() -> None:
    verdict = RelevanceVerdict.model_validate(
        {"key": "1", "relevance": 140, "tags": ["evals", "nonsense", "EVALS"], "rationale": "r"}
    )
    assert verdict.relevance == 100
    assert verdict.tags == [PracticeTag.EVALS]
    assert (
        RelevanceVerdict.model_validate(
            {"key": "1", "relevance": -3, "tags": [], "rationale": "r"}
        ).relevance
        == 0
    )


# ---------------------------------------------------------------------------
# verbatim check
# ---------------------------------------------------------------------------

EXCERPT = (
    "Spec-driven development asks the analyst to write the acceptance criteria before "
    "the agent writes a single line of code, and to keep them in the repository."
)


def test_verbatim_run_is_detected_across_punctuation_and_case() -> None:
    copied = (
        "The post argues that spec-driven development asks the Analyst to write the "
        "acceptance criteria before the agent writes a single line of code."
    )
    assert shares_verbatim_run(copied, EXCERPT)


def test_a_paraphrase_is_not_flagged() -> None:
    paraphrase = (
        "Analysts should draft acceptance criteria first and store them next to the "
        "code so agents build against an agreed specification."
    )
    assert not shares_verbatim_run(paraphrase, EXCERPT)
    assert not shares_verbatim_run("too short", EXCERPT)


# ---------------------------------------------------------------------------
# selection under caps
# ---------------------------------------------------------------------------


def test_caps_apply_per_tier_and_best_adjusted_score_wins(cfg: Settings) -> None:
    # weight 10 + leading 5 => adjusted = base + 15; base 60..69 -> critical (>= 75)
    critical = [
        make_item(f"c{i}", score=60 + i, weight=10, indicator=Indicator.LEADING) for i in range(5)
    ]
    notable = [make_item(f"n{i}", score=50, weight=5) for i in range(7)]  # 55 -> notable
    background = [make_item(f"b{i}", score=40, weight=5) for i in range(9)]  # 45 -> background

    selection = select_digest([*critical, *notable, *background], [], cfg, now=NOW)

    caps = cfg.digest.caps
    assert [row.item.id for row in selection.tiers[Priority.CRITICAL]] == ["c4", "c3", "c2"]
    assert len(selection.tiers[Priority.NOTABLE]) == caps.notable
    assert len(selection.tiers[Priority.BACKGROUND]) == caps.background
    assert selection.total == caps.critical + caps.notable + caps.background
    assert all(row.priority == Priority.CRITICAL for row in selection.tiers[Priority.CRITICAL])


def test_rollover_age_excludes_stale_analysed_items(cfg: Settings) -> None:
    stale = make_item("old", collected_at=NOW - timedelta(hours=cfg.digest.rollover_hours + 1))
    fresh = make_item("new", collected_at=NOW - timedelta(hours=cfg.digest.rollover_hours - 1))
    selection = select_digest([stale, fresh], [], cfg, now=NOW)
    assert [row.item.id for row in selection.ordered] == ["new"]


def test_filtered_items_are_never_selected(cfg: Settings) -> None:
    filtered = make_item("f", score=10, status=ItemStatus.FILTERED)
    selection = select_digest([filtered], [filtered], cfg, now=NOW)
    assert selection.total == 0


def test_unscored_items_land_in_the_fallback_block_scored_first(cfg: Settings) -> None:
    cfg.digest.unscored_max_items = 2
    never_scored = make_item("u1", score=None, status=ItemStatus.COLLECTED, weight=9)
    scored_not_analysed = make_item("u2", score=70, status=ItemStatus.COLLECTED, weight=3)
    another = make_item("u3", score=None, status=ItemStatus.COLLECTED, weight=4)

    selection = select_digest([], [never_scored, scored_not_analysed, another], cfg, now=NOW)

    assert [row.item.id for row in selection.unscored] == ["u2", "u1"]
    assert all(row.priority is None for row in selection.unscored)
    assert selection.ordered[-1].item.id == "u1"


def test_display_order_uses_persisted_tiers(cfg: Settings) -> None:
    items = [
        make_item("bg", priority=Priority.BACKGROUND, adjusted=45),
        make_item("un", priority=None, adjusted=None),
        make_item("cr", priority=Priority.CRITICAL, adjusted=80),
        make_item("nt2", priority=Priority.NOTABLE, adjusted=60),
        make_item("nt1", priority=Priority.NOTABLE, adjusted=70),
    ]
    assert [row.item.id for row in display_order(items, cfg)] == ["cr", "nt1", "nt2", "bg", "un"]


# ---------------------------------------------------------------------------
# scoring and analysis phases with the fake provider
# ---------------------------------------------------------------------------


def scoring_items(count: int) -> list[ScoringItem]:
    return [
        ScoringItem(
            item=make_item(f"s{i}", score=None, status=ItemStatus.COLLECTED),
            excerpt=f"excerpt {i}",
            source_names=("Source One",),
        )
        for i in range(count)
    ]


def route(task: str = "relevance", *, batch_size: int = 20, max_tokens: int = 4000) -> TaskRoute:
    return TaskRoute(
        task=task,
        provider="fake",
        model="fake-model",
        max_tokens=max_tokens,
        batch_size=batch_size,
        effort=None,
    )


async def test_scoring_batches_by_batch_size() -> None:
    fake = FakeProvider()
    outcome = await score_items(
        scoring_items(45),
        fake,
        route(batch_size=20),
        load_prompt("relevance"),
        deadline_seconds=30,
        concurrency=4,
    )
    assert fake.calls["relevance"] == 3
    assert len(outcome.verdicts) == 45 and outcome.unscored == 0
    assert outcome.input_tokens == 300
    first = fake.requests[0]
    assert first.metadata["keys"][:2] == ["1", "2"]
    assert "### Item 1" in first.user and "excerpt 0" in first.user


async def test_a_failing_provider_leaves_the_batch_unscored() -> None:
    fake = FakeProvider(fail=ProviderError("boom"))
    outcome = await score_items(
        scoring_items(3),
        fake,
        route(),
        load_prompt("relevance"),
        deadline_seconds=30,
        concurrency=2,
    )
    assert outcome.verdicts == {} and outcome.unscored == 3
    assert outcome.errors == ["boom"]


async def test_the_phase_deadline_bounds_a_slow_provider() -> None:
    fake = FakeProvider(delay=0.5)
    outcome = await score_items(
        scoring_items(4),
        fake,
        route(batch_size=1),
        load_prompt("relevance"),
        deadline_seconds=0.05,
        concurrency=1,
    )
    assert outcome.verdicts == {}
    assert outcome.unscored == 4
    assert any("deadline" in error for error in outcome.errors)


async def test_a_missing_verdict_counts_as_unscored() -> None:
    from ba_radar.pipeline.score import RelevanceBatch

    def drop_last(request: object) -> RelevanceBatch:
        return RelevanceBatch(
            verdicts=[RelevanceVerdict(key="1", relevance=55, tags=[], rationale="only one")]
        )

    fake = FakeProvider(scripts={"relevance": drop_last})
    outcome = await score_items(
        scoring_items(2),
        fake,
        route(),
        load_prompt("relevance"),
        deadline_seconds=30,
        concurrency=1,
    )
    assert list(outcome.verdicts) == ["s0"]
    assert outcome.unscored == 1


async def test_analysis_runs_one_request_per_item_within_the_deadline() -> None:
    fake = FakeProvider()
    entries = [
        AnalysisItem(
            item=make_item(f"a{i}", status=ItemStatus.COLLECTED),
            excerpt="text",
            source_names=("Source One",),
            base_score=60,
            adjusted_score=70,
            tags=(PracticeTag.EVALS,),
        )
        for i in range(3)
    ]
    outcome = await analyze_items(
        entries,
        fake,
        route("analysis"),
        load_prompt("analysis"),
        deadline_seconds=30,
        concurrency=2,
    )
    assert fake.calls["analysis"] == 3
    assert set(outcome.verdicts) == {"a0", "a1", "a2"}
    assert "relevance: 60 (adjusted 70)" in fake.requests[0].user
