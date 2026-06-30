"""Anime metadata: filename parsing, AniList lookup and a rich upload caption.

After the Signs & Songs MKV is built, this module derives a clean, human title
for the upload by:

1. Parsing the *source* filename with :mod:`anitopy` (title, episode, season,
   release group, source/quality terms).
2. Looking up the canonical title (and the season's total episode count for
   ``[END]`` detection) on the **AniList** GraphQL API.
3. Detecting the video codec / resolution from the remuxed output.
4. Computing the output's **CRC32** and publishing a **MediaInfo** report to
   Telegraph, linking the report from the caption.

The approach (anitopy + AniList + MediaInfo->Telegraph caption) is adapted from
``Nubuki-all/Enc``'s ``ani_utils``; the implementation here is self-contained and
free of that project's framework.

Every network/IO step is best-effort: any failure degrades gracefully to a
plain caption rather than failing the task (the MKV is already produced).
"""

from __future__ import annotations

import asyncio
import logging
import re
import string
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import anitopy

from ..ui import fmt as md

log = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"

# A trimmed AniList query: just what the caption / thumbnail need.
_ANILIST_QUERY = """
query ($search: String, $type: MediaType) {
  Media(search: $search, type: $type) {
    title { romaji english }
    format
    source(version: 2)
    episodes
    season
    seasonYear
    coverImage { extraLarge large }
  }
}
"""

# anitopy occasionally puts a real episode title alongside encoder noise; drop
# anything that is clearly a technical term rather than a human title.
_NOT_A_TITLE = (
    "END", "MULTi", "WEB", "WEB-DL", "WEBDL", "DDP5.1", "DDP2.0",
    "AAC2", "BluRay", "BD", "HEVC", "AVC",
)


@dataclass(slots=True)
class MediaMeta:
    """Everything the upload caption needs, all optional/best-effort."""

    title: str
    episode: Optional[str] = None
    version: Optional[str] = None
    season: Optional[str] = None
    episode_title: Optional[str] = None
    source: Optional[str] = None  # WEB-DL / BD / TV ...
    sub_type: str = "Eng-Sub"  # computed audio/sub tag, e.g. "(Dual-Audio)(Eng-sub)"
    audio_tag: str = ""  # short filename tag, e.g. "Tri" / "Dual" / "Jpn"
    codec: str = ""  # e.g. "[HEVC] [1080p]"
    crc32: Optional[str] = None
    mediainfo_url: Optional[str] = None
    cover_url: Optional[str] = None  # AniList cover image, used as the thumbnail
    is_end: bool = False


def audio_short_tag(audio_langs: list[str]) -> str:
    """Short audio tag for the filename, mirroring Enc's get_file_tag.

    Examples: ``MULTi`` (>3), ``Tri`` (3), ``Dual`` (2 distinct langs), or the
    single language capitalized (e.g. ``Jpn``, ``Eng``). Returns ``""`` when the
    only language is undetermined.
    """

    audios = [a for a in audio_langs if a and a.lower() != "und"]
    n_audio = len(audios)
    if n_audio > 3:
        return "MULTi"
    if n_audio == 3:
        return "Tri"
    if n_audio == 2:
        return "Dual" if audios[0] != audios[1] else audios[0].title()
    if n_audio == 1:
        return audios[0].title()
    return ""


def type_tag(audio_langs: list[str], sub_langs: list[str]) -> str:
    """Build the audio/subtitle "type" descriptor, mirroring Enc's get_file_tag.

    Examples: ``(Dual-Audio)``, ``(Tri-Audio)``, ``(Multi-Audio)[5]``,
    ``(Multi-Subs)[4]``, ``(Eng-sub)``, ``(eng & jpn subs)``.
    """

    out = ""
    audios = [a for a in audio_langs if a]
    subs = [s for s in sub_langs if s]

    n_audio = len(audios)
    if n_audio > 3:
        out += f"(Multi-Audio)[{n_audio}]"
    elif n_audio == 3:
        out += "(Tri-Audio)"
    elif n_audio == 2 and audios[0] != audios[1]:
        out += "(Dual-Audio)"

    n_sub = len(subs)
    if n_sub > 2:
        out += f"(Multi-Subs)[{n_sub}]"
    elif n_sub == 2:
        out += (
            f"({subs[0]} & {subs[1]} subs)"
            if subs[0] != subs[1]
            else f"({subs[0].title()}-subs)"
        )
    elif n_sub == 1:
        out += f"({subs[0].title()}-sub)"

    return out or "Eng-Sub"


def parse_filename(name: str) -> dict:
    """Parse a media filename into anitopy's field dict (never raises)."""

    try:
        return anitopy.parse(name) or {}
    except Exception:  # noqa: BLE001 - anitopy can throw on odd input
        log.warning("anitopy failed to parse %r", name, exc_info=True)
        return {}


def _norm_season(parsed: dict) -> Optional[str]:
    season = parsed.get("anime_season")
    if isinstance(season, list):
        season = season[0] if season else None
    if not season:
        return None
    season = str(season).lstrip("0") or "0"
    # Season 1 is the default; only surface it when it's > 1.
    return None if season in {"0", "1"} else season


def codec_tag(video_codec: Optional[str], height: Optional[int]) -> str:
    """Build a ``[HEVC] [1080p]`` style tag from the video stream."""

    parts: list[str] = []
    codec_map = {
        "hevc": "HEVC", "h265": "HEVC", "x265": "HEVC",
        "h264": "AVC", "avc": "AVC", "x264": "AVC",
        "av1": "AV1", "libsvtav1": "AV1",
        "vp9": "VP9",
    }
    if video_codec:
        mapped = codec_map.get(video_codec.lower())
        if mapped:
            parts.append(f"[{mapped}]")
    if height:
        ladder = [2160, 1440, 1080, 720, 540, 480, 360, 240]
        nearest = min(ladder, key=lambda h: abs(h - height))
        parts.append(f"[{nearest}p]")
    return " ".join(parts)


async def crc32(path: Path, chunk_size: int = 1 << 20) -> Optional[str]:
    """Compute the CRC-32 of ``path`` off the event loop."""

    def _compute() -> str:
        checksum = 0
        with open(path, "rb") as handle:
            while chunk := handle.read(chunk_size):
                checksum = zlib.crc32(chunk, checksum)
        return f"{checksum & 0xFFFFFFFF:08X}"

    try:
        return await asyncio.to_thread(_compute)
    except OSError:
        log.warning("CRC32 failed for %s", path, exc_info=True)
        return None


async def mediainfo_url(path: Path, *, author: str = "SignSub") -> Optional[str]:
    """Render a MediaInfo HTML report and publish it to Telegraph.

    Imports are local so the bot still starts if the optional ``pymediainfo`` /
    ``html_telegraph_poster`` deps are absent.
    """

    def _publish() -> Optional[str]:
        import pymediainfo
        from html_telegraph_poster import TelegraphPoster

        html = pymediainfo.MediaInfo.parse(str(path), output="HTML", full=False)
        if len(html) > 65000:
            html = html[:65000] + "<br><strong>(truncated)</strong>"
        poster = TelegraphPoster(use_api=True)
        poster.create_api_token("MediaInfo")
        page = poster.post(title="MediaInfo", author=author, text=html)
        return page.get("url")

    try:
        return await asyncio.to_thread(_publish)
    except Exception:  # noqa: BLE001 - network/parse/import all best-effort
        log.warning("MediaInfo->Telegraph failed for %s", path, exc_info=True)
        return None


async def _anilist_lookup(title: str, season: Optional[str]) -> dict:
    """Fetch canonical title + episode count from AniList (best-effort)."""

    search = f"{title} {season}" if season else title

    async def _query(term: str) -> Optional[dict]:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                ANILIST_URL,
                json={"query": _ANILIST_QUERY, "variables": {"search": term, "type": "ANIME"}},
                timeout=aiohttp.ClientTimeout(total=15),
            )
            payload = await resp.json()
        return (payload.get("data") or {}).get("Media")

    try:
        media = await _query(search)
        if media is None and season:
            media = await _query(title)
        return media or {}
    except Exception:  # noqa: BLE001 - never block the upload on AniList
        log.warning("AniList lookup failed for %r", search, exc_info=True)
        return {}


async def build_meta(
    *,
    source_name: str,
    output_path: Path,
    video_codec: Optional[str],
    video_height: Optional[int],
    audio_langs: Optional[list[str]] = None,
    sub_langs: Optional[list[str]] = None,
    anilist: bool = True,
    telegraph_author: str = "SignSub",
) -> MediaMeta:
    """Assemble a :class:`MediaMeta` for ``output_path`` (never raises)."""

    parsed = parse_filename(source_name)
    raw_title = parsed.get("anime_title") or Path(source_name).stem
    episode = parsed.get("episode_number")
    if isinstance(episode, list):
        episode = episode[0] if episode else None
    version = parsed.get("release_version")
    season = _norm_season(parsed)
    source = parsed.get("source")
    episode_title = parsed.get("episode_title")
    if episode_title and any(term in episode_title for term in _NOT_A_TITLE):
        episode_title = None

    title = string.capwords(str(raw_title))
    total_episodes: Optional[str] = None
    cover_url: Optional[str] = None
    if anilist:
        media = await _anilist_lookup(str(raw_title), season)
        names = media.get("title") or {}
        canonical = names.get("english") or names.get("romaji")
        if canonical:
            title = str(canonical)
        if media.get("episodes"):
            total_episodes = str(media["episodes"])
        cover = media.get("coverImage") or {}
        cover_url = cover.get("extraLarge") or cover.get("large")

    is_end = bool(
        episode and total_episodes and str(episode).lstrip("0") == total_episodes
    )

    # Run the two independent IO steps concurrently.
    crc_task = crc32(output_path)
    mi_task = mediainfo_url(output_path, author=telegraph_author)
    crc_value, mi_value = await asyncio.gather(crc_task, mi_task)

    return MediaMeta(
        title=title,
        episode=str(episode) if episode is not None else None,
        version=str(version) if version else None,
        season=season,
        episode_title=episode_title,
        source=str(source) if source else None,
        sub_type=type_tag(audio_langs or [], sub_langs or []),
        audio_tag=audio_short_tag(audio_langs or []),
        codec=codec_tag(video_codec, video_height),
        crc32=crc_value,
        mediainfo_url=mi_value,
        cover_url=cover_url,
        is_end=is_end,
    )


_FS_UNSAFE = re.compile(r'[\\/:*?"<>|]+')


def clean_filename(
    meta: MediaMeta,
    fallback_stem: str,
    suffix: str = ".mkv",
    *,
    release_name: str = "",
) -> str:
    """Build a clean output filename from parsed metadata, mirroring Enc.

    e.g. ``[CR] Yowayowa Sensei S2 - 04 [END] [Tri].mkv``. The optional
    ``release_name`` (e.g. ``[CR]``) is prepended and the short audio tag
    (``Tri``/``Dual``/``Jpn`` …) is appended in brackets. Falls back to the
    original stem when there isn't enough metadata to improve on it.
    """

    prefix = f"{release_name.strip()} " if release_name.strip() else ""

    if not meta.episode:
        base = meta.title or fallback_stem
        return _FS_UNSAFE.sub("", prefix + base).strip() + suffix

    name = prefix + meta.title
    if meta.season:
        name += f" S{meta.season}"
    name += f" - {meta.episode}"
    if meta.version:
        name += f"v{meta.version}"
    if meta.is_end:
        name += " [END]"
    if meta.audio_tag:
        name += f" [{meta.audio_tag}]"
    return _FS_UNSAFE.sub("", name).strip() + suffix


async def download_cover(url: str, dest: Path) -> Optional[Path]:
    """Download the AniList cover image to ``dest`` for use as a thumbnail."""

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=20))
            if resp.status != 200:
                return None
            data = await resp.read()
        dest.write_bytes(data)
        return dest
    except Exception:  # noqa: BLE001 - thumbnail is purely cosmetic
        log.warning("Cover download failed for %s", url, exc_info=True)
        return None


def _human_size(num_bytes: float) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PB"


def _human_secs(seconds: Optional[float]) -> Optional[str]:
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def render_caption(meta: MediaMeta, *, deco: str = "◎", link: str = "") -> str:
    """Render the rich upload caption as a blockquote card.

    Layout mirrors the reference bot::

        ◎ Title:   `…`
        ◎ Episode: `…`
        ◎ Season:  `…`
        ◎ Type:    [Eng-Sub](mediainfo-url)   [END]
        🌟:        `[HEVC] [1080p]` `[WEB-DL]`
        ◎ CRC32:   `[ABCD1234]`
        🔗 <link>
    """

    lines = [md.label(f"{deco} Title", md.code(meta.title))]
    if meta.episode:
        epi = md.code(meta.episode)
        if meta.version:
            epi += f" (v{md.escape(meta.version)})"
        lines.append(md.label(f"{deco} Episode", epi))
    if meta.season:
        lines.append(md.label(f"{deco} Season", md.code(meta.season)))

    type_value = (
        md.link(meta.sub_type, meta.mediainfo_url)
        if meta.mediainfo_url
        else md.code(meta.sub_type)
    )
    if meta.is_end:
        type_value += f" {md.bold('[END]')}"
    lines.append(md.label(f"{deco} Type", type_value))

    if meta.episode_title:
        lines.append(md.label(f"{deco} Episode Title", md.code(meta.episode_title)))

    if meta.codec or meta.source:
        star = "🌟: "
        if meta.codec:
            star += md.code(meta.codec)
        if meta.source:
            star += f" {md.code(f'[{meta.source}]')}"
        lines.append(star.strip())

    if meta.crc32:
        lines.append(md.label(f"{deco} CRC32", md.code(f"[{meta.crc32}]")))

    if link:
        lines.append(f"🔗 {md.bold(md.escape(link))}")

    return md.quote_block(lines)


def render_stats(
    *,
    original_size: int = 0,
    output_size: int = 0,
    download_secs: Optional[float] = None,
    process_secs: Optional[float] = None,
    upload_secs: Optional[float] = None,
) -> Optional[str]:
    """Render an optional remux-stats card (the remux analogue of Enc's
    "Encode Stats"): sizes, the size delta, and per-stage timings.

    Returns ``None`` when there's nothing meaningful to show.
    """

    lines: list[str] = []
    if original_size and output_size:
        delta = (output_size / original_size) * 100.0
        lines.append(md.label("Original Size", md.code(_human_size(original_size))))
        lines.append(md.label("Output Size", md.code(_human_size(output_size))))
        lines.append(md.label("Size", md.code(f"{delta:.1f}% of source")))
    elif output_size:
        lines.append(md.label("Output Size", md.code(_human_size(output_size))))

    timings = [
        ("Downloaded in", _human_secs(download_secs)),
        ("Processed in", _human_secs(process_secs)),
        ("Uploaded in", _human_secs(upload_secs)),
    ]
    timing_lines = [md.label(label, md.code(value)) for label, value in timings if value]

    if not lines and not timing_lines:
        return None

    body = [md.bold("📊 Stats"), md.DIVIDER, *lines]
    if timing_lines:
        if lines:
            body.append(md.DIVIDER)
        body.extend(timing_lines)
    return md.quote_block(body)
