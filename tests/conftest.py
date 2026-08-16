from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ba_radar.models import Category, Indicator, Source, SourceMethod
from ba_radar.settings import Settings
from ba_radar.store import connect

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cfg = Settings.load()
    cfg.db_path = tmp_path / "test.sqlite"
    cfg.sources_path = tmp_path / "sources.yaml"
    return cfg


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.sqlite")
    yield connection
    connection.close()


def make_source(
    source_id: str = "s1",
    *,
    name: str = "Source One",
    method: SourceMethod = SourceMethod.RSS,
    weight: int = 5,
    url: str | None = "https://example.com/feed",
    **overrides: object,
) -> Source:
    payload: dict[str, object] = {
        "id": source_id,
        "name": name,
        "method": method,
        "category": Category.PRACTITIONER,
        "indicator": Indicator.LEADING,
        "weight": weight,
        "active": True,
        "url": url,
    }
    payload.update(overrides)
    return Source.model_validate(payload)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
