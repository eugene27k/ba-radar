"""Collector behaviour against recorded payloads — no network."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ba_radar.collectors import FetchContext, HttpFetcher, SourceUnavailable
from ba_radar.collectors.github_releases import GitHubReleasesCollector
from ba_radar.collectors.hn_algolia import HnAlgoliaCollector
from ba_radar.collectors.rss import RssCollector
from ba_radar.models import SourceMethod, SourceState
from ba_radar.settings import Settings

from .conftest import fixture_text, make_source

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
CFG = Settings.load()


def context(fetcher: HttpFetcher, since: datetime) -> FetchContext:
    return FetchContext(
        fetcher=fetcher,
        settings=CFG.collection,
        since=since,
        now=NOW,
        deadline=time.monotonic() + 60,
    )


@respx.mock
async def test_rss_collects_new_entries_and_skips_old_ones() -> None:
    respx.get("https://example.com/feed").mock(
        return_value=httpx.Response(200, text=fixture_text("blog.atom"))
    )
    source = make_source("blog", name="Example Practitioner")

    async with HttpFetcher(CFG.collection) as fetcher:
        result = await RssCollector().fetch(
            source, SourceState(source_id="blog"), context(fetcher, NOW - timedelta(days=2))
        )

    titles = [item.title for item in result.items]
    assert "Spec-driven development with agents" in titles
    # req. 1.1.4 — anything at or before the cursor is not collected again.
    assert "An old post that predates the cursor" not in titles


@respx.mock
async def test_rss_undated_entry_is_kept_and_flagged() -> None:
    respx.get("https://example.com/feed").mock(
        return_value=httpx.Response(200, text=fixture_text("blog.atom"))
    )
    async with HttpFetcher(CFG.collection) as fetcher:
        result = await RssCollector().fetch(
            make_source("blog"),
            SourceState(source_id="blog"),
            context(fetcher, NOW - timedelta(days=2)),
        )

    assert "Entry with no date at all" in [i.title for i in result.items]
    assert any("no date" in w for w in result.warnings)


@respx.mock
async def test_rss_html_summary_is_stripped_to_text() -> None:
    respx.get("https://example.com/feed").mock(
        return_value=httpx.Response(200, text=fixture_text("blog.atom"))
    )
    async with HttpFetcher(CFG.collection) as fetcher:
        result = await RssCollector().fetch(
            make_source("blog"),
            SourceState(source_id="blog"),
            context(fetcher, NOW - timedelta(days=2)),
        )

    excerpt = next(i.excerpt for i in result.items if i.title.startswith("Spec-driven"))
    assert "<b>" not in excerpt
    assert "specifications" in excerpt


@respx.mock
async def test_rss_304_yields_nothing_but_still_counts_as_success() -> None:
    respx.get("https://example.com/feed").mock(return_value=httpx.Response(304))
    state = SourceState(source_id="blog", etag='W/"abc"')

    async with HttpFetcher(CFG.collection) as fetcher:
        result = await RssCollector().fetch(
            make_source("blog"), state, context(fetcher, NOW - timedelta(days=2))
        )

    assert result.items == []
    assert result.state is not None
    assert result.state.last_success_at == NOW


@respx.mock
async def test_rss_sends_conditional_headers_when_it_has_them() -> None:
    route = respx.get("https://example.com/feed").mock(return_value=httpx.Response(304))
    state = SourceState(source_id="blog", etag='W/"abc"', last_modified="Mon, 03 Aug 2026")

    async with HttpFetcher(CFG.collection) as fetcher:
        await RssCollector().fetch(
            make_source("blog"), state, context(fetcher, NOW - timedelta(days=2))
        )

    request = route.calls.last.request
    assert request.headers["if-none-match"] == 'W/"abc"'
    assert request.headers["if-modified-since"] == "Mon, 03 Aug 2026"


@respx.mock
async def test_rss_respects_the_per_source_item_cap() -> None:
    entries = "".join(
        f"<entry><title>Post {i}</title>"
        f"<link href='https://example.com/{i}'/><id>{i}</id>"
        f"<published>2026-08-03T09:00:00Z</published></entry>"
        for i in range(50)
    )
    feed = f"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>{entries}</feed>"
    respx.get("https://example.com/feed").mock(return_value=httpx.Response(200, text=feed))

    async with HttpFetcher(CFG.collection) as fetcher:
        result = await RssCollector().fetch(
            make_source("blog"),
            SourceState(source_id="blog"),
            context(fetcher, NOW - timedelta(days=2)),
        )

    assert len(result.items) == CFG.collection.max_items_per_source  # req. 1.1.10


@respx.mock
async def test_github_releases_skips_drafts_and_old_releases() -> None:
    respx.get(url__startswith="https://api.github.com/repos/acme/tool/releases").mock(
        return_value=httpx.Response(200, text=fixture_text("github_releases.json"))
    )
    source = make_source("tool", method=SourceMethod.GITHUB_RELEASES, url=None, repo="acme/tool")

    async with HttpFetcher(CFG.collection) as fetcher:
        result = await GitHubReleasesCollector().fetch(
            source, SourceState(source_id="tool"), context(fetcher, NOW - timedelta(days=2))
        )

    titles = [item.title for item in result.items]
    assert any("2.4.0" in t for t in titles)
    assert any("2.3.1" in t for t in titles)  # prereleases are kept deliberately
    assert not any("draft" in t.lower() for t in titles)
    assert not any("2.0.0" in t for t in titles)  # older than the cursor


@respx.mock
async def test_hn_issues_one_request_per_query_and_merges() -> None:
    """Algolia cannot express OR, so each term is its own search (DECISIONS D-09)."""
    route = respx.get(url__startswith="https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, text=fixture_text("hn_search.json"))
    )
    source = make_source(
        "hn",
        method=SourceMethod.HN_ALGOLIA,
        url=None,
        query=['"Claude Code"', '"MCP"'],
        min_points=50,
    )

    async with HttpFetcher(CFG.collection) as fetcher:
        result = await HnAlgoliaCollector().fetch(
            source, SourceState(source_id="hn"), context(fetcher, NOW - timedelta(days=2))
        )

    assert route.call_count == 2
    # Both searches returned the same two stories; objectID dedup collapses them.
    assert len(result.items) == 2


@respx.mock
async def test_hn_prefers_the_article_url_and_falls_back_to_the_discussion() -> None:
    respx.get(url__startswith="https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, text=fixture_text("hn_search.json"))
    )
    source = make_source(
        "hn", method=SourceMethod.HN_ALGOLIA, url=None, query='"MCP"', min_points=50
    )

    async with HttpFetcher(CFG.collection) as fetcher:
        result = await HnAlgoliaCollector().fetch(
            source, SourceState(source_id="hn"), context(fetcher, NOW - timedelta(days=2))
        )

    by_title = {item.title: item for item in result.items}
    # A link post points at the article, so it can merge with the same article via RSS.
    assert by_title["Show HN: A spec-first workflow for Claude Code"].url.startswith(
        "https://example.dev/spec-first"
    )
    # A text post has no article, so the discussion is the item.
    assert (
        by_title["Ask HN: How are you evaluating MCP servers?"].url
        == "https://news.ycombinator.com/item?id=44000002"
    )
    # The discussion link survives in the excerpt either way.
    assert "news.ycombinator.com/item?id=44000001" in (
        by_title["Show HN: A spec-first workflow for Claude Code"].excerpt
    )


@respx.mock
async def test_retries_then_gives_up_with_a_clear_error() -> None:
    """req. 1.1.9 — 3 retries after the first attempt, then the source is unavailable."""
    route = respx.get("https://flaky.example/feed").mock(return_value=httpx.Response(503))
    settings = Settings.load().collection
    settings.retry_interval_seconds = 0  # keep the test fast

    async with HttpFetcher(settings) as fetcher:
        with pytest.raises(SourceUnavailable):
            await fetcher.get("https://flaky.example/feed")

    assert route.call_count == settings.max_retries + 1


@respx.mock
async def test_an_exhausted_rate_limit_says_so() -> None:
    """A bare 'HTTP 403' is not diagnosable from a run log at 08:00."""
    respx.get("https://api.github.com/repos/acme/tool/releases").mock(
        return_value=httpx.Response(
            403,
            headers={
                "x-ratelimit-limit": "60",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1786925677",
            },
        )
    )
    settings = Settings.load().collection
    settings.retry_interval_seconds = 0

    async with HttpFetcher(settings) as fetcher:
        with pytest.raises(SourceUnavailable) as caught:
            await fetcher.get("https://api.github.com/repos/acme/tool/releases")

    message = str(caught.value)
    assert "rate limit exhausted" in message
    assert "60/hour" in message
    assert "GITHUB_TOKEN" in message


@respx.mock
async def test_a_404_is_not_retried() -> None:
    """A missing feed will still be missing in five seconds; do not burn the budget."""
    route = respx.get("https://gone.example/feed").mock(return_value=httpx.Response(404))
    settings = Settings.load().collection
    settings.retry_interval_seconds = 0

    async with HttpFetcher(settings) as fetcher:
        with pytest.raises(SourceUnavailable):
            await fetcher.get("https://gone.example/feed")

    assert route.call_count == 1


@respx.mock
async def test_redirect_resolution_falls_back_to_the_original_url() -> None:
    """Losing an item to a dead tracker is worse than an imperfect canonical URL."""
    respx.head("https://t.co/broken").mock(side_effect=httpx.ConnectError("boom"))

    async with HttpFetcher(CFG.collection) as fetcher:
        resolved = await fetcher.resolve_redirect("https://t.co/broken", 5.0, 3)

    assert resolved == "https://t.co/broken"


@respx.mock
async def test_redirect_resolution_follows_a_shortener() -> None:
    respx.head("https://t.co/abc").mock(
        return_value=httpx.Response(301, headers={"location": "https://example.com/real"})
    )
    respx.head("https://example.com/real").mock(return_value=httpx.Response(200))

    async with HttpFetcher(CFG.collection) as fetcher:
        resolved = await fetcher.resolve_redirect("https://t.co/abc", 5.0, 3)

    assert resolved == "https://example.com/real"
