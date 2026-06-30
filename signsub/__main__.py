"""Entry point: ``python -m signsub``.

Boots the Pyrogram client, ensures the aria2 daemon is up, registers handlers
and runs until interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pyrogram import Client

from .config import Config
from .core import logbuffer
from .core.manager import TaskManager
from .handlers import router

log = logging.getLogger("signsub")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    # Retain recent log lines in memory so admins can tail them via /logs.
    logbuffer.install()


async def _amain() -> int:
    _configure_logging()
    config = Config.from_env()
    problems = config.validate()
    if problems:
        for problem in problems:
            log.error("Config error: %s", problem)
        log.error("Populate a .env file (see .env.example) and try again.")
        return 2
    config.ensure_dirs()

    client = Client(
        name="signsub-bot",
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        workdir=str(config.work_dir),
        parse_mode=None,  # set per-message explicitly
    )

    manager = TaskManager(client, config)
    router.register(client, manager, config)

    log.info("Starting aria2 daemon / RPC connection...")
    await manager.startup()

    log.info("Starting Telegram client...")
    await client.start()
    me = await client.get_me()
    log.info("Bot online as @%s", me.username)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()  # run forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("Shutting down...")
        await manager.shutdown()
        await client.stop()
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
