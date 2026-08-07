"""The automated FFmpeg subtitle pipeline.

Pipeline stages:

1. ``ffprobe`` the input to map streams dynamically.
2. Extract the primary English ASS subtitle layer to a temporary ``.ass``.
3. Filter the ``[Events]`` section line-by-line, dropping any ``Dialogue`` whose
   style is named ``default`` or ``song`` -- leaving only signs/typesetting/SFX.
4. Remux video + audio + legacy English subtitles + the new signs track +
   attachments/fonts into ``{name}_clean_english.mkv``; non-English subtitle
   tracks are dropped. The new track is tagged ``language=eng`` /
   ``title=Signs & Songs``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..config import Config
from ..core import proc
from . import ffprobe

ProgressCb = Callable[[str, float, float], Awaitable[None]]

BANNED_STYLES = {"default", "song"}

# Dialogue style families recognised when auto-detecting a full .ass file's
# style scheme, in priority order. "SubsPlus+" releases use Subtitle /
# Subtitle-Alt for dialogue and Caption for positioned signs, while fansub
# releases use Default / Song for dialogue and everything else for signs.
_DIALOGUE_STYLE_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("default", "song"),
    ("subtitle", "main", "dialogue", "dialog", "text"),
)
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _in_style_family(style: str, family: tuple[str, ...]) -> bool:
    """Return True if a lower-cased style name belongs to a dialogue family."""

    for name in family:
        if style == name or style.startswith(name + "-"):
            return True
        if style.startswith(name) and style[len(name):].isdigit():
            return True
    return False


@dataclass(slots=True)
class PipelineResult:
    output_path: Path
    source_stream_index: int
    events_kept: int
    events_dropped: int
    english_sub_count: int
    temp_files: list[Path]


class PipelineError(RuntimeError):
    """Raised when a required pipeline stage fails."""


class SubtitlePipeline:
    def __init__(self, config: Config) -> None:
        self._cfg = config

    async def process(self, mkv_path: Path, progress_cb: Optional[ProgressCb] = None) -> PipelineResult:
        temp_files: list[Path] = []
        try:
            return await self._run(mkv_path, progress_cb, temp_files)
        except Exception:
            for tmp in temp_files:
                _safe_unlink(tmp)
            raise

    async def _run(
        self, mkv_path: Path, progress_cb: Optional[ProgressCb], temp_files: list[Path]
    ) -> PipelineResult:
        if not mkv_path.is_file():
            raise PipelineError(f"Input file does not exist: {mkv_path}")

        await self._emit(progress_cb, "Probing streams", 0, 1)
        info = await ffprobe.probe(mkv_path, ffprobe_bin=self._cfg.ffprobe_bin)

        source = info.first_english_ass() or info.first_ass()
        if source is None:
            raise PipelineError("No ASS/SSA subtitle track found in the file.")

        base = mkv_path.with_suffix("")
        temp_ass = Path(f"{base}_temp_full.ass")
        signs_ass = Path(f"{base}_signs.ass")
        output = Path(f"{base.name}_clean_english.mkv")
        output = mkv_path.with_name(output.name)
        temp_files.extend([temp_ass, signs_ass])

        # -- Stage 2: extract the chosen ASS track ------------------------
        await self._emit(progress_cb, "Extracting subtitles", 0, 1)
        extract_cmd = [
            self._cfg.ffmpeg_bin,
            "-y",
            "-i",
            str(mkv_path),
            "-map",
            f"0:{source.index}",
            "-c:s",
            "copy",
            str(temp_ass),
        ]
        extract = await proc.run(extract_cmd, timeout=600)
        if not extract.ok or not temp_ass.is_file():
            raise PipelineError(f"Subtitle extraction failed: {extract.stderr.strip()[-300:]}")

        # -- Stage 3: filter Dialogue lines -------------------------------
        await self._emit(progress_cb, "Filtering signs/songs", 0, 1)
        kept, dropped = _filter_ass(temp_ass, signs_ass)
        if kept == 0:
            raise PipelineError(
                "No sign/typesetting events remained after filtering out dialogue."
            )

        # -- Stage 4: remux -----------------------------------------------
        english_subs = [s for s in info.subtitles() if s.is_english]
        new_track_sub_index = len(english_subs)
        duration = await self._duration(mkv_path)

        remux_cmd = [
            self._cfg.ffmpeg_bin,
            "-y",
            "-i",
            str(mkv_path),
            "-i",
            str(signs_ass),
            "-map",
            "0:v",
            "-map",
            "0:a",
            "-map",
            "0:s:m:language:eng?",  # only English-tagged subtitles
            "-map",
            "1:s:0",  # the new signs-only track
            "-map",
            "0:t?",  # attachments / fonts (optional)
            "-c",
            "copy",
            f"-metadata:s:s:{new_track_sub_index}",
            "language=eng",
            f"-metadata:s:s:{new_track_sub_index}",
            "title=Signs & Songs",
            "-disposition:s:" + str(new_track_sub_index),
            "default",
            str(output),
        ]
        await self._run_with_progress(remux_cmd, duration, progress_cb, "Remuxing")
        if not output.is_file():
            raise PipelineError("Remux completed but the output file is missing.")

        _safe_unlink(temp_ass)
        _safe_unlink(signs_ass)
        return PipelineResult(
            output_path=output,
            source_stream_index=source.index,
            events_kept=kept,
            events_dropped=dropped,
            english_sub_count=len(english_subs),
            temp_files=[temp_ass, signs_ass],
        )

    async def _run_with_progress(
        self,
        cmd: list[str],
        duration: float,
        progress_cb: Optional[ProgressCb],
        stage: str,
    ) -> None:
        rc = 0
        last_tail = ""
        async for line in proc.stream_stderr(cmd):
            if line.startswith("__RC__:"):
                rc = int(line.split(":", 1)[1])
                continue
            last_tail = line
            match = _TIME_RE.search(line)
            if match and duration > 0 and progress_cb:
                hrs, mins, secs = match.groups()
                done = int(hrs) * 3600 + int(mins) * 60 + float(secs)
                await progress_cb(stage, min(done, duration), duration)
        if rc != 0:
            raise PipelineError(f"FFmpeg {stage.lower()} failed (rc={rc}): {last_tail[-300:]}")
        if progress_cb and duration > 0:
            await progress_cb(stage, duration, duration)

    async def _duration(self, path: Path) -> float:
        cmd = [
            self._cfg.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        result = await proc.run(cmd, timeout=60)
        try:
            return float(result.stdout.strip())
        except (ValueError, AttributeError):
            return 0.0

    @staticmethod
    async def _emit(cb: Optional[ProgressCb], stage: str, done: float, total: float) -> None:
        if cb:
            await cb(stage, done, total)


def _filter_ass(temp_ass: Path, output_ass: Path) -> tuple[int, int]:
    """Strip dialogue styles from ``temp_ass`` -> ``output_ass``.

    Returns ``(events_kept, events_dropped)`` for the ``Dialogue`` lines.
    """

    try:
        lines = temp_ass.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        lines = temp_ass.read_text(encoding="utf-8-sig").splitlines(keepends=True)

    out: list[str] = []
    in_events = False
    kept = 0
    dropped = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            out.append(line)
            continue
        if in_events and stripped.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) > 3:
                style = parts[3].strip().lower()
                if style in BANNED_STYLES:
                    dropped += 1
                    continue
                kept += 1
            out.append(line)
        else:
            out.append(line)

    output_ass.write_text("".join(out), encoding="utf-8")
    return kept, dropped


def filter_sign_styles(lines: list[str]) -> tuple[list[str], int, int]:
    """Keep only sign/SFX ``Dialogue`` lines from a list of ASS lines.

    Returns ``(output_lines, events_kept, events_dropped)``. Lines outside the
    ``[Events]`` section and non-Dialogue lines are preserved as-is.
    """

    out: list[str] = []
    in_events = False
    kept = 0
    dropped = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            out.append(line)
            continue
        if in_events and stripped.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) > 3:
                style = parts[3].strip().lower()
                if style in BANNED_STYLES:
                    dropped += 1
                    continue
                kept += 1
            out.append(line)
        else:
            out.append(line)

    return out, kept, dropped


def filter_ass_file(input_ass: Path, output_ass: Path) -> tuple[int, int]:
    """Extract sign subs from a full ``.ass`` file -> ``output_ass``.

    Returns ``(events_kept, events_dropped)`` for the ``Dialogue`` lines.
    """

    try:
        lines = input_ass.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        lines = input_ass.read_text(encoding="utf-8-sig").splitlines(keepends=True)

    out, kept, dropped = filter_sign_styles(lines)
    output_ass.write_text("".join(out), encoding="utf-8")
    return kept, dropped


def detect_dialogue_styles(lines: list[str]) -> set[str]:
    """Detect the dialogue style names used by a full ``.ass`` file.

    Returns the lower-cased style names that should be treated as dialogue and
    dropped when extracting sign subs. Falls back to ``BANNED_STYLES`` when no
    known dialogue style is present.

    Known dialogue style families (matched as exact names, ``name-N`` variants
    such as ``subtitle-alt``, and ``nameN`` suffixed variants such as
    ``subtitle2``):

    - ``default`` / ``song``            -- fansub releases (e.g. ``Fullsub.ass``)
    - ``subtitle`` / ``caption``...     -- SubsPlus+ releases (e.g. ``NEW FULL
      SUB.ass``), where ``Caption`` carries the positioned signs
    """

    present: set[str] = set()
    in_events = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            continue
        if in_events and stripped.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) > 3:
                present.add(parts[3].strip().lower())

    for family in _DIALOGUE_STYLE_FAMILIES:
        if any(_in_style_family(style, family) for style in present):
            return {style for style in present if _in_style_family(style, family)}
    return set(BANNED_STYLES)


def filter_ass_file_auto(input_ass: Path, output_ass: Path) -> tuple[int, int, set[str]]:
    """Extract sign subs from a full ``.ass`` file, auto-detecting its styles.

    Returns ``(events_kept, events_dropped, banned_styles)`` where
    ``banned_styles`` is the detected set of dialogue style names that was
    filtered out.
    """

    try:
        lines = input_ass.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        lines = input_ass.read_text(encoding="utf-8-sig").splitlines(keepends=True)

    banned = detect_dialogue_styles(lines)
    out: list[str] = []
    in_events = False
    kept = 0
    dropped = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            out.append(line)
            continue
        if in_events and stripped.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) > 3:
                style = parts[3].strip().lower()
                if style in banned:
                    dropped += 1
                    continue
                kept += 1
            out.append(line)
        else:
            out.append(line)

    output_ass.write_text("".join(out), encoding="utf-8")
    return kept, dropped, banned


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
