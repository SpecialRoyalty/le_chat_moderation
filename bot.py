
# Horaires dynamiques V14
def get_schedule_for_day(now):
    wd = now.weekday()

    # samedi
    if wd == 5:
        return {"open_hour": 23, "open_minute": 0, "close_hour": 1, "close_minute": 0}

    # dimanche
    if wd == 6:
        return {"open_hour": 22, "open_minute": 30, "close_hour": 0, "close_minute": 15}

    # semaine
    return {"open_hour": 22, "open_minute": 0, "close_hour": 0, "close_minute": 0}


import os
import re
import asyncio
import hashlib
import tempfile
from io import BytesIO
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from PIL import Image
import cv2

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

APP_VERSION = "FINAL_COMPLETE_V39_SHARE_RANKING"

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").replace("@", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TRUSTED_IDS = [int(x.strip()) for x in os.getenv("TRUSTED_IDS", "").split(",") if x.strip()]
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8080"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Paris")
MAX_HASH_FILE_MB = int(os.getenv("MAX_HASH_FILE_MB", "20"))

TZ = ZoneInfo(TIMEZONE)

db_pool = None
URL_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|bit\.ly/|tinyurl\.com/)", re.I)


# ---------------- DATABASE ----------------

async def init_db():
    global db_pool

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN manquant")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL manquant")
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL manquant")

    print(f"STARTING {APP_VERSION}", flush=True)
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            opened_at TIMESTAMP DEFAULT NOW(),
            closed_at TIMESTAMP
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            user_id BIGINT,
            session_id INTEGER,
            is_bot BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY(chat_id, message_id)
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS system_messages(
            chat_id BIGINT PRIMARY KEY,
            message_id BIGINT,
            kind TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS banned_words(
            word TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS user_violations(
            user_id BIGINT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS media_hashes(
            hash TEXT PRIMARY KEY,
            chat_id BIGINT,
            user_id BIGINT,
            message_id BIGINT,
            media_type TEXT,
            used_for_participation BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS referral_links(
            user_id BIGINT PRIMARY KEY,
            invite_link TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS pending_joins(
            invited_user_id BIGINT PRIMARY KEY,
            referrer_id BIGINT NOT NULL,
            invite_link TEXT,
            joined_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS referrals(
            referrer_id BIGINT NOT NULL,
            invited_user_id BIGINT NOT NULL,
            invite_link TEXT,
            joined_at TIMESTAMP DEFAULT NOW(),
            validated_at TIMESTAMP,
            rewarded BOOLEAN DEFAULT FALSE,
            PRIMARY KEY(referrer_id, invited_user_id)
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS user_rewards(
            user_id BIGINT PRIMARY KEY,
            max_videos_sent INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS reward_campaigns(
            id SERIAL PRIMARY KEY,
            title TEXT DEFAULT 'Rediffusion complète du groupe',
            text TEXT DEFAULT '🎁 Partagez votre lien pour recevoir la rediffusion complète du groupe.',
            photo_file_id TEXT,
            reward_url TEXT,
            objective INTEGER DEFAULT 50,
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS campaign_rewards(
            campaign_id INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            delivered_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY(campaign_id, user_id)
        )
        """)

        await con.execute("""
        CREATE TABLE IF NOT EXISTS private_users(
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            started_at TIMESTAMP DEFAULT NOW(),
            last_seen_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS leaderboard_rank_cache(
            user_id BIGINT PRIMARY KEY,
            last_rank INTEGER,
            last_count INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)

        await con.execute("""
        CREATE TABLE IF NOT EXISTS admin_states(
            user_id BIGINT PRIMARY KEY,
            state TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)

        await con.execute("""
        CREATE TABLE IF NOT EXISTS trusted_actions(
            id SERIAL PRIMARY KEY,
            session_id INTEGER,
            trusted_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            target_user_id BIGINT,
            target_message_id BIGINT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)

        await con.execute("""
        CREATE TABLE IF NOT EXISTS trusted_strikes(
            session_id INTEGER NOT NULL,
            target_user_id BIGINT NOT NULL,
            strikes INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY(session_id, target_user_id)
        )
        """)

        await con.execute("""
        CREATE TABLE IF NOT EXISTS danger_scores(
            user_id BIGINT PRIMARY KEY,
            score INTEGER DEFAULT 0,
            reason TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS banned_hashes(
            hash TEXT PRIMARY KEY,
            media_type TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS media_fingerprints(
            hash TEXT PRIMARY KEY,
            chat_id BIGINT,
            user_id BIGINT,
            message_id BIGINT,
            media_type TEXT,
            used_for_participation BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS participants(
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT NOW(),
            has_participated BOOLEAN DEFAULT FALSE,
            participation_hash TEXT,
            participated_at TIMESTAMP,
            warn_count INTEGER DEFAULT 0,
            last_warned_at TIMESTAMP
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS referrer_abuse(
            referrer_id BIGINT PRIMARY KEY,
            failed_joins INTEGER DEFAULT 0,
            blacklisted BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS reward_links(
            level INTEGER PRIMARY KEY,
            url TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)

        defaults = {
            "moderation": "on",
            "anti_links": "on",
            "anti_photo_mention": "on",
            "anti_repost": "on",
            "auto_schedule": "on",
            "silent_sanctions": "off",
            "raid_mode": "off",
            "participation": "off",
            "kick_non_participants": "off",
            "rules_auto": "off",
            "rules_text": "📌 Règles du groupe : respect, pas de lien, pas de repost, participez avec un média nouveau.",
            "rules_message_id": "0",
            "last_countdown_key": "",
            "ban_report_count": "0",
            "session_deletions": "0",
            "session_exclusions": "0",
            "session_mutes": "0",
            "non_participants_kicked_total": "0",
            "nonparticipant_kicked_today": "0",
            "last_nonparticipant_kick_date": "",
            "group_open": "off",
            "open_hour": "23",
            "close_hour": "1",
            "current_session_id": "0",
            "ad_message_id": "0",
            "ad1_enabled": "off",
            "ad1_text": "",
            "ad2_enabled": "off",
            "ad2_text": "",
            "share_publicity_text": "🤝 Partagez le groupe et montez dans le classement.",
            "share_publicity_photo_file_id": "",
            "private_broadcast_text": "",
            "share_ad_text": "🎁 Partagez votre lien pour recevoir la rediffusion complète du groupe.",
            "share_ad_photo_file_id": "",
            "leaderboard_enabled": "on",
            "vip_min_invites": "40",
            "campaign_default_objective": "50",
        }

        for k, v in defaults.items():
            await con.execute(
                "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING",
                k, v,
            )

        await ensure_active_campaign()

        for level in (1, 10, 50, 60):
            await con.execute("INSERT INTO reward_links(level,url) VALUES($1,'') ON CONFLICT(level) DO NOTHING", level)
        # SCHEMA REPAIR V22
        # Corrige automatiquement les DB Railway ayant gardé d'anciens schémas.
        await con.execute("ALTER TABLE IF EXISTS media_hashes ADD COLUMN IF NOT EXISTS hash TEXT")
        await con.execute("ALTER TABLE IF EXISTS media_hashes ADD COLUMN IF NOT EXISTS chat_id BIGINT")
        await con.execute("ALTER TABLE IF EXISTS media_hashes ADD COLUMN IF NOT EXISTS user_id BIGINT")
        await con.execute("ALTER TABLE IF EXISTS media_hashes ADD COLUMN IF NOT EXISTS message_id BIGINT")
        await con.execute("ALTER TABLE IF EXISTS media_hashes ADD COLUMN IF NOT EXISTS media_type TEXT")
        await con.execute("ALTER TABLE IF EXISTS media_hashes ADD COLUMN IF NOT EXISTS used_for_participation BOOLEAN DEFAULT FALSE")
        await con.execute("ALTER TABLE IF EXISTS media_hashes ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

        await con.execute("ALTER TABLE IF EXISTS banned_hashes ADD COLUMN IF NOT EXISTS hash TEXT")
        await con.execute("ALTER TABLE IF EXISTS banned_hashes ADD COLUMN IF NOT EXISTS media_type TEXT")
        await con.execute("ALTER TABLE IF EXISTS banned_hashes ADD COLUMN IF NOT EXISTS reason TEXT")
        await con.execute("ALTER TABLE IF EXISTS banned_hashes ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

        # Nettoyage des lignes inutilisables avant création des index uniques.
        await con.execute("DELETE FROM media_hashes WHERE hash IS NULL OR hash = ''")
        await con.execute("DELETE FROM banned_hashes WHERE hash IS NULL OR hash = ''")

        # Suppression doublons si ancienne table avait plusieurs fois le même hash.
        await con.execute("""
        DELETE FROM media_hashes a
        USING media_hashes b
        WHERE a.ctid < b.ctid AND a.hash = b.hash
        """)
        await con.execute("""
        DELETE FROM banned_hashes a
        USING banned_hashes b
        WHERE a.ctid < b.ctid AND a.hash = b.hash
        """)

        # Contraintes uniques requises pour ON CONFLICT(hash).
        await con.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'media_hashes_hash_unique'
            ) THEN
                ALTER TABLE media_hashes ADD CONSTRAINT media_hashes_hash_unique UNIQUE(hash);
            END IF;
        END $$;
        """)
        await con.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'banned_hashes_hash_unique'
            ) THEN
                ALTER TABLE banned_hashes ADD CONSTRAINT banned_hashes_hash_unique UNIQUE(hash);
            END IF;
        END $$;
        """)

        # Contraintes supplémentaires pour les autres ON CONFLICT.
        await con.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'settings_key_unique'
            ) THEN
                ALTER TABLE settings ADD CONSTRAINT settings_key_unique UNIQUE(key);
            END IF;
        END $$;
        """)
        await con.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'reward_links_level_unique'
            ) THEN
                ALTER TABLE reward_links ADD CONSTRAINT reward_links_level_unique UNIQUE(level);
            END IF;
        END $$;
        """)
        await con.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'referral_links_user_unique'
            ) THEN
                ALTER TABLE referral_links ADD CONSTRAINT referral_links_user_unique UNIQUE(user_id);
            END IF;
        END $$;
        """)
        await con.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'user_rewards_user_unique'
            ) THEN
                ALTER TABLE user_rewards ADD CONSTRAINT user_rewards_user_unique UNIQUE(user_id);
            END IF;
        END $$;
        """)

        await con.execute("ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS vip_until TIMESTAMP")
        await con.execute("ALTER TABLE IF EXISTS participants ADD COLUMN IF NOT EXISTS vip_until TIMESTAMP")

        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS title TEXT DEFAULT 'Rediffusion complète du groupe'")
        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS text TEXT DEFAULT '🎁 Partagez votre lien pour recevoir la rediffusion complète du groupe.'")
        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS photo_file_id TEXT")
        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS reward_url TEXT")
        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS objective INTEGER DEFAULT 50")
        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE")
        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
        await con.execute("ALTER TABLE IF EXISTS reward_campaigns ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()")

        await con.execute("ALTER TABLE IF EXISTS private_users ADD COLUMN IF NOT EXISTS username TEXT")
        await con.execute("ALTER TABLE IF EXISTS private_users ADD COLUMN IF NOT EXISTS first_name TEXT")
        await con.execute("ALTER TABLE IF EXISTS private_users ADD COLUMN IF NOT EXISTS last_name TEXT")
        await con.execute("ALTER TABLE IF EXISTS private_users ADD COLUMN IF NOT EXISTS started_at TIMESTAMP DEFAULT NOW()")
        await con.execute("ALTER TABLE IF EXISTS private_users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP DEFAULT NOW()")
        await con.execute("ALTER TABLE IF EXISTS leaderboard_rank_cache ADD COLUMN IF NOT EXISTS last_rank INTEGER")
        await con.execute("ALTER TABLE IF EXISTS leaderboard_rank_cache ADD COLUMN IF NOT EXISTS last_count INTEGER DEFAULT 0")
        await con.execute("ALTER TABLE IF EXISTS leaderboard_rank_cache ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()")

        tables = await con.fetch("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public'
        ORDER BY table_name
        """)
        print("TABLES:", [r["table_name"] for r in tables], flush=True)


async def get_setting(key, default=None):
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else default


async def set_setting(key, value):
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO settings(key,value) VALUES($1,$2)
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, key, str(value))


async def count_table(table):
    async with db_pool.acquire() as con:
        return await con.fetchval(f"SELECT COUNT(*) FROM {table}")


async def current_session_id():
    try:
        return int(await get_setting("current_session_id", "0"))
    except Exception:
        return 0


async def create_session():
    async with db_pool.acquire() as con:
        sid = await con.fetchval("INSERT INTO sessions(chat_id) VALUES($1) RETURNING id", GROUP_ID)
    await set_setting("current_session_id", sid)
    return sid


async def close_session():
    sid = await current_session_id()
    if sid:
        async with db_pool.acquire() as con:
            await con.execute("UPDATE sessions SET closed_at=NOW() WHERE id=$1", sid)
    await set_setting("current_session_id", "0")
    return sid


async def save_message(chat_id, message_id, user_id=None, is_bot=False, session_id=None):
    if session_id is None:
        session_id = await current_session_id()
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO messages(chat_id,message_id,user_id,is_bot,session_id)
        VALUES($1,$2,$3,$4,$5)
        ON CONFLICT DO NOTHING
        """, chat_id, message_id, user_id, is_bot, session_id)


# ---------------- HELPERS ----------------

def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_trusted_id(user_id):
    return user_id in ADMIN_IDS or user_id in TRUSTED_IDS


async def is_group_admin(context, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(GROUP_ID, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def led(value):
    return "🟢 ON" if value == "on" else "🔴 OFF"


def reward_count(valid_count):
    if valid_count >= 40:
        return 60
    if valid_count >= 30:
        return 50
    if valid_count >= 5:
        return 10
    if valid_count >= 1:
        return 1
    return 0


def bot_start_url():
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start=getlink"
    return "https://t.me/"


async def is_silent():
    return await get_setting("silent_sanctions", "off") == "on"

async def add_danger(user_id, points, reason):
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO danger_scores(user_id,score,reason,updated_at)
        VALUES($1,$2,$3,NOW())
        ON CONFLICT(user_id) DO UPDATE SET score=danger_scores.score+$2, reason=$3, updated_at=NOW()
        """, user_id, points, reason)


async def increment_session_counter(key: str, amount: int = 1):
    current = int(await get_setting(key, "0") or "0")
    await set_setting(key, current + amount)


async def reset_session_moderation_counters():
    await set_setting("session_deletions", "0")
    await set_setting("session_exclusions", "0")
    await set_setting("session_mutes", "0")


async def increment_ban_count():
    current = int(await get_setting("ban_report_count", "0") or "0")
    await set_setting("ban_report_count", current + 1)

def message_has_media(msg):
    return bool(msg.photo or msg.video or msg.animation or msg.document)

def message_is_photo_or_video(msg):
    return bool(msg.photo or msg.video)

def media_type(msg):
    if msg.photo: return "photo"
    if msg.video: return "video"
    if msg.animation: return "animation"
    if msg.document: return "document"
    return "unknown"

def _average_hash_image(img: Image.Image) -> str | None:
    try:
        img = img.convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for p in pixels:
            bits = (bits << 1) | (1 if p >= avg else 0)
        return f"{bits:016x}"
    except Exception as e:
        print(f"AHASH SKIPPED: {e}", flush=True)
        return None


def _average_hash_from_bytes(data: bytes) -> str | None:
    try:
        return _average_hash_image(Image.open(BytesIO(data)))
    except Exception as e:
        print(f"AHASH SKIPPED: {e}", flush=True)
        return None


def _hamming_hex(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return 999


async def _download_file_bytes_limited(context, file_id: str, purpose: str = "media") -> bytes | None:
    try:
        tg_file = await context.bot.get_file(file_id)
        size = getattr(tg_file, "file_size", None)
        if size and size > MAX_HASH_FILE_MB * 1024 * 1024:
            print(f"HASH SKIPPED: file too big for {purpose} ({size} bytes > {MAX_HASH_FILE_MB} MB)", flush=True)
            return None
        return bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        if "file is too big" in str(e).lower():
            print(f"HASH SKIPPED: file too big for {purpose}", flush=True)
            return None
        print(f"HASH SKIPPED: {purpose} download failed: {e}", flush=True)
        return None


def _first_frame_ahash_from_video_bytes(data: bytes) -> str | None:
    """
    V30_FRAMEHASH:
    extrait la toute première frame réelle de la vidéo et calcule un aHash visuel.
    C'est beaucoup plus fiable que SHA du fichier vidéo complet.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        cap = cv2.VideoCapture(tmp_path)
        ok, frame = cap.read()
        cap.release()

        if not ok or frame is None:
            print("VIDEO FRAME HASH SKIPPED: first frame unavailable", flush=True)
            return None

        # OpenCV = BGR ; PIL = RGB
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        return _average_hash_image(img)

    except Exception as e:
        print(f"VIDEO FRAME HASH SKIPPED: {e}", flush=True)
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _media_file_ids(msg):
    main_file_id = None
    unique_id = None

    if msg.photo:
        main_file_id = msg.photo[-1].file_id
        unique_id = msg.photo[-1].file_unique_id
    elif msg.video:
        main_file_id = msg.video.file_id
        unique_id = msg.video.file_unique_id
    elif msg.animation:
        main_file_id = msg.animation.file_id
        unique_id = msg.animation.file_unique_id
    elif msg.document:
        main_file_id = msg.document.file_id
        unique_id = msg.document.file_unique_id

    return main_file_id, unique_id


async def media_fingerprints_from_message(context, msg) -> list[str]:
    """
    Nouvelle logique :
    - photo : hash visuel de l'image
    - vidéo/mp4/mov : hash visuel de la toute première frame
    - file_unique_id et SHA exact sont conservés en bonus
    """
    main_file_id, unique_id = _media_file_ids(msg)
    keys = []

    if unique_id:
        keys.append(f"fu:{unique_id}")

    if not main_file_id:
        return keys

    data = await _download_file_bytes_limited(context, main_file_id, "media")
    if not data:
        return keys

    # Bonus exact : utile si c'est vraiment le même fichier Telegram.
    keys.append("sha:" + hashlib.sha256(data).hexdigest())

    if msg.photo:
        ah = _average_hash_from_bytes(data)
        if ah:
            keys.insert(0, "imgahash:" + ah)

    elif msg.video or msg.animation or msg.document:
        # Pour document .mp4/.mov envoyé comme fichier, on tente aussi vidéo.
        vh = _first_frame_ahash_from_video_bytes(data)
        if vh:
            keys.insert(0, "vidframe0:" + vh)

    out = []
    seen = set()
    for k in keys:
        if k and k not in seen:
            out.append(k)
            seen.add(k)
    return out


async def media_hash_from_message(context, msg):
    keys = await media_fingerprints_from_message(context, msg)
    return keys[0] if keys else None


async def find_matching_banned_media(keys: list[str]):
    if not keys:
        return None

    async with db_pool.acquire() as con:
        exact = await con.fetchrow(
            "SELECT hash FROM banned_hashes WHERE hash = ANY($1::text[]) LIMIT 1",
            keys,
        )
        if exact:
            return exact["hash"]

        # Matching approximatif pour hash visuel photo / première frame vidéo.
        visual_keys = []
        for k in keys:
            if k.startswith("imgahash:") or k.startswith("vidframe0:"):
                prefix, val = k.split(":", 1)
                visual_keys.append((prefix, val))

        for prefix, val in visual_keys:
            rows = await con.fetch("SELECT hash FROM banned_hashes WHERE hash LIKE $1", prefix + ":%")
            for r in rows:
                stored = r["hash"].split(":", 1)[1]
                if _hamming_hex(stored, val) <= 6:
                    return r["hash"]

    return None


async def find_matching_repost_media(keys: list[str]):
    if not keys:
        return None

    async with db_pool.acquire() as con:
        exact = await con.fetchrow("""
            SELECT hash FROM media_hashes
            WHERE hash = ANY($1::text[])
              AND created_at > NOW() - INTERVAL '10 days'
            LIMIT 1
        """, keys)
        if exact:
            return exact["hash"]

        visual_keys = []
        for k in keys:
            if k.startswith("imgahash:") or k.startswith("vidframe0:"):
                prefix, val = k.split(":", 1)
                visual_keys.append((prefix, val))

        for prefix, val in visual_keys:
            rows = await con.fetch("""
                SELECT hash FROM media_hashes
                WHERE hash LIKE $1
                  AND created_at > NOW() - INTERVAL '10 days'
            """, prefix + ":%")
            for r in rows:
                stored = r["hash"].split(":", 1)[1]
                if _hamming_hex(stored, val) <= 6:
                    return r["hash"]

    return None


async def insert_media_fingerprints(keys: list[str], chat_id: int, user_id: int, message_id: int, mtype: str, used_for_participation: bool):
    if not keys:
        return
    async with db_pool.acquire() as con:
        for k in keys:
            await con.execute("""
            INSERT INTO media_hashes(hash,chat_id,user_id,message_id,media_type,used_for_participation)
            VALUES($1,$2,$3,$4,$5,$6)
            ON CONFLICT(hash) DO NOTHING
            """, k, chat_id, user_id, message_id, mtype, used_for_participation)


async def insert_banned_media_fingerprints(keys: list[str], mtype: str, reason: str):
    if not keys:
        return
    async with db_pool.acquire() as con:
        for k in keys:
            await con.execute("""
            INSERT INTO banned_hashes(hash,media_type,reason)
            VALUES($1,$2,$3)
            ON CONFLICT(hash) DO UPDATE SET reason=$3
            """, k, mtype, reason)
            await con.execute("DELETE FROM media_hashes WHERE hash=$1", k)


async def reward_links_ready():
    async with db_pool.acquire() as con:
        c = await con.fetchval("SELECT COUNT(*) FROM reward_links WHERE level IN (1,10,50,60) AND COALESCE(url,'') <> ''")
    return c == 4

async def upsert_participant(user):
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO participants(user_id,username,first_name,joined_at)
        VALUES($1,$2,$3,NOW())
        ON CONFLICT(user_id) DO UPDATE SET username=$2, first_name=$3
        """, user.id, user.username, user.first_name)

async def has_participated(user_id):
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT has_participated FROM participants WHERE user_id=$1", user_id)
    return bool(row and row['has_participated'])

async def mark_participated(user_id, media_hash):
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO participants(user_id,has_participated,participation_hash,participated_at)
        VALUES($1,TRUE,$2,NOW())
        ON CONFLICT(user_id) DO UPDATE SET has_participated=TRUE, participation_hash=$2, participated_at=NOW()
        """, user_id, media_hash)

async def delete_later(context, chat_id, message_id, seconds=45):
    await asyncio.sleep(seconds)
    await delete_message_safe(context, chat_id, message_id)

async def send_temp_message(context, chat_id, text, seconds=45):
    msg = await context.bot.send_message(chat_id, text)
    context.application.create_task(delete_later(context, chat_id, msg.message_id, seconds))
    return msg


async def delete_message_safe(context, chat_id, message_id):
    try:
        await context.bot.delete_message(chat_id, message_id)
        try:
            async with db_pool.acquire() as con:
                await con.execute(
                    "DELETE FROM messages WHERE chat_id=$1 AND message_id=$2",
                    chat_id,
                    message_id,
                )
        except Exception as db_e:
            print(f"DELETE SQL CLEANUP FAILED {message_id}: {db_e}", flush=True)
        return True

    except BadRequest as e:
        err = str(e).lower()
        if (
            "message to delete not found" in err
            or "message can't be deleted" in err
            or "message identifier is not specified" in err
            or "message not found" in err
            or "not found" in err
        ):
            try:
                async with db_pool.acquire() as con:
                    await con.execute(
                        "DELETE FROM messages WHERE chat_id=$1 AND message_id=$2",
                        chat_id,
                        message_id,
                    )
            except Exception as db_e:
                print(f"DELETE SQL CLEANUP FAILED {message_id}: {db_e}", flush=True)
            print(f"DELETE SKIPPED/CLEANED {message_id}: {e}", flush=True)
            return False

        print(f"DELETE FAILED {message_id}: {e}", flush=True)
        return False

    except Exception as e:
        print(f"DELETE FAILED {message_id}: {e}", flush=True)
        return False


async def delete_last_system_message(context):
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT message_id FROM system_messages WHERE chat_id=$1", GROUP_ID)

    if row and row["message_id"]:
        await delete_message_safe(context, GROUP_ID, row["message_id"])

    async with db_pool.acquire() as con:
        await con.execute("DELETE FROM system_messages WHERE chat_id=$1", GROUP_ID)


async def send_system_message(context, text, kind, record_in_session=True, reply_markup=None):
    await delete_last_system_message(context)
    msg = await context.bot.send_message(GROUP_ID, text, reply_markup=reply_markup)

    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO system_messages(chat_id,message_id,kind,created_at)
        VALUES($1,$2,$3,NOW())
        ON CONFLICT(chat_id) DO UPDATE SET message_id=$2, kind=$3, created_at=NOW()
        """, GROUP_ID, msg.message_id, kind)

    if record_in_session:
        await save_message(GROUP_ID, msg.message_id, None, True)

    return msg


async def set_admin_state(user_id, state):
    async with db_pool.acquire() as con:
        if state is None:
            await con.execute("DELETE FROM admin_states WHERE user_id=$1", user_id)
        else:
            await con.execute("""
            INSERT INTO admin_states(user_id,state,updated_at)
            VALUES($1,$2,NOW())
            ON CONFLICT(user_id) DO UPDATE SET state=$2, updated_at=NOW()
            """, user_id, state)


async def get_admin_state(user_id):
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT state FROM admin_states WHERE user_id=$1", user_id)
        return row["state"] if row else None


# ---------------- ADMIN UI ----------------

async def main_keyboard():
    moderation = await get_setting("moderation", "off")
    anti_links = await get_setting("anti_links", "off")
    anti_photo = await get_setting("anti_photo_mention", "off")
    anti_repost = await get_setting("anti_repost", "off")
    auto_schedule = await get_setting("auto_schedule", "off")
    silent = await get_setting("silent_sanctions", "off")
    raid = await get_setting("raid_mode", "off")
    participation = await get_setting("participation", "off")
    kick_np = await get_setting("kick_non_participants", "off")
    rules_auto = await get_setting("rules_auto", "off")
    ad1_enabled = await get_setting("ad1_enabled", "off")
    ad2_enabled = await get_setting("ad2_enabled", "off")
    leaderboard_enabled = await get_setting("leaderboard_enabled", "on")

    links_ok = await reward_links_ready()
    pub_label = "📢 Publier publicité" if links_ok else "📢 Publicité bloquée : liens manquants"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🛡️ Modération {led(moderation)}", callback_data="toggle:moderation")],
        [InlineKeyboardButton(f"🔗 Anti-liens {led(anti_links)}", callback_data="toggle:anti_links")],
        [InlineKeyboardButton(f"🖼️ Photo mention {led(anti_photo)}", callback_data="toggle:anti_photo_mention")],
        [InlineKeyboardButton(f"♻️ Anti-repost {led(anti_repost)}", callback_data="toggle:anti_repost")],
        [InlineKeyboardButton(f"⏰ Auto horaires {led(auto_schedule)}", callback_data="toggle:auto_schedule")],
        [InlineKeyboardButton(f"👁️ Sanctions silencieuses {led(silent)}", callback_data="toggle_silent")],
        [InlineKeyboardButton(f"🚨 Mode RAID {led(raid)}", callback_data="toggle_raid")],
        [InlineKeyboardButton(f"🎭 Participation {led(participation)}", callback_data="toggle:participation")],
        [InlineKeyboardButton(f"🥾 Kick non-participants {led(kick_np)}", callback_data="toggle:kick_non_participants")],
        [InlineKeyboardButton(f"📌 Règles auto {led(rules_auto)}", callback_data="toggle:rules_auto")],
        [
            InlineKeyboardButton("🟢 Ouvrir session", callback_data="open_group"),
            InlineKeyboardButton("🔴 Fermer + effacer", callback_data="close_group"),
        ],
        [InlineKeyboardButton("🚫 Mots interdits", callback_data="words_menu")],
        [InlineKeyboardButton("📌 Modifier règles", callback_data="rules_set")],
        [InlineKeyboardButton("📣 Broadcast groupe", callback_data="broadcast_set")],
        [InlineKeyboardButton("🚫 Ban hash", callback_data="ban_hash_set")],
        [InlineKeyboardButton("🔗 Liens récompenses", callback_data="reward_links_menu")],
        [InlineKeyboardButton(f"📢 Pub 1 {led(ad1_enabled)}", callback_data="toggle:ad1_enabled"), InlineKeyboardButton("✏️ Texte Pub 1", callback_data="set_ad1_text")],
        [InlineKeyboardButton(f"📢 Pub 2 {led(ad2_enabled)}", callback_data="toggle:ad2_enabled"), InlineKeyboardButton("✏️ Texte Pub 2", callback_data="set_ad2_text")],
        [InlineKeyboardButton("📣 Publicité partage", callback_data="share_publicity_menu")],
        [InlineKeyboardButton(f"🏆 Classement {led(leaderboard_enabled)}", callback_data="toggle:leaderboard_enabled")],
        [InlineKeyboardButton(pub_label, callback_data="publish_ad" if links_ok else "publish_ad_locked")],
        [InlineKeyboardButton("📊 Stats parrainage", callback_data="ref_stats")],
        [InlineKeyboardButton("📣 Relancer non-participants", callback_data="warn_non_participants")],
        [InlineKeyboardButton("ℹ️ Info système", callback_data="info")],
    ])


async def words_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Ajouter un mot", callback_data="word_add"),
            InlineKeyboardButton("➖ Supprimer un mot", callback_data="word_delete"),
        ],
        [InlineKeyboardButton("📋 Voir les mots", callback_data="word_list")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="info")],
    ])


async def reward_links_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Modifier lien 1 vidéo", callback_data="set_reward_link:1")],
        [InlineKeyboardButton("Modifier lien 10 vidéos", callback_data="set_reward_link:10")],
        [InlineKeyboardButton("Modifier lien 50 vidéos", callback_data="set_reward_link:50")],
        [InlineKeyboardButton("Modifier lien 60 vidéos", callback_data="set_reward_link:60")],
        [InlineKeyboardButton("📋 Voir liens", callback_data="show_reward_links")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="info")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="info")]])


async def panel_text(extra=""):
    links_ready = await reward_links_ready() if "reward_links_ready" in globals() else False

    async with db_pool.acquire() as con:
        msg_count = await con.fetchval("SELECT COUNT(*) FROM messages")
        words = await con.fetchval("SELECT COUNT(*) FROM banned_words")
        valid_refs = await con.fetchval("SELECT COUNT(*) FROM referrals WHERE validated_at IS NOT NULL")
        links = await con.fetchval("SELECT COUNT(*) FROM referral_links")
        group_open = await con.fetchval("SELECT value FROM settings WHERE key='group_open'")
        moderation = await con.fetchval("SELECT value FROM settings WHERE key='moderation'")
        anti_links = await con.fetchval("SELECT value FROM settings WHERE key='anti_links'")
        anti_photo = await con.fetchval("SELECT value FROM settings WHERE key='anti_photo_mention'")
        anti_repost = await con.fetchval("SELECT value FROM settings WHERE key='anti_repost'")
        auto_schedule = await con.fetchval("SELECT value FROM settings WHERE key='auto_schedule'")
        silent = await con.fetchval("SELECT value FROM settings WHERE key='silent_sanctions'") if await table_has_setting("silent_sanctions") else "off"
        raid = await con.fetchval("SELECT value FROM settings WHERE key='raid_mode'") if await table_has_setting("raid_mode") else "off"
        participation = await con.fetchval("SELECT value FROM settings WHERE key='participation'") if await table_has_setting("participation") else "off"
        kick_np = await con.fetchval("SELECT value FROM settings WHERE key='kick_non_participants'") if await table_has_setting("kick_non_participants") else "off"
        kicked_total = await con.fetchval("SELECT value FROM settings WHERE key='non_participants_kicked_total'") if await table_has_setting("non_participants_kicked_total") else "0"
        rules_auto = await con.fetchval("SELECT value FROM settings WHERE key='rules_auto'") if await table_has_setting("rules_auto") else "off"

        non_participants = 0
        banned_hashes = 0
        try:
            non_participants = await con.fetchval("SELECT COUNT(*) FROM participants WHERE has_participated=FALSE")
        except Exception:
            pass
        try:
            banned_hashes = await con.fetchval("SELECT COUNT(*) FROM banned_hashes")
        except Exception:
            pass

    sid = await current_session_id()

    text = (
        "⚙️ PANEL ADMIN\n\n"
        f"🧩 Version : {APP_VERSION}\n"
        "🗄️ PostgreSQL : ✅ branchée\n"
        f"👥 Groupe : {'✅' if GROUP_ID else '❌'} branché\n"
        f"🚪 Groupe ouvert : {led(group_open)}\n"
        f"🧾 Session active : {sid if sid else 'aucune'}\n\n"
        f"🛡️ Modération : {led(moderation)}\n"
        f"🔗 Anti-liens : {led(anti_links)}\n"
        f"🖼️ Photo mention : {led(anti_photo)}\n"
        f"♻️ Anti-repost : {led(anti_repost)}\n"
        f"⏰ Auto horaires : {led(auto_schedule)}\n"
        f"👁️ Sanctions silencieuses : {led(silent)}\n"
        f"🚨 Mode RAID : {led(raid)}\n"
        f"🎭 Participation : {led(participation)}\n"
        f"🥾 Kick non-participants : {led(kick_np)}\n"
        f"🥾 Déjà supprimés non-participation : {kicked_total}\n"
        f"📌 Règles auto : {led(rules_auto)}\n\n"
        f"🔗 Liens récompenses : {'✅ complets' if links_ready else '❌ incomplets'}\n"
        f"💬 Messages session stockés : {msg_count}\n"
        f"🚫 Mots interdits : {words}\n"
        f"🚫 Média interdits : {banned_hashes}\n"
        f"🎭 Non-participants : {non_participants}\n"
        f"🔗 Liens privés : {links}\n"
        f"✅ Parrainages validés : {valid_refs}\n"
    )
    if extra:
        text += f"\n✅ Dernière action : {extra}"
    return text


async def table_has_setting(key):
    try:
        value = await get_setting(key, None)
        return value is not None
    except Exception:
        return False



async def safe_answer_callback(q):
    try:
        await safe_answer_callback(q)
    except BadRequest as e:
        if "Query is too old" in str(e) or "query id is invalid" in str(e):
            print("CALLBACK ANSWER SKIPPED: old query", flush=True)
            return
        raise
    except Exception as e:
        print(f"CALLBACK ANSWER SKIPPED: {e}", flush=True)


async def safe_edit(q, text, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        s = str(e)
        if (
            "Message is not modified" in s
            or "message to edit not found" in s.lower()
            or "Query is too old" in s
            or "query id is invalid" in s
        ):
            print(f"CALLBACK EDIT SKIPPED: {e}", flush=True)
            return
        raise
    except Exception as e:
        print(f"CALLBACK EDIT ERROR: {e}", flush=True)


async def show_panel(query, extra=""):
    await safe_edit(query, await panel_text(extra), reply_markup=await main_keyboard())


# ---------------- START / CALLBACKS ----------------



# =========================
# TRUSTED MODERATION
# =========================

async def get_session_for_trusted():
    sid = await current_session_id()
    return sid if sid else 0


async def trusted_action_count(session_id: int, trusted_id: int, action: str) -> int:
    async with db_pool.acquire() as con:
        return await con.fetchval(
            "SELECT COUNT(*) FROM trusted_actions WHERE session_id=$1 AND trusted_id=$2 AND action=$3",
            session_id, trusted_id, action
        )


async def log_trusted_action(session_id: int, trusted_id: int, action: str, target_user_id: int, target_message_id: int):
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO trusted_actions(session_id,trusted_id,action,target_user_id,target_message_id)
        VALUES($1,$2,$3,$4,$5)
        """, session_id, trusted_id, action, target_user_id, target_message_id)


async def delete_user_session_messages(context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    sid = await current_session_id()
    if not sid:
        return 0

    async with db_pool.acquire() as con:
        rows = await con.fetch("""
        SELECT chat_id, message_id
        FROM messages
        WHERE chat_id=$1 AND session_id=$2 AND user_id=$3
        ORDER BY created_at ASC
        """, GROUP_ID, sid, target_user_id)

    deleted = 0
    for r in rows:
        if await delete_message_safe(context, r["chat_id"], r["message_id"]):
            deleted += 1
        await asyncio.sleep(0.03)
    if deleted:
        await increment_session_counter("session_deletions", deleted)
    return deleted


async def ban_hashes_from_user_session(target_user_id: int):
    sid = await current_session_id()
    async with db_pool.acquire() as con:
        if sid:
            rows = await con.fetch("""
            SELECT hash, media_type
            FROM media_hashes
            WHERE user_id=$1
              AND hash IS NOT NULL
              AND created_at > NOW() - INTERVAL '10 days'
            """, target_user_id)
        else:
            rows = await con.fetch("""
            SELECT hash, media_type
            FROM media_hashes
            WHERE user_id=$1
              AND hash IS NOT NULL
              AND created_at > NOW() - INTERVAL '10 days'
            """, target_user_id)

        for r in rows:
            await con.execute("""
            INSERT INTO banned_hashes(hash,media_type,reason)
            VALUES($1,$2,$3)
            ON CONFLICT(hash) DO UPDATE SET reason=$3
            """, r["hash"], r["media_type"], "trusted ban user media")

    return len(rows)


async def trusted_supprime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    actor = update.effective_user
    if not msg or not actor:
        return

    if not is_trusted_id(actor.id):
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        return

    target = msg.reply_to_message.from_user

    if await is_group_admin(context, target.id) or target.id in TRUSTED_IDS:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        if not await is_silent():
            warn = await send_temp_message(context, GROUP_ID, "⛔ Action refusée : impossible de cibler un admin, owner ou trusted.", seconds=180)
            await save_message(GROUP_ID, warn.message_id, None, True)
        return

    sid = await get_session_for_trusted()
    used = await trusted_action_count(sid, actor.id, "supprime")
    if used >= 20:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        if not await is_silent():
            warn = await send_temp_message(context, GROUP_ID, "⛔ Limite atteinte : 20 /supprime par session.", seconds=180)
            await save_message(GROUP_ID, warn.message_id, None, True)
        return

    await delete_message_safe(context, msg.chat_id, msg.reply_to_message.message_id)
    await increment_session_counter("session_deletions")  # trusted_supprime
    await delete_message_safe(context, msg.chat_id, msg.message_id)
    await log_trusted_action(sid, actor.id, "supprime", target.id, msg.reply_to_message.message_id)

    async with db_pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT strikes FROM trusted_strikes WHERE session_id=$1 AND target_user_id=$2",
            sid, target.id
        )
        strikes = (row["strikes"] if row else 0) + 1
        await con.execute("""
        INSERT INTO trusted_strikes(session_id,target_user_id,strikes,updated_at)
        VALUES($1,$2,$3,NOW())
        ON CONFLICT(session_id,target_user_id)
        DO UPDATE SET strikes=$3, updated_at=NOW()
        """, sid, target.id, strikes)

    if strikes >= 2:
        until = datetime.now(TZ) + timedelta(days=7)
        try:
            await context.bot.restrict_chat_member(GROUP_ID, target.id, ChatPermissions(can_send_messages=False), until_date=until)
        except Exception as e:
            print(f"TRUSTED MUTE ERROR: {e}", flush=True)

        deleted = await delete_user_session_messages(context, target.id)
        await add_danger(target.id, 5, "trusted strikes")
async def trusted_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    actor = update.effective_user
    if not msg or not actor:
        return

    if not is_trusted_id(actor.id):
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        return

    target = msg.reply_to_message.from_user

    if await is_group_admin(context, target.id) or target.id in TRUSTED_IDS:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        if not await is_silent():
            warn = await send_temp_message(context, GROUP_ID, "⛔ Action refusée : impossible de cibler un admin, owner ou trusted.", seconds=180)
            await save_message(GROUP_ID, warn.message_id, None, True)
        return

    sid = await get_session_for_trusted()
    used = await trusted_action_count(sid, actor.id, "ban")
    if used >= 20:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        if not await is_silent():
            warn = await send_temp_message(context, GROUP_ID, "⛔ Limite atteinte : 20 /ban par session.", seconds=180)
            await save_message(GROUP_ID, warn.message_id, None, True)
        return

    await log_trusted_action(sid, actor.id, "ban", target.id, msg.reply_to_message.message_id)

    banned_hashes = await ban_hashes_from_user_session(target.id)
    deleted = await delete_user_session_messages(context, target.id)

    try:
        await context.bot.ban_chat_member(GROUP_ID, target.id)
    except Exception as e:
        print(f"TRUSTED BAN ERROR: {e}", flush=True)

    await delete_message_safe(context, msg.chat_id, msg.reply_to_message.message_id)
    await increment_session_counter("session_deletions")  # trusted_supprime
    await delete_message_safe(context, msg.chat_id, msg.message_id)
    await add_danger(target.id, 20, "trusted ban")
    await increment_ban_count()
    await increment_session_counter("session_exclusions")


async def ensure_active_campaign():
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT id FROM reward_campaigns WHERE active=TRUE ORDER BY id DESC LIMIT 1")
        if row:
            return row["id"]
        new_id = await con.fetchval("""
        INSERT INTO reward_campaigns(title,text,objective,active)
        VALUES('Rediffusion complète du groupe','🎁 Partagez votre lien pour recevoir la rediffusion complète du groupe.',50,TRUE)
        RETURNING id
        """)
        return new_id


async def get_active_campaign():
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT * FROM reward_campaigns WHERE active=TRUE ORDER BY id DESC LIMIT 1")
    if not row:
        await ensure_active_campaign()
        async with db_pool.acquire() as con:
            row = await con.fetchrow("SELECT * FROM reward_campaigns WHERE active=TRUE ORDER BY id DESC LIMIT 1")
    return row


async def get_campaign_progress(user_id: int, campaign=None):
    campaign = campaign or await get_active_campaign()
    if not campaign:
        return 0
    async with db_pool.acquire() as con:
        total = await con.fetchval("""
        SELECT COUNT(*)
        FROM referrals
        WHERE referrer_id=$1
          AND validated_at IS NOT NULL
          AND validated_at >= $2
        """, user_id, campaign["created_at"])
    return int(total or 0)


async def user_campaign_unlocked(user_id: int, campaign=None):
    campaign = campaign or await get_active_campaign()
    if not campaign:
        return False
    async with db_pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT 1 FROM campaign_rewards WHERE campaign_id=$1 AND user_id=$2",
            campaign["id"], user_id
        )
    return bool(row)


async def deliver_campaign_reward_if_ready(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    campaign = await get_active_campaign()
    if not campaign or not campaign["reward_url"]:
        return False

    progress = await get_campaign_progress(user_id, campaign)
    objective = int(campaign["objective"] or 50)
    if progress < objective:
        return False

    if await user_campaign_unlocked(user_id, campaign):
        return False

    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO campaign_rewards(campaign_id,user_id,delivered_at)
        VALUES($1,$2,NOW())
        ON CONFLICT(campaign_id,user_id) DO NOTHING
        """, campaign["id"], user_id)

    try:
        await context.bot.send_message(
            user_id,
            f"🎉 Objectif atteint.\n\nVoici la rediffusion complète :\n{campaign['reward_url']}"
        )
    except Forbidden:
        print(f"CAMPAIGN REWARD SKIPPED user={user_id}: user did not start bot", flush=True)
    except Exception as e:
        print(f"CAMPAIGN REWARD SEND ERROR user={user_id}: {e}", flush=True)

    return True


async def notify_new_campaign(context: ContextTypes.DEFAULT_TYPE, campaign_id: int):
    async with db_pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM referral_links ORDER BY created_at DESC LIMIT 500")
    for r in rows:
        try:
            await context.bot.send_message(
                r["user_id"],
                "🎁 Nouvelle rediffusion disponible.\n\nObjectif : 50 invitations validées.\nVotre compteur repart à zéro pour cette nouvelle rediffusion.\nCliquez sur Mon lien pour récupérer votre lien personnel."
            )
        except Exception:
            pass


async def create_new_campaign(context: ContextTypes.DEFAULT_TYPE, reward_url: str):
    old = await get_active_campaign()
    title = old["title"] if old else "Rediffusion complète du groupe"
    text = old["text"] if old else "🎁 Partagez votre lien pour recevoir la rediffusion complète du groupe."
    photo = old["photo_file_id"] if old else None
    objective = int(old["objective"] or 50) if old else 50

    async with db_pool.acquire() as con:
        await con.execute("UPDATE reward_campaigns SET active=FALSE, updated_at=NOW() WHERE active=TRUE")
        cid = await con.fetchval("""
        INSERT INTO reward_campaigns(title,text,photo_file_id,reward_url,objective,active,created_at,updated_at)
        VALUES($1,$2,$3,$4,$5,TRUE,NOW(),NOW())
        RETURNING id
        """, title, text, photo, reward_url, objective)

    await notify_new_campaign(context, cid)
    return cid


async def campaign_status_text(user_id: int):
    campaign = await get_active_campaign()
    link = None
    objective = int(campaign["objective"] or 50)
    progress = await get_campaign_progress(user_id, campaign)
    unlocked = await user_campaign_unlocked(user_id, campaign)

    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT invite_link FROM referral_links WHERE user_id=$1", user_id)
        if row:
            link = row["invite_link"]

    txt = (
        "🎁 Rediffusion complète du groupe\n\n"
        f"Objectif : {objective} invitations validées\n"
        f"Votre progression : {progress}/{objective}\n\n"
    )
    if link:
        txt += f"Votre lien personnel :\n{link}\n"
    if unlocked and campaign["reward_url"]:
        txt += f"\n✅ Déjà débloqué :\n{campaign['reward_url']}"
    return txt


async def get_or_create_user_private_link(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT invite_link FROM referral_links WHERE user_id=$1", user_id)
        if row and row["invite_link"]:
            return row["invite_link"]

    link = await context.bot.create_chat_invite_link(
        GROUP_ID,
        name=f"ref_{user_id}",
        creates_join_request=False,
    )

    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO referral_links(user_id, invite_link, created_at)
        VALUES($1,$2,NOW())
        ON CONFLICT(user_id) DO UPDATE SET invite_link=$2
        """, user_id, link.invite_link)

    return link.invite_link


async def build_referral_leaderboard_text(limit: int = 10):
    return await build_leaderboard_text()





async def track_private_user(user):
    if not user:
        return
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO private_users(user_id,username,first_name,last_name,started_at,last_seen_at)
        VALUES($1,$2,$3,$4,NOW(),NOW())
        ON CONFLICT(user_id) DO UPDATE SET
            username=$2,
            first_name=$3,
            last_name=$4,
            last_seen_at=NOW()
        """, user.id, user.username, user.first_name, user.last_name)


def mask_display_name(username, first_name=None, user_id=None):
    if username:
        raw = username.replace("@", "")
        if len(raw) <= 2:
            return "@" + raw[:1] + "**"
        return "@" + raw[:2] + "**" + raw[-1:]
    if first_name:
        if len(first_name) <= 2:
            return first_name[:1] + "**"
        return first_name[:2] + "**" + first_name[-1:]
    return f"ID {str(user_id)[:2]}****"


async def get_or_create_user_private_link(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT invite_link FROM referral_links WHERE user_id=$1", user_id)
        if row and row["invite_link"]:
            return row["invite_link"]

    link = await context.bot.create_chat_invite_link(
        GROUP_ID,
        name=f"ref_{user_id}",
        creates_join_request=False,
    )

    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO referral_links(user_id, invite_link, created_at)
        VALUES($1,$2,NOW())
        ON CONFLICT(user_id) DO UPDATE SET invite_link=$2
        """, user_id, link.invite_link)

    return link.invite_link


async def get_share_count(user_id: int):
    async with db_pool.acquire() as con:
        c = await con.fetchval("""
        SELECT COUNT(*) FROM referrals
        WHERE referrer_id=$1 AND validated_at IS NOT NULL
        """, user_id)
    return int(c or 0)


async def build_global_top10():
    async with db_pool.acquire() as con:
        return await con.fetch("""
        SELECT
            r.referrer_id,
            COUNT(*) AS total,
            MIN(r.validated_at) AS first_validated,
            MIN(rl.created_at) AS link_created,
            MAX(pu.username) AS username,
            MAX(pu.first_name) AS first_name
        FROM referrals r
        LEFT JOIN referral_links rl ON rl.user_id = r.referrer_id
        LEFT JOIN private_users pu ON pu.user_id = r.referrer_id
        WHERE r.validated_at IS NOT NULL
        GROUP BY r.referrer_id
        ORDER BY total DESC, first_validated ASC NULLS LAST, link_created ASC NULLS LAST, r.referrer_id ASC
        LIMIT 10
        """)


async def build_leaderboard_text():
    rows = await build_global_top10()
    if not rows:
        return None
    lines = ["🏆 Top 10 partageurs", ""]
    for i, r in enumerate(rows, start=1):
        name = mask_display_name(r["username"], r["first_name"], r["referrer_id"])
        lines.append(f"{i}. {name} — {int(r['total'] or 0)} invitation(s)")
    lines.append("")
    lines.append("Cliquez sur « Je partage » pour recevoir votre lien.")
    return "\n".join(lines)


async def notify_top10_changes(context: ContextTypes.DEFAULT_TYPE):
    rows = await build_global_top10()
    current_ids = set()
    for i, r in enumerate(rows, start=1):
        uid = r["referrer_id"]
        total = int(r["total"] or 0)
        current_ids.add(uid)
        async with db_pool.acquire() as con:
            old = await con.fetchrow("SELECT last_rank FROM leaderboard_rank_cache WHERE user_id=$1", uid)
        if not old or not old["last_rank"] or int(old["last_rank"]) > 10:
            try:
                await context.bot.send_message(uid, f"🎉 Bravo, vous êtes entré dans le Top 10 des partageurs.\nVotre rang actuel : #{i}.")
            except Exception:
                pass
        async with db_pool.acquire() as con:
            await con.execute("""
            INSERT INTO leaderboard_rank_cache(user_id,last_rank,last_count,updated_at)
            VALUES($1,$2,$3,NOW())
            ON CONFLICT(user_id) DO UPDATE SET last_rank=$2,last_count=$3,updated_at=NOW()
            """, uid, i, total)

    async with db_pool.acquire() as con:
        await con.execute("""
        UPDATE leaderboard_rank_cache
        SET last_rank=NULL, updated_at=NOW()
        WHERE last_rank IS NOT NULL
          AND user_id <> ALL($1::bigint[])
        """, list(current_ids))


async def send_share_link_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await track_private_user(user)
    async with db_pool.acquire() as con:
        abuse = await con.fetchrow("SELECT blacklisted FROM referrer_abuse WHERE referrer_id=$1", user.id)
        if abuse and abuse["blacklisted"]:
            await update.message.reply_text("❌ Ton accès au partage est bloqué.")
            return
    link = await get_or_create_user_private_link(context, user.id)
    total = await get_share_count(user.id)
    await update.message.reply_text(
        "🤝 Votre lien personnel\n\n"
        f"{link}\n\n"
        f"✅ Invitations validées : {total}\n\n"
        "Partagez ce lien pour monter dans le classement."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await track_private_user(user)

    payload = context.args[0] if context.args else ""
    if payload == "share":
        await send_share_link_private(update, context)
        return

    if not user:
        return

    args = context.args or []
    if args and args[0] == "getlink":
        await send_share_link_private(update, context)
        return

    if is_admin(user.id):
        await set_admin_state(user.id, None)
        await update.message.reply_text(await panel_text(), reply_markup=await main_keyboard())
    else:
        await update.message.reply_text("Cliquez sur le bouton « Je partage » dans le groupe pour recevoir votre lien personnel.")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer_callback(q)

    if not q.from_user or not is_admin(q.from_user.id):
        return

    data = q.data

    if data.startswith("toggle:"):
        key = data.split(":", 1)[1]
        current = await get_setting(key, "off")
        new = "off" if current == "on" else "on"
        await set_setting(key, new)
        await show_panel(q, f"{key} = {new.upper()}")
        return

    if data == "info":
        await set_admin_state(q.from_user.id, None)
        await show_panel(q, "infos actualisées")
        return

    if data == "open_group":
        await open_group(context)
        await show_panel(q, "session ouverte")
        return

    if data == "close_group":
        deleted = await close_group_and_clean(context)
        await show_panel(q, f"session fermée, {deleted} messages supprimés")
        return

    if data == "publish_ad_locked":
        await q.answer("Il faut d'abord uploader 60 vidéos.", show_alert=True)
        return

    if data == "publish_ad":
        ok = await publish_ad(context)
        await show_panel(q, "publicité publiée" if ok else "60 vidéos requises")
        return

    if data == "words_menu":
        await safe_edit(q, "🚫 MOTS INTERDITS\n\nChoisis une action :", reply_markup=await words_keyboard())
        return

    if data == "word_add":
        await set_admin_state(q.from_user.id, "adding_word")
        await safe_edit(q, "➕ Envoie maintenant le mot à interdire en privé.", reply_markup=back_keyboard())
        return

    if data == "word_delete":
        await set_admin_state(q.from_user.id, "deleting_word")
        await safe_edit(q, "➖ Envoie maintenant le mot à supprimer en privé.", reply_markup=back_keyboard())
        return

    if data == "word_list":
        async with db_pool.acquire() as con:
            rows = await con.fetch("SELECT word FROM banned_words ORDER BY word")
        words = "\n".join([f"• {r['word']}" for r in rows]) or "Aucun mot interdit."
        await safe_edit(q, f"📋 MOTS INTERDITS\n\n{words}", reply_markup=await words_keyboard())
        return


    if data == "toggle_raid":
        current = await get_setting("raid_mode", "off")
        new = "off" if current == "on" else "on"
        await set_setting("raid_mode", new)
        await show_panel(q, f"mode RAID = {new.upper()}")
        return

    if data == "rules_set":
        await set_admin_state(q.from_user.id, "setting_rules")
        await safe_edit(q, "📌 Envoie maintenant le texte des règles.", reply_markup=back_keyboard())
        return

    if data == "broadcast_set":
        await set_admin_state(q.from_user.id, "broadcast_group")
        await safe_edit(q, "📣 Envoie maintenant le message à publier dans le groupe.", reply_markup=back_keyboard())
        return

    if data == "ban_hash_set":
        await set_admin_state(q.from_user.id, "ban_hash")
        await safe_edit(q, "🚫 Envoie maintenant la photo ou vidéo à bannir par hash.", reply_markup=back_keyboard())
        return

    if data == "set_ad1_text":
        await set_admin_state(q.from_user.id, "set_ad1_text")
        await safe_edit(q, "✏️ Envoie maintenant le texte de la Publicité 1.", reply_markup=back_keyboard())
        return

    if data == "set_ad2_text":
        await set_admin_state(q.from_user.id, "set_ad2_text")
        await safe_edit(q, "✏️ Envoie maintenant le texte de la Publicité 2.", reply_markup=back_keyboard())
        return

    if data == "set_share_ad":
        await set_admin_state(q.from_user.id, "set_share_ad")
        await safe_edit(q, "🖼️ Envoie maintenant la pub image avec son texte en légende. Le bouton sera : Mon lien.", reply_markup=back_keyboard())
        return

    if data == "publish_share_ad":
        await publish_share_ad(context)
        await safe_edit(q, await panel_text("Pub Mon lien publiée"), reply_markup=await main_keyboard())
        return

    if data == "campaign_menu":
        c = await get_active_campaign()
        text = (
            "🎁 CAMPAGNE REDIFFUSION\n\n"
            f"Objectif : {c['objective']} invitations\n"
            f"Lien GoFile : {'✅ configuré' if c['reward_url'] else '❌ manquant'}\n"
            f"Image : {'✅ configurée' if c['photo_file_id'] else '❌ manquante'}\n\n"
            "Modifier :"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Texte", callback_data="campaign_set_text"), InlineKeyboardButton("🖼️ Image", callback_data="campaign_set_image")],
            [InlineKeyboardButton("🎯 Objectif", callback_data="campaign_set_objective")],
            [InlineKeyboardButton("🔗 Nouveau lien GoFile", callback_data="campaign_set_link")],
            [InlineKeyboardButton("📣 Publier campagne", callback_data="publish_campaign_ad")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="info")]
        ])
        await safe_edit(q, text, reply_markup=kb)
        return

    if data == "campaign_set_text":
        await set_admin_state(q.from_user.id, "campaign_set_text")
        await safe_edit(q, "✏️ Envoie le texte de la campagne rediffusion.", reply_markup=back_keyboard())
        return

    if data == "campaign_set_image":
        await set_admin_state(q.from_user.id, "campaign_set_image")
        await safe_edit(q, "🖼️ Envoie l'image de la campagne rediffusion.", reply_markup=back_keyboard())
        return

    if data == "campaign_set_objective":
        await set_admin_state(q.from_user.id, "campaign_set_objective")
        await safe_edit(q, "🎯 Envoie le nouvel objectif en nombre d'invitations validées. Exemple : 50", reply_markup=back_keyboard())
        return

    if data == "campaign_set_link":
        await set_admin_state(q.from_user.id, "campaign_set_link")
        await safe_edit(q, "🔗 Envoie le nouveau lien GoFile.\n\nAttention : cela crée une nouvelle campagne et remet les compteurs à zéro.", reply_markup=back_keyboard())
        return

    if data == "publish_campaign_ad":
        await publish_campaign_ad(context)
        await safe_edit(q, await panel_text("Campagne publiée"), reply_markup=await main_keyboard())
        return

    if data == "share_publicity_menu":
        text = (
            "📣 PUBLICITÉ PARTAGE\n\n"
            "Configure le texte et l'image.\n"
            "Le bouton affiché sera : 🤝 Je partage."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Texte", callback_data="share_pub_set_text"), InlineKeyboardButton("🖼️ Image", callback_data="share_pub_set_image")],
            [InlineKeyboardButton("📣 Publier publicité", callback_data="publish_share_publicity")],
            [InlineKeyboardButton("📢 Broadcast privé", callback_data="broadcast_private_set")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="info")]
        ])
        await safe_edit(q, text, reply_markup=kb)
        return

    if data == "share_pub_set_text":
        await set_admin_state(q.from_user.id, "share_pub_set_text")
        await safe_edit(q, "✏️ Envoie le texte de la publicité partage.", reply_markup=back_keyboard())
        return

    if data == "share_pub_set_image":
        await set_admin_state(q.from_user.id, "share_pub_set_image")
        await safe_edit(q, "🖼️ Envoie l'image de la publicité partage.", reply_markup=back_keyboard())
        return

    if data == "publish_share_publicity":
        await publish_share_publicity(context)
        await safe_edit(q, await panel_text("Publicité partage publiée"), reply_markup=await main_keyboard())
        return

    if data == "broadcast_private_set":
        await set_admin_state(q.from_user.id, "broadcast_private")
        await safe_edit(q, "📢 Envoie le message à broadcaster en privé aux personnes qui ont déjà lancé le bot.", reply_markup=back_keyboard())
        return

    if data == "reward_links_menu":
        await safe_edit(q, "🔗 LIENS RÉCOMPENSES\n\nChoisis le lien à modifier.", reply_markup=await reward_links_keyboard())
        return

    if data.startswith("set_reward_link:"):
        level = data.split(":", 1)[1]
        await set_admin_state(q.from_user.id, f"set_reward_link:{level}")
        await safe_edit(q, f"🔗 Envoie maintenant le lien pour le palier {level}.", reply_markup=back_keyboard())
        return

    if data == "show_reward_links":
        async with db_pool.acquire() as con:
            rows = await con.fetch("SELECT level,url FROM reward_links ORDER BY level")
        txt = "🔗 LIENS RÉCOMPENSES\n\n" + "\n".join([f"{r['level']} : {r['url'] or '❌ vide'}" for r in rows])
        await safe_edit(q, txt, reply_markup=await reward_links_keyboard())
        return

    if data == "warn_non_participants":
        count = await warn_non_participants(context)
        await show_panel(q, f"{count} non-participant(s) averti(s)")
        return
    if data == "ref_stats":
        async with db_pool.acquire() as con:
            rows = await con.fetch("""
            SELECT referrer_id, COUNT(*) AS total
            FROM referrals
            WHERE validated_at IS NOT NULL
            GROUP BY referrer_id
            ORDER BY total DESC
            LIMIT 20
            """)
        if not rows:
            txt = "📊 STATS PARRAINAGE\n\nAucun parrainage validé."
        else:
            txt = "📊 STATS PARRAINAGE\n\n" + "\n".join([f"• {r['referrer_id']} : {r['total']}" for r in rows])
        await safe_edit(q, txt, reply_markup=back_keyboard())
        return


# ---------------- GROUP OPEN / CLOSE ----------------

async def open_group(context: ContextTypes.DEFAULT_TYPE):
    await reset_session_moderation_counters()
    await delete_last_system_message(context)

    sid = await create_session()
    await set_setting("group_open", "on")

    perms = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
    )

    await context.bot.set_chat_permissions(GROUP_ID, perms)
    await send_system_message(context, "🟢 Groupe ouvert, vous pouvez envoyer tous vos médias.", "open", record_in_session=True)
    print(f"SESSION OPEN {sid}", flush=True)




async def send_trusted_session_report(context: ContextTypes.DEFAULT_TYPE, session_id: int, deleted_count: int):
    if not session_id:
        return

    async with db_pool.acquire() as con:
        rows = await con.fetch("""
        SELECT trusted_id, action, COUNT(*) AS total
        FROM trusted_actions
        WHERE session_id=$1
        GROUP BY trusted_id, action
        ORDER BY trusted_id, action
        """, session_id)

        strikes = await con.fetch("""
        SELECT target_user_id, strikes
        FROM trusted_strikes
        WHERE session_id=$1
        ORDER BY strikes DESC
        LIMIT 20
        """, session_id)

    by_user = {}
    for r in rows:
        tid = r["trusted_id"]
        by_user.setdefault(tid, {"supprime": 0, "ban": 0})
        by_user[tid][r["action"]] = r["total"]

    lines = [
        f"📊 Bilan modération trusted — Session #{session_id}",
        "",
        f"🧹 Messages supprimés à la fermeture : {deleted_count}",
        "",
    ]

    if not by_user:
        lines.append("Aucune action trusted pendant cette session.")
    else:
        for tid, data in by_user.items():
            sup = data.get("supprime", 0)
            ban = data.get("ban", 0)
            sup_limit = " ⚠️ limite atteinte" if sup >= 20 else ""
            ban_limit = " ⚠️ limite atteinte" if ban >= 20 else ""
            lines.append(f"👤 Trusted ID {tid}")
            lines.append(f"• /supprime : {sup}{sup_limit}")
            lines.append(f"• /ban : {ban}{ban_limit}")
            lines.append("")

    if strikes:
        lines.append("🎯 Utilisateurs avec strikes :")
        for s in strikes:
            lines.append(f"• {s['target_user_id']} : {s['strikes']} strike(s)")

    text = "\n".join(lines)

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text)
        except Exception as e:
            print(f"TRUSTED REPORT SKIPPED admin={admin_id}: admin must start the bot in private first ({e})", flush=True)

async def close_group_and_clean(context: ContextTypes.DEFAULT_TYPE):
    sid = await current_session_id()
    await set_setting("group_open", "off")

    try:
        await context.bot.set_chat_permissions(GROUP_ID, ChatPermissions(can_send_messages=False))
    except Exception as e:
        print(f"SET PERMS CLOSE ERROR: {e}", flush=True)

    if sid:
        async with db_pool.acquire() as con:
            rows = await con.fetch("""
            SELECT chat_id, message_id
            FROM messages
            WHERE chat_id=$1 AND session_id=$2
            ORDER BY created_at ASC
            """, GROUP_ID, sid)
    else:
        async with db_pool.acquire() as con:
            rows = await con.fetch("""
            SELECT chat_id, message_id
            FROM messages
            WHERE chat_id=$1
            ORDER BY created_at ASC
            """, GROUP_ID)

    deleted = 0
    for r in rows:
        ok = await delete_message_safe(context, r["chat_id"], r["message_id"])
        if ok:
            deleted += 1
        await asyncio.sleep(0.04)

    await delete_last_system_message(context)

    async with db_pool.acquire() as con:
        if sid:
            await con.execute("DELETE FROM messages WHERE chat_id=$1 AND session_id=$2", GROUP_ID, sid)
        else:
            await con.execute("DELETE FROM messages WHERE chat_id=$1", GROUP_ID)

    await close_session()

    await send_system_message(
        context,
        "🔴 Groupe fermé, vous ne pouvez plus envoyer.",
        "closed",
        record_in_session=False,
    )

    await send_trusted_session_report(context, sid, deleted)
    return deleted


# ---------------- PUBLICITY / REFERRAL ----------------

async def publish_ad(context: ContextTypes.DEFAULT_TYPE):
    if not await reward_links_ready():
        return False

    old_id = int(await get_setting("ad_message_id", "0") or "0")
    if old_id:
        await delete_message_safe(context, GROUP_ID, old_id)

    text = (
        "🎁 Partagez votre lien pour recevoir la rediffusion complète du groupe.\n\n"
        "\n"
        "\n"
        "\n"
        "\n\n"
        "Cliquez sur « Je partage » pour recevoir votre lien personnel."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Recevoir mon lien privé", url=bot_start_url())]
    ])

    msg = await context.bot.send_message(GROUP_ID, text, reply_markup=keyboard)
    await set_setting("ad_message_id", msg.message_id)
    return True


async def send_referral_link_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_share_link_private(update, context)


async def validate_join_later(context: ContextTypes.DEFAULT_TYPE, invited_user_id: int):
    await asyncio.sleep(300)
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT referrer_id, invite_link FROM pending_joins WHERE invited_user_id=$1", invited_user_id)
    if not row:
        return
    referrer_id = row["referrer_id"]
    try:
        member = await context.bot.get_chat_member(GROUP_ID, invited_user_id)
        if member.status in ("left", "kicked"):
            async with db_pool.acquire() as con:
                await con.execute("DELETE FROM pending_joins WHERE invited_user_id=$1", invited_user_id)
                await con.execute("""
                INSERT INTO referrer_abuse(referrer_id,failed_joins,updated_at)
                VALUES($1,1,NOW())
                ON CONFLICT(referrer_id) DO UPDATE SET failed_joins=referrer_abuse.failed_joins+1, updated_at=NOW()
                """, referrer_id)
                failed = await con.fetchval("SELECT failed_joins FROM referrer_abuse WHERE referrer_id=$1", referrer_id)
                if failed and failed >= 5:
                    await con.execute("UPDATE referrer_abuse SET blacklisted=TRUE, updated_at=NOW() WHERE referrer_id=$1", referrer_id)
            return
    except Exception:
        return

    invite_link = row["invite_link"]
    async with db_pool.acquire() as con:
        await con.execute("""
        INSERT INTO referrals(referrer_id,invited_user_id,invite_link,joined_at,validated_at)
        VALUES($1,$2,$3,NOW(),NOW())
        ON CONFLICT(referrer_id, invited_user_id) DO UPDATE SET validated_at=NOW()
        """, referrer_id, invited_user_id, invite_link)
        await con.execute("DELETE FROM pending_joins WHERE invited_user_id=$1", invited_user_id)
    await deliver_campaign_reward_if_ready(context, referrer_id)
    await notify_top10_changes(context)


async def send_reward_link(context: ContextTypes.DEFAULT_TYPE, user_id: int, level: int):
    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT url FROM reward_links WHERE level=$1", level)
    if not row or not row["url"]:
        return
    try:
        await context.bot.send_message(user_id, f"{MSG_REWARD_UNLOCKED}\n{row['url']}")
    except Forbidden:
        print(f"Cannot send reward link to {user_id}: user did not start bot", flush=True)
    except Exception as e:
        print(f"SEND REWARD LINK ERROR {user_id}: {e}", flush=True)


# ---------------- PRIVATE ADMIN ----------------

async def handle_private_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message
    if not user or not msg or not is_admin(user.id):
        return

    state = await get_admin_state(user.id)
    text = (msg.text or "").strip()

    if state == "adding_word":
        word = text.lower()
        if not word:
            await msg.reply_text("❌ Envoie un vrai mot.")
            return
        async with db_pool.acquire() as con:
            await con.execute("INSERT INTO banned_words(word) VALUES($1) ON CONFLICT DO NOTHING", word)
        await set_admin_state(user.id, None)
        await msg.reply_text(f"✅ Mot interdit ajouté : {word}")
        return

    if state == "deleting_word":
        word = text.lower()
        async with db_pool.acquire() as con:
            await con.execute("DELETE FROM banned_words WHERE word=$1", word)
        await set_admin_state(user.id, None)
        await msg.reply_text(f"✅ Mot supprimé : {word}")
        return


    if state == "setting_rules":
        await set_setting("rules_text", text)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Règles mises à jour.")
        return

    if state == "broadcast_group":
        sent = await context.bot.send_message(GROUP_ID, text)
        await save_message(GROUP_ID, sent.message_id, None, True)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Broadcast envoyé dans le groupe.")
        return

    if state and state.startswith("set_reward_link:"):
        level = int(state.split(":", 1)[1])
        async with db_pool.acquire() as con:
            await con.execute("""
            INSERT INTO reward_links(level,url,updated_at)
            VALUES($1,$2,NOW())
            ON CONFLICT(level) DO UPDATE SET url=$2, updated_at=NOW()
            """, level, text)
        await set_admin_state(user.id, None)
        await msg.reply_text(f"✅ Lien palier {level} mis à jour.")
        return

    if state == "set_ad1_text":
        await set_setting("ad1_text", text)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Texte Publicité 1 mis à jour.")
        return

    if state == "set_ad2_text":
        await set_setting("ad2_text", text)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Texte Publicité 2 mis à jour.")
        return

    if state == "set_share_ad":
        caption = msg.caption or msg.text or ""
        if msg.photo:
            await set_setting("share_ad_photo_file_id", msg.photo[-1].file_id)
            await set_setting("share_ad_text", caption)
            await set_admin_state(user.id, None)
            await msg.reply_text("✅ Pub image + bouton Mon lien enregistrée.")
            return
        if text:
            await set_setting("share_ad_text", text)
            await set_admin_state(user.id, None)
            await msg.reply_text("✅ Texte pub Mon lien enregistré.")
            return

    if state == "campaign_set_text":
        await ensure_active_campaign()
        async with db_pool.acquire() as con:
            await con.execute("UPDATE reward_campaigns SET text=$1, updated_at=NOW() WHERE active=TRUE", text)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Texte campagne mis à jour.")
        return

    if state == "campaign_set_image":
        if not msg.photo:
            await msg.reply_text("❌ Envoie une image.")
            return
        await ensure_active_campaign()
        async with db_pool.acquire() as con:
            await con.execute("UPDATE reward_campaigns SET photo_file_id=$1, updated_at=NOW() WHERE active=TRUE", msg.photo[-1].file_id)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Image campagne mise à jour.")
        return

    if state == "campaign_set_objective":
        try:
            objective = int(text)
        except Exception:
            await msg.reply_text("❌ Envoie un nombre valide. Exemple : 50")
            return

        if objective < 1 or objective > 10000:
            await msg.reply_text("❌ Objectif invalide.")
            return

        await set_setting("campaign_default_objective", str(objective))
        await ensure_active_campaign()
        async with db_pool.acquire() as con:
            await con.execute("UPDATE reward_campaigns SET objective=$1, updated_at=NOW() WHERE active=TRUE", objective)
        await set_admin_state(user.id, None)
        await msg.reply_text(f"✅ Objectif mis à jour : {objective} invitations validées.")
        return

    if state == "campaign_set_link":
        if not text:
            await msg.reply_text("❌ Envoie un lien GoFile.")
            return
        await create_new_campaign(context, text)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Nouvelle campagne créée. Les utilisateurs engagés ont été notifiés.")
        return

    if state == "share_pub_set_text":
        await set_setting("share_publicity_text", text)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Texte publicité partage mis à jour.")
        return

    if state == "share_pub_set_image":
        if not msg.photo:
            await msg.reply_text("❌ Envoie une image.")
            return
        await set_setting("share_publicity_photo_file_id", msg.photo[-1].file_id)
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Image publicité partage mise à jour.")
        return

    if state == "broadcast_private":
        broadcast_text = msg.caption or text
        if not broadcast_text:
            await msg.reply_text("❌ Envoie un texte à broadcaster.")
            return
        await set_admin_state(user.id, None)
        sent = await broadcast_private_users(context, broadcast_text)
        await msg.reply_text(f"✅ Broadcast terminé : {sent} personne(s) contactée(s).")
        return

    if state == "ban_hash":
        if not message_has_media(msg):
            await msg.reply_text("❌ Envoie une photo ou une vidéo.")
            return

        keys = await media_fingerprints_from_message(context, msg)
        if not keys:
            await msg.reply_text("❌ Ce média est trop gros ou impossible à analyser.")
            return

        await insert_banned_media_fingerprints(keys, media_type(msg), "média interdit admin")
        await set_admin_state(user.id, None)
        await msg.reply_text("✅ Média interdit enregistré.")
        return

    if message_has_media(msg):
        await msg.reply_text("ℹ️ Média reçu, mais aucun mode actif. Pour bannir un hash, clique d’abord sur 🚫 Ban hash dans le panel.")
        return

    await msg.reply_text("Admin prêt. Envoie /start pour ouvrir le panel.")


# ---------------- GROUP MODERATION ----------------

def media_unique_id(msg):
    if msg.photo:
        return msg.photo[-1].file_unique_id
    if msg.video:
        return msg.video.file_unique_id
    if msg.animation:
        return msg.animation.file_unique_id
    if msg.document:
        return msg.document.file_unique_id
    return None


async def save_user_message_if_session(update: Update):
    if await get_setting("group_open", "off") != "on":
        return
    sid = await current_session_id()
    if not sid:
        return
    await save_message(
        update.effective_chat.id,
        update.message.message_id,
        update.effective_user.id if update.effective_user else None,
        False,
        sid,
    )




async def trusted_pasfr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    actor = update.effective_user
    if not msg or not actor:
        return

    if not is_trusted_id(actor.id):
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        until = datetime.now(TZ) + timedelta(days=2)
        try:
            await context.bot.restrict_chat_member(
                GROUP_ID,
                actor.id,
                ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            await increment_session_counter("session_mutes")
        except Exception as e:
            print(f"FAKE PASFR MUTE ERROR: {e}", flush=True)
        try:
            await send_temp_message(context, GROUP_ID, MSG_FAKE_COMMAND, seconds=180)
        except Exception:
            pass
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        return

    target = msg.reply_to_message.from_user
    if await is_group_admin(context, target.id) or target.id in TRUSTED_IDS:
        await delete_message_safe(context, msg.chat_id, msg.message_id)
        return

    await delete_message_safe(context, msg.chat_id, msg.reply_to_message.message_id)
    await delete_message_safe(context, msg.chat_id, msg.message_id)
    try:
        await increment_session_counter("session_deletions")
    except Exception:
        pass

    until = datetime.now(TZ) + timedelta(days=2)
    try:
        await context.bot.restrict_chat_member(
            GROUP_ID,
            target.id,
            ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await increment_session_counter("session_mutes")
    except Exception as e:
        print(f"PASFR MUTE ERROR: {e}", flush=True)

    try:
        await log_trusted_action(await get_session_for_trusted(), actor.id, "pasfr", target.id, msg.reply_to_message.message_id)
    except Exception as e:
        print(f"PASFR LOG ERROR: {e}", flush=True)

    await send_temp_message(context, GROUP_ID, MSG_PASFR, seconds=180)


# =========================
# PUBLIC MESSAGES V24
# =========================

MSG_PARTICIPATION_REQUIRED = "⚠️ Merci de participer avant d’envoyer un message.\nEnvoyez au moins 1 photo ou 1 vidéo jamais publiée."
MSG_REPOST = "♻️ Ce média a déjà été publié."
MSG_LINK_FORBIDDEN = "🔗 Les liens ne sont pas autorisés."
MSG_FORWARD_FORBIDDEN = "🚫 Les transferts ne sont pas autorisés."
MSG_GENERIC_FORBIDDEN = "🚫 Message non autorisé."
MSG_FAKE_COMMAND = "🔇 Commande réservée à la modération.\nSi vous essayez encore, vous serez banni."
MSG_PASFR = "Je viens de restreindre une personne qui n’a pas envoyé du contenu FR, ne faites pas comme lui."
MSG_PRIVATE_LINK_TITLE = "🎁 Voici votre lien privé de parrainage."
MSG_REWARD_UNLOCKED = "🎉 Récompense débloquée."

def clean_public_reason(reason: str) -> str:
    mapping = {
        "lien interdit": MSG_LINK_FORBIDDEN,
        "lien interdit": MSG_LINK_FORBIDDEN,
        "transfert interdit": MSG_FORWARD_FORBIDDEN,
        "transfert interdit": MSG_FORWARD_FORBIDDEN,
        "mot interdit": MSG_GENERIC_FORBIDDEN,
        "média interdit": MSG_GENERIC_FORBIDDEN,
        "photo avec identification interdite": MSG_GENERIC_FORBIDDEN,
        "photo avec identification interdite": MSG_GENERIC_FORBIDDEN,
    }
    return mapping.get(reason, MSG_GENERIC_FORBIDDEN)

async def punish_ban(update, context, reason, custom_message=None):
    user = update.effective_user
    if not user:
        return

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user.id)
    except Exception as e:
        print(f"BAN ERROR: {e}", flush=True)

    # V35 : purge complète de la session après ban automatique.
    # Important pour média interdit : on ne laisse pas les anciens médias/messages visibles.
    try:
        deleted = await delete_user_session_messages(context, user.id)
        print(f"PURGE SESSION AFTER AUTO BAN: user={user.id} deleted={deleted}", flush=True)
    except Exception as e:
        print(f"PURGE SESSION AFTER AUTO BAN ERROR: {e}", flush=True)

    await delete_message_safe(context, update.effective_chat.id, update.message.message_id)
    await add_danger(user.id, 10, reason)
    await increment_ban_count()
    await increment_session_counter("session_exclusions")

    if await is_silent():
        return

    msg = await send_temp_message(
        context,
        update.effective_chat.id,
        custom_message or clean_public_reason(reason),
        seconds=180,
    )
    await save_message(update.effective_chat.id, msg.message_id, None, True)


async def punish_word(update, context):
    user = update.effective_user
    if not user:
        return

    async with db_pool.acquire() as con:
        row = await con.fetchrow("SELECT count FROM user_violations WHERE user_id=$1", user.id)
        count = (row["count"] if row else 0) + 1
        await con.execute("""
        INSERT INTO user_violations(user_id,count,updated_at)
        VALUES($1,$2,NOW())
        ON CONFLICT(user_id) DO UPDATE SET count=$2, updated_at=NOW()
        """, user.id, count)

    await delete_message_safe(context, update.effective_chat.id, update.message.message_id)

    if count == 1:
        until = datetime.now(TZ) + timedelta(days=1)
        await context.bot.restrict_chat_member(update.effective_chat.id, user.id, ChatPermissions(can_send_messages=False), until_date=until)
        action = "mute 1 jour"
        await increment_session_counter("session_mutes")
    elif count == 2:
        until = datetime.now(TZ) + timedelta(days=7)
        await context.bot.restrict_chat_member(update.effective_chat.id, user.id, ChatPermissions(can_send_messages=False), until_date=until)
        action = "mute 1 semaine"
        await increment_session_counter("session_mutes")
    else:
        await context.bot.ban_chat_member(update.effective_chat.id, user.id)
        action = "ban"

    msg = await context.bot.send_message(
        update.effective_chat.id,
        MSG_GENERIC_FORBIDDEN,
        parse_mode="HTML",
    )
    await save_message(update.effective_chat.id, msg.message_id, None, True)


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.id != GROUP_ID:
        return

    msg = update.message
    user = update.effective_user

    # 0) Supprime messages Telegram automatiques entrée/sortie.
    if msg.new_chat_members or msg.left_chat_member:
        await delete_message_safe(context, update.effective_chat.id, msg.message_id)
        return

    if not user:
        return

    # 1) Commandes trusted /ban et /supprime sont traitées par CommandHandler avant ce handler.
    admin_exempt = await is_group_admin(context, user.id)

    # 2) Les autres commandes / sont supprimées. Récidive = mute 1 mois.
    if msg.text and msg.text.startswith("/") and not admin_exempt and not is_trusted_id(user.id):
        await delete_message_safe(context, GROUP_ID, msg.message_id)
        async with db_pool.acquire() as con:
            row = await con.fetchrow("SELECT score FROM danger_scores WHERE user_id=$1", user.id)
            score = row["score"] if row else 0
        await add_danger(user.id, 2, "commande slash")
        if score >= 2:
            until = datetime.now(TZ) + timedelta(days=30)
            try:
                await context.bot.restrict_chat_member(
                    GROUP_ID,
                    user.id,
                    ChatPermissions(can_send_messages=False),
                    until_date=until
                )
            except Exception as e:
                print(f"SLASH MUTE ERROR: {e}", flush=True)
        return

    await upsert_participant(user)
    await save_user_message_if_session(update)

    if await get_setting("moderation", "on") != "on":
        return

    # 3) Admins/owner exemptés de toutes les règles fortes.
    if admin_exempt:
        return

    text = (msg.text or msg.caption or "").lower()

    # 4) Anti-lien prioritaire : ban direct même si participation ON.
    if await get_setting("anti_links", "on") == "on" and URL_RE.search(text):
        await punish_ban(update, context, "lien interdit")
        return

    # 5) Transferts interdits prioritaires.
    is_forward = bool(
        msg.forward_origin
        or getattr(msg, "forward_date", None)
        or getattr(msg, "forward_from", None)
        or getattr(msg, "forward_from_chat", None)
    )
    # V25: les transferts sont autorisés, pas de sanction.
    if is_forward:
        pass


    # 6) Photo + mention prioritaire.
    if await get_setting("anti_photo_mention", "on") == "on":
        has_photo = bool(msg.photo)
        has_mention = bool(
            msg.caption_entities
            and any(e.type in ("mention", "text_mention") for e in msg.caption_entities)
        )
        if has_photo and has_mention:
            await punish_ban(update, context, "photo avec identification interdite")
            return

    # 7) Empreintes média : média interdit avant repost simple.
    media_keys = []
    h = None
    if message_has_media(msg):
        media_keys = await media_fingerprints_from_message(context, msg)
        h = media_keys[0] if media_keys else None

    if media_keys:
        banned_match = await find_matching_banned_media(media_keys)
        if banned_match:
            await punish_ban(
                update,
                context,
                "média interdit",
                MSG_GENERIC_FORBIDDEN if "MSG_GENERIC_FORBIDDEN" in globals() else "🚫 Message non autorisé."
            )
            return

        if await get_setting("anti_repost", "on") == "on":
            repost_match = await find_matching_repost_media(media_keys)
            if repost_match:
                await delete_message_safe(context, GROUP_ID, msg.message_id)
                try:
                    await increment_session_counter("session_deletions")
                except Exception:
                    pass
                warn = await send_temp_message(
                    context,
                    GROUP_ID,
                    MSG_REPOST if "MSG_REPOST" in globals() else "♻️ Ce média a déjà été publié.",
                    seconds=180
                )
                await save_message(GROUP_ID, warn.message_id, None, True)
                await add_danger(user.id, 2, "repost média")
                return

        await insert_media_fingerprints(
            media_keys,
            GROUP_ID,
            user.id,
            msg.message_id,
            media_type(msg),
            bool(message_is_photo_or_video(msg)),
        )

    # 8) Mots interdits.
    async with db_pool.acquire() as con:
        words = await con.fetch("SELECT word FROM banned_words")
    for r in words:
        word = r["word"].lower()
        if word and re.search(rf"\b{re.escape(word)}\b", text, re.I):
            await punish_word(update, context)
            return

    # 9) Participation obligatoire EN DERNIER.
    if await get_setting("participation", "off") == "on":
        if not await has_participated(user.id):
            if not message_is_photo_or_video(msg):
                await delete_message_safe(context, GROUP_ID, msg.message_id)
                await increment_session_counter("session_deletions")
                await send_temp_message(
                    context,
                    GROUP_ID,
                    MSG_PARTICIPATION_REQUIRED,
                    seconds=180
                )
                await add_danger(user.id, 1, "message avant participation")
                return

            if h:
                await mark_participated(user.id, h)
                return

    return


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu or cmu.chat.id != GROUP_ID:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status
    user = cmu.new_chat_member.user

    if old_status in ("left", "kicked") and new_status in ("member", "restricted"):
        await upsert_participant(user)
        if user.is_bot:
            try:
                await context.bot.ban_chat_member(GROUP_ID, user.id)
            except Exception:
                pass
            return
        if await get_setting("raid_mode", "off") == "on":
            try:
                until = datetime.now(TZ) + timedelta(hours=6)
                await context.bot.restrict_chat_member(GROUP_ID, user.id, ChatPermissions(can_send_messages=False), until_date=until)
            except Exception as e:
                print(f"RAID MUTE NEW MEMBER ERROR: {e}", flush=True)
        invite_link = cmu.invite_link.invite_link if cmu.invite_link else None
        if invite_link:
            async with db_pool.acquire() as con:
                ref = await con.fetchrow("SELECT user_id FROM referral_links WHERE invite_link=$1", invite_link)

            if ref and ref["user_id"] != user.id:
                async with db_pool.acquire() as con:
                    await con.execute("""
                    INSERT INTO pending_joins(invited_user_id,referrer_id,invite_link,joined_at)
                    VALUES($1,$2,$3,NOW())
                    ON CONFLICT(invited_user_id) DO UPDATE SET referrer_id=$2, invite_link=$3, joined_at=NOW()
                    """, user.id, ref["user_id"], invite_link)

                context.application.create_task(validate_join_later(context, user.id))

    if new_status in ("left", "kicked"):
        async with db_pool.acquire() as con:
            await con.execute("DELETE FROM pending_joins WHERE invited_user_id=$1", user.id)




# ---------------- PARTICIPATION / RULES ----------------

async def warn_non_participants(context):
    if await get_setting("participation", "off") != "on":
        return 0

    kicked_total = await get_setting("non_participants_kicked_total", "0")
    kick_np = await get_setting("kick_non_participants", "off")

    async with db_pool.acquire() as con:
        rows = await con.fetch("""
        SELECT user_id, username, first_name
        FROM participants
        WHERE has_participated=FALSE
        ORDER BY joined_at ASC
        LIMIT 200
        """)

    if not rows:
        return 0

    total = 0
    for i in range(0, len(rows), 20):
        batch = rows[i:i+20]
        mentions = []
        for r in batch:
            if r["username"]:
                mentions.append(f"@{r['username']}")
            else:
                mentions.append(f'<a href="tg://user?id={r["user_id"]}">{r["first_name"] or r["user_id"]}</a>')

        txt = (
            "⚠️ Veuillez participer si vous voulez rester dans le groupe.\n"
            "Envoyez au moins 1 photo ou 1 vidéo jamais publiée.\n\n"
            "✅ Une seule participation valide suffit pour rester définitivement.\n\n"
            "Si vous ne participez pas, vous serez supprimé du groupe sous peu.\n\n"
            f"🥾 Déjà supprimés pour non-participation : {kicked_total}\n"
            f"🥾 Kick automatique : {'ON' if kick_np == 'on' else 'OFF'}\n"
            "Limite : 20 suppressions / jour\n\n"
        )
        txt += " ".join(mentions)

        msg = await context.bot.send_message(GROUP_ID, txt, parse_mode="HTML")
        await save_message(GROUP_ID, msg.message_id, None, True)
        total += len(batch)
        await asyncio.sleep(0.2)

        async with db_pool.acquire() as con:
            for r in batch:
                await con.execute("""
                UPDATE participants
                SET warn_count=warn_count+1, last_warned_at=NOW()
                WHERE user_id=$1
                """, r["user_id"])

    return total


async def kick_old_non_participants(context):
    if await get_setting("participation", "off") != "on":
        return
    if await get_setting("kick_non_participants", "off") != "on":
        return

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    last_day = await get_setting("last_nonparticipant_kick_date", "")

    if last_day != today:
        await set_setting("last_nonparticipant_kick_date", today)
        await set_setting("nonparticipant_kicked_today", "0")

    kicked_today = int(await get_setting("nonparticipant_kicked_today", "0") or "0")
    remaining = max(0, 20 - kicked_today)
    if remaining <= 0:
        return

    async with db_pool.acquire() as con:
        rows = await con.fetch("""
        SELECT user_id
        FROM participants
        WHERE has_participated=FALSE
          AND joined_at < NOW() - INTERVAL '3 days'
        ORDER BY joined_at ASC
        LIMIT $1
        """, remaining)

    if not rows:
        return

    kicked = 0
    for r in rows:
        uid = r["user_id"]
        try:
            await context.bot.ban_chat_member(GROUP_ID, uid)
            await context.bot.unban_chat_member(GROUP_ID, uid, only_if_banned=True)
            kicked += 1
        except Exception as e:
            print(f"KICK NON PARTICIPANT ERROR {uid}: {e}", flush=True)

        async with db_pool.acquire() as con:
            await con.execute("DELETE FROM participants WHERE user_id=$1", uid)
            await con.execute("DELETE FROM pending_joins WHERE invited_user_id=$1", uid)

        await asyncio.sleep(0.1)

    if kicked:
        old_total = int(await get_setting("non_participants_kicked_total", "0") or "0")
        await set_setting("non_participants_kicked_total", old_total + kicked)
        await set_setting("nonparticipant_kicked_today", kicked_today + kicked)

        report = (
            "🥾 Bilan kick non-participants\n\n"
            f"Supprimés aujourd'hui : {kicked_today + kicked}/20\n"
            f"Nouveaux supprimés maintenant : {kicked}\n"
            f"Total supprimés pour non-participation : {old_total + kicked}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id, report)
            except Exception as e:
                print(f"KICK REPORT SKIPPED admin={admin_id}: admin must start the bot in private first ({e})", flush=True)



async def publish_campaign_ad(context: ContextTypes.DEFAULT_TYPE):
    c = await get_active_campaign()
    url = f"https://t.me/{BOT_USERNAME}?start=share" if BOT_USERNAME else None
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Mon lien", url=url)]]) if url else None
    text = c["text"] or "🎁 Partagez votre lien pour recevoir la rediffusion complète du groupe."

    if c["photo_file_id"]:
        msg = await context.bot.send_photo(GROUP_ID, c["photo_file_id"], caption=text, reply_markup=keyboard)
    else:
        msg = await context.bot.send_message(GROUP_ID, text, reply_markup=keyboard)

    await save_message(GROUP_ID, msg.message_id, None, True)
    return msg


async def publish_share_ad(context: ContextTypes.DEFAULT_TYPE):
    return await publish_share_publicity(context)


async def auto_ads_job(context: ContextTypes.DEFAULT_TYPE):
    if await get_setting("group_open", "off") != "on":
        return

    for key in ("ad1", "ad2"):
        if await get_setting(f"{key}_enabled", "off") != "on":
            continue
        text = await get_setting(f"{key}_text", "")
        if not text:
            continue
        msg = await context.bot.send_message(GROUP_ID, text)
        await save_message(GROUP_ID, msg.message_id, None, True)
        context.application.create_task(delete_later(context, GROUP_ID, msg.message_id, 180))


async def leaderboard_job(context: ContextTypes.DEFAULT_TYPE):
    if await get_setting("leaderboard_enabled", "on") != "on":
        return
    if await get_setting("group_open", "off") != "on":
        return
    text = await build_referral_leaderboard_text()
    if not text:
        return
    msg = await context.bot.send_message(GROUP_ID, text)
    await save_message(GROUP_ID, msg.message_id, None, True)
    context.application.create_task(delete_later(context, GROUP_ID, msg.message_id, 180))


async def publish_share_publicity(context: ContextTypes.DEFAULT_TYPE):
    text = await get_setting("share_publicity_text", "🤝 Partagez le groupe et montez dans le classement.")
    photo_id = await get_setting("share_publicity_photo_file_id", "")
    url = f"https://t.me/{BOT_USERNAME}?start=share" if BOT_USERNAME else None
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🤝 Je partage", url=url)]]) if url else None

    if photo_id:
        msg = await context.bot.send_photo(GROUP_ID, photo_id, caption=text, reply_markup=keyboard)
    else:
        msg = await context.bot.send_message(GROUP_ID, text, reply_markup=keyboard)

    await save_message(GROUP_ID, msg.message_id, None, True)
    return msg


async def broadcast_private_users(context: ContextTypes.DEFAULT_TYPE, text: str):
    async with db_pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM private_users ORDER BY last_seen_at DESC")

    sent = 0
    for r in rows:
        try:
            await context.bot.send_message(r["user_id"], text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    return sent


async def post_rules(context):
    if await get_setting("rules_auto", "off") != "on":
        return
    if await get_setting("group_open", "off") != "on":
        return
    old_id = int(await get_setting("rules_message_id", "0") or "0")
    if old_id:
        await delete_message_safe(context, GROUP_ID, old_id)
    text = await get_setting("rules_text", "")
    if not text:
        return
    msg = await context.bot.send_message(GROUP_ID, text)
    await save_message(GROUP_ID, msg.message_id, None, True)
    await set_setting("rules_message_id", msg.message_id)

async def ban_report(context):
    if await get_setting("group_open", "off") != "on":
        return

    deletions = int(await get_setting("session_deletions", "0") or "0")
    exclusions = int(await get_setting("session_exclusions", "0") or "0")
    mutes = int(await get_setting("session_mutes", "0") or "0")

    if deletions <= 0 and exclusions <= 0 and mutes <= 0:
        return

    text = (
        "⚠️ Veuillez respecter les règles du groupe.\n\n"
        "🛡️ Modération active :\n"
        f"• {deletions} suppressions\n"
        f"• {exclusions} exclusions\n"
        f"• {mutes} restrictions\n\n"
        "Merci de faire attention."
    )

    msg = await context.bot.send_message(GROUP_ID, text)
    await save_message(GROUP_ID, msg.message_id, None, True)
    context.application.create_task(delete_later(context, GROUP_ID, msg.message_id, 180))


# ---------------- SCHEDULE ----------------

def get_schedule_for_weekday(wd: int):
    # 0=lundi, 5=samedi, 6=dimanche
    if wd == 5:
        return {"open_hour": 23, "open_minute": 0, "close_hour": 1, "close_minute": 0}
    if wd == 6:
        return {"open_hour": 22, "open_minute": 30, "close_hour": 0, "close_minute": 15}
    return {"open_hour": 22, "open_minute": 0, "close_hour": 0, "close_minute": 0}


def get_schedule_for_day(now):
    return get_schedule_for_weekday(now.weekday())


def schedule_window_for_date(day: datetime, schedule: dict):
    open_dt = day.replace(
        hour=schedule["open_hour"],
        minute=schedule["open_minute"],
        second=0,
        microsecond=0,
    )
    close_dt = day.replace(
        hour=schedule["close_hour"],
        minute=schedule["close_minute"],
        second=0,
        microsecond=0,
    )
    if close_dt <= open_dt:
        close_dt += timedelta(days=1)
    return open_dt, close_dt


def active_schedule_window(now: datetime):
    # On vérifie d'abord la session commencée hier.
    # C'est indispensable pour samedi->dimanche 01h00 et dimanche->lundi 00h15.
    yesterday = now - timedelta(days=1)
    y_schedule = get_schedule_for_weekday(yesterday.weekday())
    y_open, y_close = schedule_window_for_date(yesterday, y_schedule)
    if y_open <= now < y_close:
        return y_schedule, y_open, y_close

    today_schedule = get_schedule_for_weekday(now.weekday())
    t_open, t_close = schedule_window_for_date(now, today_schedule)
    return today_schedule, t_open, t_close


def target_datetime(now: datetime, hour: int, minute: int):
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def minutes_until_dt(now: datetime, target: datetime) -> int:
    seconds = int((target - now).total_seconds())
    return max(0, (seconds + 59) // 60)


def minutes_until(now: datetime, hour: int, minute: int) -> int:
    return minutes_until_dt(now, target_datetime(now, hour, minute))


def is_open_window_precise(now: datetime, schedule: dict) -> bool:
    _, open_dt, close_dt = active_schedule_window(now)
    return open_dt <= now < close_dt


async def send_countdown_once(context: ContextTypes.DEFAULT_TYPE, key: str, text: str):
    last = await get_setting("last_countdown_key", "")
    if last == key:
        return
    await set_setting("last_countdown_key", key)
    await send_system_message(context, text, "countdown", record_in_session=False)


async def mid_session_leaderboard_job(context: ContextTypes.DEFAULT_TYPE):
    if await get_setting("leaderboard_enabled", "on") != "on":
        return
    if await get_setting("group_open", "off") != "on":
        return

    now = datetime.now(TZ)
    try:
        _, open_dt, close_dt = active_schedule_window(now)
    except Exception:
        return

    mid = open_dt + (close_dt - open_dt) / 2
    if abs((now - mid).total_seconds()) > 90:
        return

    key = f"leaderboard_mid_{open_dt.strftime('%Y%m%d%H%M')}"
    if await get_setting("last_leaderboard_key", "") == key:
        return

    text = await build_leaderboard_text()
    if not text:
        return

    await set_setting("last_leaderboard_key", key)
    msg = await context.bot.send_message(GROUP_ID, text)
    await save_message(GROUP_ID, msg.message_id, None, True)
    context.application.create_task(delete_later(context, GROUP_ID, msg.message_id, 180))


async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    if await get_setting("auto_schedule", "on") != "on":
        return

    now = datetime.now(TZ)
    schedule, open_dt, close_dt = active_schedule_window(now)

    group_open = await get_setting("group_open", "off")
    should_be_open = open_dt <= now < close_dt

    if should_be_open and group_open != "on":
        await set_setting("last_countdown_key", "")
        await open_group(context)
        await warn_non_participants(context)
        return

    if not should_be_open and group_open == "on":
        await set_setting("last_countdown_key", "")
        await close_group_and_clean(context)
        return

    # Groupe fermé : countdown vers la prochaine ouverture réelle.
    if group_open != "on":
        if now < open_dt:
            next_open = open_dt
        else:
            tomorrow = now + timedelta(days=1)
            tomorrow_schedule = get_schedule_for_weekday(tomorrow.weekday())
            next_open, _ = schedule_window_for_date(tomorrow, tomorrow_schedule)

        m = minutes_until_dt(now, next_open)
        suffix = next_open.strftime("%Y%m%d%H%M")

        if m > 60:
            if now.minute == 0:
                hours = max(1, (m + 59) // 60)
                await send_countdown_once(
                    context,
                    f"open_h_{hours}_{suffix}",
                    f"⏰ Prochaine ouverture dans {hours} heure(s)."
                )
            return

        if m == 60:
            await send_countdown_once(context, f"open_60_{suffix}", "⏰ Prochaine ouverture dans 60 minutes.")
        elif m == 30:
            await send_countdown_once(context, f"open_30_{suffix}", "⏰ Prochaine ouverture dans 30 minutes.")
        elif m == 10:
            await send_countdown_once(context, f"open_10_{suffix}", "⏰ Ouverture dans 10 minutes.")
        elif 1 <= m <= 5:
            await send_countdown_once(context, f"open_{m}_{suffix}", f"⏰ Ouverture dans {m} minute(s).")
        return

    # Groupe ouvert : countdown vers la fermeture de la session active.
    m = minutes_until_dt(now, close_dt)
    suffix = close_dt.strftime("%Y%m%d%H%M")

    if m == 30:
        await send_countdown_once(context, f"close_30_{suffix}", "⚠️ Fermeture du groupe dans 30 minutes.")
    elif m == 15:
        await send_countdown_once(context, f"close_15_{suffix}", "⚠️ Fermeture du groupe dans 15 minutes.")
    elif 1 <= m <= 5:
        await send_countdown_once(context, f"close_{m}_{suffix}", f"⚠️ Fermeture dans {m} minute(s).")


async def post_init(app):
    await init_db()
    app.job_queue.run_repeating(hourly_job, interval=60, first=10)
    app.job_queue.run_repeating(post_rules, interval=15 * 60, first=90)
    app.job_queue.run_repeating(ban_report, interval=20 * 60, first=120)
    app.job_queue.run_repeating(kick_old_non_participants, interval=6 * 60 * 60, first=300)
    app.job_queue.run_repeating(auto_ads_job, interval=10 * 60, first=240)
    app.job_queue.run_repeating(leaderboard_job, interval=60 * 60, first=3600)
    app.job_queue.run_repeating(mid_session_leaderboard_job, interval=60, first=30)


async def error_handler(update, context):
    print(f"ERROR: {context.error}", flush=True)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("supprime", trusted_supprime, filters=filters.Chat(GROUP_ID)))
    app.add_handler(CommandHandler("supprimer", trusted_supprime, filters=filters.Chat(GROUP_ID)))
    app.add_handler(CommandHandler("ban", trusted_ban, filters=filters.Chat(GROUP_ID)))
    app.add_handler(CommandHandler("pasfr", trusted_pasfr, filters=filters.Chat(GROUP_ID)))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, handle_private_admin))
    app.add_handler(MessageHandler(filters.Chat(GROUP_ID) & ~filters.COMMAND, handle_group_message))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(error_handler)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()

# trusted ban silent

# trusted supprimer silent
