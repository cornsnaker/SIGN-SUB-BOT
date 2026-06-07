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
ACT_ADD_AUDIO = "aud"
ACT_AUDIO_LANG = "alng"
ACT_AUDIO_NAME = "anm"
ACT_NOOP = "noop"

# Common audio languages offered as buttons (ISO 639-2 code, label).
AUDIO_LANGUAGES: list[tuple[str, str]] = [
    ("eng", "English"),
    ("jpn", "Japanese"),
    ("hin", "Hindi"),
    ("spa", "Spanish"),
    ("fre", "French"),
    ("ger", "German"),
    ("ita", "Italian"),
    ("por", "Portuguese"),
    ("rus", "Russian"),
    ("ara", "Arabic"),
    ("chi", "Chinese"),
    ("kor", "Korean"),
    ("tam", "Tamil"),
    ("tel", "Telugu"),
    ("ind", "Indonesian"),
    ("und", "Other / Undetermined"),
]

# Preset track titles offered as buttons. A separate sentinel tells the handler
# to reuse the uploaded file's name as the title.
AUDIO_NAME_PRESETS: list[str] = [
    "Original",
    "Dub",
    "English Dub",
    "Commentary",
    "Karaoke",
    "Surround 5.1",
    "Stereo",
]
AUDIO_NAME_USE_FILENAME = "file"


def source_menu(token: str, *, audio_count: int = 0) -> InlineKeyboardMarkup:
    """Initial selection menu shown when a user submits a source link."""

    audio_label = "🎵 Add Audio" if not audio_count else f"🎵 Add Audio ({audio_count})"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 Start Download", callback_data=f"{ACT_START}:{token}"),
                InlineKeyboardButton("⚙️ Filter Streams", callback_data=f"{ACT_FILTER}:{token}"),
            ],
            [InlineKeyboardButton(audio_label, callback_data=f"{ACT_ADD_AUDIO}:{token}")],
            [InlineKeyboardButton("❌ Cancel Task", callback_data=f"{ACT_CANCEL}:{token}")],
        ]
    )


def audio_language_menu(token: str) -> InlineKeyboardMarkup:
    """Grid of language buttons for the audio track being added."""

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, label in AUDIO_LANGUAGES:
        row.append(InlineKeyboardButton(label, callback_data=f"{ACT_AUDIO_LANG}:{token}:{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{ACT_CANCEL}:{token}")])
    return InlineKeyboardMarkup(buttons)


def audio_name_menu(token: str) -> InlineKeyboardMarkup:
    """Grid of preset track-title buttons for the audio track being added."""

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, name in enumerate(AUDIO_NAME_PRESETS):
        row.append(InlineKeyboardButton(name, callback_data=f"{ACT_AUDIO_NAME}:{token}:{idx}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(
        [
            InlineKeyboardButton(
                "📄 Use file name",
                callback_data=f"{ACT_AUDIO_NAME}:{token}:{AUDIO_NAME_USE_FILENAME}",
            )
        ]
    )
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{ACT_CANCEL}:{token}")])
    return InlineKeyboardMarkup(buttons)


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
