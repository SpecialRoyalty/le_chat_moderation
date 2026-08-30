from __future__ import annotations

from datetime import datetime, timedelta
import json

from aiogram import Bot
from aiogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models import GroupInviteLink, NetworkMembership, PendingInviteValidation, RecentJoin, User
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.moderation import remember_recent_join, text_has_word
from app.services.network import (
    active_chat_id,
    default_target_chat_id,
    get_group,
    is_approved_group,
    membership_left,
    membership_seen,
    selected_chat_id,
)
from app.services.sanctions import apply_user_sanctions_on_join
from app.services.state import log_error, track
from app.services.users import upsert_user

# Les validations d'invitation sont persistées dans PostgreSQL afin de survivre
# aux redémarrages Railway. Aucun cache mémoire n'est nécessaire ici.

DEFAULT_TIERS = [
    {'count': 1, 'label': '1 vidéo', 'link': ''},
    {'count': 10, 'label': '20 vidéos', 'link': ''},
    {'count': 50, 'label': '100 vidéos', 'link': ''},
    {'count': 100, 'label': '200 vidéos', 'link': ''},
    {'count': 300, 'label': '500 vidéos', 'link': ''},
    {'count': 500, 'label': '1 500 vidéos', 'link': ''},
    {'count': 1000, 'label': 'Accès bonus à vie', 'link': ''},
]


async def tiers():
    raw = await st.get_value('invite_tiers_json', '')
    if not raw:
        return DEFAULT_TIERS
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else DEFAULT_TIERS
    except Exception:
        return DEFAULT_TIERS


async def set_tiers_from_text(text: str):
    rows = []
    for line in (text or '').splitlines():
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 3 and parts[0].isdigit():
            rows.append({'count': int(parts[0]), 'label': parts[1], 'link': parts[2]})
    if not rows:
        return False
    rows = sorted(rows, key=lambda x: x['count'])
    await st.set_value('invite_tiers_json', json.dumps(rows, ensure_ascii=False))
    return True


async def invite_text():
    return await st.get_value(
        'invite_text',
        '🎁 Programme de récompenses\n\nInvite des membres et débloque tes récompenses.',
    )


async def invite_kb():
    username = get_settings().public_bot_username.strip().lstrip('@') or (await st.get_value('bot_username', '')).strip().lstrip('@')
    url = f'https://t.me/{username}?start=invite' if username else None
    if url:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='🔗 Recevoir mon lien', url=url),
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='🔗 Recevoir mon lien', callback_data='invite_private'),
    ]])


async def send_invite_ad(bot: Bot, force: bool = False, chat_id: int | None = None):
    target = chat_id or await active_chat_id()
    if not target:
        if not force:
            return None
        target = await default_target_chat_id()
    if not target:
        return None
    if not force and not await st.is_open(target):
        return None
    text = await invite_text()
    img = await st.get_value('invite_image_file_id', '')
    kb = await invite_kb()
    if img:
        message = await bot.send_photo(target, img, caption=text, reply_markup=kb)
        await track(target, message.message_id, None, 'invite_ad', True)
    else:
        message = await bot.send_message(target, text, reply_markup=kb)
        await track(target, message.message_id, None, 'invite_ad', False)
    await st.group_set_values(target, {
        'last_invite_sent_at': datetime.utcnow().isoformat(timespec='seconds'),
        'last_invite_message_id': str(message.message_id),
    })
    return message.message_id


async def get_or_create_link(bot: Bot, owner_id: int, group_chat_id: int | None = None):
    target = group_chat_id or await active_chat_id() or await selected_chat_id()
    if not target or not await is_approved_group(target):
        raise RuntimeError('Aucun groupe disponible pour générer une invitation.')
    group = await get_group(target)
    if not group or not group.enabled:
        raise RuntimeError('Le groupe cible est OFF.')

    async with SessionLocal() as db:
        row = (await db.execute(select(GroupInviteLink).where(
            GroupInviteLink.owner_id == owner_id,
            GroupInviteLink.group_chat_id == target,
            GroupInviteLink.active.is_(True),
        ).order_by(GroupInviteLink.id.desc()).limit(1))).scalar_one_or_none()
        if row and row.link:
            return row.link

    link_obj = await bot.create_chat_invite_link(
        target,
        name=f'groschat_{target}_{owner_id}',
        creates_join_request=False,
    )
    link = link_obj.invite_link
    async with SessionLocal() as db:
        db.add(GroupInviteLink(owner_id=owner_id, group_chat_id=target, link=link, active=True))
        await db.commit()
    return link


async def send_invite_private(bot: Bot, user_id: int, group_chat_id: int | None = None):
    target = group_chat_id or await active_chat_id() or await selected_chat_id()
    if not target:
        await bot.send_message(user_id, '🔴 Aucun groupe n’est disponible pour les invitations actuellement.')
        return
    link = await get_or_create_link(bot, user_id, target)
    group = await get_group(target)
    t = await tiers()
    lines = [
        '🎁 Ton lien unique',
        f'Groupe : {group.title if group else target}',
        '', link, '',
        'Chaque invité validé augmente ton compteur.', '', 'Paliers :',
    ]
    for row in t:
        lines.append(f"- {row['count']} invité(s) → {row['label']}")
    lines += ['', 'ℹ️ Si ce groupe devient indisponible, ce lien sera invalidé et tu pourras demander un nouveau lien pour le groupe de remplacement.']
    await bot.send_message(user_id, '\n'.join(lines))


async def on_join(event: ChatMemberUpdated, bot: Bot | None = None):
    if not await is_approved_group(event.chat.id):
        return
    if not event.new_chat_member:
        return
    joined_user = event.new_chat_member.user
    status = event.new_chat_member.status

    if status in ('left', 'kicked'):
        await membership_left(joined_user.id, event.chat.id)
        # Un départ avant les 5 minutes annule la validation de l'invitation.
        async with SessionLocal() as db:
            await db.execute(delete(PendingInviteValidation).where(
                PendingInviteValidation.user_id == joined_user.id,
                PendingInviteValidation.chat_id == event.chat.id,
            ))
            await db.commit()
        return
    if status not in ('member', 'restricted'):
        return

    # On consulte la connaissance réseau AVANT l'upsert : cela permet de
    # reconnaître les membres déjà présents dans users_test de l'ancienne
    # version sans considérer tous les nouveaux arrivants comme anciens.
    _membership, known_elsewhere = await membership_seen(joined_user.id, event.chat.id, joined=True)
    await upsert_user(joined_user, force=True)

    if bot and await apply_user_sanctions_on_join(bot, event.chat.id, joined_user.id):
        return

    name = ((joined_user.username or '') + ' ' + (joined_user.full_name or '')).strip()
    if bot and await text_has_word('nameban', name, event.chat.id):
        try:
            from app.services.moderation import ban
            await ban(bot, event.chat.id, joined_user.id, reason='nameban_join')
        except Exception as exc:
            await log_error('nameban_join', exc)
        return

    # L'heure reste locale au groupe. Un membre déjà connu dans un autre groupe
    # reçoit cependant une exemption de migration dans NetworkMembership.
    async with SessionLocal() as db:
        recent = await db.get(RecentJoin, (joined_user.id, event.chat.id))
        if recent:
            recent.joined_at = datetime.utcnow()
        else:
            db.add(RecentJoin(user_id=joined_user.id, chat_id=event.chat.id, joined_at=datetime.utcnow()))
        await db.commit()
    remember_recent_join(joined_user.id, event.chat.id)

    owner = None
    link_row_id = None
    inv = getattr(event, 'invite_link', None)
    link = getattr(inv, 'invite_link', None) if inv else None
    if link:
        async with SessionLocal() as db:
            row = (await db.execute(select(GroupInviteLink).where(
                GroupInviteLink.link == link,
                GroupInviteLink.group_chat_id == event.chat.id,
                GroupInviteLink.active.is_(True),
            ).limit(1))).scalar_one_or_none()
            if row:
                owner = row.owner_id
                link_row_id = row.id
    if owner and owner == joined_user.id:
        owner = None
    # Persistance de la validation différée. Si Telegram émet plusieurs updates
    # pour la même arrivée, la clé composite évite les doublons et remet le
    # délai à zéro sur la dernière arrivée réelle.
    async with SessionLocal() as db:
        pending = await db.get(PendingInviteValidation, (joined_user.id, event.chat.id))
        if pending:
            pending.owner_id = owner
            pending.group_invite_link_id = link_row_id
            pending.joined_at = datetime.utcnow()
        else:
            db.add(PendingInviteValidation(
                user_id=joined_user.id,
                chat_id=event.chat.id,
                owner_id=owner,
                group_invite_link_id=link_row_id,
                joined_at=datetime.utcnow(),
            ))
        await db.commit()


async def _maybe_reward(bot: Bot, owner: int):
    async with SessionLocal() as db:
        user = await db.get(User, owner)
        if not user:
            return
        counter = user.reward_counter
    available = [row for row in await tiers() if counter >= int(row.get('count', 0))]
    if not available:
        return
    reward = max(available, key=lambda row: int(row.get('count', 0)))
    async with SessionLocal() as db:
        user = await db.get(User, owner)
        if user:
            user.reward_counter = 0
            await db.commit()
    label = reward.get('label', 'Récompense')
    link = reward.get('link', '')
    msg = f'🎁 PALIER ATTEINT\n\nRécompense débloquée :\n{label}\n\nTon compteur récompense repart à 0.'
    if link:
        msg += f'\n\nLien :\n{link}'
    try:
        await bot.send_message(owner, msg)
    except Exception:
        pass


async def validate_invites(bot: Bot):
    """Valide les invitations âgées d'au moins 5 min, de façon persistante.

    La validation ne compte que si le membre est encore présent dans CE groupe.
    Une relance Railway entre l'arrivée et les 5 minutes ne perd donc plus le
    compteur et un membre parti rapidement ne crédite pas l'invitant.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    async with SessionLocal() as db:
        pending_rows = list((await db.execute(
            select(PendingInviteValidation)
            .where(PendingInviteValidation.joined_at <= cutoff)
            .order_by(PendingInviteValidation.joined_at.asc())
            .limit(500)
        )).scalars().all())

    for pending in pending_rows:
        owner = pending.owner_id
        should_credit = False
        async with SessionLocal() as db:
            membership = await db.get(NetworkMembership, (pending.user_id, pending.chat_id))
            should_credit = bool(membership and membership.status in ('member', 'restricted'))

            counter = total = 0
            if owner and should_credit:
                user = await db.get(User, owner)
                if user:
                    user.total_invites += 1
                    user.reward_counter += 1
                    user.weekly_invites += 1
                    counter = user.reward_counter
                    total = user.total_invites
                if pending.group_invite_link_id:
                    link_row = await db.get(GroupInviteLink, pending.group_invite_link_id)
                    if link_row:
                        link_row.valid_count += 1

            # Toujours consommer la validation arrivée à échéance, créditée ou
            # non. La clé composite interdit ainsi tout double crédit.
            await db.execute(delete(PendingInviteValidation).where(
                PendingInviteValidation.user_id == pending.user_id,
                PendingInviteValidation.chat_id == pending.chat_id,
            ))
            await db.commit()

        if owner and should_credit and (counter or total):
            try:
                await bot.send_message(
                    owner,
                    f'✅ +1 invité validé\n\nProgression récompense : {counter}\nTotal invités : {total}',
                )
            except Exception:
                pass
            await _maybe_reward(bot, owner)


async def top_text():
    async with SessionLocal() as db:
        users = list((await db.execute(select(User).where(
            User.weekly_invites >= 100,
        ).order_by(User.weekly_invites.desc()).limit(10))).scalars().all())
    if not users:
        return '🏆 TOP INVITEURS\n\nAucune statistique pour le moment.'
    lines = ['🏆 TOP INVITEURS — J-7', '']
    for i, user in enumerate(users, 1):
        name = ('@' + user.username[:2] + '****') if user.username else (user.full_name[:2] + '****')
        lines.append(f'{i}. {name} — {user.weekly_invites} invités')
    lines.append('\nLe TOP 3 débloque tous les avantages.\nFin du classement dans : 7 jours')
    return '\n'.join(lines)


async def invite_health_text(chat_id: int | None = None):
    target = chat_id or await active_chat_id() or await selected_chat_id()
    if not target:
        return '🎁 Invitations\n\nAucun groupe cible.'
    group = await get_group(target)
    async with SessionLocal() as db:
        links = list((await db.execute(select(GroupInviteLink).where(
            GroupInviteLink.group_chat_id == target,
            GroupInviteLink.active.is_(True),
        ))).scalars().all())
    return (
        f'🎁 Invitations — {group.title if group else target}\n\n'
        f'Dernière publication : {await st.group_get_value(target, "last_invite_sent_at", "jamais", inherit_global=False)}\n'
        f'Liens actifs : {len(links)}\n'
        f'Image configurée : {"oui" if await st.get_value("invite_image_file_id", "") else "non"}\n'
        f'Paliers : {len(await tiers())}'
    )


async def tiers_text():
    lines = ['🎁 Paliers actuels', '', 'Format édition : 1|Label|Lien GoFile']
    for row in await tiers():
        lines.append(f"{row['count']}|{row['label']}|{row.get('link', '')}")
    return '\n'.join(lines)
