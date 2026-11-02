"""Campagnes de déban réseau. Sécurité: aperçu persistant, double validation et journal.

Ne modifie jamais les empreintes /pedo ni les filtres de modération. Une
réhabilitation porte uniquement sur les sanctions globales explicitement sûres.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, select, update, or_

from app.config import get_settings
from app.db.models import (
    AmnestyAttempt, AmnestyCampaign, AmnestyCandidate,
    GlobalSanction, GlobalMediaRegistry, MediaBanJob, MediaFingerprint, MediaHash, NetworkEvent, NetworkGroup,
    SanctionHistory, TrustedAction, User,
)
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Ces motifs correspondent à des automatismes révisables ; les bans sensibles
# et les motifs inconnus ne sont JAMAIS inclus automatiquement.
REVIEWABLE_REASONS = frozenset({'vote_without_participation', 'story', 'media_under_60s'})
PROTECTED_REASONS = frozenset({
    'pedo', 'hashban', 'manual_admin_ban', 'legacy_migration',
    'ban_word', 'nameban_join', 'link', 'moderation',
})
BLOCKED_STATUSES = frozenset({'removed', 'pending'})
MAX_USERS = 4000
MAX_ATTEMPTS = 40000
PREVIEW_TTL = timedelta(minutes=15)


def classify_user(user_id: int, reasons: set[str], historical: set[str],
                  banned_media_users: set[int], protected: set[int]) -> tuple[bool, str]:
    """Une raison ambiguë, sensible ou des empreintes bannies = exclusion.

    Les motifs anciens écrasés AVANT cette version ne sont pas récupérables :
    ce risque est signalé clairement dans le panneau et dans MIGRATION.md.
    """
    if user_id in protected:
        return False, 'admin/trusted'
    if user_id in banned_media_users:
        return False, 'media_blacklist'
    combined = reasons | historical
    if not combined or any(r not in REVIEWABLE_REASONS for r in combined):
        return False, 'sensible_ou_inconnu'
    return True, 'eligible'


async def _classify(db):
    bans = (await db.execute(select(GlobalSanction).where(
        GlobalSanction.active.is_(True), GlobalSanction.sanction_type == 'ban'
    ))).scalars().all()
    by_user: dict[int, set[str]] = defaultdict(set)
    for row in bans:
        by_user[row.user_id].add(row.reason or '')
    # On vérifie aussi les sanctions HISTORIQUES actives/inactives depuis ce correctif
    # pour ne pas réhabiliter un /pedo ensuite écrasé par un autre ban automatique.
    history: dict[int, set[str]] = defaultdict(set)
    banned_media_users = set()
    uids = list(by_user)
    # Grouper les recherches : les gros réseaux peuvent dépasser les limites
    # de paramètres PostgreSQL si l'on construit un seul IN sur tous les IDs.
    for start in range(0, len(uids), 800):
        chunk = uids[start:start + 800]
        rows = (await db.execute(select(SanctionHistory.user_id, SanctionHistory.reason).where(
            SanctionHistory.user_id.in_(chunk), SanctionHistory.sanction_type == 'ban'
        ))).all()
        for uid, reason in rows:
            history[uid].add(reason or '')
        banned_media_users.update((await db.execute(select(MediaHash.user_id).distinct().where(
            MediaHash.user_id.in_(chunk), MediaHash.banned.is_(True)
        ))).scalars().all())
        banned_media_users.update((await db.execute(select(MediaFingerprint.user_id).distinct().where(
            MediaFingerprint.user_id.in_(chunk), MediaFingerprint.banned.is_(True)
        ))).scalars().all())
        banned_media_users.update((await db.execute(select(GlobalMediaRegistry.first_user_id).distinct().where(
            GlobalMediaRegistry.first_user_id.in_(chunk), GlobalMediaRegistry.banned.is_(True)
        ))).scalars().all())
        banned_media_users.update((await db.execute(select(MediaBanJob.user_id).distinct().where(
            MediaBanJob.user_id.in_(chunk)
        ))).scalars().all())
        # L'ancien bot conserve souvent une trace trusted même si le motif
        # global /pedo a ensuite été remplacé par un autre ban automatique.
        pedo_targets = (await db.execute(select(TrustedAction.target_user_id).distinct().where(
            TrustedAction.target_user_id.in_(chunk), TrustedAction.command == '/pedo'
        ))).scalars().all()
        banned_media_users.update(pedo_targets)
    protected = set(get_settings().all_admin_ids)
    eligible = {}
    excluded = Counter()
    for uid, reasons in by_user.items():
        okay, why = classify_user(uid, reasons, history.get(uid, set()), banned_media_users, protected)
        if okay:
            eligible[uid] = next(iter(sorted(reasons)))
        else:
            excluded[why] += 1
    # Utilisateurs marqués bannis historiquement mais sans entrée global_sanctions.
    legacy_banned_ids = (await db.execute(select(User.id).where(User.is_banned.is_(True)))).scalars().all()
    unknown = sum(1 for uid in legacy_banned_ids if uid not in by_user)
    return eligible, excluded, unknown


async def _groups(db) -> list[NetworkGroup]:
    # Même les groupes OFF sont inclus si le bot peut encore les gérer.
    return (await db.execute(select(NetworkGroup).where(
        NetworkGroup.approved.is_(True),
        NetworkGroup.status.not_in(BLOCKED_STATUSES),
    ).order_by(NetworkGroup.priority.asc(), NetworkGroup.chat_id.asc()))).scalars().all()


async def build_preview(admin_id: int) -> AmnestyCampaign:
    if admin_id not in get_settings().admin_id_set:
        raise PermissionError('ADMIN_IDS uniquement')
    async with SessionLocal() as db:
        eligible, excluded, unknown = await _classify(db)
        groups = await _groups(db)
        if not groups:
            raise ValueError('Aucun groupe approuvé dans le réseau : aucune campagne de déban possible.')
        if len(eligible) > MAX_USERS or len(eligible) * len(groups) > MAX_ATTEMPTS:
            raise ValueError('Campagne trop volumineuse : utiliser une procédure de migration par lots, sans déban partiel silencieux.')
        # Seule une campagne encore en cours par instance du réseau.
        active = (await db.execute(select(AmnestyCampaign.id).where(
            AmnestyCampaign.status == 'running'
        ).limit(1))).scalar_one_or_none()
        if active:
            raise ValueError(f'Campagne #{active} en cours / à reprendre. Terminez-la avant une nouvelle simulation.')
        report = {
            'excluded': dict(excluded),
            'unknown_legacy_without_sanction': unknown,
            'group_ids': [g.chat_id for g in groups],
            'group_names': [g.title for g in groups],
            'temporarily_unavailable_groups': sum(g.status in {'lost', 'offline'} for g in groups),
            'excluded_group_statuses': ['lost', 'offline', 'removed', 'pending'],
            'eligible_reasons': dict(Counter(eligible.values())),
            'sample_user_ids': list(sorted(eligible))[:12],
        }
        now = datetime.utcnow()
        campaign = AmnestyCampaign(
            created_by=admin_id, status='preview', created_at=now,
            expires_at=now + PREVIEW_TTL, eligible_count=len(eligible),
            excluded_count=sum(excluded.values()), unknown_count=unknown,
            targeted_groups=len(groups), report=json.dumps(report, ensure_ascii=False),
        )
        db.add(campaign)
        await db.flush()
        for user_id, reason in eligible.items():
            db.add(AmnestyCandidate(campaign_id=campaign.id, user_id=user_id, reason=reason))
        db.add(NetworkEvent(action='amnesty_preview', admin_id=admin_id,
                            details=f'campaign={campaign.id} eligible={len(eligible)} excluded={sum(excluded.values())}'))
        await db.commit()
        return campaign


def preview_text(campaign: AmnestyCampaign) -> str:
    details = json.loads(campaign.report or '{}')
    reasons = details.get('eligible_reasons', {})
    exclusions = details.get('excluded', {})
    return (
        f'🕊️ CAMPAGNE DE DÉBAN — SIMULATION #{campaign.id}\n\n'
        f'✅ Candidats révisables : {campaign.eligible_count}\n'
        f'🛡️ Exclus par sécurité : {campaign.excluded_count}\n'
        f'❓ Bans historiques sans motif connu : {campaign.unknown_count}\n'
        f'🏠 Groupes enregistrés ciblés : {campaign.targeted_groups}\n'
        f'⏳ Dont indisponibles (reprises différées) : {details.get("temporarily_unavailable_groups", 0)}\n'
        f'📨 Opérations Telegram au maximum : {campaign.eligible_count * campaign.targeted_groups}\n\n'
        f'Motifs admissibles : {", ".join(f"{k} ({v})" for k,v in reasons.items()) or "aucun"}\n'
        f'Exclusions : {", ".join(f"{k} ({v})" for k,v in exclusions.items()) or "aucune"}\n'
        f'Échantillon IDs : {", ".join(map(str, details.get("sample_user_ids", []))) or "aucun"}\n\n'
        '⛔ /pedo, hash-ban, bans manuels, mots/nom interdits et motifs inconnus EXCLUS.\n'
        '⚠️ Les motifs anciens écrasés avant cette mise à jour ne sont pas toujours reconstructibles.\n'
        'La simulation ne débannit personne. Confirmation réservée aux ADMIN_IDS.\n'
        'Après confirmation, suppression des bans globaux retenus, puis déban Telegram journalisé. '
        'Un groupe inaccessible sera signalé et retenté à son retour.\n'
        '⏱️ Confirmation valable 15 minutes.'
    )


async def confirm_campaign(campaign_id: int, admin_id: int) -> tuple[int, int]:
    if admin_id not in get_settings().admin_id_set:
        raise PermissionError('ADMIN_IDS uniquement')
    async with SessionLocal() as db:
        # PostgreSQL verrouille cette campagne : deux confirmations ne peuvent
        # pas exécuter deux fois la transition.
        campaign = (await db.execute(select(AmnestyCampaign).where(
            AmnestyCampaign.id == campaign_id
        ).with_for_update())).scalar_one_or_none()
        if not campaign or campaign.created_by != admin_id:
            raise ValueError('Campagne inconnue ou appartenant à un autre admin')
        if campaign.status != 'preview' or campaign.expires_at <= datetime.utcnow():
            raise ValueError('Confirmation expirée ou campagne déjà exécutée')
        candidates = (await db.execute(select(AmnestyCandidate).where(
            AmnestyCandidate.campaign_id == campaign_id
        ))).scalars().all()
        current, _excluded, _unknown = await _classify(db)
        groups = await _groups(db)
        # La liste des destinations est figée au moment de la confirmation.
        # Ne jamais réhabiliter un motif devenu sensible entre aperçu et OK.
        released_ids = []
        for candidate in candidates:
            if candidate.user_id not in current:
                candidate.status = 'skipped_changed'
                continue
            candidate.status = 'released'
            candidate.released_at = datetime.utcnow()
            released_ids.append(candidate.user_id)
            db.add(NetworkEvent(action='amnesty_release_global', user_id=candidate.user_id,
                                admin_id=admin_id,
                                details=f'campaign={campaign_id} prior_reason={candidate.reason}'))
            for group in groups:
                db.add(AmnestyAttempt(
                    campaign_id=campaign_id, user_id=candidate.user_id,
                    chat_id=group.chat_id, status='pending',
                ))
        if released_ids:
            # Ici les sanctions centrales sont levées AVANT les appels Telegram :
            # cela empêche leur réapplication automatique pendant la campagne.
            await db.execute(update(GlobalSanction).where(
                GlobalSanction.user_id.in_(released_ids),
                GlobalSanction.sanction_type == 'ban', GlobalSanction.active.is_(True),
            ).values(active=False))
            await db.execute(update(User).where(User.id.in_(released_ids)).values(is_banned=False))
        campaign.status = 'running' if (released_ids and groups) else 'completed'
        campaign.confirmed_at = datetime.utcnow()
        if campaign.status == 'completed':
            campaign.finished_at = datetime.utcnow()
        db.add(NetworkEvent(action='amnesty_confirm', admin_id=admin_id,
                            details=f'campaign={campaign_id} released={len(released_ids)} groups={len(groups)}'))
        await db.commit()
        return len(released_ids), len(released_ids) * len(groups)


async def cancel_preview(campaign_id: int, admin_id: int) -> bool:
    if admin_id not in get_settings().admin_id_set:
        return False
    async with SessionLocal() as db:
        result = await db.execute(update(AmnestyCampaign).where(
            AmnestyCampaign.id == campaign_id, AmnestyCampaign.created_by == admin_id,
            AmnestyCampaign.status == 'preview'
        ).values(status='cancelled', finished_at=datetime.utcnow()))
        db.add(NetworkEvent(action='amnesty_cancel', admin_id=admin_id,
                            details=f'campaign={campaign_id}'))
        await db.commit()
        return bool(result.rowcount)


async def campaign_progress(campaign_id: int) -> str:
    async with SessionLocal() as db:
        campaign = await db.get(AmnestyCampaign, campaign_id)
        if not campaign:
            return 'Campagne inconnue.'
        outcomes = (await db.execute(select(AmnestyAttempt.status, func.count()).where(
            AmnestyAttempt.campaign_id == campaign_id
        ).group_by(AmnestyAttempt.status))).all()
        skipped = (await db.execute(select(func.count()).select_from(AmnestyCandidate).where(
            AmnestyCandidate.campaign_id == campaign_id,
            AmnestyCandidate.status == 'skipped_changed'
        ))).scalar_one()
    counts = dict(outcomes)
    return (
        f'🕊️ CAMPAGNE #{campaign_id} — {campaign.status.upper()}\n'
        f'👥 Candidats de la simulation : {campaign.eligible_count}\n'
        f'🔐 Réexclus au moment de confirmer : {skipped}\n'
        f'✅ Débans Telegram réussis : {counts.get("success", 0)}\n'
        f'⏳ En attente / à retenter : {counts.get("pending", 0) + counts.get("working", 0)}\n'
        f'⚠️ Échecs définitifs : {counts.get("failed_final", 0)}\n'
        f'🚫 Groupes retirés : {counts.get("skipped_removed", 0)}\n'
        f'🛡️ Nouveaux bans respectés : {counts.get("skipped_new_ban", 0)}\n'
        'Les échecs restent enregistrés. Les groupes indisponibles sont retentés automatiquement.'
    )


async def process_amnesty_jobs(bot: Bot, batch: int = 30) -> dict[str, int]:
    """Worker idempotent : réservation SQL, retry, aucun appel Telegram sous transaction."""
    now = datetime.utcnow()
    async with SessionLocal() as db:
        # Récupération d'un worker interrompu ou d'un conteneur Railway redémarré.
        await db.execute(update(AmnestyAttempt).where(
            AmnestyAttempt.status == 'working', AmnestyAttempt.next_retry_at <= now
        ).values(status='pending'))
        rows = (await db.execute(select(AmnestyAttempt).where(
            AmnestyAttempt.status == 'pending',
            or_(AmnestyAttempt.next_retry_at.is_(None), AmnestyAttempt.next_retry_at <= now),
        ).order_by(AmnestyAttempt.id).limit(batch).with_for_update(skip_locked=True))).scalars().all()
        claimed = [(r.id, r.user_id, r.chat_id, r.campaign_id, r.attempts) for r in rows]
        for row in rows:
            row.status = 'working'
            row.next_retry_at = now + timedelta(minutes=3)
        await db.commit()

    semaphore = asyncio.Semaphore(4)

    async def one(task):
        attempt_id, user_id, chat_id, campaign_id, prior_attempts = task
        status = 'success'
        error = ''
        async with semaphore:
            try:
                # Garde-fou: une sanction de sécurité nouvelle a priorité sur le déban.
                async with SessionLocal() as db:
                    now_groups = await db.get(NetworkGroup, chat_id)
                    active_ban = (await db.execute(select(GlobalSanction.id).where(
                        GlobalSanction.user_id == user_id,
                        GlobalSanction.sanction_type == 'ban',
                        GlobalSanction.active.is_(True),
                    ).limit(1))).scalar_one_or_none()
                if active_ban:
                    status, error = 'skipped_new_ban', 'Nouvelle sanction globale active'
                elif not now_groups or not now_groups.approved or now_groups.status == 'removed':
                    status = 'skipped_removed'
                else:
                    # Un groupe hors ligne est retenté plus tard sans vider la DB.
                    if now_groups.status in {'offline', 'lost', 'pending'}:
                        raise RuntimeError('Groupe indisponible / hors ligne')
                    acknowledged = await bot.unban_chat_member(chat_id, user_id, only_if_banned=True, request_timeout=12)
                    if acknowledged is False:
                        raise RuntimeError('Telegram a refusé de confirmer le déban')
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'[:800]
                status = 'failed_final' if prior_attempts >= 5 else 'pending'
                logger.warning('amnesty user=%s group=%s: %s', user_id, chat_id, error)
        async with SessionLocal() as db:
            row = await db.get(AmnestyAttempt, attempt_id)
            if row and row.status == 'working':
                row.status = status
                row.attempts = prior_attempts + 1
                row.last_error = error
                row.next_retry_at = (
                    datetime.utcnow() + timedelta(minutes=min(60, 2 ** min(prior_attempts + 1, 6)))
                    if status == 'pending' else None
                )
                if status != 'pending':
                    row.finished_at = datetime.utcnow()
                db.add(NetworkEvent(
                    action='amnesty_unban_attempt', chat_id=chat_id, user_id=user_id,
                    details=f'campaign={campaign_id} status={status} attempt={row.attempts} {error}'[:1000]
                ))
                await db.commit()
        return status

    result = await asyncio.gather(*(one(task) for task in claimed)) if claimed else []
    if claimed:
        campaign_ids = {item[3] for item in claimed}
        async with SessionLocal() as db:
            for campaign_id in campaign_ids:
                remain = (await db.execute(select(func.count()).select_from(AmnestyAttempt).where(
                    AmnestyAttempt.campaign_id == campaign_id,
                    AmnestyAttempt.status.in_(['pending', 'working'])
                ))).scalar_one()
                if remain == 0:
                    fails = (await db.execute(select(func.count()).select_from(AmnestyAttempt).where(
                        AmnestyAttempt.campaign_id == campaign_id,
                        AmnestyAttempt.status == 'failed_final'
                    ))).scalar_one()
                    await db.execute(update(AmnestyCampaign).where(
                        AmnestyCampaign.id == campaign_id,
                        AmnestyCampaign.status.in_(['running', 'partial']),
                    ).values(status='partial' if fails else 'completed', finished_at=datetime.utcnow()))
            await db.commit()
    return dict(Counter(result))


async def retry_failed_campaign(campaign_id: int, admin_id: int) -> int:
    if admin_id not in get_settings().admin_id_set:
        raise PermissionError('ADMIN_IDS uniquement')
    async with SessionLocal() as db:
        campaign = await db.get(AmnestyCampaign, campaign_id)
        if not campaign or campaign.status not in {'partial', 'running'}:
            return 0
        result = await db.execute(update(AmnestyAttempt).where(
            AmnestyAttempt.campaign_id == campaign_id,
            AmnestyAttempt.status == 'failed_final'
        ).values(status='pending', attempts=0, next_retry_at=None, last_error=''))
        if result.rowcount:
            campaign.status = 'running'
            campaign.finished_at = None
            db.add(NetworkEvent(action='amnesty_manual_retry', admin_id=admin_id,
                                details=f'campaign={campaign_id} attempts={result.rowcount}'))
        await db.commit()
        return result.rowcount


async def campaign_log_text(campaign_id: int) -> str:
    async with SessionLocal() as db:
        campaign = await db.get(AmnestyCampaign, campaign_id)
        if campaign is None:
            return 'Campagne inconnue.'
        groups = (await db.execute(select(AmnestyAttempt.chat_id, AmnestyAttempt.status, func.count()).where(
            AmnestyAttempt.campaign_id == campaign_id,
        ).group_by(AmnestyAttempt.chat_id, AmnestyAttempt.status))).all()
        failures = (await db.execute(select(AmnestyAttempt).where(
            AmnestyAttempt.campaign_id == campaign_id,
            AmnestyAttempt.last_error != '',
        ).order_by(AmnestyAttempt.id.desc()).limit(8))).scalars().all()
    by_group: dict[int, Counter] = defaultdict(Counter)
    for gid, status, num in groups:
        by_group[gid][status] = num
    lines = [f'📑 JOURNAL CAMPAGNE #{campaign_id}', f'État : {campaign.status}', '']
    for gid, counts in by_group.items():
        lines.append(f'Groupe {gid} : ✅ {counts["success"]} | ⏳ {counts["pending"] + counts["working"]} | ❌ {counts["failed_final"]}')
    if failures:
        lines.append('\nDernières erreurs :')
        for item in failures:
            lines.append(f'{item.user_id} / {item.chat_id} : {item.last_error[:140]}')
    lines.append('\nLe journal complet (admin, utilisateur, groupe, tentative, erreur) est dans network_events_test et amnesty_attempts_test.')
    return '\n'.join(lines)[:3900]
