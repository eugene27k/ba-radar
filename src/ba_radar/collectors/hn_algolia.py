"""Hacker News collector via the Algolia search API (PRD §6, method `hn_algolia`).

This is the one collector that deliberately ignores the incremental cursor. HN points
accumulate for hours after a story is posted, so a story collected shortly after
submission may sit below `min_points` and, under the normal "only items newer than the
last run" rule, never be looked at again. Instead the trailing window is re-queried on
every run (answers doc Q11); dedup by canonical URL is what prevents re-delivery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from ba_radar.collectors.base import FetchContext, FetchResult, register
from ba_radar.models import RawItem, Source, SourceMethod, SourceState
from ba_radar.normalize import truncate_excerpt

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"


def hn_permalink(object_id: str) -> str:
    return f"https://news.ycombinator.com/item?id={object_id}"


class HnAlgoliaCollector:
    method = SourceMethod.HN_ALGOLIA

    async def fetch(self, source: Source, state: SourceState, ctx: FetchContext) -> FetchResult:
        assert source.min_points is not None

        window_start = ctx.now - timedelta(hours=ctx.settings.hn_requery_hours)
        numeric_filters = (
            f"created_at_i>{int(window_start.timestamp())},points>={source.min_points}"
        )

        new_state = state.model_copy(
            update={
                "last_success_at": ctx.now,
                "consecutive_failures": 0,
                "last_error": None,
            }
        )

        warnings: list[str] = []
        hits: list[dict[str, Any]] = []
        seen_object_ids: set[str] = set()

        # One request per query term — Algolia cannot express OR in a single search.
        for query in source.queries:
            params = {
                "query": query,
                "tags": "story",
                "numericFilters": numeric_filters,
                "hitsPerPage": str(ctx.settings.max_items_per_source),
            }
            response = await ctx.fetcher.get(
                f"{SEARCH_URL}?{urlencode(params)}", deadline=ctx.deadline
            )
            payload = response.json()
            term_hits = payload.get("hits") if isinstance(payload, dict) else None
            if not isinstance(term_hits, list):
                warnings.append(f"unexpected Algolia payload for query {query!r}")
                continue
            for hit in term_hits:
                object_id = str(hit.get("objectID") or "")
                if object_id and object_id not in seen_object_ids:
                    seen_object_ids.add(object_id)
                    hits.append(hit)

        # A story matching several terms is more on-topic, so rank by points and let
        # the per-source cap keep the strongest.
        hits.sort(key=lambda h: int(h.get("points") or 0), reverse=True)

        items: list[RawItem] = []
        newest = state.last_seen_at

        for hit in hits:
            object_id = str(hit.get("objectID") or "")
            title = (hit.get("title") or "").strip()
            if not object_id or not title:
                continue

            created = hit.get("created_at_i")
            if created is None:
                continue
            published = datetime.fromtimestamp(int(created), tz=UTC)

            discussion = hn_permalink(object_id)
            # Prefer the article URL so an item arriving via HN and via RSS collapses
            # into one row. Text posts (Ask HN / Show HN) have no external URL.
            link = (hit.get("url") or "").strip() or discussion

            points = hit.get("points") or 0
            comments = hit.get("num_comments") or 0
            body = (hit.get("story_text") or "").strip()
            excerpt = " ".join(
                part
                for part in (
                    f"HN: {points} points, {comments} comments — {discussion}",
                    body,
                )
                if part
            )

            items.append(
                RawItem(
                    url=link,
                    title=title,
                    published_at=published,
                    excerpt=truncate_excerpt(excerpt, 2000),
                    external_id=object_id,
                )
            )
            newest = published if newest is None else max(newest, published)

            if len(items) >= ctx.settings.max_items_per_source:
                break

        new_state = new_state.model_copy(update={"last_seen_at": newest})
        return FetchResult(items=items, state=new_state, warnings=warnings)


register(HnAlgoliaCollector())
