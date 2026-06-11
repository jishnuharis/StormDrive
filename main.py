import json
import os
from pathlib import Path

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN       = os.environ["BOT_TOKEN"]
ARCHIVE_CHAT_ID = int(os.environ["ARCHIVE_CHAT_ID"])
OWNER_ID        = int(os.environ["OWNER_ID"])

DB_FILE   = Path("metadata.json")
PAGE_SIZE = 10

# ConversationHandler states
WAIT_NEW_FOLDER = 1
WAIT_STORE_FILE = 2

# ---------------------------------------------------------------------------
# In-memory user state
# ---------------------------------------------------------------------------
# user_state[user_id] = {
#   "mode":          "store" | "retrieve" | "delete",
#   "path":          str,            e.g. "Root" or "Root/Videos/Edits"
#   "view":          "folders" | "files",
#   "page":          int,
#   "last_btn_msg":  int | None,     message_id of the last button-reply we sent
#   "store_count":   int,            files stored in this session
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
    return [item for item in db.values()
            if normalize_path(item.get("folder", "Root")) == folder_path]


def count_all_in_tree(db: dict, folder_path: str) -> int:
    """Count all files at or under folder_path."""
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    return sum(
        1 for item in db.values()
        if normalize_path(item.get("folder", "Root")) == folder_path
        or normalize_path(item.get("folder", "Root")).startswith(prefix)
    )


def delete_folder_tree(db: dict, folder_path: str) -> list[str]:
    folder_path = normalize_path(folder_path)
    prefix = folder_path + "/"
    keys = [
        k for k, item in db.items()
        if normalize_path(item.get("folder", "Root")) == folder_path
        or normalize_path(item.get("folder", "Root")).startswith(prefix)
    ]
    for k in keys:
        del db[k]
    return keys


def format_breadcrumb(path: str) -> str:
    return normalize_path(path).replace("/", " › ")


def parent_path(path: str) -> str:
    path = normalize_path(path)
    if path == "Root":
        return "Root"
    return normalize_path(path.rsplit("/", 1)[0])


def resolve_filename(message, fallback: str) -> str:
    """
    Best-effort filename from a Telegram message.
    Priority: caption (user-supplied name) → file_name attr → fallback.
    """
    # User can set a caption like "my_clip.mp4" to name the file
    caption = (message.caption or "").strip()
    if caption:
        return caption

    if message.document and message.document.file_name:
        return message.document.file_name
    if message.video and message.video.file_name:
        return message.video.file_name
    if message.audio and message.audio.file_name:
        return message.audio.file_name
    # Photos and camera-recorded videos have no native filename
    return fallback


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


async def deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("Unauthorized.", show_alert=True)
    elif update.message:
        await update.message.reply_text("Unauthorized.")


# ---------------------------------------------------------------------------
# Button-message tracking helpers
# ---------------------------------------------------------------------------

async def clear_last_btn_msg(context: ContextTypes.DEFAULT_TYPE, chat_id: int, state: dict) -> None:
    """
    Strip the inline keyboard from the previous store-confirmation message
    so it becomes plain text, leaving only the last message with buttons.
    """
    msg_id = state.get("last_btn_msg")
    if not msg_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=None,
        )
    except Exception:
        pass  # already edited / deleted / too old — silently ignore
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
    chunk = items[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]
    for label, cb in chunk:
        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"page:{page + 1}"))
    if nav:
        keyboard.append(nav)

    for row in extra_bottom_rows or []:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Store",    callback_data="mode:store")],
        [InlineKeyboardButton("📤 Retrieve", callback_data="mode:retrieve")],
        [InlineKeyboardButton("🗑 Delete",   callback_data="mode:delete")],
    ])


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",  "Open archive menu"),
        BotCommand("menu",   "Return to main menu"),
        BotCommand("cancel", "Cancel current action"),
    ])


# ---------------------------------------------------------------------------
# /start  &  /menu
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "📦 *Archive Bot*\n\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await deny(update)
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "📦 *Archive Bot*\n\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# View helpers  (all edit the inline message in place)
# ---------------------------------------------------------------------------

async def show_folder_list(query, user_id: int) -> None:
    db    = load_db()
    state = user_state.setdefault(
        user_id, {"mode": "retrieve", "path": "Root", "view": "folders", "page": 0,
                  "last_btn_msg": None, "store_count": 0}
    )
    state["view"] = "folders"

    mode     = state["mode"]
    page     = state.get("page", 0)
    path     = normalize_path(state.get("path", "Root"))
    children = get_subfolders_for_path(db, path)

    # Annotate subfolders with file counts in retrieve/delete modes
    if mode in ("retrieve", "delete") and children:
        def folder_label(name: str) -> str:
            full = normalize_path(f"{path}/{name}")
            n = count_all_in_tree(db, full)
            return f"📁 {name}  ({n})" if n else f"📁 {name}"
        items = [(folder_label(name), f"cd:{name}") for name in children]
    else:
        items = [(f"📁 {name}", f"cd:{name}") for name in children]

    top: list[list[InlineKeyboardButton]] = []

    if mode == "store":
        top.append([InlineKeyboardButton("📥 Store here",  callback_data="action:store_here")])
        top.append([InlineKeyboardButton("➕ New Folder",   callback_data="action:new_folder")])
    if path != "Root":
        top.append([InlineKeyboardButton("⬆ Up",           callback_data="action:up")])
    top.append([InlineKeyboardButton("🏠 Main Menu",        callback_data="action:menu")])

    total_files = len(get_files_in_folder(db, path))
    file_info   = f"  ·  {total_files} file{'s' if total_files != 1 else ''} here" if total_files else ""

    if not children:
        if mode == "store":
            text = f"📂 *{format_breadcrumb(path)}*{file_info} — no subfolders.\n\nStore here or create a new folder:"
        elif mode == "delete":
            text = f"🗑 *{format_breadcrumb(path)}*{file_info} — no subfolders."
        else:
            text = f"📁 *{format_breadcrumb(path)}*{file_info} — no subfolders."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(top))
        return

    if mode == "store":
        text = f"📁 *{format_breadcrumb(path)}*{file_info}\n\nChoose a subfolder or store here:"
    elif mode == "delete":
        text = f"🗑 *{format_breadcrumb(path)}*{file_info}\n\nSelect a folder to manage:"
    else:
        text = f"📁 *{format_breadcrumb(path)}*{file_info}\n\nSelect a folder to browse:"

    kb = build_paginated_keyboard(items, page, extra_top_rows=top)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_files_in_folder(query, user_id: int) -> None:
    db    = load_db()
    state = user_state.setdefault(
        user_id, {"mode": "retrieve", "path": "Root", "view": "files", "page": 0,
                  "last_btn_msg": None, "store_count": 0}
    )
    state["view"] = "files"

    path  = normalize_path(state.get("path", "Root"))
    page  = state.get("page", 0)
    mode  = state["mode"]
    files = get_files_in_folder(db, path)

    back_row = [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")]
    menu_row = [InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")]

    if not files:
        extra: list[list] = [back_row]
        if mode == "delete":
            extra.append([InlineKeyboardButton(
                f"🗑 Delete empty folder",
                callback_data="action:delete_this_folder",
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
        items = [(f"🗑 {f['filename']}", f"del_file:{f['message_id']}") for f in files]
        kb    = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=bot_)
        text  = f"🗑 *{format_breadcrumb(path)}* — {count_label}\n\nTap a file to delete it:"
    else:
        items = [(f"📄 {f['filename']}", f"get_file:{f['message_id']}") for f in files]
        kb    = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row])
        text  = f"📂 *{format_breadcrumb(path)}* — {count_label}\n\nTap a file to receive it:"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_store_prompt(query, user_id: int) -> int:
    state = user_state.setdefault(
        user_id, {"mode": "store", "path": "Root", "view": "files", "page": 0,
                  "last_btn_msg": None, "store_count": 0}
    )
    state["view"]        = "files"
    state["store_count"] = 0
    state["last_btn_msg"] = None
    path = normalize_path(state.get("path", "Root"))

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆ Up",         callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    await query.edit_message_text(
        f"📁 *{format_breadcrumb(path)}*\n\nSend file(s) — you can send many in a row.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return WAIT_STORE_FILE


# ---------------------------------------------------------------------------
# Main callback / button handler
# ---------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id != OWNER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return ConversationHandler.END

    data  = query.data
    state = user_state.setdefault(
        user_id, {"mode": "retrieve", "path": "Root", "view": "folders", "page": 0,
                  "last_btn_msg": None, "store_count": 0}
    )

    # ── Main menu ──────────────────────────────────────────────────────────
    if data == "action:menu":
        user_state.pop(user_id, None)
        await query.edit_message_text(
            "📦 *Archive Bot*\n\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    # ── Mode selection ─────────────────────────────────────────────────────
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        user_state[user_id] = {
            "mode": mode, "path": "Root", "view": "folders", "page": 0,
            "last_btn_msg": None, "store_count": 0,
        }
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Pagination ─────────────────────────────────────────────────────────
    if data.startswith("page:"):
        state["page"] = int(data.split(":", 1)[1])
        if state.get("view") == "files":
            await show_files_in_folder(query, user_id)
        else:
            await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Up one level ───────────────────────────────────────────────────────
    if data == "action:up":
        state["path"] = parent_path(normalize_path(state.get("path", "Root")))
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Enter subfolder ────────────────────────────────────────────────────
    if data.startswith("cd:"):
        folder_name  = data[3:]
        current      = normalize_path(state.get("path", "Root"))
        new_path     = normalize_path(f"{current}/{folder_name}")
        state["path"] = new_path
        state["page"] = 0
        mode          = state["mode"]

        if mode == "delete":
            db              = load_db()
            files_here      = len(get_files_in_folder(db, new_path))
            subfolders_here = len(get_subfolders_for_path(db, new_path))
            total_tree      = count_all_in_tree(db, new_path)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📂 Browse ({files_here} files · {subfolders_here} subfolders)",
                    callback_data="action:open_del_folder",
                )],
                [InlineKeyboardButton(
                    f"🗑 Delete all ({total_tree} total files)",
                    callback_data="action:ask_del_tree",
                )],
                [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
                [InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")],
            ])
            await query.edit_message_text(
                f"🗑 *{format_breadcrumb(new_path)}*\n"
                f"Files here: {files_here}  ·  Subfolders: {subfolders_here}  ·  Total in tree: {total_tree}\n\n"
                f"What do you want to do?",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return ConversationHandler.END

        if mode == "store":
            state["view"] = "folders"
            await show_folder_list(query, user_id)
            return ConversationHandler.END

        # retrieve
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── Store: "Store here" ────────────────────────────────────────────────
    if data == "action:store_here":
        return await show_store_prompt(query, user_id)

    # ── New folder ─────────────────────────────────────────────────────────
    if data == "action:new_folder":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
        ])
        await query.edit_message_text(
            f"📁 Creating inside *{format_breadcrumb(state.get('path', 'Root'))}*\n\nType the new folder name:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return WAIT_NEW_FOLDER

    # ── Back to folder list ────────────────────────────────────────────────
    if data == "action:back_folders":
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Back to file list ─────────────────────────────────────────────────
    if data == "action:back_files":
        state["page"] = 0
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── Retrieve: file tapped ──────────────────────────────────────────────
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
            await query.message.reply_text(f"⚠️ Could not retrieve file: {e}")
        return ConversationHandler.END

    # ==========================================================================
    # DELETE flow
    # ==========================================================================

    if data == "action:open_del_folder":
        state["page"] = 0
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    if data in ("action:ask_del_tree", "action:delete_this_folder"):
        path  = normalize_path(state.get("path", "Root"))
        db    = load_db()
        total = count_all_in_tree(db, path)
        warn  = "\n\n⚠ This folder contains a lot of files!" if total >= 20 else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠ Yes, delete everything", callback_data="action:confirm_del_tree")],
            [InlineKeyboardButton("❌ Cancel",                 callback_data="action:back_folders")],
        ])
        await query.edit_message_text(
            f"⚠ *WARNING*\n\n"
            f"Folder: *{format_breadcrumb(path)}*\n"
            f"Total files to delete: {total}{warn}\n\n"
            f"This cannot be undone.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return ConversationHandler.END

    if data == "action:confirm_del_tree":
        path  = normalize_path(state.get("path", "Root"))
        db    = load_db()
        keys  = delete_folder_tree(db, path)
        save_db(db)

        for k in keys:
            try:
                await context.bot.delete_message(ARCHIVE_CHAT_ID, int(k))
            except Exception:
                pass

        deleted_label = format_breadcrumb(path)
        # Navigate to parent folder list immediately
        state["path"] = parent_path(path)
        state["page"] = 0
        state["view"] = "folders"

        # Show a brief success toast then render the parent folder list
        await query.answer(f"✅ '{deleted_label}' deleted ({len(keys)} files).", show_alert=True)
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    if data.startswith("del_file:"):
        msg_id = int(data.split(":", 1)[1])
        db     = load_db()
        item   = db.get(str(msg_id))
        fname  = item["filename"] if item else f"ID {msg_id}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_del_file:{msg_id}")],
            [InlineKeyboardButton("❌ Cancel",       callback_data="action:back_files")],
        ])
        await query.edit_message_text(
            f"🗑 Delete *{fname}*?\n\nThis cannot be undone.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return ConversationHandler.END

    if data.startswith("confirm_del_file:"):
        msg_id = int(data.split(":", 1)[1])
        db     = load_db()
        item   = db.pop(str(msg_id), None)
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
# ConversationHandler steps
# ---------------------------------------------------------------------------

async def receive_new_folder_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        return await deny(update)

    user_id     = update.effective_user.id
    folder_name = update.message.text.strip()

    if not folder_name:
        await update.message.reply_text("Folder name cannot be empty. Try again:")
        return WAIT_NEW_FOLDER
    if "/" in folder_name:
        await update.message.reply_text("Folder name cannot contain '/'. Try again:")
        return WAIT_NEW_FOLDER

    state    = user_state.setdefault(
        user_id, {"mode": "store", "path": "Root", "view": "folders", "page": 0,
                  "last_btn_msg": None, "store_count": 0}
    )
    current  = normalize_path(state.get("path", "Root"))
    new_path = normalize_path(f"{current}/{folder_name}")

    state["path"]        = new_path
    state["page"]        = 0
    state["view"]        = "folders"
    state["store_count"] = 0
    state["last_btn_msg"] = None

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Store here",    callback_data="action:store_here")],
        [InlineKeyboardButton("➕ New Subfolder",  callback_data="action:new_folder")],
        [InlineKeyboardButton("⬆ Up",             callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu",     callback_data="action:menu")],
    ])
    await update.message.reply_text(
        f"✅ Folder *{format_breadcrumb(new_path)}* created.\n\nStore here, add subfolders, or go up:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return WAIT_STORE_FILE


async def receive_store_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        return await deny(update)

    user_id = update.effective_user.id
    message = update.message
    state   = user_state.get(user_id, {})
    folder_path = normalize_path(state.get("path", "Root"))

    if not (message.document or message.photo or message.video or message.audio):
        await message.reply_text("Please send a file (document, photo, video, or audio).")
        return WAIT_STORE_FILE

    copied = await context.bot.copy_message(
        chat_id=ARCHIVE_CHAT_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )

    # ── Filename resolution (FIX: preserve original names) ─────────────────
    file_type: str
    if message.document:
        file_type = "document"
        filename  = resolve_filename(message, f"file_{copied.message_id}")
    elif message.photo:
        file_type = "photo"
        filename  = resolve_filename(message, f"photo_{copied.message_id}.jpg")
    elif message.video:
        file_type = "video"
        filename  = resolve_filename(message, f"video_{copied.message_id}.mp4")
    elif message.audio:
        file_type = "audio"
        filename  = resolve_filename(message, f"audio_{copied.message_id}")
    else:
        return WAIT_STORE_FILE

    db = load_db()
    db[str(copied.message_id)] = {
        "filename":   filename,
        "folder":     folder_path,
        "message_id": copied.message_id,
        "type":       file_type,
    }
    save_db(db)

    # ── Update session counter ─────────────────────────────────────────────
    state["store_count"] = state.get("store_count", 0) + 1
    count = state["store_count"]

    # ── Clear buttons from previous confirmation message ───────────────────
    await clear_last_btn_msg(context, message.chat.id, state)

    # ── Plain-text ack (no buttons) ────────────────────────────────────────
    plain_ack = await message.reply_text(
        f"✅ `{filename}` → *{format_breadcrumb(folder_path)}*",
        parse_mode="Markdown",
    )

    # ── Button message (replaces the previous one) ─────────────────────────
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📥 Store another  ({count} stored)",
            callback_data="action:store_here",
        )],
        [InlineKeyboardButton("⬆ Up",         callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    btn_msg = await message.reply_text(
        f"📁 *{format_breadcrumb(folder_path)}*\n\nSend more files or choose an action:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    state["last_btn_msg"] = btn_msg.message_id

    return WAIT_STORE_FILE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

app = Application.builder().token(BOT_TOKEN).post_init(set_commands).build()

conv = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(button_handler),
    ],
    states={
        WAIT_NEW_FOLDER: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_folder_name),
            CallbackQueryHandler(button_handler),
        ],
        WAIT_STORE_FILE: [
            MessageHandler(
                (filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO)
                & ~filters.COMMAND,
                receive_store_file,
            ),
            CallbackQueryHandler(button_handler),
        ],
    },
    fallbacks=[
        CommandHandler("menu",   menu_command),
        CommandHandler("start",  start),
        CommandHandler("cancel", cancel),
    ],
    per_message=False,
)

app.add_handler(CommandHandler("start",  start))
app.add_handler(CommandHandler("menu",   menu_command))
app.add_handler(conv)

if __name__ == "__main__":
    print("Bot running...")
    app.run_polling()