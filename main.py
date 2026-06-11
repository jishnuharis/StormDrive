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

DB_FILE = Path("metadata.json")
PAGE_SIZE = 10

# ConversationHandler states
WAIT_NEW_FOLDER = 1
WAIT_STORE_FILE = 2
WAIT_RENAME_INPUT = 3
WAIT_SEARCH_INPUT = 4

# ---------------------------------------------------------------------------
# In-memory user state
# ---------------------------------------------------------------------------
# user_state[user_id] = {
#   "mode":          "store" | "retrieve" | "delete" | "move",
#   "path":          str  — current folder path e.g. "Root/Videos/Edits"
#   "view":          "folders" | "files",
#   "page":          int,
#   "last_btn_msg":  int | None  — message_id of last summary-with-buttons
#   "store_count":   int  — files stored this session
#   "rename_target": int | None  — message_id of file being renamed
#   "move_target":   int | None  — message_id of file being moved
# }
user_state: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

if not DB_FILE.exists():
    DB_FILE.write_text("{}")


def load_db() -> dict:
    try:
        return json.loads(DB_FILE.read_text())
    except Exception:
        return {}


def save_db(data: dict) -> None:
    DB_FILE.write_text(json.dumps(data, indent=2))


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
        # collect every ancestor
        parts = p.split("/")
        for i in range(len(parts)):
            folders.add("/".join(parts[: i + 1]))
    return len(db), max(0, len(folders) - 1)  # exclude Root itself


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
# Button-message tracking: edit previous summary instead of just removing kb
# ---------------------------------------------------------------------------

async def retire_last_btn_msg(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    state: dict,
    count: int,
    folder_path: str,
) -> None:
    """
    Edit the previous store-summary message to a plain 'still storing' note
    with no buttons, so only the newest message has the action buttons.
    """
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
            reply_markup=None,  # remove buttons
        )
    except Exception:
        # Message might be too old to edit or already gone — silently ignore
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
            [InlineKeyboardButton("📥 Store", callback_data="mode:store")],
            [InlineKeyboardButton("📤 Retrieve", callback_data="mode:retrieve")],
            [InlineKeyboardButton("🗑 Delete", callback_data="mode:delete")],
            [
                InlineKeyboardButton("🔍 Search", callback_data="action:search"),
                InlineKeyboardButton("📊 Stats", callback_data="action:stats"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Open archive menu"),
            BotCommand("menu", "Return to main menu"),
            BotCommand("cancel", "Cancel current action"),
            BotCommand("stats", "Show archive statistics"),
            BotCommand("search", "Search files by name"),
            BotCommand("help", "Show help & command list"),
            BotCommand("joinme", "Get invite link & become admin in archive group"),
        ]
    )


# ---------------------------------------------------------------------------
# /start  /menu  /help  /stats  /search
# ---------------------------------------------------------------------------

def _fresh_state(mode: str = "retrieve") -> dict:
    return {
        "mode": mode,
        "path": "Root",
        "view": "folders",
        "page": 0,
        "last_btn_msg": None,
        "store_count": 0,
        "rename_target": None,
        "move_target": None,
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
        "/joinme — get archive group invite & become admin\n"
        "/help — this message\n\n"
        "*Modes:*\n"
        "📥 *Store* — navigate to a folder (or create one) and send files.\n"
        "  • Supports documents, photos, videos, audio, voice, stickers.\n"
        "  • Caption becomes the filename. Send many files in a row.\n"
        "📤 *Retrieve* — browse folders → tap a file to have it sent to you.\n"
        "🗑 *Delete* — delete individual files or entire folder trees.\n"
        "🔍 *Search* — find files across all folders by name.\n"
        "  • From a search result you can retrieve, rename, or move the file.\n\n"
        "*Archive group security:*\n"
        "The archive group auto-kicks anyone who isn't you (the owner).\n"
        "Use /joinme to get a fresh invite link and be promoted to admin.",
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

    # Per-folder breakdown (top-level only for brevity)
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
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
                ]
            ),
        )
        return ConversationHandler.END

    items = [
        (
            f"📄 {r['filename']}  [{format_breadcrumb(r.get('folder','Root'))}]",
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
            [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
        ],
    )
    await update.message.reply_text(
        f"🔍 *{len(results)} result{'s' if len(results) != 1 else ''}* for _{query_text}_\n\nTap a file to retrieve it:",
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

    items = [(folder_label(name), f"cd:{name}") for name in children]

    top: list[list[InlineKeyboardButton]] = []
    if mode == "store":
        top.append([InlineKeyboardButton("📥 Store here", callback_data="action:store_here")])
        top.append([InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")])
    if path != "Root":
        top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])
    top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])

    total_files = len(get_files_in_folder(db, path))
    file_info = f" · {total_files} file{'s' if total_files != 1 else ''} here" if total_files else ""

    if mode == "retrieve" and total_files:
        top.insert(
            0 if path == "Root" else 0,
            [InlineKeyboardButton(f"📂 View {total_files} file{'s' if total_files != 1 else ''} here", callback_data="action:view_files")],
        )

    if not children:
        if mode == "store":
            text = (
                f"📂 *{format_breadcrumb(path)}*{file_info} — no subfolders.\n\n"
                "Store here or create a new folder:"
            )
        elif mode == "delete":
            text = f"🗑 *{format_breadcrumb(path)}*{file_info} — no subfolders."
        else:
            text = f"📁 *{format_breadcrumb(path)}*{file_info} — no subfolders."
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(top)
        )
        return

    if mode == "store":
        text = (
            f"📁 *{format_breadcrumb(path)}*{file_info}\n\n"
            "Choose a subfolder or store here:"
        )
    elif mode == "delete":
        text = (
            f"🗑 *{format_breadcrumb(path)}*{file_info}\n\n"
            "Select a folder to manage:"
        )
    else:
        text = (
            f"📁 *{format_breadcrumb(path)}*{file_info}\n\n"
            "Select a folder to browse:"
        )

    kb = build_paginated_keyboard(items, page, extra_top_rows=top)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_files_in_folder(query, user_id: int) -> None:
    db = load_db()
    state = user_state.setdefault(user_id, _fresh_state())
    state["view"] = "files"

    path = normalize_path(state.get("path", "Root"))
    page = state.get("page", 0)
    mode = state["mode"]
    files = get_files_in_folder(db, path)

    # Sort by stored_at descending if available, else by filename
    files.sort(key=lambda f: f.get("stored_at", ""), reverse=True)

    back_row = [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")]
    menu_row = [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]

    if not files:
        extra: list[list] = [back_row]
        if mode == "delete":
            extra.append(
                [InlineKeyboardButton("🗑 Delete empty folder", callback_data="action:delete_this_folder")]
            )
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
            [InlineKeyboardButton(f"🗑 Delete entire folder ({count_label})", callback_data="action:delete_this_folder")],
            menu_row,
        ]
        items = [(f"🗑 {f['filename']}", f"del_file:{f['message_id']}") for f in files]
        kb = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=bot_)
        text = f"🗑 *{format_breadcrumb(path)}* — {count_label}\n\nTap a file to delete it:"
    else:
        items = [(f"📄 {f['filename']}", f"get_file:{f['message_id']}") for f in files]
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
            [InlineKeyboardButton("⬆ Up", callback_data="action:up")],
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

    # ── no-op (page counter label) ──────────────────────────────────────────
    if data == "noop":
        return ConversationHandler.END

    # ── main menu ───────────────────────────────────────────────────────────
    if data == "action:menu":
        user_state.pop(user_id, None)
        await query.edit_message_text(
            "📦 *Archive Bot*\n\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    # ── stats (inline) ──────────────────────────────────────────────────────
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

    # ── search trigger ───────────────────────────────────────────────────────
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

    # ── mode selection ───────────────────────────────────────────────────────
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        user_state[user_id] = _fresh_state(mode)
        await show_folder_list(query, user_id)
        return ConversationHandler.END  # folder nav doesn't need conv state

    # ── pagination ───────────────────────────────────────────────────────────
    if data.startswith("page:"):
        state["page"] = int(data.split(":", 1)[1])
        if state.get("view") == "files":
            await show_files_in_folder(query, user_id)
        else:
            await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── up ───────────────────────────────────────────────────────────────────
    if data == "action:up":
        state["path"] = parent_path(normalize_path(state.get("path", "Root")))
        state["page"] = 0
        state["view"] = "folders"
        if state["mode"] == "store":
            await show_folder_list(query, user_id)
            return WAIT_STORE_FILE
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── navigate into folder ─────────────────────────────────────────────────
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
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
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

        # retrieve mode — show files directly
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── view files in current folder (retrieve) ──────────────────────────────
    if data == "action:view_files":
        state["view"] = "files"
        state["page"] = 0
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── store: "Store here" ──────────────────────────────────────────────────
    if data == "action:store_here":
        return await show_store_prompt(query, user_id)

    # ── new folder ───────────────────────────────────────────────────────────
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

    # ── back to folder list ──────────────────────────────────────────────────
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

    # ── retrieve: file tapped ────────────────────────────────────────────────
    if data.startswith("get_file:"):
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

    # ── delete flow: open folder ─────────────────────────────────────────────
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
                [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
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
                [InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_del_file:{msg_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="action:back_files")],
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
            [InlineKeyboardButton("📥 Store here", callback_data="action:store_here")],
            [InlineKeyboardButton("➕ New Subfolder", callback_data="action:new_folder")],
            [InlineKeyboardButton("⬆ Up", callback_data="action:up")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
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

    # Determine type and filename
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
        "filename": filename,
        "folder": folder_path,
        "message_id": copied.message_id,
        "type": file_type,
        "stored_at": _now_iso(),
    }
    save_db(db)

    state["store_count"] = state.get("store_count", 0) + 1
    count = state["store_count"]

    # Edit previous summary message to archived state (removes buttons + updates text)
    await retire_last_btn_msg(context, message.chat.id, state, count, folder_path)

    # Send the per-file confirmation (no buttons — keeps chat tidy)
    await message.reply_text(
        f"✅ `{filename}` → *{format_breadcrumb(folder_path)}*",
        parse_mode="Markdown",
    )

    # Send new summary with buttons
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                f"📥 Store another  ({count} stored)",
                callback_data="action:store_here",
            )],
            [InlineKeyboardButton("⬆ Up", callback_data="action:up")],
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
# ConversationHandler: search input
# ---------------------------------------------------------------------------

# (receive_search_query defined earlier)

# ---------------------------------------------------------------------------
# Security: /joinme and archive group protection
# ---------------------------------------------------------------------------

async def joinme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)

    bot = context.bot

    try:
        chat = await bot.get_chat(ARCHIVE_CHAT_ID)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Cannot access archive group: {e}")
        return

    # Create a fresh single-use invite link
    invite_link = None
    try:
        invite = await bot.create_chat_invite_link(
            ARCHIVE_CHAT_ID,
            creates_join_request=False,
            member_limit=1,  # single-use for security
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

    # Try to promote OWNER_ID if already present
    try:
        member = await bot.get_chat_member(ARCHIVE_CHAT_ID, OWNER_ID)
        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
        ):
            await _promote_owner(bot, ARCHIVE_CHAT_ID)
    except Exception:
        pass  # owner not in group yet — will be promoted when they join


async def _promote_owner(bot, chat_id: int) -> None:
    """Grant the owner full admin permissions in the archive group."""
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
        pass  # may already be creator/owner; swallow silently


async def protect_archive_group(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Fires on any membership change in ARCHIVE_CHAT_ID.

    Rules:
    - A non-owner human who joins → ban immediately, then unban so they
      can be invited legitimately later, then notify the owner via DM.
    - The owner joins → promote to full admin.
    - Bot joins (self) → nothing (let it stay).
    - Any "left" / "kicked" event → ignore.
    """
    event: ChatMemberUpdated = update.chat_member
    if event.chat.id != ARCHIVE_CHAT_ID:
        return

    new = event.new_chat_member
    old = event.old_chat_member
    user = new.user
    bot = context.bot

    # Only act on actual joins (not leaves/kicks/bans)
    joined_statuses = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
    was_outside = old.status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
    is_now_inside = new.status in joined_statuses

    if not (was_outside and is_now_inside):
        return  # not a join event

    if user.is_bot:
        return  # allow bots (including self)

    if user.id == OWNER_ID:
        await _promote_owner(bot, ARCHIVE_CHAT_ID)
        return

    # Intruder — ban then immediately unban (removes from group but doesn't
    # add to ban list so they could be legitimately invited later)
    try:
        await bot.ban_chat_member(ARCHIVE_CHAT_ID, user.id)
    except Exception:
        pass
    try:
        await bot.unban_chat_member(ARCHIVE_CHAT_ID, user.id)
    except Exception:
        pass

    # Notify owner via DM
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
        CommandHandler("start", start),
        CommandHandler("menu", menu_command),
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
        WAIT_SEARCH_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_query),
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
app.add_handler(
    ChatMemberHandler(
        protect_archive_group,
        ChatMemberHandler.CHAT_MEMBER,
    )
)

if __name__ == "__main__":
    print("Bot running…")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])