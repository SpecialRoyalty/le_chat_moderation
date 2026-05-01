import os
import re
import time
import asyncio
import psycopg
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.error import BadRequest, RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID = int(os.getenv("GROUP_ID"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}

TZ = ZoneInfo("Europe/Paris")

OPEN_TEXT = "💚 Groupe ouvert, vous pouvez envoyer vos médias <3"
CLOSED_TEXT = "🔒 Groupe fermé. Les messages ne sont pas autorisés pour le moment."


def db():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_messages (
            chat_id BIGINT,
            message_id BIGINT,
            created_at BIGINT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at BIGINT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS banned_words (
            word TEXT PRIMARY KEY
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS joined_users (
            user_id BIGINT PRIMARY KEY,
            joined_at BIGINT
        )
        """)
        conn.execute("""
        INSERT INTO settings(key, value) VALUES
        ('group_open', '0'),
        ('auto_open', '0'),
        ('open_message_id', ''),
        ('closed_message_id', '')
        ON CONFLICT DO NOTHING
        """)


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(%s, %s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
            """,
            (key, str(value)),
        )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_keyboard():
    auto = get_setting("auto_open", "0")

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"Ouverture automatique : {'ON' if auto == '1' else 'OFF'}",
                callback_data="toggle_auto",
            )
        ],
        [
            InlineKeyboardButton("✅ Ouvrir maintenant", callback_data="open_now"),
            InlineKeyboardButton("🔒 Fermer maintenant", callback_data="close_now"),
        ],
        [
            InlineKeyboardButton("🚨 Urgence : tout supprimer", callback_data="emergency")
        ],
        [
            InlineKeyboardButton("➕ Ajouter mot interdit", callback_data="add_word"),
            InlineKeyboardButton("📋 Voir mots interdits", callback_data="list_words"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast utilisateurs", callback_data="broadcast")
        ],
        [
            InlineKeyboardButton("ℹ️ Info", callback_data="info")
        ],
    ])


async def safe_edit(q, text, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


async def track_message_by_id(chat_id: int, message_id: int):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tracked_messages(chat_id, message_id, created_at)
            VALUES(%s, %s, %s)
            """,
            (chat_id, message_id, int(time.time())),
        )


async def track_message(update: Update):
    if not update.message:
        return

    if update.message.chat_id != GROUP_ID:
        return

    await track_message_by_id(update.message.chat_id, update.message.message_id)


async def delete_all_tracked(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        row = conn.execute(
            """
            SELECT MIN(message_id), MAX(message_id)
            FROM tracked_messages
            WHERE chat_id=%s
            """,
            (GROUP_ID,),
        ).fetchone()

    if not row or row[0] is None or row[1] is None:
        print("Suppression terminée : 0 supprimés, 0 échecs")
        return

    min_id, max_id = row

    deleted = 0
    failed = 0

    print(f"🧹 Début suppression range : {min_id} → {max_id}")

    for message_id in range(max_id, min_id - 1, -1):
        try:
            await context.bot.delete_message(GROUP_ID, message_id)
            deleted += 1
            print(f"✅ Supprimé : {message_id}")
            await asyncio.sleep(0.08)

        except RetryAfter as e:
            print(f"⏳ Rate limit sur {message_id}, attente {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
            try:
                await context.bot.delete_message(GROUP_ID, message_id)
                deleted += 1
                print(f"✅ Supprimé après attente : {message_id}")
            except Exception as retry_error:
                failed += 1
                print(f"❌ Échec après attente message_id={message_id} | raison={retry_error}")

        except BadRequest as e:
            failed += 1
            print(f"❌ BadRequest message_id={message_id} | raison={e}")
            await asyncio.sleep(0.05)

        except TimedOut as e:
            failed += 1
            print(f"❌ Timeout message_id={message_id} | raison={e}")
            await asyncio.sleep(0.1)

        except NetworkError as e:
            failed += 1
            print(f"❌ NetworkError message_id={message_id} | raison={e}")
            await asyncio.sleep(0.1)

        except Exception as e:
            failed += 1
            print(f"❌ Erreur inconnue message_id={message_id} | raison={type(e).__name__}: {e}")
            await asyncio.sleep(0.1)

    with db() as conn:
        conn.execute("DELETE FROM tracked_messages WHERE chat_id=%s", (GROUP_ID,))

    print(f"Suppression terminée : {deleted} supprimés, {failed} échecs")


async def send_status_message(context: ContextTypes.DEFAULT_TYPE, text: str, setting_key: str):
    old_open = get_setting("open_message_id", "")
    old_closed = get_setting("closed_message_id", "")

    for old_id in [old_open, old_closed]:
        if old_id:
            try:
                await context.bot.delete_message(GROUP_ID, int(old_id))
                print(f"✅ Ancien message d’état supprimé : {old_id}")
            except Exception as e:
                print(f"❌ Impossible de supprimer ancien message d’état {old_id} | {e}")

    set_setting("open_message_id", "")
    set_setting("closed_message_id", "")

    msg = await context.bot.send_message(GROUP_ID, text)
    set_setting(setting_key, str(msg.message_id))
    await track_message_by_id(GROUP_ID, msg.message_id)

    print(f"📌 Nouveau message d’état : {msg.message_id}")


async def open_group(context: ContextTypes.DEFAULT_TYPE):
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_documents=True,
        can_send_audios=True,
        can_send_voice_notes=True,
        can_send_video_notes=True,
        can_send_other_messages=True,
    )

    await context.bot.set_chat_permissions(GROUP_ID, perms)
    set_setting("group_open", "1")

    await send_status_message(context, OPEN_TEXT, "open_message_id")


async def close_group(context: ContextTypes.DEFAULT_TYPE):
    perms = ChatPermissions(can_send_messages=False)

    await context.bot.set_chat_permissions(GROUP_ID, perms)
    set_setting("group_open", "0")

    await delete_all_tracked(context)
    await send_status_message(context, CLOSED_TEXT, "closed_message_id")


async def emergency(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.set_chat_permissions(
            GROUP_ID,
            ChatPermissions(can_send_messages=False),
        )
    except Exception as e:
        print(f"❌ Erreur fermeture urgence : {e}")

    set_setting("group_open", "0")

    await delete_all_tracked(context)
    await send_status_message(context, CLOSED_TEXT, "closed_message_id")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    with db() as conn:
        conn.execute(
            """
            INSERT INTO users(user_id, username, first_name, created_at)
            VALUES(%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name
            """,
            (user.id, user.username, user.first_name, int(time.time())),
        )

    if is_admin(user.id):
        await update.message.reply_text("Panel admin :", reply_markup=admin_keyboard())
    else:
        await update.message.reply_text(
            "✅ Si le groupe saute, tu auras le nouveau lien ici."
        )


async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    await update.message.reply_text("Panel administrateur :", reply_markup=admin_keyboard())


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    data = q.data

    if data == "toggle_auto":
        current = get_setting("auto_open", "0")
        new_value = "0" if current == "1" else "1"
        set_setting("auto_open", new_value)

        now = datetime.now(TZ)
        should_open = now.hour in [22, 23]

        if new_value == "1" and should_open:
            await open_group(context)
            text = "✅ Ouverture automatique activée.\n\nLe groupe est ouvert maintenant."
        else:
            text = f"✅ Ouverture automatique : {'ON' if new_value == '1' else 'OFF'}"

        await safe_edit(q, text, reply_markup=admin_keyboard())

    elif data == "open_now":
        await open_group(context)
        await safe_edit(q, "✅ Groupe ouvert.", reply_markup=admin_keyboard())

    elif data == "close_now":
        await close_group(context)
        await safe_edit(
            q,
            "🔒 Groupe fermé et messages supprimés.",
            reply_markup=admin_keyboard(),
        )

    elif data == "emergency":
        await emergency(context)
        await safe_edit(
            q,
            "🚨 Suppression d’urgence effectuée.",
            reply_markup=admin_keyboard(),
        )

    elif data == "add_word":
        await safe_edit(
            q,
            "Envoie maintenant :\n\n/addword mot",
            reply_markup=admin_keyboard(),
        )

    elif data == "list_words":
        with db() as conn:
            rows = conn.execute("SELECT word FROM banned_words ORDER BY word").fetchall()

        words = "\n".join(f"- {r[0]}" for r in rows) or "Aucun mot interdit."

        await safe_edit(
            q,
            f"📋 Mots interdits :\n\n{words}",
            reply_markup=admin_keyboard(),
        )

    elif data == "broadcast":
        context.user_data["waiting_broadcast"] = True
        await safe_edit(
            q,
            "📢 Envoie maintenant le message à broadcast.\n\n"
            "Tous les utilisateurs qui ont fait /start le recevront.",
            reply_markup=admin_keyboard(),
        )

    elif data == "info":
        db_status = "❌ Non connectée"
        group_status = "❌ Non branché"

        try:
            with db() as conn:
                conn.execute("SELECT 1")
            db_status = "✅ Connectée"
        except Exception as e:
            db_status = f"❌ Non connectée\nErreur : {e}"

        try:
            chat = await context.bot.get_chat(GROUP_ID)
            member = await context.bot.get_chat_member(GROUP_ID, context.bot.id)

            if member.status in ["administrator", "creator"]:
                group_status = (
                    f"✅ Branché au groupe\n"
                    f"Nom : {chat.title}\n"
                    f"Bot admin : ✅ Oui"
                )
            else:
                group_status = (
                    f"⚠️ Branché au groupe\n"
                    f"Nom : {chat.title}\n"
                    f"Bot admin : ❌ Non"
                )
        except Exception as e:
            group_status = f"❌ Non branché\nErreur : {e}"

        await safe_edit(
            q,
            f"ℹ️ Info bot\n\n"
            f"Base de données : {db_status}\n\n"
            f"Groupe : {group_status}\n\n"
            f"Ouverture automatique : {'ON' if get_setting('auto_open') == '1' else 'OFF'}\n"
            f"État groupe : {'Ouvert' if get_setting('group_open') == '1' else 'Fermé'}",
            reply_markup=admin_keyboard(),
        )


async def addword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage : /addword mot")
        return

    word = " ".join(context.args).lower().strip()

    with db() as conn:
        conn.execute(
            "INSERT INTO banned_words(word) VALUES(%s) ON CONFLICT DO NOTHING",
            (word,),
        )

    await update.message.reply_text(f"✅ Mot interdit ajouté : {word}")


async def delword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage : /delword mot")
        return

    word = " ".join(context.args).lower().strip()

    with db() as conn:
        conn.execute("DELETE FROM banned_words WHERE word=%s", (word,))

    await update.message.reply_text(f"✅ Mot interdit supprimé : {word}")


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if not text.strip():
        await update.message.reply_text("❌ Message vide.")
        return

    with db() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()

    sent = 0
    failed = 0

    for (user_id,) in users:
        try:
            await context.bot.send_message(user_id, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            print(f"❌ Broadcast échec user={user_id} | {e}")

    await update.message.reply_text(
        f"📢 Broadcast terminé.\n\n✅ Envoyés : {sent}\n❌ Échecs : {failed}"
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text("Usage : /broadcast ton message")
        return

    await do_broadcast(update, context, text)


async def member_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if not msg:
        return

    if msg.chat_id == GROUP_ID:
        await track_message(update)

    if msg.new_chat_members:
        for user in msg.new_chat_members:
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO joined_users(user_id, joined_at)
                    VALUES(%s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET joined_at=EXCLUDED.joined_at
                    """,
                    (user.id, int(time.time())),
                )

    try:
        await msg.delete()
        print(f"✅ Message entrée/sortie supprimé : {msg.message_id}")
    except Exception as e:
        print(f"❌ Impossible supprimer entrée/sortie {msg.message_id} | {e}")


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    if not msg:
        return

    print(
        f"MSG reçu | id={msg.message_id} | "
        f"user={user.id if user else None} | "
        f"is_bot={user.is_bot if user else None} | "
        f"text={bool(msg.text)} | caption={bool(msg.caption)} | "
        f"photo={bool(msg.photo)} | video={bool(msg.video)} | "
        f"document={bool(msg.document)} | animation={bool(msg.animation)}"
    )

    if msg.chat_id == GROUP_ID:
        await track_message(update)

    if not user:
        return

    if is_admin(user.id) and context.user_data.get("waiting_broadcast"):
        context.user_data["waiting_broadcast"] = False
        await do_broadcast(update, context, msg.text or msg.caption or "")
        return

    if user.id in ADMIN_IDS:
        return

    if msg.new_chat_members or msg.left_chat_member:
        try:
            await msg.delete()
        except Exception as e:
            print(f"❌ Impossible supprimer message service {msg.message_id} | {e}")
        return

    if get_setting("group_open", "0") != "1":
        try:
            await msg.delete()
            print(f"✅ Message supprimé car groupe fermé : {msg.message_id}")
        except Exception as e:
            print(f"❌ Impossible supprimer message groupe fermé {msg.message_id} | {e}")

        try:
            await context.bot.send_message(
                user.id,
                "🔒 Le groupe est fermé pour le moment. Ton message a été supprimé.",
            )
        except Exception as e:
            print(f"❌ Impossible MP user={user.id} | {e}")

        return

    with db() as conn:
        row = conn.execute(
            "SELECT joined_at FROM joined_users WHERE user_id=%s",
            (user.id,),
        ).fetchone()

    if row and int(time.time()) - row[0] < 60:
        try:
            await msg.delete()
            print(f"✅ Spam nouveau membre supprimé : {msg.message_id}")
        except Exception as e:
            print(f"❌ Impossible supprimer spam nouveau membre {msg.message_id} | {e}")

        try:
            await context.bot.restrict_chat_member(
                GROUP_ID,
                user.id,
                ChatPermissions(can_send_messages=False),
                until_date=int(time.time() + 30 * 24 * 3600),
            )
        except Exception as e:
            print(f"❌ Impossible mute nouveau membre user={user.id} | {e}")

        return

    text = msg.text or msg.caption or ""

    if text and re.search(r"[а-яА-Я\u0600-\u06FF\u4e00-\u9fff]", text):
        try:
            await msg.delete()
            print(f"✅ Message langue interdite supprimé : {msg.message_id}")
        except Exception as e:
            print(f"❌ Impossible supprimer langue interdite {msg.message_id} | {e}")

        try:
            await context.bot.restrict_chat_member(
                GROUP_ID,
                user.id,
                ChatPermissions(can_send_messages=False),
                until_date=int(time.time() + 400 * 24 * 3600),
            )
        except Exception as e:
            print(f"❌ Impossible mute langue interdite user={user.id} | {e}")

        return

    lowered = text.lower()

    if lowered:
        with db() as conn:
            words = [
                r[0]
                for r in conn.execute("SELECT word FROM banned_words").fetchall()
            ]

        if any(word in lowered for word in words):
            try:
                await msg.delete()
                print(f"✅ Mot interdit supprimé : {msg.message_id}")
            except Exception as e:
                print(f"❌ Impossible supprimer mot interdit {msg.message_id} | {e}")

            try:
                await context.bot.restrict_chat_member(
                    GROUP_ID,
                    user.id,
                    ChatPermissions(can_send_messages=False),
                    until_date=int(time.time() + 30 * 24 * 3600),
                )
            except Exception as e:
                print(f"❌ Impossible mute mot interdit user={user.id} | {e}")


async def schedule_checker(context: ContextTypes.DEFAULT_TYPE):
    if get_setting("auto_open", "0") != "1":
        return

    now = datetime.now(TZ)
    should_open = now.hour in [22, 23]
    group_open = get_setting("group_open", "0") == "1"

    if should_open and not group_open:
        await open_group(context)

    if not should_open and group_open:
        await close_group(context)


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("addword", addword))
    app.add_handler(CommandHandler("delword", delword))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    app.add_handler(CallbackQueryHandler(callbacks))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, member_updates))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, member_updates))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate_message))

    app.job_queue.run_repeating(schedule_checker, interval=60, first=5)

    app.run_polling(
        allowed_updates=[
            "message",
            "callback_query",
            "chat_member",
            "my_chat_member",
        ]
    )


if __name__ == "__main__":
    main()
