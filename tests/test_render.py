"""Digest rendering and the 4096-character split (PRD req. 3.1)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from ba_radar.labels import NO_NEW_ITEMS
from ba_radar.render import DigestEntry, render_digest

TODAY = date(2026, 8, 3)
STAMP = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def entry(title: str, source: str = "Simon Willison", url: str = "https://e.com/p") -> DigestEntry:
    return DigestEntry(title=title, url=url, source_label=source, published_at=STAMP)


def test_header_carries_the_date_and_count() -> None:
    """req. 3.1.3 — DD.MM.YYYY plus the number of items processed."""
    messages = render_digest([entry("A post")], digest_date=TODAY, processed_count=42)
    assert "03.08.2026" in messages[0]
    assert "Опрацьовано матеріалів: 42" in messages[0]


def test_empty_day_is_reported_explicitly() -> None:
    """req. 3.1.9 — silence would be indistinguishable from a failure."""
    messages = render_digest([], digest_date=TODAY, processed_count=0)
    assert len(messages) == 1
    assert NO_NEW_ITEMS in messages[0]


def test_html_is_escaped_in_titles_and_urls() -> None:
    messages = render_digest(
        [entry("Tags <b>& ampersands</b>", url="https://e.com/p?a=1&b=2")],
        digest_date=TODAY,
        processed_count=1,
    )
    body = messages[0]
    assert "Tags &lt;b&gt;&amp; ampersands&lt;/b&gt;" in body
    assert 'href="https://e.com/p?a=1&amp;b=2"' in body
    # The only tags present are the ones we emit deliberately.
    assert set(re.findall(r"</?([a-z]+)", body)) <= {"b", "a", "i"}


def test_items_are_grouped_under_their_source() -> None:
    messages = render_digest(
        [entry("One", "Source A"), entry("Two", "Source A"), entry("Three", "Source B")],
        digest_date=TODAY,
        processed_count=3,
    )
    body = messages[0]
    assert body.count("<b>Source A</b>") == 1
    assert body.count("<b>Source B</b>") == 1


def test_long_digest_splits_and_every_part_fits() -> None:
    """req. 3.1.7 — split into several messages when over the limit."""
    entries = [entry(f"Item number {i} " + "padding " * 12) for i in range(60)]
    messages = render_digest(entries, digest_date=TODAY, processed_count=60, max_chars=4096)

    assert len(messages) > 1
    assert all(len(m) <= 4096 for m in messages)


def test_split_never_lands_inside_an_item() -> None:
    """req. 3.1.7 — splits happen at block boundaries, not mid-item."""
    entries = [entry(f"Item number {i} " + "padding " * 12) for i in range(60)]
    messages = render_digest(entries, digest_date=TODAY, processed_count=60, max_chars=4096)

    for message in messages:
        # Every anchor that opens is closed within the same message.
        assert message.count("<a href=") == message.count("</a>")
        for line in message.splitlines():
            if line.startswith("•"):
                assert line.rstrip().endswith("</i>")


def test_no_item_is_lost_or_duplicated_across_the_split() -> None:
    entries = [entry(f"Unique title {i} " + "padding " * 12) for i in range(60)]
    messages = render_digest(entries, digest_date=TODAY, processed_count=60, max_chars=4096)

    combined = "\n".join(messages)
    for i in range(60):
        assert combined.count(f"Unique title {i} ") == 1


def test_continuation_messages_are_labelled() -> None:
    entries = [entry(f"Item {i} " + "padding " * 12) for i in range(60)]
    messages = render_digest(entries, digest_date=TODAY, processed_count=60, max_chars=4096)
    for message in messages[1:]:
        assert "продовження" in message


def test_group_heading_repeats_when_a_group_spans_a_split() -> None:
    """A continuation message has to stand on its own for the reader."""
    entries = [entry(f"Item {i} " + "padding " * 12, "One Big Source") for i in range(60)]
    messages = render_digest(entries, digest_date=TODAY, processed_count=60, max_chars=4096)

    assert len(messages) > 1
    assert all("<b>One Big Source</b>" in message for message in messages)


def test_absurdly_long_title_is_clipped_rather_than_breaking_the_split() -> None:
    messages = render_digest(
        [entry("x" * 9000)], digest_date=TODAY, processed_count=1, max_chars=4096
    )
    assert len(messages) == 1
    assert len(messages[0]) <= 4096
    assert "…" in messages[0]


def test_a_pathological_url_loses_its_link_but_not_its_item() -> None:
    """One oversized line would make Telegram reject the whole message, failing the
    send — and with it the day's digest — for a single bad item."""
    poison = entry("Poison item", url="https://e.com/?q=" + "x" * 8000)
    healthy = entry("Healthy item")
    messages = render_digest(
        [healthy, poison], digest_date=TODAY, processed_count=2, max_chars=4096
    )

    assert all(len(m) <= 4096 for m in messages)
    combined = "\n".join(messages)
    assert "Poison item" in combined  # the item survives, linkless
    assert "x" * 100 not in combined  # the URL does not
    assert 'href="https://e.com/p"' in combined  # healthy links are untouched
