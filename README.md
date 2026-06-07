<div align="center">

# 🎬 SIGN-SUB-BOT

**A production-grade, fully asynchronous Telegram bot that leeches media and auto-builds a clean _“Signs & Songs”_ subtitle track.**

Give it a direct link, magnet, `.torrent`, or a Nyaa.si search — it downloads with `aria2c`, runs the MKV through an `ffmpeg` subtitle pipeline, and sends back a tidy `{name}_clean_english.mkv`.

`python` · `asyncio` · `pyrogram` · `aria2c` · `ffmpeg`

</div>

---

## ✨ What it does

```
   you ──► 📥 leech ──► 🔍 ffprobe ──► ✂️ filter signs ──► 🧩 remux ──► 📤 upload ──► you
          (aria2c)      (stream map)   (drop dialogue)   (+Signs&Songs)
```

1. **Leech** the source with multi-connection `aria2c` (direct / magnet / torrent / Nyaa).
2. **Probe** the MKV's streams as JSON and locate the primary **English ASS** subtitle.
3. **Filter** that subtitle line-by-line, dropping every `Dialogue:` whose style is `default` or `song` — leaving only signs, typesetting and SFX.
4. **Remux** video + audio + English subtitles + the new **Signs & Songs** track (+ fonts/attachments), dropping all non-English subtitles and tagging the new track `language=eng` / `title=Signs & Songs`.
5. **Upload** the result back to your chat, then wipe every temporary file.

## 🚀 Features

| | |
|---|---|
| 💬 **Premium quoted UI** | Every notification is a native Telegram **blockquote** card with bold labels, a divider and a clean one-stat-per-line layout. |
| ⌨️ **Fully button-driven** | `📥 Start Download` · `⚙️ Filter Streams` · `🎵 Add Audio` · `❌ Cancel Task` — no commands to memorize. |
| 🎵 **Add external audio** | Attach extra audio (upload a file **or** paste a direct link), then pick its **language** and **title** from inline keyboards. Muxed in with correct metadata. |
| 📊 **Live progress** | Emoji progress bars with **speed / ETA / transferred / %** for download, processing **and** upload. |
| ⚡ **aria2c core** | Async JSON-RPC client + auto-spawned daemon; magnet→metadata→torrent hand-off handled for you. |
| 🔎 **Nyaa.si** | RSS-first scraper with HTML fallback — search by text or paste a `/view/` link. |
| 🏷️ **Smart filenames** | Reads the real title from a `.torrent`'s `info.name`, percent-decoded URLs, and HTTP `Content-Disposition`. |
| 🔒 **Write-locks** | Processing never touches a file that is still downloading. |
| 🧹 **Guaranteed cleanup** | Every task purges its buffers and loose `.ass` assets in a `finally` block. |

### 🎵 Add-Audio flow

```
🎵 Add Audio ─► send an audio file / paste a link ─► 🌐 pick language ─► 🏷️ pick title ─► (repeat or) 📥 Start
```

- **Formats:** AAC, MP3, M4A, FLAC, Opus, OGG, WAV, AC3, E-AC3, DTS, ALAC, WMA, MKA, AIFF, APE, …
- **Languages:** English, Japanese, Hindi, Spanish, French, German, Italian, Portuguese, Russian, Arabic, Chinese, Korean, Tamil, Telugu, Indonesian, Undetermined.
- **Titles:** Original, Dub, English Dub, Commentary, Karaoke, Surround 5.1, Stereo.

## 🖼️ What the cards look like

> 🔄 **Downloading**
> ➖➖➖➖➖➖➖➖➖
> ⚡ **Speed:** `12.40 MB/s`
> ⏳ **ETA:** `00:01:42`
> 📦 **Downloaded:** `450.00 MB / 900.00 MB`
> 📊 **Progress:** `50.0%`
> `[■■■■■□□□□□]`

> 🧲 **Source Received**
> ➖➖➖➖➖➖➖➖➖
> 📦 Yowayowa.Sensei.S01E01.mkv
> Choose an action:

## 🧱 Architecture

```
signsub/
├── __main__.py            # entrypoint: python -m signsub
├── config.py              # env-driven configuration
├── core/
│   ├── proc.py            # async subprocess helpers (ffmpeg/ffprobe)
│   ├── sources.py         # classify magnet/torrent/direct/nyaa/search
│   ├── status.py          # throttled edit-in-place status message
│   ├── task.py            # task + external-audio state model
│   └── manager.py         # orchestration: leech → process → upload → cleanup
├── handlers/
│   └── router.py          # Pyrogram message/callback handlers (the UX)
├── leech/
│   ├── aria2_client.py    # async aria2 JSON-RPC client
│   ├── daemon.py          # spawn/supervise a local aria2c daemon
│   ├── engine.py          # resolve source → download → write-lock verify
│   ├── nyaa.py            # Nyaa.si RSS + HTML scraper
│   └── torrent_meta.py    # bencode/URL/Content-Disposition filename detection
├── processing/
│   ├── ffprobe.py         # JSON stream introspection
│   └── pipeline.py        # the subtitle automation pipeline
├── ui/
│   ├── fmt.py             # HTML blockquote/bold/code primitives
│   ├── progress.py        # sizes/speeds/ETA + emoji progress cards
│   └── keyboards.py       # inline keyboard factories
└── upload/
    └── uploader.py        # chunked document upload with progress
```

> **Why HTML, not MarkdownV2?** Pyrogram's markdown dialect can't emit blockquote
> entities, so the cards are authored in HTML — which renders a genuine
> `MessageEntityBlockquote` and gives the exact quoted look.

## ⚙️ Setup

**Requirements:** Python 3.10+, plus `ffmpeg`/`ffprobe` and `aria2` on `PATH`.

```bash
sudo apt-get install -y ffmpeg aria2
pip install -r requirements.txt
```

**Configure** — copy the example env and fill in your Telegram credentials from
[my.telegram.org](https://my.telegram.org) and [@BotFather](https://t.me/BotFather):

```bash
cp .env.example .env       # then edit the three values below
```

| Variable | Required | Description |
|---|:---:|---|
| `TELEGRAM_API_ID` | ✅ | API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | API hash from my.telegram.org |
| `BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `WORK_DIR` | | Working dir for downloads + session (default `./data`, `/data` in Docker) |
| `OWNER_ID` | | Owner's Telegram user ID — full control, incl. `/users add\|remove` |
| `ADMINS` | | Comma/space-separated admin IDs — may use `/stats`, `/tasks`, `/users` |
| `ALLOWED_USERS` | | User IDs allowed to use the bot (default: everyone; owner/admins always allowed) |
| `MAX_CONCURRENT_TASKS` | | Max simultaneous tasks (default `3`) |

## ▶️ Run

```bash
python -m signsub
```

Then DM the bot:

- a **direct link**, **magnet**, **Nyaa.si link**, or upload a **`.torrent`** file, **or**
- any **text** to search Nyaa.si and pick a result,

and use the inline buttons to start, add audio, inspect the filter policy, or cancel.

## 🤖 Commands & roles

There are three roles: **owner** (`OWNER_ID`) ▸ **admin** (`ADMINS`) ▸ **user**
(`ALLOWED_USERS`, or everyone if the allow-list is empty). Owner and admins are
always allowed regardless of the allow-list.

| Command | Who | What |
|---|---|---|
| `/start`, `/help` | everyone | Role-aware help card |
| `/addaudio` (`/muxaudio`) | users | Add an external audio track to the pending file before starting (same as the 🎵 button) |
| `/stats` | admin/owner | Uptime, tasks created, completed/failed/cancelled, active vs. slots, unique users |
| `/tasks` | admin/owner | Live list of tracked tasks with state, label, owner and age |
| `/users` | admin/owner | List owner/admins/allow-list and seen users |
| `/users add <id>` · `/users remove <id>` | owner | Authorize / revoke a user at runtime (not persisted across restarts) |

## 🐳 Docker

The image bundles `ffmpeg`/`ffprobe` and `aria2c`, so it is fully self-contained.

```bash
docker build -t signsub-bot .

docker run --rm -it \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  signsub-bot
```

`WORK_DIR` defaults to `/data` inside the image — mount a volume there to persist
the Pyrogram session and downloads across restarts.

## 🩺 Troubleshooting

- **`FFmpeg … failed (rc=…)` cards** now include the most relevant ffmpeg stderr
  lines (not just the generic tail), so the card tells you the real cause — e.g.
  an unsupported codec or a stream that can't be stream-copied.
- **No output / “No sign events remained”** — the source had no ASS subtitle with
  non-dialogue styles to keep; nothing to build a Signs & Songs track from.
- **aria2 won't start** — ensure `aria2c` is on `PATH`; the bot spawns
  `aria2c --enable-rpc` automatically when targeting localhost.

## 🖥️ Standalone CLI

`SIGNSUB.py` remains available as a synchronous, offline CLI for processing a
local `.mkv` without Telegram:

```bash
python SIGNSUB.py
```
