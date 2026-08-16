"""Source registry validation (PRD req. 4.1, answers doc Q3)."""

from __future__ import annotations

from pathlib import Path

from ba_radar.registry import load_registry
from ba_radar.settings import DEFAULT_SOURCES_PATH

VALID_RSS = """
- id: good
  name: Good Source
  method: rss
  url: https://example.com/feed
  category: practitioner
  indicator: leading
  weight: 8
  active: true
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_registry_loads(tmp_path: Path) -> None:
    result = load_registry(write(tmp_path, VALID_RSS))
    assert not result.errors
    assert [s.id for s in result.sources] == ["good"]


def test_missing_universal_field_skips_only_that_record(tmp_path: Path) -> None:
    """req. 4.1.4 — a bad record is skipped and logged; the run still proceeds."""
    text = (
        VALID_RSS
        + """
- id: broken
  method: rss
  url: https://example.com/other
  category: practitioner
  indicator: leading
  weight: 5
  active: true
"""
    )
    result = load_registry(write(tmp_path, text))
    assert [s.id for s in result.sources] == ["good"]
    assert len(result.errors) == 1
    assert "broken" in result.errors[0]
    assert "name" in result.errors[0]


def test_per_method_required_fields(tmp_path: Path) -> None:
    """The PRD's flat 'address or repository' rule rejected its own hn_algolia example."""
    text = """
- id: hn
  name: Hacker News
  method: hn_algolia
  query: ['"MCP"']
  min_points: 50
  category: community
  indicator: leading
  weight: 6
  active: true

- id: rss-without-url
  name: No URL
  method: rss
  category: practitioner
  indicator: leading
  weight: 5
  active: true

- id: releases-without-repo
  name: No Repo
  method: github_releases
  url: https://github.com/acme/tool
  category: vendor
  indicator: leading
  weight: 5
  active: true
"""
    result = load_registry(write(tmp_path, text))

    # hn_algolia is valid with query + min_points and no url/repo at all.
    assert [s.id for s in result.sources] == ["hn"]
    assert len(result.errors) == 2
    assert any("rss-without-url" in e and "url" in e for e in result.errors)
    assert any("releases-without-repo" in e and "repo" in e for e in result.errors)


def test_duplicate_id_keeps_the_first(tmp_path: Path) -> None:
    """req. 4.1.5 — first record wins, and the collision is logged."""
    text = (
        VALID_RSS
        + """
- id: good
  name: Impostor
  method: rss
  url: https://impostor.example.com/feed
  category: vendor
  indicator: lagging
  weight: 1
  active: true
"""
    )
    result = load_registry(write(tmp_path, text))
    assert len(result.sources) == 1
    assert result.sources[0].name == "Good Source"
    assert any("duplicate id" in e for e in result.errors)


def test_weight_must_be_within_range(tmp_path: Path) -> None:
    text = VALID_RSS.replace("weight: 8", "weight: 42")
    result = load_registry(write(tmp_path, text))
    assert not result.sources
    assert any("weight" in e for e in result.errors)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    """A typo'd key should fail loudly rather than be silently ignored."""
    text = VALID_RSS + "  typo_field: oops\n"
    result = load_registry(write(tmp_path, text))
    assert not result.sources
    assert any("typo_field" in e for e in result.errors)


def test_inactive_sources_are_excluded_from_active(tmp_path: Path) -> None:
    """req. 1.1.2 — only active records are processed."""
    text = VALID_RSS.replace("active: true", "active: false")
    result = load_registry(write(tmp_path, text))
    assert len(result.sources) == 1
    assert result.active == []


def test_missing_file_is_an_error_not_a_crash(tmp_path: Path) -> None:
    result = load_registry(tmp_path / "nope.yaml")
    assert result.sources == []
    assert result.errors


def test_malformed_yaml_is_an_error_not_a_crash(tmp_path: Path) -> None:
    result = load_registry(write(tmp_path, "- id: x\n  name: [unclosed\n"))
    assert result.sources == []
    assert result.errors


def test_shipped_registry_is_valid() -> None:
    """The registry committed to this repo must always load cleanly."""
    result = load_registry(DEFAULT_SOURCES_PATH)
    assert result.errors == []
    assert len(result.active) >= 10
