"""Wires Pyrogram update handlers to the :class:`TaskManager`.

Responsibilities:
* ``/start`` & ``/help`` command cards.
* Inbound source links / search queries -> inline selection menu.
* Uploaded ``.torrent`` documents -> inline selection menu.
* Callback button routing (start / filter / cancel / nyaa-pick).
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from ..config import Config
from ..core import sources
from ..core.manager import TaskManager
from ..core.status import StatusReporter
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


def register(client: Client, manager: TaskManager, config: Config) -> None:
    """Attach all handlers to ``client``."""

    def _authorized(user_id: int | None) -> bool:
        return config.is_user_allowed(user_id)

    @client.on_message(filters.command(["start", "help"]) & filters.private)
    async def _on_start(_: Client, message: Message) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            await message.reply_text(pg.render_error("You are not authorized to use this bot."),
                                     parse_mode=ParseMode.HTML)
            return
        await message.reply_text(_START_CARD, parse_mode=ParseMode.HTML)

    @client.on_message(filters.document & filters.private)
    async def _on_document(_: Client, message: Message) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            return
        doc = message.document
        if not doc or not (doc.file_name or "").lower().endswith(".torrent"):
            await message.reply_text(
                pg.render_status("Unsupported file", ["Please send a .torrent file or a link."],
                                 emoji="⚠️"),
                parse_mode=ParseMode.HTML,
            )
            return
        dest_dir = config.download_dir / "torrents"
        dest_dir.mkdir(parents=True, exist_ok=True)
        local_path = await message.download(file_name=str(dest_dir / doc.file_name))
        spec = sources.torrent_file_spec(str(local_path), doc.file_name)
        task = manager.create_task(
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            trigger_message_id=message.id,
            spec=spec,
        )
        await message.reply_text(
            pg.render_status("Source Received", [f"📦 {doc.file_name}", "Choose an action:"],
                             emoji="🧲"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.source_menu(task.token),
        )

    @client.on_message(filters.text & filters.private & ~filters.command(["start", "help"]))
    async def _on_text(_: Client, message: Message) -> None:
        if not _authorized(message.from_user.id if message.from_user else None):
            return
        spec = sources.classify(message.text or "")
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
                reply_markup=kb.source_menu(task.token),
            )
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
