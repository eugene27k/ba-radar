"""Domain model for BA Radar.

Enum values are English keys everywhere in code, config, storage and model output.
Ukrainian display labels live in `ba_radar.labels` and are applied at render time only
(answers doc Q19).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class SourceMethod(StrEnum):
    RSS = "rss"
    GITHUB_RELEASES = "github_releases"
    GITHUB_COMMITS_PATH = "github_commits_path"
    HTML_DIFF = "html_diff"
    HN_ALGOLIA = "hn_algolia"


class Category(StrEnum):
    VENDOR = "vendor"
    PRACTITIONER = "practitioner"
    NEWSLETTER = "newsletter"
    RESEARCH = "research"
    COMMUNITY = "community"
    BA_SOURCE = "ba_source"
    REGIONAL = "regional"


class Indicator(StrEnum):
    LEADING = "leading"
    LAGGING = "lagging"
    MIXED = "mixed"


class PracticeTag(StrEnum):
    SPECIFICATIONS = "specifications"
    CONTEXT_ENGINEERING = "context_engineering"
    AGENT_ORCHESTRATION = "agent_orchestration"
    EVALS = "evals"
    TOOL_RELEASE = "tool_release"
    ROLE_CHANGE = "role_change"
    EVIDENCE_CRITIQUE = "evidence_critique"
    SECURITY_QUALITY = "security_quality"


class Priority(StrEnum):
    CRITICAL = "critical"
    NOTABLE = "notable"
    BACKGROUND = "background"


class Action(StrEnum):
    TRY = "try"
    READ = "read"
    NOTE = "note"


class ItemStatus(StrEnum):
    COLLECTED = "collected"
    FILTERED = "filtered"
    ANALYZED = "analyzed"
    DELIVERED = "delivered"


class RunStatus(StrEnum):
    # RUNNING is not one of the PRD's three statuses; it is an in-flight marker so a
    # crashed run is distinguishable from a completed one. See DECISIONS.md D-06.
    RUNNING = "running"
    SUCCESS = "success"
    DEGRADED = "degraded"
    FAILED = "failed"


class RunKind(StrEnum):
    COLLECT = "collect"
    DIGEST = "digest"
    WEEKLY = "weekly"


# Required fields per collection method, in addition to the universal set.
# Answers doc Q3 — replaces the flat list in PRD req. 4.1.2.
METHOD_REQUIRED_FIELDS: dict[SourceMethod, tuple[str, ...]] = {
    SourceMethod.RSS: ("url",),
    SourceMethod.GITHUB_RELEASES: ("repo",),
    SourceMethod.GITHUB_COMMITS_PATH: ("repo", "path"),
    SourceMethod.HTML_DIFF: ("url", "selector"),
    SourceMethod.HN_ALGOLIA: ("query", "min_points"),
}

UNIVERSAL_REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "method",
    "category",
    "indicator",
    "weight",
    "active",
)


class Source(BaseModel):
    """One record of the source registry (`config/sources.yaml`)."""

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    method: SourceMethod
    category: Category
    indicator: Indicator
    weight: int = Field(ge=1, le=10)
    active: bool

    # Method-specific. Presence is enforced by `_check_method_fields` below.
    url: str | None = None
    repo: str | None = None
    path: str | None = None
    selector: str | None = None
    ignore_selectors: list[str] = Field(default_factory=list)
    # Algolia's `query` is full-text, not boolean: "a OR b" searches for the literal
    # word "OR" and matches nothing. A list issues one search per term and merges the
    # results. See DECISIONS.md D-09.
    query: str | list[str] | None = None
    min_points: int | None = None

    @property
    def queries(self) -> list[str]:
        if self.query is None:
            return []
        if isinstance(self.query, str):
            return [self.query]
        return list(self.query)

    @model_validator(mode="after")
    def _check_method_fields(self) -> Source:
        missing = [
            field
            for field in METHOD_REQUIRED_FIELDS[self.method]
            if _is_blank(getattr(self, field, None))
        ]
        if missing:
            raise ValueError(f"method '{self.method}' requires {', '.join(missing)}")
        return self


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not value
    return False


class RawItem(BaseModel):
    """A single item as returned by a collector, before normalisation.

    `excerpt` exists only here and in memory for the duration of a run. It is never
    persisted (answers doc §0) — that keeps the committed DB small and means no
    copyrighted text is retained at rest.
    """

    url: str
    title: str
    published_at: datetime | None = None
    excerpt: str = ""
    external_id: str | None = None


class Item(BaseModel):
    """A normalised, deduplicated material. Mirrors PRD §5.3."""

    id: str
    url: str
    title: str
    title_key: str
    source_ids: list[str] = Field(default_factory=list)
    source_count: int = 1
    # Highest weight among the sources this item was seen in. PRD 2.1.6 assumes a
    # single source; this is the multi-source reading. See DECISIONS.md D-04.
    max_source_weight: int = 1
    category: Category
    indicator: Indicator
    published_at: datetime
    collected_at: datetime

    relevance_score: int | None = None
    tags: list[PracticeTag] = Field(default_factory=list)
    priority: Priority | None = None
    suggested_priority: Priority | None = None
    summary: str | None = None
    ba_insight: str | None = None
    action: Action | None = None

    status: ItemStatus = ItemStatus.COLLECTED
    verbatim_flag: bool = False
    delivered_run_id: int | None = None
    delivered_at: datetime | None = None
    pruned: bool = False

    # Stage 2 (migration 002). `relevance_score` above is the model's base score;
    # `adjusted_score` is the rule-adjusted value persisted at scoring and again at
    # selection. The provenance trio is for Stage 5 calibration.
    adjusted_score: int | None = None
    scored_at: datetime | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    prompt_version: str | None = None


class RunLogEntry(BaseModel):
    level: str  # "error" | "warning" | "info"
    source_id: str | None = None
    message: str


class Run(BaseModel):
    """PRD §5.3 «Запуск», plus `telegram_confirmed_at` per answers doc Q14."""

    id: int | None = None
    kind: RunKind
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.RUNNING
    collected_count: int = 0
    delivered_count: int = 0
    telegram_confirmed_at: datetime | None = None
    log: list[RunLogEntry] = Field(default_factory=list)

    @property
    def errors(self) -> list[RunLogEntry]:
        return [e for e in self.log if e.level == "error"]

    @property
    def warnings(self) -> list[RunLogEntry]:
        return [e for e in self.log if e.level == "warning"]

    @property
    def infos(self) -> list[RunLogEntry]:
        return [e for e in self.log if e.level == "info"]


class SourceState(BaseModel):
    """Per-source incremental cursor. The `cursor` field is method-private."""

    source_id: str
    last_success_at: datetime | None = None
    last_seen_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    cursor: str | None = None
    block_text: str | None = None  # html_diff only (Stage 3)
    consecutive_failures: int = 0
    last_error: str | None = None
