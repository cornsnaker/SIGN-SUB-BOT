"""A throttled, edit-in-place status message backed by a Telegram message.

The reporter keeps a single message updated with HTML blockquote cards. Edits
are throttled and de-duplicated (Telegram rejects identical edits) to stay
within rate limits.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, Message


class StatusReporter:
    def __init__(self, client: Client, message: Message, *, min_interval: float = 5.0) -> None:
        self._client = client
        self._message = message
        self._min_interval = min_interval
        self._last_edit = 0.0
        self._last_text: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def message(self) -> Message:
        return self._message

    async def update(
        self,
        text: str,
        *,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        force: bool = False,
    ) -> None:
        """Edit the backing message, throttling and de-duplicating edits."""

        async with self._lock:
            now = time.monotonic()
            if not force and (now - self._last_edit) < self._min_interval:
                return
            if text == self._last_text and reply_markup is None:
                return
            try:
                await self._message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                self._last_text = text
                self._last_edit = now
            except MessageNotModified:
                self._last_edit = now
            except FloodWait as exc:
                await asyncio.sleep(int(getattr(exc, "value", 3)) + 1)
                # Reset the throttle window so the next call does not retry
                # immediately and trigger another FloodWait.
                self._last_edit = time.monotonic()
            except Exception:
                # A failed status edit must never abort the underlying task.
                pass

    async def finalize(self, text: str, *, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
        await self.update(text, reply_markup=reply_markup, force=True)
