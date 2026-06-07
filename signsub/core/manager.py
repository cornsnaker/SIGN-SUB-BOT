"""Task orchestration: ties leeching, processing and uploading together.

Each task runs as its own asyncio task, bounded by a global concurrency
semaphore. Every stage reports progress through the task's
:class:`StatusReporter`, and a ``finally`` block guarantees temporary assets are
purged whether the task succeeds, fails or is cancelled.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

from pyrogram import Client

from ..config import Config
from ..leech.aria2_client import Aria2Client
from ..leech.daemon import Aria2Daemon
from ..leech.engine import LeechEngine, LeechError
from ..leech.nyaa import NyaaScraper
from ..processing.pipeline import PipelineError, SubtitlePipeline
from ..ui import keyboards as kb
from ..ui import progress as pg
from ..upload.uploader import Uploader
from .sources import SourceSpec
from .status import StatusReporter
from .task import Task, TaskState, new_token

_VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".ts"}


class TaskManager:
    def __init__(self, client: Client, config: Config) -> None:
        self._client = client
        self._cfg = config
        self._tasks: dict[str, Task] = {}
        self._runners: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_tasks)

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
        token = new_token()
        task = Task(
            token=token,
            chat_id=chat_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            spec=spec,
        )
        self._tasks[token] = task
        return task

    def get(self, token: str) -> Optional[Task]:
        return self._tasks.get(token)

    @property
    def scraper(self) -> NyaaScraper:
        return self._scraper

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
        return True

    def launch(self, task: Task) -> None:
        """Schedule ``task`` to run on the event loop."""

        runner = asyncio.create_task(self._execute(task), name=f"task-{task.token}")
        self._runners[task.token] = runner
        runner.add_done_callback(lambda _t, tok=task.token: self._runners.pop(tok, None))

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
                produced = await self._do_process(task)
                await self._do_upload(task, produced)

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

    async def _do_process(self, task: Task) -> Path:
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

        result = await self._pipeline.process(target, progress_cb=progress)
        task.produced_files.append(result.output_path)
        if reporter:
            await reporter.update(
                pg.render_status(
                    "Pipeline Done",
                    [
                        f"Source track: #{result.source_stream_index}",
                        f"Signs kept: {result.events_kept} | dialogue dropped: {result.events_dropped}",
                        f"English subtitle tracks retained: {result.english_sub_count}",
                    ],
                    emoji="✅",
                ),
                force=True,
            )
        return result.output_path

    async def _do_upload(self, task: Task, produced: Path) -> None:
        task.state = TaskState.UPLOADING
        reporter = task.reporter

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

    async def _cleanup(self, task: Task) -> None:
        """Purge all on-disk assets for this task (always runs)."""

        if task.gid:
            try:
                await self._aria2.remove(task.gid)
            except Exception:
                pass
        if task.work_subdir and task.work_subdir.exists():
            shutil.rmtree(task.work_subdir, ignore_errors=True)
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
