import os
import logging
import ssl
import pg8000.dbapi
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Database ──────────────────────────────────────────────────────────────────

def get_conn():
    url = urlparse(os.environ["DATABASE_URL"])
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg8000.dbapi.connect(
        host=url.hostname,
        user=url.username,
        password=url.password,
        database=url.path[1:],
        port=url.port or 5432,
        ssl_context=ctx,
    )


def rows_as_dicts(cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS members (
            chat_id   BIGINT NOT NULL,
            user_id   BIGINT NOT NULL,
            username  TEXT,
            full_name TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def upsert_member(chat_id: int, user_id: int, username: str | None, full_name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO members (chat_id, user_id, username, full_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (chat_id, user_id) DO UPDATE SET
            username  = EXCLUDED.username,
            full_name = EXCLUDED.full_name
    """, (chat_id, user_id, username, full_name))
    conn.commit()
    cur.close()
    conn.close()


def remove_member(chat_id: int, user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM members WHERE chat_id = %s AND user_id = %s",
        (chat_id, user_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_members(chat_id: int) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, username, full_name FROM members WHERE chat_id = %s",
        (chat_id,),
    )
    result = rows_as_dicts(cur)
    cur.close()
    conn.close()
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def mention(user: dict) -> str:
    if user["username"]:
        return f"@{user['username']}"
    return f"[{user['full_name']}](tg://user?id={user['user_id']})"


def track_user(update: Update):
    user = update.effective_user
    chat = update.effective_chat
    if user and chat and not user.is_bot and chat.type in ("group", "supergroup"):
        upsert_member(chat.id, user.id, user.username, user.full_name)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)


async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    new_status = result.new_chat_member.status
    user = result.new_chat_member.user

    if user.is_bot:
        return

    if new_status in ("member", "administrator", "restricted"):
        upsert_member(result.chat.id, user.id, user.username, user.full_name)
        logger.info("Added %s to chat %s", user.full_name, result.chat.id)
    elif new_status in ("left", "kicked", "banned"):
        remove_member(result.chat.id, user.id)
        logger.info("Removed %s from chat %s", user.full_name, result.chat.id)


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups.")
        return

    track_user(update)

    members = get_members(chat.id)
    if not members:
        await update.message.reply_text(
            "No members tracked yet. Once people start chatting I'll remember them!"
        )
        return

    CHUNK = 20
    for i in range(0, len(members), CHUNK):
        chunk = members[i : i + CHUNK]
        text = " ".join(mention(m) for m in chunk)
        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups.")
        return

    members = get_members(chat.id)
    await update.message.reply_text(
        f"I'm tracking {len(members)} member(s) in this group.\n"
        "Use /all to mention everyone."
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    await update.message.reply_text(
        "Hi! I'm TagAll Bot.\n\n"
        "Commands:\n"
        "• /all — mention everyone in this group\n"
        "• /list — see how many members I'm tracking\n\n"
        "I track members as they chat. Make me a group admin to also catch "
        "new members who haven't spoken yet."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Set the TELEGRAM_BOT_TOKEN environment variable.")
    if not os.environ.get("DATABASE_URL"):
        raise ValueError("Set the DATABASE_URL environment variable.")

    init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("list", cmd_list))

    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_message)
    )
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
