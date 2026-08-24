from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import select, update
from aiogram import Bot
from aiogram.types import Message

from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import WordRule, MediaHash, User, RecentJoin
from app.services.users import protected, display_name
from app.services.state import track, log_error
from app.services.hashban import contains_banned_hash, media_file_entries
from app.services import settings as st


def has_link(text: str):
    return bool(re.search(r'(https?://|t\.me/|www\.|\.com\b|\.net\b|\.io\b)', text or '', re.I))


def has_mention(text: str):
    return '@' in (text or '')


def has_command(text: str):
    return (text or '').strip().startswith('/')


def is_media(msg: Message):
    return bool(msg.photo or msg.video or msg.document or msg.animation or msg.audio or msg.voice or msg.video_note)


def is_story(msg: Message):
    return getattr(msg, 'story', None) is not None


def file_ids(msg: Message):
    return [(u, f, t) for u, f, t, _size in media_file_entries(msg)]


def _normalise_rule_text(value: str) -> str:
    return unicodedata.normalize('NFKC', value or '').casefold()


def _isolated_rule_pattern(rule: str) -> re.Pattern[str] | None:
    """Recherche un mot/expression isolé(e), jamais une sous-chaîne.

    Exemple : ``cp`` correspond à ``cp``, ``cp!`` et ``je_cp_quoi``, mais pas
    à ``jecpquoi``, ``cp123`` ou ``123cp``.
    """
    normalised = _normalise_rule_text(rule).strip()
    if not normalised:
        return None
    parts = [re.escape(part) for part in normalised.split()]
    body = r'\s+'.join(parts)
    return re.compile(rf'(?<![^\W_]){body}(?![^\W_])', re.UNICODE)


# Les listes de mots changeant rarement, les recharger depuis PostgreSQL pour
# chaque message était un coût important. On charge les 3 listes en une fois et
# on garde directement les regex compilées.
_RULE_CACHE_TTL_SECONDS = 30.0
_RULE_CACHE_EXPIRES = 0.0
_RULE_PATTERN_CACHE: dict[str, list[re.Pattern[str]]] = {}
_RULE_CACHE_LOCK = asyncio.Lock()


def invalidate_word_cache(kind: str | None = None) -> None:
    global _RULE_CACHE_EXPIRES
    if kind is None:
        _RULE_PATTERN_CACHE.clear()
    else:
        _RULE_PATTERN_CACHE.pop(kind, None)
    _RULE_CACHE_EXPIRES = 0.0


async def _ensure_rule_cache() -> None:
    global _RULE_CACHE_EXPIRES, _RULE_PATTERN_CACHE
    now = time.monotonic()
    if _RULE_PATTERN_CACHE and now < _RULE_CACHE_EXPIRES:
        return
    async with _RULE_CACHE_LOCK:
        now = time.monotonic()
        if _RULE_PATTERN_CACHE and now < _RULE_CACHE_EXPIRES:
            return
        async with SessionLocal() as db:
            rows = list((await db.execute(select(WordRule))).scalars().all())
        compiled: dict[str, list[re.Pattern[str]]] = {'ban': [], 'forbidden': [], 'nameban': []}
        for row in rows:
            pattern = _isolated_rule_pattern(row.word)
            if pattern:
                compiled.setdefault(row.kind, []).append(pattern)
        _RULE_PATTERN_CACHE = compiled
        _RULE_CACHE_EXPIRES = now + _RULE_CACHE_TTL_SECONDS


async def words(kind):
    """Compatibilité : retourne les règles brutes du type demandé."""
    async with SessionLocal() as db:
        res = await db.execute(select(WordRule.word).where(WordRule.kind == kind))
        return [str(x).lower() for x in res.scalars().all()]


async def text_has_word(kind, text):
    value = _normalise_rule_text(text)
    if not value:
        return False
    await _ensure_rule_cache()
    return any(pattern.search(value) for pattern in _RULE_PATTERN_CACHE.get(kind, ()))


# Cache de l'heure d'arrivée. La base reste la source de vérité après un
# redémarrage, mais une fois un utilisateur vu on n'interroge plus PostgreSQL à
# chaque média.
_RECENT_JOIN_CACHE: dict[tuple[int, int], datetime | None] = {}
_USER_HAS_MEDIA_CACHE: dict[int, bool] = {}


def remember_recent_join(user_id: int, chat_id: int, joined_at: datetime | None = None) -> None:
    _RECENT_JOIN_CACHE[(user_id, chat_id)] = joined_at or datetime.utcnow()


async def _joined_within(user_id: int, chat_id: int, seconds: int) -> bool:
    key = (user_id, chat_id)
    if key not in _RECENT_JOIN_CACHE:
        async with SessionLocal() as db:
            recent = await db.get(RecentJoin, key)
            _RECENT_JOIN_CACHE[key] = recent.joined_at if recent else None
    joined_at = _RECENT_JOIN_CACHE.get(key)
    return bool(joined_at and datetime.utcnow() - joined_at <= timedelta(seconds=seconds))


async def _user_has_media(user_id: int) -> bool:
    cached = _USER_HAS_MEDIA_CACHE.get(user_id)
    if cached is not None:
        return cached
    async with SessionLocal() as db:
        media_count = (await db.execute(
            select(User.media_count).where(User.id == user_id)
        )).scalar_one_or_none()
    has_media = bool(media_count and media_count > 0)
    _USER_HAS_MEDIA_CACHE[user_id] = has_media
    return has_media


async def _store_metrics(**values: str) -> None:
    """Les métriques de santé ne doivent jamais ralentir une sanction."""
    try:
        await st.set_values(values)
    except Exception as exc:
        await log_error('moderation_metrics', exc)


async def restrict(bot: Bot, chat_id: int, user_id: int, days: int):
    if await protected(user_id):
        return
    until = datetime.utcnow() + timedelta(days=days)
    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions={'can_send_messages': False},
            until_date=until,
        )
        async with SessionLocal() as db:
            await db.execute(update(User).where(User.id == user_id).values(is_restricted=True))
            await db.commit()
    except Exception as exc:
        await log_error('restrict', exc)


async def ban(bot: Bot, chat_id: int, user_id: int):
    if await protected(user_id):
        return
    try:
        await bot.ban_chat_member(chat_id, user_id)
        async with SessionLocal() as db:
            await db.execute(update(User).where(User.id == user_id).values(is_banned=True))
            await db.commit()
    except Exception as exc:
        await log_error('ban', exc)


async def delete(bot: Bot, msg: Message):
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass


async def record_media(msg: Message, banned=False):
    entries = file_ids(msg)
    if not entries:
        return
    sid = int(await st.get_value('active_session_id', '0') or '0')
    async with SessionLocal() as db:
        for unique, file_id, media_type in entries:
            rows = list((await db.execute(
                select(MediaHash).where(MediaHash.file_unique_id == unique)
            )).scalars().all())
            if rows:
                for row in rows:
                    if banned:
                        row.banned = True
                    row.file_id = file_id
                    row.media_type = media_type
                    if msg.from_user:
                        row.user_id = msg.from_user.id
            else:
                db.add(MediaHash(
                    user_id=msg.from_user.id if msg.from_user else None,
                    file_unique_id=unique,
                    file_id=file_id,
                    media_type=media_type,
                    banned=banned,
                ))
            if msg.from_user and not banned:
                await db.execute(
                    update(User)
                    .where(User.id == msg.from_user.id)
                    .values(
                        media_count=User.media_count + 1,
                        last_media_session=sid,
                    )
                )
        await db.commit()
    if msg.from_user and not banned:
        _USER_HAS_MEDIA_CACHE[msg.from_user.id] = True


async def contains_known_media(msg: Message):
    ids = [x[0] for x in file_ids(msg)]
    if not ids:
        return False
    async with SessionLocal() as db:
        return (await db.execute(
            select(MediaHash.id)
            .where(MediaHash.file_unique_id.in_(ids))
            .limit(1)
        )).scalar_one_or_none() is not None


async def moderate_message(bot: Bot, msg: Message) -> bool:
    """Retourne False dès que le message est bloqué."""
    if not msg.from_user:
        return True

    s = get_settings()
    # Aucun traitement/écriture inutile pour les autres chats autorisés au bot.
    if msg.chat.id != s.main_group_id:
        return True

    uid = msg.from_user.id
    media = is_media(msg)
    await track(msg.chat.id, msg.message_id, uid, 'message', media)

    text = msg.text or msg.caption or ''
    trusted = uid in s.trusted_id_set
    admin = uid in s.admin_id_set

    if is_story(msg):
        await asyncio.gather(delete(bot, msg), ban(bot, msg.chat.id, uid))
        asyncio.create_task(_store_metrics(
            last_story_ban_user=str(uid),
            last_story_ban_at=datetime.utcnow().isoformat(timespec='seconds'),
        ))
        return False

    if not await st.is_open() and not (trusted or admin):
        await delete(bot, msg)
        return False

    if media:
        if await _joined_within(uid, msg.chat.id, 60):
            await asyncio.gather(delete(bot, msg), ban(bot, msg.chat.id, uid))
            asyncio.create_task(_store_metrics(
                last_fast_media_ban_user=str(uid),
                last_fast_media_ban_at=datetime.utcnow().isoformat(timespec='seconds'),
            ))
            return False

        blocked, details = await contains_banned_hash(bot, msg)
        if blocked:
            await asyncio.gather(delete(bot, msg), ban(bot, msg.chat.id, uid))
            asyncio.create_task(_store_metrics(
                last_hashban_method=str(details.get('method', 'unknown')),
                last_hashban_user=str(uid),
                last_hashban_at=datetime.utcnow().isoformat(timespec='seconds'),
            ))
            return False

        if (await st.get_value('repost_enabled', 'false')) == 'true' and await contains_known_media(msg):
            await delete(bot, msg)
            asyncio.create_task(_store_metrics(
                last_repost_blocked_at=datetime.utcnow().isoformat(timespec='seconds'),
                last_repost_blocked_user=str(uid),
            ))
            warn = await bot.send_message(
                msg.chat.id,
                f'{display_name(msg.from_user)}, média déjà posté : repost interdit.',
            )
            await track(msg.chat.id, warn.message_id, None, 'temp', False)
            return False
        await record_media(msg)

    if has_link(text):
        if trusted or admin:
            await delete(bot, msg)
        else:
            await asyncio.gather(delete(bot, msg), ban(bot, msg.chat.id, uid))
        return False
    if trusted or admin:
        return True
    if has_command(text):
        await asyncio.gather(delete(bot, msg), restrict(bot, msg.chat.id, uid, 1))
        return False
    if msg.video_note:
        await asyncio.gather(delete(bot, msg), restrict(bot, msg.chat.id, uid, 1))
        return False
    if has_mention(text):
        await asyncio.gather(delete(bot, msg), restrict(bot, msg.chat.id, uid, 2))
        return False
    if await text_has_word('ban', text):
        await asyncio.gather(delete(bot, msg), ban(bot, msg.chat.id, uid))
        return False
    if await text_has_word('forbidden', text):
        await asyncio.gather(delete(bot, msg), restrict(bot, msg.chat.id, uid, 1))
        return False
    if text and not media and not await _user_has_media(uid):
        await delete(bot, msg)
        warn = await bot.send_message(
            msg.chat.id,
            f'{display_name(msg.from_user)}, envoie d’abord un média avant d’écrire.',
        )
        await track(msg.chat.id, warn.message_id, None, 'temp', False)
        return False
    return True
