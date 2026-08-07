"""In-memory log capture used by the ``/logs`` command and error reports.

Two pieces:

* :class:`LogBuffer` -- a :class:`logging.Handler` that keeps the most recent
  formatted records in a thread-safe ring buffer, optionally tagging each entry
  with the task token of the currently running task.
* :func:`task_log_context` -- a context manager (driven by a
  :class:`contextvars.ContextVar`) that scopes every record emitted inside a
  task runner to that task's token, so a failure can ship just its own logs.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections import deque
from contextlib import contextmanager
from typing import Iterator, Optional

_current_task_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "signsub_task_token", default=None
)


@contextmanager
def task_log_context(token: str) -> Iterator[None]:
    """Tag all log records emitted inside the block with ``token``."""

    reset = _current_task_token.set(token)
    try:
        yield
    finally:
        _current_task_token.reset(reset)


class LogBuffer(logging.Handler):
    """Ring buffer of recent log records, grouped per task when scoped."""

    def __init__(self, capacity: int = 500, task_capacity: int = 100) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._entries: deque[str] = deque(maxlen=capacity)
        self._task_entries: dict[str, deque[str]] = {}
        self._task_capacity = task_capacity

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - logging must never raise
            return
        token = _current_task_token.get()
        with self._lock:
            self._entries.append(line)
            if token:
                buf = self._task_entries.get(token)
                if buf is None:
                    buf = deque(maxlen=self._task_capacity)
                    self._task_entries[token] = buf
                buf.append(line)

    def tail(self, limit: int = 50) -> list[str]:
        """Return the most recent ``limit`` formatted log lines."""

        with self._lock:
            entries = list(self._entries)
        return entries[-limit:] if limit > 0 else entries

    def for_task(self, token: str) -> list[str]:
        """Return the log lines captured while ``token``'s context was active."""

        with self._lock:
            buf = self._task_entries.get(token)
            return list(buf) if buf else []

    def render(self, limit: int = 50) -> str:
        """Render the recent log tail as plain text."""

        return "\n".join(self.tail(limit)) or "(no log entries captured yet)"


#: Shared instance installed by ``__main__._configure_logging``.
buffer = LogBuffer()
