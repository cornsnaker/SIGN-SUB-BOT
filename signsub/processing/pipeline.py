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
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


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


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
