"""Task orchestration: ties leeching, processing and uploading together.

Each task runs as its own asyncio task, bounded by a global concurrency
semaphore. Every stage reports progress through the task's
:class:`StatusReporter`, and a ``finally`` block guarantees temporary assets are
purged whether the task succeeds, fails or is cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

import aiohttp
from pyrogram import Client

from ..config import Config
from ..leech import torrent_meta
from ..leech.aria2_client import Aria2Client
from ..leech.daemon import Aria2Daemon
from ..leech.engine import LeechEngine, LeechError
from ..leech.nyaa import NyaaScraper
from ..processing.pipeline import PipelineError, PipelineResult, SubtitlePipeline
from ..ui import keyboards as kb
from ..ui import progress as pg
from ..upload.uploader import Uploader
from .sources import SourceKind, SourceSpec
from .status import StatusReporter
from .task import AUDIO_AWAIT_FILE, Task, TaskState, new_token

log = logging.getLogger(__name__)

_VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".ts"}
# Evict tasks the user created but never started after this long, and never
# keep more than this many un-started tasks in the registry.
_PENDING_TTL_SECONDS = 3600.0
_MAX_PENDING_TASKS = 200


class TaskManager:
    def __init__(self, client: Client, config: Config) -> None:
        self._client = client
        self._cfg = config
        self._tasks: dict[str, Task] = {}
        self._runners: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_tasks)

        # Lightweight bot-wide metrics for the /stats and /users commands.
        self._started_at = time.time()
        self._total_created = 0
        self._outcomes: dict[TaskState, int] = {
            TaskState.DONE: 0,
            TaskState.FAILED: 0,
            TaskState.CANCELLED: 0,
        }
        self._seen_users: set[int] = set()

        self._daemon = Aria2Daemon(config)
        self._aria2 = Aria2Client(config.aria2_rpc_url, config.aria2_secret)
        self._scraper = NyaaScraper()
        self._engine = LeechEngine(self._aria2, self._scraper)
        self._pipeline = SubtitlePipeline(config)
        self._uploader = Uploader(client, min_interval=config.progress_update_interval)

    # -- lifecycle ----------------------------------------------------------

    async def startup(self) -> None:
        await self._daemon.ensure_running()
        await self._aria2.connect()

    async def shutdown(self) -> None:
        for runner in list(self._runners.values()):
            runner.cancel()
        await self._aria2.close()
        await self._daemon.shutdown()

    # -- registry -----------------------------------------------------------

    def create_task(self, *, chat_id: int, user_id: int, trigger_message_id: int, spec: SourceSpec) -> Task:
        self._prune_stale()
        token = new_token()
        task = Task(
            token=token,
            chat_id=chat_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            spec=spec,
        )
        self._tasks[token] = task
        self._total_created += 1
        if user_id:
            self._seen_users.add(user_id)
        return task

    def _prune_stale(self) -> None:
        """Drop tasks that were created but never started (avoids leaks).

        A task only leaves ``_tasks`` via ``_execute``'s ``finally`` block, so a
        user who requests a source and never taps *Start* would otherwise leak a
        :class:`Task` (and any downloaded ``.torrent`` file) forever.
        """

        now = time.monotonic()
        pending = [
            (t.created_at, tok)
            for tok, t in self._tasks.items()
            if tok not in self._runners and t.state == TaskState.PENDING
        ]
        expired = {tok for created, tok in pending if (now - created) > _PENDING_TTL_SECONDS}
        if len(pending) - len(expired) > _MAX_PENDING_TASKS:
            pending.sort()  # oldest first
            overflow = len(pending) - len(expired) - _MAX_PENDING_TASKS
            for _created, tok in pending:
                if overflow <= 0:
                    break
                if tok not in expired:
                    expired.add(tok)
                    overflow -= 1
        for tok in expired:
            self._discard_pending(tok)

    def _discard_pending(self, token: str) -> None:
        task = self._tasks.pop(token, None)
        if task is None:
            return
        # Remove a not-yet-started local .torrent file, if one was saved.
        if task.spec.kind == SourceKind.TORRENT_FILE:
            try:
                Path(task.spec.value).unlink(missing_ok=True)
            except OSError:
                pass
        # Remove the staging dir holding a not-yet-started uploaded video.
        if task.spec.kind == SourceKind.LOCAL_FILE and task.work_subdir:
            shutil.rmtree(task.work_subdir, ignore_errors=True)
        # Remove any staged external audio for an abandoned task.
        if task.audio_dir and task.audio_dir.exists():
            shutil.rmtree(task.audio_dir, ignore_errors=True)

    def get(self, token: str) -> Optional[Task]:
        return self._tasks.get(token)

    @property
    def scraper(self) -> NyaaScraper:
        return self._scraper

    # -- introspection (admin commands) -------------------------------------

    def list_tasks(self) -> list[Task]:
        """All tasks currently tracked (pending + active), newest first."""

        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    def active_tasks(self) -> list[Task]:
        """Tasks that have a running coroutine (i.e. past the menu stage)."""

        return [t for t in self.list_tasks() if t.token in self._runners]

    def seen_user_ids(self) -> list[int]:
        return sorted(self._seen_users)

    def stats(self) -> dict[str, object]:
        """A snapshot of bot-wide counters for the /stats command."""

        per_state: dict[str, int] = {}
        for task in self._tasks.values():
            per_state[task.state.value] = per_state.get(task.state.value, 0) + 1
        return {
            "uptime_seconds": max(0.0, time.time() - self._started_at),
            "total_created": self._total_created,
            "completed": self._outcomes[TaskState.DONE],
            "failed": self._outcomes[TaskState.FAILED],
            "cancelled": self._outcomes[TaskState.CANCELLED],
            "active": len(self._runners),
            "tracked": len(self._tasks),
            "unique_users": len(self._seen_users),
            "per_state": per_state,
            "max_concurrent": self._cfg.max_concurrent_tasks,
        }

    # -- external audio -----------------------------------------------------

    def awaiting_audio_task(self, chat_id: int) -> Optional[Task]:
        """Return the (un-started) task in ``chat_id`` waiting for an audio file."""

        for task in self._tasks.values():
            if (
                task.chat_id == chat_id
                and task.state == TaskState.PENDING
                and task.audio_stage == AUDIO_AWAIT_FILE
            ):
                return task
        return None

    def audio_dir_for(self, task: Task) -> Path:
        """Per-task staging directory for external audio uploads/downloads."""

        if task.audio_dir is None:
            task.audio_dir = self._cfg.download_dir / "audio" / task.token
        task.audio_dir.mkdir(parents=True, exist_ok=True)
        return task.audio_dir

    def audio_dest(self, task: Task, name: str) -> Path:
        """A collision-free destination path for an audio file named ``name``."""

        dest_dir = self.audio_dir_for(task)
        dest = dest_dir / (name or "audio")
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        return dest

    def local_video_dest(self, task: Task, name: str) -> Path:
        """A destination path (inside the task's work dir) for an uploaded video.

        Placing it under ``download_dir / token`` means the standard task
        ``finally`` cleanup (which removes that directory) reclaims the file.
        """

        base = Path(name).name or "upload.mkv"
        if Path(base).suffix.lower() not in _VIDEO_EXTS:
            base += ".mkv"
        dest_dir = self._cfg.download_dir / task.token
        dest_dir.mkdir(parents=True, exist_ok=True)
        task.work_subdir = dest_dir
        return dest_dir / base

    async def stage_remote_audio(self, task: Task, url: str) -> Path:
        """Download a remote audio file to the task's audio staging dir.

        The filename is taken from the ``Content-Disposition`` header when the
        server provides one, otherwise from the URL's (percent-decoded) path.
        """

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                name = (
                    torrent_meta.filename_from_content_disposition(
                        resp.headers.get("Content-Disposition")
                    )
                    or torrent_meta.filename_from_url(url)
                    or "audio"
                )
                dest = self.audio_dest(task, name)
                with dest.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        fh.write(chunk)
        return dest

    def attach_reporter(self, task: Task, reporter: StatusReporter) -> None:
        task.reporter = reporter

    def cancel(self, token: str) -> bool:
        task = self._tasks.get(token)
        if not task:
            return False
        task.cancel()
        runner = self._runners.get(token)
        if runner:
            runner.cancel()
        else:
            # No runner means the task was never launched (still collecting a
            # source/audio). Drop it now so it stops intercepting messages and
            # its staged audio is cleaned up; a running task is cleaned by its
            # own finally-block instead.
            self._discard_pending(token)
        return True

    def launch(self, task: Task) -> bool:
        """Schedule ``task`` to run on the event loop.

        Returns ``False`` (and does nothing) if the task has already been
        launched -- this guards against a user double-tapping *Start Download*
        before the keyboard is removed, which would otherwise spawn a second
        runner racing on the same work directory. Safe because this method is
        synchronous: the state flip happens with no intervening ``await``.
        """

        if task.token in self._runners or task.state != TaskState.PENDING:
            return False
        task.state = TaskState.QUEUED
        runner = asyncio.create_task(self._execute(task), name=f"task-{task.token}")
        self._runners[task.token] = runner
        runner.add_done_callback(lambda _t, tok=task.token: self._runners.pop(tok, None))
        return True

    # -- execution ----------------------------------------------------------

    async def _execute(self, task: Task) -> None:
        reporter = task.reporter
        work_subdir = self._cfg.download_dir / task.token
        task.work_subdir = work_subdir
        try:
            task.state = TaskState.QUEUED
            if reporter:
                await reporter.update(
                    pg.render_status("Queued", ["Waiting for a free worker slot..."], emoji="⏳"),
                    reply_markup=kb.cancel_only(task.token),
                    force=True,
                )

            async with self._semaphore:
                if task.cancelled:
                    raise asyncio.CancelledError()
                await self._do_download(task, work_subdir)
                result = await self._do_process(task)
                await self._do_upload(task, result)

            task.state = TaskState.DONE
            if reporter:
                await reporter.finalize(
                    pg.render_status(
                        "Task Complete",
                        ["Your clean English file has been uploaded."],
                        emoji="🎉",
                    )
                )
        except asyncio.CancelledError:
            task.state = TaskState.CANCELLED
            if reporter:
                await reporter.finalize(
                    pg.render_status("Task Cancelled", ["All assets were cleaned up."], emoji="🛑")
                )
        except (LeechError, PipelineError) as exc:
            task.state = TaskState.FAILED
            if reporter:
                await reporter.finalize(pg.render_error(str(exc)))
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
            task.state = TaskState.FAILED
            if reporter:
                await reporter.finalize(pg.render_error("Unexpected failure", repr(exc)))
        finally:
            if task.state in self._outcomes:
                self._outcomes[task.state] += 1
            await self._cleanup(task)
            self._tasks.pop(task.token, None)

    async def _do_download(self, task: Task, work_subdir: Path) -> None:
        task.state = TaskState.DOWNLOADING
        reporter = task.reporter
        work_subdir.mkdir(parents=True, exist_ok=True)

        async def progress(stage, done, total, speed, eta):  # type: ignore[no-untyped-def]
            if reporter:
                await reporter.update(
                    pg.render_progress(stage, done=done, total=total, speed=speed, eta=eta),
                    reply_markup=kb.cancel_only(task.token),
                )

        # A directly uploaded video file is already on disk inside work_subdir;
        # there is nothing to leech, so skip straight to processing.
        if task.spec.kind == SourceKind.LOCAL_FILE:
            local = Path(task.spec.value)
            if not local.is_file():
                raise LeechError("The uploaded video file is no longer available.")
            task.downloaded_files = [local]
            if reporter:
                await reporter.update(
                    pg.render_status("Using Uploaded File", [local.name], emoji="📂"),
                    reply_markup=kb.cancel_only(task.token),
                    force=True,
                )
            return

        if reporter:
            await reporter.update(
                pg.render_status("Starting Download", [task.spec.label], emoji="📥"),
                reply_markup=kb.cancel_only(task.token),
                force=True,
            )

        gid = await self._engine.start(task.spec, download_dir=work_subdir)
        task.gid = gid
        files = await self._engine.wait(gid, progress_cb=progress, cancel_event=task.cancel_event)
        task.downloaded_files = files
        if not files:
            raise LeechError("Download finished but produced no files.")

    async def _do_process(self, task: Task) -> PipelineResult:
        task.state = TaskState.PROCESSING
        reporter = task.reporter
        target = self._pick_video(task.downloaded_files)
        if target is None:
            raise PipelineError("No video file (.mkv/.mp4/...) found in the download.")

        async def progress(stage, done, total):  # type: ignore[no-untyped-def]
            if reporter:
                await reporter.update(
                    pg.render_progress(stage, done=done, total=total)
                    if total
                    else pg.render_status(stage, emoji="⚙️"),
                    reply_markup=kb.cancel_only(task.token),
                )

        if reporter:
            await reporter.update(
                pg.render_status("Processing Media", [target.name], emoji="⚙️"),
                reply_markup=kb.cancel_only(task.token),
                force=True,
            )

        result = await self._pipeline.process(
            target, progress_cb=progress, extra_audios=task.extra_audios
        )
        task.produced_files.append(result.output_path)
        if reporter:
            done_lines = [
                f"Source track: #{result.source_stream_index}",
                f"Signs kept: {result.events_kept} | dialogue dropped: {result.events_dropped}",
                f"English subtitle tracks retained: {result.english_sub_count}",
            ]
            if result.extra_audio_count:
                done_lines.append(f"External audio tracks added: {result.extra_audio_count}")
            await reporter.update(
                pg.render_status("Pipeline Done", done_lines, emoji="✅"),
                force=True,
            )
        return result

    async def _do_upload(self, task: Task, result: PipelineResult) -> None:
        task.state = TaskState.UPLOADING
        reporter = task.reporter
        produced = result.output_path

        async def progress(done, total, speed):  # type: ignore[no-untyped-def]
            if reporter:
                eta = (total - done) / speed if speed > 0 else None
                await reporter.update(
                    pg.render_progress("Uploading", done=done, total=total, speed=speed, eta=eta),
                    reply_markup=kb.cancel_only(task.token),
                )

        if reporter:
            await reporter.update(
                pg.render_status("Uploading", [produced.name], emoji="📤"),
                reply_markup=kb.cancel_only(task.token),
                force=True,
            )

        caption = pg.render_status("Signs & Songs ready", [produced.name], emoji="📦")
        await self._uploader.send_document(
            task.chat_id,
            produced,
            caption=caption,
            progress_cb=progress,
            reply_to=task.trigger_message_id,
        )

        # Also send the extracted subtitle scripts as .txt for confirmation.
        await self._send_subtitle_txts(task, result)

    async def _send_subtitle_txts(self, task: Task, result: PipelineResult) -> None:
        """Upload the extracted signs/songs and full subtitle as ``.txt`` files.

        These are confirmation artifacts; a failure here must never fail the
        task (the main MKV is already delivered).
        """

        stem = result.output_path.stem
        artifacts = [
            (result.signs_sub_path, f"{stem}.signs.txt",
             "Signs & Songs subtitle (extracted)"),
            (result.full_sub_path, f"{stem}.fullsub.txt",
             "Full English subtitle (source)"),
        ]
        for src, name, label in artifacts:
            if src is None or not Path(src).is_file():
                continue
            txt_path = Path(src).with_name(name)
            try:
                shutil.copyfile(src, txt_path)
            except OSError:
                log.warning("Could not stage %s for upload", name, exc_info=True)
                continue
            task.produced_files.append(txt_path)
            try:
                await self._uploader.send_document(
                    task.chat_id,
                    txt_path,
                    caption=pg.render_status(label, [name], emoji="🧾"),
                    reply_to=task.trigger_message_id,
                )
            except Exception:
                log.warning("Failed to upload %s", name, exc_info=True)

    async def _cleanup(self, task: Task) -> None:
        """Purge all on-disk assets for this task (always runs)."""

        if task.gid:
            try:
                await self._aria2.remove(task.gid)
            except Exception:
                pass
        if task.work_subdir and task.work_subdir.exists():
            shutil.rmtree(task.work_subdir, ignore_errors=True)
        if task.audio_dir and task.audio_dir.exists():
            shutil.rmtree(task.audio_dir, ignore_errors=True)
        # Defensive: remove any stray produced/temp files outside the subdir.
        for path in [*task.produced_files]:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _pick_video(files: list[Path]) -> Optional[Path]:
        videos = [f for f in files if f.suffix.lower() in _VIDEO_EXTS and f.exists()]
        if not videos:
            return None
        # Prefer .mkv (only MKV carries ASS+attachments cleanly), else largest.
        mkvs = [f for f in videos if f.suffix.lower() == ".mkv"]
        pool = mkvs or videos
        return max(pool, key=lambda f: f.stat().st_size)
