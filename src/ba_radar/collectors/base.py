"""Collector protocol and registry.

Adding a collection method is one new module plus one `@register` decorator; the
pipeline never changes. Adding a *source* that uses an existing method is a YAML edit
with no code change at all, which is what req. 4.1.6 asks for.

Collectors return `RawItem`s, never `Item`s: canonicalisation, hashing and dedup are
deliberately centralised so five collectors cannot drift into five identity schemes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from ba_radar.collectors.http import HttpFetcher
from ba_radar.models import RawItem, Source, SourceMethod, SourceState
from ba_radar.settings import CollectionSettings


@dataclass
class FetchContext:
    fetcher: HttpFetcher
    settings: CollectionSettings
    since: datetime
    now: datetime
    deadline: float
    github_token: str | None = None


@dataclass
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    state: SourceState | None = None
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class Collector(Protocol):
    method: SourceMethod

    async def fetch(self, source: Source, state: SourceState, ctx: FetchContext) -> FetchResult: ...


_REGISTRY: dict[SourceMethod, Collector] = {}


def register(collector: Collector) -> Collector:
    _REGISTRY[collector.method] = collector
    return collector


def get_collector(method: SourceMethod) -> Collector | None:
    return _REGISTRY.get(method)


def registered_methods() -> list[SourceMethod]:
    return sorted(_REGISTRY, key=lambda m: m.value)
