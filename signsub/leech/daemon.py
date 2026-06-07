"""Lifecycle management for a local ``aria2c`` RPC daemon.

If the bot is configured to talk to ``localhost`` and no daemon is already
listening, we spawn one with sane defaults (multi-connection acceleration,
DHT/peer-exchange for torrents). When the bot owns the process it also tears it
down on shutdown.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Optional

from ..config import Config
from .aria2_client import Aria2Client, Aria2Error


class Aria2Daemon:
    """Spawns and supervises an ``aria2c`` process when needed."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._owned = False

    async def ensure_running(self) -> None:
        """Make sure an aria2 RPC endpoint is reachable, starting one if not."""

        if await self._is_reachable():
            return
        binary = shutil.which("aria2c")
        if not binary:
            raise Aria2Error(
                "aria2c binary not found on PATH. Install it (e.g. `apt-get install aria2`)."
            )
        self._config.ensure_dirs()
        args = [
            binary,
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self._config.aria2_port}",
            "--rpc-allow-origin-all=true",
            f"--dir={self._config.download_dir}",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--max-concurrent-downloads=4",
            "--continue=true",
            "--seed-time=0",
            "--bt-enable-lpd=true",
            "--enable-dht=true",
            "--bt-max-peers=0",
            "--summary-interval=0",
            "--console-log-level=warn",
        ]
        if self._config.aria2_secret:
            args.append(f"--rpc-secret={self._config.aria2_secret}")
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._owned = True
        # Wait for the RPC port to come up.
        for _ in range(20):
            await asyncio.sleep(0.25)
            if await self._is_reachable():
                return
        raise Aria2Error("Spawned aria2c but the RPC endpoint never became reachable.")

    async def _is_reachable(self) -> bool:
        client = Aria2Client(self._config.aria2_rpc_url, self._config.aria2_secret, timeout=5)
        try:
            await client.get_version()
            return True
        except Aria2Error:
            return False
        finally:
            await client.close()

    async def shutdown(self) -> None:
        if self._proc and self._owned and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
        self._owned = False
