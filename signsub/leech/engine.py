"""High-level leeching engine.

Resolves a :class:`SourceSpec` into an aria2 download, then polls to completion
while invoking a progress callback. Handles the magnet -> metadata -> torrent
``followedBy`` hand-off and enforces a write-lock so downstream processing never
touches a file that is still being written.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp

from ..core.sources import SourceKind, SourceSpec
from .aria2_client import Aria2Client
from .nyaa import NyaaScraper

# stage, done, total, speed, eta
ProgressCb = Callable[[str, float, float, Optional[float], Optional[float]], Awaitable[None]]


class LeechError(RuntimeError):
    pass


class LeechEngine:
    def __init__(self, client: Aria2Client, scraper: NyaaScraper) -> None:
        self._aria2 = client
        self._scraper = scraper

    async def start(self, spec: SourceSpec, *, download_dir: Path) -> str:
        """Submit ``spec`` to aria2 and return the gid."""

        download_dir.mkdir(parents=True, exist_ok=True)
        ddir = str(download_dir)

        if spec.kind == SourceKind.MAGNET:
            return await self._aria2.add_uri(spec.value, download_dir=ddir)

        if spec.kind == SourceKind.DIRECT:
            return await self._aria2.add_uri(spec.value, download_dir=ddir)

        if spec.kind == SourceKind.TORRENT_URL:
            data = await self._fetch_bytes(spec.value)
            return await self._aria2.add_torrent(data, download_dir=ddir)

        if spec.kind == SourceKind.TORRENT_FILE:
            data = Path(spec.value).read_bytes()
            return await self._aria2.add_torrent(data, download_dir=ddir)

        if spec.kind == SourceKind.NYAA_VIEW:
            result = await self._scraper.resolve(spec.value)
            if not result or not result.best_source:
                raise LeechError("Could not resolve a download link from the Nyaa listing.")
            source = result.best_source
            if source.startswith("magnet:"):
                return await self._aria2.add_uri(source, download_dir=ddir)
            data = await self._fetch_bytes(source)
            return await self._aria2.add_torrent(data, download_dir=ddir)

        raise LeechError(f"Unsupported source kind for direct leeching: {spec.kind}")

    async def wait(
        self,
        gid: str,
        *,
        progress_cb: Optional[ProgressCb] = None,
        cancel_event: Optional[asyncio.Event] = None,
        poll_interval: float = 2.0,
    ) -> list[Path]:
        """Poll a download to completion and return its on-disk files.

        Follows the magnet metadata ``followedBy`` hand-off and verifies the
        terminal completion state before returning (the write-lock).
        """

        current = gid
        while True:
            if cancel_event and cancel_event.is_set():
                await self._aria2.remove(current)
                raise asyncio.CancelledError()

            status = await self._aria2.tell_status(current)

            # Magnet metadata download spawns the real torrent download.
            if status.followed_by:
                current = status.followed_by[0]
                await asyncio.sleep(poll_interval)
                continue

            if status.is_failed:
                raise LeechError(status.error_message or f"download {current} failed")

            if progress_cb:
                await progress_cb(
                    "Downloading",
                    float(status.completed_length),
                    float(status.total_length),
                    float(status.download_speed),
                    status.eta_seconds,
                )

            if status.is_done:
                return await self._verify_complete(current)

            await asyncio.sleep(poll_interval)

    async def _verify_complete(self, gid: str) -> list[Path]:
        """Write-lock: confirm the download is complete and sizes are stable."""

        status = await self._aria2.tell_status(gid)
        if not status.is_done:
            raise LeechError("download reported complete but verification failed")
        files = [f for f in status.files if f.exists() and f.is_file()]
        # Stability check: file size must not change across a short interval.
        sizes = {f: f.stat().st_size for f in files}
        await asyncio.sleep(0.75)
        for f in files:
            if not f.exists() or f.stat().st_size != sizes.get(f):
                raise LeechError(f"file still being written: {f.name}")
        return files

    @staticmethod
    async def _fetch_bytes(url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SignSubBot/1.0)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()
