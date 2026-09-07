"""A scriptable in-memory provider for tests.

Implements the `Provider` protocol without any network. Default behaviour scores every
item with `default_score` (overridable per title) and returns a canned Ukrainian
verdict for analysis; `scripts` replaces the reply for a task, `fail` raises, and
`delay` makes it slow enough to trip a phase deadline.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ba_radar.llm import LLMRequest, LLMResponse, ProviderPool
from ba_radar.models import Action, PracticeTag, Priority
from ba_radar.pipeline.analyze import AnalysisVerdict
from ba_radar.pipeline.score import RelevanceBatch, RelevanceVerdict
from ba_radar.settings import Settings

Script = Callable[[LLMRequest[Any]], BaseModel]


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        *,
        scores: dict[str, int] | None = None,
        default_score: int = 60,
        tags: list[PracticeTag] | None = None,
        delay: float = 0.0,
        fail: Exception | None = None,
        scripts: dict[str, Script] | None = None,
        input_tokens: int = 100,
        output_tokens: int = 20,
    ) -> None:
        self.scores = scores or {}
        self.default_score = default_score
        self.tags = tags if tags is not None else [PracticeTag.SPECIFICATIONS]
        self.delay = delay
        self.fail = fail
        self.scripts = scripts or {}
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: dict[str, int] = defaultdict(int)
        self.requests: list[LLMRequest[Any]] = []

    async def complete[M: BaseModel](self, request: LLMRequest[M]) -> LLMResponse[M]:
        self.calls[request.task] += 1
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        script = self.scripts.get(request.task)
        reply = script(request) if script else self._default(request)
        parsed = request.output_model.model_validate(reply.model_dump(mode="json"))
        return LLMResponse(
            parsed=parsed,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            model=request.model,
            provider=self.name,
        )

    def _default(self, request: LLMRequest[Any]) -> BaseModel:
        if request.task == "relevance":
            keys = list(request.metadata["keys"])
            titles = list(request.metadata["titles"])
            return RelevanceBatch(
                verdicts=[
                    RelevanceVerdict(
                        key=key,
                        relevance=self.scores.get(title, self.default_score),
                        tags=list(self.tags),
                        rationale=f"fake verdict for {title}",
                    )
                    for key, title in zip(keys, titles, strict=True)
                ]
            )
        if request.task == "analysis":
            title = str(request.metadata["title"])
            return AnalysisVerdict(
                summary=f"Коротко про {title}: що нового і навіщо це читати.",
                ba_insight="Впливає на те, як формулювати критерії приймання для агентів.",
                action=Action.READ,
                suggested_priority=Priority.NOTABLE,
            )
        raise NotImplementedError(f"no script for task {request.task!r}")


def fake_pool(settings: Settings, fake: FakeProvider | None = None) -> ProviderPool:
    """A pool that routes every configured provider name to the same fake."""
    fake = fake or FakeProvider()
    return ProviderPool(settings.llm, providers={"anthropic": fake, "openai": fake})
