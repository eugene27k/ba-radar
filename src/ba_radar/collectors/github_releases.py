"""GitHub releases collector (PRD §6, method `github_releases`).

Authenticates when a token is available: unauthenticated GitHub allows 60 requests an
hour, which the registry outgrows at roughly 15 repos.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ba_radar.collectors.base import FetchContext, FetchResult, register
from ba_radar.models import RawItem, Source, SourceMethod, SourceState
from ba_radar.normalize import truncate_excerpt

API_ROOT = "https://api.github.com"

# A release named just "v2.1.261" or "0.154.0-alpha.3" is meaningless once the digest
# stops grouping by source (Stage 2 groups by priority), so bare versions get the
# source name prefixed. Titles with actual words are left alone.
_BARE_VERSION = re.compile(r"^v?\d[\w.+-]*$", re.ASCII)


class GitHubReleasesCollector:
    method = SourceMethod.GITHUB_RELEASES

    async def fetch(self, source: Source, state: SourceState, ctx: FetchContext) -> FetchResult:
        assert source.repo is not None

        url = (
            f"{API_ROOT}/repos/{source.repo}/releases?per_page={ctx.settings.max_items_per_source}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if ctx.github_token:
            headers["Authorization"] = f"Bearer {ctx.github_token}"
        if state.etag:
            # GitHub does not bill a 304 against the rate limit, so this is free headroom.
            headers["If-None-Match"] = state.etag

        response = await ctx.fetcher.get(url, headers=headers, deadline=ctx.deadline)

        new_state = state.model_copy(
            update={
                "etag": response.headers.get("etag") or state.etag,
                "last_success_at": ctx.now,
                "consecutive_failures": 0,
                "last_error": None,
            }
        )

        if response.not_modified:
            return FetchResult(items=[], state=new_state)

        payload = response.json()
        if not isinstance(payload, list):
            return FetchResult(
                items=[],
                state=new_state,
                warnings=[f"unexpected releases payload for {source.repo}"],
            )

        items: list[RawItem] = []
        newest = state.last_seen_at
        warnings: list[str] = []

        for release in payload:
            if release.get("draft"):
                continue  # drafts are not public; prereleases are kept deliberately

            raw_published = release.get("published_at") or release.get("created_at")
            if not raw_published:
                continue
            published = datetime.fromisoformat(raw_published.replace("Z", "+00:00")).astimezone(UTC)
            if published <= ctx.since:
                continue

            title = (release.get("name") or release.get("tag_name") or "").strip()
            link = release.get("html_url")
            if not title or not link:
                continue

            items.append(
                RawItem(
                    url=link,
                    title=f"{source.name} {title}" if _BARE_VERSION.match(title) else title,
                    published_at=published,
                    excerpt=truncate_excerpt(release.get("body") or "", 2000),
                    external_id=str(release.get("id") or ""),
                )
            )
            newest = published if newest is None else max(newest, published)

            if len(items) >= ctx.settings.max_items_per_source:
                break

        if not payload:
            warnings.append(f"{source.repo} publishes no releases")

        new_state = new_state.model_copy(update={"last_seen_at": newest})
        return FetchResult(items=items, state=new_state, warnings=warnings)


register(GitHubReleasesCollector())
