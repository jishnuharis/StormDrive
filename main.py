import json
import os
from pathlib import Path

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ChatMemberUpdated,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
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
#   "mode": "store" | "retrieve" | "delete",
#   "path": str, e.g. "Root" or "Root/Videos/Edits"
#   "view": "folders" | "files",
#   "page": int,
#   "last_btn_msg": int | None, message_id of the last summary-with-buttons
#   "store_count": int, files stored in this session
# }
user_state: dict[int, dict] = {}

# Also track last summary message in DM per user, so clear_last_btn_msg
# edits the correct message even when called from another update.
last_summary_msg_id: dict[int, int] = {}

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
    return [
        item
        for item in db.values()
        if normalize_path(item.get("folder", "Root")) == folder_path
    ]


def count_all_in_tree(db: dict, folder_path: str) -> int:
    """Count all files at or under folder_path."""
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


def format_breadcrumb(path: str) -> str:
    return normalize_path(path).replace("/", " › ")


def parent_path(path: str) -> str:
    path = normalize_path(path)
    if path == "Root":
        return "Root"
    return normalize_path(path.rsplit("/", 1)[0])


def resolve_filename(message: Message, fallback: str) -> str:
    """
    Best-effort filename from a Telegram message.
    Priority: caption (user name) → file_name attr → fallback.
    """
    caption = (message.caption or "").strip()
    if caption:
        return caption

    if message.document and message.document.file_name:
        return message.document.file_name
    if message.video and message.video.file_name:
        return message.video.file_name
    if message.audio and message.audio.file_name:
        return message.audio.file_name
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


async def clear_last_btn_msg(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, state: dict, user_id: int
) -> None:
    """
    Strip the inline keyboard from the previous store-summary message
    so only the last summary has buttons.
    """
    msg_id = state.get("last_btn_msg") or last_summary_msg_id.get(user_id)
    if not msg_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=None,
        )
    except Exception:
        pass  # already edited / deleted / too old
    state["last_btn_msg"] = None
    last_summary_msg_id[user_id] = 0


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
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Store", callback_data="mode:store")],
            [InlineKeyboardButton("📤 Retrieve", callback_data="mode:retrieve")],
            [InlineKeyboardButton("🗑 Delete", callback_data="mode:delete")],
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
            BotCommand("joinme", "Send invite link & make me admin in archive group"),  # NEW
        ]
    )


# ---------------------------------------------------------------------------
# /start & /menu
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
# View helpers (edit inline messages in place)
# ---------------------------------------------------------------------------


async def show_folder_list(query, user_id: int) -> None:
    db = load_db()
    state = user_state.setdefault(
        user_id,
        {
            "mode": "retrieve",
            "path": "Root",
            "view": "folders",
            "page": 0,
            "last_btn_msg": None,
            "store_count": 0,
        },
    )
    state["view"] = "folders"

    mode = state["mode"]
    page = state.get("page", 0)
    path = normalize_path(state.get("path", "Root"))
    children = get_subfolders_for_path(db, path)

    # annotate subfolders with file counts in retrieve/delete
    if mode in ("retrieve", "delete") and children:
        def folder_label(name: str) -> str:
            full = normalize_path(f"{path}/{name}")
            n = count_all_in_tree(db, full)
            return f"📁 {name} ({n})" if n else f"📁 {name}"
        items = [(folder_label(name), f"cd:{name}") for name in children]
    else:
        items = [(f"📁 {name}", f"cd:{name}") for name in children]

    top: list[list[InlineKeyboardButton]] = []

    if mode == "store":
        top.append([InlineKeyboardButton("📥 Store here", callback_data="action:store_here")])
        top.append([InlineKeyboardButton("➕ New Folder", callback_data="action:new_folder")])
    if path != "Root":
        top.append([InlineKeyboardButton("⬆ Up", callback_data="action:up")])
    top.append([InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")])

    total_files = len(get_files_in_folder(db, path))
    file_info = f" · {total_files} file{'s' if total_files != 1 else ''} here" if total_files else ""

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
    state = user_state.setdefault(
        user_id,
        {
            "mode": "retrieve",
            "path": "Root",
            "view": "files",
            "page": 0,
            "last_btn_msg": None,
            "store_count": 0,
        },
    )
    state["view"] = "files"

    path = normalize_path(state.get("path", "Root"))
    page = state.get("page", 0)
    mode = state["mode"]
    files = get_files_in_folder(db, path)

    back_row = [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")]
    menu_row = [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")]

    if not files:
        extra: list[list] = [back_row]
        if mode == "delete":
            extra.append(
                [
                    InlineKeyboardButton(
                        "🗑 Delete empty folder",
                        callback_data="action:delete_this_folder",
                    )
                ]
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
            [
                InlineKeyboardButton(
                    f"🗑 Delete entire folder ({count_label})",
                    callback_data="action:delete_this_folder",
                )
            ],
            menu_row,
        ]
        items = [(f"🗑 {f['filename']}", f"del_file:{f['message_id']}") for f in files]
        kb = build_paginated_keyboard(
            items, page, extra_top_rows=[back_row], extra_bottom_rows=bot_
        )
        text = (
            f"🗑 *{format_breadcrumb(path)}* — {count_label}\n\n"
            "Tap a file to delete it:"
        )
    else:
        items = [(f"📄 {f['filename']}", f"get_file:{f['message_id']}") for f in files]
        kb = build_paginated_keyboard(
            items, page, extra_top_rows=[back_row], extra_bottom_rows=[menu_row]
        )
        text = (
            f"📂 *{format_breadcrumb(path)}* — {count_label}\n\n"
            "Tap a file to receive it:"
        )

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_store_prompt(query, user_id: int) -> int:
    state = user_state.setdefault(
        user_id,
        {
            "mode": "store",
            "path": "Root",
            "view": "files",
            "page": 0,
            "last_btn_msg": None,
            "store_count": 0,
        },
    )
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
        f"📁 *{format_breadcrumb(path)}*\n\nSend file(s) — you can send many in a row.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return WAIT_STORE_FILE


# ---------------------------------------------------------------------------
# Main callback / button handler
# ---------------------------------------------------------------------------


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id != OWNER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return ConversationHandler.END

    data = query.data
    state = user_state.setdefault(
        user_id,
        {
            "mode": "retrieve",
            "path": "Root",
            "view": "folders",
            "page": 0,
            "last_btn_msg": None,
            "store_count": 0,
        },
    )

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
        user_state[user_id] = {
            "mode": mode,
            "path": "Root",
            "view": "folders",
            "page": 0,
            "last_btn_msg": None,
            "store_count": 0,
        }
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # Pagination
    if data.startswith("page:"):
        state["page"] = int(data.split(":", 1)[1])
        if state.get("view") == "files":
            await show_files_in_folder(query, user_id)
        else:
            await show_folder_list(query, user_id)
        return ConversationHandler.END

    # Up one level
    if data == "action:up":
        state["path"] = parent_path(normalize_path(state.get("path", "Root")))
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # Enter subfolder
    if data.startswith("cd:"):
        folder_name = data[3:]
        current = normalize_path(state.get("path", "Root"))
        new_path = normalize_path(f"{current}/{folder_name}")
        state["path"] = new_path
        state["page"] = 0
        mode = state["mode"]

        if mode == "delete":
            db = load_db()
            files_here = len(get_files_in_folder(db, new_path))
            subfolders_here = len(get_subfolders_for_path(db, new_path))
            total_tree = count_all_in_tree(db, new_path)
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"📂 Browse ({files_here} files · {subfolders_here} subfolders)",
                            callback_data="action:open_del_folder",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            f"🗑 Delete all ({total_tree} total files)",
                            callback_data="action:ask_del_tree",
                        )
                    ],
                    [InlineKeyboardButton("◀ Back to Folders", callback_data="action:back_folders")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="action:menu")],
                ]
            )
            await query.edit_message_text(
                f"🗑 *{format_breadcrumb(new_path)}*\n"
                f"Files here: {files_here} · Subfolders: {subfolders_here} · Total in tree: {total_tree}\n\n"
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

    # Store: "Store here"
    if data == "action:store_here":
        return await show_store_prompt(query, user_id)

    # New folder
    if data == "action:new_folder":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
            ]
        )
        await query.edit_message_text(
            f"📁 Creating inside *{format_breadcrumb(state.get('path', 'Root'))}*\n\n"
            "Type the new folder name:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return WAIT_NEW_FOLDER

    # Back to folder list
    if data == "action:back_folders":
        state["page"] = 0
        state["view"] = "folders"
        await show_folder_list(query, user_id)
        return ConversationHandler.END

    # Back to file list
    if data == "action:back_files":
        state["page"] = 0
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    # Retrieve: file tapped
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

    # DELETE flow: open folder
    if data == "action:open_del_folder":
        state["page"] = 0
        state["view"] = "files"
        await show_files_in_folder(query, user_id)
        return ConversationHandler.END

    if data in ("action:ask_del_tree", "action:delete_this_folder"):
        path = normalize_path(state.get("path", "Root"))
        db = load_db()
        total = count_all_in_tree(db, path)
        warn = "\n\n⚠ This folder contains a lot of files!" if total >= 20 else ""
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚠ Yes, delete everything", callback_data="action:confirm_del_tree"
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="action:back_folders")],
            ]
        )
        await query.edit_message_text(
            "⚠ *WARNING*\n\n"
            f"Folder: *{format_breadcrumb(path)}*\n"
            f"Total files to delete: {total}{warn}\n\n"
            "This cannot be undone.",
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
                [
                    InlineKeyboardButton(
                        "✅ Yes, delete", callback_data=f"confirm_del_file:{msg_id}"
                    )
                ],
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
# ConversationHandler steps
# ---------------------------------------------------------------------------


async def receive_new_folder_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        return await deny(update)

    user_id = update.effective_user.id
    folder_name = update.message.text.strip()

    if not folder_name:
        await update.message.reply_text("Folder name cannot be empty. Try again:")
        return WAIT_NEW_FOLDER
    if "/" in folder_name:
        await update.message.reply_text("Folder name cannot contain '/'. Try again:")
        return WAIT_NEW_FOLDER

    state = user_state.setdefault(
        user_id,
        {
            "mode": "store",
            "path": "Root",
            "view": "folders",
            "page": 0,
            "last_btn_msg": None,
            "store_count": 0,
        },
    )
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


async def receive_store_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not authorized(update):
        return await deny(update)

    user_id = update.effective_user.id
    message = update.message
    state = user_state.get(user_id, {})
    folder_path = normalize_path(state.get("path", "Root"))

    if not (message.document or message.photo or message.video or message.audio):
        await message.reply_text("Please send a file (document, photo, video, or audio).")
        return WAIT_STORE_FILE

    copied = await context.bot.copy_message(
        chat_id=ARCHIVE_CHAT_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )

    # Filename resolution
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
    else:
        return WAIT_STORE_FILE

    db = load_db()
    db[str(copied.message_id)] = {
        "filename": filename,
        "folder": folder_path,
        "message_id": copied.message_id,
        "type": file_type,
    }
    save_db(db)

    # Update session counter
    state["store_count"] = state.get("store_count", 0) + 1
    count = state["store_count"]

    # Clear previous summary buttons
    await clear_last_btn_msg(context, message.chat.id, state, user_id)

    # Plain-text ack
    await message.reply_text(
        f"✅ `{filename}` → *{format_breadcrumb(folder_path)}*",
        parse_mode="Markdown",
    )

    # Summary message with buttons
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"📥 Store another ({count} stored)",
                    callback_data="action:store_here",
                )
            ],
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
    last_summary_msg_id[user_id] = btn_msg.message_id

    return WAIT_STORE_FILE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Security / group-management features
# ---------------------------------------------------------------------------

async def joinme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an invite link to the archive group and try to ensure OWNER_ID is/will be admin."""
    if not authorized(update):
        return await deny(update)

    bot = context.bot

    # Create/GGet invite link
    try:
        # get_chat will fail if bot not in group
        chat = await bot.get_chat(ARCHIVE_CHAT_ID)
        # For private groups, we can create an invite link
        invite_link = None
        try:
            invite = await bot.create_chat_invite_link(ARCHIVE_CHAT_ID, creates_join_request=False)
            invite_link = invite.invite_link
        except Exception:
            # Fall back: maybe there is already an existing link
            invite_link = chat.invite_link
    except Exception as e:
        await update.message.reply_text(f"Cannot access archive group: {e}")
        return

    if not invite_link:
        await update.message.reply_text("Could not obtain an invite link for the archive group.")
        return

    # Send the link
    await update.message.reply_text(
        f"🔐 Archive group invite link:\n{invite_link}\n\n"
        "Join this group; the bot will automatically ensure you are an admin and kick anyone else."
    )

    # Try to promote you immediately if you're already a member
    try:
        member = await bot.get_chat_member(ARCHIVE_CHAT_ID, OWNER_ID)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            try:
                await bot.promote_chat_member(
                    chat_id=ARCHIVE_CHAT_ID,
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
    except Exception:
        pass


async def protect_archive_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ChatMemberHandler: whenever anyone joins/leaves the archive group,
    keep only OWNER_ID (and the bot) and ensure OWNER_ID is admin.
    """
    chat_member_update: ChatMemberUpdated = update.chat_member
    chat = chat_member_update.chat

    if chat.id != ARCHIVE_CHAT_ID:
        return

    bot = context.bot
    new_member = chat_member_update.new_chat_member
    user = new_member.user

    # Kick any human that is not OWNER_ID
    if user.id != OWNER_ID and not user.is_bot:
        try:
            await bot.ban_chat_member(chat.id, user.id)
        except Exception:
            pass
        return

    # Ensure OWNER_ID is admin with full perms when he appears/reappears
    if user.id == OWNER_ID:
        try:
            await bot.promote_chat_member(
                chat_id=chat.id,
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
        CommandHandler("menu", menu_command),
        CommandHandler("start", start),
        CommandHandler("cancel", cancel),
    ],
    per_message=False,
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("menu", menu_command))
app.add_handler(CommandHandler("cancel", cancel))
app.add_handler(CommandHandler("joinme", joinme_command))  # NEW

# Security: watch member changes in the archive group
app.add_handler(
    ChatMemberHandler(
        protect_archive_group,
        ChatMemberHandler.CHAT_MEMBER,
    )
)

app.add_handler(conv)

if __name__ == "__main__":
    print("Bot running...")
    app.run_polling()