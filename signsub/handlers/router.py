"""Wires Pyrogram update handlers to the :class:`TaskManager`.

Responsibilities:
* ``/start`` & ``/help`` (role-aware) command cards.
* Admin commands: ``/stats``, ``/tasks``, ``/users`` (owner can add/remove users).
* ``/addaudio`` (alias ``/muxaudio``) to attach external audio to a pending task.
* Inbound source links / search queries -> inline selection menu.
* Uploaded ``.torrent`` documents -> inline selection menu.
* Callback button routing (start / filter / cancel / nyaa-pick / add-audio).
"""

from __future__ import annotations

import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from ..config import Config
from ..core import sources
from ..core.manager import TaskManager
from ..core.status import StatusReporter
from ..core.task import (
    AUDIO_AWAIT_FILE,
    AUDIO_AWAIT_LANG,
    AUDIO_AWAIT_NAME,
    ExtraAudio,
    Task,
    TaskState,
)
from ..leech import torrent_meta
from ..ui import keyboards as kb
from ..ui import fmt as md
from ..ui import progress as pg

_START_CARD = pg.render_status(
    "Sign & Songs Leech Bot",
    [
        "Send me a direct link, magnet, .torrent file or a Nyaa.si link.",
        "Or type any text to search Nyaa.si.",
        "I will leech it, build a clean Signs & Songs track and send it back.",
    ],
    emoji="🎬",
)

_ADD_AUDIO_PROMPT = pg.render_status(
    "Add External Audio",
    [
        "Send me the audio now — upload a file or paste a direct link.",
        "Formats: AAC, MP3, M4A, FLAC, Opus, OGG, WAV, AC3, E-AC3, DTS, ALAC, …",
        "I'll then ask for its language and track name.",
    ],
    emoji="🎵",
)


def _fmt_age(seconds: float) -> str:
    """Compact human duration, e.g. ``3s``, ``5m 02s``, ``2h 09m``, ``1d 03h``."""

    secs = int(max(0, seconds))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {s:02d}s"
    hours, m = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {m:02d}m"
    days, h = divmod(hours, 24)
    return f"{days}d {h:02d}h"


def _monotonic_age(created_at: float) -> float:
    """Seconds elapsed since a ``time.monotonic()`` timestamp."""

    return max(0.0, time.monotonic() - created_at)


def register(client: Client, manager: TaskManager, config: Config) -> None:
    """Attach all handlers to ``client``."""

    def _authorized(user_id: int | None) -> bool:
        return config.is_user_allowed(user_id)

    def _role(user_id: int | None) -> str:
        if config.is_owner(user_id):
            return "owner"
        if config.is_admin(user_id):
            return "admin"
        return "user"

    async def _deny(message: Message, *, admin: bool = False) -> None:
        msg = (
            "This command is for admins only."
            if admin
            else "You are not authorized to use this bot."
        )
        await message.reply_text(pg.render_error(msg), parse_mode=ParseMode.HTML)

    def _help_card(user_id: int | None) -> str:
        role = _role(user_id)
        lines = [
            "Send a direct link, magnet, .torrent file or a Nyaa.si link.",
            "Or type any text to search Nyaa.si.",
            "I leech it, build a clean Signs & Songs track and send it back.",
            "",
            "👤 Commands",
            "• /start, /help — this message",
            "• /addaudio — add an external audio track to the pending file",
        ]
        if role in ("admin", "owner"):
            lines += [
                "",
                "🛡️ Admin",
                "• /stats — bot uptime and task counters",
                "• /tasks — live list of active tasks",
                "• /users — list known users" + (" (add/remove)" if role == "owner" else ""),
            ]
        if role == "owner":
            lines += ["• /users add <id> | /users remove <id> — manage access"]
        lines += ["", f"Your role: {role}."]
        return pg.render_status("Sign & Songs Leech Bot", lines, emoji="🎬")

    @client.on_message(filters.command(["start", "help"]) & filters.private)
    async def _on_start(_: Client, message: Message) -> None:
        uid = message.from_user.id if message.from_user else None
        if not _authorized(uid):
            await _deny(message)
            return
        await message.reply_text(_help_card(uid), parse_mode=ParseMode.HTML)

    @client.on_message(filters.command("stats") & filters.private)
    async def _on_stats(_: Client, message: Message) -> None:
        uid = message.from_user.id if message.from_user else None
        if not config.is_admin(uid):
            await _deny(message, admin=True)
            return
        s = manager.stats()
        per_state = s["per_state"] or {}  # type: ignore[assignment]
        state_line = ", ".join(f"{k}:{v}" for k, v in per_state.items()) or "none"
        await message.reply_text(
            pg.render_status(
                "Bot Stats",
                [
                    f"⏱️ Uptime: {_fmt_age(float(s['uptime_seconds']))}",
                    f"📈 Tasks created: {s['total_created']}",
                    f"🎉 Completed: {s['completed']}",
                    f"❌ Failed: {s['failed']}",
                    f"🛑 Cancelled: {s['cancelled']}",
                    f"▶️ Active now: {s['active']} / {s['max_concurrent']} slots",
                    f"📦 Tracked: {s['tracked']} ({state_line})",
                    f"👥 Unique users: {s['unique_users']}",
                ],
                emoji="📊",
            ),
            parse_mode=ParseMode.HTML,
        )

    @client.on_message(filters.command("tasks") & filters.private)
    async def _on_tasks(_: Client, message: Message) -> None:
        uid = message.from_user.id if message.from_user else None
        if not config.is_admin(uid):
            await _deny(message, admin=True)
            return
        tasks = manager.list_tasks()
        if not tasks:
            await message.reply_text(
                pg.render_status("Active Tasks", ["No tasks right now."], emoji="📭"),
                parse_mode=ParseMode.HTML,
            )
            return
        lines: list[str] = []
        for t in tasks[:20]:
            running = "▶️" if t.token in {x.token for x in manager.active_tasks()} else "⏸️"
            lines.append(
                f"{running} {t.state.value} · {t.spec.label[:34]} "
                f"· u{t.user_id} · {_fmt_age(_monotonic_age(t.created_at))}"
            )
        if len(tasks) > 20:
            lines.append(f"… and {len(tasks) - 20} more")
        await message.reply_text(
            pg.render_status("Active Tasks", lines, emoji="🗂️"),
            parse_mode=ParseMode.HTML,
        )

    @client.on_message(filters.command("users") & filters.private)
    async def _on_users(_: Client, message: Message) -> None:
        uid = message.from_user.id if message.from_user else None
        if not config.is_admin(uid):
            await _deny(message, admin=True)
            return
        parts = (message.text or "").split()
        # Owner-only mutations: /users add <id> | /users remove <id>
        if len(parts) >= 3 and parts[1].lower() in {"add", "remove", "del"}:
            if not config.is_owner(uid):
                await _deny(message, admin=True)
                return
            if not parts[2].lstrip("-").isdigit():
                await message.reply_text(
                    pg.render_error("Usage", "/users add <id>  |  /users remove <id>"),
                    parse_mode=ParseMode.HTML,
                )
                return
            target = int(parts[2])
            if parts[1].lower() == "add":
                config.extra_allowed_ids.add(target)
                note = f"Authorized user {target}."
            else:
                config.extra_allowed_ids.discard(target)
                note = f"Revoked user {target}."
            await message.reply_text(
                pg.render_status("Users Updated", [note], emoji="✅"),
                parse_mode=ParseMode.HTML,
            )
            return
        # Default: list known users and their roles.
        seen = manager.seen_user_ids()
        lines: list[str] = []
        if config.owner_id:
            lines.append(f"👑 Owner: {config.owner_id}")
        if config.admin_ids:
            lines.append("🛡️ Admins: " + ", ".join(str(i) for i in sorted(config.admin_ids)))
        allow = sorted(set(config.allowed_user_ids) | config.extra_allowed_ids)
        lines.append("✅ Allow-list: " + (", ".join(str(i) for i in allow) if allow else "open to all"))
        lines.append("")
        lines.append(f"👥 Seen users ({len(seen)}): " + (", ".join(str(i) for i in seen) or "none"))
        await message.reply_text(
            pg.render_status("Users", lines, emoji="👥"),
            parse_mode=ParseMode.HTML,
        )

    @client.on_message(filters.command(["addaudio", "muxaudio"]) & filters.private)
    async def _on_addaudio(_: Client, message: Message) -> None:
        uid = message.from_user.id if message.from_user else None
        if not _authorized(uid):
            await _deny(message)
            return
        pending = [
            t for t in manager.list_tasks()
            if t.chat_id == message.chat.id and t.state == TaskState.PENDING
        ]
        if not pending:
            await message.reply_text(
                pg.render_status(
                    "Add External Audio",
                    [
                        "Send a source first (link / magnet / .torrent / Nyaa),",
                        "then use /addaudio or the 🎵 Add Audio button before starting.",
                    ],
                    emoji="🎵",
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        task = pending[0]
        task.audio_stage = AUDIO_AWAIT_FILE
        task.audio_draft = None
        await message.reply_text(_ADD_AUDIO_PROMPT, parse_mode=ParseMode.HTML,
                                 reply_markup=kb.cancel_only(task.token))

    @client.on_message((filters.audio | filters.voice) & filters.private)
    async def _on_audio(_: Client, message: Message) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            return
        task = manager.awaiting_audio_task(message.chat.id)
        if task is None:
            return
        media = message.audio or message.voice
        fname = getattr(media, "file_name", None) or f"audio_{message.id}"
        await _capture_telegram_audio(message, task, fname)

    @client.on_message(filters.document & filters.private)
    async def _on_document(_: Client, message: Message) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            return
        doc = message.document
        fname = (doc.file_name if doc else "") or ""

        # If we're collecting an external audio track, treat audio docs as audio.
        awaiting = manager.awaiting_audio_task(message.chat.id)
        if awaiting is not None:
            if Path(fname).suffix.lower() in sources.AUDIO_EXTS:
                await _capture_telegram_audio(message, awaiting, fname or f"audio_{message.id}")
            else:
                await message.reply_text(
                    pg.render_status(
                        "Not an audio file",
                        ["Send an audio file/link, or tap Cancel on the menu."],
                        emoji="⚠️",
                    ),
                    parse_mode=ParseMode.HTML,
                )
            return

        if not doc or not fname.lower().endswith(".torrent"):
            await message.reply_text(
                pg.render_status("Unsupported file", ["Please send a .torrent file or a link."],
                                 emoji="⚠️"),
                parse_mode=ParseMode.HTML,
            )
            return
        dest_dir = config.download_dir / "torrents"
        dest_dir.mkdir(parents=True, exist_ok=True)
        local_path = await message.download(file_name=str(dest_dir / doc.file_name))
        # Prefer the real media name stored inside the .torrent (info.name).
        inner = torrent_meta.torrent_name(local_path)
        label = inner or doc.file_name
        spec = sources.torrent_file_spec(str(local_path), label)
        task = manager.create_task(
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            trigger_message_id=message.id,
            spec=spec,
        )
        await message.reply_text(
            pg.render_status("Source Received", [f"📦 {label}", "Choose an action:"],
                             emoji="🧲"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.source_menu(task.token),
        )

    @client.on_message(
        filters.text
        & filters.private
        & ~filters.command(["start", "help", "stats", "tasks", "users", "addaudio", "muxaudio"])
    )
    async def _on_text(_: Client, message: Message) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            return
        text = (message.text or "").strip()

        # While collecting an external audio track, interpret a link as audio.
        awaiting = manager.awaiting_audio_task(message.chat.id)
        if awaiting is not None:
            if sources.is_audio_url(text):
                await _capture_remote_audio(message, awaiting, text)
            else:
                await message.reply_text(
                    pg.render_status(
                        "Send the audio",
                        ["Upload an audio file or paste a direct audio link.",
                         "Or tap Cancel on the menu to abort."],
                        emoji="🎵",
                    ),
                    parse_mode=ParseMode.HTML,
                )
            return

        spec = sources.classify(text)
        if spec is None:
            return

        if spec.kind == sources.SourceKind.NYAA_SEARCH:
            await _handle_search(message, spec.value)
            return

        task = manager.create_task(
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            trigger_message_id=message.id,
            spec=spec,
        )
        await message.reply_text(
            pg.render_status("Source Received", [f"🔗 {spec.label}", "Choose an action:"],
                             emoji="🧲"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.source_menu(task.token),
        )

    async def _handle_search(message: Message, query: str) -> None:
        status = await message.reply_text(
            pg.render_status("Searching Nyaa.si", [query], emoji="🔎"),
            parse_mode=ParseMode.HTML,
        )
        try:
            results = await manager.scraper.search(query, limit=10)
        except Exception as exc:  # noqa: BLE001
            await status.edit_text(pg.render_error("Nyaa search failed", repr(exc)),
                                   parse_mode=ParseMode.HTML)
            return
        if not results:
            await status.edit_text(
                pg.render_status("No Results", ["Nothing found on Nyaa.si for that query."],
                                 emoji="🤷"),
                parse_mode=ParseMode.HTML,
            )
            return

        task = manager.create_task(
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            trigger_message_id=message.id,
            spec=sources.SourceSpec(sources.SourceKind.NYAA_SEARCH, query, query[:48]),
        )
        task.nyaa_choices = results
        lines = [md.bold(f"🔎 {md.escape('Nyaa.si results')}")]
        for idx, r in enumerate(results):
            lines.append(
                f"{md.bold(str(idx + 1) + '.')} {md.escape(r.title[:60])} "
                f"({md.escape(r.size)}, S:{md.escape(str(r.seeders))})"
            )
        await status.edit_text(
            md.quote_block(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.nyaa_results(task.token, len(results)),
        )

    @client.on_callback_query()
    async def _on_callback(_: Client, query: CallbackQuery) -> None:
        if not _authorized(query.from_user.id if query.from_user else None):
            await query.answer("Not authorized.", show_alert=True)
            return
        action, args = kb.parse_callback(query.data or "")
        token = args[0] if args else ""
        task = manager.get(token)

        if action == kb.ACT_CANCEL:
            manager.cancel(token)
            await query.answer("Cancelling task...")
            return

        if task is None:
            await query.answer("This task has expired. Send the link again.", show_alert=True)
            return

        if action == kb.ACT_NYAA_PICK:
            await _pick_nyaa(query, task, args)
            return

        if action == kb.ACT_FILTER:
            await query.answer()
            await query.message.edit_text(
                pg.render_status(
                    "Stream Filter Policy",
                    [
                        "On processing I will keep:",
                        "• video + audio (all)",
                        "• English-tagged subtitle tracks",
                        "• a new Signs & Songs track (default+song styles stripped)",
                        "• fonts/attachments",
                        "Non-English subtitles are dropped.",
                    ],
                    emoji="⚙️",
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb.source_menu(task.token, audio_count=len(task.extra_audios)),
            )
            return

        if action == kb.ACT_ADD_AUDIO:
            await _begin_add_audio(query, task)
            return

        if action == kb.ACT_AUDIO_LANG:
            await _audio_pick_language(query, task, args)
            return

        if action == kb.ACT_AUDIO_NAME:
            await _audio_pick_name(query, task, args)
            return

        if action == kb.ACT_START:
            reporter = StatusReporter(client, query.message,
                                      min_interval=config.progress_update_interval)
            manager.attach_reporter(task, reporter)
            if manager.launch(task):
                await query.answer("Starting...")
            else:
                await query.answer("Task already started.", show_alert=True)
            return

        await query.answer()

    # -- external audio flow -----------------------------------------------

    async def _begin_add_audio(query: CallbackQuery, task: Task) -> None:
        if task.state != TaskState.PENDING:
            await query.answer("Audio can only be added before the download starts.",
                               show_alert=True)
            return
        task.audio_stage = AUDIO_AWAIT_FILE
        task.audio_draft = None
        await query.answer()
        await query.message.edit_text(
            pg.render_status(
                "Add External Audio",
                [
                    "Send me the audio now — upload a file or paste a direct link.",
                    "Formats: AAC, MP3, M4A, FLAC, Opus, OGG, WAV, AC3, E-AC3, DTS, ALAC, …",
                    "I'll then ask for its language and track name.",
                ],
                emoji="🎵",
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.cancel_only(task.token),
        )

    async def _capture_telegram_audio(message: Message, task: Task, file_name: str) -> None:
        status = await message.reply_text(
            pg.render_status("Receiving audio", [file_name], emoji="⬇️"),
            parse_mode=ParseMode.HTML,
        )
        try:
            local = await message.download(file_name=str(manager.audio_dest(task, file_name)))
        except Exception as exc:  # noqa: BLE001
            await status.edit_text(pg.render_error("Could not save that audio", repr(exc)),
                                   parse_mode=ParseMode.HTML)
            return
        await _audio_received(status, task, Path(local), file_name)

    async def _capture_remote_audio(message: Message, task: Task, url: str) -> None:
        status = await message.reply_text(
            pg.render_status("Fetching audio", [url[:80]], emoji="⬇️"),
            parse_mode=ParseMode.HTML,
        )
        try:
            local = await manager.stage_remote_audio(task, url)
        except Exception as exc:  # noqa: BLE001
            await status.edit_text(pg.render_error("Audio download failed", repr(exc)),
                                   parse_mode=ParseMode.HTML)
            return
        await _audio_received(status, task, local, local.name)

    async def _audio_received(status: Message, task: Task, path: Path, label: str) -> None:
        task.audio_draft = ExtraAudio(path=path, label=label)
        task.audio_stage = AUDIO_AWAIT_LANG
        await status.edit_text(
            pg.render_status(
                "Select Audio Language",
                [f"🎵 {label}", "Choose the language of this audio track:"],
                emoji="🌐",
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.audio_language_menu(task.token),
        )

    async def _audio_pick_language(query: CallbackQuery, task: Task, args: list[str]) -> None:
        if task.audio_stage != AUDIO_AWAIT_LANG or task.audio_draft is None or len(args) < 2:
            await query.answer("Send an audio file first.", show_alert=True)
            return
        code = args[1]
        task.audio_draft.language = code
        task.audio_stage = AUDIO_AWAIT_NAME
        label = dict(kb.AUDIO_LANGUAGES).get(code, code)
        await query.answer(f"Language: {label}")
        await query.message.edit_text(
            pg.render_status(
                "Name the Audio Track",
                [f"🎵 {task.audio_draft.label}", f"Language: {label}",
                 "Pick a track title:"],
                emoji="🏷️",
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.audio_name_menu(task.token),
        )

    async def _audio_pick_name(query: CallbackQuery, task: Task, args: list[str]) -> None:
        if task.audio_stage != AUDIO_AWAIT_NAME or task.audio_draft is None or len(args) < 2:
            await query.answer("Send an audio file first.", show_alert=True)
            return
        choice = args[1]
        if choice == kb.AUDIO_NAME_USE_FILENAME:
            title = Path(task.audio_draft.label).stem or task.audio_draft.label
        elif choice.isdigit() and int(choice) < len(kb.AUDIO_NAME_PRESETS):
            title = kb.AUDIO_NAME_PRESETS[int(choice)]
        else:
            await query.answer("Invalid choice.", show_alert=True)
            return

        draft = task.audio_draft
        draft.title = title
        task.extra_audios.append(draft)
        task.audio_draft = None
        task.audio_stage = None
        await query.answer("Audio queued.")

        lang_label = dict(kb.AUDIO_LANGUAGES).get(draft.language, draft.language)
        lines = [f"✅ Queued: {draft.label}", f"Language: {lang_label} | Title: {title}", ""]
        lines.append(f"Audio tracks to add: {len(task.extra_audios)}")
        lines.append("Add another audio, or Start Download.")
        await query.message.edit_text(
            pg.render_status("Audio Added", lines, emoji="🎶"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.source_menu(task.token, audio_count=len(task.extra_audios)),
        )

    async def _pick_nyaa(query: CallbackQuery, task, args: list[str]) -> None:
        if len(args) < 2 or not args[1].isdigit():
            await query.answer("Invalid selection.", show_alert=True)
            return
        idx = int(args[1])
        if idx >= len(task.nyaa_choices):
            await query.answer("Selection out of range.", show_alert=True)
            return
        chosen = task.nyaa_choices[idx]
        source = chosen.best_source
        if not source:
            await query.answer("That entry has no downloadable link.", show_alert=True)
            return
        if source.startswith("magnet:"):
            task.spec = sources.SourceSpec(sources.SourceKind.MAGNET, source, chosen.title[:48])
        else:
            task.spec = sources.SourceSpec(sources.SourceKind.TORRENT_URL, source, chosen.title[:48])
        await query.answer("Selected.")
        await query.message.edit_text(
            pg.render_status("Source Selected", [f"🔗 {chosen.title[:60]}", "Choose an action:"],
                             emoji="🧲"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.source_menu(task.token),
        )
