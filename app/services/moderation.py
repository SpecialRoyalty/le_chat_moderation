from __future__ import annotations
import re
import unicodedata
from datetime import datetime, timedelta
from sqlalchemy import select
from aiogram import Bot
from aiogram.types import Message
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import WordRule, MediaHash, User, RecentJoin
from app.services.users import protected, display_name
from app.services.state import track, log_error
from app.services.hashban import contains_banned_hash, media_file_entries
from app.services import settings as st


def has_link(text: str): return bool(re.search(r'(https?://|t\.me/|www\.|\.com\b|\.net\b|\.io\b)', text or '', re.I))
def has_mention(text: str): return '@' in (text or '')
def has_command(text: str): return (text or '').strip().startswith('/')
def is_media(msg: Message): return bool(msg.photo or msg.video or msg.document or msg.animation or msg.audio or msg.voice or msg.video_note)
def is_story(msg: Message): return getattr(msg, 'story', None) is not None
def file_ids(msg: Message): return [(u, f, t) for u, f, t, _size in media_file_entries(msg)]


async def words(kind):
    async with SessionLocal() as db:
        res = await db.execute(select(WordRule).where(WordRule.kind == kind))
        return [x.word.lower() for x in res.scalars().all()]


def _normalise_rule_text(value: str) -> str:
    """Normalise la casse et les variantes Unicode sans retirer les accents."""
    return unicodedata.normalize('NFKC', value or '').casefold()


def _isolated_rule_pattern(rule: str) -> re.Pattern[str] | None:
    """Construit une recherche de mot/expression isolé(e).

    Les lettres et chiffres accolés empêchent la correspondance. Les espaces,
    tirets, ponctuations et underscores sont considérés comme séparateurs.
    Exemple : la règle ``cp`` correspond à ``cp``, ``cp!`` ou ``je_cp_quoi``,
    mais pas à ``jecpquoi``.
    """
    normalised = _normalise_rule_text(rule).strip()
    if not normalised:
        return None

    # Une expression enregistrée avec plusieurs espaces continue de fonctionner
    # avec un ou plusieurs espaces dans le message ou dans le nom.
    parts = [re.escape(part) for part in normalised.split()]
    body = r'\s+'.join(parts)

    # [^\W_] représente une lettre ou un chiffre Unicode, sans underscore.
    return re.compile(rf'(?<![^\W_]){body}(?![^\W_])', re.UNICODE)


async def text_has_word(kind, text):
    value = _normalise_rule_text(text)
    for word in await words(kind):
        pattern = _isolated_rule_pattern(word)
        if pattern and pattern.search(value):
            return True
    return False


async def restrict(bot: Bot, chat_id: int, user_id: int, days: int):
    if await protected(user_id): return
    until = datetime.utcnow() + timedelta(days=days)
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions={'can_send_messages': False}, until_date=until)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if user: user.is_restricted = True
            await db.commit()
    except Exception as exc:
        await log_error('restrict', exc)


async def ban(bot: Bot, chat_id: int, user_id: int):
    if await protected(user_id): return
    try:
        await bot.ban_chat_member(chat_id, user_id)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if user: user.is_banned = True
            await db.commit()
    except Exception as exc:
        await log_error('ban', exc)


async def delete(bot: Bot, msg: Message):
    try: await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception: pass


async def record_media(msg: Message, banned=False):
    for unique, file_id, media_type in file_ids(msg):
        async with SessionLocal() as db:
            rows = list((await db.execute(select(MediaHash).where(MediaHash.file_unique_id == unique))).scalars().all())
            if rows:
                for row in rows:
                    if banned: row.banned = True
                    row.file_id = file_id
                    row.media_type = media_type
                    if msg.from_user: row.user_id = msg.from_user.id
            else:
                db.add(MediaHash(
                    user_id=msg.from_user.id if msg.from_user else None,
                    file_unique_id=unique,
                    file_id=file_id,
                    media_type=media_type,
                    banned=banned,
                ))
            user = await db.get(User, msg.from_user.id) if msg.from_user else None
            if user and not banned:
                user.media_count += 1
                user.last_media_session = int(await st.get_value('active_session_id', '0') or '0')
            await db.commit()


async def contains_known_media(msg: Message):
    ids = [x[0] for x in file_ids(msg)]
    if not ids: return False
    async with SessionLocal() as db:
        return (await db.execute(select(MediaHash.id).where(MediaHash.file_unique_id.in_(ids)).limit(1))).scalar_one_or_none() is not None


async def moderate_message(bot: Bot, msg: Message) -> bool:
    """Retourne False dès que le message est bloqué afin d'arrêter tout pipeline suivant."""
    if not msg.from_user: return True
    await track(msg.chat.id, msg.message_id, msg.from_user.id, 'message', is_media(msg))
    if msg.chat.id != get_settings().main_group_id: return True

    uid = msg.from_user.id
    text = msg.text or msg.caption or ''
    trusted = uid in get_settings().trusted_id_set
    admin = uid in get_settings().admin_id_set

    # Toute story partagée ou transférée est supprimée et son expéditeur banni.
    # getattr est utilisé pour rester robuste avec les objets Message d'aiogram.
    if is_story(msg):
        await delete(bot, msg)
        await ban(bot, msg.chat.id, uid)
        await st.set_value('last_story_ban_user', str(uid))
        await st.set_value('last_story_ban_at', datetime.utcnow().isoformat(timespec='seconds'))
        return False

    if not await st.is_open() and not (trusted or admin):
        await delete(bot, msg)
        return False

    if is_media(msg):
        # Un membre qui publie un média moins de 60 secondes après son arrivée
        # est considéré comme un compte de spam. Les comptes protégés restent
        # protégés par la fonction ban(), conformément au reste du bot.
        async with SessionLocal() as db:
            recent_join = await db.get(RecentJoin, (uid, msg.chat.id))
            joined_at = recent_join.joined_at if recent_join else None

        if joined_at and datetime.utcnow() - joined_at <= timedelta(seconds=60):
            await delete(bot, msg)
            await ban(bot, msg.chat.id, uid)
            await st.set_value('last_fast_media_ban_user', str(uid))
            await st.set_value('last_fast_media_ban_at', datetime.utcnow().isoformat(timespec='seconds'))
            return False

        blocked, details = await contains_banned_hash(bot, msg)
        if blocked:
            await delete(bot, msg)
            await ban(bot, msg.chat.id, uid)
            await st.set_value('last_hashban_method', str(details.get('method', 'unknown')))
            await st.set_value('last_hashban_user', str(uid))
            await st.set_value('last_hashban_at', datetime.utcnow().isoformat(timespec='seconds'))
            return False
        if (await st.get_value('repost_enabled', 'false')) == 'true' and await contains_known_media(msg):
            await delete(bot, msg)
            await st.set_value('last_repost_blocked_at', datetime.utcnow().isoformat(timespec='seconds'))
            await st.set_value('last_repost_blocked_user', str(uid))
            warn = await bot.send_message(msg.chat.id, f'{display_name(msg.from_user)}, média déjà posté : repost interdit.')
            await track(msg.chat.id, warn.message_id, None, 'temp', False)
            return False
        await record_media(msg)

    if has_link(text):
        await delete(bot, msg)
        if not (trusted or admin): await ban(bot, msg.chat.id, uid)
        return False
    if trusted or admin: return True
    if has_command(text):
        await delete(bot, msg); await restrict(bot, msg.chat.id, uid, 1); return False
    if msg.video_note:
        await delete(bot, msg); await restrict(bot, msg.chat.id, uid, 1); return False
    if has_mention(text):
        await delete(bot, msg); await restrict(bot, msg.chat.id, uid, 2); return False
    if await text_has_word('ban', text):
        await delete(bot, msg); await ban(bot, msg.chat.id, uid); return False
    if await text_has_word('forbidden', text):
        await delete(bot, msg); await restrict(bot, msg.chat.id, uid, 1); return False
    if text and not is_media(msg):
        async with SessionLocal() as db:
            user = await db.get(User, uid)
            if user and user.media_count <= 0:
                await delete(bot, msg)
                warn = await bot.send_message(msg.chat.id, f'{display_name(msg.from_user)}, envoie d’abord un média avant d’écrire.')
                await track(msg.chat.id, warn.message_id, None, 'temp', False)
                return False
    return True
