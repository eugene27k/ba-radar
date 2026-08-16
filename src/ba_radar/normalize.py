"""Canonical URLs, item identity, and title keys.

Canonicalisation quality is what dedup quality reduces to: the same article arriving
via RSS, via a newsletter tracker and via Hacker News only collapses into one item if
all three normalise to the same string. The rules here are the answers-doc Q16 spec.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ba_radar.settings import NormalizeSettings

_WHITESPACE = re.compile(r"\s+")
# Leading/trailing punctuation to shave off a title before comparing.
_EDGE_PUNCT = re.compile(r"^[\s\-–—:|·•\"'“”‘’(\[{]+|[\s\-–—:|·•\"'“”‘’)\]}]+$")
_SOURCE_SEPARATORS = (":", "–", "—", "-", "|", "·")


def host_matches(host: str, pattern: str) -> bool:
    """Match a host against an exact name or a `*.example.com` suffix pattern."""
    host = host.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix) or host == pattern[2:]
    return host == pattern


def should_resolve_redirect(url: str, settings: NormalizeSettings) -> bool:
    """Only known shorteners and link trackers are worth a network round trip.

    Following redirects for arbitrary hosts would add a request per item across
    hundreds of items, which threatens the 10-minute cycle for marginal dedup gain.
    """
    host = urlsplit(url).hostname or ""
    return any(host_matches(host, pattern) for pattern in settings.resolve_redirect_hosts)


def _strip_tracking_params(query: str, settings: NormalizeSettings) -> str:
    exact = {p.lower() for p in settings.strip_params_exact}
    prefixes = tuple(p.lower() for p in settings.strip_params_prefix)

    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in exact and not key.lower().startswith(prefixes)
    ]
    # Sorting makes two orderings of the same parameters produce one canonical string.
    kept.sort()
    return urlencode(kept)


def canonical_url(
    url: str,
    settings: NormalizeSettings,
    *,
    keep_fragment: bool = False,
    https_hosts: frozenset[str] | None = None,
) -> str:
    """Normalise a URL for identity and dedup.

    `keep_fragment` is set for html_diff sources, where the anchor identifies which
    block of the page changed and is therefore meaningful.
    """
    parts = urlsplit(url.strip())

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    # http -> https only for hosts we already know answer over https, either because
    # the registry reaches them that way or because they are listed explicitly.
    if scheme == "http":
        known = set(https_hosts or frozenset()) | {h.lower() for h in settings.force_https_hosts}
        if host in known:
            scheme = "https"

    netloc = host
    if parts.port and not (
        (scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)
    ):
        netloc = f"{host}:{parts.port}"

    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query = _strip_tracking_params(parts.query, settings)
    fragment = parts.fragment if keep_fragment else ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def item_id(canonical: str) -> str:
    """PRD req. 1.2.1 — the item identifier is a hash of the canonical link."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def title_key(title: str, source_name: str | None = None) -> str:
    """Normalised title for the windowed title match (answers doc Q12).

    Lowercase, whitespace collapsed, edge punctuation stripped, and a leading source
    name removed so "Simon Willison: X" and "X" compare equal.
    """
    text = unicodedata.normalize("NFKC", title)
    text = _WHITESPACE.sub(" ", text).strip()

    if source_name:
        lowered = text.lower()
        prefix = source_name.lower().strip()
        if lowered.startswith(prefix):
            remainder = text[len(prefix) :].lstrip()
            if remainder[:1] in _SOURCE_SEPARATORS:
                text = remainder[1:].strip()

    text = _EDGE_PUNCT.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text.casefold()


def truncate_excerpt(text: str, max_chars: int) -> str:
    """PRD §8 — excerpts are capped. Trims at a word boundary where one is close by."""
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    if len(collapsed) <= max_chars:
        return collapsed
    cut = collapsed[:max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.8:
        cut = cut[:space]
    return cut.rstrip()


def strip_html(raw: str) -> str:
    """Crude tag stripper for feed summaries. Good enough to feed a model an excerpt."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return _WHITESPACE.sub(" ", text).strip()
