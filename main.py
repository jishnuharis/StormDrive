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
#   "mode":   "store" | "retrieve" | "delete",
#   "path":   str,           e.g. "Root" or "Root/Videos/Edits"
#   "view":   "folders" | "files",   ← tracks what is currently rendered
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
    """Ensure every path is 'Root' or 'Root/...', no duplicates or trailing slashes."""
    if not path:
        return "Root"
    path = path.strip().strip("/")
    if path == "" or path == "Root":
        return "Root"
    if path.startswith("Root/"):
        path = path[5:]
    parts = [p for p in path.split("/") if p]
    return "Root" if not parts else "Root/" + "/".join(parts)


def get_subfolders_for_path(db: dict, current_path: str) -> list[str]:
    """Immediate child folder names under current_path."""
    current_path = normalize_path(current_path)
    prefix = current_path.rstrip("/") + "/"
    paths = {normalize_path(item.get("folder", "Root")) for item in db.values()}
    children: set[str] = set()
    for p in paths:
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if not rest:
            continue
        children.add(rest.split("/", 1)[0])
    return sorted(children)


def get_files_in_folder(db: dict, folder_path: str) -> list[dict]:
    folder_path = normalize_path(folder_path)
    return [item for item in db.values()
            if normalize_path(item.get("folder", "Root")) == folder_path]


def delete_folder_tree(db: dict, folder_path: str) -> list[str]:
    """Remove all DB entries at or under folder_path. Returns deleted keys."""
    folder_path = normalize_path(folder_path)
    prefix = folder_path.rstrip("/") + "/"
    keys = [
        k for k, item in db.items()
        if normalize_path(item.get("folder", "Root")) in (folder_path,)
        or normalize_path(item.get("folder", "Root")).startswith(prefix)
    ]
    for k in keys:
        del db[k]
    return keys


def format_breadcrumb(path: str) -> str:
    path = normalize_path(path)
    return path.replace("/", " › ")


def parent_path(path: str) -> str:
    path = normalize_path(path)
    if path == "Root":
        return "Root"
    return normalize_path(path.rsplit("/", 1)[0])


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


def folder_list_keyboard(
    children: list[str], page: int, mode: str, path: str
) -> InlineKeyboardMarkup:
    items = [(f"📁 {name}", f"cd:{name}") for name in children]

    top: list[list[InlineKeyboardButton]] = []

    # In store mode: offer to store directly in current folder
    if mode == "store":
        top.append([InlineKeyboardButton("📥 Store here", callback_data="action:store_here")])
        top.append([InlineKeyboardButton("➕ New Folder",  callback_data="action:new_folder")])

    if normalize_path(path) != "Root":
        top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])

    top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])

    return build_paginated_keyboard(items, page, extra_top_rows=top)


def files_in_folder_keyboard(
    files: list[dict], page: int, mode: str
) -> InlineKeyboardMarkup:
    if mode == "delete":
        items = [(f"🗑 {f['filename']}", f"del_file:{f['message_id']}") for f in files]
    else:
        items = [(f"📄 {f['filename']}", f"get_file:{f['message_id']}") for f in files]

    top  = [[InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")]]
    bot_ = [[InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")]]

    return build_paginated_keyboard(items, page, extra_top_rows=top, extra_bottom_rows=bot_)


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
    state = user_state.setdefault(user_id, {"mode": "retrieve", "path": "Root", "view": "folders", "page": 0})
    state["view"] = "folders"

    mode  = state["mode"]
    page  = state.get("page", 0)
    path  = normalize_path(state.get("path", "Root"))

    children = get_subfolders_for_path(db, path)

    top: list[list[InlineKeyboardButton]] = []

    # Store mode always shows "Store here" + "New Folder"
    if mode == "store":
        top.append([InlineKeyboardButton("📥 Store here", callback_data="action:store_here")])
        top.append([InlineKeyboardButton("➕ New Folder",  callback_data="action:new_folder")])

    if path != "Root":
        top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])

    top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])

    if not children:
        if mode == "store":
            text = f"📂 *{format_breadcrumb(path)}* — no subfolders.\n\nStore here or create a new folder:"
        elif mode == "delete":
            text = f"🗑 *{format_breadcrumb(path)}* — no subfolders."
        else:
            text = f"📁 *{format_breadcrumb(path)}* — no subfolders."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(top))
        return

    items = [(f"📁 {name}", f"cd:{name}") for name in children]

    if mode == "store":
        text = f"📁 *{format_breadcrumb(path)}* — choose a folder or store here:"
    elif mode == "delete":
        text = f"🗑 *{format_breadcrumb(path)}* — select a folder to manage:"
    else:
        text = f"📁 *{format_breadcrumb(path)}* — select a folder to browse:"

    kb = build_paginated_keyboard(items, page, extra_top_rows=top)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_files_in_folder(query, user_id: int) -> None:
    db    = load_db()
    state = user_state.setdefault(user_id, {"mode": "retrieve", "path": "Root", "view": "files", "page": 0})
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
                f"🗑 Delete folder '{format_breadcrumb(path)}'",
                callback_data="action:delete_this_folder",
            )])
        extra.append(menu_row)
        await query.edit_message_text(
            f"📂 *{format_breadcrumb(path)}* — no files here.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(extra),
        )
        return

    if mode == "delete":
        # Extra bottom row: delete entire folder
        bot_ = [
            [InlineKeyboardButton(
                f"🗑 Delete entire folder ({len(files)} files)",
                callback_data="action:delete_this_folder",
            )],
            menu_row,
        ]
        items = [(f"🗑 {f['filename']}", f"del_file:{f['message_id']}") for f in files]
        kb    = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=bot_)
        text  = f"🗑 *{format_breadcrumb(path)}* — tap a file to delete it:"
    else:
        items = [(f"📄 {f['filename']}", f"get_file:{f['message_id']}") for f in files]
        kb    = build_paginated_keyboard(items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row])
        text  = f"📂 *{format_breadcrumb(path)}* — tap a file to receive it:"

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_store_prompt(query, user_id: int) -> int:
    """Show the 'send me a file' prompt for store mode."""
    state = user_state.setdefault(user_id, {"mode": "store", "path": "Root", "view": "files", "page": 0})
    state["view"] = "files"
    path = normalize_path(state.get("path", "Root"))

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆ Up",         callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
    ])
    await query.edit_message_text(
        f"📁 Storing into *{format_breadcrumb(path)}*\n\nSend me the file(s) to store.",
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
    state = user_state.setdefault(user_id, {"mode": "retrieve", "path": "Root", "view": "folders", "page": 0})

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
        user_state[user_id] = {"mode": mode, "path": "Root", "view": "folders", "page": 0}
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Pagination ─────────────────────────────────────────────────────────
    # BUG FIX: use state["view"] to decide what to re-render, not always folder list
    if data.startswith("page:"):
        page = int(data.split(":", 1)[1])
        state["page"] = page
        if state.get("view") == "files":
            if state["mode"] == "store":
                # store mode file view = store prompt, no pagination needed; shouldn't happen
                await show_folder_list(query, user_id)
            else:
                await show_files_in_folder(query, user_id)
        else:
            await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Up one level ───────────────────────────────────────────────────────
    if data == "action:up":
        current = normalize_path(state.get("path", "Root"))
        state["path"] = parent_path(current)
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # ── Enter subfolder (cd) ───────────────────────────────────────────────
    if data.startswith("cd:"):
        folder_name = data[3:]
        current     = normalize_path(state.get("path", "Root"))
        new_path    = normalize_path(f"{current}/{folder_name}")
        state["path"] = new_path
        state["page"] = 0

        mode = state["mode"]

        if mode == "delete":
            # Show a choice: open folder OR delete whole tree
            db             = load_db()
            files_here     = len(get_files_in_folder(db, new_path))
            subfolders_here = len(get_subfolders_for_path(db, new_path))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📂 Open folder ({files_here} files, {subfolders_here} subfolders)",
                    callback_data="action:open_del_folder",
                )],
                [InlineKeyboardButton(
                    "🗑 Delete folder + all contents",
                    callback_data="action:ask_del_tree",
                )],
                [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
                [InlineKeyboardButton("🏠 Main Menu",       callback_data="action:menu")],
            ])
            await query.edit_message_text(
                f"🗑 *{format_breadcrumb(new_path)}*\n"
                f"Files: {files_here} · Subfolders: {subfolders_here}\n\nWhat do you want to do?",
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

    # ── New folder: ask for name ───────────────────────────────────────────
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

    # ── Back to file list (after cancel on a delete confirm) ──────────────
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
        except Exception as e:
            await query.message.reply_text(f"⚠️ Could not retrieve file: {e}")
        return ConversationHandler.END

    # ==========================================================================
    # DELETE flow
    # ==========================================================================

    # ── Open folder in delete mode ─────────────────────────────────────────
    if data == "action:open_del_folder":
        state["page"] = 0
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # ── Ask to confirm tree-delete ─────────────────────────────────────────
    if data == "action:ask_del_tree" or data == "action:delete_this_folder":
        path = normalize_path(state.get("path", "Root"))
        db   = load_db()

        prefix = path.rstrip("/") + "/"
        total  = sum(
            1 for item in db.values()
            if normalize_path(item.get("folder", "Root")) == path
            or normalize_path(item.get("folder", "Root")).startswith(prefix)
        )
        warning = "\n\n⚠ This folder has many files!" if total >= 20 else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠ Yes, delete everything", callback_data="action:confirm_del_tree")],
            [InlineKeyboardButton("❌ Cancel",                 callback_data="action:back_folders")],
        ])
        await query.edit_message_text(
            f"⚠ *WARNING*\n\n"
            f"Folder: *{format_breadcrumb(path)}*\n"
            f"Total files: {total}{warning}\n\n"
            f"This cannot be undone.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return ConversationHandler.END

    # ── Confirmed: delete whole tree ───────────────────────────────────────
    if data == "action:confirm_del_tree":
        path = normalize_path(state.get("path", "Root"))
        db   = load_db()
        keys = delete_folder_tree(db, path)
        save_db(db)

        for k in keys:
            try:
                await context.bot.delete_message(ARCHIVE_CHAT_ID, int(k))
            except Exception:
                pass

        # Go up one level after deletion
        state["path"] = parent_path(path)
        state["page"] = 0
        state["view"] = "folders"

        await query.edit_message_text(
            f"✅ *{format_breadcrumb(path)}* and {len(keys)} file(s) deleted.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # ── Delete: single file tapped → confirm ──────────────────────────────
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

    # ── Confirmed: delete single file ─────────────────────────────────────
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

    # Disallow slashes in folder names (would break path logic)
    if "/" in folder_name:
        await update.message.reply_text("Folder name cannot contain '/'. Try again:")
        return WAIT_NEW_FOLDER

    state   = user_state.setdefault(user_id, {"mode": "store", "path": "Root", "view": "folders", "page": 0})
    current = normalize_path(state.get("path", "Root"))
    new_path = normalize_path(f"{current}/{folder_name}")

    state["path"] = new_path
    state["page"] = 0
    state["view"] = "folders"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Store here",   callback_data="action:store_here")],
        [InlineKeyboardButton("➕ New Subfolder", callback_data="action:new_folder")],
        [InlineKeyboardButton("⬆ Up",            callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu",    callback_data="action:menu")],
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

    user_id     = update.effective_user.id
    message     = update.message
    state       = user_state.get(user_id, {})
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
        [InlineKeyboardButton("📥 Store another here", callback_data="action:store_here")],
        [InlineKeyboardButton("⬆ Up",                  callback_data="action:up")],
        [InlineKeyboardButton("🏠 Main Menu",          callback_data="action:menu")],
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

app.add_handler(CommandHandler("start",  start))
app.add_handler(CommandHandler("menu",   menu_command))
app.add_handler(conv)

if __name__ == "__main__":
    print("Bot running...")
    app.run_polling()
