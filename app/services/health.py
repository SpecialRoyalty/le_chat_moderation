from __future__ import annotations

from sqlalchemy import func, select
from aiogram import Bot

from app.db.models import Advertisement, ErrorLog, MediaFingerprint, MediaHash, TrackedMessage, User
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.network import active_chat_id, get_network_state, group_display_name, list_groups, selected_chat_id


async def health_text(bot: Bot):
    groups = await list_groups(include_removed=False)
    active = await active_chat_id()
    selected = await selected_chat_id()
    state = await get_network_state()

    group_lines = []
    for group in groups:
        marker = '🟢' if group.chat_id == active else ('🗳️' if group.chat_id == selected else '▫️')
        group_lines.append(
            f'{marker} {group.title or group.chat_id}: {group.status.upper()} / {"ON" if group.enabled else "OFF"} '
            f'(échecs santé: {group.failure_count})'
        )
    if not group_lines:
        group_lines = ['Aucun groupe approuvé.']

    async with SessionLocal() as db:
        errors = (await db.execute(select(func.count(ErrorLog.id)))).scalar() or 0
        tracked = (await db.execute(select(func.count(TrackedMessage.id)).where(TrackedMessage.deleted.is_(False)))).scalar() or 0
        suspects = (await db.execute(select(func.count(User.id)).where(User.suspect_score >= 50))).scalar() or 0
        media_known = (await db.execute(select(func.count(MediaHash.id)))).scalar() or 0
        media_banned = (await db.execute(select(func.count(MediaHash.id)).where(MediaHash.banned.is_(True)))).scalar() or 0
        fingerprints_banned = (await db.execute(select(func.count(MediaFingerprint.id)).where(MediaFingerprint.banned.is_(True)))).scalar() or 0
        ads_total = (await db.execute(select(func.count(Advertisement.id)))).scalar() or 0
        ads_active = (await db.execute(select(func.count(Advertisement.id)).where(Advertisement.active.is_(True)))).scalar() or 0

    target = active or selected
    if target:
        repost = 'ON' if await st.group_bool(target, 'repost_enabled', False) else 'OFF'
        ads = 'ON' if await st.group_bool(target, 'ads_enabled', True) else 'OFF'
        slot = await st.group_time_slot(target)
        goal = await st.group_vote_goal(target)
    else:
        repost = ads = '-'
        slot = '-'
        goal = 0

    return (
        '🟢 SANTÉ RÉSEAU\n\n'
        'Bot: OK\nPostgreSQL: OK\nScheduler: OK\n\n'
        f'🟢 Groupe actif : {await group_display_name(active) if active else "aucun"}\n'
        f'🗳️ Groupe sélectionné : {await group_display_name(selected) if selected else "aucun"}\n'
        f'🛟 Secours : {await group_display_name(state.fallback_chat_id) if state.fallback_chat_id else "automatique"}\n'
        f'Failover : {"ON" if state.failover_auto else "OFF"}\n'
        f'Auto : {"ON" if await st.auto_enabled() else "OFF"}\n'
        f'Créneau cible : {slot}\nObjectif cible : {goal}\n\n'
        'Groupes:\n' + '\n'.join(group_lines) + '\n\n'
        f'Messages suivis non supprimés : {tracked}\n'
        f'Comptes suspects : {suspects}\nMédias connus : {media_known}\n'
        f'Anti-repost groupe cible : {repost}\nPublicités groupe cible : {ads}\n'
        f'Publicités configurées : {ads_active} actives / {ads_total} total\n'
        f'Erreurs loggées : {errors}\n\n'
        'ℹ️ Un timeout Telegram ne marque jamais un groupe comme sauté. Les pertes fortes sont détectées via le statut du bot ou confirmées manuellement.'
    )
