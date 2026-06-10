import json
import os
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
# Config  (swap to os.getenv in production)
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ARCHIVE_CHAT_ID = int(os.environ["ARCHIVE_CHAT_ID"])
OWNER_ID = int(os.environ["OWNER_ID"])

DB_FILE   = Path("metadata.json")
PAGE_SIZE = 10

# ConversationHandler states
WAIT_NEW_FOLDER = 1
WAIT_STORE_FILE = 2

# ---------------------------------------------------------------------------
# In-memory user state
# ---------------------------------------------------------------------------
# user_state[user_id] = {
#   "mode":   "store" | "retrieve" | "delete",
#   "folder": str | None,          # currently selected folder (None = folder list)
#   "page":   int,                 # current pagination page (0-indexed)
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


def get_folders(db: dict) -> list[str]:
    return sorted({item.get("folder", "Root") for item in db.values()})


def get_files_in_folder(db: dict, folder: str) -> list[dict]:
    return [item for item in db.values() if item.get("folder", "Root") == folder]


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
    items: list[tuple[str, str]],   # list of (label, callback_data)
    page: int,
    extra_top_rows: list | None = None,
    extra_bottom_rows: list | None = None,
) -> InlineKeyboardMarkup:
    """
    Renders up to PAGE_SIZE items per page.
    Navigation row (◀ / ▶) is added only when needed.
    extra_top_rows   – inserted before the item rows.
    extra_bottom_rows – inserted after nav row.
    """
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]

    keyboard = list(extra_top_rows or [])

    for label, cb in chunk:
        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    # Navigation row
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


def folder_list_keyboard(folders: list[str], page: int, mode: str) -> InlineKeyboardMarkup:
    """
    In delete mode:
      - Just show folders (📁 <name>), clicking them opens a second menu
        (open / delete) for that folder.
    In store/retrieve:
      - 📁 <folder> -> folder:<name>
    """
    items = [(f"📁 {f}", f"folder:{f}") for f in folders]

    top: list[list[InlineKeyboardButton]] = []
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


def folder_delete_choice_keyboard(folder: str, file_count: int) -> InlineKeyboardMarkup:
    """
    Second-level menu when user taps a folder in delete mode.
    """
    text_count = f" ({file_count} files)" if file_count else " (empty)"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📂 Open folder{text_count}", callback_data=f"action:open_folder_delete:{folder}")],
        [InlineKeyboardButton("🗑 Delete folder", callback_data=f"action:delete_folder_confirm1:{folder}")],
        [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
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
# Helpers to (re-)render views
# ---------------------------------------------------------------------------

async def show_folder_list(query, user_id: int) -> None:
    db      = load_db()
    folders = get_folders(db)
    state   = user_state.setdefault(user_id, {"mode": "retrieve", "folder": None, "page": 0})
    mode    = state["mode"]
    page    = state.get("page", 0)

    if not folders:
        if mode == "store":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")],
                [InlineKeyboardButton("🏠 Main Menu",  callback_data="action:menu")],
            ])
            await query.edit_message_text("No folders yet. Create one first.", reply_markup=kb)
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
            ])
            await query.edit_message_text("No folders found.", reply_markup=kb)
        return

    kb = folder_list_keyboard(folders, page, mode)

    if mode == "store":
        text = "📁 *Select a folder to store into:*"
    elif mode == "delete":
        text = "🗑 *Select a folder to manage:*"
    else:
        text = "📁 *Select a folder to browse:*"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_files_in_folder(query, user_id: int) -> None:
    db    = load_db()
    state = user_state.setdefault(user_id, {"mode": "retrieve", "folder": None, "page": 0})
    folder = state.get("folder")
    page   = state.get("page", 0)
    mode   = state["mode"]

    if not folder:
        return await show_folder_list(query, user_id)

    files = get_files_in_folder(db, folder)

    if not files:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
            [InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")],
        ])
        await query.edit_message_text(
            f"📂 *{folder}* is empty.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    kb = files_in_folder_keyboard(files, page, mode)

    if mode == "delete":
        text = f"🗑 *{folder}* — choose a file to delete:"
    else:
        text = f"📂 *{folder}* — tap a file to receive it:"

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
        user_state[user_id] = {"mode": mode, "folder": None, "page": 0}
        await show_folder_list(query, user_id)
        # Only store mode uses conversation state for file receiving
        return WAIT_STORE_FILE if mode == "store" else ConversationHandler.END

    # ── Pagination ─────────────────────────────────────────────────────────
    if data.startswith("page:"):
        page = int(data.split(":", 1)[1])
        state = user_state.setdefault(user_id, {"mode": "retrieve", "folder": None, "page": 0})
        state["page"] = page
        if state.get("folder"):
            await show_files_in_folder(query, user_id)
        else:
            await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Folder selected ────────────────────────────────────────────────────
    if data.startswith("folder:"):
        folder_name = data.split(":", 1)[1]
        state = user_state.setdefault(user_id, {"mode": "retrieve", "folder": None, "page": 0})
        mode  = state["mode"]

        if mode == "delete":
            # In delete mode, show second-level menu (open / delete) for this folder
            db = load_db()
            count = len(get_files_in_folder(db, folder_name))
            kb = folder_delete_choice_keyboard(folder_name, count)
            await query.edit_message_text(
                f"🗑 Folder *{folder_name}*\nFiles: {count}\n\nWhat do you want to do?",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            # Do not change folder selection yet; only when user chooses open
            return ConversationHandler.END

        # Other modes behave as before
        state["folder"] = folder_name
        state["page"]   = 0

        if mode == "store":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
            ])
            await query.edit_message_text(
                f"📁 Folder set to *{folder_name}*\n\nNow send me the file you want to store.",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return WAIT_STORE_FILE
        else:
            await show_files_in_folder(query, user_id)
            return ConversationHandler.END

    # ── Open folder from delete choice ─────────────────────────────────────
    if data.startswith("action:open_folder_delete:"):
        folder_name = data.split(":", 2)[2]
        state = user_state.setdefault(user_id, {"mode": "delete", "folder": None, "page": 0})
        state["folder"] = folder_name
        state["page"]   = 0
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── First confirmation before delete folder ────────────────────────────
    if data.startswith("action:delete_folder_confirm1:"):
        folder = data.split(":", 2)[2]
        db = load_db()
        count = len(get_files_in_folder(db, folder))
        # Extra warning if too many files
        extra = "\n\n⚠ This folder has a lot of files!" if count >= 20 else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠ Yes, delete this folder", callback_data=f"confirm_delete_folder:{folder}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
        ])
        await query.edit_message_text(
            f"⚠ WARNING\n\nFolder: {folder}\nFiles: {count}{extra}\n\nThis cannot be undone.",
            reply_markup=kb,
        )
        return ConversationHandler.END

    # ── Back to folder list ────────────────────────────────────────────────
    if data == "action:back_folders":
        state = user_state.setdefault(user_id, {"mode": "retrieve", "folder": None, "page": 0})
        state["folder"] = None
        state["page"]   = 0
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Back to files after cancel delete file ─────────────────────────────
    if data == "action:back_files":
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── Delete single file ────────────────────────────────────────────────
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

    # ── Final folder delete (after double confirmation) ────────────────────
    if data.startswith("confirm_delete_folder:"):
        folder = data.split(":", 1)[1]
        db = load_db()
        keys_to_delete: list[str] = []

        for k, item in db.items():
            if item.get("folder", "Root") == folder:
                try:
                    await context.bot.delete_message(ARCHIVE_CHAT_ID, item["message_id"])
                except Exception:
                    pass
                keys_to_delete.append(k)

        for k in keys_to_delete:
            del db[k]

        save_db(db)
        await query.edit_message_text(f"✅ Folder '{folder}' deleted.")
        return ConversationHandler.END

    # ── File selected (retrieve mode) ──────────────────────────────────────
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

    state = user_state.setdefault(user_id, {"mode": "store", "folder": None, "page": 0})
    state["folder"] = folder_name
    state["page"]   = 0

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    await update.message.reply_text(
        f"✅ Folder *{folder_name}* created and selected.\n\nNow send me the file to store.",
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
    folder_name = state.get("folder", "Root")

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
        "folder":     folder_name,
        "message_id": copied.message_id,
        "type":       file_type,
    }
    save_db(db)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Store another", callback_data=f"folder:{folder_name}")],
        [InlineKeyboardButton("🏠 Main Menu",     callback_data="action:menu")],
    ])
    await message.reply_text(
        f"✅ *{filename}* stored in *{folder_name}*.",
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