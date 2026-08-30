from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db.models import ErrorLog, NetworkGroup, SessionLog, TrackedMessage, Vote
from app.db.session import SessionLocal
from app.keyboards.common import group_redirect_kb, vote_kb
from app.services import settings as st
from app.services.network import (
    STATUS_ACTIVE,
    STATUS_DEGRADED,
    STATUS_DISABLED,
    STATUS_LOST,
    STATUS_OFFLINE,
    active_chat_id,
    get_group,
    group_display_name,
    group_navigation_link,
    selected_chat_id,
)
from app.utils.time import countdown_text, day_key, in_slot


async def log_error(area, msg):
    logging.exception('%s: %s', area, msg) if isinstance(msg, Exception) else logging.error('%s: %s', area, msg)
    try:
        async with SessionLocal() as db:
            db.add(ErrorLog(area=area, message=str(msg)[:2000]))
            await db.commit()
    except Exception:
        pass


async def vote_count(chat_id: int):
    s = get_settings()
    async with SessionLocal() as db:
        res = await db.execute(select(func.count(Vote.id)).where(
            Vote.chat_id == chat_id,
            Vote.day_key == day_key(s.timezone),
        ))
        return int(res.scalar() or 0)


async def add_vote(chat_id: int, user_id: int):
    dk = day_key(get_settings().timezone)
    async with SessionLocal() as db:
        stmt = (
            pg_insert(Vote)
            .values(chat_id=chat_id, user_id=user_id, day_key=dk)
            .on_conflict_do_nothing(index_elements=['chat_id', 'user_id', 'day_key'])
            .returning(Vote.id)
        )
        inserted = (await db.execute(stmt)).scalar_one_or_none()
        await db.commit()
        return inserted is not None


async def clear_votes(chat_id: int) -> int:
    """Consomme le cycle de votes du jour après une ouverture réussie."""
    from sqlalchemy import delete
    async with SessionLocal() as db:
        result = await db.execute(delete(Vote).where(
            Vote.chat_id == chat_id,
            Vote.day_key == day_key(get_settings().timezone),
        ))
        await db.commit()
        return int(result.rowcount or 0)


async def status_text(chat_id: int):
    group = await get_group(chat_id)
    if not group or not group.approved:
        return '🟡 Groupe en attente d’autorisation du réseau.'

    active = await active_chat_id()
    selected = await selected_chat_id()

    if group.status == STATUS_LOST:
        return '💥 GROUPE DÉCLARÉ INDISPONIBLE\n\nCe groupe a été retiré de la rotation du réseau.'
    if group.status == STATUS_OFFLINE:
        return '🔴 GROUPE INDISPONIBLE\n\nLe réseau a perdu l’accès à ce groupe.'
    if group.status == STATUS_DISABLED or not group.enabled:
        return '⚫ GROUPE OFF\n\nCe groupe est actuellement désactivé dans le réseau.'

    if active == chat_id:
        slot = await st.group_time_slot(chat_id)
        closing = slot.split('-')[1]
        return (
            '🟢 GROUPE OUVERT\n\n'
            'Vous pouvez envoyer vos médias <3\n\n'
            f'Fermeture prévue à {closing}.'
        )

    # Tant qu'une session est active ailleurs, aucun autre groupe ne reçoit de vote.
    if active:
        name = await group_display_name(active)
        link = await group_navigation_link(active)
        suffix = '\n\n➡️ Utilisez le bouton ci-dessous.' if link else ''
        return f'🔴 MAINTENANCE\n\nLa session se déroule actuellement dans {name}.{suffix}'

    # Aucun groupe ouvert : seul le groupe sélectionné reçoit le système de vote.
    if selected == chat_id:
        goal = await st.group_vote_goal(chat_id)
        votes = await vote_count(chat_id)
        slot = await st.group_time_slot(chat_id)
        opening = slot.split('-')[0]
        missing = max(goal - votes, 0)

        if not await st.auto_enabled():
            return (
                '🔴 GROUPE SÉLECTIONNÉ — MODE MANUEL\n\n'
                'Ce groupe est le prochain groupe choisi.\n'
                'L’ouverture automatique est désactivée.'
            )
        if votes >= goal:
            if in_slot(slot, get_settings().timezone):
                return '🟢 OBJECTIF ATTEINT\n\nOuverture en cours...'
            remaining = countdown_text(slot, get_settings().timezone, achieved=True)
            return (
                f'🟡 OBJECTIF ATTEINT\n\nCe groupe ouvrira à {opening}.\n'
                f'Ouverture dans : {remaining}\n\nObjectif : {votes} / {goal} ✅'
            )
        remaining = countdown_text(slot, get_settings().timezone, achieved=False)
        return (
            f'🔴 GROUPE SÉLECTIONNÉ\n\nOuverture prévue à {opening}.\n'
            f'Temps restant : {remaining}\n\nObjectif : {votes} / {goal}\n'
            f'Il manque encore {missing} votes.'
        )

    if selected:
        name = await group_display_name(selected)
        link = await group_navigation_link(selected)
        suffix = '\n\n➡️ Rejoignez-le avec le bouton ci-dessous.' if link else ''
        return (
            '🔴 MAINTENANCE\n\n'
            f'Le prochain vote / la prochaine ouverture aura lieu dans {name}.{suffix}'
        )

    return '🔴 MAINTENANCE\n\nAucun groupe n’est sélectionné pour la prochaine ouverture.'


async def track(chat_id: int, message_id: int, user_id: int | None, kind='message', is_media=False):
    sid = int(await st.group_get_value(chat_id, 'active_session_id', '0', inherit_global=False) or '0')
    async with SessionLocal() as db:
        stmt = (
            pg_insert(TrackedMessage)
            .values(
                chat_id=chat_id, message_id=message_id, user_id=user_id,
                session_id=sid, kind=kind, is_media=is_media, deleted=False,
            )
            .on_conflict_do_nothing(index_elements=['chat_id', 'message_id'])
            .returning(TrackedMessage.id)
        )
        inserted = (await db.execute(stmt)).scalar_one_or_none()
        if inserted is not None and sid and kind != 'status':
            values = {'messages_seen': SessionLog.messages_seen + 1}
            if is_media:
                values['media_seen'] = SessionLog.media_seen + 1
            await db.execute(update(SessionLog).where(SessionLog.id == sid).values(**values))
        await db.commit()


async def _status_markup(chat_id: int):
    active = await active_chat_id()
    selected = await selected_chat_id()
    if not active and selected == chat_id and await st.auto_enabled():
        return vote_kb()
    target = active or selected
    if target and target != chat_id:
        link = await group_navigation_link(target)
        if link:
            name = await group_display_name(target)
            return group_redirect_kb(link, f'➡️ Rejoindre {name}'[:60])
    return None


async def ensure_status_message(bot: Bot, chat_id: int, recreate_on_change: bool = False):
    """Crée ou édite le message de statut d'un groupe.

    ``recreate_on_change`` est gardé pour compatibilité avec les anciens appels,
    mais un changement de texte ne provoque plus delete+send. En réseau
    multi-groupes, le compte à rebours peut changer chaque minute : recréer le
    message à chaque tick provoquerait une rafale Telegram inutile.
    """
    group = await get_group(chat_id)
    if not group or not group.approved:
        return None
    text = await status_text(chat_id)
    mid = await st.group_get_value(chat_id, 'status_message_id', '', inherit_global=False)
    last_text = await st.group_get_value(chat_id, 'status_last_text', '', inherit_global=False)
    kb = await _status_markup(chat_id)

    # Signature minimale du clavier. Cela permet d'éviter tout appel Telegram
    # pour les groupes maintenance/ouverts dont le statut n'a pas changé.
    active = await active_chat_id()
    selected = await selected_chat_id()
    target = active or selected
    link = await group_navigation_link(target) if target and target != chat_id else None
    markup_sig = f'vote:{selected}' if (not active and selected == chat_id and await st.auto_enabled()) else f'redirect:{target}:{link or ""}' if link else 'none'
    last_markup_sig = await st.group_get_value(chat_id, 'status_markup_sig', '', inherit_global=False)

    if mid and text == last_text and markup_sig == last_markup_sig:
        return int(mid)

    if mid:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=int(mid), reply_markup=kb, request_timeout=8,
            )
            await st.group_set_values(chat_id, {
                'last_status_update_at': datetime.utcnow().isoformat(timespec='seconds'),
                'status_last_text': text,
                'status_markup_sig': markup_sig,
            })
            return int(mid)
        except TelegramBadRequest as exc:
            low = str(exc).lower()
            if 'message is not modified' in low:
                await st.group_set_values(chat_id, {
                    'status_last_text': text,
                    'status_markup_sig': markup_sig,
                })
                return int(mid)
            if 'message to edit not found' not in low:
                await log_error('edit_status', exc)
                return int(mid)
            # Message réellement disparu : seulement ici on en recrée un.
            mid = ''
        except (TelegramNetworkError, TelegramServerError) as exc:
            logging.warning('edit_status reporté chat=%s: %s', chat_id, exc)
            return int(mid)
        except Exception as exc:
            await log_error('edit_status', exc)
            return int(mid)

    try:
        message = await bot.send_message(chat_id, text, reply_markup=kb, request_timeout=8)
    except Exception as exc:
        await log_error('send_status', exc)
        return None
    await st.group_set_values(chat_id, {
        'status_message_id': str(message.message_id),
        'status_last_text': text,
        'status_markup_sig': markup_sig,
        'last_status_update_at': datetime.utcnow().isoformat(timespec='seconds'),
    })
    await track(chat_id, message.message_id, None, 'status', False)
    await cleanup_known_status_duplicates(bot, chat_id)
    return message.message_id


async def ensure_all_status_messages(bot: Bot, recreate_on_change: bool = False):
    from app.services.network import list_groups
    groups = await list_groups(approved_only=True, include_removed=False)
    # Pas de rafale illimitée contre Telegram.
    import asyncio
    sem = asyncio.Semaphore(4)

    async def one(group: NetworkGroup):
        if group.status in {STATUS_OFFLINE, STATUS_LOST}:
            return
        async with sem:
            try:
                await ensure_status_message(bot, group.chat_id, recreate_on_change=recreate_on_change)
            except Exception as exc:
                logging.warning('status group=%s failed: %s', group.chat_id, exc)
    await asyncio.gather(*(one(g) for g in groups)) if groups else None


async def cleanup_known_status_duplicates(bot: Bot, chat_id: int):
    keep = int(await st.group_get_value(chat_id, 'status_message_id', '0', inherit_global=False) or '0')
    async with SessionLocal() as db:
        res = await db.execute(select(TrackedMessage).where(
            TrackedMessage.chat_id == chat_id,
            TrackedMessage.kind == 'status',
            TrackedMessage.deleted.is_(False),
        ))
        rows = list(res.scalars().all())
    deleted_ids = []
    for tm in rows:
        if tm.message_id == keep:
            continue
        try:
            await bot.delete_message(chat_id, tm.message_id, request_timeout=8)
            deleted_ids.append(tm.id)
        except TelegramBadRequest as exc:
            if 'message to delete not found' in str(exc).lower():
                deleted_ids.append(tm.id)
        except Exception:
            pass
    if deleted_ids:
        async with SessionLocal() as db:
            await db.execute(update(TrackedMessage).where(TrackedMessage.id.in_(deleted_ids)).values(deleted=True))
            await db.commit()
