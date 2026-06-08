"""Classification of user-supplied sources into actionable download specs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SourceKind(str, Enum):
    MAGNET = "magnet"
    TORRENT_URL = "torrent_url"
    DIRECT = "direct"
    NYAA_VIEW = "nyaa_view"
    NYAA_SEARCH = "nyaa_search"
    TORRENT_FILE = "torrent_file"  # an uploaded .torrent document
    LOCAL_FILE = "local_file"  # an uploaded video file already on disk


@dataclass(slots=True)
class SourceSpec:
    kind: SourceKind
    value: str  # URL / magnet / search text / local file path
    label: str  # short human label for UI


_MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_NYAA_VIEW_RE = re.compile(r"https?://nyaa\.si/view/\d+", re.IGNORECASE)

# Common audio container/codec extensions accepted as external audio tracks.
AUDIO_EXTS = {
    ".aac", ".mp3", ".m4a", ".m4b", ".flac", ".opus", ".ogg", ".oga",
    ".wav", ".ac3", ".eac3", ".dts", ".wma", ".alac", ".mka", ".aiff",
    ".aif", ".ape", ".wv", ".mp2", ".mpa", ".caf",
}


def is_audio_url(text: str) -> bool:
    """True if ``text`` is an http(s) URL with a known audio extension."""

    candidate = text.strip().lower()
    if not _URL_RE.match(candidate):
        return False
    path = candidate.split("?", 1)[0]
    return any(path.endswith(ext) for ext in AUDIO_EXTS)


def classify(text: str) -> Optional[SourceSpec]:
    """Classify a raw text message into a :class:`SourceSpec`.

    Returns ``None`` if the text is not actionable as a source.
    """

    candidate = text.strip()
    if not candidate:
        return None

    if _MAGNET_RE.match(candidate):
        return SourceSpec(SourceKind.MAGNET, candidate, _magnet_label(candidate))

    if _NYAA_VIEW_RE.match(candidate):
        return SourceSpec(SourceKind.NYAA_VIEW, candidate, "Nyaa listing")

    if _URL_RE.match(candidate):
        lower = candidate.lower()
        # Strip query string when checking the extension.
        path = lower.split("?", 1)[0]
        if path.endswith(".torrent"):
            return SourceSpec(SourceKind.TORRENT_URL, candidate, _basename(candidate))
        return SourceSpec(SourceKind.DIRECT, candidate, _basename(candidate))

    # Treat any remaining non-URL, non-command text as a Nyaa search query.
    if not candidate.startswith("/"):
        return SourceSpec(SourceKind.NYAA_SEARCH, candidate, candidate[:48])

    return None


def torrent_file_spec(path: str, label: str) -> SourceSpec:
    return SourceSpec(SourceKind.TORRENT_FILE, path, label)


# Video container extensions accepted as a direct upload to run the pipeline on.
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".ts"}


def is_video_filename(name: str) -> bool:
    """True if ``name`` ends with a known video container extension."""

    from pathlib import PurePosixPath

    return PurePosixPath(name.lower()).suffix in VIDEO_EXTS


def local_file_spec(path: str, label: str) -> SourceSpec:
    return SourceSpec(SourceKind.LOCAL_FILE, path, label)


def _magnet_label(magnet: str) -> str:
    match = re.search(r"[?&]dn=([^&]+)", magnet)
    if match:
        from urllib.parse import unquote_plus

        return unquote_plus(match.group(1))[:48]
    ih = re.search(r"btih:([0-9a-fA-F]+)", magnet)
    return f"magnet:{ih.group(1)[:12]}" if ih else "magnet link"


def _basename(url: str) -> str:
    from ..leech.torrent_meta import filename_from_url

    name = filename_from_url(url)
    return (name or url)[:64]
