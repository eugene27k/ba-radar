"""Digest rendering for Telegram (PRD req. 3.1).

Increment 1 renders a plain grouped list: there is no scoring or model analysis yet, so
there are no priority tiers and no per-item summary. Stage 2 replaces `render_digest`
with the Critical/Notable block format under the 3/5/7 caps; the splitting, escaping
and header logic here carries over unchanged.

Telegram HTML is used rather than MarkdownV2: MarkdownV2 requires escaping around
fifteen characters in ordinary prose, and a single missed escape rejects the whole
message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ba_radar.labels import NO_NEW_ITEMS

MAX_TITLE_CHARS = 200


@dataclass(frozen=True)
class DigestEntry:
    title: str
    url: str
    source_label: str
    published_at: datetime


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML parser treats as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(url: str) -> str:
    return escape_html(url).replace('"', "&quot;")


def _clip(title: str) -> str:
    if len(title) <= MAX_TITLE_CHARS:
        return title
    return title[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def render_digest(
    entries: list[DigestEntry],
    *,
    digest_date: date,
    processed_count: int,
    max_chars: int = 4096,
) -> list[str]:
    """Render the digest as one or more Telegram messages.

    Returns at least one message. Splits happen only between items, never inside one
    (req. 3.1.7); when a source's group is split across messages its heading is
    repeated so the continuation is readable on its own.
    """
    header = f"<b>BA Radar — {digest_date:%d.%m.%Y}</b>\nОпрацьовано матеріалів: {processed_count}"

    if not entries:
        # req. 3.1.9 — an empty day is reported, not silently skipped.
        return [f"{header}\n\n{NO_NEW_ITEMS}"]

    continuation = f"<b>BA Radar — {digest_date:%d.%m.%Y} (продовження)</b>"
    line_budget = max_chars - max(len(header), len(continuation)) - 2

    groups: list[tuple[str, list[str]]] = []
    for entry in entries:
        line = (
            f'• <a href="{escape_attr(entry.url)}">{escape_html(_clip(entry.title))}</a>'
            f" <i>{entry.published_at:%d.%m}</i>"
        )
        # A single oversized line (a pathological URL — titles are already clipped)
        # would produce a message Telegram rejects outright, failing the whole send
        # for one bad item. Dropping the link keeps the item and the digest.
        heading_len = len(f"\n<b>{escape_html(entry.source_label)}</b>") + 1
        if len(line) > line_budget - heading_len:
            line = f"• {escape_html(_clip(entry.title))} <i>{entry.published_at:%d.%m}</i>"
        if groups and groups[-1][0] == entry.source_label:
            groups[-1][1].append(line)
        else:
            groups.append((entry.source_label, [line]))

    messages: list[str] = []
    current: list[str] = [header]
    current_len = len(header)
    open_group: str | None = None

    def flush() -> None:
        nonlocal current, current_len, open_group
        if current:
            messages.append("\n".join(current))
        current = [continuation]
        current_len = len(continuation)
        open_group = None

    for label, lines in groups:
        heading = f"\n<b>{escape_html(label)}</b>"
        for index, line in enumerate(lines):
            needs_heading = index == 0 or open_group != label
            addition = (len(heading) + 1 if needs_heading else 0) + len(line) + 1

            if current_len + addition > max_chars and len(current) > 1:
                flush()
                needs_heading = True
                addition = len(heading) + 1 + len(line) + 1

            if needs_heading:
                current.append(heading)
                current_len += len(heading) + 1
                open_group = label

            current.append(line)
            current_len += len(line) + 1

    if len(current) > 1:
        messages.append("\n".join(current))

    return messages or [f"{header}\n\n{NO_NEW_ITEMS}"]
