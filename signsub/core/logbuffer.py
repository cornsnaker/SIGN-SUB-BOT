"""In-memory ring buffer of recent log records, exposed via the ``/logs`` command.

A :class:`RingBufferHandler` is attached to the root logger at start-up. It keeps
the last ``capacity`` formatted log lines in a bounded :class:`collections.deque`
so an admin can tail them from Telegram without shell access to the host.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque

_DEFAULT_CAPACITY = 400


class RingBufferHandler(logging.Handler):
    """A logging handler that retains the most recent formatted records."""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        super().__init__()
        self._lines: Deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def tail(self, count: int) -> list[str]:
        """Return up to ``count`` most recent lines, oldest first."""

        if count <= 0:
            return []
        lines = list(self._lines)
        return lines[-count:]


# Process-wide singleton; installed by ``install()`` and read by ``/logs``.
_buffer: RingBufferHandler | None = None


def install(capacity: int = _DEFAULT_CAPACITY) -> RingBufferHandler:
    """Attach a ring-buffer handler to the root logger (idempotent)."""

    global _buffer
    if _buffer is None:
        handler = RingBufferHandler(capacity)
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
        )
        logging.getLogger().addHandler(handler)
        _buffer = handler
    return _buffer


def get_buffer() -> RingBufferHandler | None:
    return _buffer
