# 📦 Telegram File Archive Bot

A personal Telegram bot that turns a private group chat into a structured file archive — store, organise, search, and retrieve any file type through an interactive inline keyboard interface.

---

## Table of Contents

- [Features](#features)
- [Architecture Overview](#architecture-overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Bot](#running-the-bot)
- [Commands](#commands)
- [File Organisation](#file-organisation)
- [Supported File Types](#supported-file-types)
- [Database & Backup](#database--backup)
- [Security](#security)
- [Disk Report](#disk-report)

---

## Features

- **Folder tree** — create nested folders, rename, move, and delete them
- **File management** — store, rename, move, copy, delete, and mark files as favourites
- **Search** — fuzzy filename search across the entire archive
- **Recent files** — quick view of the last 10 uploaded files
- **Pagination** — browse large folders page by page (10 items per page)
- **Sorting** — sort files by newest, oldest, or alphabetical order
- **Stats** — full folder-tree breakdown with file counts
- **Disk report** — visual PNG dashboard showing storage usage and backup health
- **Automatic backups** — rolling JSON backups (5 generations) with corruption recovery
- **Archive group protection** — auto-kicks anyone other than the owner who joins the archive group

---

## Architecture Overview

```
main.py
├── Config & constants
├── JSON database layer  (load, save, rotate backups, migrate timestamps)
├── DB helpers           (path normalisation, search, folder tree ops)
├── File type classifier (extension → type, emoji mapping)
├── Auth guard           (single-owner access control)
├── Conversation handler (PTB ConversationHandler with 8 states)
├── Command handlers     (/start /menu /search /recent /stats /help …)
├── Inline button router (button_handler dispatches all callback queries)
├── Disk report renderer (Pillow-based PNG dashboard)
└── Archive group guard  (ChatMemberHandler auto-kicks intruders)
```

The bot uses **python-telegram-bot v20+** (async) and stores all metadata in a local `metadata.json` file. The actual files are forwarded to and stored in the Telegram archive group; the bot only keeps the `message_id` pointers.

---

## Requirements

- Python 3.10+
- `python-telegram-bot[job-queue]` v20+
- `Pillow` (for the `/disk` report image)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A **private Telegram group** to act as the file archive backend

### Install dependencies

```bash
pip install "python-telegram-bot[job-queue]" Pillow
```

---

## Installation

```bash
git clone <your-repo-url>
cd <repo-directory>
pip install "python-telegram-bot[job-queue]" Pillow
```

---

## Configuration

All configuration is done via **environment variables**. No config file is needed.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from BotFather |
| `ARCHIVE_CHAT_ID` | ✅ | Numeric ID of the private archive group (negative number) |
| `OWNER_ID` | ✅ | Your Telegram user ID — the only user who can operate the bot |
| `DB_PATH` | ☐ | Path to the metadata JSON file (default: `metadata.json`) |

### Example

```bash
export BOT_TOKEN="123456:ABC-DEF..."
export ARCHIVE_CHAT_ID="-1001234567890"
export OWNER_ID="987654321"
export DB_PATH="/data/metadata.json"   # optional
```

> **Tip:** Use a `.env` file and load it with `python-dotenv`, or set these in your systemd unit / Docker environment.

---

## Running the Bot

```bash
python main.py
```

The bot uses long-polling. On startup it checks the database for corruption and migrates any legacy UTC timestamps to IST automatically.

### Running with Docker (example)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY main.py .
RUN pip install "python-telegram-bot[job-queue]" Pillow
ENV DB_PATH=/data/metadata.json
VOLUME ["/data"]
CMD ["python", "main.py"]
```

```bash
docker run -d \
  -e BOT_TOKEN="..." \
  -e ARCHIVE_CHAT_ID="..." \
  -e OWNER_ID="..." \
  -v /your/data/dir:/data \
  your-image-name
```

---

## Commands

| Command | Description |
|---|---|
| `/start` | Open the main menu |
| `/menu` | Open the main menu (alias) |
| `/search` | Search files by name |
| `/recent` | View the 10 most recently stored files |
| `/stats` | Full folder tree with file counts |
| `/help` | Show available commands |
| `/backup` | Send a copy of the database file to your DM |
| `/restore` | Restore the database from a previously sent backup file |
| `/disk` | Generate a visual disk-usage and backup-health report |
| `/joinme` | Get a single-use invite link to the archive group (auto-promotes you to admin) |
| `/cancel` | Cancel the current operation |

---

## File Organisation

Files are organised in a virtual folder tree rooted at **Root**. Paths are stored as strings in the format `Root/FolderA/SubfolderB`.

### Operations available via the inline menu

- **Browse** folders and files
- **Create** new folders
- **Store** files (send any supported file; caption becomes the filename)
- **Retrieve** files (bot forwards the file from the archive group)
- **Delete** files or entire folder trees
- **Rename** files or folders
- **Move** files or folders to a different location
- **Copy** files to another folder
- **Favourite** / unfavourite files
- **Sort** files (newest / oldest / alphabetical)

---

## Supported File Types

| Category | Extensions |
|---|---|
| 🎬 Video | mp4, mkv, mov, avi, webm, flv, m4v, 3gp, ts, wmv, … |
| 🎵 Audio | mp3, m4a, flac, wav, ogg, opus, aac, wma, aiff, … |
| 🖼️ Image | jpg, jpeg, png, gif, webp, bmp, heic, tiff, svg, avif, … |
| 📄 Document | pdf, doc, docx, xls, xlsx, ppt, pptx, txt, csv, epub, … |
| 🗂️ Archive | zip, rar, 7z, tar, gz, bz2, xz, zst, iso, … |
| ⚙️ App | apk, aab, ipa, exe, msi, dmg, deb, rpm, … |
| 🧩 Code | json, xml, yaml, html, css, js, py, java, kt, sql, sh, … |
| 🔤 Font | ttf, otf, woff, woff2 |

Telegram voice messages, stickers, and photos are also stored natively.

---

## Database & Backup

Metadata is stored in a single JSON file (`metadata.json` by default). Each entry is keyed by the archive group `message_id` and contains:

```json
{
  "12345": {
    "filename": "project_report.pdf",
    "folder": "Root/Work/2024",
    "type": "document",
    "stored_at": "2024-08-01T14:30:00+05:30",
    "file_size": 204800,
    "favourite": false
  }
}
```

### Backup strategy

- On every save, a rolling set of **5 backup files** (`metadata.backup.1.json` … `metadata.backup.5.json`) is maintained.
- On startup, if the main file is missing or corrupt, the bot automatically recovers from the most recent healthy backup.
- Use `/backup` to receive the live database as a file in your DM at any time.
- Use `/restore` to upload a previously received backup and replace the live database.

---

## Security

- **Single-owner access** — every command and callback is gated by `OWNER_ID`. Anyone else gets an `⛔ Unauthorized` response.
- **Archive group protection** — the bot listens for `chat_member` events on the archive group. Any non-owner who joins is immediately kicked and banned, and you receive an intruder alert in your DM.
- The bot never exposes file contents or invite links to anyone other than the owner.

---

## Disk Report

`/disk` generates a PNG image showing:

- Total file count and storage used
- Per-type breakdown (video, audio, document, etc.) with size bars
- Size of each database file (live + backups)
- Backup health indicators (healthy / corrupt / missing)
- `DB_PATH` environment variable status

Sizes are fetched live from Telegram for files under 20 MB and cached in the metadata for subsequent calls.

---

## Timestamps

All timestamps are stored and displayed in **IST (UTC+5:30)**. On startup the bot automatically migrates any legacy UTC timestamps in the database to IST.