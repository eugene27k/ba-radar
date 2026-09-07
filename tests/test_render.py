"""Digest rendering: tier blocks, per-item verdicts and the 4096-character split."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from ba_radar.labels import NO_NEW_ITEMS, PRIORITY_UK, UNSCORED_BLOCK_UK
from ba_radar.models import Action, PracticeTag, Priority
from ba_radar.render import DigestEntry, format_entry, render_digest

TODAY = date(2026, 8, 3)
STAMP = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def entry(
    title: str,
    *,
    priority: Priority | None = Priority.NOTABLE,
    source: str = "Simon Willison",
    url: str = "https://e.com/p",
    summary: str | None = "Стислий переказ матеріалу.",
    insight: str | None = "Змінює те, як писати критерії приймання.",
    action: Action | None = Action.READ,
    tags: tuple[PracticeTag, ...] = (PracticeTag.SPECIFICATIONS,),
    verbatim: bool = False,
) -> DigestEntry:
    return DigestEntry(
        title=title,
        url=url,
        source_label=source,
        published_at=STAMP,
        priority=priority,
        summary=summary,
        ba_insight=insight,
        action=action,
        tags=tags,
        verbatim_flag=verbatim,
    )


def test_header_carries_the_title_date_and_count() -> None:
    """req. 3.1.3 — DD.MM.YYYY plus the number of items processed."""
    messages = render_digest([entry("A post")], digest_date=TODAY, processed_count=42)
    assert messages[0].startswith("<b>AIforBA Radar — 03.08.2026</b>")
    assert "Опрацьовано матеріалів: 42" in messages[0]

    custom = render_digest(
        [entry("A post")], digest_date=TODAY, processed_count=1, title="BA Radar"
    )
    assert custom[0].startswith("<b>BA Radar — 03.08.2026</b>")


def test_empty_day_is_reported_explicitly() -> None:
    """req. 3.1.9 — silence would be indistinguishable from a failure."""
    messages = render_digest([], digest_date=TODAY, processed_count=0)
    assert len(messages) == 1
    assert NO_NEW_ITEMS in messages[0]


def test_an_item_renders_its_verdict_in_ukrainian() -> None:
    body = render_digest(
        [entry("Spec-driven agents", action=Action.TRY, tags=(PracticeTag.EVALS,))],
        digest_date=TODAY,
        processed_count=1,
    )[0]
    assert '<a href="https://e.com/p">Spec-driven agents</a> <i>03.08 · Simon Willison</i>' in body
    assert "Стислий переказ матеріалу." in body
    assert "<b>BA:</b> Змінює те, як писати критерії приймання." in body
    assert "<i>Спробувати</i> · Оцінювання" in body


def test_blocks_follow_tier_order_with_ukrainian_headings() -> None:
    """PRD 3.1.4 — Critical / Notable / Background blocks, then the unscored block."""
    entries = [
        entry("C1", priority=Priority.CRITICAL),
        entry("C2", priority=Priority.CRITICAL),
        entry("N1", priority=Priority.NOTABLE),
        entry("B1", priority=Priority.BACKGROUND),
        entry("U1", priority=None, summary=None, insight=None, action=None, tags=()),
    ]
    body = render_digest(entries, digest_date=TODAY, processed_count=5)[0]

    positions = [
        body.index(f"<b>{PRIORITY_UK[Priority.CRITICAL]}</b>"),
        body.index(f"<b>{PRIORITY_UK[Priority.NOTABLE]}</b>"),
        body.index(f"<b>{PRIORITY_UK[Priority.BACKGROUND]}</b>"),
        body.index(f"<b>{UNSCORED_BLOCK_UK}</b>"),
    ]
    assert positions == sorted(positions)
    assert body.count(f"<b>{PRIORITY_UK[Priority.CRITICAL]}</b>") == 1
    assert body.index("C1") < body.index("C2") < body.index("N1")


def test_a_verbatim_flagged_item_shows_title_and_insight_only() -> None:
    """PRD §8 — never reproduce source text; the insight is ours, the summary may not be."""
    body = render_digest(
        [entry("Copied", summary="Дослівний фрагмент", verbatim=True)],
        digest_date=TODAY,
        processed_count=1,
    )[0]
    assert "Дослівний фрагмент" not in body
    assert "Copied" in body and "<b>BA:</b>" in body


def test_html_is_escaped_in_titles_prose_and_urls() -> None:
    messages = render_digest(
        [
            entry(
                "Tags <b>& ampersands</b>",
                url="https://e.com/p?a=1&b=2",
                summary="A < B & C > D",
            )
        ],
        digest_date=TODAY,
        processed_count=1,
    )
    body = messages[0]
    assert "Tags &lt;b&gt;&amp; ampersands&lt;/b&gt;" in body
    assert 'href="https://e.com/p?a=1&amp;b=2"' in body
    assert "A &lt; B &amp; C &gt; D" in body
    # The only tags present are the ones we emit deliberately.
    assert set(re.findall(r"</?([a-z]+)", body)) <= {"b", "a", "i"}


def many(count: int, priority: Priority | None = Priority.NOTABLE) -> list[DigestEntry]:
    return [
        entry(f"Item number {i} " + "padding " * 12, priority=priority, url=f"https://e.com/{i}")
        for i in range(count)
    ]


def test_long_digest_splits_and_every_part_fits() -> None:
    """req. 3.1.7 — split into several messages when over the limit."""
    messages = render_digest(many(40), digest_date=TODAY, processed_count=40, max_chars=4096)
    assert len(messages) > 1
    assert all(len(m) <= 4096 for m in messages)


def test_split_never_lands_inside_an_item() -> None:
    """req. 3.1.7 — every item's whole fragment sits in exactly one message."""
    entries = many(40)
    messages = render_digest(entries, digest_date=TODAY, processed_count=40, max_chars=4096)
    for item in entries:
        fragment = format_entry(item)
        assert sum(message.count(fragment) for message in messages) == 1
    for message in messages:
        assert message.count("<a href=") == message.count("</a>")


def test_continuation_messages_repeat_the_block_heading() -> None:
    """A continuation message has to stand on its own for the reader."""
    messages = render_digest(many(40), digest_date=TODAY, processed_count=40, max_chars=4096)
    assert len(messages) > 1
    for message in messages[1:]:
        assert "продовження" in message
        assert f"<b>{PRIORITY_UK[Priority.NOTABLE]}</b>" in message


def test_absurdly_long_title_and_prose_are_clipped() -> None:
    messages = render_digest(
        [entry("x" * 9000, summary="y" * 5000, insight="z" * 5000)],
        digest_date=TODAY,
        processed_count=1,
        max_chars=4096,
    )
    assert len(messages) == 1
    assert len(messages[0]) <= 4096
    assert messages[0].count("…") == 3


def test_a_pathological_url_loses_its_link_but_not_its_item() -> None:
    """One oversized item would make Telegram reject the whole message, failing the
    send — and with it the day's digest — for a single bad item."""
    poison = entry("Poison item", url="https://e.com/?q=" + "x" * 8000)
    healthy = entry("Healthy item")
    messages = render_digest(
        [healthy, poison], digest_date=TODAY, processed_count=2, max_chars=4096
    )

    assert all(len(m) <= 4096 for m in messages)
    combined = "\n".join(messages)
    assert "• Poison item" in combined  # the item survives, linkless
    assert "x" * 100 not in combined  # the URL does not
    assert 'href="https://e.com/p"' in combined  # healthy links are untouched
