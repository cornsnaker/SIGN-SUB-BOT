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


def italic(text: str) -> str:
    """Italicize an already-escaped fragment."""

    return f"<i>{text}</i>"


# A thin rule used to separate a card's header from its body.
DIVIDER = "➖➖➖➖➖➖➖➖➖"


def label(name: str, value: str) -> str:
    """A ``<b>Name:</b> value`` body line (``name`` is escaped, ``value`` raw)."""

    return f"{bold(escape(name) + ':')} {value}"


def quote_block(lines: Iterable[str], *, expandable: bool = False) -> str:
    """Wrap pre-rendered (already escaped) lines in a single blockquote card.

    When ``expandable`` is set, Telegram renders a collapsible blockquote so
    long bodies (e.g. error traces) don't flood the chat.
    """

    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return tag + "\n".join(lines) + "</blockquote>"
