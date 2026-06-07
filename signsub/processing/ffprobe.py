"""Dynamic stream introspection via ``ffprobe`` JSON output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core import proc


@dataclass(slots=True)
class Stream:
    index: int
    codec_type: str  # video | audio | subtitle | attachment | data
    codec_name: str
    language: Optional[str]
    title: Optional[str]
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_subtitle(self) -> bool:
        return self.codec_type == "subtitle"

    @property
    def is_english(self) -> bool:
        return (self.language or "").lower() in {"eng", "en", "english"}

    @property
    def is_ass(self) -> bool:
        return self.codec_name.lower() in {"ass", "ssa"}


@dataclass(slots=True)
class MediaInfo:
    path: Path
    streams: list[Stream]

    def subtitles(self) -> list[Stream]:
        return [s for s in self.streams if s.is_subtitle]

    def first_english_ass(self) -> Optional[Stream]:
        """Return the first English ASS/SSA subtitle stream, if any."""

        for stream in self.streams:
            if stream.is_subtitle and stream.is_ass and stream.is_english:
                return stream
        return None

    def first_ass(self) -> Optional[Stream]:
        for stream in self.streams:
            if stream.is_subtitle and stream.is_ass:
                return stream
        return None

    def describe(self) -> list[str]:
        """Human-readable per-stream summary lines (for the Filter Streams view)."""

        lines: list[str] = []
        for s in self.streams:
            lang = s.language or "und"
            title = f" [{s.title}]" if s.title else ""
            lines.append(f"#{s.index} {s.codec_type}/{s.codec_name} ({lang}){title}")
        return lines


async def probe(path: Path, *, ffprobe_bin: str = "ffprobe") -> MediaInfo:
    """Run ffprobe and parse its JSON stream layout."""

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
    ]
    result = await proc.run(cmd, timeout=120)
    if not result.ok:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")

    payload = json.loads(result.stdout or "{}")
    streams: list[Stream] = []
    for raw in payload.get("streams", []):
        tags = {str(k).lower(): str(v) for k, v in (raw.get("tags") or {}).items()}
        streams.append(
            Stream(
                index=int(raw.get("index", 0)),
                codec_type=str(raw.get("codec_type", "")),
                codec_name=str(raw.get("codec_name", "")),
                language=tags.get("language"),
                title=tags.get("title"),
                tags=tags,
            )
        )
    return MediaInfo(path=path, streams=streams)
