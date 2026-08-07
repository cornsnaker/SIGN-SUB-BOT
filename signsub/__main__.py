"""Entry point: ``python -m signsub``.

Boots the Pyrogram client, ensures the aria2 daemon is up, registers handlers
and runs until interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from pyrogram import Client

from .config import Config
from .core import logbuffer
from .core.manager import TaskManager
from .handlers import router

log = logging.getLogger("signsub")

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def _configure_logging(config: Config) -> None:
    """Console + rotating file + in-memory ring buffer (for ``/logs``)."""

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(console)

    try:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.log_file,
            maxBytes=max(config.log_max_bytes, 10_000),
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"WARNING: cannot open log file {config.log_file}: {exc}", file=sys.stderr)

    logbuffer.buffer.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(logbuffer.buffer)

    logging.getLogger("pyrogram").setLevel(logging.WARNING)


async def _amain() -> int:
    config = Config.from_env()
    _configure_logging(config)
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
