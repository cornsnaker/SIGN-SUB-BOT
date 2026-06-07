"""Asynchronous, multi-part upload of finished media back to Telegram.

Pyrogram performs chunked (multi-part) uploads internally and exposes a
progress callback per chunk. We throttle those callbacks to avoid hammering the
Telegram ``editMessageText`` rate limit and surface speed/ETA stats.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

ProgressCb = Callable[[float, float, float], Awaitable[None]]  # done, total, speed


class Uploader:
    def __init__(self, client: Client, *, min_interval: float = 5.0) -> None:
        self._client = client
        self._min_interval = min_interval

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str = "",
        progress_cb: Optional[ProgressCb] = None,
        reply_to: Optional[int] = None,
    ) -> Message:
        """Upload ``path`` as a document with throttled progress reporting."""

        state = {"last": 0.0, "start": time.monotonic()}

        async def _raw_progress(current: int, total: int) -> None:
            if progress_cb is None:
                return
            now = time.monotonic()
            # Always report the final 100% tick; throttle intermediate ones.
            if current < total and (now - state["last"]) < self._min_interval:
                return
            state["last"] = now
            elapsed = max(now - state["start"], 1e-6)
            speed = current / elapsed
            await progress_cb(float(current), float(total), speed)

        return await self._client.send_document(
            chat_id=chat_id,
            document=str(path),
            caption=caption,
            parse_mode=ParseMode.HTML,
            force_document=True,
            reply_to_message_id=reply_to,
            progress=_raw_progress,
        )
