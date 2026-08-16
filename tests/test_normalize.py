"""Canonical URL rules — the answers-doc Q16 spec, clause by clause."""

from __future__ import annotations

import pytest

from ba_radar.normalize import (
    canonical_url,
    host_matches,
    item_id,
    should_resolve_redirect,
    strip_html,
    title_key,
    truncate_excerpt,
)
from ba_radar.settings import Settings

CFG = Settings.load().normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # utm_* and at_* are prefix-stripped; ordinary params survive.
        (
            "https://example.com/post?utm_source=rss&utm_campaign=x&id=7",
            "https://example.com/post?id=7",
        ),
        ("https://example.com/post?at_medium=email&at_campaign=z", "https://example.com/post"),
        # Exact-match tracking params.
        ("https://example.com/p?fbclid=abc&gclid=d&ref=hn", "https://example.com/p"),
        ("https://example.com/p?mc_cid=1&mc_eid=2&_hsenc=3", "https://example.com/p"),
        ("https://example.com/p?ck_subscriber_id=9", "https://example.com/p"),
        # www stripped, host and scheme lowercased.
        ("HTTPS://WWW.Example.COM/Path", "https://example.com/Path"),
        # Trailing slash removed, except at the root.
        ("https://example.com/a/b/", "https://example.com/a/b"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
        # Fragment dropped by default.
        ("https://example.com/p#section-2", "https://example.com/p"),
        # Remaining params sorted so ordering cannot fork identity.
        ("https://example.com/p?b=2&a=1", "https://example.com/p?a=1&b=2"),
        # Default ports removed.
        ("https://example.com:443/p", "https://example.com/p"),
    ],
)
def test_canonical_url_rules(raw: str, expected: str) -> None:
    assert canonical_url(raw, CFG) == expected


def test_fragment_kept_for_html_diff() -> None:
    """html_diff anchors identify which block changed, so they are meaningful."""
    assert (
        canonical_url("https://example.com/changelog#v2", CFG, keep_fragment=True)
        == "https://example.com/changelog#v2"
    )


def test_http_upgraded_only_for_known_hosts() -> None:
    known = frozenset({"example.com"})
    assert canonical_url("http://example.com/p", CFG, https_hosts=known) == (
        "https://example.com/p"
    )
    # An unknown host is left alone rather than guessed at.
    assert canonical_url("http://unknown.test/p", CFG, https_hosts=known) == (
        "http://unknown.test/p"
    )


def test_tracked_and_untracked_urls_collapse_to_one_id() -> None:
    """The whole point: the same article via three channels is one item."""
    a = canonical_url("https://www.example.com/post/?utm_source=rss", CFG)
    b = canonical_url("https://example.com/post", CFG)
    c = canonical_url("HTTPS://Example.com/post/#intro", CFG)
    assert a == b == c
    assert item_id(a) == item_id(b) == item_id(c)
    assert len(item_id(a)) == 64


def test_different_urls_produce_different_ids() -> None:
    assert item_id(canonical_url("https://example.com/a", CFG)) != item_id(
        canonical_url("https://example.com/b", CFG)
    )


@pytest.mark.parametrize(
    ("host", "pattern", "expected"),
    [
        ("t.co", "t.co", True),
        ("link.substack.com", "*.substack.com", True),
        ("substack.com", "*.substack.com", True),
        ("notsubstack.com", "*.substack.com", False),
        ("example.com", "t.co", False),
    ],
)
def test_host_matches(host: str, pattern: str, expected: bool) -> None:
    assert host_matches(host, pattern) is expected


def test_only_known_shorteners_are_resolved() -> None:
    assert should_resolve_redirect("https://t.co/abc123", CFG)
    assert should_resolve_redirect("https://link.substack.com/redirect?j=x", CFG)
    # Following redirects for every host would cost a request per item.
    assert not should_resolve_redirect("https://example.com/post", CFG)


@pytest.mark.parametrize(
    ("title", "source", "expected"),
    [
        ("Simon Willison: CORS Chat", "Simon Willison", "cors chat"),
        ("  Spaced   Out  Title  ", None, "spaced out title"),
        ("“Quoted Title”", None, "quoted title"),
        ("Same Title", None, "same title"),
        ("SAME TITLE", None, "same title"),
    ],
)
def test_title_key_normalisation(title: str, source: str | None, expected: str) -> None:
    assert title_key(title, source) == expected


def test_title_key_does_not_strip_a_merely_similar_prefix() -> None:
    """Only a source name followed by a separator is a prefix, not any substring."""
    assert title_key("Simon Willison wrote a thing", "Simon Willison") == (
        "simon willison wrote a thing"
    )


def test_truncate_excerpt_respects_the_cap() -> None:
    text = "word " * 1000
    result = truncate_excerpt(text, 2000)
    assert len(result) <= 2000
    assert not result.endswith(" ")


def test_truncate_excerpt_leaves_short_text_alone() -> None:
    assert truncate_excerpt("  short   text ", 2000) == "short text"


def test_strip_html_removes_markup_and_entities() -> None:
    raw = "<p>Hello <b>world</b> &amp; <script>evil()</script>friends</p>"
    assert strip_html(raw) == "Hello world & friends"
