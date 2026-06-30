"""Minimal, dependency-free helpers to recover a human filename.

Covers the three places a name can hide:

* a ``.torrent`` file's ``info.name`` (bencode),
* an HTTP ``Content-Disposition`` header,
* a percent-encoded URL path basename.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote, unquote_plus


def _bdecode(data: bytes, idx: int) -> Tuple[object, int]:
    """Decode a single bencoded value starting at ``data[idx]``."""

    prefix = data[idx : idx + 1]
    if prefix == b"i":  # integer: i<digits>e
        end = data.index(b"e", idx)
        return int(data[idx + 1 : end]), end + 1
    if prefix == b"l":  # list
        idx += 1
        items: list[object] = []
        while data[idx : idx + 1] != b"e":
            value, idx = _bdecode(data, idx)
            items.append(value)
        return items, idx + 1
    if prefix == b"d":  # dict
        idx += 1
        out: dict[bytes, object] = {}
        while data[idx : idx + 1] != b"e":
            key, idx = _bdecode(data, idx)
            value, idx = _bdecode(data, idx)
            if isinstance(key, bytes):
                out[key] = value
        return out, idx + 1
    if prefix.isdigit():  # byte string: <len>:<bytes>
        colon = data.index(b":", idx)
        length = int(data[idx:colon])
        start = colon + 1
        return data[start : start + length], start + length
    raise ValueError(f"Invalid bencode at offset {idx}")


def torrent_name(source: "str | Path | bytes") -> Optional[str]:
    """Return the display name stored in a ``.torrent`` file, if any.

    Reads ``info.name`` (single-file/folder name). Returns ``None`` if the
    file can't be parsed.
    """

    try:
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        decoded, _ = _bdecode(raw, 0)
    except (OSError, ValueError, IndexError):
        return None
    if not isinstance(decoded, dict):
        return None
    info = decoded.get(b"info")
    if not isinstance(info, dict):
        return None
    name = info.get(b"name.utf-8") or info.get(b"name")
    if isinstance(name, bytes):
        try:
            return name.decode("utf-8").strip() or None
        except UnicodeDecodeError:
            return name.decode("latin-1", errors="ignore").strip() or None
    return None


_CD_FILENAME_STAR = re.compile(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", re.IGNORECASE)
_CD_FILENAME = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


def filename_from_content_disposition(header: Optional[str]) -> Optional[str]:
    """Extract a filename from an HTTP ``Content-Disposition`` header value."""

    if not header:
        return None
    star = _CD_FILENAME_STAR.search(header)
    if star:  # RFC 5987: filename*=UTF-8''some%20name.mkv
        return unquote(star.group(1).strip()).strip() or None
    plain = _CD_FILENAME.search(header)
    if plain:
        return plain.group(1).strip() or None
    return None


def filename_from_url(url: str) -> Optional[str]:
    """Best-effort, percent-decoded basename from a URL path."""

    tail = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    name = unquote_plus(tail).strip()
    return name or None
