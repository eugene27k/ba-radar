"""Relevance scoring (PRD 2.1; answers doc Q1).

Items are scored in prompt batches of `llm.tasks.relevance.batch_size` — several items
in one request, not the Batches API — inside `collect`, while the excerpts still exist
in memory. The model returns the *base* score and the practice tags; source weight,
indicator and multi-source bonuses are applied by rules afterwards (`adjust.py`).

A phase deadline bounds the whole step. Batches that fail or never start leave their
items unscored; the caller marks the run DEGRADED and the items are re-scored on a
later run if a feed shows them again (DECISIONS.md D-20).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from ba_radar.llm import ENUM_GUIDE, LLMRequest, PromptFile, Provider, ProviderError, TaskRoute
from ba_radar.models import Item, PracticeTag


class RelevanceVerdict(BaseModel):
    """One item's verdict in a scoring reply. `rationale` is logged, never stored."""

    model_config = ConfigDict(extra="forbid")

    key: str
    relevance: int
    tags: list[PracticeTag]
    rationale: str

    @field_validator("relevance", mode="before")
    @classmethod
    def _clamp(cls, value: object) -> int:
        # The wire schema cannot carry a numeric range (neither provider's strict mode
        # allows `minimum`/`maximum`), so an out-of-range value is clamped rather than
        # failing the whole batch.
        try:
            number = round(float(str(value)))
        except ValueError as exc:
            raise ValueError(f"relevance must be a number, got {value!r}") from exc
        return max(0, min(100, number))

    @field_validator("tags", mode="before")
    @classmethod
    def _known_tags(cls, value: object) -> list[str]:
        known = {tag.value for tag in PracticeTag}
        if not isinstance(value, list):
            return []
        seen: list[str] = []
        for raw in value:
            tag = str(raw).strip().lower()
            if tag in known and tag not in seen:
                seen.append(tag)
        return seen


class RelevanceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[RelevanceVerdict]


@dataclass(frozen=True)
class ScoringItem:
    item: Item
    excerpt: str
    source_names: tuple[str, ...]


@dataclass
class ScoringOutcome:
    verdicts: dict[str, RelevanceVerdict] = field(default_factory=dict)  # item id -> verdict
    errors: list[str] = field(default_factory=list)
    skipped: int = 0  # items never attempted because the deadline passed
    failed: int = 0  # items whose batch errored or whose verdict was missing
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def unscored(self) -> int:
        return self.skipped + self.failed


def batch_prompt(batch: list[ScoringItem]) -> tuple[str, list[str]]:
    """The user message for one batch, plus the keys in order."""
    keys = [str(index) for index in range(1, len(batch) + 1)]
    parts = [f"Score the following {len(batch)} item(s).", ""]
    for key, entry in zip(keys, batch, strict=True):
        item = entry.item
        sources = ", ".join(entry.source_names) or "unknown source"
        parts.append(f"### Item {key}")
        parts.append(f"key: {key}")
        parts.append(f"title: {item.title}")
        parts.append(
            f"source: {sources} ({item.category.value}, {item.indicator.value} indicator, "
            f"seen in {item.source_count} source(s))"
        )
        parts.append(f"published: {item.published_at:%Y-%m-%d}")
        excerpt = entry.excerpt.strip()
        parts.append("excerpt:")
        parts.append(excerpt if excerpt else "(no excerpt available; judge by the title)")
        parts.append("")
    return "\n".join(parts).rstrip(), keys


def build_request(
    batch: list[ScoringItem], route: TaskRoute, prompt: PromptFile
) -> LLMRequest[RelevanceBatch]:
    user, keys = batch_prompt(batch)
    return LLMRequest(
        task=route.task,
        model=route.model,
        system=prompt.text + "\n\n" + ENUM_GUIDE,
        user=user,
        output_model=RelevanceBatch,
        max_tokens=route.max_tokens,
        effort=route.effort,
        metadata={
            "keys": keys,
            "item_ids": [entry.item.id for entry in batch],
            "titles": [entry.item.title for entry in batch],
        },
    )


def chunked(items: list[ScoringItem], size: int) -> list[list[ScoringItem]]:
    size = max(1, size)
    return [items[start : start + size] for start in range(0, len(items), size)]


async def score_items(
    items: list[ScoringItem],
    provider: Provider,
    route: TaskRoute,
    prompt: PromptFile,
    *,
    deadline_seconds: float,
    concurrency: int,
) -> ScoringOutcome:
    """Score every item in batches, bounded by one wall-clock deadline for the phase."""
    outcome = ScoringOutcome()
    if not items:
        return outcome

    deadline = time.monotonic() + deadline_seconds
    semaphore = asyncio.Semaphore(max(1, concurrency))
    batches = chunked(items, route.batch_size)

    async def run(batch: list[ScoringItem]) -> tuple[list[ScoringItem], Any]:
        async with semaphore:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return batch, None
            request = build_request(batch, route, prompt)
            try:
                response = await asyncio.wait_for(provider.complete(request), timeout=remaining)
            except TimeoutError:
                return batch, ProviderError(
                    f"{route.task}: batch of {len(batch)} hit the {deadline_seconds:.0f}s "
                    f"phase deadline"
                )
            except ProviderError as exc:
                return batch, exc
            except Exception as exc:  # a bug in an adapter must not kill collection
                return batch, ProviderError(f"{route.task}: {type(exc).__name__}: {exc}")
            return batch, response

    results = await asyncio.gather(*(run(batch) for batch in batches))

    for batch, result in results:
        if result is None:
            outcome.skipped += len(batch)
            continue
        if isinstance(result, ProviderError):
            outcome.errors.append(str(result))
            outcome.failed += len(batch)
            continue
        outcome.requests += 1
        outcome.input_tokens += result.input_tokens
        outcome.output_tokens += result.output_tokens
        by_key = {verdict.key.strip(): verdict for verdict in result.parsed.verdicts}
        for key, entry in zip(
            (str(index) for index in range(1, len(batch) + 1)), batch, strict=True
        ):
            verdict = by_key.get(key)
            if verdict is None:
                outcome.errors.append(f"{route.task}: no verdict returned for '{entry.item.title}'")
                outcome.failed += 1
                continue
            outcome.verdicts[entry.item.id] = verdict
    return outcome
