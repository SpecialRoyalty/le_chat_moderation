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
TRUSTED_IDS = {int(x) for x in os.getenv("TRUSTED_IDS", "").split(",") if x}

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
            user_id BIGINT,
            created_at BIGINT
        )
        """)

        conn.execute("""
        ALTER TABLE tracked_messages
        ADD COLUMN IF NOT EXISTS user_id BIGINT
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
        ('closed_message_id', ''),
        ('ad_enabled', '0'),
        ('ad_text', ''),
        ('last_ad_at', '0')
        ON CONFLICT DO NOTHING
        """)


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=%s",
            (key,),
        ).fetchone()
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


def is_trusted(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in TRUSTED_IDS


def contains_slash_command(text: str) -> bool:
    return bool(re.search(r"(^|\s)/[a-zA-Z0-9_]+", text or ""))


def extract_slash_command(text: str) -> str:
    match = re.search(r"(^|\s)/[a-zA-Z0-9_]+", text or "")
    return match.group(0).strip().lower() if match else ""


def admin_keyboard():
    auto = get_setting("auto_open", "0")
    ad = get_setting("ad_enabled", "0")

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
            InlineKeyboardButton("📢 Broadcast utilisateurs", callback_data="broadcast_users")
        ],
        [
            InlineKeyboardButton("📣 Broadcast groupe", callback_data="broadcast_group")
        ],
        [
            InlineKeyboardButton(
                f"📣 Publicité : {'ON' if ad == '1' else 'OFF'}",
                callback_data="toggle_ad",
            ),
            InlineKeyboardButton("✍️ Texte pub", callback_data="set_ad_text"),
        ],
        [
            InlineKeyboardButton("ℹ️ Info", callback_data="info")
        ],
    ])


async def safe_answer(q):
    try:
        await q.answer()
    except Exception:
        pass


async def safe_edit(q, text, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        if "Query is too old" in str(e):
            return
        raise
    except Exception:
        pass


async def track_message_by_id(chat_id: int, message_id: int, user_id=None):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tracked_messages(chat_id, message_id, user_id, created_at)
            VALUES(%s, %s, %s, %s)
            """,
            (chat_id, message_id, user_id, int(time.time())),
        )


async def track_message(update: Update):
    if not update.message:
        return

    if update.message.chat_id != GROUP_ID:
        return

    user = update.effective_user

    await track_message_by_id(
        update.message.chat_id,
        update.message.message_id,
        user.id if user else None,
    )


def remove_tracked_message(chat_id, message_id):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM tracked_messages
            WHERE chat_id=%s AND message_id=%s
            """,
            (chat_id, message_id),
        )


async def mute_user(context, user_id, seconds, reason=""):
    try:
        await context.bot.restrict_chat_member(
            GROUP_ID,
            user_id,
            ChatPermissions(can_send_messages=False),
            until_date=int(time.time() + seconds),
        )
        print(f"✅ User muté user={user_id} raison={reason}")
    except Exception as e:
        print(f"❌ Impossible mute user={user_id} raison={reason} | {e}")


async def delete_user_messages(context, target_user_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, message_id
            FROM tracked_messages
            WHERE chat_id=%s AND user_id=%s
            ORDER BY message_id DESC
            """,
            (GROUP_ID, target_user_id),
        ).fetchall()

    deleted = 0

    for chat_id, message_id in rows:
        try:
            await context.bot.delete_message(chat_id, message_id)
            remove_tracked_message(chat_id, message_id)
            deleted += 1
            await asyncio.sleep(0.05)

        except BadRequest as e:
            if "not found" in str(e).lower():
                remove_tracked_message(chat_id, message_id)
            else:
                print(f"❌ Delete user msg BadRequest id={message_id} | {e}")

        except Exception as e:
            print(f"❌ Delete user msg id={message_id} | {e}")

    return deleted


async def delete_later(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    message_id = data["message_id"]

    try:
        await context.bot.delete_message(chat_id, message_id)
        remove_tracked_message(chat_id, message_id)
        print(f"✅ Message temporaire supprimé : {message_id}")
    except Exception as e:
        print(f"❌ Impossible supprimer message temporaire {message_id} | {e}")


async def delete_all_tracked(context: ContextTypes.DEFAULT_TYPE):
    async def delete_pass(pass_name):
        with db() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, message_id
                FROM tracked_messages
                WHERE chat_id=%s
                ORDER BY message_id DESC
                """,
                (GROUP_ID,),
            ).fetchall()

        print(f"🧹 {pass_name} : {len(rows)} messages en DB")

        deleted = 0
        failed = 0
        already_gone = 0

        for chat_id, message_id in rows:
            try:
                await context.bot.delete_message(chat_id, message_id)
                remove_tracked_message(chat_id, message_id)
                deleted += 1
                await asyncio.sleep(0.06)

            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)

                try:
                    await context.bot.delete_message(chat_id, message_id)
                    remove_tracked_message(chat_id, message_id)
                    deleted += 1
                except Exception as retry_error:
                    failed += 1
                    print(f"❌ Échec après attente {message_id} | {retry_error}")

            except BadRequest as e:
                err = str(e).lower()

                if "not found" in err or "message to delete not found" in err:
                    remove_tracked_message(chat_id, message_id)
                    already_gone += 1
                else:
                    failed += 1
                    print(f"❌ BadRequest {message_id} | {e}")

                await asyncio.sleep(0.05)

            except (TimedOut, NetworkError) as e:
                failed += 1
                print(f"❌ Réseau/timeout {message_id} | {e}")
                await asyncio.sleep(0.15)

            except Exception as e:
                failed += 1
                print(f"❌ Échec {message_id} | {type(e).__name__}: {e}")
                await asyncio.sleep(0.08)

        print(
            f"{pass_name} terminé : "
            f"{deleted} supprimés, {already_gone} déjà absents, {failed} vrais échecs"
        )

    await delete_pass("PASS 1")
    await asyncio.sleep(3)
    await delete_pass("PASS 2")


async def send_status_message(context: ContextTypes.DEFAULT_TYPE, text: str, setting_key: str):
    old_open = get_setting("open_message_id", "")
    old_closed = get_setting("closed_message_id", "")

    for old_id in [old_open, old_closed]:
        if old_id:
            try:
                await context.bot.delete_message(GROUP_ID, int(old_id))
                remove_tracked_message(GROUP_ID, int(old_id))
            except Exception as e:
                print(f"❌ Impossible supprimer ancien état {old_id} | {e}")

    set_setting("open_message_id", "")
    set_setting("closed_message_id", "")

    msg = await context.bot.send_message(GROUP_ID, text)
    set_setting(setting_key, str(msg.message_id))
    await track_message_by_id(GROUP_ID, msg.message_id, context.bot.id)

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
    await context.bot.set_chat_permissions(
        GROUP_ID,
        ChatPermissions(can_send_messages=False),
    )

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

    if update.effective_chat and update.effective_chat.id == GROUP_ID:
        return

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

    await update.message.reply_text(
        "Panel administrateur :",
        reply_markup=admin_keyboard(),
    )


async def dbcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*), MIN(message_id), MAX(message_id)
            FROM tracked_messages
            WHERE chat_id=%s
            """,
            (GROUP_ID,),
        ).fetchone()

    await update.message.reply_text(
        f"DB tracked_messages\n\n"
        f"Count : {row[0]}\n"
        f"Min ID : {row[1]}\n"
        f"Max ID : {row[2]}\n"
        f"GROUP_ID : {GROUP_ID}"
    )


async def testdelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage : /testdelete 1581")
        return

    try:
        message_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ message_id invalide.")
        return

    try:
        await context.bot.delete_message(GROUP_ID, message_id)
        remove_tracked_message(GROUP_ID, message_id)
        await update.message.reply_text(f"✅ Message {message_id} supprimé.")
    except BadRequest as e:
        await update.message.reply_text(
            f"❌ Impossible de supprimer {message_id}\n\nBadRequest : {e}"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Impossible de supprimer {message_id}\n\nErreur : {type(e).__name__}: {e}"
        )


async def send_group_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if not text.strip():
        await update.message.reply_text("❌ Message vide.")
        return

    msg = await context.bot.send_message(GROUP_ID, text)
    await track_message_by_id(GROUP_ID, msg.message_id, context.bot.id)

    context.job_queue.run_once(
        delete_later,
        when=12 * 60 * 60,
        data={"chat_id": GROUP_ID, "message_id": msg.message_id},
    )

    await update.message.reply_text("✅ Annonce publiée dans le groupe pour 12h.")


async def ad_checker(context: ContextTypes.DEFAULT_TYPE):
    if get_setting("ad_enabled", "0") != "1":
        return

    if get_setting("group_open", "0") != "1":
        return

    ad_text = get_setting("ad_text", "").strip()
    if not ad_text:
        return

    now = int(time.time())
    last_ad_at = int(get_setting("last_ad_at", "0") or "0")

    if now - last_ad_at < 12 * 60:
        return

    msg = await context.bot.send_message(GROUP_ID, ad_text)
    await track_message_by_id(GROUP_ID, msg.message_id, context.bot.id)

    set_setting("last_ad_at", str(now))

    context.job_queue.run_once(
        delete_later,
        when=3 * 60,
        data={"chat_id": GROUP_ID, "message_id": msg.message_id},
    )

    print(f"📣 Publicité envoyée : {msg.message_id}")


async def do_user_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
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

    await do_user_broadcast(update, context, text)


async def trusted_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    if not msg or not user or msg.chat_id != GROUP_ID:
        return

    text = msg.text or msg.caption or ""
    command = extract_slash_command(text)

    if not command:
        return

    await track_message(update)

    try:
        await msg.delete()
        remove_tracked_message(msg.chat_id, msg.message_id)
    except Exception:
        pass

    if command == "/telecharge":
        return

    if command not in ["/supprime", "/ban"]:
        if not is_trusted(user.id):
            await mute_user(context, user.id, 7 * 24 * 3600, reason="commande slash")
        return

    if not is_trusted(user.id):
        await mute_user(context, user.id, 7 * 24 * 3600, reason="commande slash non trusted")
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return

    target = msg.reply_to_message.from_user

    await delete_user_messages(context, target.id)

    try:
        await msg.reply_to_message.delete()
    except Exception:
        pass

    if command == "/ban":
        try:
            await context.bot.ban_chat_member(GROUP_ID, target.id)
            print(f"✅ Ban user={target.id}")
        except Exception as e:
            print(f"❌ Impossible ban user={target.id} | {e}")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

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
        await safe_edit(
            q,
            "🔒 Fermeture lancée.\n\nLe nettoyage continue en arrière-plan.",
            reply_markup=admin_keyboard(),
        )
        context.application.create_task(close_group(context))

    elif data == "emergency":
        await safe_edit(
            q,
            "🚨 Urgence lancée.\n\nLe nettoyage continue en arrière-plan.",
            reply_markup=admin_keyboard(),
        )
        context.application.create_task(emergency(context))

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

    elif data == "broadcast_users":
        context.user_data["waiting_user_broadcast"] = True
        await safe_edit(
            q,
            "📢 Envoie maintenant le message à envoyer aux utilisateurs.\n\n"
            "Tous ceux qui ont fait /start le recevront.",
            reply_markup=admin_keyboard(),
        )

    elif data == "broadcast_group":
        context.user_data["waiting_group_broadcast"] = True
        await safe_edit(
            q,
            "📣 Envoie maintenant l’annonce à publier dans le groupe.\n\n"
            "Elle restera 12h puis sera supprimée.",
            reply_markup=admin_keyboard(),
        )

    elif data == "toggle_ad":
        current = get_setting("ad_enabled", "0")
        new_value = "0" if current == "1" else "1"
        set_setting("ad_enabled", new_value)

        await safe_edit(
            q,
            f"📣 Publicité : {'ON' if new_value == '1' else 'OFF'}",
            reply_markup=admin_keyboard(),
        )

    elif data == "set_ad_text":
        context.user_data["waiting_ad_text"] = True
        await safe_edit(
            q,
            "✍️ Envoie maintenant le texte de la publicité.\n\n"
            "Exemple : N’oubliez pas de partager le groupe ❤️",
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
                group_status = f"✅ Branché au groupe\nNom : {chat.title}\nBot admin : ✅ Oui"
            else:
                group_status = f"⚠️ Branché au groupe\nNom : {chat.title}\nBot admin : ❌ Non"
        except Exception as e:
            group_status = f"❌ Non branché\nErreur : {e}"

        await safe_edit(
            q,
            f"ℹ️ Info bot\n\n"
            f"Base de données : {db_status}\n\n"
            f"Groupe : {group_status}\n\n"
            f"GROUP_ID : {GROUP_ID}\n"
            f"Admins : {len(ADMIN_IDS)}\n"
            f"Trusted : {len(TRUSTED_IDS)}\n"
            f"Ouverture automatique : {'ON' if get_setting('auto_open') == '1' else 'OFF'}\n"
            f"État groupe : {'Ouvert' if get_setting('group_open') == '1' else 'Fermé'}\n"
            f"Publicité : {'ON' if get_setting('ad_enabled') == '1' else 'OFF'}",
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
        remove_tracked_message(msg.chat_id, msg.message_id)
    except Exception as e:
        print(f"❌ Impossible supprimer entrée/sortie {msg.message_id} | {e}")


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    if not msg:
        return

    if msg.chat_id == GROUP_ID:
        await track_message(update)

    if not user:
        return

    if is_admin(user.id) and context.user_data.get("waiting_user_broadcast"):
        context.user_data["waiting_user_broadcast"] = False
        await do_user_broadcast(update, context, msg.text or msg.caption or "")
        return

    if is_admin(user.id) and context.user_data.get("waiting_group_broadcast"):
        context.user_data["waiting_group_broadcast"] = False
        await send_group_broadcast(update, context, msg.text or msg.caption or "")
        return

    if is_admin(user.id) and context.user_data.get("waiting_ad_text"):
        context.user_data["waiting_ad_text"] = False
        set_setting("ad_text", msg.text or msg.caption or "")
        set_setting("last_ad_at", "0")
        await update.message.reply_text("✅ Texte publicité enregistré.")
        return

    if user.id in ADMIN_IDS:
        return

    text = msg.text or msg.caption or ""

    if msg.chat_id == GROUP_ID and contains_slash_command(text):
        await trusted_command_handler(update, context)
        return

    if msg.new_chat_members or msg.left_chat_member:
        try:
            await msg.delete()
            remove_tracked_message(msg.chat_id, msg.message_id)
        except Exception as e:
            print(f"❌ Impossible supprimer message service {msg.message_id} | {e}")
        return

    if get_setting("group_open", "0") != "1":
        try:
            await msg.delete()
            remove_tracked_message(msg.chat_id, msg.message_id)
        except Exception as e:
            print(f"❌ Impossible supprimer message groupe fermé {msg.message_id} | {e}")

        if not user.is_bot:
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
            remove_tracked_message(msg.chat_id, msg.message_id)
        except Exception as e:
            print(f"❌ Impossible supprimer spam nouveau membre {msg.message_id} | {e}")

        await mute_user(
            context,
            user.id,
            30 * 24 * 3600,
            reason="nouveau membre spam",
        )
        return

    if text and re.search(r"[а-яА-Я\u0600-\u06FF\u4e00-\u9fff]", text):
        try:
            await msg.delete()
            remove_tracked_message(msg.chat_id, msg.message_id)
        except Exception as e:
            print(f"❌ Impossible supprimer langue interdite {msg.message_id} | {e}")

        await mute_user(
            context,
            user.id,
            400 * 24 * 3600,
            reason="langue interdite",
        )
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
                remove_tracked_message(msg.chat_id, msg.message_id)
            except Exception as e:
                print(f"❌ Impossible supprimer mot interdit {msg.message_id} | {e}")

            await mute_user(
                context,
                user.id,
                30 * 24 * 3600,
                reason="mot interdit",
            )


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

    # Groupe 0 : intercepte toutes les commandes /... dans le groupe.
    app.add_handler(MessageHandler(filters.COMMAND, trusted_command_handler), group=0)

    # Commandes privées/admin normales.
    app.add_handler(CommandHandler("start", start), group=1)
    app.add_handler(CommandHandler("panel", panel), group=1)
    app.add_handler(CommandHandler("dbcount", dbcount), group=1)
    app.add_handler(CommandHandler("testdelete", testdelete), group=1)
    app.add_handler(CommandHandler("addword", addword), group=1)
    app.add_handler(CommandHandler("delword", delword), group=1)
    app.add_handler(CommandHandler("broadcast", broadcast_command), group=1)

    app.add_handler(CallbackQueryHandler(callbacks), group=1)

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, member_updates), group=1)
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, member_updates), group=1)

    # Messages normaux + commandes au milieu d’une phrase.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate_message), group=2)

    app.job_queue.run_repeating(schedule_checker, interval=60, first=5)
    app.job_queue.run_repeating(ad_checker, interval=60, first=10)

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
