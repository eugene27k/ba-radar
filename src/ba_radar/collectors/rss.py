"""RSS / Atom collector (PRD §6, method `rss`)."""

from __future__ import annotations

import asyncio
import calendar
from datetime import UTC, datetime
from typing import Any

import feedparser

from ba_radar.collectors.base import FetchContext, FetchResult, register
from ba_radar.models import RawItem, Source, SourceMethod, SourceState
from ba_radar.normalize import strip_html, truncate_excerpt


def _entry_datetime(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
    return None


def _entry_excerpt(entry: Any) -> str:
    for attr in ("summary", "description"):
        value = getattr(entry, attr, None)
        if value:
            return strip_html(str(value))
    content = getattr(entry, "content", None)
    if content:
        return strip_html(str(content[0].get("value", "")))
    return ""


class RssCollector:
    method = SourceMethod.RSS

    async def fetch(self, source: Source, state: SourceState, ctx: FetchContext) -> FetchResult:
        assert source.url is not None  # guaranteed by Source validation

        headers: dict[str, str] = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified

        response = await ctx.fetcher.get(source.url, headers=headers, deadline=ctx.deadline)

        new_state = state.model_copy(
            update={
                "etag": response.headers.get("etag") or state.etag,
                "last_modified": response.headers.get("last-modified") or state.last_modified,
                "last_success_at": ctx.now,
                "consecutive_failures": 0,
                "last_error": None,
            }
        )

        if response.not_modified:
            return FetchResult(items=[], state=new_state)

        # feedparser is synchronous and does real parsing work; keep it off the loop.
        parsed = await asyncio.to_thread(feedparser.parse, response.content)

        warnings: list[str] = []
        if getattr(parsed, "bozo", False) and not parsed.entries:
            warnings.append(f"feed did not parse: {getattr(parsed, 'bozo_exception', '?')}")

        items: list[RawItem] = []
        newest = state.last_seen_at
        undated = 0

        for entry in parsed.entries:
            link = getattr(entry, "link", None)
            title = (getattr(entry, "title", "") or "").strip()
            if not link or not title:
                continue

            published = _entry_datetime(entry)
            if published is None:
                # Feeds without dates would otherwise be re-collected forever; dedup by
                # canonical URL is what actually stops that, so treat them as "now".
                undated += 1
                published = ctx.now
            elif published <= ctx.since:
                continue

            items.append(
                RawItem(
                    url=str(link),
                    title=title,
                    published_at=published,
                    excerpt=truncate_excerpt(_entry_excerpt(entry), 2000),
                    external_id=getattr(entry, "id", None),
                )
            )
            newest = published if newest is None else max(newest, published)

            if len(items) >= ctx.settings.max_items_per_source:  # req. 1.1.10
                break

        if undated:
            warnings.append(f"{undated} entr(y/ies) had no date; treated as new")

        new_state = new_state.model_copy(update={"last_seen_at": newest})
        return FetchResult(items=items, state=new_state, warnings=warnings)


register(RssCollector())
