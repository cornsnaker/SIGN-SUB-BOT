"""The automated FFmpeg subtitle pipeline.

Pipeline stages:

1. ``ffprobe`` the input to map streams dynamically.
2. Extract the primary English ASS subtitle layer to a temporary ``.ass``.
3. Filter the ``[Events]`` section line-by-line, keeping only ``Dialogue`` lines
   whose text carries both an ``\\an7`` alignment and a ``\\pos(...)`` override
   (i.e. positioned signs/typesetting/SFX), dropping plain dialogue. The
   ``[Script Info]`` and ``[V4+ Styles]`` sections are preserved verbatim.
4. Remux video + audio + legacy English subtitles + the new signs track +
   attachments/fonts into ``{name}_clean_english.mkv``; non-English subtitle
   tracks are dropped. The new track is tagged ``language=eng`` /
   ``title=Signs & Songs``.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Sequence

from ..config import Config
from ..core import proc
from . import ffprobe

if TYPE_CHECKING:
    from ..core.task import ExtraAudio

ProgressCb = Callable[[str, float, float], Awaitable[None]]

# A subtitle event is treated as a "sign/song" (kept) when its text contains
# both an \an7 alignment tag and a \pos(...) override; plain dialogue has
# neither. Small spacing variations are tolerated.
_AN7_RE = re.compile(r"\\an\s*7")
_POS_RE = re.compile(r"\\pos\s*\(")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _is_sign_event(text: str) -> bool:
    """True if an event's text is a positioned sign/song (\\an7 + \\pos)."""

    return bool(_AN7_RE.search(text) and _POS_RE.search(text))


@dataclass(slots=True)
class PipelineResult:
    output_path: Path
    source_stream_index: int
    events_kept: int
    events_dropped: int
    english_sub_count: int
    temp_files: list[Path]
    extra_audio_count: int = 0
    # The extracted subtitle scripts, retained for confirmation uploads.
    full_sub_path: Optional[Path] = None
    signs_sub_path: Optional[Path] = None


class PipelineError(RuntimeError):
    """Raised when a required pipeline stage fails."""


class SubtitlePipeline:
    def __init__(self, config: Config) -> None:
        self._cfg = config

    async def process(
        self,
        mkv_path: Path,
        progress_cb: Optional[ProgressCb] = None,
        *,
        extra_audios: Optional[Sequence["ExtraAudio"]] = None,
    ) -> PipelineResult:
        temp_files: list[Path] = []
        try:
            return await self._run(mkv_path, progress_cb, temp_files, extra_audios or [])
        except Exception:
            for tmp in temp_files:
                _safe_unlink(tmp)
            raise

    async def _run(
        self,
        mkv_path: Path,
        progress_cb: Optional[ProgressCb],
        temp_files: list[Path],
        extra_audios: Sequence["ExtraAudio"],
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
        original_audio_count = len(info.audios())
        valid_audios = [a for a in extra_audios if Path(a.path).is_file()]
        duration = await self._duration(mkv_path)

        remux_cmd = [self._cfg.ffmpeg_bin, "-y", "-i", str(mkv_path), "-i", str(signs_ass)]
        # External audio inputs occupy ffmpeg input indices 2, 3, ...
        for audio in valid_audios:
            remux_cmd += ["-i", str(audio.path)]

        remux_cmd += ["-map", "0:v?"]
        # Map each external audio FIRST (input indices 2, 3, ...), then the
        # original audio. Ordering the added track ahead of the originals is the
        # key fix for the "added audio is muted" reports: many players (incl.
        # Telegram's in-app player and most mobile/hardware players) auto-play
        # the *first* audio stream by index and ignore the ``default``
        # disposition flag, so a track placed after the originals never gets
        # heard. Putting it first makes it play everywhere.
        for offset in range(len(valid_audios)):
            remux_cmd += ["-map", f"{2 + offset}:a:0"]
        remux_cmd += ["-map", "0:a?"]
        # Map English-tagged subtitles by their absolute stream index. We avoid
        # the combined ``0:s:m:language:eng`` specifier because some ffmpeg
        # builds reject it ("Failed to set value ... for option 'map'");
        # explicit ``0:<index>`` maps are portable across versions.
        for sub in english_subs:
            remux_cmd += ["-map", f"0:{sub.index}"]
        remux_cmd += [
            "-map", "1:s:0",                 # the new signs-only track
            "-map", "0:t?",                  # attachments / fonts (optional)
            "-c", "copy",                    # stream-copy everything (no re-encode)
        ]
        # Tag the new signs subtitle track.
        remux_cmd += [
            f"-metadata:s:s:{new_track_sub_index}", "language=eng",
            f"-metadata:s:s:{new_track_sub_index}", "title=Signs & Songs",
            f"-disposition:s:{new_track_sub_index}", "default",
        ]
        # Added audios are now output streams a:0 .. a:(k-1); originals follow.
        for j, audio in enumerate(valid_audios):
            remux_cmd += [
                f"-metadata:s:a:{j}", f"language={audio.language or 'und'}",
            ]
            if audio.title:
                remux_cmd += [f"-metadata:s:a:{j}", f"title={audio.title}"]
        # Make the first added track the sole default and clear default off the
        # originals (which now sit at a:k ..), so disposition-aware players also
        # select the added audio.
        if valid_audios:
            remux_cmd += ["-disposition:a:0", "default"]
            for i in range(len(valid_audios), len(valid_audios) + original_audio_count):
                remux_cmd += [f"-disposition:a:{i}", "0"]
        remux_cmd.append(str(output))

        await self._run_with_progress(remux_cmd, duration, progress_cb, "Remuxing")
        if not output.is_file():
            raise PipelineError("Remux completed but the output file is missing.")

        # NB: keep ``temp_ass`` (full extracted sub) and ``signs_ass`` (filtered
        # signs/songs) on disk so the bot can upload them as ``.txt`` for
        # confirmation; the task's cleanup removes them afterwards.
        return PipelineResult(
            output_path=output,
            source_stream_index=source.index,
            events_kept=kept,
            events_dropped=dropped,
            english_sub_count=len(english_subs),
            temp_files=[temp_ass, signs_ass],
            extra_audio_count=len(valid_audios),
            full_sub_path=temp_ass if temp_ass.is_file() else None,
            signs_sub_path=signs_ass if signs_ass.is_file() else None,
        )

    async def _run_with_progress(
        self,
        cmd: list[str],
        duration: float,
        progress_cb: Optional[ProgressCb],
        stage: str,
    ) -> None:
        rc = 0
        recent: deque[str] = deque(maxlen=40)
        async for line in proc.stream_stderr(cmd):
            if line.startswith("__RC__:"):
                rc = int(line.split(":", 1)[1])
                continue
            # ffmpeg emits a continuous in-place progress line; don't let it
            # crowd out the real diagnostics in the rolling buffer.
            if not _TIME_RE.search(line):
                recent.append(line)
            match = _TIME_RE.search(line)
            if match and duration > 0 and progress_cb:
                hrs, mins, secs = match.groups()
                done = int(hrs) * 3600 + int(mins) * 60 + float(secs)
                await progress_cb(stage, min(done, duration), duration)
        if rc != 0:
            raise PipelineError(
                f"FFmpeg {stage.lower()} failed (rc={rc}): {_ffmpeg_error_summary(recent)}"
            )
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


_ERROR_HINTS = (
    "error", "invalid", "could not", "cannot", "no such", "failed",
    "unable", "not found", "permission", "denied", "unsupported",
    "incorrect", "conversion failed", "unknown", "not currently supported",
)


def _ffmpeg_error_summary(recent: "deque[str]") -> str:
    """Build a useful error string from the tail of ffmpeg's stderr.

    ffmpeg's final line is usually a generic ``Error opening output files:
    Invalid argument``; the actionable diagnostic (e.g. an unsupported codec
    or a bad stream map) appears earlier. We surface the most relevant
    error-looking lines so failures are diagnosable instead of opaque.
    """

    lines = [ln for ln in recent if ln.strip()]
    if not lines:
        return "no stderr captured"
    flagged = [ln for ln in lines if any(h in ln.lower() for h in _ERROR_HINTS)]
    chosen = flagged[-4:] if flagged else lines[-4:]
    summary = " | ".join(chosen)
    return summary[-500:]


def _filter_ass(temp_ass: Path, output_ass: Path) -> tuple[int, int]:
    """Keep only positioned sign/song events from ``temp_ass`` -> ``output_ass``.

    A ``Dialogue`` line is kept only when its text contains both ``\\an7`` and
    ``\\pos(...)`` (positioned typesetting/signs/songs); plain dialogue is
    dropped. The ``[Script Info]`` and ``[V4+ Styles]`` sections (and any other
    non-event lines) are copied through unchanged.

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
            # Text is the 10th field; everything after the 9th comma.
            parts = line.split(",", 9)
            text = parts[9] if len(parts) > 9 else ""
            if _is_sign_event(text):
                kept += 1
                out.append(line)
            else:
                dropped += 1
            continue
        out.append(line)

    output_ass.write_text("".join(out), encoding="utf-8")
    return kept, dropped


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
