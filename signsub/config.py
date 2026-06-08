"""Centralized runtime configuration for the Sign-Sub bot.

All values are sourced from environment variables so the bot can be deployed
without code changes. A ``.env`` file (see ``.env.example``) is loaded
automatically when present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _load_dotenv() -> None:
    """Best-effort loader for a local ``.env`` file.

    We avoid a hard dependency on ``python-dotenv`` by parsing the file
    ourselves. Existing environment variables always take precedence.
    """

    env_path = Path(os.getenv("SIGNSUB_ENV_FILE", ".env"))
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_ids(raw: str) -> frozenset[int]:
    """Parse a comma/space separated list of integer user IDs."""

    return frozenset(
        int(tok)
        for tok in raw.replace(",", " ").split()
        if tok.strip().lstrip("-").isdigit()
    )


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Config:
    """Immutable view of the bot configuration."""

    api_id: int
    api_hash: str
    bot_token: str

    work_dir: Path
    download_dir: Path

    aria2_host: str
    aria2_port: int
    aria2_secret: str
    aria2_rpc_url: str

    ffmpeg_bin: str
    ffprobe_bin: str

    progress_update_interval: float
    upload_chunk_workers: int
    max_concurrent_tasks: int

    owner_id: int = 0
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    # Users authorized at runtime via /users add (not persisted across restarts).
    extra_allowed_ids: set[int] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()

        work_dir = Path(os.getenv("WORK_DIR", "./downloads")).expanduser().resolve()
        download_dir = work_dir / "incoming"

        aria2_host = os.getenv("ARIA2_HOST", "http://localhost")
        aria2_port = _get_int("ARIA2_PORT", 6800)
        # Normalize host into a clean scheme://host form.
        if not aria2_host.startswith(("http://", "https://")):
            aria2_host = f"http://{aria2_host}"
        rpc_url = f"{aria2_host.rstrip('/')}:{aria2_port}/jsonrpc"

        allowed_raw = os.getenv("ALLOWED_USER_IDS") or os.getenv("ALLOWED_USERS") or ""
        allowed_ids = _parse_ids(allowed_raw)
        admin_ids = _parse_ids(os.getenv("ADMINS") or os.getenv("ADMIN_IDS") or "")
        owner_id = _get_int("OWNER_ID", 0)

        return cls(
            api_id=_get_int("TELEGRAM_API_ID", 0),
            api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            bot_token=os.getenv("BOT_TOKEN", ""),
            work_dir=work_dir,
            download_dir=download_dir,
            aria2_host=aria2_host,
            aria2_port=aria2_port,
            aria2_secret=os.getenv("ARIA2_SECRET", ""),
            aria2_rpc_url=rpc_url,
            ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg"),
            ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe"),
            progress_update_interval=float(os.getenv("PROGRESS_INTERVAL", "5")),
            upload_chunk_workers=_get_int("UPLOAD_WORKERS", 4),
            max_concurrent_tasks=_get_int("MAX_CONCURRENT_TASKS", 3),
            owner_id=owner_id,
            admin_ids=admin_ids,
            allowed_user_ids=allowed_ids,
        )

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration problems."""

        problems: list[str] = []
        if self.api_id <= 0:
            problems.append("TELEGRAM_API_ID is missing or invalid.")
        if not self.api_hash:
            problems.append("TELEGRAM_API_HASH is missing.")
        if not self.bot_token:
            problems.append("BOT_TOKEN is missing.")
        return problems

    def is_owner(self, user_id: Optional[int]) -> bool:
        return user_id is not None and self.owner_id != 0 and user_id == self.owner_id

    def is_admin(self, user_id: Optional[int]) -> bool:
        """Owners are implicitly admins."""

        return self.is_owner(user_id) or (user_id is not None and user_id in self.admin_ids)

    def is_user_allowed(self, user_id: Optional[int]) -> bool:
        # Owner/admins always pass. When no allow-list is configured the bot is
        # open to everyone; otherwise the user must be on a list.
        if self.is_admin(user_id):
            return True
        if not self.allowed_user_ids and not self.extra_allowed_ids:
            return True
        if user_id is None:
            return False
        return user_id in self.allowed_user_ids or user_id in self.extra_allowed_ids

    def ensure_dirs(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
