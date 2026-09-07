"""Per-item analysis: the verdict a BA acts on (PRD §5.3; answers doc Q8).

Runs inside `collect` for items that passed the threshold, best adjusted score first,
at most `scoring.analysis_cap` per run. One request per item with bounded concurrency
rather than one big batch: a Ukrainian summary plus insight per item is long enough
that a batch would be slow, and one bad reply should cost one item, not twenty
(DECISIONS.md D-27).
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from ba_radar.llm import ENUM_GUIDE, LLMRequest, PromptFile, Provider, ProviderError, TaskRoute
from ba_radar.models import Action, Item, PracticeTag, Priority

VERBATIM_MIN_WORDS = 12


class AnalysisVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    ba_insight: str
    action: Action
    suggested_priority: Priority


@dataclass(frozen=True)
class AnalysisItem:
    item: Item
    excerpt: str
    source_names: tuple[str, ...]
    base_score: int
    adjusted_score: int
    tags: tuple[PracticeTag, ...]


@dataclass
class AnalysisOutcome:
    verdicts: dict[str, AnalysisVerdict] = field(default_factory=dict)  # item id -> verdict
    errors: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def item_prompt(entry: AnalysisItem) -> str:
    item = entry.item
    sources = ", ".join(entry.source_names) or "unknown source"
    tags = ", ".join(tag.value for tag in entry.tags) or "none"
    excerpt = entry.excerpt.strip() or "(no excerpt available; work from the title)"
    return "\n".join(
        [
            f"title: {item.title}",
            f"url: {item.url}",
            f"source: {sources} ({item.category.value}, {item.indicator.value} indicator, "
            f"seen in {item.source_count} source(s))",
            f"published: {item.published_at:%Y-%m-%d}",
            f"tags: {tags}",
            f"relevance: {entry.base_score} (adjusted {entry.adjusted_score})",
            "excerpt:",
            excerpt,
        ]
    )


def build_request(
    entry: AnalysisItem, route: TaskRoute, prompt: PromptFile
) -> LLMRequest[AnalysisVerdict]:
    return LLMRequest(
        task=route.task,
        model=route.model,
        system=prompt.text + "\n\n" + ENUM_GUIDE,
        user=item_prompt(entry),
        output_model=AnalysisVerdict,
        max_tokens=route.max_tokens,
        effort=route.effort,
        metadata={"item_id": entry.item.id, "title": entry.item.title},
    )


async def analyze_items(
    items: list[AnalysisItem],
    provider: Provider,
    route: TaskRoute,
    prompt: PromptFile,
    *,
    deadline_seconds: float,
    concurrency: int,
) -> AnalysisOutcome:
    outcome = AnalysisOutcome()
    if not items:
        return outcome

    deadline = time.monotonic() + deadline_seconds
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(entry: AnalysisItem) -> tuple[AnalysisItem, AnalysisVerdict | Exception | None]:
        async with semaphore:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return entry, None
            request = build_request(entry, route, prompt)
            try:
                response = await asyncio.wait_for(provider.complete(request), timeout=remaining)
            except TimeoutError:
                return entry, ProviderError(
                    f"{route.task}: '{entry.item.title}' hit the {deadline_seconds:.0f}s "
                    f"phase deadline"
                )
            except ProviderError as exc:
                return entry, exc
            except Exception as exc:
                return entry, ProviderError(f"{route.task}: {type(exc).__name__}: {exc}")
            outcome.requests += 1
            outcome.input_tokens += response.input_tokens
            outcome.output_tokens += response.output_tokens
            return entry, response.parsed

    results = await asyncio.gather(*(run(entry) for entry in items))
    for entry, result in results:
        if result is None:
            outcome.skipped += 1
        elif isinstance(result, Exception):
            outcome.errors.append(str(result))
            outcome.failed += 1
        else:
            outcome.verdicts[entry.item.id] = result
    return outcome


# ---------------------------------------------------------------------------
# verbatim check (PRD §8; DECISIONS.md D-22)
# ---------------------------------------------------------------------------

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)


def _words(text: str) -> list[str]:
    normalised = unicodedata.normalize("NFKC", text).casefold()
    return _NON_WORD.sub(" ", normalised).split()


def shares_verbatim_run(summary: str, excerpt: str, *, min_words: int = VERBATIM_MIN_WORDS) -> bool:
    """True when `summary` contains `min_words` consecutive words also consecutive in
    `excerpt`. Punctuation and case are ignored so a copied sentence with a changed
    comma still counts."""
    source = _words(excerpt)
    target = _words(summary)
    if len(source) < min_words or len(target) < min_words:
        return False
    runs = {tuple(source[i : i + min_words]) for i in range(len(source) - min_words + 1)}
    return any(tuple(target[i : i + min_words]) in runs for i in range(len(target) - min_words + 1))
