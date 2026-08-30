from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db.models import ErrorLog, SessionLog, TrackedMessage, TrustedAction, User
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.network import (
    STATUS_LOST,
    STATUS_OFFLINE,
    active_chat_id,
    default_target_chat_id,
    get_group,
    list_groups,
    mark_active,
    clear_active,
    set_selected_group,
)
from app.services.state import clear_votes, ensure_all_status_messages, ensure_status_message, log_error

OPEN_PERMS = {
    'can_send_messages': True,
    'can_send_audios': True,
    'can_send_documents': True,
    'can_send_photos': True,
    'can_send_videos': True,
    'can_send_video_notes': False,
    'can_send_voice_notes': True,
    'can_send_polls': False,
    'can_send_other_messages': False,
    'can_add_web_page_previews': False,
}
CLOSED_PERMS = {'can_send_messages': False}

# Un seul basculement/ouverture/fermeture à la fois dans le processus. Cela
# empêche deux clics admin simultanés d'ouvrir deux groupes en concurrence.
_SESSION_SWITCH_LOCK = asyncio.Lock()


async def _set_permissions(bot: Bot, chat_id: int, opened: bool) -> bool:
    try:
        await bot.set_chat_permissions(chat_id, permissions=OPEN_PERMS if opened else CLOSED_PERMS, request_timeout=10)
        return True
    except Exception as exc:
        await log_error(f'permissions:{chat_id}', exc)
        return False


async def _create_session(chat_id: int, kind: str) -> int:
    async with SessionLocal() as db:
        session = SessionLog(chat_id=chat_id, kind=kind, status='open')
        db.add(session)
        await db.flush()
        sid = session.id
        await db.commit()
    await st.group_set_values(chat_id, {
        'active_session_id': str(sid),
        'manual_opened_at': datetime.utcnow().isoformat() if kind == 'manual' else '',
        'manual_security_warned_at': '',
    })
    return sid


async def close_active_session(chat_id: int):
    sid = int(await st.group_get_value(chat_id, 'active_session_id', '0', inherit_global=False) or '0')
    if sid:
        async with SessionLocal() as db:
            session = await db.get(SessionLog, sid)
            if session:
                session.status = 'closed'
                session.closed_at = datetime.utcnow()
            await db.commit()
    await st.group_set_values(chat_id, {
        'active_session_id': '0',
        'manual_opened_at': '',
        'manual_security_warned_at': '',
    })


async def _close_one(bot: Bot, chat_id: int, kind: str, *, cleanup: bool = True, report: bool = True):
    sid = int(await st.group_get_value(chat_id, 'active_session_id', '0', inherit_global=False) or '0')
    # On ne prétend jamais qu'un groupe est fermé si Telegram n'a pas confirmé
    # la fermeture des permissions. Le prochain tick/admin pourra réessayer.
    if not await _set_permissions(bot, chat_id, False):
        raise RuntimeError(f'Impossible de fermer les permissions du groupe {chat_id}.')
    if cleanup:
        await cleanup_session(bot, chat_id=chat_id, all_known=False)
    await close_active_session(chat_id)
    await clear_active(chat_id)
    if report and sid:
        await send_report(bot, kind, chat_id=chat_id, sid=sid)
    return True


async def set_group_open(bot: Bot, open_: bool, kind='auto', chat_id: int | None = None):
    """Ouvre/ferme un groupe du réseau de façon exclusive et fail-safe."""
    async with _SESSION_SWITCH_LOCK:
        if open_:
            target = chat_id or await default_target_chat_id()
            if not target:
                raise RuntimeError('Aucun groupe approuvé/ON à ouvrir.')
            group = await get_group(target)
            if not group or not group.approved or not group.enabled or group.status in {STATUS_LOST, STATUS_OFFLINE}:
                raise RuntimeError('Le groupe cible n’est pas disponible.')

            current = await active_chat_id()
            if current == target:
                await ensure_all_status_messages(bot)
                return target

            # 1) Ferme d'abord le groupe réellement actif. Si Telegram refuse,
            # le transfert est annulé : on n'ouvre surtout pas le nouveau.
            if current and current != target:
                await _close_one(bot, current, 'transfer', cleanup=True, report=True)

            # 2) Fermeture de sécurité des autres groupes. Toute fermeture d'un
            # groupe ON doit réussir avant d'ouvrir la cible. Les groupes OFF
            # sont best-effort uniquement.
            groups = await list_groups(approved_only=True, include_removed=False)
            candidates = [
                g for g in groups
                if g.chat_id not in {target, current}
                and g.status not in {STATUS_LOST, STATUS_OFFLINE}
            ]
            sem = asyncio.Semaphore(4)

            async def close_perm(g):
                async with sem:
                    ok = await _set_permissions(bot, g.chat_id, False)
                    return g, ok

            results = await asyncio.gather(*(close_perm(g) for g in candidates)) if candidates else []
            failed_required = [g for g, ok in results if not ok and g.enabled]
            if failed_required:
                names = ', '.join(str(g.title or g.chat_id) for g in failed_required[:5])
                raise RuntimeError(f'Ouverture annulée : impossible de confirmer la fermeture de {names}.')

            # 3) La cible est ouverte seulement après toutes les confirmations.
            if not await _set_permissions(bot, target, True):
                raise RuntimeError('Impossible d’ouvrir les permissions du groupe cible.')

            session_created = False
            try:
                await _create_session(target, kind)
                session_created = True
                await mark_active(target)
                await st.set_value('group_open', 'true')  # compatibilité ancienne clé
                await set_selected_group(target)
                # Les votes ayant servi à cette ouverture sont consommés. Sans
                # cela une fermeture manuelle pendant le créneau pourrait rouvrir
                # automatiquement le groupe au tick suivant.
                await clear_votes(target)
            except Exception:
                # Échec DB après ouverture Telegram : retour immédiat en fermé.
                await _set_permissions(bot, target, False)
                if session_created:
                    await close_active_session(target)
                await clear_active(target)
                await st.set_value('group_open', 'false')
                raise

            await ensure_all_status_messages(bot, recreate_on_change=True)
            return target

        target = chat_id or await active_chat_id()
        if not target:
            await ensure_all_status_messages(bot)
            return None
        await _close_one(bot, target, kind, cleanup=True, report=True)
        if not await active_chat_id():
            await st.set_value('group_open', 'false')
        await ensure_all_status_messages(bot, recreate_on_change=True)
        return target


async def transfer_session(bot: Bot, target_chat_id: int, kind: str = 'manual_transfer') -> int:
    # set_group_open ne modifie la sélection qu'après une ouverture réussie.
    # Un transfert raté laisse donc intact l'état logique du réseau.
    return int(await set_group_open(bot, True, kind, chat_id=target_chat_id))


async def cleanup_session(bot: Bot, chat_id: int | None = None, all_known: bool = False):
    target = chat_id or await active_chat_id() or await default_target_chat_id()
    if not target:
        return 0, 0
    sid = int(await st.group_get_value(target, 'active_session_id', '0', inherit_global=False) or '0')
    async with SessionLocal() as db:
        q = select(
            TrackedMessage.id, TrackedMessage.chat_id,
            TrackedMessage.message_id, TrackedMessage.is_media,
        ).where(
            TrackedMessage.chat_id == target,
            TrackedMessage.deleted.is_(False),
            TrackedMessage.kind != 'status',
        )
        if sid and not all_known:
            q = q.where(TrackedMessage.session_id == sid)
        items = (await db.execute(q)).all()

    sem = asyncio.Semaphore(8)
    async def remove(item):
        async with sem:
            try:
                await bot.delete_message(item.chat_id, item.message_id)
                return True, None
            except TelegramBadRequest as exc:
                if 'message to delete not found' in str(exc).lower():
                    return True, None
                return False, exc
            except Exception as exc:
                return False, exc

    results = await asyncio.gather(*(remove(item) for item in items)) if items else []
    deleted_ids = [item.id for item, (ok, _exc) in zip(items, results) if ok]
    failures = [(item, exc) for item, (ok, exc) in zip(items, results) if not ok]
    deleted = len(deleted_ids)
    failed = len(failures)
    media_failed = sum(1 for item, _exc in failures if item.is_media)

    async with SessionLocal() as db:
        if deleted_ids:
            await db.execute(update(TrackedMessage).where(TrackedMessage.id.in_(deleted_ids)).values(deleted=True))
        if sid and deleted:
            await db.execute(update(SessionLog).where(SessionLog.id == sid).values(
                messages_deleted=SessionLog.messages_deleted + deleted,
            ))
        await db.commit()

    if failures:
        sample = '; '.join(f'{item.chat_id}/{item.message_id}: {exc}' for item, exc in failures[:5])
        await log_error('cleanup_delete', f'{failed} échec(s). Exemples: {sample}')
        await notify_admins(
            bot,
            f'🚨 ERREUR NETTOYAGE\n\nGroupe : {target}\nMessages non supprimés : {failed}\n'
            f'Médias non supprimés : {media_failed}\n\nVérifie les droits “Supprimer les messages”.',
        )
    return deleted, failed


async def notify_admins(bot: Bot, text: str, reply_markup=None):
    await asyncio.gather(
        *(bot.send_message(aid, text, reply_markup=reply_markup) for aid in get_settings().admin_id_set),
        return_exceptions=True,
    )


async def send_report(bot: Bot, kind='auto', chat_id: int | None = None, sid: int | None = None):
    target = chat_id or await default_target_chat_id()
    async with SessionLocal() as db:
        session = await db.get(SessionLog, sid) if sid else None
        if not session and target:
            session = (await db.execute(select(SessionLog).where(
                SessionLog.chat_id == target,
            ).order_by(SessionLog.id.desc()).limit(1))).scalar_one_or_none()
        actions = await db.execute(select(
            TrustedAction.trusted_username, TrustedAction.command, func.count(TrustedAction.id),
        ).group_by(TrustedAction.trusted_username, TrustedAction.command))
        action_lines = [f'@{name or "trusted"} {cmd}: {count}' for name, cmd, count in actions.all()]
        inactive_count = (await db.execute(select(func.count(User.id)).where(User.media_count == 0))).scalar() or 0
        errors = (await db.execute(select(func.count(ErrorLog.id)).where(
            ErrorLog.created_at >= datetime.utcnow() - timedelta(hours=24),
        ))).scalar() or 0
        remain = 0
        if sid:
            remain = (await db.execute(select(func.count(TrackedMessage.id)).where(
                TrackedMessage.session_id == sid,
                TrackedMessage.deleted.is_(False),
                TrackedMessage.kind != 'status',
            ))).scalar() or 0
    group = await get_group(target) if target else None
    text = (
        f'📊 RAPPORT DE SESSION\n\nGroupe : {(group.title if group else target) or "?"}\nType : {kind}\n'
        f'Messages vus : {session.messages_seen if session else 0}\n'
        f'Médias vus : {session.media_seen if session else 0}\n'
        f'Messages supprimés : {session.messages_deleted if session else 0}\n'
        f'Messages restants suivis : {remain}\n\nInactifs jamais média : {inactive_count}\n\n'
        'Actions trusted :\n' + ('\n'.join(action_lines[-20:]) or 'Aucune') +
        f'\n\nErreurs 24h : {errors}'
    )
    await notify_admins(bot, text)


async def security_close_if_manual(bot: Bot):
    target = await active_chat_id()
    if not target or await st.auto_enabled():
        return
    opened = await st.group_get_value(target, 'manual_opened_at', '', inherit_global=False)
    if not opened:
        return
    try:
        dt = datetime.fromisoformat(opened)
    except Exception:
        return
    if datetime.utcnow() - dt < timedelta(hours=2):
        return
    warned = await st.group_get_value(target, 'manual_security_warned_at', '', inherit_global=False)
    if not warned:
        kb = InlineKeyboardMarkup(inline_keyboard=[[ 
            InlineKeyboardButton(text='✅ Maintenir ouvert', callback_data='manual_keep_open'),
            InlineKeyboardButton(text='🔒 Fermer', callback_data='manual_security_close'),
        ]])
        await st.group_set_value(target, 'manual_security_warned_at', datetime.utcnow().isoformat())
        await notify_admins(
            bot,
            '⚠️ FERMETURE DE SÉCURITÉ\n\nLe groupe actif est ouvert manuellement depuis 2h.\n'
            'Sans réponse sous 5 minutes : fermeture.',
            kb,
        )
        return
    try:
        warned_dt = datetime.fromisoformat(warned)
    except Exception:
        warned_dt = datetime.utcnow() - timedelta(minutes=10)
    if datetime.utcnow() - warned_dt >= timedelta(minutes=5):
        await set_group_open(bot, False, 'security', chat_id=target)
