from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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

# DB_PATH env var lets you point the database at a persistent Volume on Railway.
# Default falls back to the current directory so local dev works without changes.
_DB_PATH     = Path(os.environ.get("DB_PATH", "metadata.json"))
_DB_DIR      = _DB_PATH.parent
DB_FILE      = _DB_PATH
DB_TMP_FILE  = _DB_DIR / "metadata.tmp.json"   # atomic-write staging file
BACKUP_COUNT = 5                                # rolling backups kept on disk
PAGE_SIZE    = 10

# Ensure the directory exists (important on first deploy when Volume is empty)
_DB_DIR.mkdir(parents=True, exist_ok=True)

# ConversationHandler states
WAIT_NEW_FOLDER      = 1
WAIT_STORE_FILE      = 2
WAIT_RENAME_INPUT    = 3
WAIT_SEARCH_INPUT    = 4
WAIT_RENAME_FOLDER   = 5
WAIT_MOVE_FOLDER     = 6   # picking destination for folder move

# ---------------------------------------------------------------------------
# In-memory user state
# ---------------------------------------------------------------------------
# user_state[user_id] = {
#   "mode":              "store" | "retrieve" | "delete" | "move" | "rename" | "copy"
#   "path":              str  — current folder path e.g. "Root/Videos/Edits"
#   "view":              "folders" | "files" | "search"
#   "page":              int
#   "last_btn_msg":      int | None  — message_id of last summary-with-buttons
#   "store_count":       int  — files stored this session
#   "rename_target":     int | None  — message_id of file being renamed
#   "rename_folder_path":str | None  — full path of folder being renamed
#   "move_target":       int | None  — message_id of file being moved
#   "move_folder_path":  str | None  — full path of folder being moved
#   "copy_target":       int | None  — message_id of file being copied
#   "sort_key":          "date" | "name" | "type"  — file sort order
#   "search_items":      list | None — last search results (for back nav)
# }
user_state: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# DB persistence layer  (atomic writes + rolling backups + startup check)
# ---------------------------------------------------------------------------

def _backup_path(n: int) -> Path:
    """Place backups beside the live DB: /data/metadata.backup.1.json … .5.json"""
    return _DB_DIR / f"metadata.backup.{n}.json"


def _rotate_backups() -> None:
    """Shift backups down: .4→.5, .3→.4, …, live→.1"""
    for i in range(BACKUP_COUNT - 1, 0, -1):
        src = _backup_path(i)
        dst = _backup_path(i + 1)
        if src.exists():
            src.replace(dst)
    if DB_FILE.exists():
        import shutil
        shutil.copy2(DB_FILE, _backup_path(1))


def _try_parse(path: Path) -> dict | None:
    """Return parsed dict if file exists and is valid JSON, else None."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _startup_check() -> None:
    """
    Called once at import time.
    • If DB_FILE is missing or corrupt, try backups 1→5 in order.
    • Logs a warning to stdout; never raises.
    """
    if _try_parse(DB_FILE) is not None:
        return  # all good

    print(f"⚠️  {DB_FILE} is missing or corrupt — attempting recovery…")
    for i in range(1, BACKUP_COUNT + 1):
        bp = _backup_path(i)
        data = _try_parse(bp)
        if data is not None:
            print(f"✅  Recovered from {bp}  ({len(data)} records)")
            DB_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return
        if bp.exists():
            print(f"   {bp} — also corrupt, skipping")

    print("⚠️  No valid backup found — starting with an empty database.")
    DB_FILE.write_text("{}", encoding="utf-8")


# Run the startup check immediately (before the bot starts taking updates)
_startup_check()


def load_db() -> dict:
    """Load DB from disk; fall back to empty dict on any error."""
    data = _try_parse(DB_FILE)
    if data is None:
        # Live file just went bad mid-run — try backups
        for i in range(1, BACKUP_COUNT + 1):
            data = _try_parse(_backup_path(i))
            if data is not None:
                return data
        return {}
    return data


def save_db(data: dict) -> None:
    """
    Atomically write *data* to DB_FILE.
    1. Serialise to a temp file in the same directory.
    2. Rotate backups.
    3. Replace the live file with the temp file (atomic on POSIX; best-effort on Windows).
    """
    text = json.dumps(data, indent=2, ensure_ascii=False)
    DB_TMP_FILE.write_text(text, encoding="utf-8")
    _rotate_backups()
    DB_TMP_FILE.replace(DB_FILE)   # atomic rename on Linux/macOS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if rest:
            children.add(rest.split("/", 1)[0])
    return sorted(children)


def get_files_in_folder(db: dict, folder_path: str) -> list[dict]:
    folder_path = normalize_path(folder_path)
    return [
        item
        for item in db.values()
        if normalize_path(item.get("folder", "Root")) == folder_path
    ]


def count_all_in_tree(db: dict, folder_path: str) -> int:
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    return sum(
        1
        for item in db.values()
        if normalize_path(item.get("folder", "Root")) == folder_path
        or normalize_path(item.get("folder", "Root")).startswith(prefix)
    )


def delete_folder_tree(db: dict, folder_path: str) -> list[str]:
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    keys = [
        k
        for k, item in db.items()
        if normalize_path(item.get("folder", "Root")) == folder_path
        or normalize_path(item.get("folder", "Root")).startswith(prefix)
    ]
    for k in keys:
        del db[k]
    return keys


def rename_folder_in_db(db: dict, old_path: str, new_path: str) -> int:
    """Rewrite every file's folder that lives under old_path to new_path.
    Returns number of records updated."""
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


def move_file_in_db(db: dict, message_id: int, new_folder: str) -> bool:
    """Move a single file to a different folder. Returns True if found."""
    key = str(message_id)
    if key not in db:
        return False
    db[key]["folder"] = normalize_path(new_folder)
    return True


def move_folder_in_db(db: dict, old_path: str, new_parent: str) -> tuple[int, str]:
    """Move *old_path* folder (and entire subtree) under *new_parent*.
    Returns (records_updated, new_full_path)."""
    old_path   = normalize_path(old_path)
    new_parent = normalize_path(new_parent)
    folder_name = old_path.rsplit("/", 1)[-1]
    new_path    = normalize_path(f"{new_parent}/{folder_name}")
    updated = rename_folder_in_db(db, old_path, new_path)
    return updated, new_path


def copy_file_in_db(db: dict, message_id: int, new_folder: str) -> dict | None:
    """Return a shallow copy of the file record pointing to new_folder, or None."""
    key = str(message_id)
    item = db.get(key)
    if not item:
        return None
    return {**item, "folder": normalize_path(new_folder)}


def toggle_favourite(db: dict, message_id: int) -> bool:
    """Toggle the 'favourite' flag on a file. Returns new flag state."""
    key = str(message_id)
    if key not in db:
        return False
    db[key]["favourite"] = not db[key].get("favourite", False)
    return db[key]["favourite"]


def get_favourites(db: dict) -> list[dict]:
    return [item for item in db.values() if item.get("favourite")]


def get_recent_files(db: dict, n: int = 15) -> list[dict]:
    """Return the *n* most recently stored files."""
    return sorted(
        db.values(),
        key=lambda x: x.get("stored_at", ""),
        reverse=True,
    )[:n]


def search_files(db: dict, query: str) -> list[dict]:
    query = query.strip().lower()
    return [
        item
        for item in db.values()
        if query in item.get("filename", "").lower()
    ]


def format_breadcrumb(path: str) -> str:
    return normalize_path(path).replace("/", " › ")


def parent_path(path: str) -> str:
    path = normalize_path(path)
    if path == "Root":
        return "Root"
    return normalize_path(path.rsplit("/", 1)[0])


def resolve_filename(message: Message, fallback: str) -> str:
    """Best-effort filename: caption > file_name attr > fallback."""
    caption = (message.caption or "").strip()
    if caption:
        return caption
    for attr in ("document", "video", "audio"):
        obj = getattr(message, attr, None)
        if obj and getattr(obj, "file_name", None):
            return obj.file_name
    return fallback


def db_stats(db: dict) -> tuple[int, int]:
    """Return (total_files, total_folders)."""
    folders: set[str] = set()
    for item in db.values():
        p = normalize_path(item.get("folder", "Root"))
        parts = p.split("/")
        for i in range(len(parts)):
            folders.add("/".join(parts[: i + 1]))
    return len(db), max(0, len(folders) - 1)  # exclude Root itself


def file_type_emoji(file_type: str) -> str:
    return {
        "document": "📄",
        "photo":    "🖼",
        "video":    "🎬",
        "audio":    "🎵",
        "voice":    "🎙",
        "sticker":  "🎴",
    }.get(file_type, "📄")


# ---------------------------------------------------------------------------
# Auth helpers
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
# Button-message tracking
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
                f"📁 *{format_breadcrumb(folder_path)}*\n"
                f"_{count - 1} file{'s' if count - 1 != 1 else ''} stored so far — "
                "keep sending…_"
            ),
            parse_mode="Markdown",
            reply_markup=None,
        )
    except Exception:
        pass
    state["last_btn_msg"] = None


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def build_paginated_keyboard(
    items: list[tuple[str, str]],
    page: int,
    extra_top_rows: list | None = None,
    extra_bottom_rows: list | None = None,
) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    keyboard = list(extra_top_rows or [])
    chunk = items[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]
    for label, cb in chunk:
        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"page:{page - 1}"))
    if total_pages > 1:
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{total_pages}", callback_data="noop"
            )
        )
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"page:{page + 1}"))
    if nav:
        keyboard.append(nav)

    for row in extra_bottom_rows or []:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 Store",      callback_data="mode:store"),
                InlineKeyboardButton("📤 Retrieve",   callback_data="mode:retrieve"),
            ],
            [
                InlineKeyboardButton("🗑 Delete",     callback_data="mode:delete"),
                InlineKeyboardButton("✏️ Rename",     callback_data="mode:rename"),
            ],
            [
                InlineKeyboardButton("🔀 Move",       callback_data="mode:move"),
                InlineKeyboardButton("📋 Copy",       callback_data="mode:copy"),
            ],
            [
                InlineKeyboardButton("🔍 Search",     callback_data="action:search"),
                InlineKeyboardButton("⭐ Favourites", callback_data="action:favourites"),
            ],
            [
                InlineKeyboardButton("🕓 Recent",     callback_data="action:recent"),
                InlineKeyboardButton("📊 Stats",      callback_data="action:stats"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start",      "Open archive menu"),
            BotCommand("menu",       "Return to main menu"),
            BotCommand("cancel",     "Cancel current action"),
            BotCommand("stats",      "Show archive statistics"),
            BotCommand("search",     "Search files by name"),
            BotCommand("recent",     "Show recently stored files"),
            BotCommand("favourites", "Show starred/favourite files"),
            BotCommand("help",       "Show help & command list"),
            BotCommand("joinme",     "Get invite link & become admin in archive group"),
            BotCommand("backup",     "Download metadata.json as a file"),
            BotCommand("restore",    "Reply to a .json file to restore the database"),
        ]
    )


# ---------------------------------------------------------------------------
# /start  /menu  /help  /stats  /search  /cancel
# ---------------------------------------------------------------------------

def _fresh_state(mode: str = "retrieve") -> dict:
    return {
        "mode":               mode,
        "path":               "Root",
        "view":               "folders",
        "page":               0,
        "last_btn_msg":       None,
        "store_count":        0,
        "rename_target":      None,
        "rename_folder_path": None,
        "move_target":        None,
        "move_folder_path":   None,
        "copy_target":        None,
        "sort_key":           "date",
        "search_items":       None,
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        return await deny(update)
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "📦 *Archive Bot*\n\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        return await deny(update)
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "📦 *Archive Bot*\n\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    await update.message.reply_text(
        "📖 *Archive Bot — Help*\n\n"
        "*Commands:*\n"
        "/start — open the main menu\n"
        "/menu — return to main menu from anywhere\n"
        "/cancel — cancel the current action\n"
        "/stats — show total files & folder count\n"
        "/search — search files by name\n"
        "/recent — show the 15 most recently stored files\n"
        "/favourites — show all starred files\n"
        "/joinme — get archive group invite & become admin\n"
        "/backup — download metadata.json (your archive index)\n"
        "/restore — reply to a .json file to restore the database\n"
        "/help — this message\n\n"
        "*Modes:*\n"
        "📥 *Store* — navigate/create folders, then send files.\n"
        "  Caption becomes the filename. Batch-send supported.\n"
        "📤 *Retrieve* — browse and tap a file to have it sent.\n"
        "  Tap any file for a full action panel (retrieve/rename/move/copy/star).\n"
        "🗑 *Delete* — remove individual files or whole folder trees.\n"
        "✏️ *Rename* — rename any file or subfolder.\n"
        "🔀 *Move* — move a file or an entire folder to a new location.\n"
        "📋 *Copy* — duplicate a file into another folder.\n"
        "🔍 *Search* — find files by name across all folders.\n"
        "⭐ *Favourites* — star files for quick access; tap ⭐ on any file.\n"
        "🕓 *Recent* — view the 15 most recently stored files.\n"
        "📊 *Stats* — per-folder file counts.\n\n"
        "*Sorting (in any file list):*\n"
        "Tap 🔃 to cycle sort order: Date ↓ → Name A-Z → Type.\n\n"
        "*Data safety:*\n"
        "• Saves are atomic — a crash mid-write won't corrupt the DB.\n"
        "• Last 5 saves are kept as rolling backups automatically.\n"
        "• Use /backup regularly to keep an off-device copy.\n"
        "• Use /restore to reload from any backup file.\n\n"
        "*Archive group security:*\n"
        "The archive group auto-kicks any intruder.\n"
        "Use /joinme for a fresh single-use invite link.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
        ),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    db = load_db()
    total_files, total_folders = db_stats(db)

    top_folders = get_subfolders_for_path(db, "Root")
    lines = []
    for f in top_folders[:15]:
        n = count_all_in_tree(db, f"Root/{f}")
        lines.append(f"  📁 {f}: {n} file{'s' if n != 1 else ''}")
    breakdown = "\n".join(lines) if lines else "  (empty)"

    root_files = len(get_files_in_folder(db, "Root"))
    if root_files:
        breakdown = f"  📂 Root: {root_files} file{'s' if root_files != 1 else ''}\n" + breakdown

    await update.message.reply_text(
        f"📊 *Archive Statistics*\n\n"
        f"Total files: *{total_files}*\n"
        f"Total folders: *{total_folders}*\n\n"
        f"*Top-level breakdown:*\n{breakdown}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
        ),
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        return await deny(update)
    uid = update.effective_user.id
    user_state[uid] = _fresh_state()
    await update.message.reply_text(
        "🔍 *Search*\n\nType the filename (or part of it) to search for:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="action:menu")]]
        ),
    )
    return WAIT_SEARCH_INPUT


async def receive_search_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        return await deny(update)
    uid = update.effective_user.id
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("Please type something to search for.")
        return WAIT_SEARCH_INPUT

    db = load_db()
    results = search_files(db, query_text)
    state = user_state.setdefault(uid, _fresh_state())
    state["mode"] = "retrieve"
    state["view"] = "search"

    if not results:
        await update.message.reply_text(
            f"🔍 No files found matching *{query_text}*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔍 Search again", callback_data="action:search")],
                    [InlineKeyboardButton("🏠 Main Menu",    callback_data="action:menu")],
                ]
            ),
        )
        return ConversationHandler.END

    items = [
        (
            f"{file_type_emoji(r.get('type','document'))} {r['filename']}  [{format_breadcrumb(r.get('folder','Root'))}]",
            f"get_file:{r['message_id']}",
        )
        for r in results
    ]
    state["search_items"] = items
    state["page"] = 0
    kb = build_paginated_keyboard(
        items,
        0,
        extra_bottom_rows=[
            [InlineKeyboardButton("🔍 Search again", callback_data="action:search")],
            [InlineKeyboardButton("🏠 Main Menu",    callback_data="action:menu")],
        ],
    )
    await update.message.reply_text(
        f"🔍 *{len(results)} result{'s' if len(results) != 1 else ''}* for _{query_text}_\n\n"
        "Tap a file to retrieve it:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        return await deny(update)
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=main_menu_keyboard(),
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

    # In rename mode, subfolder buttons trigger rename; in move-folder mode,
    # they navigate (destination picking); everything else navigates.
    if mode == "rename":
        items = [(folder_label(name), f"rename_folder:{name}") for name in children]
    else:
        items = [(folder_label(name), f"cd:{name}") for name in children]

    top: list[list[InlineKeyboardButton]] = []

    if mode == "store":
        top.append([InlineKeyboardButton("📥 Store here",     callback_data="action:store_here")])
        top.append([InlineKeyboardButton("➕ New Folder",     callback_data="action:new_folder")])
    elif mode == "rename":
        top.append([InlineKeyboardButton("✏️ Rename this folder", callback_data="action:rename_this_folder")])
        top.append([InlineKeyboardButton("🔀 Move this folder",   callback_data="action:move_this_folder")])
    elif mode == "move":
        move_target = state.get("move_target")
        move_folder = state.get("move_folder_path")
        if move_target or move_folder:
            top.append([InlineKeyboardButton("📂 Move here", callback_data="action:move_here")])

    if path != "Root":
        top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])
    top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])

    total_files = len(get_files_in_folder(db, path))
    file_info = f" · {total_files} file{'s' if total_files != 1 else ''} here" if total_files else ""

    if mode == "retrieve" and total_files:
        top.insert(0, [InlineKeyboardButton(
            f"📂 View {total_files} file{'s' if total_files != 1 else ''} here",
            callback_data="action:view_files",
        )])
    elif mode in ("rename", "delete") and total_files:
        top.insert(0, [InlineKeyboardButton(
            f"📂 View {total_files} file{'s' if total_files != 1 else ''} here",
            callback_data="action:view_files",
        )])
    elif mode in ("move", "copy") and total_files and state.get("move_target") is None and state.get("copy_target") is None:
        top.insert(0, [InlineKeyboardButton(
            f"📂 View {total_files} file{'s' if total_files != 1 else ''} here",
            callback_data="action:view_files",
        )])

    mode_labels = {
        "store":    f"📁 *{format_breadcrumb(path)}*{file_info}",
        "retrieve": f"📁 *{format_breadcrumb(path)}*{file_info}",
        "delete":   f"🗑 *{format_breadcrumb(path)}*{file_info}",
        "rename":   f"✏️ *{format_breadcrumb(path)}*{file_info}",
        "move":     f"🔀 *{format_breadcrumb(path)}*{file_info}",
        "copy":     f"📋 *{format_breadcrumb(path)}*{file_info}",
    }
    header = mode_labels.get(mode, f"📁 *{format_breadcrumb(path)}*{file_info}")

    if not children:
        suffix = {
            "store":    "\n\nNo subfolders — store here or create one:",
            "retrieve": "\n\nNo subfolders here.",
            "delete":   "\n\nNo subfolders.",
            "rename":   "\n\nNo subfolders to rename.",
            "move":     "\n\nNo subfolders — move here or go up.",
            "copy":     "\n\nNo subfolders — copy here or go up.",
        }.get(mode, "")
        await query.edit_message_text(
            header + suffix, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(top),
        )
        return

    suffix = {
        "store":    "\n\nChoose a subfolder or store here:",
        "retrieve": "\n\nSelect a folder to browse:",
        "delete":   "\n\nSelect a folder to manage:",
        "rename":   "\n\nTap a folder to rename it, or use the buttons above:",
        "move":     "\n\nNavigate to destination folder:",
        "copy":     "\n\nNavigate to destination folder:",
    }.get(mode, "\n\nSelect a folder:")

    kb = build_paginated_keyboard(items, page, extra_top_rows=top)
    await query.edit_message_text(header + suffix, parse_mode="Markdown", reply_markup=kb)


async def show_files_in_folder(query, user_id: int) -> None:
    db = load_db()
    state = user_state.setdefault(user_id, _fresh_state())
    state["view"] = "files"

    path = normalize_path(state.get("path", "Root"))
    page = state.get("page", 0)
    mode = state["mode"]
    files = get_files_in_folder(db, path)

    files.sort(key=lambda f: f.get("stored_at", ""), reverse=True)

    back_row = [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")]
    menu_row = [InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")]

    if not files:
        extra: list[list] = [back_row]
        if mode == "delete":
            extra.append([InlineKeyboardButton(
                "🗑 Delete empty folder", callback_data="action:delete_this_folder"
            )])
        extra.append(menu_row)
        await query.edit_message_text(
            f"📂 *{format_breadcrumb(path)}* — no files here.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(extra),
        )
        return

    count_label = f"{len(files)} file{'s' if len(files) != 1 else ''}"

    if mode == "delete":
        bot_ = [
            [InlineKeyboardButton(
                f"🗑 Delete entire folder ({count_label})",
                callback_data="action:delete_this_folder",
            )],
            menu_row,
        ]
        items = [
            (f"🗑 {file_type_emoji(f.get('type','document'))} {f['filename']}",
             f"del_file:{f['message_id']}")
            for f in files
        ]
        kb = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=bot_)
        text = f"🗑 *{format_breadcrumb(path)}* — {count_label}\n\nTap a file to delete it:"

    elif mode == "rename":
        items = [
            (f"✏️ {file_type_emoji(f.get('type','document'))} {f['filename']}",
             f"rename_file:{f['message_id']}")
            for f in files
        ]
        kb = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row])
        text = f"✏️ *{format_breadcrumb(path)}* — {count_label}\n\nTap a file to rename it:"

    elif mode == "move":
        move_target = state.get("move_target")
        if move_target is None:
            # Step 1: pick the file to move
            items = [
                (f"🔀 {file_type_emoji(f.get('type','document'))} {f['filename']}",
                 f"pick_move:{f['message_id']}")
                for f in files
            ]
            kb = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row])
            text = f"🔀 *{format_breadcrumb(path)}* — {count_label}\n\nTap a file to move:"
        else:
            # Step 2: confirm drop here or keep browsing
            db2 = load_db()
            moving_file = db2.get(str(move_target), {})
            fname = moving_file.get("filename", f"ID {move_target}")
            top_rows = [
                [InlineKeyboardButton(f"📂 Move '{fname}' here", callback_data="action:move_here")],
                back_row,
            ]
            items = [
                (f"{file_type_emoji(f.get('type','document'))} {f['filename']}",
                 "noop")
                for f in files
            ]
            kb = build_paginated_keyboard(items, page, extra_top_rows=top_rows, extra_bottom_rows=[menu_row])
            text = (f"🔀 Moving: *{fname}*\n\n"
                    f"Destination: *{format_breadcrumb(path)}*\n\n"
                    "Tap 'Move here' to confirm, or navigate to a different folder:")

    else:
        # retrieve
        items = [
            (f"{file_type_emoji(f.get('type','document'))} {f['filename']}",
             f"get_file:{f['message_id']}")
            for f in files
        ]
        kb = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row])
        text = f"📂 *{format_breadcrumb(path)}* — {count_label}\n\nTap a file to receive it:"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_store_prompt(query, user_id: int) -> int:
    state = user_state.setdefault(user_id, _fresh_state("store"))
    state["mode"] = "store"
    state["view"] = "files"
    state["store_count"] = 0
    state["last_btn_msg"] = None
    path = normalize_path(state.get("path", "Root"))

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬆ Up",        callback_data="action:up")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
        ]
    )
    await query.edit_message_text(
        f"📁 *{format_breadcrumb(path)}*\n\n"
        "Send file(s) — you can send many in a row.\n"
        "_Supported: documents, photos, videos, audio, voice messages, stickers._",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return WAIT_STORE_FILE


# ---------------------------------------------------------------------------
# File action panel (retrieve: file tapped → offer retrieve/rename/move)
# ---------------------------------------------------------------------------

async def show_file_action_panel(query, user_id: int, message_id: int) -> int:
    """Show per-file actions: retrieve, rename, move."""
    db = load_db()
    item = db.get(str(message_id))
    if not item:
        await query.answer("⚠️ File not found in DB.", show_alert=True)
        return ConversationHandler.END

    fname  = item.get("filename", f"ID {message_id}")
    ftype  = item.get("type", "document")
    folder = format_breadcrumb(item.get("folder", "Root"))
    stored = item.get("stored_at", "unknown")

    emoji = file_type_emoji(ftype)
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 Retrieve", callback_data=f"do_retrieve:{message_id}")],
            [
                InlineKeyboardButton("✏️ Rename", callback_data=f"rename_file:{message_id}"),
                InlineKeyboardButton("🔀 Move",   callback_data=f"pick_move:{message_id}"),
            ],
            [InlineKeyboardButton("🗑 Delete",    callback_data=f"del_file:{message_id}")],
            [InlineKeyboardButton("◀ Back",       callback_data="action:back_files")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
        ]
    )
    await query.edit_message_text(
        f"{emoji} *{fname}*\n\n"
        f"📁 Folder: {folder}\n"
        f"🗓 Stored: `{stored}`\n\n"
        "What would you like to do?",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main callback / button handler
# ---------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id != OWNER_ID:
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return ConversationHandler.END

    data = query.data
    state = user_state.setdefault(user_id, _fresh_state())

    # ── no-op ────────────────────────────────────────────────────────────────
    if data == "noop":
        return ConversationHandler.END

    # ── main menu ────────────────────────────────────────────────────────────
    if data == "action:menu":
        user_state.pop(user_id, None)
        await query.edit_message_text(
            "📦 *Archive Bot*\n\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    # ── stats (inline) ───────────────────────────────────────────────────────
    if data == "action:stats":
        db = load_db()
        total_files, total_folders = db_stats(db)
        top_folders = get_subfolders_for_path(db, "Root")
        lines = []
        for f in top_folders[:10]:
            n = count_all_in_tree(db, f"Root/{f}")
            lines.append(f"  📁 {f}: {n}")
        root_files = len(get_files_in_folder(db, "Root"))
        if root_files:
            lines.insert(0, f"  📂 Root: {root_files}")
        breakdown = "\n".join(lines) if lines else "  (empty)"
        await query.edit_message_text(
            f"📊 *Archive Statistics*\n\n"
            f"Total files: *{total_files}*\n"
            f"Total folders: *{total_folders}*\n\n"
            f"*Breakdown:*\n{breakdown}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
            ),
        )
        return ConversationHandler.END

    # ── search trigger ────────────────────────────────────────────────────────
    if data == "action:search":
        user_state[user_id] = _fresh_state()
        await query.edit_message_text(
            "🔍 *Search*\n\nType the filename (or part of it):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:menu")]]
            ),
        )
        return WAIT_SEARCH_INPUT

    # ── mode selection ────────────────────────────────────────────────────────
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        user_state[user_id] = _fresh_state(mode)
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── pagination ────────────────────────────────────────────────────────────
    if data.startswith("page:"):
        state["page"] = int(data.split(":", 1)[1])
        if state.get("view") == "files":
            await show_files_in_folder(query, user_id)
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
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── navigate into folder ──────────────────────────────────────────────────
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
            files_here     = len(get_files_in_folder(db, new_path))
            subfolders_here = len(get_subfolders_for_path(db, new_path))
            total_tree     = count_all_in_tree(db, new_path)
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(
                        f"📂 Browse ({files_here} files · {subfolders_here} subfolders)",
                        callback_data="action:open_del_folder",
                    )],
                    [InlineKeyboardButton(
                        f"🗑 Delete all ({total_tree} total)",
                        callback_data="action:ask_del_tree",
                    )],
                    [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
                    [InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")],
                ]
            )
            await query.edit_message_text(
                f"🗑 *{format_breadcrumb(new_path)}*\n"
                f"Files here: {files_here}  ·  Subfolders: {subfolders_here}  ·  Total in tree: {total_tree}\n\n"
                "What would you like to do?",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return ConversationHandler.END

        # rename, move, retrieve — show file list directly
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── view files ────────────────────────────────────────────────────────────
    if data == "action:view_files":
        state["view"] = "files"
        state["page"] = 0
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── store: "Store here" ───────────────────────────────────────────────────
    if data == "action:store_here":
        return await show_store_prompt(query, user_id)

    # ── new folder ────────────────────────────────────────────────────────────
    if data == "action:new_folder":
        await query.edit_message_text(
            f"📁 Creating inside *{format_breadcrumb(state.get('path', 'Root'))}*\n\n"
            "Type the new folder name:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")]]
            ),
        )
        return WAIT_NEW_FOLDER

    # ── back to folder list ───────────────────────────────────────────────────
    if data == "action:back_folders":
        state["page"] = 0
        state["view"] = "folders"
        if state["mode"] == "store":
            await show_folder_list(query, user_id)
            return WAIT_STORE_FILE
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── back to file list ─────────────────────────────────────────────────────
    if data == "action:back_files":
        state["page"] = 0
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── retrieve: file tapped → action panel ─────────────────────────────────
    if data.startswith("get_file:"):
        message_id = int(data.split(":", 1)[1])
        return await show_file_action_panel(query, user_id, message_id)

    # ── retrieve: actually send the file ─────────────────────────────────────
    if data.startswith("do_retrieve:"):
        message_id = int(data.split(":", 1)[1])
        try:
            await context.bot.copy_message(
                chat_id=query.message.chat.id,
                from_chat_id=ARCHIVE_CHAT_ID,
                message_id=message_id,
            )
            await query.answer("✅ File sent!", show_alert=False)
        except Exception as e:
            await query.message.reply_text(
                f"⚠️ Could not retrieve file: {e}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
                ),
            )
        return ConversationHandler.END

    # ── delete flow: open folder ──────────────────────────────────────────────
    if data == "action:open_del_folder":
        state["page"] = 0
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    if data in ("action:ask_del_tree", "action:delete_this_folder"):
        path = normalize_path(state.get("path", "Root"))
        db = load_db()
        total = count_all_in_tree(db, path)
        warn = "\n\n⚠️ This folder contains a lot of files!" if total >= 20 else ""
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⚠️ Yes, delete everything", callback_data="action:confirm_del_tree")],
                [InlineKeyboardButton("❌ Cancel",                  callback_data="action:back_folders")],
            ]
        )
        await query.edit_message_text(
            "⚠️ *WARNING*\n\n"
            f"Folder: *{format_breadcrumb(path)}*\n"
            f"Total files to delete: *{total}*{warn}\n\n"
            "This *cannot* be undone.",
            parse_mode="Markdown",
            reply_markup=kb,
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
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, delete",  callback_data=f"confirm_del_file:{msg_id}")],
                [InlineKeyboardButton("❌ Cancel",        callback_data="action:back_files")],
            ]
        )
        await query.edit_message_text(
            f"🗑 Delete *{fname}*?\n\nThis cannot be undone.",
            parse_mode="Markdown",
            reply_markup=kb,
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
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── rename: file selected ─────────────────────────────────────────────────
    if data.startswith("rename_file:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        item = db.get(str(msg_id))
        old_name = item["filename"] if item else f"ID {msg_id}"
        state["rename_target"]  = msg_id
        state["rename_folder_path"] = None
        await query.edit_message_text(
            f"✏️ *Rename File*\n\n"
            f"Current name: `{old_name}`\n\n"
            "Send the new name:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_files")]]
            ),
        )
        return WAIT_RENAME_INPUT

    # ── rename: folder selected from list (tap on subfolder) ──────────────────
    if data.startswith("rename_folder:"):
        folder_name = data[len("rename_folder:"):]
        current = normalize_path(state.get("path", "Root"))
        full_path = normalize_path(f"{current}/{folder_name}")
        state["rename_folder_path"] = full_path
        state["rename_target"] = None
        await query.edit_message_text(
            f"✏️ *Rename Folder*\n\n"
            f"Current name: `{folder_name}`\n"
            f"Full path: `{format_breadcrumb(full_path)}`\n\n"
            "Send the new folder name:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")]]
            ),
        )
        return WAIT_RENAME_FOLDER

    # ── rename: "rename this folder" (the current folder itself) ──────────────
    if data == "action:rename_this_folder":
        path = normalize_path(state.get("path", "Root"))
        if path == "Root":
            await query.answer("⚠️ Cannot rename Root.", show_alert=True)
            return ConversationHandler.END
        folder_name = path.rsplit("/", 1)[-1]
        state["rename_folder_path"] = path
        state["rename_target"] = None
        await query.edit_message_text(
            f"✏️ *Rename Folder*\n\n"
            f"Current name: `{folder_name}`\n"
            f"Full path: `{format_breadcrumb(path)}`\n\n"
            "Send the new folder name:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")]]
            ),
        )
        return WAIT_RENAME_FOLDER

    # ── move: pick a file to move ─────────────────────────────────────────────
    if data.startswith("pick_move:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        item = db.get(str(msg_id))
        if not item:
            await query.answer("⚠️ File not found.", show_alert=True)
            return ConversationHandler.END
        fname = item.get("filename", f"ID {msg_id}")
        state["move_target"] = msg_id
        state["mode"] = "move"
        state["path"] = "Root"
        state["page"] = 0
        state["view"] = "folders"
        await query.edit_message_text(
            f"🔀 *Move File*\n\n"
            f"Moving: *{fname}*\n\n"
            "Navigate to the destination folder:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
            ),
        )
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── move: drop file in current folder ─────────────────────────────────────
    if data == "action:move_here":
        move_target = state.get("move_target")
        if not move_target:
            await query.answer("⚠️ No file selected to move.", show_alert=True)
            return ConversationHandler.END
        dest_path = normalize_path(state.get("path", "Root"))
        db = load_db()
        item = db.get(str(move_target))
        if not item:
            await query.answer("⚠️ File no longer exists.", show_alert=True)
            state["move_target"] = None
            return ConversationHandler.END
        old_folder = format_breadcrumb(item.get("folder", "Root"))
        fname = item.get("filename", f"ID {move_target}")
        moved = move_file_in_db(db, move_target, dest_path)
        if moved:
            save_db(db)
        state["move_target"] = None
        state["mode"] = "retrieve"
        await query.answer(f"✅ Moved!", show_alert=False)
        await query.edit_message_text(
            f"✅ *Moved successfully*\n\n"
            f"File: *{fname}*\n"
            f"From: {old_folder}\n"
            f"To: {format_breadcrumb(dest_path)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]]
            ),
        )
        return ConversationHandler.END

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler: create folder
# ---------------------------------------------------------------------------

async def receive_new_folder_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        await deny(update)
        return ConversationHandler.END

    user_id = update.effective_user.id
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

    state = user_state.setdefault(user_id, _fresh_state("store"))
    current = normalize_path(state.get("path", "Root"))
    new_path = normalize_path(f"{current}/{folder_name}")

    state["path"] = new_path
    state["page"] = 0
    state["view"] = "folders"
    state["store_count"] = 0
    state["last_btn_msg"] = None

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Store here",       callback_data="action:store_here")],
            [InlineKeyboardButton("➕ New Subfolder",     callback_data="action:new_folder")],
            [InlineKeyboardButton("⬆ Up",                callback_data="action:up")],
            [InlineKeyboardButton("🏠 Main Menu",         callback_data="action:menu")],
        ]
    )
    await update.message.reply_text(
        f"✅ Folder *{format_breadcrumb(new_path)}* created.\n\n"
        "Store here, add subfolders, or go up:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return WAIT_STORE_FILE


# ---------------------------------------------------------------------------
# ConversationHandler: rename file input
# ---------------------------------------------------------------------------

async def receive_rename_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        await deny(update)
        return ConversationHandler.END

    user_id = update.effective_user.id
    new_name = update.message.text.strip()
    state = user_state.get(user_id, {})
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
        await update.message.reply_text(
            "⚠️ File not found in archive.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    old_name = item["filename"]
    item["filename"] = new_name
    save_db(db)
    state["rename_target"] = None

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("◀ Back to Folder", callback_data="action:back_files")],
            [InlineKeyboardButton("🏠 Main Menu",      callback_data="action:menu")],
        ]
    )
    await update.message.reply_text(
        f"✅ *File renamed*\n\n"
        f"Old name: `{old_name}`\n"
        f"New name: `{new_name}`",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler: rename folder input
# ---------------------------------------------------------------------------

async def receive_rename_folder_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        await deny(update)
        return ConversationHandler.END

    user_id = update.effective_user.id
    new_name = update.message.text.strip()
    state = user_state.get(user_id, {})
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
    if old_path is None:
        await update.message.reply_text("⚠️ No folder selected. Returning to menu.")
        return ConversationHandler.END
    if old_path == "Root":
        await update.message.reply_text("⚠️ Cannot rename Root.")
        return ConversationHandler.END

    parent = parent_path(old_path)
    new_path = normalize_path(f"{parent}/{new_name}")

    # Check collision
    db = load_db()
    existing_siblings = get_subfolders_for_path(db, parent)
    if new_name in existing_siblings:
        await update.message.reply_text(
            f"⚠️ A folder named *{new_name}* already exists here. Choose a different name:",
            parse_mode="Markdown",
        )
        return WAIT_RENAME_FOLDER

    old_display = format_breadcrumb(old_path)
    updated = rename_folder_in_db(db, old_path, new_path)
    save_db(db)
    state["rename_folder_path"] = None
    # Navigate to the renamed folder
    state["path"] = new_path

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📂 Open renamed folder", callback_data="action:view_files")],
            [InlineKeyboardButton("⬆ Up",                   callback_data="action:up")],
            [InlineKeyboardButton("🏠 Main Menu",            callback_data="action:menu")],
        ]
    )
    await update.message.reply_text(
        f"✅ *Folder renamed*\n\n"
        f"Old: `{old_display}`\n"
        f"New: `{format_breadcrumb(new_path)}`\n\n"
        f"_{updated} file{'s' if updated != 1 else ''} updated._",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler: store file
# ---------------------------------------------------------------------------

_STORE_FILTER = (
    filters.Document.ALL
    | filters.PHOTO
    | filters.VIDEO
    | filters.AUDIO
    | filters.VOICE
    | filters.Sticker.ALL
) & ~filters.COMMAND


async def receive_store_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        await deny(update)
        return ConversationHandler.END

    user_id = update.effective_user.id
    message = update.message
    state = user_state.get(user_id, {})
    folder_path = normalize_path(state.get("path", "Root"))

    has_media = (
        message.document
        or message.photo
        or message.video
        or message.audio
        or message.voice
        or message.sticker
    )
    if not has_media:
        await message.reply_text(
            "⚠️ Please send a file (document, photo, video, audio, voice, or sticker)."
        )
        return WAIT_STORE_FILE

    try:
        copied = await context.bot.copy_message(
            chat_id=ARCHIVE_CHAT_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        await message.reply_text(
            f"⚠️ Failed to store file: {e}\n\nTry again or use /menu to abort."
        )
        return WAIT_STORE_FILE

    if message.document:
        file_type = "document"
        filename = resolve_filename(message, f"file_{copied.message_id}")
    elif message.photo:
        file_type = "photo"
        filename = resolve_filename(message, f"photo_{copied.message_id}.jpg")
    elif message.video:
        file_type = "video"
        filename = resolve_filename(message, f"video_{copied.message_id}.mp4")
    elif message.audio:
        file_type = "audio"
        filename = resolve_filename(message, f"audio_{copied.message_id}")
    elif message.voice:
        file_type = "voice"
        filename = resolve_filename(message, f"voice_{copied.message_id}.ogg")
    elif message.sticker:
        file_type = "sticker"
        emoji = message.sticker.emoji or ""
        filename = resolve_filename(message, f"sticker_{copied.message_id}{emoji}")
    else:
        return WAIT_STORE_FILE

    db = load_db()
    db[str(copied.message_id)] = {
        "filename":  filename,
        "folder":    folder_path,
        "message_id":copied.message_id,
        "type":      file_type,
        "stored_at": _now_iso(),
    }
    save_db(db)

    state["store_count"] = state.get("store_count", 0) + 1
    count = state["store_count"]

    await retire_last_btn_msg(context, message.chat.id, state, count, folder_path)

    emoji = file_type_emoji(file_type)
    await message.reply_text(
        f"{emoji} `{filename}` → *{format_breadcrumb(folder_path)}*",
        parse_mode="Markdown",
    )

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                f"📥 Store another  ({count} stored)",
                callback_data="action:store_here",
            )],
            [InlineKeyboardButton("⬆ Up",        callback_data="action:up")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
        ]
    )
    btn_msg = await message.reply_text(
        f"📁 *{format_breadcrumb(folder_path)}*\n\nSend more files or choose an action:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    state["last_btn_msg"] = btn_msg.message_id

    return WAIT_STORE_FILE


# ---------------------------------------------------------------------------
# /backup  /restore
# ---------------------------------------------------------------------------

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the live metadata.json to the owner as a downloadable file."""
    if not authorized(update):
        return await deny(update)

    if not DB_FILE.exists():
        await update.message.reply_text("⚠️ No database file found.")
        return

    db = load_db()
    total_files, total_folders = db_stats(db)
    caption = (
        f"🗄 *Archive backup*\n"
        f"`{DB_FILE.name}` — {total_files} files, {total_folders} folders\n"
        f"_{_now_iso()}_"
    )
    with open(DB_FILE, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=DB_FILE.name,
            caption=caption,
            parse_mode="Markdown",
        )


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Reply to a .json document with /restore to hot-reload the database.
    The current DB is backed up first so the restore is always reversible.
    """
    if not authorized(update):
        return await deny(update)

    # Must be a reply to a document message
    replied = update.message.reply_to_message
    if not replied or not replied.document:
        await update.message.reply_text(
            "ℹ️ *How to restore:*\n\n"
            "1. Use /backup to download your current database.\n"
            "2. Edit the file if needed.\n"
            "3. Send the `.json` file back to this chat.\n"
            "4. *Reply* to that file message with `/restore`.",
            parse_mode="Markdown",
        )
        return

    doc = replied.document
    if not doc.file_name or not doc.file_name.endswith(".json"):
        await update.message.reply_text("⚠️ The replied-to file must be a `.json` file.")
        return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:  # 5 MB sanity guard
        await update.message.reply_text("⚠️ File too large (max 5 MB).")
        return

    status_msg = await update.message.reply_text("⏳ Downloading and validating…")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw_bytes = await tg_file.download_as_bytearray()
        raw_text = raw_bytes.decode("utf-8")
        new_data = json.loads(raw_text)
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Could not parse the file: {e}")
        return

    if not isinstance(new_data, dict):
        await status_msg.edit_text("⚠️ Invalid format — expected a JSON object at the top level.")
        return

    # Back up current DB before overwriting
    _rotate_backups()

    # Write new data atomically
    DB_TMP_FILE.write_text(json.dumps(new_data, indent=2, ensure_ascii=False), encoding="utf-8")
    DB_TMP_FILE.replace(DB_FILE)

    total_files, total_folders = db_stats(new_data)
    await status_msg.edit_text(
        f"✅ *Database restored successfully*\n\n"
        f"Records loaded: *{total_files}* files across *{total_folders}* folders.\n"
        f"Previous database saved as `{_backup_path(1).name}`.",
        parse_mode="Markdown",
    )




async def joinme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)

    bot = context.bot

    try:
        chat = await bot.get_chat(ARCHIVE_CHAT_ID)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Cannot access archive group: {e}")
        return

    invite_link = None
    try:
        invite = await bot.create_chat_invite_link(
            ARCHIVE_CHAT_ID,
            creates_join_request=False,
            member_limit=1,
        )
        invite_link = invite.invite_link
    except Exception:
        invite_link = getattr(chat, "invite_link", None)

    if not invite_link:
        await update.message.reply_text("⚠️ Could not obtain an invite link.")
        return

    await update.message.reply_text(
        f"🔐 *Archive Group Invite*\n\n"
        f"{invite_link}\n\n"
        "This link is single-use. After joining, the bot will "
        "automatically promote you to admin and keep the group locked.\n\n"
        "_Anyone else who joins will be immediately kicked._",
        parse_mode="Markdown",
    )

    try:
        member = await bot.get_chat_member(ARCHIVE_CHAT_ID, OWNER_ID)
        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
        ):
            await _promote_owner(bot, ARCHIVE_CHAT_ID)
    except Exception:
        pass


async def _promote_owner(bot, chat_id: int) -> None:
    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=OWNER_ID,
            can_manage_chat=True,
            can_change_info=True,
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=True,
            can_manage_video_chats=True,
        )
    except Exception:
        pass


async def protect_archive_group(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
            f"🚨 *Intruder alert*\n\n"
            f"*{name}*{username} (ID: `{user.id}`) tried to join the archive group "
            f"and was immediately kicked.",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

app = Application.builder().token(BOT_TOKEN).post_init(set_commands).build()

conv = ConversationHandler(
    entry_points=[
        CommandHandler("start",  start),
        CommandHandler("menu",   menu_command),
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
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CommandHandler("menu",   menu_command),
        CommandHandler("start",  start),
    ],
    per_message=False,
    allow_reentry=True,
)

app.add_handler(conv)
app.add_handler(CommandHandler("help",    help_command))
app.add_handler(CommandHandler("stats",   stats_command))
app.add_handler(CommandHandler("joinme",  joinme_command))
app.add_handler(CommandHandler("backup",  backup_command))
app.add_handler(CommandHandler("restore", restore_command))
app.add_handler(
    ChatMemberHandler(
        protect_archive_group,
        ChatMemberHandler.CHAT_MEMBER,
    )
)

if __name__ == "__main__":
    print("Bot running…")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])