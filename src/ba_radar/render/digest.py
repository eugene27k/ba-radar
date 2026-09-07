"""Digest rendering for Telegram (PRD req. 3.1, 3.1.4, 3.1.5, §5.3).

Stage 2 renders tier blocks — Критичний / Вартий уваги / Фоновий — each item with its
linked title, date and source, a Ukrainian summary, the BA insight, the recommended
action and the practice tags. A final «Без аналізу» block carries items a failed or
timed-out provider left without a verdict, so an outage is visible rather than silent.

The 4096-character splitting from Increment 1 carries over unchanged in spirit: a
split never lands inside an item, a block heading is repeated on a continuation
message, and an item whose line cannot fit loses its link rather than the digest.

Telegram HTML is used rather than MarkdownV2: MarkdownV2 requires escaping around
fifteen characters in ordinary prose, and a single missed escape rejects the whole
message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ba_radar.labels import (
    ACTION_UK,
    CONTINUATION_UK,
    INSIGHT_PREFIX_UK,
    NO_NEW_ITEMS,
    PRACTICE_TAG_UK,
    PRIORITY_UK,
    PROCESSED_UK,
    UNSCORED_BLOCK_UK,
)
from ba_radar.models import Action, PracticeTag, Priority

MAX_TITLE_CHARS = 200
MAX_PROSE_CHARS = 600  # summary and insight are bounded by the prompt; this is the belt
DEFAULT_TITLE = "AIforBA Radar"


@dataclass(frozen=True)
class DigestEntry:
    title: str
    url: str
    source_label: str
    published_at: datetime
    priority: Priority | None = None  # None -> the «Без аналізу» block
    summary: str | None = None
    ba_insight: str | None = None
    action: Action | None = None
    tags: tuple[PracticeTag, ...] = ()
    # Summary shares a long verbatim run with the source (PRD §8): render the title
    # and the insight only, never the summary.
    verbatim_flag: bool = False

    @property
    def block_label(self) -> str:
        return PRIORITY_UK[self.priority] if self.priority else UNSCORED_BLOCK_UK


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML parser treats as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(url: str) -> str:
    return escape_html(url).replace('"', "&quot;")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_entry(entry: DigestEntry, *, with_link: bool = True) -> str:
    """One item as a multi-line HTML fragment. Never split across messages."""
    title = escape_html(_clip(entry.title, MAX_TITLE_CHARS))
    head = f'• <a href="{escape_attr(entry.url)}">{title}</a>' if with_link else f"• {title}"
    lines = [f"{head} <i>{entry.published_at:%d.%m} · {escape_html(entry.source_label)}</i>"]

    if entry.summary and not entry.verbatim_flag:
        lines.append(escape_html(_clip(entry.summary.strip(), MAX_PROSE_CHARS)))
    if entry.ba_insight:
        insight = escape_html(_clip(entry.ba_insight.strip(), MAX_PROSE_CHARS))
        lines.append(f"<b>{INSIGHT_PREFIX_UK}</b> {insight}")

    footer: list[str] = []
    if entry.action:
        footer.append(f"<i>{ACTION_UK[entry.action]}</i>")
    if entry.tags:
        footer.append(", ".join(PRACTICE_TAG_UK[tag] for tag in entry.tags))
    if footer:
        lines.append(" · ".join(footer))
    return "\n".join(lines)


def render_digest(
    entries: list[DigestEntry],
    *,
    digest_date: date,
    processed_count: int,
    max_chars: int = 4096,
    title: str = DEFAULT_TITLE,
) -> list[str]:
    """Render the digest as one or more Telegram messages.

    `entries` must already be in display order (tier blocks, then the unscored
    block); consecutive entries with the same block label share a heading. Returns
    at least one message. Splits happen only between items (req. 3.1.7); a block
    split across messages repeats its heading so the continuation reads on its own.
    """
    safe_title = escape_html(title)
    header = f"<b>{safe_title} — {digest_date:%d.%m.%Y}</b>\n{PROCESSED_UK}: {processed_count}"

    if not entries:
        # req. 3.1.9 — an empty day is reported, not silently skipped.
        return [f"{header}\n\n{NO_NEW_ITEMS}"]

    continuation = f"<b>{safe_title} — {digest_date:%d.%m.%Y} ({CONTINUATION_UK})</b>"
    item_budget = max_chars - max(len(header), len(continuation)) - 2

    blocks: list[tuple[str, list[str]]] = []
    for entry in entries:
        heading_len = len(f"\n<b>{escape_html(entry.block_label)}</b>") + 2
        fragment = format_entry(entry)
        # A single oversized item (a pathological URL — titles and prose are already
        # clipped) would produce a message Telegram rejects outright, failing the
        # whole send for one bad item. Dropping the link keeps the item and the digest.
        if len(fragment) > item_budget - heading_len:
            fragment = format_entry(entry, with_link=False)
        if blocks and blocks[-1][0] == entry.block_label:
            blocks[-1][1].append(fragment)
        else:
            blocks.append((entry.block_label, [fragment]))

    messages: list[str] = []
    current: list[str] = [header]
    current_len = len(header)
    open_block: str | None = None

    def flush() -> None:
        nonlocal current, current_len, open_block
        if len(current) > 1:
            messages.append("\n".join(current))
        current = [continuation]
        current_len = len(continuation)
        open_block = None

    for label, fragments in blocks:
        heading = f"\n<b>{escape_html(label)}</b>"
        for index, fragment in enumerate(fragments):
            piece = f"\n{fragment}"  # a blank line before every item keeps blocks readable
            needs_heading = index == 0 or open_block != label
            addition = (len(heading) + 1 if needs_heading else 0) + len(piece) + 1

            if current_len + addition > max_chars and len(current) > 1:
                flush()
                needs_heading = True
                addition = len(heading) + 1 + len(piece) + 1

            if needs_heading:
                current.append(heading)
                current_len += len(heading) + 1
                open_block = label

            current.append(piece)
            current_len += len(piece) + 1

    if len(current) > 1:
        messages.append("\n".join(current))

    return messages or [f"{header}\n\n{NO_NEW_ITEMS}"]
