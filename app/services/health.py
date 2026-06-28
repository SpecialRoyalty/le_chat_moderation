from sqlalchemy import select, func
from aiogram import Bot
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import ErrorLog, TrackedMessage, User, MediaHash, Advertisement
from app.services import settings as st
from app.utils.time import mid_time, slot_times, next_open_text, next_status_update_text
from app.services.justice import candidate_count

async def health_text(bot:Bot):
    s=get_settings(); slot=await st.time_slot(); start,end=slot_times(slot,s.timezone)
    groups=[('Principal',s.main_group_id),('Logs',s.log_group_id)]
    group_lines=[]; missing=[]
    for name,gid in groups:
        if not gid:
            if name != 'Logs':
                missing.append(name)
            group_lines.append(f'{name}: non configuré')
            continue
        try:
            me=await bot.get_me(); member=await bot.get_chat_member(gid,me.id)
            group_lines.append(f'{name}: OK ({member.status})')
        except Exception:
            group_lines.append(f'{name}: ERREUR')
            missing.append(name)
    async with SessionLocal() as db:
        errors=(await db.execute(select(func.count(ErrorLog.id)))).scalar() or 0
        tracked=(await db.execute(select(func.count(TrackedMessage.id)).where(TrackedMessage.deleted==False))).scalar() or 0
        suspects=(await db.execute(select(func.count(User.id)).where(User.suspect_score>=50))).scalar() or 0
        media_known=(await db.execute(select(func.count(MediaHash.id)))).scalar() or 0
        ads_total=(await db.execute(select(func.count(Advertisement.id)))).scalar() or 0
        ads_active=(await db.execute(select(func.count(Advertisement.id)).where(Advertisement.active==True))).scalar() or 0
    mode='🟢 Fonctionnement total' if not missing else '🟡 Fonctionnement partiel sans modules manquants'
    ads_enabled='ON' if (await st.get_value('ads_enabled','true'))=='true' else 'OFF'
    repost_enabled='ON' if (await st.get_value('repost_enabled','false'))=='true' else 'OFF'
    return f'''{mode}

Bot: OK
PostgreSQL: OK
Scheduler: OK

Session:
Auto: {'ON' if await st.auto_enabled() else 'OFF'}
Ouvert: {'OUI' if await st.is_open() else 'NON'}
Créneau: {slot}
Prochaine ouverture: {next_open_text(slot,s.timezone)}
Prochaine mise à jour statut: {next_status_update_text(slot,s.timezone)}
Dernière mise à jour statut: {await st.get_value('last_status_update_at','jamais')}
Prochaine justice: {mid_time(slot,s.timezone).strftime('%H:%M')}
Limite justice: {await st.justice_limit()} / session
Justifiables actuels: {await candidate_count()}
Prochaine fermeture: {end.strftime('%H:%M')}

Groupes:
{chr(10).join(group_lines)}

Contrôles:
Messages suivis non supprimés: {tracked}
Comptes suspects: {suspects}
Médias connus: {media_known}
Anti-repost: {repost_enabled}
Dernier repost bloqué: {await st.get_value('last_repost_blocked_at','jamais')}
Publicités automatiques: {ads_enabled}
Publicités configurées: {ads_active} actives / {ads_total} total
Erreurs loggées: {errors}

Diffusions planifiées:
Publicité — dernier envoi: {await st.get_value('last_ad_sent_at','jamais')} — prochain: automatique pendant ouverture si ON
Règles — dernier envoi: {await st.get_value('last_rules_sent_at','jamais')} — prochain: toutes les 30 min si ouvert
Top inviteurs — dernier envoi: {await st.get_value('last_top_sent_at','jamais')}
'''
