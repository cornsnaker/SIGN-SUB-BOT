"""Human-friendly formatting: sizes, speeds, durations and emoji progress bars.

All public renderers emit MarkdownV2-safe strings wrapped in the bot's
signature ``>`` blockquote layout.
"""

from __future__ import annotations

from typing import Optional

from . import fmt as md

_FILLED = "■"
_EMPTY = "□"
_BAR_LEN = 10


def human_size(num_bytes: Optional[float]) -> str:
    """Format a byte count as a base-1024 human readable string."""

    if not num_bytes or num_bytes < 0:
        return "0 B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024.0:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} EB"


def human_speed(bytes_per_sec: Optional[float]) -> str:
    """Format a transfer rate (bytes/second)."""

    return f"{human_size(bytes_per_sec)}/s"


def human_eta(seconds: Optional[float]) -> str:
    """Format an ETA in HH:MM:SS, capping unknown/huge values."""

    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "--:--:--"
    seconds = int(seconds)
    if seconds > 359999:  # > ~99h, treat as unknown
        return "--:--:--"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def bar(percent: float, length: int = _BAR_LEN) -> str:
    """Build an emoji progress bar for ``percent`` in the range [0, 100]."""

    pct = max(0.0, min(100.0, percent))
    filled = int(round((pct / 100.0) * length))
    filled = max(0, min(length, filled))
    return _FILLED * filled + _EMPTY * (length - filled)


def percent_of(done: float, total: float) -> float:
    if not total or total <= 0:
        return 0.0
    return (done / total) * 100.0


def render_progress(
    stage: str,
    *,
    done: float = 0.0,
    total: float = 0.0,
    speed: Optional[float] = None,
    eta: Optional[float] = None,
    extra: Optional[str] = None,
) -> str:
    """Render a full progress card as a blockquote (the ``> Quote`` layout).

    Rendered shape (sent as HTML so Telegram shows a native blockquote)::

        > 🔄 Downloading
        > Speed: `12.4 MB/s` | ETA: `00:01:42`
        > Processed: `450 MB / 900 MB`
        > [■■■■■□□□□□] `50%`
    """

    pct = percent_of(done, total) if total else 0.0
    lines = [
        md.bold(f"🔄 {md.escape(stage)}"),
        f"Speed: {md.code(human_speed(speed))} | ETA: {md.code(human_eta(eta))}",
        f"Processed: {md.code(f'{human_size(done)} / {human_size(total)}')}",
        f"[{bar(pct)}] {md.code(f'{pct:.1f}%')}",
    ]
    if extra:
        lines.append(md.escape(extra))
    return md.quote_block(lines)


def render_status(title: str, lines: Optional[list[str]] = None, *, emoji: str = "ℹ️") -> str:
    """Render a generic status card (no progress bar) as a blockquote."""

    body = [md.bold(f"{emoji} {md.escape(title)}")]
    for line in lines or []:
        body.append(md.escape(line))
    return md.quote_block(body)


def render_error(message: str, detail: Optional[str] = None) -> str:
    """Render an error card."""

    lines = [md.bold(f"❌ {md.escape('Error')}"), md.escape(message)]
    if detail:
        snippet = detail.strip().splitlines()[-1] if detail.strip() else detail
        lines.append(md.code(snippet[:200]))
    return md.quote_block(lines)
