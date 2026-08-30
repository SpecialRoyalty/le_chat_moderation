from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramServerError
from aiogram.types import Chat, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select, update

from app.config import get_settings
from app.db.models import (
    GlobalSanction,
    GroupInviteLink,
    InviteLink,
    NetworkEvent,
    NetworkGroup,
    NetworkMembership,
    NetworkState,
    SessionLog,
    Vote,
    User,
)
from app.db.session import SessionLocal
from app.services import settings as st
from app.utils.time import day_key

logger = logging.getLogger(__name__)

STATUS_PENDING = 'pending'
STATUS_CLOSED = 'closed'
STATUS_ACTIVE = 'active'
STATUS_DISABLED = 'disabled'
STATUS_DEGRADED = 'degraded'
STATUS_OFFLINE = 'offline'
STATUS_LOST = 'lost'
STATUS_REMOVED = 'removed'

OPERABLE_STATUSES = {STATUS_CLOSED, STATUS_ACTIVE, STATUS_DEGRADED}
AUTO_REPLACEMENT_STATUSES = {STATUS_CLOSED}
REQUIRED_ADMIN_PERMISSIONS = ('can_delete_messages', 'can_restrict_members', 'can_invite_users')


async def log_network_event(action: str, chat_id: int | None = None, *, user_id: int | None = None,
                            admin_id: int | None = None, details: str = '') -> None:
    try:
        async with SessionLocal() as db:
            db.add(NetworkEvent(
                action=action,
                chat_id=chat_id,
                user_id=user_id,
                admin_id=admin_id,
                details=(details or '')[:4000],
            ))
            await db.commit()
    except Exception:
        logger.exception('network event logging failed: %s', action)


async def get_network_state() -> NetworkState:
    async with SessionLocal() as db:
        row = await db.get(NetworkState, 1)
        if row is None:
            row = NetworkState(id=1)
            db.add(row)
            await db.commit()
        return row


async def _mutate_state(**values) -> None:
    async with SessionLocal() as db:
        row = await db.get(NetworkState, 1)
        if row is None:
            row = NetworkState(id=1)
            db.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.utcnow()
        await db.commit()


async def get_group(chat_id: int | None) -> NetworkGroup | None:
    if not chat_id:
        return None
    async with SessionLocal() as db:
        return await db.get(NetworkGroup, int(chat_id))


async def list_groups(*, approved_only: bool = False, enabled_only: bool = False,
                      include_removed: bool = False) -> list[NetworkGroup]:
    async with SessionLocal() as db:
        q = select(NetworkGroup)
        if approved_only:
            q = q.where(NetworkGroup.approved.is_(True))
        if enabled_only:
            q = q.where(NetworkGroup.enabled.is_(True))
        if not include_removed:
            q = q.where(NetworkGroup.status != STATUS_REMOVED)
        q = q.order_by(NetworkGroup.priority.asc(), NetworkGroup.created_at.asc())
        return list((await db.execute(q)).scalars().all())


async def operational_group_ids() -> list[int]:
    rows = await list_groups(approved_only=True, enabled_only=True)
    return [g.chat_id for g in rows if g.status in OPERABLE_STATUSES]


async def is_approved_group(chat_id: int) -> bool:
    group = await get_group(chat_id)
    return bool(group and group.approved and group.status != STATUS_REMOVED)


async def is_enabled_group(chat_id: int) -> bool:
    group = await get_group(chat_id)
    return bool(group and group.approved and group.enabled and group.status in OPERABLE_STATUSES)


async def active_chat_id() -> int | None:
    state = await get_network_state()
    if not state.active_chat_id:
        return None
    group = await get_group(state.active_chat_id)
    if not group or not group.approved or not group.enabled or group.status != STATUS_ACTIVE:
        return None
    return int(state.active_chat_id)


async def selected_chat_id() -> int | None:
    state = await get_network_state()
    if state.selected_chat_id:
        group = await get_group(state.selected_chat_id)
        if group and group.approved and group.enabled and group.status in OPERABLE_STATUSES:
            return int(group.chat_id)
    return None


async def default_target_chat_id() -> int | None:
    return await active_chat_id() or await selected_chat_id() or await choose_replacement(exclude=set())


async def group_display_name(group_or_id: NetworkGroup | int | None) -> str:
    group = group_or_id if isinstance(group_or_id, NetworkGroup) else await get_group(group_or_id)
    if not group:
        return 'Aucun groupe'
    return group.title or (f'@{group.username}' if group.username else str(group.chat_id))


def group_start_arg(chat_id: int) -> str:
    # Telegram start payload: caractères simples et stables, sans signe moins.
    return f'group_{abs(int(chat_id))}'


def chat_id_from_start_arg(arg: str) -> int | None:
    if not (arg or '').startswith('group_'):
        return None
    raw = (arg or '')[6:]
    if not raw.isdigit():
        return None
    return -int(raw)


async def group_navigation_link(chat_id: int | None) -> str | None:
    group = await get_group(chat_id)
    if not group:
        return None
    if group.public_link:
        return group.public_link
    if group.username:
        return f'https://t.me/{group.username.lstrip("@")}'

    # Groupe privé sans lien public : redirection via le bot. Au clic, le bot
    # vérifie à nouveau quel groupe est actif/sélectionné avant de générer un
    # lien d'invitation. Un ancien bouton ne peut donc pas ramener vers un
    # groupe devenu OFF/LOST.
    bot_username = (await st.get_value('bot_username', '')).strip().lstrip('@')
    if bot_username:
        return f'https://t.me/{bot_username}?start={group_start_arg(group.chat_id)}'
    return None


async def _upsert_group_from_chat(chat: Chat, *, approved: bool | None = None, enabled: bool | None = None,
                                  status: str | None = None, approved_by: int | None = None) -> NetworkGroup:
    async with SessionLocal() as db:
        row = await db.get(NetworkGroup, chat.id)
        if row is None:
            max_priority = (await db.execute(select(func.max(NetworkGroup.priority)))).scalar() or 0
            row = NetworkGroup(
                chat_id=chat.id,
                title=chat.title or '',
                username=getattr(chat, 'username', None),
                public_link=(f'https://t.me/{chat.username}' if getattr(chat, 'username', None) else None),
                approved=False,
                enabled=False,
                status=STATUS_PENDING,
                priority=int(max_priority) + 10,
            )
            db.add(row)
        row.title = chat.title or row.title or ''
        row.username = getattr(chat, 'username', None)
        if row.username and not row.public_link:
            row.public_link = f'https://t.me/{row.username}'
        row.last_seen_at = datetime.utcnow()
        if approved is not None:
            row.approved = approved
        if enabled is not None:
            row.enabled = enabled
        if status is not None:
            row.status = status
        if approved_by is not None:
            row.approved_by = approved_by
            row.approved_at = datetime.utcnow()
        await db.commit()
        return row


async def register_seen_group(chat: Chat) -> tuple[NetworkGroup, bool]:
    """Enregistre un groupe découvert. Retourne (groupe, est_nouveau_pending)."""
    existing = await get_group(chat.id)
    is_new = existing is None
    if existing and existing.approved:
        # Si un groupe connu revient après une perte, il reste approuvé mais OFF
        # jusqu'à décision admin. On ne le réactive jamais silencieusement.
        status = existing.status
        if status in {STATUS_OFFLINE, STATUS_LOST}:
            status = STATUS_DISABLED
        row = await _upsert_group_from_chat(chat, status=status)
        return row, False
    row = await _upsert_group_from_chat(chat, approved=False, enabled=False, status=STATUS_PENDING)
    return row, is_new or (existing is not None and existing.status != STATUS_PENDING)


async def approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Ajouter au réseau', callback_data=f'net_approve:{chat_id}'),
        InlineKeyboardButton(text='❌ Refuser', callback_data=f'net_reject:{chat_id}'),
    ]])


async def notify_admins(bot: Bot, text: str, reply_markup=None) -> None:
    await asyncio.gather(
        *(bot.send_message(uid, text, reply_markup=reply_markup) for uid in get_settings().admin_id_set),
        return_exceptions=True,
    )


async def approve_group(bot: Bot, chat_id: int, admin_id: int) -> tuple[bool, str]:
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status != 'administrator':
            return False, 'Le bot doit d’abord être administrateur du groupe.'
        missing = [name for name in REQUIRED_ADMIN_PERMISSIONS if getattr(member, name, False) is not True]
        if missing:
            return False, 'Droits admin manquants : ' + ', '.join(missing)
        # Un groupe nouvellement approuvé entre toujours dans le réseau FERME.
        # On confirme l'état réel Telegram avant d'écrire enabled=True en DB.
        await bot.set_chat_permissions(
            chat_id,
            permissions={'can_send_messages': False},
            request_timeout=10,
        )
    except Exception as exc:
        return False, f'Groupe inaccessible ou fermeture impossible : {type(exc).__name__}: {exc}'
    row = await _upsert_group_from_chat(
        chat, approved=True, enabled=True, status=STATUS_CLOSED, approved_by=admin_id,
    )
    state = await get_network_state()
    if not state.selected_chat_id:
        await _mutate_state(selected_chat_id=chat_id)
    await log_network_event('group_approved', chat_id, admin_id=admin_id, details=row.title)

    # IMPORTANT : on ne copie jamais les identifiants de messages/session de
    # l'ancien groupe vers un nouveau groupe. La migration legacy n'est faite
    # que pour MAIN_GROUP_ID dans bootstrap_network().

    # Réapplique les sanctions globales connues dans ce nouveau groupe.
    try:
        from app.services.sanctions import reconcile_group_sanctions
        await reconcile_group_sanctions(bot, chat_id)
    except Exception:
        logger.exception('sanction reconciliation failed on group approval')
    return True, f'✅ {row.title or chat_id} ajouté au réseau.'


async def reject_group(bot: Bot, chat_id: int, admin_id: int) -> None:
    async with SessionLocal() as db:
        row = await db.get(NetworkGroup, chat_id)
        if row:
            row.approved = False
            row.enabled = False
            row.status = STATUS_REMOVED
            row.updated_at = datetime.utcnow()
            await db.commit()
    await log_network_event('group_rejected', chat_id, admin_id=admin_id)
    try:
        await bot.leave_chat(chat_id)
    except Exception:
        pass


async def set_group_public_link(chat_id: int, link: str | None) -> None:
    async with SessionLocal() as db:
        row = await db.get(NetworkGroup, chat_id)
        if not row:
            return
        cleaned = (link or '').strip()
        row.public_link = cleaned or None
        row.updated_at = datetime.utcnow()
        await db.commit()


async def choose_replacement(exclude: set[int] | None = None) -> int | None:
    exclude = exclude or set()
    state = await get_network_state()
    # En failover automatique on choisit uniquement un groupe sain/fermé. Un
    # groupe DEGRADED reste sélectionnable manuellement par un admin, mais il
    # n'est jamais choisi automatiquement.
    if state.fallback_chat_id and state.fallback_chat_id not in exclude:
        g = await get_group(state.fallback_chat_id)
        if g and g.approved and g.enabled and g.status in AUTO_REPLACEMENT_STATUSES:
            return int(g.chat_id)
    rows = await list_groups(approved_only=True, enabled_only=True)
    for group in rows:
        if group.chat_id not in exclude and group.status in AUTO_REPLACEMENT_STATUSES:
            return int(group.chat_id)
    return None


async def set_selected_group(chat_id: int | None, admin_id: int | None = None) -> bool:
    if chat_id is not None:
        group = await get_group(chat_id)
        if not group or not group.approved or not group.enabled or group.status not in OPERABLE_STATUSES:
            return False
    state = await get_network_state()
    old = state.selected_chat_id
    # Changer de groupe sélectionné démarre un NOUVEAU cycle de vote pour la
    # cible. On supprime donc les voix résiduelles de la cible du même jour.
    # Si aucun groupe n'est actuellement ouvert, on annule aussi le vote de
    # l'ancienne cible. Cela évite qu'une sélection/failover récupère par erreur
    # des voix d'un ancien cycle.
    if old != chat_id:
        async with SessionLocal() as db:
            if chat_id is not None:
                await db.execute(
                    delete(Vote).where(
                        Vote.chat_id == chat_id,
                        Vote.day_key == day_key(get_settings().timezone),
                    )
                )
            if old and not state.active_chat_id:
                await db.execute(
                    delete(Vote).where(
                        Vote.chat_id == old,
                        Vote.day_key == day_key(get_settings().timezone),
                    )
                )
            await db.commit()
    await _mutate_state(selected_chat_id=chat_id)
    await log_network_event('selected_group_changed', chat_id, admin_id=admin_id, details=f'old={old}')
    return True


async def set_fallback_group(chat_id: int | None, admin_id: int | None = None) -> bool:
    if chat_id is not None and not await is_enabled_group(chat_id):
        return False
    await _mutate_state(fallback_chat_id=chat_id)
    await log_network_event('fallback_group_changed', chat_id, admin_id=admin_id)
    return True


async def set_failover_auto(enabled: bool, admin_id: int | None = None) -> None:
    await _mutate_state(failover_auto=enabled)
    await log_network_event('failover_auto', details=str(enabled), admin_id=admin_id)


async def mark_active(chat_id: int) -> None:
    async with SessionLocal() as db:
        rows = list((await db.execute(select(NetworkGroup).where(NetworkGroup.approved.is_(True)))).scalars().all())
        for row in rows:
            if row.chat_id == chat_id:
                row.status = STATUS_ACTIVE
                row.enabled = True
            elif row.status == STATUS_ACTIVE:
                row.status = STATUS_CLOSED if row.enabled else STATUS_DISABLED
        state = await db.get(NetworkState, 1)
        if state is None:
            state = NetworkState(id=1)
            db.add(state)
        state.active_chat_id = chat_id
        # Par défaut le groupe courant reste sélectionné; l'admin peut ensuite
        # choisir explicitement le prochain groupe pendant la session.
        if state.selected_chat_id is None:
            state.selected_chat_id = chat_id
        state.updated_at = datetime.utcnow()
        await db.commit()


async def clear_active(chat_id: int | None = None) -> None:
    async with SessionLocal() as db:
        state = await db.get(NetworkState, 1)
        if state is None:
            return
        old = state.active_chat_id
        if chat_id is not None and old != chat_id:
            return
        if old:
            row = await db.get(NetworkGroup, old)
            if row and row.status == STATUS_ACTIVE:
                row.status = STATUS_CLOSED if row.enabled else STATUS_DISABLED
        state.active_chat_id = None
        state.updated_at = datetime.utcnow()
        await db.commit()


async def set_group_enabled(chat_id: int, enabled: bool, admin_id: int | None = None) -> tuple[bool, str]:
    group = await get_group(chat_id)
    if not group or not group.approved:
        return False, 'Groupe non approuvé.'
    if enabled and group.status in {STATUS_OFFLINE, STATUS_LOST}:
        return False, 'Le groupe est indisponible. Réajoute/promouvoie d’abord le bot, puis réactive-le.'
    state = await get_network_state()
    if not enabled and state.active_chat_id == chat_id:
        return False, 'Ferme/transfère d’abord la session active.'
    async with SessionLocal() as db:
        row = await db.get(NetworkGroup, chat_id)
        row.enabled = enabled
        row.status = STATUS_CLOSED if enabled else STATUS_DISABLED
        row.updated_at = datetime.utcnow()
        await db.commit()
    if not enabled and state.selected_chat_id == chat_id:
        replacement = await choose_replacement({chat_id}) if state.failover_auto else None
        await set_selected_group(replacement, admin_id=admin_id)
    await log_network_event('group_enabled' if enabled else 'group_disabled', chat_id, admin_id=admin_id)
    return True, 'ON' if enabled else 'OFF'


async def invalidate_group_invites(bot: Bot, chat_id: int, reason: str) -> tuple[int, int]:
    """Invalide en DB immédiatement, puis tente la révocation Telegram."""
    async with SessionLocal() as db:
        rows = list((await db.execute(select(GroupInviteLink).where(
            GroupInviteLink.group_chat_id == chat_id,
            GroupInviteLink.active.is_(True),
        ))).scalars().all())
        links = [(row.id, row.link) for row in rows]
        for row in rows:
            row.active = False
            row.revoked_at = datetime.utcnow()
            row.revoked_reason = reason[:255]
        await db.commit()

    sem = asyncio.Semaphore(4)
    async def revoke(link: str) -> bool:
        if not link:
            return True
        async with sem:
            try:
                await bot.revoke_chat_invite_link(chat_id, link)
                return True
            except Exception:
                # Si le groupe a réellement sauté, l'appel Telegram peut être impossible.
                # La DB centrale l'a déjà invalidé, ce qui suffit pour ne plus le réutiliser.
                return False
    results = await asyncio.gather(*(revoke(link) for _id, link in links)) if links else []
    return len(links), sum(1 for ok in results if ok)


async def mark_group_unavailable(bot: Bot, chat_id: int, *, reason: str, lost: bool = False,
                                 admin_id: int | None = None) -> int | None:
    group = await get_group(chat_id)
    if not group or not group.approved:
        return None
    state = await get_network_state()
    status = STATUS_LOST if lost else STATUS_OFFLINE
    async with SessionLocal() as db:
        row = await db.get(NetworkGroup, chat_id)
        row.enabled = False
        row.status = status
        row.failure_count = max(row.failure_count, 3)
        row.last_health_check_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()
        ns = await db.get(NetworkState, 1)
        if ns is None:
            ns = NetworkState(id=1)
            db.add(ns)
        if ns.active_chat_id == chat_id:
            ns.active_chat_id = None
        await db.commit()

    # Ferme l'état de session/vote en base même si Telegram est devenu inaccessible.
    sid = int(await st.group_get_value(chat_id, 'active_session_id', '0', inherit_global=False) or '0')
    async with SessionLocal() as db:
        if sid:
            session = await db.get(SessionLog, sid)
            if session and session.status == 'open':
                session.status = 'cancelled_lost'
                session.closed_at = datetime.utcnow()
        await db.execute(
            delete(Vote).where(
                Vote.chat_id == chat_id,
                Vote.day_key == day_key(get_settings().timezone),
            )
        )
        await db.commit()
    await st.group_set_values(chat_id, {
        'active_session_id': '0',
        'manual_opened_at': '',
        'manual_security_warned_at': '',
    })

    if state.active_chat_id == chat_id:
        # Compatibilité avec l'ancienne clé mono-groupe : elle ne doit jamais
        # ressusciter un groupe perdu lors d'un redémarrage.
        await st.set_value('group_open', 'false')

    invalidated, revoked = await invalidate_group_invites(bot, chat_id, f'{status}:{reason}')
    replacement = None
    if state.selected_chat_id == chat_id:
        # La cible prévue est elle-même perdue : on choisit un remplaçant sain
        # uniquement si le failover est activé, sinon on laisse la cible vide.
        replacement = await choose_replacement({chat_id}) if state.failover_auto else None
        await set_selected_group(replacement, admin_id=admin_id)
    elif state.active_chat_id == chat_id:
        # Si l'admin avait déjà préparé un prochain groupe différent pendant la
        # session, on le CONSERVE. On ne remplace pas ce choix par la priorité
        # automatique simplement parce que le groupe actif vient de sauter.
        replacement = await selected_chat_id()
        if replacement is None and state.failover_auto:
            replacement = await choose_replacement({chat_id})
            await set_selected_group(replacement, admin_id=admin_id)

    await log_network_event(
        'group_lost' if lost else 'group_offline', chat_id, admin_id=admin_id,
        details=f'{reason}; invitations={invalidated}; revoked={revoked}; replacement={replacement}',
    )
    replacement_name = await group_display_name(replacement)
    await notify_admins(
        bot,
        f'🚨 Groupe {"déclaré SAUTÉ" if lost else "indisponible"}\n\n'
        f'{group.title or chat_id}\nRaison : {reason}\n'
        f'Invitations invalidées : {invalidated}\n'
        f'Prochain groupe : {replacement_name if replacement else "aucun"}',
    )
    # Met à jour immédiatement les messages maintenance des groupes encore
    # accessibles : le lien doit basculer sans intervention manuelle.
    try:
        from app.services.state import ensure_all_status_messages
        await ensure_all_status_messages(bot, recreate_on_change=True)
    except Exception:
        logger.exception('status refresh after group loss failed')
    return replacement


async def handle_bot_membership_update(event: ChatMemberUpdated, bot: Bot) -> None:
    """Découverte dynamique et perte forte via my_chat_member."""
    if event.chat.type not in {'group', 'supergroup'}:
        return
    status = event.new_chat_member.status
    if status in {'member', 'administrator'}:
        row, should_notify = await register_seen_group(event.chat)
        if row.approved:
            if status != 'administrator':
                await mark_group_unavailable(bot, event.chat.id, reason='le bot n’est plus administrateur', lost=False)
                return
            # Groupe retrouvé : on met à jour la trace mais il reste OFF si nous
            # l'avions désactivé après une perte.
            if row.status in {STATUS_DISABLED, STATUS_OFFLINE, STATUS_LOST}:
                await notify_admins(bot, f'ℹ️ Groupe retrouvé : {row.title}\nIl reste OFF jusqu’à réactivation admin.')
            return
        if should_notify:
            await notify_admins(
                bot,
                f'➕ Nouveau groupe détecté\n\n{event.chat.title or "Sans titre"}\nID : {event.chat.id}\n\n'
                'Le bot reste inactif dans ce groupe tant qu’un ADMIN_ID ne l’a pas accepté.',
                await approval_keyboard(event.chat.id),
            )
            await log_network_event('group_pending', event.chat.id, user_id=event.from_user.id if event.from_user else None)
        return
    if status in {'left', 'kicked'}:
        if await is_approved_group(event.chat.id):
            await mark_group_unavailable(bot, event.chat.id, reason=f'bot_status={status}', lost=False)


async def group_health_check(bot: Bot) -> None:
    """Contrôle léger. Les timeouts réseau ne déclarent jamais un groupe sauté."""
    groups = await list_groups(approved_only=True, include_removed=False)
    if not groups:
        return
    try:
        me = await bot.get_me(request_timeout=8)
    except Exception:
        return

    for group in groups:
        if group.status in {STATUS_LOST, STATUS_REMOVED, STATUS_PENDING}:
            continue
        try:
            member = await bot.get_chat_member(group.chat_id, me.id, request_timeout=8)
            if member.status in {'left', 'kicked'}:
                await mark_group_unavailable(bot, group.chat_id, reason=f'health_status={member.status}', lost=False)
                continue
            if member.status != 'administrator':
                await mark_group_unavailable(bot, group.chat_id, reason=f'bot_not_admin status={member.status}', lost=False)
                continue
            # Permissions critiques : on signale DEGRADED sans confondre avec
            # une disparition du groupe.
            missing_perms = []
            for attr in ('can_delete_messages', 'can_restrict_members', 'can_invite_users'):
                if getattr(member, attr, True) is False:
                    missing_perms.append(attr)
            recovered = False
            async with SessionLocal() as db:
                row = await db.get(NetworkGroup, group.chat_id)
                if row:
                    old_status = row.status
                    row.failure_count = 0
                    row.last_health_check_at = datetime.utcnow()
                    row.last_seen_at = datetime.utcnow()
                    if missing_perms:
                        if row.status != STATUS_ACTIVE:
                            row.status = STATUS_DEGRADED
                    elif row.status == STATUS_OFFLINE:
                        # Le groupe est revenu, mais on ne le réactive jamais
                        # automatiquement : validation humaine obligatoire.
                        row.enabled = False
                        row.status = STATUS_DISABLED
                        recovered = True
                    elif row.status == STATUS_DEGRADED:
                        row.status = STATUS_CLOSED if row.enabled else STATUS_DISABLED
                    await db.commit()
            if recovered:
                await notify_admins(bot, f'ℹ️ Groupe retrouvé : {group.title or group.chat_id}\nIl reste OFF jusqu’à réactivation admin.')
        except (TelegramNetworkError, TelegramServerError):
            # Réseau Telegram lent : seulement dégradé après répétition, jamais OFFLINE.
            async with SessionLocal() as db:
                row = await db.get(NetworkGroup, group.chat_id)
                if row:
                    row.failure_count += 1
                    row.last_health_check_at = datetime.utcnow()
                    if row.failure_count >= 3 and row.enabled and row.status != STATUS_ACTIVE:
                        row.status = STATUS_DEGRADED
                    await db.commit()
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            failures = 0
            async with SessionLocal() as db:
                row = await db.get(NetworkGroup, group.chat_id)
                if row:
                    row.failure_count += 1
                    failures = row.failure_count
                    row.last_health_check_at = datetime.utcnow()
                    await db.commit()
            if failures >= 2:
                await mark_group_unavailable(bot, group.chat_id, reason=f'{type(exc).__name__}: {exc}', lost=False)
        except Exception:
            logger.exception('health check failed for %s', group.chat_id)


async def membership_seen(user_id: int, chat_id: int, *, joined: bool = False) -> tuple[NetworkMembership, bool]:
    """Met à jour la présence et indique si l'utilisateur était déjà connu ailleurs."""
    now = datetime.utcnow()
    async with SessionLocal() as db:
        known_elsewhere = (await db.execute(select(NetworkMembership.user_id).where(
            NetworkMembership.user_id == user_id,
            NetworkMembership.chat_id != chat_id,
            NetworkMembership.status == 'member',
        ).limit(1))).scalar_one_or_none() is not None
        # Migration depuis l'ancien bot mono-groupe : users_test contient déjà
        # les membres connus, même si network_memberships_test vient d'être créé.
        known_legacy_user = await db.get(User, user_id) is not None
        known_before = known_elsewhere or known_legacy_user
        row = await db.get(NetworkMembership, (user_id, chat_id))
        if row is None:
            row = NetworkMembership(user_id=user_id, chat_id=chat_id)
            db.add(row)
        row.last_seen_at = now
        if joined:
            row.status = 'member'
            row.joined_at = now
            row.left_at = None
            row.known_before_join = known_before
            if known_before:
                # Migration/réentrée d'un membre déjà connu : évite le faux positif
                # "média dans les 60 secondes" lors d'un basculement de groupe.
                row.migration_exempt_until = now + timedelta(minutes=5)
        await db.commit()
        return row, known_before


async def membership_left(user_id: int, chat_id: int) -> None:
    async with SessionLocal() as db:
        row = await db.get(NetworkMembership, (user_id, chat_id))
        if row:
            row.status = 'left'
            row.left_at = datetime.utcnow()
            row.last_seen_at = datetime.utcnow()
            await db.commit()


async def migration_exempt(user_id: int, chat_id: int) -> bool:
    async with SessionLocal() as db:
        row = await db.get(NetworkMembership, (user_id, chat_id))
        return bool(row and row.migration_exempt_until and row.migration_exempt_until > datetime.utcnow())


async def migrate_legacy_settings(chat_id: int) -> None:
    """Copie les anciennes clés mono-groupe vers les clés scoppées, une fois."""
    keys = [
        'status_message_id', 'status_last_text', 'last_status_update_at',
        'active_session_id', 'manual_opened_at', 'manual_security_warned_at',
        'rules_message_id', 'last_rules_sent_at', 'last_ad_sent_at',
        'last_ad_message_id', 'last_invite_sent_at', 'last_invite_message_id',
        'last_top_sent_at',
    ]
    values: dict[str, str] = {}
    for key in keys:
        scoped = f'group:{chat_id}:{key}'
        if await st.get_value(scoped, ''):
            continue
        old = await st.get_value(key, '')
        if old:
            values[scoped] = old
    if values:
        await st.set_values(values)


async def bootstrap_network(bot: Bot) -> None:
    """Initialise le registre et migre sans destruction l'ancienne instance.

    MAIN_GROUP_ID est uniquement une aide de première migration. Une fois le
    réseau initialisé, network_state_test est la seule source de vérité.
    """
    await get_network_state()
    legacy = get_settings().main_group_id
    legacy_group = None

    if legacy:
        existing = await get_group(legacy)
        if not existing:
            try:
                chat = await bot.get_chat(legacy)
                legacy_group = await _upsert_group_from_chat(
                    chat, approved=True, enabled=True, status=STATUS_CLOSED,
                    approved_by=next(iter(get_settings().admin_id_set), None),
                )
            except Exception as exc:
                logger.warning('Legacy MAIN_GROUP_ID inaccessible au bootstrap: %s', exc)
                async with SessionLocal() as db:
                    row = NetworkGroup(
                        chat_id=legacy, title='Groupe principal (legacy)', approved=True,
                        enabled=False, status=STATUS_OFFLINE, priority=10,
                    )
                    db.add(row)
                    await db.commit()
                legacy_group = await get_group(legacy)
        else:
            legacy_group = existing

        # Les identifiants de statut/session historiques ne sont copiés QUE vers
        # le groupe legacy, jamais vers les nouveaux groupes.
        await migrate_legacy_settings(legacy)

        state = await get_network_state()
        if (
            state.selected_chat_id is None
            and legacy_group
            and legacy_group.approved
            and legacy_group.enabled
            and legacy_group.status in OPERABLE_STATUSES
        ):
            await _mutate_state(selected_chat_id=legacy)

        # Conserve une éventuelle session ouverte de l'ancienne version une seule
        # fois. Le marqueur empêche MAIN_GROUP_ID de ressusciter un groupe LOST
        # lors d'un futur redémarrage.
        legacy_state_done = await st.get_value('network_legacy_state_migrated', 'false') == 'true'
        if not legacy_state_done:
            if (
                (await st.get_value('group_open', 'false')) == 'true'
                and state.active_chat_id is None
                and legacy_group
                and legacy_group.enabled
                and legacy_group.status in OPERABLE_STATUSES
            ):
                await mark_active(legacy)
            await st.set_value('network_legacy_state_migrated', 'true')

        # Migration non destructive des anciens liens de parrainage vers le groupe legacy.
        async with SessionLocal() as db:
            old_links = list((await db.execute(select(InviteLink).where(InviteLink.active.is_(True)))).scalars().all())
            for old in old_links:
                exists = (await db.execute(select(GroupInviteLink.id).where(
                    GroupInviteLink.link == old.link,
                ).limit(1))).scalar_one_or_none()
                if exists is None:
                    db.add(GroupInviteLink(
                        owner_id=old.owner_id,
                        group_chat_id=legacy,
                        link=old.link,
                        active=old.active,
                        valid_count=old.valid_count,
                        suspect_count=old.suspect_count,
                        banned_count=old.banned_count,
                    ))
            await db.commit()

    # Les bans de users_test deviennent des sanctions réseau, que
    # MAIN_GROUP_ID soit encore défini ou non.
    if (await st.get_value('network_legacy_bans_migrated', 'false')) != 'true':
        async with SessionLocal() as db:
            banned_ids = list((await db.execute(select(User.id).where(User.is_banned.is_(True)))).scalars().all())
            for user_id in banned_ids:
                exists = (await db.execute(select(GlobalSanction.id).where(
                    GlobalSanction.user_id == user_id,
                    GlobalSanction.sanction_type == 'ban',
                    GlobalSanction.active.is_(True),
                ).limit(1))).scalar_one_or_none()
                if exists is None:
                    db.add(GlobalSanction(
                        user_id=user_id, sanction_type='ban', reason='legacy_migration',
                        source_chat_id=legacy, active=True,
                    ))
            await db.commit()
        await st.set_value('network_legacy_bans_migrated', 'true')


async def network_dashboard_text() -> str:
    groups = await list_groups(include_removed=False)
    state = await get_network_state()
    active = await group_display_name(state.active_chat_id)
    selected = await group_display_name(state.selected_chat_id)
    fallback = await group_display_name(state.fallback_chat_id)
    lines = [
        '🌐 RÉSEAU GROSCHAT', '',
        f'🟢 Actif : {active if state.active_chat_id else "aucun"}',
        f'🗳️ Sélectionné : {selected if state.selected_chat_id else "aucun"}',
        f'🛟 Secours : {fallback if state.fallback_chat_id else "automatique"}',
        f'🔄 Failover : {"ON" if state.failover_auto else "OFF"}', '',
    ]
    if not groups:
        lines.append('Aucun groupe enregistré. Ajoute le bot à un groupe puis accepte-le ici.')
    else:
        icon = {
            STATUS_ACTIVE: '🟢', STATUS_CLOSED: '🟠', STATUS_DISABLED: '⚫',
            STATUS_PENDING: '🟡', STATUS_DEGRADED: '⚠️', STATUS_OFFLINE: '🔴', STATUS_LOST: '💥',
        }
        for g in groups:
            lines.append(f'{icon.get(g.status,"▫️")} {g.title or g.chat_id} — {g.status.upper()} — {"ON" if g.enabled else "OFF"}')
    return '\n'.join(lines)
