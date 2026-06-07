"""Rich-text formatting helpers.

The bot's signature look is a Telegram **blockquote** card. Telegram renders a
native, collapsible blockquote from the ``> Quote`` MarkdownV2 convention, but
Pyrogram's MTProto markdown dialect does not emit blockquote entities. We
therefore author the same quoted layout and render it through Pyrogram's HTML
parser, which *does* produce a genuine ``MessageEntityBlockquote`` (plus nested
bold/code entities) -- giving the exact premium quoted visual identity.

All renderers in :mod:`signsub.ui.progress` build on these primitives and the
resulting strings must be sent with ``ParseMode.HTML``.
"""

from __future__ import annotations

from typing import Iterable

# The Telegram parse mode these helpers target.
PARSE_MODE = "html"


def escape(text: object) -> str:
    """Escape text for safe inclusion in HTML-parsed messages."""

    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def code(text: object) -> str:
    """Inline monospace span."""

    return f"<code>{escape(text)}</code>"


def bold(text: str) -> str:
    """Bold an already-escaped fragment."""

    return f"<b>{text}</b>"


def quote_block(lines: Iterable[str]) -> str:
    """Wrap pre-rendered (already escaped) lines in a single blockquote card."""

    return "<blockquote>" + "\n".join(lines) + "</blockquote>"
