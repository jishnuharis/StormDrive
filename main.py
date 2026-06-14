"""
Archive Bot — v4
Changes from uploaded version:
  FIXED:
    - /help crash: switched to HTML parse_mode, no more Markdown special-char breakage
    - Subfolder rename: cd: in rename mode now drills down (folders stay navigable);
      rename_folder: only fires when user taps the rename-icon button, not cd:
    - Move mode cd: now correctly shows folder list (not file list)
    - pick_move no longer double-edits the message (removed the dead first edit)
    - "Rename this folder" hidden at Root level
    - /cancel properly resets move_target and all pending ops
    - Sort preference preserved across folder navigation (not reset on mode change)
    - Back-from-file-action returns to search results when view=="search"
    - Stats (inline + command) shows full recursive breakdown
    - Folder-info panel added (tap folder in retrieve mode)
    - "Rename this folder" button also available in retrieve mode (not just rename mode)
  REMOVED:
    - /migrate_ist command and migrate_ist_command function
    - migrate_ist from set_commands and help text
    (IST timezone is still used for all timestamps; auto-migration at startup is kept)
  ADDED:
    - /recent — last 15 stored files with quick retrieve
    - ⭐ Favourite / pin files — shows at top of folder, accessible via Favourites view
    - Folder move — move an entire folder tree to a new parent
    - Folder info panel — file count, subfolder count, newest file date
    - Full recursive stats breakdown
    - WAIT_MOVE_DEST conversation state (so /cancel works during move)
"""

from __future__ import annotations

import json
import os
import time as _time_module
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import (
    BotCommand,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ARCHIVE_CHAT_ID = int(os.environ["ARCHIVE_CHAT_ID"])
OWNER_ID = int(os.environ["OWNER_ID"])

_DB_PATH = Path(os.environ.get("DB_PATH", "metadata.json"))
_DB_DIR = _DB_PATH.parent
DB_FILE = _DB_PATH
DB_TMP_FILE = _DB_DIR / "metadata.tmp.json"
BACKUP_COUNT = 5
PAGE_SIZE = 10
RECENT_N = 15
IST = timezone(timedelta(hours=5, minutes=30))
_BOT_START_TIME = _time_module.time()
_MSG_PAD = "\u200b\u200c\u200d" * 5  # Invisible padding for message height

_DB_DIR.mkdir(parents=True, exist_ok=True)

# Conversation states
WAIT_NEW_FOLDER = 1
WAIT_STORE_FILE = 2
WAIT_RENAME_INPUT = 3
WAIT_RENAME_FOLDER = 4
WAIT_SEARCH_INPUT = 5
WAIT_MOVE_DEST = 6  # navigating to folder-move destination
WAIT_MOVE_FILE_DST = 7  # navigating to file-move destination
WAIT_COPY_DST = 8  # navigating to copy destination

# ---------------------------------------------------------------------------
# In-memory user state
# user_state[uid] = {
#   mode               : "store"|"retrieve"|"delete"|"rename"|"move_file"|"move_folder"|"copy_file"
#   path               : str
#   view               : "folders"|"files"|"search"
#   page               : int
#   last_btn_msg       : int|None
#   store_count        : int
#   rename_target      : int|None      message_id of file being renamed
#   rename_folder_path : str|None      full path of folder being renamed
#   move_target        : int|None      message_id of file being moved
#   move_folder_path   : str|None      full path of folder being moved
#   search_items       : list|None     last search result items
#   sort_order         : "newest"|"oldest"|"alpha"
# }
# ---------------------------------------------------------------------------
user_state: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def _backup_path(n: int) -> Path:
    return _DB_DIR / f"metadata.backup.{n}.json"


def _rotate_backups() -> None:
    for i in range(BACKUP_COUNT - 1, 0, -1):
        src, dst = _backup_path(i), _backup_path(i + 1)
        if src.exists():
            src.replace(dst)
    if DB_FILE.exists():
        import shutil
        shutil.copy2(DB_FILE, _backup_path(1))


def _try_parse(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _migrate_timestamps_to_ist(data: dict) -> tuple[dict, int]:
    """Shift any UTC/naive timestamps to IST. Idempotent."""
    changed = 0
    for item in data.values():
        ts = item.get("stored_at", "")
        if not ts or "+05:30" in ts:
            continue
        for fmt in ("%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                item["stored_at"] = dt.astimezone(IST).isoformat(timespec="seconds")
                changed += 1
                break
            except ValueError:
                continue
    return data, changed


def _startup_check() -> None:
    if _try_parse(DB_FILE) is not None:
        data = _try_parse(DB_FILE)
        if data:
            migrated, n = _migrate_timestamps_to_ist(data)
            if n:
                print(f"🕐  Migrated {n} timestamp(s) GMT → IST.")
                DB_FILE.write_text(json.dumps(migrated, indent=2, ensure_ascii=False), encoding="utf-8")
        return
    print(f"⚠️  {DB_FILE} missing/corrupt — attempting recovery…")
    for i in range(1, BACKUP_COUNT + 1):
        bp = _backup_path(i)
        data = _try_parse(bp)
        if data is not None:
            print(f"✅  Recovered from {bp}  ({len(data)} records)")
            DB_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return
        if bp.exists():
            print(f"   {bp} — also corrupt, skipping")
    print("⚠️  No valid backup — starting with empty database.")
    DB_FILE.write_text("{}", encoding="utf-8")


_startup_check()


def load_db() -> dict:
    data = _try_parse(DB_FILE)
    if data is None:
        for i in range(1, BACKUP_COUNT + 1):
            data = _try_parse(_backup_path(i))
            if data is not None:
                return data
        return {}
    return data


def save_db(data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    DB_TMP_FILE.write_text(text, encoding="utf-8")
    _rotate_backups()
    DB_TMP_FILE.replace(DB_FILE)


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _copy_name(db: dict, base_name: str, file_type: str, folder: str) -> str:
    """
    Generate a copy name like 'invoice (1)', 'invoice (2)' etc.
    Counts existing files in the same folder with the same base name and type.
    """
    folder = normalize_path(folder)
    count = sum(
        1 for v in db.values()
        if normalize_path(v.get("folder", "Root")) == folder
        and v.get("type") == file_type
        and (v.get("filename", "") == base_name or v.get("filename", "").startswith(f"{base_name} ("))
    )
    return f"{base_name} ({count})"


def _unique_copy_name(db: dict, base_name: str, file_type: str, dest_path: str) -> str:
    """
    Return a filename safe to use in dest_path: if base_name (same type) doesn't
    already exist there, keep it as-is; otherwise append ' (n)' with the next
    free number, regardless of whether dest_path is the source folder or not.
    """
    dest_path = normalize_path(dest_path)
    existing = {
        v.get("filename", "")
        for v in db.values()
        if normalize_path(v.get("folder", "Root")) == dest_path and v.get("type") == file_type
    }
    if base_name not in existing:
        return base_name
    n = 1
    while f"{base_name} ({n})" in existing:
        n += 1
    return f"{base_name} ({n})"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def normalize_path(path: str | None) -> str:
    if not path:
        return "Root"
    path = path.strip().strip("/")
    if path in ("", "Root"):
        return "Root"
    if path.startswith("Root/"):
        path = path[5:]
    parts = [p for p in path.split("/") if p]
    return "Root" if not parts else "Root/" + "/".join(parts)


def get_subfolders_for_path(db: dict, current_path: str) -> list[str]:
    current_path = normalize_path(current_path)
    prefix = current_path + "/"
    paths = {normalize_path(item.get("folder", "Root")) for item in db.values()}
    children: set[str] = set()
    for p in paths:
        if p.startswith(prefix):
            rest = p[len(prefix):]
            if rest:
                children.add(rest.split("/", 1)[0])
    return sorted(children)


def get_files_in_folder(db: dict, folder_path: str) -> list[dict]:
    folder_path = normalize_path(folder_path)
    return [item for item in db.values()
            if normalize_path(item.get("folder", "Root")) == folder_path]


def count_all_in_tree(db: dict, folder_path: str) -> int:
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    return sum(1 for item in db.values()
               if normalize_path(item.get("folder", "Root")) == folder_path
               or normalize_path(item.get("folder", "Root")).startswith(prefix))


def delete_folder_tree(db: dict, folder_path: str) -> list[str]:
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    keys = [k for k, item in db.items()
            if normalize_path(item.get("folder", "Root")) == folder_path
            or normalize_path(item.get("folder", "Root")).startswith(prefix)]
    for k in keys:
        del db[k]
    return keys


def rename_folder_in_db(db: dict, old_path: str, new_path: str) -> int:
    old_path = normalize_path(old_path)
    new_path = normalize_path(new_path)
    old_prefix = old_path + "/"
    count = 0
    for item in db.values():
        folder = normalize_path(item.get("folder", "Root"))
        if folder == old_path:
            item["folder"] = new_path
            count += 1
        elif folder.startswith(old_prefix):
            item["folder"] = new_path + "/" + folder[len(old_prefix):]
            count += 1
    return count


def move_folder_in_db(db: dict, old_path: str, new_parent: str) -> tuple[int, str]:
    """
    Move folder old_path under new_parent.
    Returns (records_updated, new_full_path).
    """
    old_path = normalize_path(old_path)
    new_parent = normalize_path(new_parent)
    folder_name = old_path.rsplit("/", 1)[-1]
    new_path = normalize_path(f"{new_parent}/{folder_name}")
    updated = rename_folder_in_db(db, old_path, new_path)
    return updated, new_path


def search_files(db: dict, query: str) -> list[dict]:
    q = query.strip().lower()
    return [item for item in db.values() if q in item.get("filename", "").lower()]


def get_recent_files(db: dict, n: int = RECENT_N) -> list[dict]:
    items = [i for i in db.values() if i.get("stored_at")]
    items.sort(key=lambda x: x["stored_at"], reverse=True)
    return items[:n]


def get_favourite_files(db: dict) -> list[dict]:
    return [item for item in db.values() if item.get("favourite")]


def format_breadcrumb(path: str) -> str:
    return normalize_path(path).replace("/", " › ")


def parent_path(path: str) -> str:
    path = normalize_path(path)
    if path == "Root":
        return "Root"
    return normalize_path(path.rsplit("/", 1)[0])


def resolve_filename(message: Message, fallback: str) -> str:
    caption = (message.caption or "").strip()
    if caption:
        return caption
    for attr in ("document", "video", "audio"):
        obj = getattr(message, attr, None)
        if obj and getattr(obj, "file_name", None):
            return obj.file_name
    return fallback


def db_stats_full(db: dict) -> str:
    """
    Return a full recursive stats string.
    Walks every unique folder path and shows file count + subfolder count.
    """
    if not db:
        return "  (empty)"

    # Build a set of all folder paths that actually have files
    folder_file_counts: dict[str, int] = {}
    for item in db.values():
        fp = normalize_path(item.get("folder", "Root"))
        folder_file_counts[fp] = folder_file_counts.get(fp, 0) + 1

    # Collect all unique folder paths (including ancestors)
    all_folders: set[str] = set()
    for fp in folder_file_counts:
        parts = fp.split("/")
        for i in range(len(parts)):
            all_folders.add("/".join(parts[:i + 1]))

    total_files = len(db)
    total_folders = len(all_folders) - 1  # exclude Root itself

    # Build lines: show folders depth-first, indented
    def _children(parent: str) -> list[str]:
        prefix = parent + "/"
        direct = set()
        for f in all_folders:
            if f.startswith(prefix):
                rest = f[len(prefix):]
                if rest and "/" not in rest:
                    direct.add(f)
        return sorted(direct)

    lines: list[str] = [
        f"Total: <b>{total_files}</b> files in <b>{total_folders}</b> folder(s)\n"
    ]
    root_direct = len(folder_file_counts.get("Root", 0) and [1] or [])
    root_files = folder_file_counts.get("Root", 0)

    def _recurse(folder: str, depth: int) -> None:
        indent = "     " * depth
        name = folder.rsplit("/", 1)[-1] if "/" in folder else folder
        direct_files = folder_file_counts.get(folder, 0)
        tree_files = count_all_in_tree(db, folder)
        children = _children(folder)
        sub_count = len(children)
        suffix = f" ({tree_files} total)" if tree_files != direct_files else ""
        lines.append(f"{indent}📁 <b>{name}</b>: {direct_files} file(s){suffix}")
        for child in children:
            _recurse(child, depth + 1)

    if root_files:
        lines.append(f"📂 Root: {root_files} file(s)")

    for child in _children("Root"):
        _recurse(child, 0)

    return "\n".join(lines)


def folder_tree_size(db: dict, folder_path: str) -> int:
    """Total file_size (bytes) of everything in folder_path and its subtree."""
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    total = 0
    for item in db.values():
        fp = normalize_path(item.get("folder", "Root"))
        if fp == folder_path or fp.startswith(prefix):
            total += item.get("file_size", 0) or 0
    return total


def type_size_breakdown_for_path(db: dict, folder_path: str) -> dict[str, int]:
    """Map effective_type -> total size in bytes, for files in folder_path's subtree."""
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    sizes: dict[str, int] = {}
    for item in db.values():
        fp = normalize_path(item.get("folder", "Root"))
        if fp == folder_path or fp.startswith(prefix):
            t = effective_type(item)
            sizes[t] = sizes.get(t, 0) + (item.get("file_size", 0) or 0)
    return sizes


EXT_TYPE_MAP = {
    # video
    ".mp4": "video", ".mkv": "video", ".mov": "video", ".avi": "video",
    ".webm": "video", ".flv": "video", ".m4v": "video", ".3gp": "video",
    ".ts": "video", ".mts": "video", ".m2ts": "video", ".vob": "video",
    ".wmv": "video", ".rm": "video", ".rmvb": "video", ".divx": "video",
    # audio
    ".mp3": "audio", ".m4a": "audio", ".flac": "audio", ".wav": "audio",
    ".ogg": "audio", ".opus": "audio", ".aac": "audio", ".wma": "audio",
    ".aiff": "audio", ".alac": "audio", ".mid": "audio", ".midi": "audio",
    ".amr": "audio", ".ape": "audio",
    # images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".heic": "image", ".heif": "image",
    ".tiff": "image", ".tif": "image", ".svg": "image", ".ico": "image",
    ".raw": "image", ".cr2": "image", ".nef": "image", ".arw": "image",
    ".avif": "image",
    # documents
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".xls": "document", ".xlsx": "document", ".ppt": "document",
    ".pptx": "document", ".txt": "document", ".csv": "document",
    ".odt": "document", ".ods": "document", ".odp": "document",
    ".rtf": "document", ".md": "document", ".epub": "document",
    ".pages": "document", ".numbers": "document", ".key": "document",
    # archives
    ".zip": "archive", ".rar": "archive", ".7z": "archive",
    ".tar": "archive", ".gz": "archive", ".bz2": "archive",
    ".xz": "archive", ".zst": "archive", ".iso": "archive",
    ".tgz": "archive", ".tbz2": "archive",
    # apps / executables
    ".apk": "app", ".aab": "app", ".ipa": "app",
    ".exe": "app", ".msi": "app", ".dmg": "app", ".pkg": "app",
    ".deb": "app", ".rpm": "app", ".appimage": "app",
    # code / data
    ".json": "code", ".xml": "code", ".yaml": "code", ".yml": "code",
    ".html": "code", ".htm": "code", ".css": "code", ".js": "code",
    ".py": "code", ".java": "code", ".kt": "code", ".swift": "code",
    ".c": "code", ".cpp": "code", ".h": "code",
    ".sql": "code", ".sh": "code", ".bat": "code",
    # fonts
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
}

TYPE_EMOJI = {
    "video":    "🎬",
    "audio":    "🎵",
    "image":    "🖼️",
    "document": "📄",
    "archive":  "🗂️",
    "app":      "⚙️",
    "code":     "🧩",
    "font":     "🔤",
    "photo":    "🖼️",
    "voice":    "🎙",
    "sticker":  "🎴",
    "other":    "❓",
}


def classify_by_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return EXT_TYPE_MAP.get(ext, "other")


def effective_type(item: dict) -> str:
    """
    Returns the best available type for a DB record.
    For non-document Telegram types (video/audio/photo/voice/sticker),
    trust Telegram directly. For 'document', refine using the filename extension.
    """
    stored = item.get("type", "document")
    if stored != "document":
        return stored
    filename = item.get("filename", "")
    ext_type = classify_by_extension(filename)
    if ext_type != "other":
        return ext_type
    return stored  # stay as "document" if extension gives nothing


def file_type_emoji(file_type: str) -> str:
    return TYPE_EMOJI.get(file_type, "📄")


def _esc(text: str) -> str:
    """Escape HTML special chars for safe inclusion in HTML parse_mode messages."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


async def deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("⛔ Unauthorized.", show_alert=True)
    elif update.message:
        await update.message.reply_text("⛔ Unauthorized.")


# ---------------------------------------------------------------------------
# Retire old button message
# ---------------------------------------------------------------------------

async def retire_last_btn_msg(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        state: dict,
        count: int,
        folder_path: str,
) -> None:
    msg_id = state.get("last_btn_msg")
    if not msg_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                f"📁 <b>{_esc(format_breadcrumb(folder_path))}</b>\n"
                f"<i>{count - 1} file{'s' if count - 1 != 1 else ''} stored so far — keep sending…</i>"
            ),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        pass
    state["last_btn_msg"] = None


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def build_paginated_keyboard(
        items: list[tuple[str, str]],
        page: int,
        extra_top_rows: list | None = None,
        extra_bottom_rows: list | None = None,
) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    keyboard = [row for row in (extra_top_rows or []) if row]
    chunk = items[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]
    for label, cb in chunk:
        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"page:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"page:{page + 1}"))
    if nav:
        keyboard.append(nav)
    for row in extra_bottom_rows or []:
        if row:
            keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Favourites", callback_data="action:favourites"),
         InlineKeyboardButton("🔍 Search", callback_data="action:search")],

        [InlineKeyboardButton("📋 Recent", callback_data="action:recent"),
         InlineKeyboardButton("📂 Browse", callback_data="mode:retrieve")],

        [InlineKeyboardButton("📥 Store", callback_data="mode:store"),
         InlineKeyboardButton("🗑 Delete", callback_data="mode:delete")],

        [InlineKeyboardButton("✏️ Rename", callback_data="mode:rename"),
         InlineKeyboardButton("📊 Stats", callback_data="action:stats")],

        [InlineKeyboardButton("📦 Storage Explorer", callback_data="action:storage_explorer")],

        [InlineKeyboardButton("💾 Disk Report", callback_data="action:disk")]
    ])


def _uptime_str() -> str:
    elapsed = int(_time_module.time() - _BOT_START_TIME)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    elif m:
        return f"{m}m {s}s"
    return f"{s}s"


def _home_menu_text() -> str:
    """Build the main menu header with live file/folder counts."""
    db = load_db()
    total_files = len(db)
    # Count unique folder paths (excluding Root itself)
    folder_paths: set[str] = set()
    for item in db.values():
        fp = normalize_path(item.get("folder", "Root"))
        parts = fp.split("/")
        for i in range(len(parts)):
            folder_paths.add("/".join(parts[:i + 1]))
    total_folders = len(folder_paths) - 1  # exclude Root
    _PAD = "\u2007" * 38  # figure spaces — invisible width padding
    return (
        f"📦 <b>Archive Vault</b>{_PAD}\n\n"
        f"📄 Files: <b>{total_files:,}</b>\n"
        f"📁 Folders: <b>{max(total_folders, 0):,}</b>\n\n"
        f"📂 One place for everything you want to keep.\n"
        f"🔐 Private  •  v4  •  @super_storm5\n\n"
        f"🕐 {datetime.now(IST).strftime('%d %b %Y, %I:%M %p')} IST\n"
        f"⚙️ Uptime: {_uptime_str()}\n\n"
        f"🤖 <i>Created by and for</i>  •  <i>@super_storm5</i>\n\n"
        "Choose an action:"
    )


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Open archive menu"),
        BotCommand("menu", "Return to main menu"),
        BotCommand("cancel", "Cancel current action"),
        BotCommand("recent", "Show recently stored files"),
        BotCommand("stats", "Show archive statistics"),
        BotCommand("search", "Search files by name"),
        BotCommand("help", "Show help & command list"),
        BotCommand("joinme", "Get invite link & become admin in archive group"),
        BotCommand("backup", "Download metadata.json as a file"),
        BotCommand("restore", "Reply to a .json file to restore the database"),
        BotCommand("disk", "Show real-time volume disk usage"),
    ])


# ---------------------------------------------------------------------------
# Fresh state
# ---------------------------------------------------------------------------

def _fresh_state(mode: str = "retrieve", sort_order: str = "newest") -> dict:
    return {
        "mode": mode,
        "path": "Root",
        "view": "folders",
        "page": 0,
        "last_btn_msg": None,
        "store_count": 0,
        "rename_target": None,
        "rename_folder_path": None,
        "move_target": None,
        "move_targets": None,
        "move_folder_path": None,
        "copy_source": None,
        "search_items": None,
        "sort_order": sort_order,  # preserved on mode change
        "multiselect": False,
        "selected_files": set(),
        "copy_sources": None,
        "last_paste_dest": "Root",
        "storage_path": "Root",
    }


def _inherit_state(user_id: int, mode: str) -> dict:
    """Create a fresh state for a new mode but preserve sort_order."""
    old = user_state.get(user_id, {})
    return _fresh_state(mode, old.get("sort_order", "newest"))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text(
        _home_menu_text(),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text(
        _home_menu_text(),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    user_state.pop(update.effective_user.id, None)
    _PAD = "\u2007" * 38
    await update.message.reply_text(f"❌ Cancelled.{_PAD}", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    # Use HTML to avoid Markdown v1 parse errors
    await update.message.reply_text(
        "📖 <b>Archive Bot — Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start — open the main menu\n"
        "/menu — return to main menu from anywhere\n"
        "/cancel — cancel the current action\n"
        "/recent — show last 15 stored files\n"
        "/stats — show total files &amp; folder count\n"
        "/search — search files by name\n"
        "/joinme — get archive group invite &amp; become admin\n"
        "/backup — download metadata.json (your archive index)\n"
        "/restore — reply to a .json file to restore the database\n"
        "/disk — real-time disk &amp; archive PNG report\n"
        "/help — this message\n\n"
        "<b>Modes:</b>\n"
        "📥 <b>Store</b> — navigate/create folders, then send files.\n"
        "  Caption becomes the filename. Batch-send supported.\n\n"
        "📤 <b>Retrieve</b> — browse, sort (Newest / Oldest / A→Z), tap a file.\n"
        "  File card: Retrieve · Rename · Move · Delete · Duplicate · ⭐ Favourite\n"
        "  Folder card: file count, subfolder count, newest file date, rename/move.\n\n"
        "🗑 <b>Delete</b> — remove individual files or whole folder trees.\n\n"
        "✏️ <b>Rename</b> — rename any file or any folder (including subfolders).\n"
        "  Navigate into subfolders first, then tap to rename.\n\n"
        "🔀 <b>Move File</b> — pick a file, navigate to the destination folder.\n\n"
        "📁 <b>Move Folder</b> — pick a folder, navigate to the new parent.\n\n"
        "🔍 <b>Search</b> — find files by name across all folders.\n\n"
        "📋 <b>Recent</b> — last 15 stored files with quick-retrieve.\n\n"
        "⭐ <b>Favourites</b> — all starred files in one list.\n\n"
        "<b>Data safety:</b>\n"
        "• Saves are atomic — a crash mid-write won't corrupt the DB.\n"
        "• Last 5 saves are kept as rolling backups automatically.\n"
        "• Use /backup regularly to keep an off-device copy.\n"
        "• Use /restore to reload from any backup file.\n\n"
        "<b>Archive group security:</b>\n"
        "The archive group auto-kicks any intruder instantly.\n"
        "Use /joinme for a fresh single-use invite link.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
        ),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    db = load_db()
    breakdown = db_stats_full(db)
    await update.message.reply_text(
        f"📊 <b>Archive Statistics</b>\n\n{breakdown}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
        ),
    )


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    db = load_db()
    items = get_recent_files(db, RECENT_N)
    uid = update.effective_user.id
    state = user_state.setdefault(uid, _inherit_state(uid, "retrieve"))
    state["mode"] = "retrieve"
    if not items:
        await update.message.reply_text(
            "📋 No files stored yet.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
            ),
        )
        return ConversationHandler.END
    kb_items = [
        (
            f"{file_type_emoji(r.get('type', 'document'))} {r['filename']}\n"
            f"\u2514 📁 {format_breadcrumb(r.get('folder', 'Root'))}",
            f"file_action:{r['message_id']}",
        )
        for r in items
    ]
    kb = build_paginated_keyboard(
        kb_items, 0,
        extra_bottom_rows=[[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]],
    )
    await update.message.reply_text(
        f"📋 <b>Recent files</b> (last {len(items)}):\n\nTap a file for options:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    uid = update.effective_user.id
    user_state[uid] = _inherit_state(uid, "retrieve")
    await update.message.reply_text(
        "🔍 <b>Search</b>\n\nType the filename (or part of it):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="action:menu")]]
        ),
    )
    return WAIT_SEARCH_INPUT


async def receive_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    uid = update.effective_user.id
    q = update.message.text.strip()
    if not q:
        await update.message.reply_text("Please type something to search for.")
        return WAIT_SEARCH_INPUT
    db = load_db()
    results = search_files(db, q)
    state = user_state.setdefault(uid, _inherit_state(uid, "retrieve"))
    state["mode"] = "retrieve"
    state["view"] = "search"
    if not results:
        await update.message.reply_text(
            f"🔍 No files found matching <b>{_esc(q)}</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search again", callback_data="action:search")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
            ]),
        )
        return ConversationHandler.END
    items = [
        (
            f"{file_type_emoji(r.get('type', 'document'))} {r['filename']}\n"
            f"\u2514 📁 {format_breadcrumb(r.get('folder', 'Root'))}",
            f"file_action:{r['message_id']}",
        )
        for r in results
    ]
    state["search_items"] = items
    state["page"] = 0
    kb = build_paginated_keyboard(
        items, 0,
        extra_bottom_rows=[
            [InlineKeyboardButton("🔍 Search again", callback_data="action:search")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
        ],
    )
    await update.message.reply_text(
        f"🔍 <b>{len(results)} result{'s' if len(results) != 1 else ''}</b> for <i>{_esc(q)}</i>\n\n"
        "Tap a file for options:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------

async def show_folder_list(query, user_id: int) -> None:
    db = load_db()
    state = user_state.setdefault(user_id, _fresh_state())
    state["view"] = "folders"
    mode = state["mode"]
    page = state.get("page", 0)
    path = normalize_path(state.get("path", "Root"))
    children = get_subfolders_for_path(db, path)

    def folder_label(name: str) -> str:
        full = normalize_path(f"{path}/{name}")
        n = count_all_in_tree(db, full)
        return f"📁 {name}  ({n})" if n else f"📁 {name}"

    # In rename mode, folders are navigable (cd:) AND each has an individual rename button.
    # The cd: callback drills down; rename_folder: fires the rename prompt.
    # We show BOTH by putting each folder as a cd: item, and add an
    # "✏️ Rename this folder" for the CURRENT folder at the top.
    # (rename_folder_item: is a separate callback for subfolder rename from list)
    if mode == "rename":
        # cd: navigates in; rename_folder_item: renames that subfolder directly
        items = [(folder_label(name), f"cd:{name}") for name in children]
    elif mode in ("move_folder",):
        # navigating to drop destination
        items = [(folder_label(name), f"cd:{name}") for name in children]
    else:
        items = [(folder_label(name), f"cd:{name}") for name in children]

    top: list[list[InlineKeyboardButton]] = []
    total_files = len(get_files_in_folder(db, path))
    file_info = f" · {total_files} file{'s' if total_files != 1 else ''} here" if total_files else ""

    if mode == "store":
        top.append([InlineKeyboardButton("📥 Store here", callback_data="action:store_here")])
        top.append([InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")])
    elif mode == "rename":
        # Show current folder's rename button (hidden at Root)
        if path != "Root":
            top.append([InlineKeyboardButton("✏️ Rename this folder", callback_data="action:rename_this_folder")])
        # Show rename buttons for each child subfolder
        if children:
            top.append([InlineKeyboardButton(
                "⬇ Tap subfolder name to enter it, or:", callback_data="noop"
            )])
        # File renaming in rename mode
        if total_files:
            top.insert(0, [InlineKeyboardButton(
                f"✏️ Rename a file ({total_files})", callback_data="action:view_files"
            )])
    elif mode == "move_file":
        if state.get("move_targets"):
            n = len(state["move_targets"])
            top.append([InlineKeyboardButton(
                f"📂 Move {n} file(s) here", callback_data="action:move_here"
            )])
        elif state.get("move_target") is not None:
            db2 = load_db()
            fitem = db2.get(str(state["move_target"]), {})
            fname = fitem.get("filename", "file")
            top.append([InlineKeyboardButton(
                f"📂 Move '{_esc(fname[:20])}' here", callback_data="action:move_here"
            )])
        top.append([InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")])
    elif mode == "copy_file":
        if state.get("copy_sources"):
            n = len(state["copy_sources"])
            top.append([InlineKeyboardButton(
                f"📋 Copy {n} file(s) here", callback_data="action:copy_here"
            )])
        elif state.get("copy_source") is not None:
            db2 = load_db()
            fitem = db2.get(str(state["copy_source"]), {})
            fname = fitem.get("filename", "file")
            top.append([InlineKeyboardButton(
                f"📋 Copy '{_esc(fname[:20])}' here", callback_data="action:copy_here"
            )])
        top.append([InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")])
    elif mode == "move_folder":
        mfp = state.get("move_folder_path")
        if mfp:
            mname = normalize_path(mfp).rsplit("/", 1)[-1]
            # Cannot move into itself or a descendant
            if not normalize_path(path).startswith(normalize_path(mfp)):
                top.append([InlineKeyboardButton(
                    f"📁 Move '{_esc(mname[:20])}' here", callback_data="action:move_folder_here"
                )])

    if mode == "retrieve":
        if total_files:
            top.insert(0, [InlineKeyboardButton(
                f"📂 View {total_files} file{'s' if total_files != 1 else ''} here",
                callback_data="action:view_files",
            )])
        if path != "Root":
            top.append([InlineKeyboardButton("✏️ Rename this folder", callback_data="action:rename_this_folder")])
    elif mode in ("delete",) and total_files:
        top.insert(0, [InlineKeyboardButton(
            f"📂 View {total_files} file{'s' if total_files != 1 else ''} here",
            callback_data="action:view_files",
        )])

    if path != "Root":
        top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])
    top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])

    mode_headers = {
        "store": f"📁 <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}",
        "retrieve": f"📁 <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}",
        "delete": f"🗑 <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}",
        "rename": f"✏️ <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}",
        "move_file": f"🔀 <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}",
        "move_folder": f"📁 <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}",
        "copy_file": f"📋 <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}",
    }
    header = mode_headers.get(mode, f"📁 <b>{_esc(format_breadcrumb(path))}</b>{_esc(file_info)}")

    mode_suffixes = {
        "store": "\n\nChoose a subfolder or store here:",
        "retrieve": "\n\nSelect a folder to browse:",
        "delete": "\n\nSelect a folder to manage:",
        "rename": "\n\nEnter a subfolder to rename its contents, or tap ✏️ to rename the current folder:",
        "move_file": "\n\nNavigate to the destination folder:",
        "move_folder": "\n\nNavigate to the new parent folder:",
        "copy_file": "\n\nNavigate to the destination folder:",
    }
    no_child_suffixes = {
        "store": "\n\nNo subfolders — store here or create one:",
        "retrieve": "\n\nNo subfolders here.",
        "delete": "\n\nNo subfolders.",
        "rename": "\n\nNo subfolders here. Use ✏️ to rename this folder.",
        "move_file": "\n\nNo subfolders — move here or go up.",
        "move_folder": "\n\nNo subfolders — move here or go up.",
        "copy_file": "\n\nNo subfolders — copy here or go up.",
    }

    if not children:
        text = header + no_child_suffixes.get(mode, "") + "\n\n<i>No folders to browse</i>" + _MSG_PAD
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(top),
        )
        return

    text = header + mode_suffixes.get(mode, "\n\nSelect a folder:") + _MSG_PAD
    kb = build_paginated_keyboard(items, page, extra_top_rows=top)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def show_combined_view(query, user_id: int) -> None:
    """Unified folder view: subfolders on top, files below, all paginated together."""
    db = load_db()
    state = user_state.setdefault(user_id, _fresh_state())
    state["view"] = "files"
    path = normalize_path(state.get("path", "Root"))
    page = state.get("page", 0)
    mode = state["mode"]
    sort_order = state.get("sort_order", "newest")

    subfolders = get_subfolders_for_path(db, path)
    files = get_files_in_folder(db, path)

    # Sort files only — subfolders always stay on top
    if sort_order == "newest":
        files_fav = [f for f in files if f.get("favourite")]
        files_rest = [f for f in files if not f.get("favourite")]
        files_rest.sort(key=lambda f: f.get("stored_at", ""), reverse=True)
        files = files_fav + files_rest
    elif sort_order == "oldest":
        files_fav = [f for f in files if f.get("favourite")]
        files_rest = [f for f in files if not f.get("favourite")]
        files_rest.sort(key=lambda f: f.get("stored_at", ""))
        files = files_fav + files_rest
    else:  # alpha
        files_fav = [f for f in files if f.get("favourite")]
        files_rest = [f for f in files if not f.get("favourite")]
        files_rest.sort(key=lambda f: f.get("filename", "").lower())
        files = files_fav + files_rest

    _next_sort = {"newest": "oldest", "oldest": "alpha", "alpha": "newest"}
    _sort_label = {"newest": "🕐 Newest", "oldest": "🕑 Oldest", "alpha": "🔤 A→Z"}

    back_row = [InlineKeyboardButton("◀ Back", callback_data="action:back_folders")] if path != "Root" else []
    menu_row = [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]

    folder_label_fn = lambda name: (f"📁 {name}", f"cd:{name}")
    file_count_label = f"{len(files)} file{'s' if len(files) != 1 else ''}"
    folder_count_label = f"{len(subfolders)} subfolder{'s' if len(subfolders) != 1 else ''}"

    if not subfolders and not files:
        extra: list[list] = [back_row] if back_row else []
        if mode == "delete":
            extra.append([InlineKeyboardButton(
                "🗑 Delete empty folder", callback_data="action:delete_this_folder"
            )])
        extra.append(menu_row)
        await query.edit_message_text(
            f"📂 <b>{_esc(format_breadcrumb(path))}</b>\n\n<i>This folder is empty</i>\n\nReady to store your files here. Create subfolders to organize.{_MSG_PAD}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(extra),
        )
        return

    if mode == "delete":
        folder_items = [(f"📁 {name}", f"cd:{name}") for name in subfolders]
        file_items = [
            (f"🗑 {file_type_emoji(f.get('type', 'other'))} {f['filename']}",
             f"del_file:{f['message_id']}")
            for f in files
        ]
        all_items = folder_items + file_items
        top_rows = [back_row]
        if files:
            top_rows.insert(0, [InlineKeyboardButton(
                f"🗑 Delete entire folder ({file_count_label})",
                callback_data="action:delete_this_folder",
            )])
        kb = build_paginated_keyboard(all_items, page, extra_top_rows=top_rows, extra_bottom_rows=[menu_row])
        text = f"🗑 <b>{_esc(format_breadcrumb(path))}</b>\n\nTap a file to delete or enter a subfolder:"

    elif mode == "rename":
        folder_items = [(f"📁 {name}", f"cd:{name}") for name in subfolders]
        file_items = [
            (f"✏️ {file_type_emoji(f.get('type', 'other'))} {f['filename']}",
             f"rename_file:{f['message_id']}")
            for f in files
        ]
        all_items = folder_items + file_items
        kb = build_paginated_keyboard(all_items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row])
        text = f"✏️ <b>{_esc(format_breadcrumb(path))}</b>\n\nTap a file to rename or enter a subfolder:"

    elif mode == "move_file":
        move_target = state.get("move_target")
        move_targets = state.get("move_targets")
        folder_items = [(f"📁 {name}", f"cd:{name}") for name in subfolders]
        if move_targets:
            file_items = [(f"{file_type_emoji(f.get('type', 'other'))} {f['filename']}", "noop") for f in files]
            all_items = folder_items + file_items
            top_rows = [
                [InlineKeyboardButton(f"📂 Move {len(move_targets)} file(s) here", callback_data="action:move_here")],
                back_row,
            ]
            kb = build_paginated_keyboard(all_items, page, extra_top_rows=top_rows, extra_bottom_rows=[menu_row])
            text = (
                f"🔀 Moving {len(move_targets)} file(s)\n\n"
                f"Destination: <b>{_esc(format_breadcrumb(path))}</b>\n\n"
                "Tap 'Move here' to confirm, or navigate to a subfolder:"
            )
        elif move_target is None:
            file_items = [
                (f"🔀 {file_type_emoji(f.get('type', 'other'))} {f['filename']}",
                 f"pick_move_file:{f['message_id']}")
                for f in files
            ]
            all_items = folder_items + file_items
            kb = build_paginated_keyboard(all_items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row])
            text = f"🔀 <b>{_esc(format_breadcrumb(path))}</b>\n\nTap a file to move:"
        else:
            db2 = load_db()
            fitem = db2.get(str(move_target), {})
            fname = fitem.get("filename", f"ID {move_target}")
            file_items = [(f"{file_type_emoji(f.get('type', 'other'))} {f['filename']}", "noop") for f in files]
            all_items = folder_items + file_items
            top_rows = [
                [InlineKeyboardButton(f"📂 Move '{_esc(fname[:20])}' here", callback_data="action:move_here")],
                back_row,
            ]
            kb = build_paginated_keyboard(all_items, page, extra_top_rows=top_rows, extra_bottom_rows=[menu_row])
            text = (
                f"🔀 Moving: <b>{_esc(fname)}</b>\n\n"
                f"Destination: <b>{_esc(format_breadcrumb(path))}</b>\n\n"
                "Tap 'Move here' to confirm, or navigate to a subfolder:"
            )

    else:
        # retrieve — sort toggle + combined view
        multiselect = state.get("multiselect", False)
        selected = state.get("selected_files") or set()
        sort_row = [InlineKeyboardButton(
            f"Sort: {_sort_label[sort_order]}",
            callback_data=f"sort:{_next_sort[sort_order]}"
        )]
        folder_items = [(f"📁 {name}", f"cd:{name}") for name in subfolders]
        if multiselect:
            select_row = [InlineKeyboardButton("✖ Exit select mode", callback_data="action:multiselect_off")]
            file_items = [
                (f"{'✅' if f['message_id'] in selected else '⬜'} "
                 f"{'⭐' if f.get('favourite') else file_type_emoji(f.get('type', 'other'))} {f['filename']}",
                 f"toggle_sel:{f['message_id']}")
                for f in files
            ]
        else:
            select_row = [InlineKeyboardButton("☑️ Select files", callback_data="action:multiselect_on")] if files else []
            file_items = [
                (f"{'⭐' if f.get('favourite') else file_type_emoji(f.get('type', 'other'))} {f['filename']}",
                 f"file_action:{f['message_id']}")
                for f in files
            ]
        all_items = folder_items + file_items
        top_rows = []
        if select_row:
            top_rows.append(select_row)
        if not multiselect and len(files) > 1:
            top_rows.append(sort_row)
        top_rows.append(back_row)
        bottom_rows = []
        if multiselect:
            bottom_rows.append([InlineKeyboardButton(
                f"✅ Done ({len(selected)} selected)", callback_data="action:multi_done"
            )])
        bottom_rows.append(menu_row)
        kb = build_paginated_keyboard(
            all_items, page,
            extra_top_rows=top_rows,
            extra_bottom_rows=bottom_rows
        )
        # Build a clean folder display name (last segment, not full breadcrumb)
        _display_name = normalize_path(path).rsplit("/", 1)[-1]
        stats_line = ""
        if subfolders or files:
            _stat_parts = []
            if subfolders:
                _stat_parts.append(f"📁 {len(subfolders)} folder{'s' if len(subfolders) != 1 else ''}")
            if files:
                _stat_parts.append(f"📄 {len(files)} file{'s' if len(files) != 1 else ''}")
            stats_line = "\n" + "\n".join(_stat_parts)
        suffix = "\n\nMultiple files selected for batch operations." if multiselect else "\n\nSelect a folder or file:"
        text = f"📂 <b>{_esc(_display_name)}</b>{stats_line}{suffix}{_MSG_PAD}"

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def show_store_prompt(query, user_id: int) -> int:
    state = user_state.setdefault(user_id, _fresh_state("store"))
    state["mode"] = "store"
    state["view"] = "files"
    state["store_count"] = 0
    state["last_btn_msg"] = None
    path = normalize_path(state.get("path", "Root"))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆ Up", callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    await query.edit_message_text(
        f"📥 <b>Uploading to:</b>\n\n"
        f"📁 {_esc(format_breadcrumb(path))}\n\n"
        "Send one or more files.\n"
        "<i>Captions will be used as filename. Supported: documents, photos, videos, audio, voice messages, stickers.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return WAIT_STORE_FILE


async def show_file_action_panel(query, user_id: int, message_id: int) -> int:
    """Per-file action card: retrieve, rename, move, delete, duplicate, favourite."""
    db = load_db()
    item = db.get(str(message_id))
    if not item:
        await query.answer("⚠️ File not found.", show_alert=True)
        return ConversationHandler.END

    fname = item.get("filename", f"ID {message_id}")
    ftype = item.get("type", "document")
    folder = format_breadcrumb(item.get("folder", "Root"))
    stored = item.get("stored_at", "unknown")
    fav = item.get("favourite", False)
    emoji = file_type_emoji(ftype)
    fav_btn = "⭐ Unfavourite" if fav else "☆ Favourite"
    fav_cb = f"unfav_file:{message_id}" if fav else f"fav_file:{message_id}"
    file_size = item.get("file_size")
    size_line = f"📦 {_fmt_bytes(file_size)}\n" if file_size else ""

    state = user_state.get(user_id, {})
    # If we came from search, back should return to search results
    back_cb = "action:back_search" if state.get("view") == "search" else "action:back_files"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Retrieve", callback_data=f"do_retrieve:{message_id}"),
         InlineKeyboardButton(fav_btn, callback_data=fav_cb)],
        [InlineKeyboardButton("✏️ Rename", callback_data=f"rename_file:{message_id}"),
         InlineKeyboardButton("✏️ Type", callback_data=f"set_type:{message_id}")],
        [InlineKeyboardButton("📋 Copy", callback_data=f"pick_copy_file:{message_id}"),
         InlineKeyboardButton("🔀 Move", callback_data=f"pick_move_file:{message_id}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"del_file:{message_id}")],
        [InlineKeyboardButton("◀ Back", callback_data=back_cb),
         InlineKeyboardButton("🏠 Menu", callback_data="action:menu")],
    ])
    await query.edit_message_text(
        f"{emoji} <b>{_esc(fname)}</b>\n\n"
        f"{size_line}"
        f"📁 {_esc(folder)}\n"
        f"🗓 {_esc(stored[:10] if stored != 'unknown' else stored)}\n"
        f"{'⭐ Favourite' if fav else '☆ Not marked'}\n\n"
        f"<i>Retrieve, move, copy, or delete this file</i>{_MSG_PAD}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


async def show_multi_action_panel(query, user_id: int) -> int:
    """Action card for a multi-selected set of files: retrieve, favourite, copy, delete."""
    state = user_state.setdefault(user_id, _fresh_state())
    db = load_db()
    selected = state.get("selected_files") or set()
    items = [db.get(str(mid)) for mid in selected]
    items = [i for i in items if i]
    if not items:
        await query.answer("⚠️ No files selected.", show_alert=True)
        state["multiselect"] = False
        state["selected_files"] = set()
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    names = "\n".join(
        f"{'⭐' if i.get('favourite') else file_type_emoji(i.get('type', 'other'))} {_esc(i.get('filename', ''))}"
        for i in items[:15]
    )
    if len(items) > 15:
        names += f"\n…and {len(items) - 15} more"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📤 Retrieve all ({len(items)})", callback_data="action:multi_retrieve")],
        [InlineKeyboardButton("⭐ Favourite / ☆ Unfavourite", callback_data="action:multi_fav")],
        [InlineKeyboardButton("📋 Copy", callback_data="action:multi_copy"),
         InlineKeyboardButton("🔀 Move", callback_data="action:multi_move")],
        [InlineKeyboardButton("🗑 Delete", callback_data="action:multi_delete")],
        [InlineKeyboardButton("◀ Back", callback_data="action:multi_back"),
         InlineKeyboardButton("🏠 Menu", callback_data="action:menu")],
    ])
    await query.edit_message_text(
        f"☑️ <b>{len(items)} file(s) selected</b>\n\n<b>Selected:</b>\n{names}\n\n<i>Delete, copy, move, or mark these files</i>{_MSG_PAD}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


async def show_folder_info_panel(query, user_id: int, folder_name: str) -> int:
    """Folder info card: file count, subfolder count, newest file, actions."""
    state = user_state.get(user_id, {})
    # NOTE: the cd: handler already advanced state["path"] to this folder's
    # full path before calling us — do NOT append folder_name again here,
    # or every nested view ends up duplicated (e.g. Root/Videos/Videos).
    full_path = normalize_path(state.get("path", "Root"))
    db = load_db()

    direct_files = len(get_files_in_folder(db, full_path))
    tree_files = count_all_in_tree(db, full_path)
    subfolders = get_subfolders_for_path(db, full_path)
    sub_count = len(subfolders)

    # Newest file in tree
    tree_items = [i for i in db.values()
                  if normalize_path(i.get("folder", "Root")).startswith(full_path)
                  or normalize_path(i.get("folder", "Root")) == full_path]
    tree_items_dated = [i for i in tree_items if i.get("stored_at")]
    newest = max(tree_items_dated, key=lambda x: x["stored_at"])["stored_at"][:10] if tree_items_dated else "—"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Browse folder", callback_data="action:enter_folder_files"),
         InlineKeyboardButton("✏️ Rename", callback_data="action:rename_this_folder")],
        [InlineKeyboardButton("📁 Move folder", callback_data="action:start_move_this_folder"),
         InlineKeyboardButton("🗑 Delete tree", callback_data="action:ask_del_tree")],
        [InlineKeyboardButton("◀ Back", callback_data="action:back_folders"),
         InlineKeyboardButton("🏠 Menu", callback_data="action:menu")],
    ])
    await query.edit_message_text(
        f"📁 <b>{_esc(format_breadcrumb(full_path))}</b>\n\n"
        f"📊 <b>Statistics</b>\n"
        f"  Files here: {direct_files} | Total: {tree_files}\n"
        f"  Subfolders: {sub_count}\n"
        f"  Latest: {newest}\n\n"
        f"<i>View files, explore subfolders, or rename items</i>{_MSG_PAD}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


_STORAGE_TYPE_ORDER = ["video", "image", "document", "audio", "archive", "app", "code", "font", "other", "voice", "sticker", "photo"]


async def show_storage_explorer(query, user_id: int) -> int:
    """Show storage usage breakdown by type and by folder for the current storage path."""
    state = user_state.setdefault(user_id, _fresh_state())
    db = load_db()
    path = normalize_path(state.get("storage_path", "Root"))

    type_sizes = type_size_breakdown_for_path(db, path)
    lines: list[str] = [f"📊 <b>Storage Usage</b>\n📁 {_esc(format_breadcrumb(path))}\n"]

    if type_sizes:
        lines.append("<b>By type:</b>")
        for t in sorted(type_sizes, key=lambda x: -type_sizes[x]):
            sz = type_sizes[t]
            if sz <= 0:
                continue
            lines.append(f"{TYPE_EMOJI.get(t, '📄')} {t.capitalize():<10} {_fmt_bytes(sz)}")
    else:
        lines.append("<i>No sized files here yet.</i>")

    subfolders = get_subfolders_for_path(db, path)
    folder_sizes = []
    for name in subfolders:
        full = normalize_path(f"{path}/{name}")
        folder_sizes.append((name, folder_tree_size(db, full)))
    folder_sizes.sort(key=lambda x: -x[1])

    kb_rows = []
    if folder_sizes:
        lines.append("\n<b>Folders:</b>")
        for name, sz in folder_sizes:
            lines.append(f"📁 {_esc(name):<10} {_fmt_bytes(sz)}")
            kb_rows.append([InlineKeyboardButton(f"📁 {name}  ({_fmt_bytes(sz)})", callback_data=f"storage_cd:{name}")])

    nav_row = []
    if path != "Root":
        nav_row.append(InlineKeyboardButton("⬆ Up", callback_data="storage_up"))
    nav_row.append(InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu"))
    kb_rows.append(nav_row)

    await query.edit_message_text(
        "\n".join(lines) + _MSG_PAD,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )
    return ConversationHandler.END


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id != OWNER_ID:
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return ConversationHandler.END

    data = query.data
    state = user_state.setdefault(user_id, _fresh_state())

    # ── no-op ─────────────────────────────────────────────────────────────────
    if data == "noop":
        return ConversationHandler.END

    # ── sort toggle ───────────────────────────────────────────────────────────
    if data.startswith("sort:"):
        state["sort_order"] = data.split(":", 1)[1]
        state["page"] = 0
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── main menu ─────────────────────────────────────────────────────────────
    if data == "action:menu":
        user_state.pop(user_id, None)
        await query.edit_message_text(
            _home_menu_text(),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    # ── stats (inline) ────────────────────────────────────────────────────────
    if data == "action:stats":
        db = load_db()
        breakdown = db_stats_full(db)
        await query.edit_message_text(
            f"📊 <b>Archive Statistics</b>\n\n{breakdown}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
            ),
        )
        return ConversationHandler.END

    # ── storage explorer (inline) ────────────────────────────────────────────
    if data == "action:storage_explorer":
        state["storage_path"] = "Root"
        return await show_storage_explorer(query, user_id)

    if data.startswith("storage_cd:"):
        name = data.split(":", 1)[1]
        current = normalize_path(state.get("storage_path", "Root"))
        state["storage_path"] = normalize_path(f"{current}/{name}")
        return await show_storage_explorer(query, user_id)

    if data == "storage_up":
        current = normalize_path(state.get("storage_path", "Root"))
        state["storage_path"] = parent_path(current)
        return await show_storage_explorer(query, user_id)

    # ── recent (inline) ───────────────────────────────────────────────────────
    if data == "action:recent":
        db = load_db()
        items = get_recent_files(db, RECENT_N)
        state["mode"] = "retrieve"
        if not items:
            await query.edit_message_text(
                "📋 No files stored yet.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
                ),
            )
            return ConversationHandler.END
        kb_items = [
            (
                f"{file_type_emoji(r.get('type', 'document'))} {r['filename']}\n"
                f"\u2514 📁 {format_breadcrumb(r.get('folder', 'Root'))}",
                f"file_action:{r['message_id']}",
            )
            for r in items
        ]
        kb = build_paginated_keyboard(
            kb_items, 0,
            extra_bottom_rows=[[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]],
        )
        await query.edit_message_text(
            f"📋 <b>Recent files</b> (last {len(items)}):\n\nTap a file for options:",
            parse_mode="HTML", reply_markup=kb,
        )
        return ConversationHandler.END

    # ── favourites (inline) ───────────────────────────────────────────────────
    if data == "action:favourites":
        db = load_db()
        favs = get_favourite_files(db)
        state["mode"] = "retrieve"
        if not favs:
            await query.edit_message_text(
                "⭐ No favourites yet.\n\nOpen a file card and tap ☆ Favourite to star it.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
                ),
            )
            return ConversationHandler.END
        kb_items = [
            (
                f"⭐ {f['filename']}\n\u2514 📁 {format_breadcrumb(f.get('folder', 'Root'))}",
                f"file_action:{f['message_id']}",
            )
            for f in favs
        ]
        kb = build_paginated_keyboard(
            kb_items, 0,
            extra_bottom_rows=[[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]],
        )
        await query.edit_message_text(
            f"⭐ <b>Favourites</b> — {len(favs)} file{'s' if len(favs) != 1 else ''}:\n\nTap a file for options:",
            parse_mode="HTML", reply_markup=kb,
        )
        return ConversationHandler.END

    # ── disk report (inline) ────────────────────────────────────────
    if data == "action:disk":
        try:
            await query.message.delete()
        except Exception:
            pass
        await _send_disk_report(query.message, context)
        return ConversationHandler.END

    # ── search trigger ────────────────────────────────────────────────────────
    if data == "action:search":
        user_state[user_id] = _inherit_state(user_id, "retrieve")
        await query.edit_message_text(
            "🔍 <b>Search</b>\n\nType the filename (or part of it):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:menu")]]
            ),
        )
        return WAIT_SEARCH_INPUT

    # ── mode selection ────────────────────────────────────────────────────────
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        user_state[user_id] = _inherit_state(user_id, mode)
        if mode == "retrieve":
            await show_combined_view(query, user_id)
        else:
            await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── pagination ────────────────────────────────────────────────────────────
    if data.startswith("page:"):
        state["page"] = int(data.split(":", 1)[1])
        if state.get("view") == "files":
            await show_combined_view(query, user_id)
        elif state.get("view") == "search":
            items = state.get("search_items") or []
            kb = build_paginated_keyboard(
                items, state["page"],
                extra_bottom_rows=[
                    [InlineKeyboardButton("🔍 Search again", callback_data="action:search")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
                ],
            )
            await query.edit_message_text(
                f"🔍 <b>Search results</b> — page {state['page'] + 1}\n\nTap a file for options:",
                parse_mode="HTML", reply_markup=kb,
            )
        else:
            await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── up ────────────────────────────────────────────────────────────────────
    if data == "action:up":
        state["path"] = parent_path(normalize_path(state.get("path", "Root")))
        state["page"] = 0
        state["view"] = "folders"
        if state["mode"] == "store":
            await show_folder_list(query, user_id)
            return WAIT_STORE_FILE
        if state["mode"] == "move_file":
            await show_folder_list(query, user_id)
            return WAIT_MOVE_FILE_DST
        if state["mode"] == "move_folder":
            await show_folder_list(query, user_id)
            return WAIT_MOVE_DEST
        if state["mode"] == "copy_file":
            await show_folder_list(query, user_id)
            return WAIT_COPY_DST
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── navigate into folder (cd:) ────────────────────────────────────────────
    if data.startswith("cd:"):
        folder_name = data[3:]
        current = normalize_path(state.get("path", "Root"))
        new_path = normalize_path(f"{current}/{folder_name}")
        state["path"] = new_path
        state["page"] = 0
        mode = state["mode"]

        if mode == "store":
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return WAIT_STORE_FILE

        if mode == "delete":
            db = load_db()
            files_here = len(get_files_in_folder(db, new_path))
            subfolders_here = len(get_subfolders_for_path(db, new_path))
            total_tree = count_all_in_tree(db, new_path)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📂 Browse ({files_here} files · {subfolders_here} subfolders)",
                    callback_data="action:open_del_folder")],
                [InlineKeyboardButton(
                    f"🗑 Delete all ({total_tree} total)",
                    callback_data="action:ask_del_tree")],
                [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
            ])
            await query.edit_message_text(
                f"🗑 <b>{_esc(format_breadcrumb(new_path))}</b>\n"
                f"Files here: {files_here}  ·  Subfolders: {subfolders_here}  ·  Total in tree: {total_tree}\n\n"
                "What would you like to do?",
                parse_mode="HTML", reply_markup=kb,
            )
            return ConversationHandler.END

        if mode == "rename":
            # drill down — keep showing folder list so user can rename deeper folders
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return ConversationHandler.END

        if mode in ("move_file", "move_folder", "copy_file"):
            # Navigate further — always show folder list
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            if mode == "move_file":
                return WAIT_MOVE_FILE_DST
            if mode == "copy_file":
                return WAIT_COPY_DST
            return WAIT_MOVE_DEST

        # retrieve — show folder info panel first
        return await show_folder_info_panel(query, user_id, folder_name)

    # ── enter folder directly into file view (from folder info panel) ─────────
    if data == "action:enter_folder_files":
        state["page"] = 0
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── view files ────────────────────────────────────────────────────────────
    if data == "action:view_files":
        state["view"] = "files"
        state["page"] = 0
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── store here ────────────────────────────────────────────────────────────
    if data == "action:store_here":
        return await show_store_prompt(query, user_id)

    # ── new folder ────────────────────────────────────────────────────────────
    if data == "action:new_folder":
        await query.edit_message_text(
            f"📁 Creating inside <b>{_esc(format_breadcrumb(state.get('path', 'Root')))}</b>\n\n"
            "Type the new folder name:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")]]
            ),
        )
        return WAIT_NEW_FOLDER

    # ── back to folder list ───────────────────────────────────────────────────
    if data == "action:back_folders":
        state["page"] = 0
        # Go up to parent folder
        current = normalize_path(state.get("path", "Root"))
        if "/" in current:
            state["path"] = current.rsplit("/", 1)[0]
        else:
            state["path"] = "Root"
        if state["mode"] == "store":
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return WAIT_STORE_FILE
        if state["mode"] == "move_file":
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return WAIT_MOVE_FILE_DST
        if state["mode"] == "move_folder":
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return WAIT_MOVE_DEST
        if state["mode"] == "copy_file":
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return WAIT_COPY_DST
        # For rename — return to folder list (rename navigates via show_folder_list)
        if state["mode"] == "rename":
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return ConversationHandler.END
        # For retrieve and delete — show combined view of the parent folder
        # (consistent with how entering a folder via "Browse folder" works)
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── back to file list ─────────────────────────────────────────────────────
    if data == "action:back_files":
        state["page"] = 0
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── go to the folder a file was just copied/moved into ───────────────────
    if data == "action:goto_pasted":
        state["path"] = state.get("last_paste_dest", "Root")
        state["page"] = 0
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── go back to source folder parent (after copy/move) ────────────────────
    if data == "action:back_source":
        state["page"] = 0
        state["view"] = "files"
        # state["path"] is already set to parent_path(src_folder_path)
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── back to search results ────────────────────────────────────────────────
    if data == "action:back_search":
        items = state.get("search_items") or []
        page = state.get("page", 0)
        state["view"] = "search"
        if not items:
            await query.edit_message_text(
                "🔍 Search session expired. Run a new search.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Search", callback_data="action:search")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="action:menu")],
                ]),
            )
            return ConversationHandler.END
        kb = build_paginated_keyboard(
            items, page,
            extra_bottom_rows=[
                [InlineKeyboardButton("🔍 Search again", callback_data="action:search")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
            ],
        )
        await query.edit_message_text(
            "🔍 <b>Search results</b>\n\nTap a file for options:",
            parse_mode="HTML", reply_markup=kb,
        )
        return ConversationHandler.END

    # ── file action panel ─────────────────────────────────────────────────────
    if data.startswith("file_action:"):
        msg_id = int(data.split(":", 1)[1])
        return await show_file_action_panel(query, user_id, msg_id)

    # ── multiselect: enter / exit ─────────────────────────────────────────────
    if data == "action:multiselect_on":
        state["multiselect"] = True
        state["selected_files"] = set()
        state["page"] = 0
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    if data == "action:multiselect_off":
        state["multiselect"] = False
        state["selected_files"] = set()
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── multiselect: toggle a file ────────────────────────────────────────────
    if data.startswith("toggle_sel:"):
        msg_id = int(data.split(":", 1)[1])
        selected = state.setdefault("selected_files", set())
        if msg_id in selected:
            selected.discard(msg_id)
        else:
            selected.add(msg_id)
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── multiselect: done → action panel ──────────────────────────────────────
    if data == "action:multi_done":
        if not (state.get("selected_files") or set()):
            await query.answer("⚠️ No files selected.", show_alert=True)
            return ConversationHandler.END
        return await show_multi_action_panel(query, user_id)

    # ── multiselect: back to folder (keep selection) ──────────────────────────
    if data == "action:multi_back":
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── multiselect: retrieve all ─────────────────────────────────────────────
    if data == "action:multi_retrieve":
        selected = state.get("selected_files") or set()
        sent = 0
        for msg_id in selected:
            try:
                await context.bot.copy_message(
                    chat_id=query.message.chat.id,
                    from_chat_id=ARCHIVE_CHAT_ID,
                    message_id=msg_id,
                )
                sent += 1
            except Exception:
                pass
        await query.answer(f"📤 Sent {sent} file(s).", show_alert=False)
        return await show_multi_action_panel(query, user_id)

    # ── multiselect: favourite / unfavourite all ──────────────────────────────
    if data == "action:multi_fav":
        selected = state.get("selected_files") or set()
        db = load_db()
        items = [db.get(str(mid)) for mid in selected]
        items = [i for i in items if i]
        all_fav = all(i.get("favourite") for i in items) if items else False
        new_val = not all_fav
        for i in items:
            i["favourite"] = new_val
        save_db(db)
        await query.answer("⭐ Marked as favourite." if new_val else "☆ Removed from favourites.", show_alert=False)
        return await show_multi_action_panel(query, user_id)

    # ── multiselect: delete all (confirm) ─────────────────────────────────────
    if data == "action:multi_delete":
        selected = state.get("selected_files") or set()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Yes, delete {len(selected)} file(s)", callback_data="action:multi_delete_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="action:multi_done")],
        ])
        await query.edit_message_text(
            f"🗑 Delete <b>{len(selected)}</b> selected file(s)?\n\nThis cannot be undone.",
            parse_mode="HTML", reply_markup=kb,
        )
        return ConversationHandler.END

    if data == "action:multi_delete_confirm":
        selected = state.get("selected_files") or set()
        db = load_db()
        deleted = 0
        for msg_id in list(selected):
            item = db.pop(str(msg_id), None)
            if item:
                deleted += 1
                try:
                    await context.bot.delete_message(ARCHIVE_CHAT_ID, msg_id)
                except Exception:
                    pass
        save_db(db)
        await query.answer(f"✅ Deleted {deleted} file(s).", show_alert=False)
        state["multiselect"] = False
        state["selected_files"] = set()
        state["page"] = 0
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── multiselect: copy all → pick destination ──────────────────────────────
    if data == "action:multi_copy":
        selected = state.get("selected_files") or set()
        if not selected:
            await query.answer("⚠️ No files selected.", show_alert=True)
            return ConversationHandler.END
        state["copy_sources"] = list(selected)
        state["copy_source"] = None
        state["mode"] = "copy_file"
        state["path"] = "Root"
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return WAIT_COPY_DST

    # ── multiselect: move all → pick destination ─────────────────────────────
    if data == "action:multi_move":
        selected = state.get("selected_files") or set()
        if not selected:
            await query.answer("⚠️ No files selected.", show_alert=True)
            return ConversationHandler.END
        state["move_targets"] = list(selected)
        state["move_target"] = None
        state["mode"] = "move_file"
        state["path"] = "Root"
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return WAIT_MOVE_FILE_DST

    # ── retrieve file ─────────────────────────────────────────────────────────
    if data.startswith("do_retrieve:"):
        msg_id = int(data.split(":", 1)[1])
        try:
            await context.bot.copy_message(
                chat_id=query.message.chat.id,
                from_chat_id=ARCHIVE_CHAT_ID,
                message_id=msg_id,
            )
            await query.answer("✅ File sent!", show_alert=False)
        except Exception as e:
            await query.message.reply_text(
                f"⚠️ Could not retrieve file: {_esc(str(e))}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
                ),
            )
        return ConversationHandler.END

    # ── favourite / unfavourite ───────────────────────────────────────────────
    if data.startswith("fav_file:") or data.startswith("unfav_file:"):
        action, _, mid = data.partition(":")
        msg_id = int(mid)
        db = load_db()
        item = db.get(str(msg_id))
        if item:
            item["favourite"] = (action == "fav_file")
            save_db(db)
            await query.answer("⭐ Added to favourites!" if item["favourite"] else "Removed from favourites.",
                               show_alert=False)
        return await show_file_action_panel(query, user_id, msg_id)

    # ── delete flow ───────────────────────────────────────────────────────────
    if data == "action:open_del_folder":
        state["page"] = 0
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    if data in ("action:ask_del_tree", "action:delete_this_folder"):
        path = normalize_path(state.get("path", "Root"))
        db = load_db()
        total = count_all_in_tree(db, path)
        warn = "\n\n⚠️ This folder contains a lot of files!" if total >= 20 else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Yes, delete everything", callback_data="action:confirm_del_tree")],
            [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
        ])
        await query.edit_message_text(
            f"⚠️ <b>WARNING</b>\n\n"
            f"Folder: <b>{_esc(format_breadcrumb(path))}</b>\n"
            f"Total files to delete: <b>{total}</b>{_esc(warn)}\n\n"
            "This <b>cannot</b> be undone.",
            parse_mode="HTML", reply_markup=kb,
        )
        return ConversationHandler.END

    if data == "action:confirm_del_tree":
        path = normalize_path(state.get("path", "Root"))
        db = load_db()
        keys = delete_folder_tree(db, path)
        save_db(db)
        for k in keys:
            try:
                await context.bot.delete_message(ARCHIVE_CHAT_ID, int(k))
            except Exception:
                pass
        deleted_label = format_breadcrumb(path)
        state["path"] = parent_path(path)
        state["page"] = 0
        state["view"] = "folders"
        await query.answer(f"✅ '{deleted_label}' deleted ({len(keys)} files).", show_alert=True)
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    if data.startswith("del_file:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        item = db.get(str(msg_id))
        fname = item["filename"] if item else f"ID {msg_id}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_del_file:{msg_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="action:back_files")],
        ])
        await query.edit_message_text(
            f"🗑 Delete <b>{_esc(fname)}</b>?\n\nThis cannot be undone.",
            parse_mode="HTML", reply_markup=kb,
        )
        return ConversationHandler.END

    if data.startswith("confirm_del_file:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        item = db.pop(str(msg_id), None)
        save_db(db)
        fname = item["filename"] if item else f"ID {msg_id}"
        try:
            await context.bot.delete_message(ARCHIVE_CHAT_ID, msg_id)
        except Exception:
            pass
        await query.answer(f"✅ '{fname}' deleted.", show_alert=False)
        state["page"] = 0
        state["view"] = "files"
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── rename file ───────────────────────────────────────────────────────────
    if data.startswith("rename_file:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        item = db.get(str(msg_id))
        if not item:
            await query.answer("File not found.", show_alert=True)
            return ConversationHandler.END
        state["rename_target"] = msg_id
        state["rename_folder_path"] = None
        await query.edit_message_text(
            f"✏️ <b>Rename File</b>\n\nCurrent name: <code>{_esc(item['filename'])}</code>\n\nSend the new name:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_files")]]
            ),
        )
        return WAIT_RENAME_INPUT

    # ── rename this folder (current) ─────────────────────────────────────────
    if data == "action:rename_this_folder":
        path = normalize_path(state.get("path", "Root"))
        if path == "Root":
            await query.answer("⚠️ Cannot rename Root.", show_alert=True)
            return ConversationHandler.END
        folder_name = path.rsplit("/", 1)[-1]
        state["rename_folder_path"] = path
        state["rename_target"] = None
        await query.edit_message_text(
            f"✏️ <b>Rename Folder</b>\n\n"
            f"Current name: <code>{_esc(folder_name)}</code>\n"
            f"Full path: <code>{_esc(format_breadcrumb(path))}</code>\n\n"
            "Send the new folder name:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")]]
            ),
        )
        return WAIT_RENAME_FOLDER

    # ── rename a subfolder from the folder info panel ─────────────────────────
    if data.startswith("rename_folder_item:"):
        folder_name = data[len("rename_folder_item:"):]
        current = normalize_path(state.get("path", "Root"))
        full_path = normalize_path(f"{current}/{folder_name}")
        state["rename_folder_path"] = full_path
        state["rename_target"] = None
        await query.edit_message_text(
            f"✏️ <b>Rename Folder</b>\n\n"
            f"Current name: <code>{_esc(folder_name)}</code>\n"
            f"Full path: <code>{_esc(format_breadcrumb(full_path))}</code>\n\n"
            "Send the new folder name:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")]]
            ),
        )
        return WAIT_RENAME_FOLDER

    # ── move file: pick a file ────────────────────────────────────────────────
    if data.startswith("pick_move_file:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        item = db.get(str(msg_id))
        if not item:
            await query.answer("⚠️ File not found.", show_alert=True)
            return ConversationHandler.END
        fname = item.get("filename", f"ID {msg_id}")
        src_folder = normalize_path(item.get("folder", "Root"))
        state["move_target"] = msg_id
        state["move_targets"] = None
        state["mode"] = "move_file"
        state["path"] = parent_path(src_folder)
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return WAIT_MOVE_FILE_DST

    # ── move file: drop here ──────────────────────────────────────────────────
    if data == "action:move_here":
        move_targets = state.get("move_targets")
        dest_path = normalize_path(state.get("path", "Root"))

        if move_targets:
            db = load_db()
            moved = 0
            for msg_id in move_targets:
                item = db.get(str(msg_id))
                if not item:
                    continue
                item["folder"] = dest_path
                moved += 1
            save_db(db)
            state["move_targets"] = None
            state["move_target"] = None
            state["mode"] = "retrieve"
            state["multiselect"] = False
            state["selected_files"] = set()
            # For multi-move: return to destination (all files are there now)
            state["path"] = dest_path
            state["last_paste_dest"] = dest_path
            state["page"] = 0
            state["view"] = "files"
            await query.answer(f"✅ Moved {moved} file(s)!", show_alert=False)
            await show_combined_view(query, user_id)
            return ConversationHandler.END

        move_target = state.get("move_target")
        if not move_target:
            await query.answer("⚠️ No file selected.", show_alert=True)
            return ConversationHandler.END
        dest_path = normalize_path(state.get("path", "Root"))
        db = load_db()
        item = db.get(str(move_target))
        if not item:
            await query.answer("⚠️ File no longer exists.", show_alert=True)
            state["move_target"] = None
            return ConversationHandler.END
        old_folder_path = normalize_path(item.get("folder", "Root"))
        old_folder = format_breadcrumb(old_folder_path)
        fname = item.get("filename", f"ID {move_target}")
        item["folder"] = dest_path
        save_db(db)
        state["move_target"] = None
        state["mode"] = "retrieve"
        state["path"] = parent_path(old_folder_path)
        state["last_paste_dest"] = dest_path
        state["page"] = 0
        state["view"] = "files"
        await query.answer("✅ Moved!", show_alert=False)
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    # ── move folder: start (from folder info panel) ──────────────────────────
    if data == "action:start_move_this_folder":
        full_path = normalize_path(state.get("path", "Root"))
        if full_path == "Root":
            await query.answer("⚠️ Cannot move Root.", show_alert=True)
            return ConversationHandler.END
        state["move_folder_path"] = full_path
        state["mode"] = "move_folder"
        state["path"] = "Root"
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return WAIT_MOVE_DEST

    # ── move folder: drop here ────────────────────────────────────────────────
    if data == "action:move_folder_here":
        mfp = state.get("move_folder_path")
        if not mfp:
            await query.answer("⚠️ No folder selected.", show_alert=True)
            return ConversationHandler.END
        new_parent = normalize_path(state.get("path", "Root"))
        mfp_norm = normalize_path(mfp)
        # Guard: cannot move into itself or a child
        if new_parent == mfp_norm or new_parent.startswith(mfp_norm + "/"):
            await query.answer("⚠️ Cannot move a folder into itself.", show_alert=True)
            return ConversationHandler.END
        db = load_db()
        old_display = format_breadcrumb(mfp_norm)
        folder_name_only = mfp_norm.rsplit("/", 1)[-1]
        new_full = normalize_path(f"{new_parent}/{folder_name_only}")
        # Collision check
        siblings = get_subfolders_for_path(db, new_parent)
        if folder_name_only in siblings:
            await query.answer(f"⚠️ A folder named '{folder_name_only}' already exists there.", show_alert=True)
            return ConversationHandler.END
        updated, new_full = move_folder_in_db(db, mfp_norm, new_parent)
        save_db(db)
        state["move_folder_path"] = None
        state["mode"] = "retrieve"
        state["path"] = new_full
        await query.answer("✅ Folder moved!", show_alert=False)
        await query.edit_message_text(
            f"✅ <b>Folder moved</b>\n\n"
            f"From: {_esc(old_display)}\n"
            f"To:   {_esc(format_breadcrumb(new_full))}\n"
            f"<i>{updated} file record(s) updated.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
            ),
        )
        return ConversationHandler.END

    # ── copy file: pick destination ───────────────────────────────────────────
    if data.startswith("pick_copy_file:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        item = db.get(str(msg_id))
        if not item:
            await query.answer("⚠️ File not found.", show_alert=True)
            return ConversationHandler.END
        src_folder = normalize_path(item.get("folder", "Root"))
        state["copy_source"] = msg_id
        state["copy_sources"] = None
        state["mode"] = "copy_file"
        state["path"] = parent_path(src_folder)
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return WAIT_COPY_DST

    # ── copy file: drop here ──────────────────────────────────────────────────
    if data == "action:copy_here":
        dest_path = normalize_path(state.get("path", "Root"))
        copy_sources = state.get("copy_sources")

        if copy_sources:
            db = load_db()
            results = []
            for src_id in copy_sources:
                src_item = db.get(str(src_id))
                if not src_item:
                    continue
                try:
                    copied = await context.bot.copy_message(
                        chat_id=ARCHIVE_CHAT_ID,
                        from_chat_id=ARCHIVE_CHAT_ID,
                        message_id=src_id,
                    )
                except Exception:
                    continue
                orig_name = src_item.get("filename", f"file_{src_id}")
                file_type = src_item.get("type", "other")
                new_name = _unique_copy_name(db, orig_name, file_type, dest_path)
                db[str(copied.message_id)] = {
                    "filename": new_name,
                    "folder": dest_path,
                    "message_id": copied.message_id,
                    "type": file_type,
                    "file_size": src_item.get("file_size"),
                    "stored_at": _now_iso(),
                    "favourite": False,
                }
                results.append((orig_name, new_name))
            save_db(db)
            state["copy_sources"] = None
            state["copy_source"] = None
            state["mode"] = "retrieve"
            state["multiselect"] = False
            state["selected_files"] = set()
            # For multi-copy: return to destination (all copies are there now)
            state["path"] = dest_path
            state["last_paste_dest"] = dest_path
            state["page"] = 0
            state["view"] = "files"
            await query.answer(f"📋 Copied {len(results)} file(s)!", show_alert=False)
            await show_combined_view(query, user_id)
            return ConversationHandler.END

        src_id = state.get("copy_source")
        if not src_id:
            await query.answer("⚠️ No file selected.", show_alert=True)
            return ConversationHandler.END
        db = load_db()
        src_item = db.get(str(src_id))
        if not src_item:
            await query.answer("⚠️ File no longer exists.", show_alert=True)
            state["copy_source"] = None
            return ConversationHandler.END
        try:
            copied = await context.bot.copy_message(
                chat_id=ARCHIVE_CHAT_ID,
                from_chat_id=ARCHIVE_CHAT_ID,
                message_id=src_id,
            )
        except Exception as e:
            await query.answer(f"⚠️ Copy failed: {e}", show_alert=True)
            return ConversationHandler.END
        orig_name = src_item.get("filename", f"file_{src_id}")
        file_type = src_item.get("type", "other")
        src_folder_path = normalize_path(src_item.get("folder", "Root"))
        # Always check for a name collision in the destination folder,
        # whether it's the source folder or a different one.
        new_name = _unique_copy_name(db, orig_name, file_type, dest_path)
        db[str(copied.message_id)] = {
            "filename": new_name,
            "folder": dest_path,
            "message_id": copied.message_id,
            "type": file_type,
            "file_size": src_item.get("file_size"),
            "stored_at": _now_iso(),
            "favourite": False,
        }
        save_db(db)
        state["copy_source"] = None
        state["mode"] = "retrieve"
        state["path"] = parent_path(src_folder_path)
        state["last_paste_dest"] = dest_path
        state["page"] = 0
        state["view"] = "files"
        await query.answer("📋 Copied!", show_alert=False)
        await show_combined_view(query, user_id)
        return ConversationHandler.END

    if data.startswith("set_type:"):
        msg_id = int(data.split(":", 1)[1])
        type_buttons = [
            [InlineKeyboardButton(f"{TYPE_EMOJI.get(t, '📄')} {t.capitalize()}", callback_data=f"confirm_set_type:{msg_id}:{t}")]
            for t in ("video", "audio", "image", "document", "archive", "app", "code", "font")
        ]
        type_buttons.append([InlineKeyboardButton("◀ Back", callback_data=f"file_action:{msg_id}")])
        await query.edit_message_text(
            f"✏️ <b>Set File Type</b>\n\nSelect the correct category for this file:\n\n<i>This helps organize and sort your files</i>{_MSG_PAD}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(type_buttons),
        )
        return ConversationHandler.END

    if data.startswith("confirm_set_type:"):
        _, msg_id_str, new_type = data.split(":", 2)
        msg_id = int(msg_id_str)
        db = load_db()
        item = db.get(str(msg_id))
        if not item:
            await query.answer("⚠️ File not found.", show_alert=True)
            return ConversationHandler.END
        item["type"] = new_type
        save_db(db)
        await query.answer(f"✅ Type set to {new_type}", show_alert=False)
        await show_file_action_panel(query, update.effective_user.id, msg_id)
        return ConversationHandler.END

    return ConversationHandler.END

async def receive_new_folder_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    uid = update.effective_user.id
    folder_name = update.message.text.strip()
    if not folder_name:
        await update.message.reply_text("Folder name cannot be empty. Try again:")
        return WAIT_NEW_FOLDER
    if "/" in folder_name:
        await update.message.reply_text("Folder name cannot contain '/'. Try again:")
        return WAIT_NEW_FOLDER
    if len(folder_name) > 64:
        await update.message.reply_text("Folder name too long (max 64 chars). Try again:")
        return WAIT_NEW_FOLDER
    state = user_state.setdefault(uid, _fresh_state("store"))
    current = normalize_path(state.get("path", "Root"))
    new_path = normalize_path(f"{current}/{folder_name}")
    state.update({"path": new_path, "page": 0, "view": "folders", "store_count": 0, "last_btn_msg": None})
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Store here", callback_data="action:store_here")],
        [InlineKeyboardButton("➕ New Subfolder", callback_data="action:new_folder")],
        [InlineKeyboardButton("⬆ Up", callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    await update.message.reply_text(
        f"✅ Folder <b>{_esc(format_breadcrumb(new_path))}</b> created.\n\nStore here, add subfolders, or go up:",
        parse_mode="HTML", reply_markup=kb,
    )
    return WAIT_STORE_FILE


# ---------------------------------------------------------------------------
# Conversation: rename file
# ---------------------------------------------------------------------------

async def receive_rename_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    uid = update.effective_user.id
    new_name = update.message.text.strip()
    state = user_state.get(uid, {})
    msg_id = state.get("rename_target")
    if not new_name:
        await update.message.reply_text("Name cannot be empty. Try again:")
        return WAIT_RENAME_INPUT
    if len(new_name) > 200:
        await update.message.reply_text("Name too long (max 200 chars). Try again:")
        return WAIT_RENAME_INPUT
    if msg_id is None:
        await update.message.reply_text("⚠️ No file selected. Returning to menu.")
        return ConversationHandler.END
    db = load_db()
    item = db.get(str(msg_id))
    if not item:
        await update.message.reply_text("⚠️ File not found.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    old_name = item["filename"]
    item["filename"] = new_name
    save_db(db)
    state["rename_target"] = None
    back_cb = "action:back_search" if state.get("view") == "search" else "action:back_files"
    await update.message.reply_text(
        f"✅ <b>File renamed</b>\n\n"
        f"Old: <code>{_esc(old_name)}</code>\n"
        f"New: <code>{_esc(new_name)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Back", callback_data=back_cb)],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
        ]),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation: rename folder
# ---------------------------------------------------------------------------

async def receive_rename_folder_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    uid = update.effective_user.id
    new_name = update.message.text.strip()
    state = user_state.get(uid, {})
    old_path = state.get("rename_folder_path")
    if not new_name:
        await update.message.reply_text("Folder name cannot be empty. Try again:")
        return WAIT_RENAME_FOLDER
    if "/" in new_name:
        await update.message.reply_text("Folder name cannot contain '/'. Try again:")
        return WAIT_RENAME_FOLDER
    if len(new_name) > 64:
        await update.message.reply_text("Folder name too long (max 64 chars). Try again:")
        return WAIT_RENAME_FOLDER
    if not old_path:
        await update.message.reply_text("⚠️ No folder selected. Returning to menu.")
        return ConversationHandler.END
    if old_path == "Root":
        await update.message.reply_text("⚠️ Cannot rename Root.")
        return ConversationHandler.END
    parent = parent_path(old_path)
    new_path = normalize_path(f"{parent}/{new_name}")
    db = load_db()
    # Collision check
    siblings = get_subfolders_for_path(db, parent)
    old_name = old_path.rsplit("/", 1)[-1]
    if new_name in siblings and new_name != old_name:
        await update.message.reply_text(
            f"⚠️ A folder named <b>{_esc(new_name)}</b> already exists here. Choose a different name:",
            parse_mode="HTML",
        )
        return WAIT_RENAME_FOLDER
    old_display = format_breadcrumb(old_path)
    updated = rename_folder_in_db(db, old_path, new_path)
    save_db(db)
    state["rename_folder_path"] = None
    state["path"] = new_path
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Open renamed folder", callback_data="action:view_files")],
        [InlineKeyboardButton("⬆ Up", callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    await update.message.reply_text(
        f"✅ <b>Folder renamed</b>\n\n"
        f"Old: <code>{_esc(old_display)}</code>\n"
        f"New: <code>{_esc(format_breadcrumb(new_path))}</code>\n\n"
        f"<i>{updated} file{'s' if updated != 1 else ''} updated.</i>",
        parse_mode="HTML", reply_markup=kb,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation: store file
# ---------------------------------------------------------------------------

_STORE_FILTER = (
                        filters.Document.ALL | filters.PHOTO | filters.VIDEO |
                        filters.AUDIO | filters.VOICE | filters.Sticker.ALL
                ) & ~filters.COMMAND


async def receive_store_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        await deny(update);
        return ConversationHandler.END
    uid = update.effective_user.id
    message = update.message
    state = user_state.get(uid, {})
    folder_path = normalize_path(state.get("path", "Root"))

    has_media = (message.document or message.photo or message.video or
                 message.audio or message.voice or message.sticker)
    if not has_media:
        await message.reply_text("⚠️ Please send a file (document, photo, video, audio, voice, or sticker).")
        return WAIT_STORE_FILE

    try:
        copied = await context.bot.copy_message(
            chat_id=ARCHIVE_CHAT_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        await message.reply_text(f"⚠️ Failed to store file: {e}\n\nTry again or /cancel to abort.")
        return WAIT_STORE_FILE

    file_size: int | None = None
    if message.document:
        filename = resolve_filename(message, f"file_{copied.message_id}")
        file_size = message.document.file_size
        # Extension first, magic bytes only if no extension
        file_type = "other"
        ext_type = classify_by_extension(filename)
        if ext_type not in ("other", "document"):
            file_type = ext_type
        else:
            try:
                import magic as _magic
                import httpx
                tg_file = await context.bot.get_file(message.document.file_id)
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        tg_file.file_path,
                        headers={"Range": "bytes=0-1023"},
                        timeout=10,
                    )
                mime = _magic.from_buffer(bytes(resp.content), mime=True)
                detected = _mime_to_type(mime)
                if detected:
                    file_type = detected
            except Exception:
                pass  # stay as "document"
    elif message.photo:
        file_type, filename = "photo", resolve_filename(message, f"photo_{copied.message_id}.jpg")
        file_size = message.photo[-1].file_size if message.photo else None
    elif message.video:
        file_type, filename = "video", resolve_filename(message, f"video_{copied.message_id}.mp4")
        file_size = message.video.file_size
    elif message.audio:
        file_type, filename = "audio", resolve_filename(message, f"audio_{copied.message_id}")
        file_size = message.audio.file_size
    elif message.voice:
        file_type, filename = "voice", resolve_filename(message, f"voice_{copied.message_id}.ogg")
        file_size = message.voice.file_size
    elif message.sticker:
        file_type = "sticker"
        emoji = message.sticker.emoji or ""
        filename = resolve_filename(message, f"sticker_{copied.message_id}{emoji}")
        file_size = message.sticker.file_size
    else:
        return WAIT_STORE_FILE

    # Strip extension from display name — type is already detected above
    filename = Path(filename).stem or filename

    db = load_db()
    record: dict = {
        "filename": filename,
        "folder": folder_path,
        "message_id": copied.message_id,
        "type": file_type,
        "stored_at": _now_iso(),
        "favourite": False,
    }
    if file_size is not None:
        record["file_size"] = file_size
    db[str(copied.message_id)] = record
    save_db(db)

    state["store_count"] = state.get("store_count", 0) + 1
    count = state["store_count"]
    await retire_last_btn_msg(context, message.chat.id, state, count, folder_path)

    emoji = file_type_emoji(file_type)
    await message.reply_text(
        f"{emoji} <code>{_esc(filename)}</code> → <b>{_esc(format_breadcrumb(folder_path))}</b>",
        parse_mode="HTML",
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Store another  ({count} stored)", callback_data="action:store_here")],
        [InlineKeyboardButton("⬆ Up", callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    btn_msg = await message.reply_text(
        f"📁 <b>{_esc(format_breadcrumb(folder_path))}</b>\n\nSend more files or choose an action:",
        parse_mode="HTML", reply_markup=kb,
    )
    state["last_btn_msg"] = btn_msg.message_id
    return WAIT_STORE_FILE


# ---------------------------------------------------------------------------
# Move destination text fallback (user types while bot awaits folder nav)
# ---------------------------------------------------------------------------

async def move_text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Use the folder navigation buttons above to choose the destination.\n/cancel to abort."
    )
    uid = update.effective_user.id
    mode = user_state.get(uid, {}).get("mode", "")
    return WAIT_MOVE_FILE_DST if mode == "move_file" else WAIT_MOVE_DEST


# ---------------------------------------------------------------------------
# /backup  /restore
# ---------------------------------------------------------------------------

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    if not DB_FILE.exists():
        await update.message.reply_text("⚠️ No database file found.")
        return
    db = load_db()
    total_files, total_folders = len(db), sum(
        1 for _ in {normalize_path(i.get("folder", "Root")) for i in db.values()}
    )
    caption = (
        f"🗄 <b>Archive backup</b>\n"
        f"<code>{DB_FILE.name}</code> — {total_files} files, {total_folders} folders\n"
        f"<i>{_now_iso()}</i>"
    )
    with open(DB_FILE, "rb") as f:
        await update.message.reply_document(
            document=f, filename=DB_FILE.name,
            caption=caption, parse_mode="HTML",
        )


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    replied = update.message.reply_to_message
    if not replied or not replied.document:
        await update.message.reply_text(
            "ℹ️ <b>How to restore:</b>\n\n"
            "1. Use /backup to download your current database.\n"
            "2. Send the <code>.json</code> file back to this chat.\n"
            "3. <b>Reply</b> to that file message with <code>/restore</code>.",
            parse_mode="HTML",
        )
        return
    doc = replied.document
    if not (doc.file_name or "").endswith(".json"):
        await update.message.reply_text("⚠️ The replied-to file must be a <code>.json</code> file.", parse_mode="HTML")
        return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("⚠️ File too large (max 5 MB).")
        return
    status = await update.message.reply_text("⏳ Downloading and validating…")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw_bytes = await tg_file.download_as_bytearray()
        new_data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        await status.edit_text(f"⚠️ Could not parse the file: {_esc(str(e))}", parse_mode="HTML")
        return
    if not isinstance(new_data, dict):
        await status.edit_text("⚠️ Invalid format — expected a JSON object at the top level.")
        return
    _rotate_backups()
    DB_TMP_FILE.write_text(json.dumps(new_data, indent=2, ensure_ascii=False), encoding="utf-8")
    DB_TMP_FILE.replace(DB_FILE)
    total_files, total_folders = len(new_data), sum(
        1 for _ in {normalize_path(i.get("folder", "Root")) for i in new_data.values()}
    )
    await status.edit_text(
        f"✅ <b>Database restored</b>\n\n"
        f"Records loaded: <b>{total_files}</b> files across <b>{total_folders}</b> folders.\n"
        f"Previous DB saved as <code>{_backup_path(1).name}</code>.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# MIME type → our type vocabulary
# ---------------------------------------------------------------------------

_MIME_TO_TYPE: dict[str, str] = {
    # video
    "video/mp4": "video", "video/x-matroska": "video", "video/quicktime": "video",
    "video/x-msvideo": "video", "video/webm": "video", "video/x-flv": "video",
    "video/3gpp": "video", "video/mpeg": "video", "video/x-ms-wmv": "video",
    # audio
    "audio/mpeg": "audio", "audio/mp4": "audio", "audio/flac": "audio",
    "audio/x-wav": "audio", "audio/wav": "audio", "audio/ogg": "audio",
    "audio/opus": "audio", "audio/aac": "audio", "audio/x-ms-wma": "audio",
    "audio/x-aiff": "audio", "audio/x-ape": "audio", "audio/midi": "audio",
    # images
    "image/jpeg": "image", "image/png": "image", "image/gif": "image",
    "image/webp": "image", "image/bmp": "image", "image/heic": "image",
    "image/heif": "image", "image/tiff": "image", "image/svg+xml": "image",
    "image/x-icon": "image", "image/avif": "image",
    # documents
    "application/pdf": "document",
    "application/msword": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.ms-excel": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "text/plain": "document", "text/csv": "document",
    "application/epub+zip": "document",
    "application/vnd.oasis.opendocument.text": "document",
    "application/vnd.oasis.opendocument.spreadsheet": "document",
    "application/vnd.oasis.opendocument.presentation": "document",
    # archives
    "application/zip": "archive", "application/x-rar-compressed": "archive",
    "application/x-7z-compressed": "archive", "application/x-tar": "archive",
    "application/gzip": "archive", "application/x-bzip2": "archive",
    "application/x-xz": "archive", "application/x-iso9660-image": "archive",
    "application/x-zstd": "archive",
    # apps
    "application/vnd.android.package-archive": "app",
    "application/x-apple-diskimage": "app",
    "application/x-msdownload": "app", "application/x-msi": "app",
    "application/x-debian-package": "app", "application/x-rpm": "app",
    "application/x-executable": "app", "application/x-elf": "app",
    # code / data
    "application/json": "code", "application/xml": "code",
    "text/xml": "code", "text/html": "code", "text/css": "code",
    "text/javascript": "code", "application/javascript": "code",
    "application/x-python-code": "code", "text/x-python": "code",
    "application/x-sh": "code", "text/x-shellscript": "code",
    "application/x-sqlite3": "code",
    # fonts
    "font/ttf": "font", "font/otf": "font", "font/woff": "font",
    "font/woff2": "font", "application/font-woff": "font",
}


def _mime_to_type(mime: str) -> str | None:
    """Map a MIME string to our type vocabulary. Returns None if unrecognised."""
    mime = mime.lower().split(";")[0].strip()
    if mime in _MIME_TO_TYPE:
        return _MIME_TO_TYPE[mime]
    # broad fallbacks
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/"):
        return "document"
    if mime.startswith("font/"):
        return "font"
    return None


# ---------------------------------------------------------------------------
# Helpers shared by disk_command
# ---------------------------------------------------------------------------

def _fmt_bytes(n) -> str:
    """Human-readable bytes string."""
    if n is None:
        return "unknown"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _fmt_uptime(seconds: int) -> str:
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt_ago(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# ---------------------------------------------------------------------------
# /disk  — PNG archive & disk report
# ---------------------------------------------------------------------------

async def disk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    await _send_disk_report(update.message, context)


async def _send_disk_report(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    import shutil, time, io
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        await message.reply_text(
            "\u26a0\ufe0f Pillow is not installed. Add <code>Pillow</code> to requirements.txt.",
            parse_mode="HTML",
        )
        return

    status = await message.reply_text("\u23f3 Generating disk report\u2026")

    bot = context.bot
    db = load_db()

    # 1. Fetch missing file sizes by forwarding archive messages
    missing = [k for k, v in db.items() if "file_size" not in v]
    newly_fetched = 0
    if missing:
        for key in missing:
            item = db[key]
            try:
                msg_id = int(item["message_id"])
                tg_msg = await bot.forward_message(
                    chat_id=OWNER_ID,
                    from_chat_id=ARCHIVE_CHAT_ID,
                    message_id=msg_id,
                    disable_notification=True,
                )
                fsize = None
                for attr in ("document", "video", "audio", "voice", "sticker"):
                    obj = getattr(tg_msg, attr, None)
                    if obj and getattr(obj, "file_size", None):
                        fsize = obj.file_size
                        break
                if fsize is None and tg_msg.photo:
                    fsize = tg_msg.photo[-1].file_size
                try:
                    await bot.delete_message(chat_id=OWNER_ID, message_id=tg_msg.message_id)
                except Exception:
                    pass
                if fsize is not None:
                    item["file_size"] = fsize
                    newly_fetched += 1
            except Exception:
                pass
        if newly_fetched:
            save_db(db)

    # 2. Collect stats
    import os as _os

    vol_dir = str(_DB_DIR.resolve())
    try:
        total_disk, used_disk, free_disk = shutil.disk_usage(vol_dir)
    except Exception:
        total_disk = used_disk = free_disk = 0

    try:
        import time as _time
        uptime_str = _fmt_uptime(int(_time.time()) - _BOT_START_TIME)
    except Exception:
        uptime_str = "n/a"

    try:
        import psutil
        ram_bytes = psutil.Process().memory_info().rss
        ram_str = _fmt_bytes(ram_bytes)
    except Exception:
        ram_str = "n/a"

    all_sizes = [v.get("file_size") for v in db.values()]
    known_sizes = [s for s in all_sizes if s is not None]
    unknown_cnt = len(all_sizes) - len(known_sizes)
    total_tg = sum(known_sizes)

    type_counts: dict[str, int] = {}
    type_sizes: dict[str, int] = {}
    for v in db.values():
        t = effective_type(v)
        type_counts[t] = type_counts.get(t, 0) + 1
        if v.get("file_size"):
            type_sizes[t] = type_sizes.get(t, 0) + v["file_size"]

    top5 = sorted(
        [v for v in db.values() if v.get("file_size")],
        key=lambda x: x["file_size"],
        reverse=True,
    )[:5]

    dated = [v for v in db.values() if v.get("stored_at")]
    oldest = min(dated, key=lambda x: x["stored_at"], default=None)
    newest = max(dated, key=lambda x: x["stored_at"], default=None)

    meta_files = [DB_FILE, DB_TMP_FILE] + [_backup_path(i) for i in range(1, BACKUP_COUNT + 1)]
    meta_info = [(p, p.stat().st_size if p.exists() else None) for p in meta_files]
    last_save = DB_FILE.stat().st_mtime if DB_FILE.exists() else None
    last_save_str = _fmt_ago(int(time.time() - last_save)) if last_save else "n/a"

    backup_health = []
    for i in range(1, BACKUP_COUNT + 1):
        bp = _backup_path(i)
        if not bp.exists():
            backup_health.append("missing")
        elif _try_parse(bp) is None:
            backup_health.append("corrupt")
        else:
            backup_health.append("ok")

    db_path_env = _os.environ.get("DB_PATH")

    # 3. Draw the PNG
    SCALE = 2  # render at 2x for crisp quality
    W = 520 * SCALE
    BG = (15, 17, 23)
    CARD = (22, 27, 38)
    ACCENT = (99, 102, 241)
    TEAL = (34, 211, 238)
    AMBER = (251, 191, 36)
    GREEN = (34, 197, 94)
    RED = (239, 68, 68)
    TEXT1 = (226, 232, 240)
    TEXT2 = (148, 163, 184)
    TEXT3 = (75, 85, 99)
    DIVIDER = (31, 35, 51)
    PAD = 14 * SCALE
    GAP = 8 * SCALE
    R = 8 * SCALE

    def load_font(size: int, bold: bool = False):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf".format("-Bold" if bold else ""),
            "/usr/share/fonts/truetype/liberation/LiberationSans-{}.ttf".format("Bold" if bold else "Regular"),
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size * SCALE)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size * SCALE)
        except TypeError:
            return ImageFont.load_default()

    F10 = load_font(10)
    F11 = load_font(11)
    F12 = load_font(12)
    F14 = load_font(14, bold=True)
    F20 = load_font(20, bold=True)

    half_types = (len(type_counts) + 1) // 2
    SECTION_HEIGHTS = {
        "header": 54 * SCALE,
        "stat_row": 70 * SCALE,
        "disk_bar": 58 * SCALE,
        "types": (36 + 20 * (half_types + 1)) * SCALE,
        "top5": ((30 + 19 * len(top5)) * SCALE if top5 else 0),
        "dates": 64 * SCALE,
        "meta": (28 + 19 * len([p for p, s in meta_info if s is not None])) * SCALE,
        "backups": 80 * SCALE,
        "footer": 28 * SCALE,
    }
    active = {k: v for k, v in SECTION_HEIGHTS.items() if v > 0}
    H = PAD + sum(active.values()) + GAP * (len(active) - 1) + PAD

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    def u(n):
        return n * SCALE

    def rrect(x1, y1, x2, y2, r=R, fill=CARD):
        draw.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=fill)

    def label(x, y, text, color=TEXT3, font=F10):
        draw.text((x, y), text.upper(), font=font, fill=color)

    cy = PAD

    # Header
    sh = active["header"]
    rrect(PAD, cy, W - PAD, cy + sh)
    draw.text((PAD + u(14), cy + u(10)), "Disk Report", font=F20, fill=TEXT1)
    draw.text((PAD + u(14), cy + u(34)), vol_dir, font=F10, fill=TEXT3)
    pill_text = f"up {uptime_str}"
    pw = int(draw.textlength(pill_text, font=F11)) + u(16)
    rrect(W - PAD - pw - u(4), cy + u(12), W - PAD - u(4), cy + u(32), r=u(10), fill=(30, 27, 60))
    draw.text((W - PAD - pw + u(4), cy + u(16)), pill_text, font=F11, fill=(167, 139, 250))
    cy += sh + GAP

    # 3 stat cards
    sh = active["stat_row"]
    cw = (W - PAD * 2 - GAP * 2) // 3
    tg_label = _fmt_bytes(total_tg) + (f" +{unknown_cnt} unk." if unknown_cnt else "")
    stats = [
        ("Volume used", _fmt_bytes(used_disk), f"of {_fmt_bytes(total_disk)}"),
        ("TG file size", tg_label, f"{len(db)} files"),
        ("RAM usage", ram_str, "bot process"),
    ]
    for i, (lbl, val, sub) in enumerate(stats):
        x = PAD + i * (cw + GAP)
        rrect(x, cy, x + cw, cy + sh)
        label(x + u(10), cy + u(9), lbl)
        draw.text((x + u(10), cy + u(24)), val, font=F14, fill=TEXT1)
        draw.text((x + u(10), cy + u(46)), sub, font=F10, fill=TEXT3)
    cy += sh + GAP

    # Disk bar
    sh = active["disk_bar"]
    rrect(PAD, cy, W - PAD, cy + sh)
    pct = (used_disk / total_disk * 100) if total_disk else 0
    bar_color = GREEN if pct < 70 else (AMBER if pct < 90 else RED)
    label(PAD + u(12), cy + u(9), f"disk usage  -  {pct:.0f}% full  -  {_fmt_bytes(free_disk)} free")
    bx, by = PAD + u(12), cy + u(26)
    bw, bh = W - PAD * 2 - u(24), u(8)
    rrect(bx, by, bx + bw, by + bh, r=u(4), fill=DIVIDER)
    fill_w = max(u(4), int(bw * pct / 100))
    rrect(bx, by, bx + fill_w, by + bh, r=u(4), fill=bar_color)
    draw.text((bx, by + u(14)), "0", font=F10, fill=TEXT3)
    draw.text((bx + bw // 2 - u(10), by + u(14)), _fmt_bytes(total_disk // 2), font=F10, fill=TEXT3)
    draw.text((bx + bw - u(32), by + u(14)), _fmt_bytes(total_disk), font=F10, fill=TEXT3)
    cy += sh + GAP

    # Type breakdown
    sh = active["types"]
    rrect(PAD, cy, W - PAD, cy + sh)
    label(PAD + u(12), cy + u(9), f"file type breakdown  -  {len(db)} total")
    DOT_COLORS = [ACCENT, TEAL, AMBER, GREEN, (236, 72, 153), (251, 146, 60)]
    type_order = sorted(type_counts.items(), key=lambda x: -type_sizes.get(x[0], 0))
    total_files = len(db) or 1
    total_known_size = sum(type_sizes.values()) or 1
    seg_x = PAD + u(12)
    bar_w = W - PAD * 2 - u(24)
    for idx2, (t, cnt) in enumerate(type_order):
        seg_w = max(u(2), int(bar_w * type_sizes.get(t, 0) / total_known_size))
        rrect(seg_x, cy + u(24), seg_x + seg_w, cy + u(30), r=u(3), fill=DOT_COLORS[idx2 % len(DOT_COLORS)])
        seg_x += seg_w
    ry = cy + u(38)
    half = len(type_order) // 2 + len(type_order) % 2
    for idx2, (t, cnt) in enumerate(type_order):
        col_x = PAD + u(12) if idx2 < half else W // 2 + u(10)
        row_y = ry + (idx2 % half) * u(20)
        dc = DOT_COLORS[idx2 % len(DOT_COLORS)]
        draw.ellipse([col_x, row_y + u(4), col_x + u(7), row_y + u(11)], fill=dc)
        emoji = TYPE_EMOJI.get(t, "")
        draw.text((col_x + u(14), row_y), f"{t.capitalize()}", font=F11, fill=TEXT2)
        sz = type_sizes.get(t)
        size_str = f"  {_fmt_bytes(sz)}" if sz else ""
        pct_str = f"{sz / total_known_size * 100:.0f}%" if sz else f"{cnt / total_files * 100:.0f}%"
        draw.text((col_x + u(14) + u(72), row_y), f"{cnt} ({pct_str}){size_str}", font=F11, fill=TEXT1)
    cy += sh + GAP

    # Top 5 largest
    if top5:
        sh = active["top5"]
        rrect(PAD, cy, W - PAD, cy + sh)
        label(PAD + u(12), cy + u(9), "top 5 largest files")
        max_sz = top5[0]["file_size"] or 1
        for idx2, item in enumerate(top5):
            fy = cy + u(26) + idx2 * u(19)
            fname = item.get("filename", "?")
            fname = fname if len(fname) <= 42 else fname[:41] + "\u2026"
            sz_str = _fmt_bytes(item.get("file_size", 0))
            draw.text((PAD + u(12), fy), fname, font=F10, fill=TEXT2)
            draw.text((W - PAD - u(12) - int(draw.textlength(sz_str, font=F10)), fy), sz_str, font=F10, fill=ACCENT)
        cy += sh + GAP

    # Oldest / newest
    sh = active["dates"]
    rrect(PAD, cy, W - PAD, cy + sh)
    half_w = (W - PAD * 2 - GAP) // 2
    for side, item in enumerate([oldest, newest]):
        lbl_text = "oldest file" if side == 0 else "newest file"
        x = PAD if side == 0 else PAD + half_w + GAP
        label(x + u(10), cy + u(9), lbl_text)
        if item:
            draw.text((x + u(10), cy + u(24)), item.get("stored_at", "")[:10], font=F12, fill=TEXT1)
            fn = item.get("filename", "?")
            fn = fn if len(fn) <= 28 else fn[:27] + "\u2026"
            draw.text((x + u(10), cy + u(42)), fn, font=F10, fill=TEXT3)
        else:
            draw.text((x + u(10), cy + u(24)), "n/a", font=F12, fill=TEXT3)
    cy += sh + GAP

    # Metadata files
    visible_meta = [(p, s) for p, s in meta_info if s is not None]
    sh = active["meta"]
    rrect(PAD, cy, W - PAD, cy + sh)
    label(PAD + u(12), cy + u(9), f"metadata files  -  last save {last_save_str}")
    my = cy + u(26)
    for p, s in visible_meta:
        is_live = (p == DB_FILE)
        name_color = TEXT1 if is_live else TEXT3
        sz_str = _fmt_bytes(s)
        draw.text((PAD + u(12), my), p.name, font=F11, fill=name_color)
        draw.text((W - PAD - u(12) - int(draw.textlength(sz_str, font=F11)), my), sz_str,
                  font=F11, fill=ACCENT if is_live else TEXT3)
        my += u(19)
    cy += sh + GAP

    # Backup health
    sh = active["backups"]
    rrect(PAD, cy, W - PAD, cy + sh)
    label(PAD + u(12), cy + u(9), "backup health")
    ok_count = backup_health.count("ok")
    pill_w = (W - PAD * 2 - u(24) - GAP * (BACKUP_COUNT - 1)) // BACKUP_COUNT
    for i, bstatus in enumerate(backup_health):
        bx2 = PAD + u(12) + i * (pill_w + GAP)
        bc = GREEN if bstatus == "ok" else (RED if bstatus == "corrupt" else TEXT3)
        bg2 = (20, 30, 20) if bstatus == "ok" else (30, 20, 20)
        rrect(bx2, cy + u(34), bx2 + pill_w, cy + u(54), r=u(6), fill=bg2)
        dot_x = bx2 + pill_w // 2 - u(4)
        draw.ellipse([dot_x, cy + u(40), dot_x + u(8), cy + u(48)], fill=bc)
        draw.text((bx2 + pill_w // 2 - u(5), cy + u(22)), f"B.{i + 1}", font=F10, fill=TEXT3)
    issues = [f"backup.{i + 1} {backup_health[i]}" for i in range(BACKUP_COUNT) if backup_health[i] != "ok"]
    summary = f"{ok_count} / {BACKUP_COUNT} healthy"
    if issues:
        summary += "  �  " + ",  ".join(issues)
    draw.text((PAD + u(12), cy + u(58)), summary, font=F10, fill=TEXT3)
    cy += sh + GAP

    # Footer
    sh = active["footer"]
    db_ok = db_path_env is not None
    db_text = f"DB_PATH  {db_path_env}  OK" if db_ok else "DB_PATH not set  !!  metadata may not be on volume"
    draw.text((PAD + u(4), cy + u(10)), db_text, font=F10, fill=GREEN if db_ok else RED)
    ts = _now_iso()[:19].replace("T", "  ")
    draw.text((W - PAD - int(draw.textlength(ts, font=F10)) - u(4), cy + u(10)), ts, font=F10, fill=TEXT3)

    # Send
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)

    caption_parts = [f"<b>Disk Report</b>  \u2014  {_now_iso()[:16].replace('T', ' ')} IST"]
    if newly_fetched:
        caption_parts.append(f"<i>Cached sizes for {newly_fetched} file(s) this run.</i>")
    if unknown_cnt:
        caption_parts.append(
            f"<i>{unknown_cnt} file(s) have no cached size. "
            f"For files &gt;20\u202fMB edit metadata.json manually.</i>"
        )

    try:
        await status.delete()
    except Exception:
        pass

    await message.reply_photo(
        photo=buf,
        caption="\n".join(caption_parts),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /joinme + archive group protection
# ---------------------------------------------------------------------------

async def joinme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    bot = context.bot
    try:
        chat = await bot.get_chat(ARCHIVE_CHAT_ID)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Cannot access archive group: {_esc(str(e))}", parse_mode="HTML")
        return
    invite_link = None
    try:
        invite = await bot.create_chat_invite_link(ARCHIVE_CHAT_ID, creates_join_request=False, member_limit=1)
        invite_link = invite.invite_link
    except Exception:
        invite_link = getattr(chat, "invite_link", None)
    if not invite_link:
        await update.message.reply_text("⚠️ Could not obtain an invite link.")
        return
    await update.message.reply_text(
        f"🔐 <b>Archive Group Invite</b>\n\n{invite_link}\n\n"
        "Single-use link. After joining, you will be auto-promoted to admin.\n"
        "<i>Anyone else who joins will be kicked instantly.</i>",
        parse_mode="HTML",
    )
    try:
        member = await bot.get_chat_member(ARCHIVE_CHAT_ID, OWNER_ID)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
            await _promote_owner(bot, ARCHIVE_CHAT_ID)
    except Exception:
        pass


async def _promote_owner(bot, chat_id: int) -> None:
    try:
        await bot.promote_chat_member(
            chat_id=chat_id, user_id=OWNER_ID,
            can_manage_chat=True, can_change_info=True, can_post_messages=True,
            can_edit_messages=True, can_delete_messages=True, can_invite_users=True,
            can_restrict_members=True, can_pin_messages=True,
            can_promote_members=True, can_manage_video_chats=True,
        )
    except Exception:
        pass


async def protect_archive_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event: ChatMemberUpdated = update.chat_member
    if event.chat.id != ARCHIVE_CHAT_ID:
        return
    new = event.new_chat_member
    old = event.old_chat_member
    user = new.user
    bot = context.bot
    joined_statuses = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
    was_outside = old.status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
    is_now_inside = new.status in joined_statuses
    if not (was_outside and is_now_inside):
        return
    if user.is_bot:
        return
    if user.id == OWNER_ID:
        await _promote_owner(bot, ARCHIVE_CHAT_ID)
        return
    try:
        await bot.ban_chat_member(ARCHIVE_CHAT_ID, user.id)
    except Exception:
        pass
    try:
        await bot.unban_chat_member(ARCHIVE_CHAT_ID, user.id)
    except Exception:
        pass
    try:
        name = user.full_name
        username = f" (@{user.username})" if user.username else ""
        await bot.send_message(
            OWNER_ID,
            f"🚨 <b>Intruder alert</b>\n\n"
            f"<b>{_esc(name)}</b>{_esc(username)} (ID: <code>{user.id}</code>) "
            f"tried to join the archive group and was kicked.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

app = Application.builder().token(BOT_TOKEN).post_init(set_commands).build()

conv = ConversationHandler(
    entry_points=[
        CommandHandler("start", start),
        CommandHandler("menu", menu_command),
        CommandHandler("recent", recent_command),
        CommandHandler("search", search_command),
        CallbackQueryHandler(button_handler),
    ],
    states={
        WAIT_NEW_FOLDER: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_folder_name),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_STORE_FILE: [
            MessageHandler(_STORE_FILTER, receive_store_file),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_RENAME_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rename_input),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_RENAME_FOLDER: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rename_folder_input),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_SEARCH_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_query),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_MOVE_FILE_DST: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, move_text_fallback),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_MOVE_DEST: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, move_text_fallback),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_COPY_DST: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, move_text_fallback),
            CallbackQueryHandler(button_handler),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CommandHandler("menu", menu_command),
        CommandHandler("start", start),
    ],
    per_message=False,
    allow_reentry=True,
)

app.add_handler(conv)
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("stats", stats_command))
app.add_handler(CommandHandler("joinme", joinme_command))
app.add_handler(CommandHandler("backup", backup_command))
app.add_handler(CommandHandler("restore", restore_command))
app.add_handler(CommandHandler("disk", disk_command))
app.add_handler(ChatMemberHandler(protect_archive_group, ChatMemberHandler.CHAT_MEMBER))

if __name__ == "__main__":
    print("Bot running…")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])