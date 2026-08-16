"""Shared async HTTP layer for every collector.

Everything the PRD says about network behaviour lives here rather than in the
individual collectors: retries (req. 1.1.9), per-API rate limiting (§8), conditional
requests, and a per-source deadline.

The deadline matters more than it looks. Fifty sources at four attempts each, with a
15-second timeout and a 5-second backoff, is unbounded enough to blow the 10-minute
cycle on a bad day; the deadline turns that into a hard ceiling per source.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ba_radar.settings import CollectionSettings


class SourceUnavailable(Exception):
    """Raised when a source could not be fetched after all retries or past its deadline."""


def _describe_client_error(response: httpx.Response, url: str) -> str:
    """Turn a 4xx into something diagnosable from the run log alone.

    A bare "HTTP 403" sends whoever reads the log at 08:00 hunting. The common cause
    here is an exhausted unauthenticated GitHub quota (60 requests an hour), which is
    silent unless the rate-limit headers are read.
    """
    status = response.status_code
    remaining = response.headers.get("x-ratelimit-remaining")

    if status in (403, 429) and remaining == "0":
        limit = response.headers.get("x-ratelimit-limit", "?")
        reset = response.headers.get("x-ratelimit-reset")
        when = ""
        if reset and reset.isdigit():
            when = f", resets at {datetime.fromtimestamp(int(reset), tz=UTC):%H:%M} UTC"
        hint = " — set GITHUB_TOKEN to raise it" if limit == "60" else ""
        return f"rate limit exhausted ({limit}/hour){when}{hint}: {url}"

    return f"HTTP {status}: {url}"


@dataclass
class Response:
    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class HttpFetcher:
    """Concurrency-limited, retrying HTTP client with per-host pacing."""

    def __init__(self, settings: CollectionSettings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last_request: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpFetcher:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": self.settings.user_agent},
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpFetcher must be used as an async context manager")
        return self._client

    async def _pace(self, host: str) -> None:
        """Honour a per-host minimum interval (PRD §8, rate-limit compliance)."""
        interval = self.settings.host_min_interval_seconds.get(host)
        if not interval:
            return
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last_request.get(host)
            now = time.monotonic()
            if last is not None and (wait := interval - (now - last)) > 0:
                await asyncio.sleep(wait)
            self._host_last_request[host] = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        deadline: float | None = None,
    ) -> Response:
        """GET with retries. Raises SourceUnavailable when every attempt fails.

        A 304 is returned normally, not raised — an unchanged feed is a success.
        """
        host = httpx.URL(url).host
        attempts = self.settings.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            if deadline is not None and time.monotonic() >= deadline:
                raise SourceUnavailable(
                    f"deadline exceeded after {attempt} attempt(s): {url}"
                ) from last_error

            try:
                async with self._semaphore:
                    await self._pace(host)
                    response = await self.client.get(url, headers=headers or {})
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
            else:
                if response.status_code < 400 or response.status_code == 304:
                    return Response(
                        status_code=response.status_code,
                        text=response.text,
                        content=response.content,
                        headers={k.lower(): v for k, v in response.headers.items()},
                    )
                # 4xx other than 429 will not fix themselves; stop early.
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise SourceUnavailable(_describe_client_error(response, url))
                last_error = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )

            if attempt < attempts - 1:
                await asyncio.sleep(self.settings.retry_interval_seconds)

        raise SourceUnavailable(f"failed after {attempts} attempt(s): {url}") from last_error

    async def resolve_redirect(self, url: str, timeout: float, max_hops: int) -> str:
        """Follow a known shortener to its destination.

        Never raises: on any failure the original URL is returned, because losing an
        item to a dead link-tracker is worse than an imperfect canonical URL.
        """
        current = url
        try:
            for _ in range(max_hops):
                response = await self.client.head(current, follow_redirects=False, timeout=timeout)
                if response.status_code in (405, 501):  # HEAD unsupported — fall back
                    response = await self.client.get(
                        current, follow_redirects=False, timeout=timeout
                    )
                location = response.headers.get("location")
                if not (300 <= response.status_code < 400) or not location:
                    return current
                current = str(httpx.URL(current).join(location))
            return current
        except (httpx.HTTPError, OSError, ValueError):
            return url
