"""Asynchronous aria2c client over the JSON-RPC interface.

The client speaks JSON-RPC 2.0 to aria2's HTTP endpoint using ``aiohttp`` so it
integrates cleanly with the asyncio event loop without blocking workers. It
covers the subset of methods the bot needs: adding URIs/torrents/magnets,
polling status, and removing/cleaning downloads.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp


class Aria2Error(RuntimeError):
    """Raised when aria2 returns a JSON-RPC error or is unreachable."""


@dataclass(slots=True)
class DownloadStatus:
    """Normalized snapshot of an aria2 download (a "gid")."""

    gid: str
    status: str  # active | waiting | paused | error | complete | removed
    total_length: int
    completed_length: int
    download_speed: int
    error_message: str
    files: list[Path]
    following: Optional[str]
    followed_by: list[str]
    info_hash: Optional[str]

    @property
    def is_done(self) -> bool:
        return self.status == "complete"

    @property
    def is_failed(self) -> bool:
        return self.status in {"error", "removed"}

    @property
    def eta_seconds(self) -> Optional[float]:
        remaining = self.total_length - self.completed_length
        if self.download_speed <= 0:
            return None
        return remaining / self.download_speed

    @property
    def percent(self) -> float:
        if self.total_length <= 0:
            return 0.0
        return (self.completed_length / self.total_length) * 100.0


class Aria2Client:
    """Thin async wrapper around aria2's JSON-RPC API."""

    def __init__(self, rpc_url: str, secret: str = "", *, timeout: float = 30.0) -> None:
        self._rpc_url = rpc_url
        self._token = f"token:{secret}" if secret else None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "Aria2Client":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _params(self, *params: Any) -> list[Any]:
        return [self._token, *params] if self._token else list(params)

    async def _call(self, method: str, *params: Any) -> Any:
        if self._session is None or self._session.closed:
            await self.connect()
        assert self._session is not None
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": self._params(*params),
        }
        try:
            async with self._session.post(self._rpc_url, json=payload) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:  # network / connection issues
            raise Aria2Error(f"aria2 RPC unreachable at {self._rpc_url}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise Aria2Error("aria2 RPC request timed out") from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise Aria2Error(f"aria2 error {err.get('code')}: {err.get('message')}")
        return data.get("result") if isinstance(data, dict) else data

    # -- public API ---------------------------------------------------------

    async def get_version(self) -> str:
        result = await self._call("aria2.getVersion")
        return str(result.get("version", "unknown")) if isinstance(result, dict) else "unknown"

    async def add_uri(self, uri: str, *, download_dir: str, options: Optional[dict[str, Any]] = None) -> str:
        """Add a direct HTTP(S)/FTP URL or a magnet link. Returns the gid."""

        opts: dict[str, Any] = {"dir": download_dir}
        if options:
            opts.update(options)
        gid = await self._call("aria2.addUri", [uri], opts)
        return str(gid)

    async def add_torrent(
        self, torrent_bytes: bytes, *, download_dir: str, options: Optional[dict[str, Any]] = None
    ) -> str:
        """Add a ``.torrent`` payload (raw bytes). Returns the gid."""

        opts: dict[str, Any] = {"dir": download_dir}
        if options:
            opts.update(options)
        b64 = base64.b64encode(torrent_bytes).decode("ascii")
        gid = await self._call("aria2.addTorrent", b64, [], opts)
        return str(gid)

    async def tell_status(self, gid: str) -> DownloadStatus:
        keys = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "errorMessage",
            "files",
            "following",
            "followedBy",
            "infoHash",
        ]
        raw = await self._call("aria2.tellStatus", gid, keys)
        return self._to_status(raw)

    async def remove(self, gid: str) -> None:
        try:
            await self._call("aria2.remove", gid)
        except Aria2Error:
            # Already finished/removed downloads cannot be force-stopped; ignore.
            pass
        await self._call_safe("aria2.removeDownloadResult", gid)

    async def _call_safe(self, method: str, *params: Any) -> None:
        try:
            await self._call(method, *params)
        except Aria2Error:
            pass

    @staticmethod
    def _to_status(raw: dict[str, Any]) -> DownloadStatus:
        files: list[Path] = []
        for entry in raw.get("files", []) or []:
            path = entry.get("path")
            if path:
                files.append(Path(path))
        return DownloadStatus(
            gid=str(raw.get("gid", "")),
            status=str(raw.get("status", "")),
            total_length=int(raw.get("totalLength", 0) or 0),
            completed_length=int(raw.get("completedLength", 0) or 0),
            download_speed=int(raw.get("downloadSpeed", 0) or 0),
            error_message=str(raw.get("errorMessage", "") or ""),
            files=files,
            following=raw.get("following"),
            followed_by=list(raw.get("followedBy", []) or []),
            info_hash=raw.get("infoHash"),
        )
