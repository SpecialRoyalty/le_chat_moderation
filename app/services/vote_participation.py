from __future__ import annotations

import asyncio
import time
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select, update

from app.config import get_settings
from app.db.models import SessionLog, Vote, VoteParticipation
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.sanctions import ban_many_global
from app.utils.time import day_key

# Le texte complet est affiché dans le message de vote. L'alerte Telegram est
# volontairement plus courte car answerCallbackQuery impose une petite limite.
VOTE_POLICY_TEXT = (
    '⚠️ VOTE = PARTICIPATION\n'
    'Ce groupe accepte uniquement les personnes qui apportent des médias.\n'
    '🚫 Un vote sans participation entraîne un ban. Regarder les médias uniquement sans jamais participer entraîne un ban.\n'
    'Si vous voulez profiter sans participer, allez dans un autre groupe !'
)

VOTE_CONFIRM_ALERT = (
    '⚠️ VOTE = PARTICIPATION\n'
    'Un vote sans participation entraîne un ban.\n'
    'Appuie une seconde fois sur le bouton pour confirmer ton engagement.'
)

_CONFIRM_TTL_SECONDS = 90.0
_CONFIRMATIONS: dict[tuple[int, int], float] = {}


def consume_or_arm_confirmation(chat_id: int, user_id: int) -> bool:
    """Retourne True au second clic effectué dans les 90 secondes."""
    now = time.monotonic()
    key = (int(chat_id), int(user_id))
    expiry = _CONFIRMATIONS.get(key, 0.0)
    if expiry >= now:
        _CONFIRMATIONS.pop(key, None)
        return True
    _CONFIRMATIONS[key] = now + _CONFIRM_TTL_SECONDS
    # Nettoyage opportuniste pour ne jamais laisser grossir ce cache.
    if len(_CONFIRMATIONS) > 5000:
        expired = [k for k, e in _CONFIRMATIONS.items() if e < now]
        for k in expired:
            _CONFIRMATIONS.pop(k, None)
    return False


async def has_current_vote(chat_id: int, user_id: int) -> bool:
    dk = day_key(get_settings().timezone)
    async with SessionLocal() as db:
        row = (await db.execute(select(Vote.id).where(
            Vote.chat_id == chat_id, Vote.user_id == user_id, Vote.day_key == dk,
        ).limit(1))).scalar_one_or_none()
        return row is not None


async def register_vote_commitment(chat_id: int, user_id: int) -> None:
    dk = day_key(get_settings().timezone)
    async with SessionLocal() as db:
        # add_vote() est protégé par une contrainte unique. Cette seconde garde
        # évite néanmoins un doublon d'engagement après un retry applicatif.
        existing = (await db.execute(select(VoteParticipation.id).where(
            VoteParticipation.chat_id == chat_id,
            VoteParticipation.user_id == user_id,
            VoteParticipation.day_key == dk,
            VoteParticipation.session_id.is_(None),
            VoteParticipation.resolved.is_(False),
        ).limit(1))).scalar_one_or_none()
        if existing is None:
            db.add(VoteParticipation(
                chat_id=chat_id, user_id=user_id, day_key=dk,
                outcome='waiting_open',
            ))
            await db.commit()


async def bind_current_votes_to_session(chat_id: int, session_id: int) -> int:
    """Lie les engagements du cycle de vote qui vient réellement d'ouvrir."""
    dk = day_key(get_settings().timezone)
    async with SessionLocal() as db:
        voter_ids = [int(x) for x in (await db.execute(select(Vote.user_id).where(
            Vote.chat_id == chat_id, Vote.day_key == dk,
        ))).scalars().all()]
        if not voter_ids:
            return 0
        result = await db.execute(update(VoteParticipation).where(
            VoteParticipation.chat_id == chat_id,
            VoteParticipation.day_key == dk,
            VoteParticipation.user_id.in_(voter_ids),
            VoteParticipation.session_id.is_(None),
            VoteParticipation.resolved.is_(False),
        ).values(session_id=session_id, outcome='session_open'))
        await db.commit()
        return int(result.rowcount or 0)


async def mark_participated(chat_id: int, session_id: int, user_id: int) -> bool:
    """Valide la participation uniquement après acceptation d'un média."""
    if not session_id:
        return False
    now = datetime.utcnow()
    async with SessionLocal() as db:
        result = await db.execute(update(VoteParticipation).where(
            VoteParticipation.chat_id == chat_id,
            VoteParticipation.session_id == session_id,
            VoteParticipation.user_id == user_id,
            VoteParticipation.resolved.is_(False),
            VoteParticipation.participated.is_(False),
        ).values(
            participated=True, participated_at=now, outcome='participated',
        ))
        await db.commit()
        return bool(result.rowcount)


async def cancel_commitments(chat_id: int, *, session_id: int | None = None, reason: str = 'cancelled') -> int:
    """Annule sans sanction les engagements d'un cycle/session invalide."""
    conditions = [
        VoteParticipation.chat_id == chat_id,
        VoteParticipation.resolved.is_(False),
    ]
    if session_id is None:
        conditions.append(VoteParticipation.session_id.is_(None))
    else:
        conditions.append(VoteParticipation.session_id == session_id)
    async with SessionLocal() as db:
        result = await db.execute(update(VoteParticipation).where(*conditions).values(
            resolved=True, resolved_at=datetime.utcnow(), outcome=reason[:40],
        ))
        await db.commit()
        return int(result.rowcount or 0)


async def resolve_session_commitments(bot: Bot, chat_id: int, session_id: int, close_kind: str) -> dict[str, int]:
    """Résout les votants à la fermeture : média valide ou ban réseau.

    Sécurité : une session de moins de 5 minutes ne provoque jamais de ban
    automatique. Un groupe perdu est annulé ailleurs via cancel_commitments().
    """
    stats = {'total': 0, 'participated': 0, 'banned': 0, 'exempt': 0, 'cancelled': 0}
    if not session_id:
        return stats

    if not await st.group_bool(chat_id, 'vote_participation_enabled', True):
        stats['cancelled'] = await cancel_commitments(chat_id, session_id=session_id, reason='cancelled_disabled')
        return stats

    async with SessionLocal() as db:
        session = await db.get(SessionLog, session_id)
        rows = list((await db.execute(select(VoteParticipation).where(
            VoteParticipation.chat_id == chat_id,
            VoteParticipation.session_id == session_id,
            VoteParticipation.resolved.is_(False),
        ).order_by(VoteParticipation.id.asc()))).scalars().all())

    stats['total'] = len(rows)
    if not rows:
        return stats

    now = datetime.utcnow()
    if not session or (now - session.opened_at).total_seconds() < 300:
        stats['cancelled'] = await cancel_commitments(chat_id, session_id=session_id, reason='cancelled_short')
        return stats

    protected = set(get_settings().all_admin_ids)
    participated_ids = [r.id for r in rows if r.participated]
    exempt_ids = [r.id for r in rows if (not r.participated and r.user_id in protected)]
    offenders = [r for r in rows if not r.participated and r.user_id not in protected]
    stats['participated'] = len(participated_ids)
    stats['exempt'] = len(exempt_ids)

    # La sanction globale est persistée en masse AVANT les appels Telegram.
    # Une panne d'un groupe ne permet donc pas d'échapper au ban réseau.
    punished_ids = [r.id for r in offenders]
    if offenders:
        await ban_many_global(
            bot, [r.user_id for r in offenders], source_chat_id=chat_id,
            reason='vote_without_participation', created_by=None,
        )
    stats['banned'] = len(punished_ids)

    async with SessionLocal() as db:
        resolved_at = datetime.utcnow()
        if participated_ids:
            await db.execute(update(VoteParticipation).where(
                VoteParticipation.id.in_(participated_ids)
            ).values(resolved=True, resolved_at=resolved_at, outcome='participated'))
        if exempt_ids:
            await db.execute(update(VoteParticipation).where(
                VoteParticipation.id.in_(exempt_ids)
            ).values(resolved=True, resolved_at=resolved_at, outcome='exempt_privileged'))
        if punished_ids:
            await db.execute(update(VoteParticipation).where(
                VoteParticipation.id.in_(punished_ids)
            ).values(resolved=True, resolved_at=resolved_at, outcome='banned_no_media'))
        await db.commit()
    return stats


async def session_commitment_stats(session_id: int) -> dict[str, int]:
    stats = {'total': 0, 'participated': 0, 'banned': 0, 'exempt': 0, 'cancelled': 0}
    if not session_id:
        return stats
    async with SessionLocal() as db:
        rows = list((await db.execute(select(VoteParticipation).where(
            VoteParticipation.session_id == session_id,
        ))).scalars().all())
    stats['total'] = len(rows)
    for row in rows:
        if row.participated:
            stats['participated'] += 1
        if row.outcome == 'banned_no_media':
            stats['banned'] += 1
        elif row.outcome == 'exempt_privileged':
            stats['exempt'] += 1
        elif row.outcome.startswith('cancelled'):
            stats['cancelled'] += 1
    return stats
