"""Async subprocess helpers built on :mod:`asyncio.subprocess`.

These wrappers keep the event loop responsive while shelling out to
``ffmpeg``/``ffprobe`` and let callers stream stderr for progress parsing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Sequence


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run(cmd: Sequence[str], *, timeout: Optional[float] = None) -> CommandResult:
    """Run ``cmd`` to completion and capture stdout/stderr as text."""

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return CommandResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="ignore"),
        stderr=stderr_b.decode("utf-8", errors="ignore"),
    )


async def stream_stderr(cmd: Sequence[str]) -> AsyncIterator[str]:
    """Run ``cmd`` and yield decoded stderr lines as they arrive.

    The subprocess return code is yielded as a final sentinel line of the form
    ``__RC__:<code>`` so callers can detect failure without a second await.
    """

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    buffer = b""
    while True:
        chunk = await proc.stderr.read(256)
        if not chunk:
            break
        buffer += chunk
        # ffmpeg uses \r to update the progress line in place.
        while b"\r" in buffer or b"\n" in buffer:
            sep_idx = min(
                (buffer.index(b) for b in (b"\r", b"\n") if b in buffer),
                default=-1,
            )
            if sep_idx < 0:
                break
            line, buffer = buffer[:sep_idx], buffer[sep_idx + 1 :]
            text = line.decode("utf-8", errors="ignore").strip()
            if text:
                yield text
    await proc.wait()
    if buffer.strip():
        yield buffer.decode("utf-8", errors="ignore").strip()
    yield f"__RC__:{proc.returncode if proc.returncode is not None else -1}"
