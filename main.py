import json, os
from pathlib import Path

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
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

BOT_TOKEN = os.environ["BOT_TOKEN"]
ARCHIVE_CHAT_ID = int(os.environ["ARCHIVE_CHAT_ID"])
OWNER_ID = int(os.environ["OWNER_ID"])

DB_FILE = Path("metadata.json")
PAGE_SIZE = 10

# ConversationHandler states
WAIT_NEW_FOLDER = 1
WAIT_STORE_FILE = 2

# ---------------------------------------------------------------------------
# In-memory user state
# ---------------------------------------------------------------------------
# user_state[user_id] = {
#   "mode":   "store" | "retrieve" | "delete",
#   "path":   "Root/Videos/Edits",
#   "page":   int,
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
    """Ensure every path is 'Root' or 'Root/...', without duplicates."""
    if not path:
        return "Root"
    path = path.strip().strip("/")
    if path == "" or path == "Root":
        return "Root"
    # Remove leading 'Root/' if present
    if path.startswith("Root/"):
        path = path[5:]
    # Collapse repeated slashes
    parts = [p for p in path.split("/") if p]
    return "Root" if not parts else "Root/" + "/".join(parts)


def get_subfolders_for_path(db: dict, current_path: str) -> list[str]:
    """
    Returns immediate child folder names under current_path.
    """
    paths = {normalize_path(item.get("folder", "Root")) for item in db.values()}
    current_path = normalize_path(current_path)
    prefix = current_path.rstrip("/") + "/"
    children: set[str] = set()

    for p in paths:
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if not rest:
            continue
        first_segment = rest.split("/", 1)[0]
        children.add(first_segment)

    return sorted(children)


def get_files_in_folder(db: dict, folder_path: str) -> list[dict]:
    folder_path = normalize_path(folder_path)
    return [item for item in db.values() if normalize_path(item.get("folder", "Root")) == folder_path]


def delete_folder_tree(db: dict, folder_path: str) -> list[str]:
    """
    Deletes all DB entries under folder_path (including subfolders).
    Returns list of DB keys that were deleted so caller can delete messages.
    """
    folder_path = normalize_path(folder_path)
    prefix = folder_path.rstrip("/") + "/"
    keys_to_delete: list[str] = []

    for k, item in db.items():
        fp = normalize_path(item.get("folder", "Root"))
        if fp == folder_path or fp.startswith(prefix):
            keys_to_delete.append(k)

    for k in keys_to_delete:
        del db[k]

    return keys_to_delete


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

    start = page * PAGE_SIZE
    chunk = items[start: start + PAGE_SIZE]

    keyboard = list(extra_top_rows or [])

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


def format_breadcrumb(path: str) -> str:
    path = normalize_path(path)
    if path == "Root":
        return "Root"
    return path.replace("Root/", "Root › ")


def folder_list_keyboard(children: list[str], page: int, mode: str, path: str) -> InlineKeyboardMarkup:
    items = [(f"📁 {name}", f"folder:{name}") for name in children]

    top: list[list[InlineKeyboardButton]] = []

    if normalize_path(path) != "Root":
        top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])

    if mode == "store":
        top.append([InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")])

    top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])

    return build_paginated_keyboard(items, page, extra_top_rows=top)


def files_in_folder_keyboard(files: list[dict], page: int, mode: str) -> InlineKeyboardMarkup:
    if mode == "delete":
        items = [(f"🗑 {f['filename']}", f"delete_file:{f['message_id']}") for f in files]
    else:
        items = [(f"📄 {f['filename']}", f"file:{f['message_id']}") for f in files]

    top = [[InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")]]
    bot_ = [[InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")]]

    return build_paginated_keyboard(items, page, extra_top_rows=top, extra_bottom_rows=bot_)


async def delete_file_confirm(query, msg_id: int) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_delete_file:{msg_id}")],
        [InlineKeyboardButton("❌ Cancel",  callback_data="action:back_files")],
    ])
    await query.edit_message_text("⚠ Delete this file?", reply_markup=kb)


def folder_delete_choice_keyboard(folder_path: str, file_count: int, subfolder_count: int) -> InlineKeyboardMarkup:
    """
    folder_path: full normalized path, e.g. 'Root/Videos'
    """
    folder_path = normalize_path(folder_path)
    total_items_text = f"{file_count} files, {subfolder_count} subfolders"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"📂 Open folder ({total_items_text})",
                callback_data=f"action:open_folder_delete:{folder_path}",
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 Delete folder (and all inside)",
                callback_data=f"action:delete_folder_confirm1:{folder_path}",
            )
        ],
        [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="action:menu")],
    ])
    return kb


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
# View helpers
# ---------------------------------------------------------------------------

async def show_folder_list(query, user_id: int) -> None:
    db    = load_db()
    state = user_state.setdefault(user_id, {"mode": "retrieve", "path": "Root", "page": 0})
    mode  = state["mode"]
    page  = state.get("page", 0)
    path  = normalize_path(state.get("path", "Root"))

    children = get_subfolders_for_path(db, path)

    if not children:
        text = f"📂 *{format_breadcrumb(path)}* — no subfolders."
        top: list[list[InlineKeyboardButton]] = []

        if path != "Root":
            top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])

        if mode == "store":
            top.append([InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")])

        top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])
        kb = InlineKeyboardMarkup(top)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return

    kb = folder_list_keyboard(children, page, mode, path)

    if mode == "store":
        text = f"📁 *{format_breadcrumb(path)}* — choose a folder or create a new one:"
    elif mode == "delete":
        text = f"🗑 *{format_breadcrumb(path)}* — select a folder to manage:"
    else:
        text = f"📁 *{format_breadcrumb(path)}* — select a folder to browse:"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_files_in_folder(query, user_id: int) -> None:
    db    = load_db()
    state = user_state.setdefault(user_id, {"mode": "retrieve", "path": "Root", "page": 0})
    path  = normalize_path(state.get("path", "Root"))
    page  = state.get("page", 0)
    mode  = state["mode"]

    files = get_files_in_folder(db, path)

    if not files:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
            [InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")],
        ])
        await query.edit_message_text(
            f"📂 *{format_breadcrumb(path)}* is empty.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    kb = files_in_folder_keyboard(files, page, mode)

    if mode == "delete":
        text = f"🗑 *{format_breadcrumb(path)}* — choose a file to delete:"
    else:
        text = f"📂 *{format_breadcrumb(path)}* — tap a file to receive it:"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


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

    data = query.data

    # Main menu
    if data == "action:menu":
        user_state.pop(user_id, None)
        await query.edit_message_text(
            "📦 *Archive Bot*\n\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    # Mode selection
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        user_state[user_id] = {"mode": mode, "path": "Root", "page": 0}
        await show_folder_list(query, user_id)
        return WAIT_STORE_FILE if mode == "store" else ConversationHandler.END

    # Pagination (for folder lists)
    if data.startswith("page:"):
        page = int(data.split(":", 1)[1])
        state = user_state.setdefault(user_id, {"mode": "retrieve", "path": "Root", "page": 0})
        state["page"] = page
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # Up one level
    if data == "action:up":
        state = user_state.setdefault(user_id, {"mode": "store", "path": "Root", "page": 0})
        current = normalize_path(state.get("path", "Root"))
        if current != "Root":
            parent = current.rsplit("/", 1)[0]
            state["path"] = normalize_path(parent)
        state["page"] = 0
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # Folder selected
    if data.startswith("folder:"):
        folder_name = data.split(":", 1)[1]
        state = user_state.setdefault(user_id, {"mode": "retrieve", "path": "Root", "page": 0})
        mode  = state["mode"]
        current = normalize_path(state.get("path", "Root"))

        # Always build new path = current + one child segment
        if current == "Root":
            new_path = f"Root/{folder_name}"
        else:
            new_path = f"{current}/{folder_name}"
        new_path = normalize_path(new_path)
        state["path"] = new_path
        state["page"] = 0

        if mode == "delete":
            db = load_db()
            files_here = len(get_files_in_folder(db, new_path))
            subfolders_here = len(get_subfolders_for_path(db, new_path))
            kb = folder_delete_choice_keyboard(new_path, files_here, subfolders_here)
            await query.edit_message_text(
                f"🗑 Folder *{format_breadcrumb(new_path)}*"
                f"\nFiles: {files_here}, Subfolders: {subfolders_here}\n\nWhat do you want to do?",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return ConversationHandler.END

        if mode == "store":
            await show_folder_list(query, user_id)
            return WAIT_STORE_FILE

        # retrieve mode
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # New folder: ask for name
    if data == "action:new_folder":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
        ])
        await query.edit_message_text(
            "📁 Type the name of the new folder:",
            reply_markup=kb,
        )
        return WAIT_NEW_FOLDER

    # Open folder from delete choice
    if data.startswith("action:open_folder_delete:"):
        # "action:open_folder_delete:<folder_path>"
        folder_path = normalize_path(data.split(":", 2)[2])
        state = user_state.setdefault(user_id, {"mode": "delete", "path": "Root", "page": 0})
        state["path"] = folder_path
        state["page"] = 0
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # First confirmation before delete folder tree
    if data.startswith("action:delete_folder_confirm1:"):
        # "action:delete_folder_confirm1:<folder_path>"
        folder_path = normalize_path(data.split(":", 2)[2])

        db = load_db()
        prefix = folder_path.rstrip("/") + "/"
        total_files = 0
        for item in db.values():
            fp = normalize_path(item.get("folder", "Root"))
            if fp == folder_path or fp.startswith(prefix):
                total_files += 1

        extra = "\n\n⚠ This folder has a lot of files!" if total_files >= 20 else ""
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⚠ Yes, delete this folder tree",
                    callback_data=f"confirm_delete_folder:{folder_path}",
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
        ])
        await query.edit_message_text(
            f"⚠ WARNING\n\nFolder: {format_breadcrumb(folder_path)}\n"
            f"Total files: {total_files}{extra}\n\nThis cannot be undone.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return ConversationHandler.END

    # Back to folder list
    if data == "action:back_folders":
        state = user_state.setdefault(user_id, {"mode": "store", "path": "Root", "page": 0})
        state["page"] = 0
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # Back to files after cancel delete file
    if data == "action:back_files":
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # Delete single file
    if data.startswith("delete_file:"):
        msg_id = int(data.split(":", 1)[1])
        await delete_file_confirm(query, msg_id)
        return ConversationHandler.END

    if data.startswith("confirm_delete_file:"):
        msg_id = int(data.split(":", 1)[1])
        db = load_db()
        try:
            await context.bot.delete_message(chat_id=ARCHIVE_CHAT_ID, message_id=msg_id)
        except Exception:
            pass
        db.pop(str(msg_id), None)
        save_db(db)
        await query.edit_message_text("✅ File deleted.")
        return ConversationHandler.END

    # Final folder tree delete
    if data.startswith("confirm_delete_folder:"):
        # "confirm_delete_folder:<folder_path>"
        folder_path = normalize_path(data.split(":", 1)[1])
        db = load_db()
        keys_to_delete = delete_folder_tree(db, folder_path)

        for k in keys_to_delete:
            try:
                msg_id = int(k)
                await context.bot.delete_message(ARCHIVE_CHAT_ID, msg_id)
            except Exception:
                pass

        save_db(db)
        await query.edit_message_text(f"✅ Folder tree '{format_breadcrumb(folder_path)}' deleted.")
        return ConversationHandler.END

    # File selected (retrieve mode)
    if data.startswith("file:"):
        message_id = int(data.split(":", 1)[1])
        try:
            await context.bot.copy_message(
                chat_id=query.message.chat.id,
                from_chat_id=ARCHIVE_CHAT_ID,
                message_id=message_id,
            )
        except Exception as e:
            await query.message.reply_text(f"⚠️ Could not retrieve file: {e}")
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

    state = user_state.setdefault(user_id, {"mode": "store", "path": "Root", "page": 0})
    current = normalize_path(state.get("path", "Root"))

    new_path = f"{current}/{folder_name}" if current != "Root" else f"Root/{folder_name}"
    new_path = normalize_path(new_path)
    state["path"] = new_path
    state["page"] = 0

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New subfolder",   callback_data="action:new_folder")],
        [InlineKeyboardButton("⬆ Up",              callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="action:menu")],
    ])
    await update.message.reply_text(
        f"✅ Folder *{format_breadcrumb(new_path)}* created.\n\nYou can create more subfolders or store files inside it.",
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

    filename: str | None = None
    file_type: str | None = None

    if message.document:
        filename  = message.document.file_name
        file_type = "document"
    elif message.photo:
        filename  = f"photo_{copied.message_id}.jpg"
        file_type = "photo"
    elif message.video:
        filename  = message.video.file_name or f"video_{copied.message_id}.mp4"
        file_type = "video"
    elif message.audio:
        filename  = message.audio.file_name or f"audio_{copied.message_id}"
        file_type = "audio"

    db = load_db()
    db[str(copied.message_id)] = {
        "filename":   filename,
        "folder":     folder_path,
        "message_id": copied.message_id,
        "type":       file_type,
    }
    save_db(db)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Store another", callback_data=f"folder:{folder_path.rsplit('/', 1)[-1]}")],
        [InlineKeyboardButton("⬆ Up",            callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu",    callback_data="action:menu")],
    ])
    await message.reply_text(
        f"✅ *{filename}* stored in *{format_breadcrumb(folder_path)}*.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
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

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("menu",  menu_command))
app.add_handler(conv)

if __name__ == "__main__":
    print("Bot running...")
    app.run_polling()