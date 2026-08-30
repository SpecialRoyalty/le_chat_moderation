from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import select, update

from app.config import get_settings
from app.db.models import GlobalSanction, NetworkGroup, User
from app.db.session import SessionLocal
from app.services import settings as st

logger = logging.getLogger(__name__)


def _protected(user_id: int) -> bool:
    return user_id in get_settings().all_admin_ids


async def _reachable_group_ids() -> list[int]:
    from app.services.network import STATUS_LOST, STATUS_OFFLINE, STATUS_PENDING, STATUS_REMOVED, list_groups
    groups = await list_groups(approved_only=True, include_removed=False)
    return [
        g.chat_id for g in groups
        if g.status not in {STATUS_LOST, STATUS_OFFLINE, STATUS_PENDING, STATUS_REMOVED}
    ]


async def _record_sanction(user_id: int, sanction_type: str, *, reason: str,
                           source_chat_id: int | None, created_by: int | None,
                           until_at: datetime | None = None) -> None:
    """Persiste d'abord la sanction, puis les appels Telegram peuvent échouer.

    Une seule sanction active par utilisateur/type est conservée : cela évite
    que des /pedo répétés gonflent la table et rende la réconciliation lente.
    """
    async with SessionLocal() as db:
        row = (await db.execute(select(GlobalSanction).where(
            GlobalSanction.user_id == user_id,
            GlobalSanction.sanction_type == sanction_type,
            GlobalSanction.active.is_(True),
        ).order_by(GlobalSanction.id.desc()).limit(1))).scalar_one_or_none()
        if row is None:
            row = GlobalSanction(user_id=user_id, sanction_type=sanction_type, active=True)
            db.add(row)
        row.reason = reason
        row.source_chat_id = source_chat_id
        row.created_by = created_by
        row.until_at = until_at
        row.active = True
        if sanction_type == 'ban':
            await db.execute(update(User).where(User.id == user_id).values(is_banned=True))
        elif sanction_type == 'restrict':
            await db.execute(update(User).where(User.id == user_id).values(is_restricted=True))
        await db.commit()


async def ban_global(bot: Bot, user_id: int, *, source_chat_id: int | None = None,
                     reason: str = 'moderation', created_by: int | None = None) -> dict[int, bool]:
    if _protected(user_id):
        return {}
    # La DB est la source de vérité : même si Telegram timeout ensuite, la
    # sanction sera réappliquée à la prochaine arrivée/réconciliation.
    await _record_sanction(user_id, 'ban', reason=reason, source_chat_id=source_chat_id, created_by=created_by)
    group_ids = await _reachable_group_ids()

    async def one(chat_id: int):
        try:
            await bot.ban_chat_member(chat_id, user_id, request_timeout=10)
            return chat_id, True
        except Exception as exc:
            logger.warning('global ban failed user=%s chat=%s: %s', user_id, chat_id, exc)
            return chat_id, False

    results = await asyncio.gather(*(one(chat_id) for chat_id in group_ids)) if group_ids else []
    return dict(results)


async def restrict_global(bot: Bot, user_id: int, days: int, *, source_chat_id: int | None = None,
                          reason: str = 'moderation', created_by: int | None = None) -> dict[int, bool]:
    if _protected(user_id):
        return {}
    until = datetime.utcnow() + timedelta(days=days)
    await _record_sanction(
        user_id, 'restrict', reason=reason, source_chat_id=source_chat_id,
        created_by=created_by, until_at=until,
    )
    group_ids = await _reachable_group_ids()

    async def one(chat_id: int):
        try:
            await bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions={'can_send_messages': False},
                until_date=until,
                request_timeout=10,
            )
            return chat_id, True
        except Exception as exc:
            logger.warning('global restrict failed user=%s chat=%s: %s', user_id, chat_id, exc)
            return chat_id, False

    results = await asyncio.gather(*(one(chat_id) for chat_id in group_ids)) if group_ids else []
    return dict(results)


async def capture_manual_admin_ban(bot: Bot, event) -> bool:
    """Propage un ban manuel Telegram à tout le réseau.

    Le Bot API envoie un ``chat_member`` quand un administrateur bannit un
    membre depuis l'interface Telegram. Les bans déclenchés par le bot lui-même
    sont ignorés pour éviter une boucle de propagation. La sanction est
    persistée avant les appels Telegram, donc elle reste valable pour les
    groupes encore non rejoints, momentanément hors-ligne ou ajoutés plus tard.
    """
    try:
        new_member = getattr(event, 'new_chat_member', None)
        old_member = getattr(event, 'old_chat_member', None)
        if not new_member or getattr(new_member, 'status', None) != 'kicked':
            return False
        if old_member and getattr(old_member, 'status', None) == 'kicked':
            return False

        target = getattr(new_member, 'user', None)
        if not target or _protected(target.id):
            return False

        actor = getattr(event, 'from_user', None)
        bot_id_raw = await st.get_value('bot_id', '0')
        try:
            bot_id = int(bot_id_raw or 0)
        except (TypeError, ValueError):
            bot_id = 0
        if actor and bot_id and actor.id == bot_id:
            return False

        await ban_global(
            bot,
            target.id,
            source_chat_id=getattr(getattr(event, 'chat', None), 'id', None),
            reason='manual_admin_ban',
            created_by=actor.id if actor else None,
        )
        return True
    except Exception as exc:
        logger.warning('manual ban capture failed: %s', exc)
        return False


async def apply_user_sanctions_on_join(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Retourne True si un ban global a été réappliqué."""
    if _protected(user_id):
        return False
    now = datetime.utcnow()
    async with SessionLocal() as db:
        rows = list((await db.execute(select(GlobalSanction).where(
            GlobalSanction.user_id == user_id,
            GlobalSanction.active.is_(True),
        ).order_by(GlobalSanction.id.desc()))).scalars().all())
        changed = False
        for row in rows:
            if row.until_at and row.until_at <= now:
                row.active = False
                changed = True
        if changed:
            await db.commit()
    active = [r for r in rows if r.active and (not r.until_at or r.until_at > now)]
    if any(r.sanction_type == 'ban' for r in active):
        try:
            await bot.ban_chat_member(chat_id, user_id)
        except Exception:
            pass
        return True
    restrictions = [r for r in active if r.sanction_type == 'restrict']
    if restrictions:
        until = max((r.until_at for r in restrictions if r.until_at), default=now + timedelta(days=1))
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions={'can_send_messages': False}, until_date=until)
        except Exception:
            pass
    return False


async def reconcile_group_sanctions(bot: Bot, chat_id: int, limit: int | None = None) -> None:
    """Réapplique toutes les sanctions actives quand un groupe revient.

    Les bans sont prioritaires : un utilisateur banni n'est pas ensuite
    restreint. ``limit`` reste accepté pour compatibilité mais n'est pas utilisé
    afin de ne jamais oublier les anciennes sanctions au-delà de 1000 lignes.
    """
    now = datetime.utcnow()
    async with SessionLocal() as db:
        rows = list((await db.execute(select(GlobalSanction).where(
            GlobalSanction.active.is_(True),
        ).order_by(GlobalSanction.id.desc()))).scalars().all())

        expired_ids = [row.id for row in rows if row.until_at and row.until_at <= now]
        if expired_ids:
            await db.execute(update(GlobalSanction).where(GlobalSanction.id.in_(expired_ids)).values(active=False))
            await db.commit()

    active_rows = [row for row in rows if row.id not in set(expired_ids)]
    bans: dict[int, GlobalSanction] = {}
    restricts: dict[int, GlobalSanction] = {}
    for row in active_rows:
        if row.sanction_type == 'ban':
            bans.setdefault(row.user_id, row)
        elif row.sanction_type == 'restrict':
            restricts.setdefault(row.user_id, row)

    sem = asyncio.Semaphore(5)

    async def apply_ban(row: GlobalSanction):
        if _protected(row.user_id):
            return
        async with sem:
            try:
                await bot.ban_chat_member(chat_id, row.user_id, request_timeout=10)
            except Exception as exc:
                logger.warning('reconcile ban failed user=%s chat=%s: %s', row.user_id, chat_id, exc)

    async def apply_restrict(row: GlobalSanction):
        if _protected(row.user_id) or row.user_id in bans:
            return
        async with sem:
            try:
                await bot.restrict_chat_member(
                    chat_id, row.user_id, permissions={'can_send_messages': False},
                    until_date=row.until_at or (now + timedelta(days=1)), request_timeout=10,
                )
            except Exception as exc:
                logger.warning('reconcile restrict failed user=%s chat=%s: %s', row.user_id, chat_id, exc)

    tasks = [apply_ban(row) for row in bans.values()] + [apply_restrict(row) for row in restricts.values()]
    if tasks:
        await asyncio.gather(*tasks)

