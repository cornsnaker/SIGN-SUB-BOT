# SIGN-SUB-BOT

A production-grade, fully asynchronous Telegram bot that **leeches media**
(direct links, magnets, `.torrent` files, or Nyaa.si) and runs the result
through an automated **FFmpeg subtitle pipeline** that builds a standalone
**"Signs & Songs"** subtitle track, drops non-English subtitles, and remuxes a
clean `{name}_clean_english.mkv` back to you.

## Features

- **MarkdownV2 quoted UI** — every notification renders inside Telegram `>` blockquotes.
- **Inline keyboards** — `📥 Start Download` · `⚙️ Filter Streams` · `❌ Cancel Task`.
- **Live emoji progress bars** with speed / ETA / processed stats for download, processing and upload.
- **aria2c JSON-RPC leeching core** (async) — multi-connection direct downloads, magnets and torrents, with the magnet→metadata→torrent hand-off handled automatically.
- **Nyaa.si scraper** — RSS-first with an HTML fallback; search by text or paste a `/view/` link.
- **Write-locks** — processing never touches a file that is still downloading.
- **FFmpeg subtitle automation** — `ffprobe` JSON stream mapping → extract the primary English ASS layer → strip `default`/`song` styled dialogue line-by-line → remux `0:v` + `0:a` + English subs + the new Signs & Songs track + fonts/attachments, tagging it `language=eng` / `title=Signs & Songs`.
- **Chunked uploads** via Pyrogram with throttled progress.
- **Guaranteed cleanup** — every task purges its download buffers and loose `.ass` assets in a `finally` block.

## Architecture

```
signsub/
├── __main__.py            # entrypoint: python -m signsub
├── config.py              # env-driven configuration
├── core/
│   ├── proc.py            # async subprocess helpers (ffmpeg/ffprobe)
│   ├── sources.py         # classify magnet/torrent/direct/nyaa/search
│   ├── status.py          # throttled edit-in-place status message
│   ├── task.py            # task state model
│   └── manager.py         # orchestration: leech → process → upload → cleanup
├── leech/
│   ├── aria2_client.py    # async aria2 JSON-RPC client
│   ├── daemon.py          # spawn/supervise a local aria2c daemon
│   ├── engine.py          # resolve source → download → write-lock verify
│   └── nyaa.py            # Nyaa.si RSS + HTML scraper
├── processing/
│   ├── ffprobe.py         # JSON stream introspection
│   └── pipeline.py        # the subtitle automation pipeline
├── ui/
│   ├── markdown.py        # MarkdownV2 escaping + blockquote builder
│   ├── progress.py        # sizes/speeds/ETA + emoji progress bars
│   └── keyboards.py       # inline keyboard factories
└── upload/
    └── uploader.py        # chunked document upload with progress
```

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`
- `aria2` (the bot will spawn `aria2c --enable-rpc` automatically when targeting localhost)

```bash
sudo apt-get install -y ffmpeg aria2
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your Telegram credentials:

```bash
cp .env.example .env
# edit TELEGRAM_API_ID, TELEGRAM_API_HASH, BOT_TOKEN
```

## Run

```bash
python -m signsub
```

Then DM the bot:
- a **direct link**, **magnet**, **Nyaa.si link**, or upload a **`.torrent`** file, or
- any **text** to search Nyaa.si and pick a result.

Use the inline buttons to start, inspect the filter policy, or cancel.

## Docker

The image bundles `ffmpeg`/`ffprobe` and `aria2c`, so it is fully self-contained.

```bash
# Build
docker build -t signsub-bot .

# Run (config via --env-file; /data persists downloads + the session file)
docker run --rm -it \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  signsub-bot
```

The container reads the same environment variables as `.env.example`. `WORK_DIR`
defaults to `/data` inside the image; mount a volume there to persist the
Pyrogram session and avoid re-downloading on restart.

## Standalone CLI

`SIGNSUB.py` remains available as a synchronous, offline CLI for processing a
local `.mkv` without Telegram:

```bash
python SIGNSUB.py
```
