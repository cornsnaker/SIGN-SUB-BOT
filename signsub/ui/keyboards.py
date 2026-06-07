"""Inline keyboard factories.

Callback payloads use a compact ``action:token`` scheme where ``token`` is a
short task identifier. Keeping the payload tiny is important because Telegram
caps callback data at 64 bytes.
"""

from __future__ import annotations

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Callback action constants (kept short to fit the 64-byte budget).
ACT_START = "dl"
ACT_FILTER = "flt"
ACT_CANCEL = "cxl"
ACT_NYAA_PICK = "nya"
ACT_NOOP = "noop"


def source_menu(token: str) -> InlineKeyboardMarkup:
    """Initial selection menu shown when a user submits a source link."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 Start Download", callback_data=f"{ACT_START}:{token}"),
                InlineKeyboardButton("⚙️ Filter Streams", callback_data=f"{ACT_FILTER}:{token}"),
            ],
            [InlineKeyboardButton("❌ Cancel Task", callback_data=f"{ACT_CANCEL}:{token}")],
        ]
    )


def cancel_only(token: str) -> InlineKeyboardMarkup:
    """A single cancel button, shown while a task is running."""

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel Task", callback_data=f"{ACT_CANCEL}:{token}")]]
    )


def nyaa_results(token: str, count: int) -> InlineKeyboardMarkup:
    """Numbered buttons for selecting a Nyaa search result."""

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx in range(count):
        row.append(
            InlineKeyboardButton(str(idx + 1), callback_data=f"{ACT_NYAA_PICK}:{token}:{idx}")
        )
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{ACT_CANCEL}:{token}")])
    return InlineKeyboardMarkup(buttons)


def parse_callback(data: str) -> tuple[str, list[str]]:
    """Split callback data into ``(action, [args...])``."""

    parts = data.split(":")
    return parts[0], parts[1:]
