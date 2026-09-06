"""Configuration loading.

Two sources, deliberately separated:
  * `config/settings.yaml` — behaviour, thresholds, model routing. Committed.
  * environment variables  — secrets only. Never committed (PRD §8).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
DEFAULT_SOURCES_PATH = REPO_ROOT / "config" / "sources.yaml"
DEFAULT_DB_PATH = REPO_ROOT / "state" / "ba_radar.sqlite"


class ScheduleSettings(BaseModel):
    # Target hours are deliberately NOT settings: they live in the workflow files,
    # next to the cron lines that must agree with them. A configurable hour here
    # would silently stop matching the cron and every gate would say "skip".
    timezone: str = "Europe/Kyiv"
    gate_tolerance_minutes: int = 59


class CollectionSettings(BaseModel):
    concurrency: int = 10
    request_timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_interval_seconds: float = 5.0
    per_source_deadline_seconds: float = 60.0
    max_items_per_source: int = 20
    default_lookback_hours: int = 48
    max_lookback_hours: int = 72
    cursor_overlap_hours: int = 24
    hn_requery_hours: int = 72
    global_item_cap: int = 300
    user_agent: str = "ba-radar/0.1"
    host_min_interval_seconds: dict[str, float] = Field(default_factory=dict)


class NormalizeSettings(BaseModel):
    excerpt_max_chars: int = 2000
    strip_params_exact: list[str] = Field(default_factory=list)
    strip_params_prefix: list[str] = Field(default_factory=list)
    force_https_hosts: list[str] = Field(default_factory=list)
    resolve_redirect_hosts: list[str] = Field(default_factory=list)
    redirect_max_hops: int = 3
    redirect_timeout_seconds: float = 5.0


class DedupeSettings(BaseModel):
    title_window_days: int = 14


class IndicatorBonus(BaseModel):
    leading: int = 5
    mixed: int = 0
    lagging: int = -5


class ScoreBands(BaseModel):
    critical: int = 75
    notable: int = 55
    background: int = 40


class ScoringSettings(BaseModel):
    threshold: int = 40
    no_tag_penalty: int = 20
    multi_source_bonus: int = 15
    multi_source_min: int = 3
    indicator_bonus: IndicatorBonus = Field(default_factory=IndicatorBonus)
    bands: ScoreBands = Field(default_factory=ScoreBands)
    analysis_cap: int = 40


class DigestCaps(BaseModel):
    critical: int = 3
    notable: int = 5
    background: int = 7


class DigestSettings(BaseModel):
    caps: DigestCaps = Field(default_factory=DigestCaps)
    stage1_max_items: int = 30
    telegram_max_chars: int = 4096
    pending_resend_days: int = 3


class RetentionSettings(BaseModel):
    full_rows_days: int = 90
    runs_days: int = 90


class LLMTask(BaseModel):
    model: str
    max_tokens: int
    batch_size: int | None = None
    effort: str | None = None


class LLMSettings(BaseModel):
    provider: str = "anthropic"
    tasks: dict[str, LLMTask] = Field(default_factory=dict)


class Settings(BaseModel):
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    collection: CollectionSettings = Field(default_factory=CollectionSettings)
    normalize: NormalizeSettings = Field(default_factory=NormalizeSettings)
    dedupe: DedupeSettings = Field(default_factory=DedupeSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    digest: DigestSettings = Field(default_factory=DigestSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)

    # Paths — overridable for tests, not part of the YAML.
    sources_path: Path = DEFAULT_SOURCES_PATH
    db_path: Path = DEFAULT_DB_PATH

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or DEFAULT_SETTINGS_PATH
        raw: dict[str, Any] = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if loaded:
                raw = loaded
        return cls.model_validate(raw)


class Secrets(BaseModel):
    """Read from the environment at the point of use, never stored or logged."""

    telegram_bot_token: str
    telegram_chat_id: str
    github_token: str | None = None
    anthropic_api_key: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        missing = [
            name for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(f"missing required environment variable(s): {', '.join(missing)}")
        return cls(
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        )


def github_token() -> str | None:
    """GitHub token for the REST collectors.

    Unauthenticated GitHub is 60 requests/hour, which is not enough once the registry
    grows past ~15 repos, so collection authenticates when a token is available but
    does not require one.
    """
    return os.environ.get("GITHUB_TOKEN") or None
