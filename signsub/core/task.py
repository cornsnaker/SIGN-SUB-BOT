"""In-memory representation of a user task and its lifecycle state."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .sources import SourceSpec
from .status import StatusReporter


class TaskState(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


def new_token() -> str:
    """Short, URL-safe token that fits Telegram's 64-byte callback budget."""

    return secrets.token_urlsafe(6)


# Stage markers for the interactive "add external audio" flow.
AUDIO_AWAIT_FILE = "await_file"
AUDIO_AWAIT_LANG = "await_lang"
AUDIO_AWAIT_NAME = "await_name"


@dataclass(slots=True)
class ExtraAudio:
    """An external audio track to mux into the final file."""

    path: Path
    label: str                      # display name (original filename / link)
    language: str = "und"           # ISO 639-2 code, e.g. ``eng``
    title: str = ""                 # human title set on the muxed track


@dataclass(slots=True)
class Task:
    token: str
    chat_id: int
    user_id: int
    trigger_message_id: int
    spec: SourceSpec

    state: TaskState = TaskState.PENDING
    created_at: float = field(default_factory=time.monotonic)

    reporter: Optional[StatusReporter] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    gid: Optional[str] = None
    work_subdir: Optional[Path] = None
    downloaded_files: list[Path] = field(default_factory=list)
    produced_files: list[Path] = field(default_factory=list)

    # Populated when a Nyaa search yields multiple choices.
    nyaa_choices: list = field(default_factory=list)

    # External audio tracks the user opted to mux in, plus the interactive
    # collection state (None when not currently adding an audio track).
    extra_audios: list[ExtraAudio] = field(default_factory=list)
    audio_stage: Optional[str] = None
    audio_draft: Optional[ExtraAudio] = None
    audio_dir: Optional[Path] = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()
