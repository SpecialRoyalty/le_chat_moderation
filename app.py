"""Telegram Group Guard: deploy as a single Railway worker, PostgreSQL-backed."""
import asyncio
import io
import hashlib
import logging
import os
import re
import uuid
import time
import secrets
from multigroup import (parse_group_ids, schema_for, select_group, current_group as _current_group,
                        private_group_selection, vip_payload, parse_vip_payload)
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import load_dotenv
from telegram import (ChatPermissions, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup,
                      Update)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, ChatJoinRequestHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)
from onboarding import admin_status, bot_permission_gaps, can_request_activation, activation_text
from logic import automatic_open, countdown_due, countdown_label, next_boundary, safe_name
from commerce_ui import (vip_catalog_view, vip_confirm_view, payment_methods_view, money, payment_total,
                         invite_dashboard_view, leaderboard_view)
from html import escape
from admin_ui import PRESET_SLOTS, POST_INTERVALS, parse_slot, parse_euros, parse_user_id, short, bool_label
from decimal import Decimal, InvalidOperation
from vip_rules import goal_progress, raffle_eligible, is_claimable
from media_guard import from_message
from media_pipeline import (id_is_banned, id_previously_seen, mark_message,
                            background_scan, ban_known_media, ban_from_message)

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('guard')
TOKEN=os.environ['BOT_TOKEN']
# Runtime registry: groups are detected from Telegram and persisted in PostgreSQL.
# Legacy GROUP_IDS/GROUP_ID are optional bootstrap values for a V7 migration.
LEGACY_GROUP_IDS=parse_group_ids(os.getenv('GROUP_IDS') or os.getenv('GROUP_ID',''))
GROUPS=list(LEGACY_GROUP_IDS)
DEFAULT_GROUP=GROUPS[0] if GROUPS else None

def current_group():
    return _current_group(DEFAULT_GROUP or (GROUPS[0] if GROUPS else None))

def bind_group(update, ctx):
    chat=update.effective_chat
    gid=chat.id if chat and chat.type in ('group','supergroup') else (private_group_selection(ctx.user_data,GROUPS) if GROUPS else None)
    if gid in GROUPS:
        select_group(gid,GROUPS)
    return gid in GROUPS

OWNERS={int(v.strip()) for v in os.environ['OWNER_IDS'].split(',') if v.strip()}
TRUSTED_USER_IDS={int(v.strip()) for v in os.getenv('TRUSTED_USER_IDS','').split(',') if v.strip()}
PAYMENT_DETAILS={
    'crypto':os.getenv('PAYMENT_CRYPTO_DETAILS','').strip(),
    'paypal':os.getenv('PAYMENT_PAYPAL_DETAILS','').strip(),
    'revolut':os.getenv('PAYMENT_REVOLUT_DETAILS','').strip()
}
TZ=os.getenv('TZ','Europe/Paris')
DB=os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')
OPEN_PERMS=ChatPermissions(can_send_messages=True, can_send_photos=True,can_send_videos=True,
                            can_send_audios=False,can_send_documents=False,can_send_video_notes=False,
                            can_send_voice_notes=False,can_send_polls=False,can_send_other_messages=False,
                            can_add_web_page_previews=False)
CLOSED_PERMS=ChatPermissions(can_send_messages=False,can_send_photos=False,can_send_videos=False)
DEFAULTS={'auto_enabled':'true','manual_until':'','manual_closed_until':'','open_state':'false',
          'countdown_id':'','countdown_key':'','last_transition':'','rules':'Respectez les membres. Aucun lien ni story. Photos, vidéos et textes seulement.',
          'rules_id':'','last_rules':'','allow_forwards':'false','ban_nonlatin':'true','last_admin_alert':'',
          'net_entries':'0','net_exits':'0','vip_ad_enabled':'false',
          'group_activation':'inactive','group_activation_nonce':'','group_activation_message_id':'',
          'group_activation_by':'','group_activation_confirmed_by':'','anti_repost_enabled':'false',
          'anti_fast_join_enabled':'false','anti_fast_join_minutes':'5','last_media_retention':'',
          'raffle_min_credits':'1'}

def utcnow():return datetime.now(timezone.utc)

def is_owner(uid):return uid in OWNERS

async def cfg(db,key,default=''):
    return await db.fetchval('SELECT value FROM config WHERE key=$1',key) or default

async def setcfg(db,key,value):
    await db.execute('INSERT INTO config(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',key,str(value))

async def flag(db,key):return (await cfg(db,key)).lower()=='true'

async def trusted(db,uid):
    return is_owner(uid) or uid in TRUSTED_USER_IDS or bool(await db.fetchval('SELECT 1 FROM trusted WHERE user_id=$1',uid))

# Group setup is explicit: adding the bot to Telegram does not activate it.
# Registration is dynamic, and each group must be confirmed by a real Telegram admin.
async def group_active(db):
    return await cfg(db,'group_activation')=='active'

async def telegram_group_admin(bot,chat_id,user):
    if not user or user.is_bot:return False
    try:
        member=await bot.get_chat_member(chat_id,user.id)
    except TelegramError:
        log.exception('Cannot verify Telegram group administrator: %s',chat_id)
        return False
    return can_request_activation(member,user)

async def group_rights(bot,chat_id):
    try:
        me=await bot.get_chat_member(chat_id,bot.id)
        return bot_permission_gaps(me)
    except TelegramError:
        log.exception('Unable to read bot group rights')
        return ['Vérifier les droits du bot (appel Telegram impossible)']

async def deactivate_group(db):
    await setcfg(db,'group_activation','inactive')
    await setcfg(db,'group_activation_nonce','')
    await setcfg(db,'group_activation_message_id','')
    # A fresh confirmation must not reinstate an earlier manual override.
    await setcfg(db,'manual_until','')
    await setcfg(db,'manual_closed_until','')
    await setcfg(db,'open_state','false')
    await setcfg(db,'countdown_id','')
    await setcfg(db,'countdown_key','')
    await setcfg(db,'rules_id','')
    await setcfg(db,'last_rules','')

async def setup_prompt(bot,db,chat,actor):
    """Stage an explicit, replay-resistant confirmation. No moderation yet."""
    nonce=uuid.uuid4().hex[:16]
    await setcfg(db,'group_activation','pending')
    await setcfg(db,'group_activation_nonce',nonce)
    await setcfg(db,'group_activation_by',actor.id)
    gaps=await group_rights(bot,chat.id)
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Confirmer l’activation',callback_data=f'onb:yes:{nonce}')],
        [InlineKeyboardButton('🔄 Revérifier les droits',callback_data=f'onb:check:{nonce}')],
        [InlineKeyboardButton('❌ Annuler et retirer le bot',callback_data=f'onb:no:{nonce}')]
    ])
    text=activation_text(chat.title or 'Supergroupe',gaps)
    old=await cfg(db,'group_activation_message_id')
    if old:
        try:
            await bot.edit_message_text(chat_id=chat.id,message_id=int(old),text=text,reply_markup=kb)
            return True
        except (TelegramError, ValueError):pass
    try:
        m=await bot.send_message(chat.id,text,reply_markup=kb)
        await setcfg(db,'group_activation_message_id',m.message_id)
        return True
    except TelegramError:
        log.exception('Cannot send group activation request: %s',chat.id)
        await notify_owner(bot,'⚠️ Ajout au groupe détecté mais le bot ne peut pas afficher la confirmation. '
                               'Vérifie ses permissions et utilise /setup dans le groupe.')
        return False

async def is_registered_vip_destination(ctx, chat_id):
    # A VIP destination must not be managed as a regular scheduled group.
    for pool in tuple(ctx.application.bot_data['pools'].values()):
        if await pool.fetchval('SELECT 1 FROM vip_registered_chats WHERE chat_id=$1',chat_id):
            return True
    return False


async def register_group(application, chat_id, title):
    """Persist a discovered group and initialize its separate database schema.

    The first group keeps public (V6/V7 backward compatibility). Only callers
    that have verified Telegram administrator status may call this function.
    """
    global DEFAULT_GROUP
    if not isinstance(chat_id,int) or chat_id >= 0:
        raise ValueError('Identifiant de groupe invalide')
    async with application.bot_data['registration_lock']:
        registry=application.bot_data['registry_pool']
        async with registry.acquire() as conn:
            async with conn.transaction():
                first=await conn.fetchval('''SELECT first_group_id FROM public.guard_registry
                    WHERE registry_key='primary_group' FOR UPDATE''')
                if first is None:
                    await conn.execute('''INSERT INTO public.guard_registry(registry_key,first_group_id)
                          VALUES('primary_group',$1) ON CONFLICT DO NOTHING''',chat_id)
                    first=await conn.fetchval('''SELECT first_group_id FROM public.guard_registry
                        WHERE registry_key='primary_group' ''')
                first=int(first)
                schema=schema_for(chat_id,first)
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
                await conn.execute('''INSERT INTO public.guard_groups(chat_id,title)
                       VALUES($1,$2) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title''',
                       chat_id,(title or str(chat_id))[:255])
        if chat_id not in application.bot_data['pools']:
            pool=await asyncpg.create_pool(DB,min_size=1,max_size=3,
                             server_settings={'search_path':schema})
            try:
                async with pool.acquire() as conn:
                    await conn.execute(application.bot_data['schema_sql'])
                    for key,value in DEFAULTS.items():
                        await conn.execute('''INSERT INTO config(key,value) VALUES($1,$2)
                                              ON CONFLICT DO NOTHING''',key,value)
            except Exception:
                await pool.close()
                raise
            application.bot_data['pools'][chat_id]=pool
        if chat_id not in GROUPS:
            GROUPS.append(chat_id)
        DEFAULT_GROUP=first
        return application.bot_data['pools'][chat_id]


async def group_setup_command(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    msg=update.effective_message
    chat=update.effective_chat
    actor=update.effective_user
    if not chat or chat.type not in ('group','supergroup') or not msg:return
    if not await telegram_group_admin(ctx.bot,chat.id,actor):
        await quiet_delete(ctx.bot,chat.id,msg.message_id)
        return
    if chat.type!='supergroup':
        await msg.reply_text('Convertis le groupe en supergroupe Telegram, puis relance /setup.')
        return
    if chat.id not in GROUPS and await is_registered_vip_destination(ctx,chat.id):
        await msg.reply_text('💎 Destination VIP reconnue : les horaires normaux ne s’appliquent pas ici.')
        return
    db=await register_group(ctx.application,chat.id,chat.title or str(chat.id))
    bind_group(update,ctx)
    await quiet_delete(ctx.bot,chat.id,msg.message_id)
    if await group_active(db):
        gaps=await group_rights(ctx.bot,chat.id)
        if not gaps:
            await ctx.bot.send_message(chat.id,'✅ Bot déjà actif et droits valides.')
            return
        await deactivate_group(db)
    await setup_prompt(ctx.bot,db,chat,actor)


async def bot_membership_update(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    """Auto-detect group creation and request admin confirmation."""
    event=update.my_chat_member
    if not event or event.chat.type not in ('group','supergroup'):return
    chat=event.chat
    new=event.new_chat_member
    if new.status in ('left','kicked'):
        if chat.id in GROUPS:
            bind_group(update,ctx)
            await deactivate_group(await db_conn(ctx))
            await notify_owner(ctx.bot,f'⚠️ Le bot a été retiré du groupe {chat.title} ({chat.id}).')
        return
    if chat.id not in GROUPS and await is_registered_vip_destination(ctx,chat.id):
        log.info('VIP destination %s: no regular group setup',chat.id)
        return
    actor=event.from_user
    if chat.id not in GROUPS:
        if not await telegram_group_admin(ctx.bot,chat.id,actor):
            try:await ctx.bot.send_message(chat.id,'🚫 Installation non autorisée : administrateur requis.')
            except TelegramError:pass
            try:await ctx.bot.leave_chat(chat.id)
            except TelegramError:log.exception('Cannot leave unauthorized group %s',chat.id)
            return
        if chat.type!='supergroup':
            try:await ctx.bot.send_message(chat.id,'⚠️ Convertis ce groupe en supergroupe pour activer le bot.')
            except TelegramError:pass
            return
        try:
            await register_group(ctx.application,chat.id,chat.title or str(chat.id))
        except Exception:
            log.exception('Could not register detected group %s',chat.id)
            await notify_owner(ctx.bot,f'⚠️ Enregistrement PostgreSQL impossible : {chat.id}')
            return
    bind_group(update,ctx)
    db=await db_conn(ctx)
    if await group_active(db):
        missing=await group_rights(ctx.bot,chat.id)
        if not missing:return
        await deactivate_group(db)
        await notify_owner(ctx.bot,f'⚠️ Droits insuffisants sur {chat.title} ({chat.id}) : '+', '.join(missing))
        try:await ctx.bot.send_message(chat.id,'⚠️ Droits manquants : modération suspendue. '
                                                'Un administrateur doit relancer /setup.')
        except TelegramError:pass
        return
    if not await telegram_group_admin(ctx.bot,chat.id,actor):
        return
    if chat.type=='supergroup':
        await setup_prompt(ctx.bot,db,chat,actor)


async def activation_callback(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    q=update.callback_query
    if not q:return
    chat=q.message.chat if q.message else None
    if not chat or chat.id!=current_group():
        await q.answer('Groupe non autorisé.',show_alert=True)
        return
    if not await telegram_group_admin(ctx.bot,chat.id,q.from_user):
        await q.answer('Réservé aux administrateurs de ce groupe.',show_alert=True)
        return
    db=await db_conn(ctx)
    tokens=q.data.split(':')
    if len(tokens)!=3 or tokens[2]!=await cfg(db,'group_activation_nonce') or not tokens[2] or await cfg(db,'group_activation')!='pending':
        await q.answer('Confirmation expirée. Utilise /setup.',show_alert=True)
        return
    action=tokens[1]
    if action=='no':
        await q.answer('Activation annulée.')
        await deactivate_group(db)
        try:await q.edit_message_text('❌ Activation refusée. Le bot quitte ce groupe.')
        except TelegramError:pass
        try:await ctx.bot.leave_chat(current_group())
        except TelegramError:log.exception('Cannot leave after activation cancellation')
        return
    missing=await group_rights(ctx.bot,current_group())
    if missing:
        await q.answer('Droits manquants. Donne-les au bot puis revérifie.',show_alert=True)
        try:await q.edit_message_text(activation_text(chat.title or 'Supergroupe',missing),reply_markup=q.message.reply_markup)
        except TelegramError:pass
        return
    if action=='check':
        await q.answer('Tous les droits nécessaires sont présents.')
        try:await q.edit_message_text(activation_text(chat.title or 'Supergroupe',[]),reply_markup=q.message.reply_markup)
        except TelegramError:pass
        return
    if action!='yes':
        await q.answer('Action inconnue.',show_alert=True)
        return
    # Apply the safe closed state before activating any periodic moderation.
    try:await ctx.bot.set_chat_permissions(current_group(),CLOSED_PERMS,use_independent_chat_permissions=True)
    except TelegramError:
        await q.answer('Impossible de fermer le groupe : vérifie le droit de restriction.',show_alert=True)
        return
    await setcfg(db,'manual_until','')
    await setcfg(db,'manual_closed_until','')
    activated=await db.fetchval('''UPDATE config SET value='active'
        WHERE key='group_activation' AND value='pending'
          AND EXISTS (SELECT 1 FROM config WHERE key='group_activation_nonce' AND value=$1)
        RETURNING value''',tokens[2])
    if not activated:
        await q.answer('Activation déjà traitée.',show_alert=True)
        return
    await setcfg(db,'open_state','false')
    await setcfg(db,'group_activation_nonce','')
    await setcfg(db,'group_activation_confirmed_by',q.from_user.id)
    await q.answer('Activation confirmée.')
    try:await q.edit_message_text('✅ Activation confirmée par un administrateur.\n'
                                  '🛡 Droits vérifiés, modération et planification démarrées.')
    except TelegramError:pass
    await notify_owner(ctx.bot,f'✅ Bot activé sur {chat.title} ({current_group()}) par {q.from_user.id}.')
    await tick(ctx)

async def quiet_delete(bot,chat_id,msg_id):
    try:await bot.delete_message(chat_id,msg_id)
    except TelegramError:pass

async def notify_owner(bot,txt):
    for uid in OWNERS:
        try:await bot.send_message(uid,txt)
        except TelegramError:pass

async def mute(bot,db,uid,days):
    await bot.restrict_chat_member(current_group(),uid,permissions=CLOSED_PERMS,until_date=utcnow()+timedelta(days=days))

async def ban(bot,uid):
    await bot.ban_chat_member(current_group(),uid,revoke_messages=True)

async def db_conn(ctx):return ctx.application.bot_data['pools'][current_group()]

async def get_rows(db):
    rows=await db.fetch('SELECT start_hm,end_hm FROM schedules WHERE enabled=true')
    return [(r['start_hm'],r['end_hm']) for r in rows]

async def is_open(db,now=None):
    now=now or utcnow()
    closed=await cfg(db,'manual_closed_until')
    if closed:
        try:
            if now<datetime.fromisoformat(closed):return False
        except ValueError:pass
    manual=await cfg(db,'manual_until')
    if manual:
        try:
            if now<datetime.fromisoformat(manual):return True
        except ValueError:pass
    return await flag(db,'auto_enabled') and automatic_open(now,await get_rows(db),TZ)

async def send_countdown(bot,db,txt,key):
    if await cfg(db,'countdown_key')==key:return
    old=await cfg(db,'countdown_id')
    if old:await quiet_delete(bot,current_group(),int(old))
    try:
        msg=await bot.send_message(current_group(),txt)
        await setcfg(db,'countdown_id',msg.message_id)
        await setcfg(db,'countdown_key',key)
    except TelegramError:log.exception('countdown')

async def clean_open_messages(bot,db):
    # Telegram's normal deletion is time-limited; old messages stay on server.
    rows=await db.fetch('SELECT message_id FROM messages WHERE chat_id=$1 AND created_at>now()-interval \'48 hours\' ORDER BY message_id DESC LIMIT 10000',current_group())
    for chunk_start in range(0,len(rows),40):
        for r in rows[chunk_start:chunk_start+40]:await quiet_delete(bot,current_group(),r['message_id'])
        await asyncio.sleep(0.15)
    await db.execute('DELETE FROM messages WHERE chat_id=$1',current_group())

async def transition(bot,db,opened):
    await bot.set_chat_permissions(current_group(),OPEN_PERMS if opened else CLOSED_PERMS,
                                   use_independent_chat_permissions=True)
    await setcfg(db,'open_state',str(opened).lower())
    await setcfg(db,'last_transition',utcnow().isoformat())
    old=await cfg(db,'countdown_id')
    if old:await quiet_delete(bot,current_group(),int(old))
    await setcfg(db,'countdown_id','')
    await setcfg(db,'countdown_key','')
    if opened:
        await bot.send_message(current_group(),'🟢 Groupe ouvert ! Vous pouvez envoyer du texte, des photos et des vidéos.')
        await publish_rules(bot,db)
    else:
        await bot.send_message(current_group(),'🔴 Groupe fermé. Prochaine ouverture : compte à rebours.')
        await clean_open_messages(bot,db)
        rid=await cfg(db,'rules_id')
        if rid:await quiet_delete(bot,current_group(),int(rid))
        await setcfg(db,'rules_id','')

async def publish_rules(bot,db):
    rid=await cfg(db,'rules_id')
    if rid:await quiet_delete(bot,current_group(),int(rid))
    msg=await bot.send_message(current_group(),'📌 Règles du groupe\n\n'+await cfg(db,'rules'))
    try:await bot.pin_chat_message(current_group(),msg.message_id,disable_notification=True)
    except TelegramError:pass
    await setcfg(db,'rules_id',msg.message_id)
    await setcfg(db,'last_rules',utcnow().isoformat())

async def send_post(bot,db,post):
    old=post['last_message_id']
    if old:await quiet_delete(bot,current_group(),old)
    markup=None
    if post['kind']=='vip':
        identity=await bot.get_me()
        if not identity.username:
            log.warning('Bot username unavailable for VIP advert');return
        markup=InlineKeyboardMarkup([[InlineKeyboardButton('💎 Choisir mes VIP',url=f'https://t.me/{identity.username}?start={vip_payload(current_group(),"vip")}')]])
    elif post['kind']=='invite':
        identity=await bot.get_me()
        if not identity.username:
            log.warning('Bot username unavailable for invite advert');return
        markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔗 Recevoir mon lien personnel',url=f'https://t.me/{identity.username}?start={vip_payload(current_group(),"invite")}')]])
    elif post['button_text'] and post['button_url']:
        markup=InlineKeyboardMarkup([[InlineKeyboardButton(post['button_text'],url=post['button_url'])]])
    try:
        if post['photo_file_id']:
            msg=await bot.send_photo(current_group(),post['photo_file_id'],caption=post['body'],reply_markup=markup)
        else:msg=await bot.send_message(current_group(),post['body'],reply_markup=markup)
        await db.execute('UPDATE posts SET last_message_id=$1,last_sent_at=now() WHERE id=$2',msg.message_id,post['id'])
    except TelegramError:log.exception('post %s',post['id'])

async def tick(ctx):
    bot=ctx.bot; db=await db_conn(ctx);now=utcnow()
    if not await group_active(db):return
    missing=await group_rights(bot,current_group())
    if missing:
        await deactivate_group(db)
        await notify_owner(bot,'⚠️ Modération désactivée : droits manquants ou groupe inaccessible : '
                                +', '.join(missing)+'. /setup pour réactiver.')
        return
    try:
        open_now=await is_open(db,now)
        old=await flag(db,'open_state')
        if old!=open_now:await transition(bot,db,open_now)
        manual=await cfg(db,'manual_until')
        if manual:
            end=datetime.fromisoformat(manual)
            if end<=now:
                await setcfg(db,'manual_until','')
                await notify_owner(bot,'⏰ Ouverture manuelle terminée : délai de sécurité de 2 heures atteint.')
            elif (end-now).total_seconds()<=15*60 and not await flag(db,'manual_warned'):
                await notify_owner(bot,'⚠️ Le groupe ouvert manuellement se fermera automatiquement dans 15 minutes. /open pour prolonger.')
                await setcfg(db,'manual_warned','true')
        if open_now:
            last=await cfg(db,'last_rules')
            if not last or (now-datetime.fromisoformat(last)).total_seconds()>=1800:await publish_rules(bot,db)
            posts=await db.fetch('SELECT * FROM posts WHERE enabled=true')
            for p in posts:
                if not p['last_sent_at'] or (now-p['last_sent_at']).total_seconds()>=p['interval_minutes']*60:
                    await send_post(bot,db,p)
        rows=await get_rows(db)
        if rows:
            kind='close' if open_now else 'open'
            target=next_boundary(now,rows,kind,TZ)
            if target:
                secs=(target-now.astimezone(ZoneInfo(TZ))).total_seconds()
                if 0<secs<=48*3600 and countdown_due(secs):
                    if open_now:txt=f'⏳ Le groupe fermera dans {countdown_label(secs)}.'
                    else:txt=f'⏳ Le groupe ouvrira dans {countdown_label(secs)}.'
                    await send_countdown(bot,db,txt,f'{kind}:{target.isoformat()}:{round(secs/60)}')
        # Credit referred members only after five minutes, if still in the group.
        referrals=await db.fetch("SELECT user_id FROM referrals WHERE credited=false AND active=true AND joined_at<now()-interval '5 minutes' LIMIT 100")
        for r in referrals:
            try:
                member=await bot.get_chat_member(current_group(),r['user_id'])
                if member.status not in ('left','kicked'):
                    await db.execute('UPDATE referrals SET credited=true WHERE user_id=$1 AND credited=false AND active=true',r['user_id'])
                else:
                    await db.execute('UPDATE referrals SET active=false WHERE user_id=$1 AND credited=false',r['user_id'])
            except TelegramError:pass
        last=await cfg(db,'last_media_retention')
        if not last or (now-datetime.fromisoformat(last)).total_seconds() > 86400:
            await db.execute("DELETE FROM messages WHERE created_at<now()-interval '7 days'")
            await db.execute("DELETE FROM member_recent_joins WHERE observed_join_at<now()-interval '2 days'")
            await db.execute("DELETE FROM trusted_actions WHERE created_at<now()-interval '90 days'")
            await db.execute("""DELETE FROM media_catalog WHERE last_seen<now()-interval '90 days'
                 AND NOT EXISTS(SELECT 1 FROM media_ban_keys b WHERE b.key=media_catalog.unique_id)""")
            await setcfg(db,'last_media_retention',now.isoformat())
    except Exception:log.exception('periodic task')

async def join_update(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    cm=update.chat_member
    if not cm or cm.chat.id!=current_group():return
    old,new=cm.old_chat_member,cm.new_chat_member
    before=old.status not in ('left','kicked')
    after=new.status not in ('left','kicked')
    if before==after:return
    db=await db_conn(ctx)
    if not await group_active(db):return
    user=new.user
    if after:
        await db.execute('INSERT INTO members(user_id,display_name,username,joined_at,left_at) VALUES($1,$2,$3,now(),NULL) ON CONFLICT(user_id) DO UPDATE SET display_name=$2,username=$3,joined_at=now(),left_at=NULL',user.id,user.full_name,user.username)
        await db.execute("INSERT INTO events(user_id,kind) VALUES($1,'join')",user.id)
        await db.execute('''INSERT INTO member_recent_joins(user_id,observed_join_at)
              VALUES($1,now()) ON CONFLICT(user_id) DO UPDATE
              SET observed_join_at=now()''',user.id)
        await db.execute("UPDATE config SET value=(value::bigint+1)::text WHERE key='net_entries'")
        if await trusted(db,user.id):return
        if await db.fetchval('SELECT removed_flag FROM members WHERE user_id=$1',user.id):
            await ban(ctx.bot,user.id);return
        if await flag(db,'ban_nonlatin') and not safe_name(user.full_name):
            await ban(ctx.bot,user.id);return
        patterns=await db.fetch('SELECT pattern FROM banned_names')
        if any(p['pattern'].casefold() in (user.full_name+' '+(user.username or '')).casefold() for p in patterns):
            await ban(ctx.bot,user.id);return
        link=cm.invite_link.invite_link if cm.invite_link else None
        owner=await db.fetchval('SELECT owner_id FROM invite_links WHERE invite_link=$1',link) if link else None
        if owner and owner!=user.id:
            await db.execute('''INSERT INTO referrals(user_id,referrer_id,joined_at,active) VALUES($1,$2,now(),true) ON CONFLICT(user_id) DO UPDATE SET referrer_id=EXCLUDED.referrer_id,joined_at=now(),active=true WHERE referrals.credited=false''',user.id,owner)
    else:
        await db.execute('UPDATE members SET left_at=now() WHERE user_id=$1',user.id)
        await db.execute('UPDATE referrals SET active=false WHERE user_id=$1 AND credited=false',user.id)
        await db.execute('DELETE FROM member_recent_joins WHERE user_id=$1',user.id)
        await db.execute("INSERT INTO events(user_id,kind) VALUES($1,'leave')",user.id)
        await db.execute("UPDATE config SET value=(value::bigint+1)::text WHERE key='net_exits'")

async def inspect_media(ctx,db,msg):
    """Fast exact ID check, then bounded async hashing in the background.

    Do not block other incoming updates while ffmpeg examines video frames.
    Telegram ID check remains active for videos >20 MiB. An unscannable
    media is never presented as fully verified in the admin interface.
    """
    info=from_message(msg)
    if info is None:return False
    await mark_message(db,msg,info)
    if await id_is_banned(db,info.unique_id):
        await ban(ctx.bot,msg.from_user.id)
        return True
    if await flag(db,'anti_repost_enabled') and await id_previously_seen(db,info.unique_id):
        await quiet_delete(ctx.bot,current_group(),msg.message_id)
        return True
    if await flag(db,'anti_fast_join_enabled'):
        mins=max(1,min(15,int(await cfg(db,'anti_fast_join_minutes','5'))))
        recently_joined=await db.fetchval("""SELECT 1 FROM member_recent_joins
            WHERE user_id=$1 AND observed_join_at > now()-($2::int * interval '1 minute')""",
            msg.from_user.id,mins)
        if recently_joined:
            await ban(ctx.bot,msg.from_user.id)
            await notify_owner(ctx.bot,'🛡 Anti-publication immédiate : membre '+str(msg.from_user.id)+' retiré.')
            return True
    ctx.application.create_task(background_scan(
        ctx,msg,ban_callback=ban,anti_repost=await flag(db,'anti_repost_enabled')),
        update=None,name=f'media:{msg.message_id}')
    return False

async def warn_link(bot,uid,name):
    m=await bot.send_message(current_group(),f'🚫 Les liens sont interdits dans ce groupe. {name[:55]} a été banni.')
    async def delayed():
        await asyncio.sleep(30)
        await quiet_delete(bot,current_group(),m.message_id)
    asyncio.create_task(delayed())

async def report_action(bot,db,target,ban_user=True):
    """Track moderator evidence first, then act, then clean up in parallel.

    Ban flags are durable even if deletion fails (e.g. 48h API limit).
    """
    if ban_user:
        await ban_known_media(db,target_user_id=target)
        await ban(bot,target)  # revoke_messages=True at the Telegram API level
    else:
        count=await db.fetchval('UPDATE members SET minor_strikes=minor_strikes+1 WHERE user_id=$1 RETURNING minor_strikes',target)
        if count and count>=2:await ban(bot,target)
        else:await mute(bot,db,target,3)
    ids=await db.fetch("""SELECT message_id FROM messages
                          WHERE user_id=$1 AND chat_id=$2 AND created_at>now()-interval '48 hours'
                          ORDER BY message_id DESC LIMIT 10000""",target,current_group())
    semaphore=asyncio.Semaphore(4)
    async def remove(mid):
        async with semaphore:await quiet_delete(bot,current_group(),mid)
    await asyncio.gather(*(remove(r['message_id']) for r in ids))

async def on_message(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    msg=update.effective_message
    if not msg or msg.chat_id not in GROUPS or msg.chat_id!=current_group() or not msg.from_user or msg.from_user.is_bot:return
    db=await db_conn(ctx)
    if not await group_active(db):return
    u=msg.from_user
    admin=await trusted(db,u.id)
    text=msg.text or msg.caption or ''
    # Store for post-close cleanup and per-user moderation.
    await db.execute('INSERT INTO members(user_id,display_name,username,joined_at) VALUES($1,$2,$3,now()) ON CONFLICT(user_id) DO UPDATE SET display_name=$2,username=$3',u.id,u.full_name,u.username)
    await db.execute('INSERT INTO messages(chat_id,message_id,user_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',current_group(),msg.message_id,u.id)
    if text.startswith('/'):
        if not admin:
            await quiet_delete(ctx.bot,current_group(),msg.message_id)
            try:await mute(ctx.bot,db,u.id,3)
            except TelegramError:log.exception('mute slash')
            return
        command=text.split()[0].split('@')[0].lower()
        trusted_commands=('/pedo','/mineur','/hashdemande','/supprime','/pasfr','/info','/clean')
        if command in trusted_commands:
            await quiet_delete(ctx.bot,current_group(),msg.message_id)
            target_msg=msg.reply_to_message
            target_uid=target_msg.from_user.id if target_msg and target_msg.from_user else None
            await db.execute('''INSERT INTO trusted_actions(chat_id,actor_user_id,command,target_user_id)
                                  VALUES($1,$2,$3,$4)''',current_group(),u.id,command,target_uid)
            if command=='/clean':
                # Old bot's trusted cleanup command; cap at 300 messages.
                args=text.split()
                count=int(args[1]) if len(args)>1 and args[1].isdecimal() else 50
                count=min(300,max(1,count))
                gate=asyncio.Semaphore(4)
                async def remove(mid):
                    async with gate: await quiet_delete(ctx.bot,current_group(),mid)
                await asyncio.gather(*(remove(mid) for mid in range(msg.message_id-1,max(0,msg.message_id-count),-1)))
                return
            if command=='/hashdemande':
                if target_msg:
                    rows=await db.fetch('''SELECT m.message_id,m.media_id,m.sha256,m.phash,m.video_phash,m.scan_status,
                            c.duration,c.width,c.height FROM messages m LEFT JOIN media_catalog c
                            ON c.unique_id=m.media_id WHERE m.chat_id=$1 AND
                            (m.message_id=$2 OR (m.media_group_id IS NOT NULL AND
                            m.media_group_id=(SELECT media_group_id FROM messages
                                WHERE chat_id=$1 AND message_id=$2)))
                            AND m.media_id IS NOT NULL ORDER BY m.message_id LIMIT 15''',current_group(),target_msg.message_id)
                else:rows=[]
                blocks=[]
                for r in rows:
                    is_banned=await id_is_banned(db,r['media_id'])
                    blocks.append(f'Message #{r["message_id"]}\n'
                        f'ID: {r["media_id"]}\nSHA: {r["sha256"] or "pas encore calculé"}\n'
                        f'Photo: {r["phash"] or "—"}\nVidéo: {r["video_phash"] or "—"}\n'
                        f'Analyse: {r["scan_status"]}\nBlacklist ID: {"OUI" if is_banned else "NON"}')
                report='🔎 AUDIT HASH\n\n'+('\n\n'.join(blocks) or 'Aucun média suivi trouvé. Vérifie que tu réponds à un média récent.')
                for index in range(0,len(report),3800):
                    try:await ctx.bot.send_message(u.id,report[index:index+3800])
                    except TelegramError:log.warning('Cannot DM trusted hash report %s',u.id)
                return
            if command=='/info':
                row=await db.fetchrow('SELECT display_name,username,joined_at,left_at,messages,strikes FROM members WHERE user_id=$1',target_uid) if target_uid else None
                detail=(f'👤 ID: {target_uid}\nNom: {row["display_name"] or "?"}\n'
                        f'Username: {row["username"] or "—"}\nMessages: {row["messages"]}\n'
                        f'Sanctions mots: {row["strikes"]}' if row else 'Réponds à un message connu avec /info.')
                try:await ctx.bot.send_message(u.id,detail)
                except TelegramError:log.warning('Cannot DM /info to %s',u.id)
                return
            if target_uid is None or target_uid in OWNERS or await trusted(db,target_uid):
                return
            if command=='/supprime':
                await quiet_delete(ctx.bot,current_group(),target_msg.message_id)
            elif command=='/pasfr':
                await quiet_delete(ctx.bot,current_group(),target_msg.message_id)
                try:await mute(ctx.bot,db,target_uid,1)
                except TelegramError:log.exception('mute /pasfr')
            elif command in ('/pedo','/mineur'):
                await report_action(ctx.bot,db,target_uid,command=='/pedo')
            return
    if admin:return
    if not await is_open(db):
        await quiet_delete(ctx.bot,current_group(),msg.message_id);return
    if msg.forward_origin and not await flag(db,'allow_forwards'):
        await quiet_delete(ctx.bot,current_group(),msg.message_id);return
    has_link=bool(msg.entities and any(e.type in ('url','text_link') for e in msg.entities)) or bool(msg.caption_entities and any(e.type in ('url','text_link') for e in msg.caption_entities)) or bool(re.search(r'(?i)(https?://|www\.|t\.me/|\b[a-z0-9-]+\.(?:com|net|org|fr|io|ru|xyz)\b)',text))
    if has_link or msg.story:
        await quiet_delete(ctx.bot,current_group(),msg.message_id)
        await ban(ctx.bot,u.id)
        await warn_link(ctx.bot,u.id,u.full_name)
        return
    # Supported: text/photo/video only; reject every other message type.
    if not (msg.text or msg.photo or msg.video):
        await quiet_delete(ctx.bot,current_group(),msg.message_id);return
    terms=await db.fetch('SELECT pattern FROM banned_words')
    if any(re.search(r'(?<!\w)'+re.escape(r['pattern'])+r'(?!\w)',text,re.IGNORECASE) for r in terms):
        await quiet_delete(ctx.bot,current_group(),msg.message_id)
        count=await db.fetchval('UPDATE members SET strikes=strikes+1 WHERE user_id=$1 RETURNING strikes',u.id)
        try:await mute(ctx.bot,db,u.id,1 if count==1 else 2 if count==2 else 7)
        except TelegramError:log.exception('mute banned word')
        return
    if text.strip().casefold()=='@admin' and msg.reply_to_message and msg.reply_to_message.from_user:
        target=msg.reply_to_message.from_user.id
        await quiet_delete(ctx.bot,current_group(),msg.message_id)
        if target==u.id or await trusted(db,target):return
        await db.execute('INSERT INTO mod_reports(message_id,reporter_id,target_user_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',msg.reply_to_message.message_id,u.id,target)
        reports=await db.fetchval('SELECT count(*) FROM mod_reports WHERE message_id=$1',msg.reply_to_message.message_id)
        if reports>=3:
            await report_action(ctx.bot,db,target,True)
            await db.execute('DELETE FROM mod_reports WHERE message_id=$1',msg.reply_to_message.message_id)
            await ctx.bot.send_message(current_group(),'✅ Signalement traité. Merci aux membres ayant alerté la modération.')
        return
    await db.execute('UPDATE members SET messages=messages+1 WHERE user_id=$1',u.id)
    if msg.photo or msg.video:
        await inspect_media(ctx,db,msg)

# OWNER PANEL V5: every navigation/choice uses Telegram inline buttons.
# Text or photo is requested only where a free-form value is inherently necessary.
def owner_button(label, action, style=None):
    return InlineKeyboardButton(label, callback_data='adm:'+action, style=style)

def owner_kb(rows):
    return InlineKeyboardMarkup([[owner_button(label, action, style) for label, action, style in row]
                                 for row in rows])

def back_row(path='home'):
    return [('↩️ Retour', path, None), ('✖️ Fermer', 'dismiss', None)]

async def display_admin(ctx, page, *, q=None, msg=None, offset=0):
    db=await db_conn(ctx)
    active=await group_active(db)
    offset=max(0, min(offset, 10000))
    rows=[]
    text='🛠 <b>Administration du groupe</b>'
    if page=='home':
        opened=await is_open(db) if active else False
        try: total=await ctx.bot.get_chat_member_count(current_group()) if active else '—'
        except TelegramError:total='?'
        auto=await flag(db,'auto_enabled')
        forwards=await flag(db,'allow_forwards')
        text=(f'🛠 <b>PANNEAU ADMIN</b>\n\n'
              f'📍 Groupe <code>{current_group()}</code> : <b>{"OUVERT" if opened else "FERMÉ"}</b>\n'
              f'👥 Membres : <b>{total}</b>\n'
              f'⚙️ Activation : {"✅ Confirmée" if active else "⚠️ Non confirmée"}\n'
              f'🕒 Horaires auto : {bool_label(auto)}\n'
              f'↪️ Forwards : {bool_label(forwards)}')
        rows=[ [('🌍 Choisir le groupe','groups',None)],
               [('🟢 Ouvrir 2h','confirm:open', 'success'),('🔴 Fermer','confirm:close','danger')],
               [('↩️ Mode prévu','confirm:resume',None)],
               [('📅 Horaires','schedules',None),('🛡 Modération','moderation',None)],
               [('👤 Trusted users','trusted',None),('📰 Publications','posts',None)],
               [('💎 VIP','vips',None),('🧾 Commandes','orders',None)],
               [('🧬 Empreintes','media',None),('🩺 Santé','health',None)],
               [('📌 Règles','rules',None),('📊 Statistiques','stats',None)],
               [('🏆 Tirage au sort','raffle',None)],
               [('♻️ Actualiser','home',None),('✖️ Fermer','dismiss',None)] ]
    elif page=='groups':
        text='🌍 <b>GROUPES AUTORISÉS</b>\n\nChoisis le groupe à administrer. Les réglages et les membres sont isolés.'
        rows=[]
        titles={r['chat_id']:r['title'] for r in await ctx.application.bot_data['registry_pool'].fetch('SELECT chat_id,title FROM public.guard_groups')}
        for index,gid in enumerate(GROUPS,1):
            rows.append([(f'{"✅" if gid==current_group() else "▫️"} {short(titles.get(gid) or f"Groupe {index}",25)} ({gid})',
                          f'group:{gid}',None)])
        rows.append(back_row())
    elif page=='schedules':
        schedules=await db.fetch('SELECT * FROM schedules ORDER BY id DESC LIMIT 20')
        auto=await flag(db,'auto_enabled')
        text=f'📅 <b>HORAIRES</b>\nMode automatique : {bool_label(auto)}\n\n'
        text+='\n'.join(f'#{x["id"]} · {escape(x["start_hm"])} → {escape(x["end_hm"])}'
                          for x in schedules) or 'Aucun créneau.'
        rows=[[('▶️ Activer automatique','set:auto:1','success')]
              if not auto else [('⏸ Désactiver automatique','set:auto:0','danger')]]
        rows += [[('➕ '+label, f'slot:{i}',None)] for i,(label,_,_) in enumerate(PRESET_SLOTS)]
        rows += [[('🕒 Autre créneau','ask:schedule',None)]]
        rows += [[(f'🗑 Supprimer #{x["id"]}',f'confirm:delslot:{x["id"]}',None)] for x in schedules[:12]]
        rows += [back_row()]
    elif page=='moderation':
        forwards=await flag(db,'allow_forwards')
        nonlatin=await flag(db,'ban_nonlatin')
        repost=await flag(db,'anti_repost_enabled')
        fastjoin=await flag(db,'anti_fast_join_enabled')
        fastmins=await cfg(db,'anti_fast_join_minutes','5')
        words=await db.fetch('SELECT pattern FROM banned_words ORDER BY pattern LIMIT 80')
        names=await db.fetch('SELECT pattern FROM banned_names ORDER BY pattern LIMIT 80')
        text=(f'🛡 <b>MODÉRATION</b>\n'
              f'↪️ Forwards : {bool_label(forwards)}\n'
              f'🔤 Filtre alphabet : {bool_label(nonlatin)}\n'
              f'♻️ Anti-repost : {bool_label(repost)}\n'
              f'🚷 Anti-publication immédiate : {bool_label(fastjoin)} ({fastmins} min)\n'
              f'⛔ Mots interdits : {len(words)} affichés\n'
              f'🚫 Noms interdits : {len(names)} affichés')
        rows=[ [('↪️ Forwards ON' if not forwards else '↪️ Forwards OFF',
                  f'set:forwards:{0 if forwards else 1}',None)],
               [('🔤 Filtre alphabet ON' if not nonlatin else '🔤 Filtre alphabet OFF',
                  f'set:nonlatin:{0 if nonlatin else 1}',None)],
               [('♻️ Anti-repost ' + ('OFF' if repost else 'ON'),
                  f'set:repost:{0 if repost else 1}',None)],
               [('🚷 Anti-publication ' + ('OFF' if fastjoin else 'ON'),
                  f'set:fastjoin:{0 if fastjoin else 1}',None)],
               [('⏱ 1 min','fastmins:1',None),('3 min','fastmins:3',None),('5 min','fastmins:5',None)],
               [('⏱ 10 min','fastmins:10',None),('15 min','fastmins:15',None)],
               [('⛔ Mots interdits','filter:word:0',None)],
               [('🚫 Noms interdits','filter:name:0',None)],back_row() ]
    elif page.startswith('filter:'):
        typ=page.split(':')[1]
        table='banned_words' if typ=='word' else 'banned_names'
        header='MOTS INTERDITS' if typ=='word' else 'NOMS INTERDITS'
        patterns=await db.fetch(f'SELECT pattern FROM {table} ORDER BY pattern LIMIT 400')
        text=f'⛔ <b>{header}</b>\nAppuie sur un élément pour le supprimer.\n'
        rows=[[('➕ Ajouter',f'ask:{typ}', 'success')]]
        rows += [[('🗑 '+short(row['pattern'],28),f'confirm:rmfilter:{typ}:{hashlib.blake2s(row["pattern"].encode(),digest_size=8).hexdigest()}',None)]
                 for i,row in enumerate(patterns[offset:offset+8])]
        if not patterns: text+='\nAucun filtre enregistré.'
        nav=[]
        if offset>0:nav.append(('◀️ Précédents',f'filter:{typ}:{max(0,offset-8)}',None))
        if offset+8<len(patterns):nav.append(('Suivants ▶️',f'filter:{typ}:{offset+8}',None))
        if nav:rows.append(nav)
        rows.append(back_row('moderation'))
    elif page=='trusted':
        records=await db.fetch('SELECT user_id FROM trusted ORDER BY user_id LIMIT 2000')
        text=('👤 <b>TRUSTED USERS</b>\n\n'
              '🔐 Depuis Railway (lecture seule) :\n'+
              (', '.join(map(str,sorted(TRUSTED_USER_IDS))) or 'Aucun')+
              '\n\n📚 Depuis PostgreSQL :\n'+
              (', '.join(str(r['user_id']) for r in records) or 'Aucun')+
              '\n\nLes personnes inscrites via Railway ne peuvent pas être retirées depuis ce panneau.')
        rows=[[('➕ Ajouter un trusted','ask:trusted','success')]]
        rows += [[(f'🗑 Retirer {r["user_id"]}',f'confirm:rmtrust:{r["user_id"]}',None)] for r in records[offset:offset+12]]
        nav=[]
        if offset>0:nav.append(('◀️ Précédents',f'page:trusted:{max(offset-12,0)}',None))
        if offset+12<len(records):nav.append(('Suivants ▶️',f'page:trusted:{offset+12}',None))
        if nav:rows.append(nav)
        rows.append(back_row())
    elif page=='media':
        recent=await db.fetch("""SELECT m.message_id,m.user_id,m.media_type,m.scan_status,m.created_at,
          m.media_id,m.sha256,m.phash,m.video_phash
          FROM messages m WHERE m.chat_id=$1 AND m.media_id IS NOT NULL
          ORDER BY m.created_at DESC LIMIT 10""",current_group())
        nban=await db.fetchval('SELECT count(*) FROM media_ban_keys')
        nvid=await db.fetchval('SELECT count(*) FROM media_video_bans')
        npho=await db.fetchval('SELECT count(*) FROM media_photo_bans')
        errors=await db.fetchval("SELECT count(*) FROM messages WHERE scan_status IN ('error','unscannable')")
        text=(f'🧬 <b>HASH-BAN & MÉDIAS</b>\n\n'
              f'⛔ Clés exactes : {nban}\n'
              f'🖼 Empreintes photo bannies : {npho}\n'
              f'🎞 Empreintes vidéo bannies : {nvid}\n'
              f'⚠️ Analyses impossibles/erreurs enregistrées : {errors}\n\n'
              'Derniers médias :')
        rows=[]
        for r in recent:
            rows.append([(f'🔎 {r["media_type"] or "média"} #{r["message_id"]} · {r["scan_status"]}',
                          f'audmedia:{r["message_id"]}',None),
                         ('⛔',f'confirm:banmedia:{r["message_id"]}', 'danger')])
        if not recent:text+='\nAucun média enregistré récemment.'
        rows.append(back_row())
    elif page=='raffle':
        minimum=int(await cfg(db,'raffle_min_credits','1'))
        entrants=await db.fetch('''SELECT referrer_id AS user_id,count(*)::int AS n
          FROM referrals r JOIN members m ON m.user_id=r.referrer_id
          WHERE r.credited=true AND m.left_at IS NULL AND m.joined_at IS NOT NULL
          GROUP BY referrer_id HAVING count(*) >= $1
          ORDER BY referrer_id LIMIT 10000''',minimum)
        last=await db.fetchrow('SELECT * FROM raffle_draws ORDER BY drawn_at DESC LIMIT 1')
        text=(f'🎲 <b>TIRAGE AU SORT</b>\n\n'
              f'🏆 Participants éligibles : <b>{len(entrants)}</b>\n'
              f'🎯 Invitations validées minimum : <b>{minimum}</b>\n'
              'Une chance par participant éligible, indépendamment du nombre d’invitations.\n\n')
        if last:text+=f'Dernier gagnant : <code>{last["winner_id"]}</code> · {last["drawn_at"].date()}\n'
        rows=[[('🎯 Modifier le minimum','raffle:min',None)],
              [('🎲 Tirer au sort','confirm:raffle','success')],back_row()]
    elif page=='health':
        missing=await group_rights(ctx.bot,current_group()) if active else ['Non activé']
        server=await db.fetchval('SELECT version()')
        # Avoid exposing PostgreSQL credentials or connection strings.
        pending=await db.fetchval("SELECT count(*) FROM messages WHERE scan_status='pending' AND media_id IS NOT NULL")
        stale=await db.fetchval("SELECT count(*) FROM messages WHERE scan_status='pending' AND media_id IS NOT NULL AND created_at<now()-interval '15 minutes'")
        undelivered=await db.fetchval("SELECT count(*) FROM manual_vip_items i JOIN manual_vip_orders o ON o.id=i.order_id WHERE o.status='approved' AND i.delivered_at IS NULL")
        expiring=await db.fetchval('SELECT count(*) FROM subscriptions WHERE expires_at<=now()')
        text=('🩺 <b>ÉTAT DU SYSTÈME</b>\n\n'
              f'Base PostgreSQL : ✅ disponible\n'
              f'Groupe : <code>{current_group()}</code> / {len(GROUPS)} configuré(s)\n'
              f'Groupe actif : {bool_label(active)}\n'
              f'Droits Telegram : {"✅ suffisants" if not missing else "⚠️ " + escape(", ".join(missing))}\n'
              f'Analyses médias en attente : {pending} (dont {stale} depuis +15 min)\n'
              f'Accès VIP à livrer : {undelivered}\n'
              f'Abonnements expirés à traiter : {expiring}\n'
              f'Moyens déclarés : {", ".join(k for k,v in PAYMENT_DETAILS.items() if v) or "aucun configuré"}\n'
              'Analyse vidéo : FFmpeg imageio-ffmpeg requis\n'
              'Limite cloud Telegram : 20 MiB par téléchargement\n\n'
              'Ce diagnostic ne remplace pas un test de bout en bout sur Railway.')
        rows=[[('♻️ Recontrôler','health',None)],back_row()]
    elif page=='rules':
        rules=await cfg(db,'rules')
        text='📌 <b>RÈGLES DU GROUPE</b>\n\n'+escape(rules[:2800])
        rows=[[('✏️ Modifier les règles','ask:rules',None)],[('📌 Republier maintenant','confirm:pubrules',None)],back_row()]
    elif page=='stats':
        text=escape(await stats_text(ctx,db))
        rows=[[('♻️ Actualiser','stats',None)],back_row()]
    elif page=='posts':
        posts=await db.fetch('SELECT * FROM posts ORDER BY id DESC LIMIT 1000')
        text='📰 <b>PUBLICATIONS</b>\n\n'
        text+='\n'.join(f'#{x["id"]} {"🟢" if x["enabled"] else "🔴"} '
                          f'{escape(x["kind"])} · {x["interval_minutes"]} min — {short(x["body"],30)}'
                          for x in posts[offset:offset+10]) or 'Aucune publication.'
        rows=[[('➕ Pub normale','postkind:ad',None),('🔗 Pub invitation','postkind:invite',None)],
              [('💎 Pub VIP','postkind:vip',None)]]
        for x in posts[offset:offset+10]:
            rows.append([(f'{"⏸ OFF" if x["enabled"] else "▶️ ON"} #{x["id"]}',f'posttoggle:{x["id"]}',None),
                         ('📤 Envoyer',f'confirm:postnow:{x["id"]}',None),
                         ('🗑',f'confirm:delpost:{x["id"]}',None)])
        nav=[]
        if offset>0:nav.append(('◀️ Précédents',f'page:posts:{max(offset-10,0)}',None))
        if offset+10<len(posts):nav.append(('Suivants ▶️',f'page:posts:{offset+10}',None))
        if nav:rows.append(nav)
        rows.append(back_row())
    elif page=='vips':
        offers=await db.fetch('SELECT * FROM vip ORDER BY id DESC LIMIT 1000')
        text='💎 <b>OFFRES VIP</b>\n\n'
        text+='\n'.join(f'#{v["id"]} {"🟢" if v["enabled"] else "🔴"} '
                          f'{short(v["title"],26)} · '
                          f'{money(v["price_eur"]) if v["price_eur"] is not None else "Prix non défini"} · '
                          f'{"À vie" if v["days"]==0 else str(v["days"])+" j"}' +
                          (f' · 🎯 {v["invite_goal"]} invitations' if v['invite_goal'] else '') +
                          (' · 24/24' if v['always_open'] else '')
                          for v in offers[offset:offset+10]) or 'Aucun VIP.'
        rows=[[('➕ Ajouter une offre','ask:vipchat','success')]]
        for v in offers[offset:offset+10]:
            rows.append([(f'{"⏸ OFF" if v["enabled"] else "▶️ ON"} #{v["id"]}',f'viptoggle:{v["id"]}',None),
                         ('💶 Prix',f'vipprice:{v["id"]}',None),
                         ('🎯 Invitations',f'vipgoal:{v["id"]}',None),
                         ('♾ 24/24',f'vip247:{v["id"]}',None)])
        nav=[]
        if offset>0:nav.append(('◀️ Précédents',f'page:vips:{max(offset-10,0)}',None))
        if offset+10<len(offers):nav.append(('Suivants ▶️',f'page:vips:{offset+10}',None))
        if nav:rows.append(nav)
        rows.append(back_row())
    elif page=='orders':
        records=await db.fetch("SELECT * FROM manual_vip_orders WHERE status='review' ORDER BY proof_at DESC LIMIT 1000")
        text='🧾 <b>PAIEMENTS À VÉRIFIER</b>\n\n'
        text+='\n'.join(f'{r["id"][:8]} · #{r["user_id"]} · {money(r["final_total_eur"])}' for r in records[offset:offset+10]) or 'Aucun dossier en attente.'
        rows += [[(f'🔎 Voir {r["id"][:8]}',f'order:{r["id"]}',None)] for r in records[offset:offset+10]]
        nav=[]
        if offset>0:nav.append(('◀️ Précédents',f'page:orders:{max(offset-10,0)}',None))
        if offset+10<len(records):nav.append(('Suivants ▶️',f'page:orders:{offset+10}',None))
        if nav:rows.append(nav)
        rows += [[('♻️ Actualiser','orders',None)],back_row()]
    else:
        return await display_admin(ctx,'home',q=q,msg=msg)
    markup=owner_kb(rows)
    if q:
        await show_screen(q,text,markup,edit=True)
    else:
        await show_screen(msg,text,markup)

async def panel(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    if update.effective_chat.type!='private' or not is_owner(update.effective_user.id):return
    if not GROUPS:
        await update.effective_message.reply_text('Ajoute d’abord le bot comme administrateur dans un supergroupe, puis confirme son activation. Aucun GROUP_ID Railway nécessaire.')
        return
    ctx.user_data.pop('adm_pending',None)
    await display_admin(ctx,'home',msg=update.effective_message)

async def admin_input(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    """Only consumes owner messages after an explicit button, never owner commands.
    The text/photo is a field value, not a command to configure the bot.
    """
    if not update.effective_user or not is_owner(update.effective_user.id) or update.effective_chat.type!='private':return False
    st=ctx.user_data.get('adm_pending')
    if not st:return False
    msg=update.effective_message
    text=(msg.text or msg.caption or '').strip()
    mode=st['mode'];db=await db_conn(ctx)
    try:
        if mode=='schedule':
            begin,end=parse_slot(text)
            await db.execute('INSERT INTO schedules(start_hm,end_hm) VALUES($1,$2)',begin,end)
            destination='schedules'
        elif mode in ('word','name'):
            if not 2<=len(text)<=120:raise ValueError('Entre 2 et 120 caractères.')
            table='banned_words' if mode=='word' else 'banned_names'
            await db.execute(f'INSERT INTO {table}(pattern) VALUES($1) ON CONFLICT DO NOTHING',text.casefold())
            destination='filter:'+mode
        elif mode=='trusted':
            uid=parse_user_id(text)
            await db.execute('INSERT INTO trusted(user_id) VALUES($1) ON CONFLICT DO NOTHING',uid)
            destination='trusted'
        elif mode=='rules':
            if not 5<=len(text)<=3000:raise ValueError('Entre 5 et 3 000 caractères.')
            await setcfg(db,'rules',text);destination='rules'
        elif mode=='post':
            if not 1<=len(text)<=1000:raise ValueError('Il faut un texte ou une légende de 1 à 1000 caractères.')
            photo=msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document and (msg.document.mime_type or '').startswith('image/') else None)
            await db.execute('INSERT INTO posts(kind,body,photo_file_id,interval_minutes) VALUES($1,$2,$3,$4)',
                             st['kind'],text,photo,st['interval'])
            destination='posts'
        elif mode=='vipchat':
            chat_id=int(text)
            if chat_id>=0:raise ValueError('Le groupe VIP doit être identifié par son ID négatif.')
            if chat_id in GROUPS:
                raise ValueError('Ce groupe est un groupe principal à horaires, pas un groupe VIP destinataire.')
            for other_gid,other_pool in ctx.application.bot_data['pools'].items():
                if other_gid != current_group() and await other_pool.fetchval(
                        'SELECT 1 FROM vip WHERE chat_id=$1',chat_id):
                    raise ValueError('Ce groupe VIP appartient déjà à un autre groupe principal.')
            await db.execute('INSERT INTO vip_registered_chats(chat_id,registered_by) VALUES($1,$2) ON CONFLICT DO NOTHING',chat_id,msg.from_user.id)
            try:member=await ctx.bot.get_chat_member(chat_id,ctx.bot.id)
            except TelegramError:
                raise ValueError('Groupe VIP préautorisé. Ajoute le bot comme administrateur du VIP puis renvoie son ID.')
            if member.status not in ('administrator','creator'):
                raise ValueError('Groupe VIP préautorisé. Accorde les droits admin au bot puis renvoie son ID.')
            ctx.user_data['adm_pending']={'mode':'viptitle','chat_id':chat_id}
            await msg.reply_text('💎 Envoie le <b>nom de l’offre VIP</b> (sans commande).',parse_mode='HTML',reply_markup=owner_kb([[('✖️ Annuler','cancel',None)]]))
            return True
        elif mode=='viptitle':
            if not 2<=len(text)<=80:raise ValueError('Entre 2 et 80 caractères.')
            st['title']=text;st['mode']='vipprice'
            await msg.reply_text('💶 Envoie le <b>prix en euros</b> (ex. 35,50).',parse_mode='HTML',reply_markup=owner_kb([[('✖️ Annuler','cancel',None)]]))
            return True
        elif mode in ('vipprice','editprice'):
            price=parse_euros(text)
            if mode=='editprice':
                await db.execute('UPDATE vip SET price_eur=$2 WHERE id=$1',st['vip_id'],price)
                destination='vips'
            else:
                st['price']=str(price);st['mode']='vipdays_buttons'
                await msg.reply_text('📅 <b>Durée de l’accès VIP :</b>',parse_mode='HTML',reply_markup=owner_kb([
                    [('♾ À vie','vipdays:0',None),('⏳ 24 heures','vipdays:1',None)],
                    [('📅 30 jours','vipdays:30',None)],
                    [('📅 90 jours','vipdays:90',None),('🖊 Autre durée','vipdays:custom',None)],
                    [('✖️ Annuler','cancel',None)]]))
                return True
        elif mode=='vipgoal':
            if not text.isdecimal() or not 0<=int(text)<=100000:
                raise ValueError('Nombre d’invitations entre 0 et 100000 (0 désactive l’accès par invitations).')
            await db.execute('UPDATE vip SET invite_goal=$2 WHERE id=$1',st['vip_id'],int(text))
            destination='vips'
        elif mode=='raffle_min':
            if not text.isdecimal() or not 1<=int(text)<=10000:
                raise ValueError('Nombre minimum entre 1 et 10000.')
            await setcfg(db,'raffle_min_credits',int(text))
            destination='raffle'
        elif mode=='vipdays_custom':
            days=int(text)
            if not 1<=days<=3650:raise ValueError('Durée entre 1 et 3650 jours.')
            st['days']=days;st['mode']='vipdescription'
            await msg.reply_text('📝 Envoie la <b>description du VIP</b> ou appuie sur « Sans description ».',parse_mode='HTML',reply_markup=owner_kb([
                [('Sans description','vipdesc:skip',None),('✖️ Annuler','cancel',None)]]))
            return True
        elif mode=='vipdescription':
            if len(text)>1000:raise ValueError('Description de 1000 caractères maximum.')
            await db.execute('INSERT INTO vip(chat_id,title,description,price_stars,price_eur,days) VALUES($1,$2,$3,0,$4,$5)',
                             st['chat_id'],st['title'],text,Decimal(st['price']),st['days'])
            destination='vips'
        else:
            return False
    except (ValueError,InvalidOperation,TelegramError) as exc:
        await msg.reply_text('⚠️ '+escape(str(exc))+'\nRéessaie, ou annule avec le bouton.',parse_mode='HTML',
                             reply_markup=owner_kb([[('✖️ Annuler','cancel',None)]]))
        return True
    ctx.user_data.pop('adm_pending',None)
    await msg.reply_text('✅ Enregistré.',reply_markup=owner_kb([[('↩️ Revenir au menu',''+destination,None)]]))
    return True

async def admin_callbacks(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    q=update.callback_query
    if not q:return
    if q.message.chat.type!='private' or not is_owner(q.from_user.id):
        await q.answer('Accès réservé au propriétaire.',show_alert=True);return
    if not GROUPS:
        await q.answer('Aucun groupe enregistré.',show_alert=True);return
    data=(q.data or '')[4:]
    if data.startswith('group:'):
        try:gid=int(data.split(':')[1])
        except (IndexError,ValueError):gid=0
        if gid not in GROUPS:
            await q.answer('Groupe non autorisé.',show_alert=True);return
        ctx.user_data['guard_selected_group']=gid
        bind_group(update,ctx)
        ctx.user_data.pop('adm_pending',None)
        await q.answer('Groupe sélectionné.')
        await display_admin(ctx,'home',q=q)
        return
    db=await db_conn(ctx)
    if data=='dismiss':
        ctx.user_data.pop('adm_pending',None)
        await q.answer()
        await q.edit_message_text('🔒 Panneau fermé. Pour le rouvrir : /panel')
        return
    if data=='cancel':
        ctx.user_data.pop('adm_pending',None)
        await q.answer('Saisie annulée.');await display_admin(ctx,'home',q=q);return
    if data.startswith('ask:'):
        mode=data.split(':')[1]
        if mode not in ('schedule','word','name','trusted','rules','vipchat'):
            await q.answer('Action inconnue.',show_alert=True);return
        ctx.user_data['adm_pending']={'mode':mode}
        info={'schedule':'deux horaires, par exemple <code>22:00 00:00</code>',
              'word':'le <b>mot interdit</b> à ajouter',
              'name':'le <b>texte interdit dans les noms</b> à ajouter',
              'trusted':'l’<b>ID numérique</b> du trusted user',
              'rules':'les <b>règles complètes du groupe</b>',
              'vipchat':'l’<b>ID du groupe VIP</b> (ex. -1001234567890)'}
        await q.answer()
        await show_screen(q,'✏️ Envoie '+info[mode]+'.\n\nAucune commande à taper.',
                          owner_kb([[('✖️ Annuler','cancel',None)]]),edit=True)
        return
    if data.startswith('page:'):
        parts=data.split(':')
        if len(parts)!=3 or parts[1] not in ('trusted','posts','vips','orders'):
            await q.answer('Page inconnue.',show_alert=True);return
        offset=int(parts[2])
        await q.answer();await display_admin(ctx,parts[1],q=q,offset=offset);return
    if data.startswith('filter:'):
        parts=data.split(':')
        typ=parts[1];offset=int(parts[2]) if len(parts)>2 else 0
        if typ not in ('word','name'):await q.answer('Invalide.',show_alert=True);return
        await q.answer();await display_admin(ctx,'filter:'+typ,q=q,offset=offset);return
    if data.startswith('postkind:'):
        kind=data.split(':')[1]
        if kind not in POST_INTERVALS:await q.answer('Type inconnu.',show_alert=True);return
        ctx.user_data['adm_post_kind']=kind
        await q.answer()
        await show_screen(q,'📰 <b>Choisis la fréquence :</b>',
                          owner_kb([[ (f'{mins} min',f'postinterval:{mins}',None) ] for mins in POST_INTERVALS[kind]]+
                                   [back_row('posts')]),edit=True)
        return
    if data.startswith('postinterval:'):
        mins=int(data.split(':')[1]);kind=ctx.user_data.get('adm_post_kind')
        if kind not in POST_INTERVALS or mins not in POST_INTERVALS[kind]:
            await q.answer('Commence par choisir un type de publicité.',show_alert=True);return
        ctx.user_data['adm_pending']={'mode':'post','kind':kind,'interval':mins}
        await q.answer()
        await show_screen(q,'📝 Envoie le <b>texte</b> ou une <b>photo avec légende</b>.\nLa publication sera enregistrée.',
                          owner_kb([[('✖️ Annuler','cancel',None)]]),edit=True)
        return
    if data=='raffle:min':
        ctx.user_data['adm_pending']={'mode':'raffle_min'}
        await q.answer()
        await show_screen(q,'🎯 Envoie le nombre minimum d’invitations validées requis pour participer au tirage.',
                          owner_kb([[('✖️ Annuler','cancel',None)]]),edit=True)
        return
    if data.startswith('vipgoal:'):
        vid=int(data.split(':')[1])
        exists=await db.fetchval('SELECT 1 FROM vip WHERE id=$1',vid)
        if not exists:await q.answer('Offre inconnue.',show_alert=True);return
        ctx.user_data['adm_pending']={'mode':'vipgoal','vip_id':vid}
        await q.answer()
        await show_screen(q,f'🎯 Combien d’invitations validées pour débloquer le VIP #{vid} ?\n'
                            'Envoie un nombre entier, ou 0 pour désactiver.',
                          owner_kb([[('✖️ Annuler','cancel',None)]]),edit=True)
        return
    if data.startswith('vip247:'):
        vid=int(data.split(':')[1])
        offer=await db.fetchrow('SELECT * FROM vip WHERE id=$1',vid)
        if not offer:await q.answer('Offre inconnue.',show_alert=True);return
        if not offer['always_open']:
            if offer['chat_id'] in GROUPS:
                await q.answer('Ce chat est déjà géré par les horaires. Choisis un groupe VIP distinct.',show_alert=True);return
            try:
                await ctx.bot.set_chat_permissions(offer['chat_id'],OPEN_PERMS,use_independent_chat_permissions=True)
            except TelegramError:
                await q.answer('Il manque les droits pour garder ce VIP ouvert.',show_alert=True);return
        await db.execute('UPDATE vip SET always_open=NOT always_open WHERE id=$1',vid)
        await q.answer('Mode 24/24 modifié.')
        await display_admin(ctx,'vips',q=q);return
    if data.startswith('vipprice:'):
        vid=int(data.split(':')[1]);exists=await db.fetchval('SELECT 1 FROM vip WHERE id=$1',vid)
        if not exists:await q.answer('Offre introuvable.',show_alert=True);return
        ctx.user_data['adm_pending']={'mode':'editprice','vip_id':vid}
        await q.answer()
        await show_screen(q,f'💶 Envoie le <b>nouveau prix en euros</b> pour VIP #{vid}.',
                          owner_kb([[('✖️ Annuler','cancel',None)]]),edit=True)
        return
    if data.startswith('vipdays:'):
        st=ctx.user_data.get('adm_pending') or {}
        if st.get('mode')!='vipdays_buttons':await q.answer('Étape expirée.',show_alert=True);return
        choice=data.split(':')[1]
        if choice=='custom':
            st['mode']='vipdays_custom'
            await q.answer()
            await show_screen(q,'📅 Envoie le <b>nombre de jours</b> (1 à 3650).',owner_kb([[('✖️ Annuler','cancel',None)]]),edit=True)
            return
        if choice not in ('0','1','30','90'):await q.answer('Durée invalide.',show_alert=True);return
        st['days']=int(choice);st['mode']='vipdescription'
        await q.answer()
        await show_screen(q,'📝 Envoie une <b>description</b>, ou choisis « Sans description ».',
                          owner_kb([[('Sans description','vipdesc:skip',None),('✖️ Annuler','cancel',None)]]),edit=True)
        return
    if data=='vipdesc:skip':
        st=ctx.user_data.get('adm_pending') or {}
        if st.get('mode')!='vipdescription':await q.answer('Étape expirée.',show_alert=True);return
        await db.execute('INSERT INTO vip(chat_id,title,description,price_stars,price_eur,days) VALUES($1,$2,$3,0,$4,$5)',
                         st['chat_id'],st['title'],'',Decimal(st['price']),st['days'])
        ctx.user_data.pop('adm_pending',None)
        await q.answer('VIP ajouté.');await display_admin(ctx,'vips',q=q);return
    if data.startswith('confirm:'):
        action=data[len('confirm:'):]
        # Require a second explicit button for sensitive or destructive operations.
        labels={'open':'Ouvrir manuellement le groupe pendant 2 h ?',
                'close':'Fermer immédiatement le groupe ?',
                'resume':'Revenir à la planification automatique ?',
                'pubrules':'Publier et épingler les règles maintenant ?',
                'raffle':'Tirer un gagnant au hasard parmi les participants éligibles ?'}
        if action.startswith('banmedia:'):labels[action]='Bannir toutes les empreintes de ce média (et de son album si connu) ?'
        if action.startswith('delslot:'):labels[action]='Supprimer ce créneau horaire ?'
        if action.startswith('rmfilter:'):labels[action]='Supprimer ce filtre de modération ?'
        if action.startswith('rmtrust:'):labels[action]='Retirer ce trusted user inscrit en base ?'
        if action.startswith('delpost:'):labels[action]='Supprimer cette publicité ?'
        if action.startswith('postnow:'):labels[action]='Publier ce message maintenant dans le groupe ?'
        if action.startswith('approve:'):labels[action]='Confirmer que les fonds sont réellement reçus et donner accès ?'
        if action.startswith('reject:'):labels[action]='Refuser ce justificatif de paiement ?'
        if action not in labels:await q.answer('Action inconnue.',show_alert=True);return
        await q.answer()
        await show_screen(q,'⚠️ <b>CONFIRMATION</b>\n\n'+escape(labels[action]),
                          owner_kb([[('✅ Confirmer','do:'+action,'danger'),('↩️ Annuler','home',None)]]),edit=True)
        return
    if data.startswith('order:'):
        oid=data[len('order:'):]
        record=await db.fetchrow("SELECT * FROM manual_vip_orders WHERE id=$1 AND status='review'",oid)
        if not record:await q.answer('Dossier non disponible.',show_alert=True);return
        await q.answer()
        try:
            await ctx.bot.send_photo(q.from_user.id,record['proof_file_id'],caption='🧾 Justificatif de commande '+oid[:8])
        except TelegramError:log.exception('Unable to deliver proof for order %s',oid)
        await show_screen(q,f'🧾 <b>COMMANDE {escape(oid[:8])}</b>\n'
                          f'Membre : <code>{record["user_id"]}</code>\n'
                          f'Montant déclaré : <b>{escape(money(record["final_total_eur"]))}</b>\n'
                          f'Méthode : {escape(record["method"])}\n\n'
                          '⚠️ Vérifie la réception des fonds chez le prestataire.',
                          owner_kb([[('✅ Paiement vérifié','confirm:approve:'+oid,'success')],
                                    [('❌ Refuser','confirm:reject:'+oid,'danger')],back_row('orders')]),edit=True)
        return
    if data.startswith('do:'):
        action=data[3:];destination='home'
        if action in ('open','close','resume') and not await group_active(db):
            await q.answer('Le groupe doit être activé via /setup.',show_alert=True);return
        if action=='open':
            await setcfg(db,'manual_closed_until','')
            await setcfg(db,'manual_until',(utcnow()+timedelta(hours=2)).isoformat())
            await setcfg(db,'manual_warned','false')
        elif action=='close':
            await setcfg(db,'manual_until','')
            nxt=next_boundary(utcnow(),await get_rows(db),'open',TZ)
            await setcfg(db,'manual_closed_until',(nxt.astimezone(timezone.utc) if nxt else utcnow()+timedelta(hours=2)).isoformat())
        elif action=='resume':
            await setcfg(db,'manual_until','');await setcfg(db,'manual_closed_until','')
        elif action=='raffle':
            winner=await draw_raffle(db,q.from_user.id)
            if not winner:
                await q.answer('Aucun participant éligible.',show_alert=True);return
            await notify_owner(ctx.bot,f'🎲 Tirage groupe {current_group()} : gagnant {winner["user_id"]} ({winner["n"]} invitations).')
            destination='raffle'
        elif action=='pubrules':
            if await group_active(db):await publish_rules(ctx.bot,db)
            destination='rules'
        elif action.startswith('banmedia:'):
            mid=int(action.split(':')[1])
            result=await ban_from_message(db,mid,q.from_user.id)
            if not result['media']:
                await q.answer('Média introuvable.',show_alert=True);return
            destination='media'
        elif action.startswith('delslot:'):
            await db.execute('DELETE FROM schedules WHERE id=$1',int(action.split(':')[1]));destination='schedules'
        elif action.startswith('rmfilter:'):
            _,typ,digest=action.split(':')
            if typ not in ('word','name') or not re.fullmatch(r'[0-9a-f]{16}',digest):
                await q.answer('Filtre invalide.',show_alert=True);return
            table='banned_words' if typ=='word' else 'banned_names'
            matches=[r['pattern'] for r in await db.fetch(f'SELECT pattern FROM {table}')
                     if hashlib.blake2s(r['pattern'].encode(),digest_size=8).hexdigest()==digest]
            if len(matches)!=1:await q.answer('Filtre absent ou ambigu.',show_alert=True);return
            await db.execute(f'DELETE FROM {table} WHERE pattern=$1',matches[0])
            destination='filter:'+typ
        elif action.startswith('rmtrust:'):
            uid=int(action.split(':')[1]);await db.execute('DELETE FROM trusted WHERE user_id=$1',uid);destination='trusted'
        elif action.startswith('delpost:'):
            rid=int(action.split(':')[1]);record=await db.fetchrow('DELETE FROM posts WHERE id=$1 RETURNING last_message_id',rid)
            if record and record['last_message_id']:await quiet_delete(ctx.bot,current_group(),record['last_message_id'])
            destination='posts'
        elif action.startswith('postnow:'):
            rid=int(action.split(':')[1]);record=await db.fetchrow('SELECT * FROM posts WHERE id=$1',rid)
            if record and await group_active(db) and await is_open(db):await send_post(ctx.bot,db,record)
            else:await q.answer('Publication impossible : le groupe est fermé ou désactivé.',show_alert=True);return
            destination='posts'
        elif action.startswith('approve:') or action.startswith('reject:'):
            approve=action.startswith('approve:')
            oid=action.split(':')[1]
            ok=await decide_manual_order(ctx.bot,db,oid,q.from_user.id,approve)
            if not ok:await q.answer('Déjà traité ou justificatif manquant.',show_alert=True);return
            destination='orders'
        else:await q.answer('Inconnu.',show_alert=True);return
        await q.answer('✅ Action appliquée.')
        if action in ('open','close','resume'):await tick(ctx)
        await display_admin(ctx,destination,q=q);return
    if data.startswith('slot:'):
        ix=int(data.split(':')[1]);
        if ix<0 or ix>=len(PRESET_SLOTS):await q.answer('Créneau invalide.',show_alert=True);return
        _,begin,end=PRESET_SLOTS[ix]
        await db.execute('INSERT INTO schedules(start_hm,end_hm) VALUES($1,$2)',begin,end)
        await q.answer('Créneau ajouté.');await display_admin(ctx,'schedules',q=q);return
    if data.startswith('set:'):
        _,key,val=data.split(':')
        col={'auto':'auto_enabled','forwards':'allow_forwards','nonlatin':'ban_nonlatin'}.get(key)
        if not col or val not in ('0','1'):await q.answer('Paramètre incorrect.',show_alert=True);return
        await setcfg(db,col,'true' if val=='1' else 'false')
        await q.answer('✅ Modifié.')
        await display_admin(ctx,'schedules' if key=='auto' else 'moderation',q=q);return
    if data.startswith('fastmins:'):
        minutes=int(data.split(':')[1])
        if minutes not in (1,3,5,10,15):
            await q.answer('Durée invalide.',show_alert=True);return
        await setcfg(db,'anti_fast_join_minutes',minutes)
        await q.answer('✅ Durée modifiée.')
        await display_admin(ctx,'moderation',q=q);return
    if data.startswith('audmedia:'):
        mid=int(data.split(':')[1])
        media=await db.fetchrow('''SELECT media_type,media_id,sha256,phash,video_phash,
               scan_status,user_id FROM messages WHERE chat_id=$1 AND message_id=$2''',current_group(),mid)
        if not media or not media['media_id']:
            await q.answer('Média introuvable.',show_alert=True);return
        blocked=await id_is_banned(db,media['media_id'])
        text=('🔎 <b>AUDIT MÉDIA</b>\n\n'
              f'Message : <code>{mid}</code>\n'
              f'Auteur : <code>{media["user_id"]}</code>\n'
              f'Type : {escape(str(media["media_type"]))}\n'
              f'Analyse : {escape(media["scan_status"] or "en attente")}\n'
              f'ID : <code>{escape(str(media["media_id"]))}</code>\n'
              f'SHA256 : <code>{escape(str(media["sha256"] or "non calculé"))}</code>\n'
              f'Photo pHash : <code>{escape(str(media["phash"] or "—"))}</code>\n'
              f'Vidéo vphash : <code>{escape(str(media["video_phash"] or "—"))[:950]}</code>\n'
              f'Blacklist exacte : {"⛔ OUI" if blocked else "NON"}\n'
              '⚠️ Le résultat perceptuel peut différer après réencodage.')
        await q.answer()
        await show_screen(q,text,owner_kb([[('⛔ Bannir le média',f'confirm:banmedia:{mid}','danger')],back_row('media')]),edit=True)
        return
    if data.startswith('posttoggle:'):
        rid=int(data.split(':')[1]);await db.execute('UPDATE posts SET enabled=NOT enabled WHERE id=$1',rid)
        await q.answer('✅ Modifié.');await display_admin(ctx,'posts',q=q);return
    if data.startswith('viptoggle:'):
        rid=int(data.split(':')[1]);await db.execute('UPDATE vip SET enabled=NOT enabled WHERE id=$1',rid)
        await q.answer('✅ Modifié.');await display_admin(ctx,'vips',q=q);return
    if data in ('home','groups','schedules','moderation','trusted','rules','stats','posts','vips','orders','media','health','raffle'):
        ctx.user_data.pop('adm_pending',None)
        await q.answer();await display_admin(ctx,data,q=q);return
    await q.answer('Option inconnue.',show_alert=True)

async def legacy_admin_callback(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    q=update.callback_query
    await q.answer('Ancien panneau remplacé. Ouvre /panel pour les nouveaux boutons.',show_alert=True)

async def stats_text(ctx,db):
    count=await ctx.bot.get_chat_member_count(current_group())
    joins=await db.fetchval("SELECT count(*) FROM events WHERE kind='join'")
    exits=await db.fetchval("SELECT count(*) FROM events WHERE kind='leave'")
    active=await db.fetchval('SELECT count(*) FROM members WHERE joined_at IS NOT NULL AND left_at IS NULL AND messages>0')
    idle=await db.fetchval('SELECT count(*) FROM members WHERE joined_at IS NOT NULL AND left_at IS NULL AND messages=0')
    validated=await db.fetchval('SELECT count(*) FROM referrals WHERE credited=true')
    waiting=await db.fetchval('SELECT count(*) FROM referrals WHERE credited=false AND active=true')
    return f'📊 Membres Telegram : {count}\nEntrées observées : {joins}\nSorties observées : {exits}\nSolde observé : {joins-exits:+d}\nActifs enregistrés : {active}\nSans message enregistré : {idle}\nParrainages validés : {validated}\nEn attente : {waiting}\n⚠️ Historique non exhaustif avant installation.'

HELP='''Commandes propriétaires (en privé) :
/setup — activation dans le supergroupe par un admin du groupe
/panel — commandes rapides
/stats — statistiques
/schedule 22:00 00:00 — ajouter un créneau
/schedules — voir les créneaux
/delschedule ID — supprimer un créneau
/word mot — interdire un mot
/unword mot — supprimer le mot
/name mot — bannir les noms contenant mot
/unname mot — enlever le filtre
/trust 12345 — accorder la modération
/untrust 12345 — retirer (si présent en base)
/trusted — afficher les modérateurs configurés
/rules texte — remplacer les règles
/forward on|off — autoriser les médias transférés
/nonlatin on|off — filtre de nom (à utiliser prudemment)
/open — ouvrir 2h ; /close — fermer jusqu'au prochain créneau ; /resume — effacer les overrides
/post 30|120|180|360 texte — créer une publicité texte (photo par réponse à une photo)
/invitead 30|120|180|360 texte — pub automatique avec bouton de parrainage
/vipad 120|180|300|600 texte — publicité VIP 2h/3h/5h/10h ; /vipad on|off
/posts — répertorier ; /delpost ID — effacer
/invite — lien personnel en privé pour parrainer
/top — classement 10 premiers
/vipadd CHAT_ID PRIX_EUR JOURS NOM | DESCRIPTION — ajouter une offre (JOURS=0 : à vie)
/vipprice ID PRIX_EUR — fixer le prix en euros d’une ancienne offre
/orders — justificatifs en attente
/approve ID ou /reject ID — vérifier puis valider/refuser une commande
/vips — catalogue à choix multiple ; /vipoff ID — désactiver
/paysupport — aide pour les paiements VIP
En groupe, trusted admins : /pedo ou /mineur EN RÉPONSE à un message.
Les justificatifs de paiement sont contrôlés manuellement. Ne jamais approuver sur capture seule.
'''

# ---- Native VIP storefront & referral dashboard (private Telegram conversations) ----
async def member_language(db,uid):
    return (await db.fetchval('SELECT lang FROM vip_preferences WHERE user_id=$1',uid)) or 'fr'

async def all_vip_offers(db):
    return await db.fetch('''SELECT * FROM vip WHERE enabled=true AND
           ((price_eur IS NOT NULL AND price_eur>0) OR invite_goal>0) ORDER BY id''')

async def show_screen(target,text,markup,edit=False):
    """Edit the same inline-keyboard message to avoid a wall of bot messages."""
    if edit:
        try:
            await target.edit_message_text(text,parse_mode='HTML',reply_markup=markup,
                                           disable_web_page_preview=True)
        except TelegramError as err:
            if 'Message is not modified' not in str(err):
                await target.message.reply_text(text,parse_mode='HTML',reply_markup=markup,
                                                disable_web_page_preview=True)
    else:
        await target.reply_text(text,parse_mode='HTML',reply_markup=markup,
                                disable_web_page_preview=True)

async def send_vip_catalog(ctx,uid,msg=None,q=None):
    db=await db_conn(ctx)
    offers=await all_vip_offers(db)
    lang=await member_language(db,uid)
    if not offers:
        return await show_screen(q or msg,'💎 Aucune offre VIP disponible actuellement.',None,edit=bool(q))
    chosen=await db.fetch('SELECT vip_id FROM vip_cart WHERE user_id=$1',uid)
    selected={row['vip_id'] for row in chosen}
    selected.intersection_update({o['id'] for o in offers})
    user=q.from_user if q else msg.from_user
    text,labels,continue_label=vip_catalog_view(offers,selected,user.first_name or '',lang)
    keyboard=[[InlineKeyboardButton(label,callback_data=f'vip:toggle:{id_}',
                                     style='primary' if id_ in selected else None)]
              for label,id_ in labels]
    current_credits=await db.fetchval('SELECT count(*) FROM referrals WHERE referrer_id=$1 AND credited=true',uid)
    for offer in offers:
        if offer['invite_goal']:
            credited,needed,ready=goal_progress(current_credits,offer['invite_goal'])
            status='✅' if ready else '🔒'
            keyboard.append([InlineKeyboardButton(
                f'{status} {offer["title"][:22]} · {credited}/{needed} invitations',
                callback_data=f'vip:claim:{offer["id"]}',
                style='success' if ready else None)])
    keyboard.append([InlineKeyboardButton(continue_label,callback_data='vip:go',style='danger')])
    keyboard.append([InlineKeyboardButton('🇫🇷 FRANÇAIS' if lang=='en' else '🇺🇸 ENGLISH',
                                          callback_data='vip:lang')])
    keyboard.append([InlineKeyboardButton('🔗 Mes invitations' if lang=='fr' else '🔗 My invites',
                                          callback_data='ref:home')])
    await show_screen(q or msg,text,InlineKeyboardMarkup(keyboard),edit=bool(q))

async def send_vip_confirmation(ctx,uid,q):
    db=await db_conn(ctx)
    chosen=await db.fetch('''SELECT v.* FROM vip_cart c JOIN vip v ON v.id=c.vip_id
                             WHERE c.user_id=$1 AND v.enabled=true AND v.price_eur>0 ORDER BY v.id''',uid)
    if not chosen:
        await q.answer('Sélectionne au moins un VIP.',show_alert=True)
        return
    lang=await member_language(db,uid)
    text,total=vip_confirm_view(chosen,lang)
    await q.answer()
    keyboard=InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Yes, confirm' if lang=='en' else '✅ Oui, je confirme',
                              callback_data='vip:confirm'),
         InlineKeyboardButton('↩ No' if lang=='en' else '↩ Non',callback_data='vip:catalog')],
        [InlineKeyboardButton('🛟 Payment support' if lang=='en' else '🛟 Aide paiement',
                              callback_data='vip:help')]
    ])
    await show_screen(q,text,keyboard,edit=True)

async def get_invitation_link(ctx,uid,user):
    db=await db_conn(ctx)
    # Never create a private group invite for a non-member, even if they
    # selected that group in the bot's private multi-group menu.
    try:
        membership=await ctx.bot.get_chat_member(current_group(),uid)
        if membership.status in ('left','kicked') or (
                membership.status=='restricted' and not membership.is_member):
            raise ValueError('Tu dois être membre de ce groupe pour obtenir ton lien.')
    except TelegramError:
        raise ValueError('Impossible de vérifier ta présence dans ce groupe.')
    link=await db.fetchval('SELECT invite_link FROM invite_links WHERE owner_id=$1 ORDER BY created_at LIMIT 1',uid)
    if link:return link
    generated=await ctx.bot.create_chat_invite_link(current_group(),name=f'Parrainage {uid}',creates_join_request=False)
    await db.execute('INSERT INTO invite_links(invite_link,owner_id) VALUES($1,$2) ON CONFLICT DO NOTHING',generated.invite_link,uid)
    await db.execute('''INSERT INTO members(user_id,display_name,username) VALUES($1,$2,$3)
                        ON CONFLICT(user_id) DO UPDATE SET display_name=$2,username=$3''',
                     uid,user.full_name,user.username)
    return generated.invite_link

async def send_invite_dashboard(ctx,uid,msg=None,q=None):
    db=await db_conn(ctx)
    try:
        link=await get_invitation_link(ctx,uid,q.from_user if q else msg.from_user)
    except (TelegramError,ValueError):
        await show_screen(q or msg,'⚠️ Tu dois être membre du groupe sélectionné, et le bot doit avoir les droits de créer des invitations.',None,edit=bool(q))
        return
    results=await db.fetchrow('''SELECT count(*) FILTER (WHERE credited=true) AS credited,
                                  count(*) FILTER (WHERE credited=false AND active=true) AS pending,
                                  count(*) AS total FROM referrals WHERE referrer_id=$1''',uid)
    lang=await member_language(db,uid)
    text=invite_dashboard_view(link,results['credited'],results['pending'],results['total'],lang)
    offers=await db.fetch('''SELECT id,title,invite_goal FROM vip
              WHERE enabled=true AND invite_goal>0 ORDER BY invite_goal,id LIMIT 10''')
    if offers:
        text+='\n\n🎯 <b>OBJECTIFS VIP</b>\n'
        for offer in offers:
            achieved,required,ready=goal_progress(results['credited'],offer['invite_goal'])
            text+=(f'\n{"✅" if ready else "🔒"} {escape(offer["title"][:40])}'
                   f' : <b>{achieved}/{required}</b> invitations')
    rows=[
        [InlineKeyboardButton('📋 Copier mon lien',copy_text=CopyTextButton(link)),
         InlineKeyboardButton('🔗 Ouvrir',url=link)],
        [InlineKeyboardButton('🏆 Classement',callback_data='ref:top'),
         InlineKeyboardButton('♻️ Actualiser',callback_data='ref:home')]]
    for offer in offers:
        if results['credited']>=offer['invite_goal']:
            rows.append([InlineKeyboardButton('🎁 Débloquer '+offer['title'][:25],
                                             callback_data=f'vip:claim:{offer["id"]}',style='success')])
    rows.append([InlineKeyboardButton('💎 Catalogue VIP',callback_data='vip:catalog')])
    kb=InlineKeyboardMarkup(rows)
    await show_screen(q or msg,text,kb,edit=bool(q))

async def send_top(ctx,uid,msg=None,q=None):
    db=await db_conn(ctx)
    rows=await db.fetch('''SELECT r.referrer_id, count(*) AS n, m.display_name
                           FROM referrals r LEFT JOIN members m ON m.user_id=r.referrer_id
                           WHERE r.credited=true GROUP BY r.referrer_id,m.display_name
                           ORDER BY n DESC,r.referrer_id LIMIT 10''')
    text=leaderboard_view(rows,await member_language(db,uid))
    kb=InlineKeyboardMarkup([[InlineKeyboardButton('🔗 Mes invitations',callback_data='ref:home')],
                             [InlineKeyboardButton('💎 VIP',callback_data='vip:catalog')]])
    await show_screen(q or msg,text,kb,edit=bool(q))

async def send_payment_methods(ctx,uid,q):
    db=await db_conn(ctx)
    offers=await db.fetch('''SELECT v.* FROM vip_cart c JOIN vip v ON v.id=c.vip_id
                             WHERE c.user_id=$1 AND v.enabled=true AND v.price_eur IS NOT NULL
                             ORDER BY v.id''',uid)
    if not offers:
        await q.answer('Panier vide. Sélectionne une offre.',show_alert=True)
        return
    total=sum((Decimal(str(o['price_eur'])) for o in offers), Decimal('0'))
    lang=await member_language(db,uid)
    choices=[('Crypto','crypto'),('PayPal','paypal'),('Revolut','revolut')]
    available=[InlineKeyboardButton(label,callback_data=f'vip:method:{key}')
               for label,key in choices if PAYMENT_DETAILS[key]]
    rows=[available[:2]] if available else []
    if len(available)>2:rows.append(available[2:])
    rows.append([InlineKeyboardButton('↩ Retour',callback_data='vip:go')])
    kb=InlineKeyboardMarkup(rows)
    await q.answer()
    txt=payment_methods_view(total,lang,[key for label,key in choices if PAYMENT_DETAILS[key]])
    if not available:
        txt+='\n\n⚠️ Aucun mode de règlement n’est configuré. Contacte un administrateur.'
    await show_screen(q,txt,kb,edit=True)

async def create_manual_order(db,uid,method):
    '''Snapshot offers/prices; only one awaiting-proof order per user.'''
    async with db.acquire() as conn:
        async with conn.transaction():
            reviewing=await conn.fetchval("SELECT 1 FROM manual_vip_orders WHERE user_id=$1 AND status='review' LIMIT 1",uid)
            if reviewing:raise ValueError('Une commande est déjà en vérification')
            offers=await conn.fetch('''SELECT v.* FROM vip_cart c JOIN vip v ON v.id=c.vip_id
                                        WHERE c.user_id=$1 AND v.enabled=true AND v.price_eur IS NOT NULL
                                        ORDER BY v.id FOR UPDATE OF c''',uid)
            if not offers:
                return None
            subtotal=sum((Decimal(str(o['price_eur'])) for o in offers), Decimal('0'))
            total=payment_total(subtotal,method)
            if total <= 0 or total > Decimal('100000'):
                raise ValueError('Total invalide')
            order_id=uuid.uuid4().hex
            await conn.execute("UPDATE manual_vip_orders SET status='cancelled' WHERE user_id=$1 AND status='awaiting_proof'",uid)
            await conn.execute('''INSERT INTO manual_vip_orders(id,user_id,base_total_eur,final_total_eur,method)
                                  VALUES($1,$2,$3,$4,$5)''',order_id,uid,subtotal,total,method)
            for offer in offers:
                await conn.execute('''INSERT INTO manual_vip_items(order_id,vip_id,title,chat_id,days,price_eur)
                                      VALUES($1,$2,$3,$4,$5,$6)''',order_id,offer['id'],offer['title'],offer['chat_id'],offer['days'],offer['price_eur'])
            await conn.execute('DELETE FROM vip_cart WHERE user_id=$1',uid)
            return order_id, total

async def send_manual_instructions(ctx,uid,q,method):
    if not PAYMENT_DETAILS.get(method):
        await q.answer('Cette méthode n’est pas encore configurée par l’administrateur.',show_alert=True)
        return
    db=await db_conn(ctx)
    created=await create_manual_order(db,uid,method)
    if not created:
        await q.answer('Panier vide, réessaie avec /vips.',show_alert=True)
        return
    order_id,total=created
    pretty={'crypto':'Crypto','paypal':'PayPal','revolut':'Revolut'}[method]
    txt=(f'<b>Commande VIP — {escape(pretty)}</b>\n\n'
         f'📋 Référence : <code>{order_id}</code>\n'
         f'💶 Montant : <b>{money(total)}</b>\n\n'
         f'{escape(PAYMENT_DETAILS[method])}\n\n'
         'Après le règlement, envoie une photo ou une image du justificatif ici, en privé.\n'
         '<b>Attention :</b> le justificatif ne valide pas automatiquement le paiement. '
         'Un administrateur vérifiera la réception réelle des fonds avant de donner accès.')
    kb=InlineKeyboardMarkup([[InlineKeyboardButton('Annuler la commande',callback_data=f'vip:cancel:{order_id}')],
                             [InlineKeyboardButton('💎 Retour au catalogue',callback_data='vip:catalog')]])
    await q.answer()
    await show_screen(q,txt,kb,edit=True)

async def commerce_callbacks(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    q=update.callback_query
    if not q:return
    if not q.message or q.message.chat.type!='private':
        await q.answer('Ouvre le bot en privé.',show_alert=True)
        return
    if not GROUPS:
        await q.answer('Aucun groupe enregistré.',show_alert=True);return
    uid=q.from_user.id
    db=await db_conn(ctx)
    if not await group_active(db):
        await q.answer('Groupe non activé : /setup dans le groupe.',show_alert=True)
        return
    data=q.data or ''
    if data.startswith('vip:claim:'):
        try:vid=int(data.split(':')[-1])
        except ValueError:await q.answer('Offre invalide.',show_alert=True);return
        await claim_vip_by_invites(ctx,uid,vid,q)
        return
    if data.startswith('vip:toggle:'):
        try:id_=int(data.split(':')[-1])
        except ValueError:
            await q.answer('Choix invalide.');return
        enabled=await db.fetchval('SELECT 1 FROM vip WHERE id=$1 AND enabled=true AND price_eur IS NOT NULL AND price_eur>0',id_)
        if not enabled:
            await q.answer('Cette offre n’est plus disponible.',show_alert=True);return
        exists=await db.fetchval('SELECT 1 FROM vip_cart WHERE user_id=$1 AND vip_id=$2',uid,id_)
        if exists:
            await db.execute('DELETE FROM vip_cart WHERE user_id=$1 AND vip_id=$2',uid,id_)
        else:
            await db.execute('INSERT INTO vip_cart(user_id,vip_id) VALUES($1,$2) ON CONFLICT DO NOTHING',uid,id_)
        await q.answer()
        await send_vip_catalog(ctx,uid,q=q)
        return
    if data=='vip:go':
        await send_vip_confirmation(ctx,uid,q)
        return
    if data=='vip:confirm':
        await send_payment_methods(ctx,uid,q)
        return
    if data.startswith('vip:method:'):
        method=data.removeprefix('vip:method:')
        if method not in PAYMENT_DETAILS:
            await q.answer('Méthode invalide.',show_alert=True)
            return
        try:
            await send_manual_instructions(ctx,uid,q,method)
        except (ValueError,TelegramError):
            log.exception('Manual checkout failed user=%s',uid)
            await q.answer('Commande indisponible.',show_alert=True)
        return
    if data.startswith('vip:cancel:'):
        order_id=data.removeprefix('vip:cancel:')
        result=await db.execute("UPDATE manual_vip_orders SET status='cancelled' WHERE id=$1 AND user_id=$2 AND status='awaiting_proof'",order_id,uid)
        await q.answer('Commande annulée.' if result.endswith('1') else 'Commande déjà en cours de traitement.')
        await send_vip_catalog(ctx,uid,q=q)
        return
    if data=='vip:lang':
        lang='en' if await member_language(db,uid)=='fr' else 'fr'
        await db.execute('INSERT INTO vip_preferences(user_id,lang) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET lang=$2',uid,lang)
        await q.answer()
        await send_vip_catalog(ctx,uid,q=q)
        return
    if data=='vip:help':
        await q.answer('Contacte un administrateur en cas de problème de paiement.',show_alert=True)
        return
    await q.answer()
    if data=='vip:catalog':await send_vip_catalog(ctx,uid,q=q)
    elif data=='ref:home':await send_invite_dashboard(ctx,uid,q=q)
    elif data=='ref:top':await send_top(ctx,uid,q=q)

async def owner_text_input(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    await admin_input(update,ctx)

async def owner_photo_or_proof(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    if await admin_input(update,ctx):return
    await manual_proof(update,ctx)

async def manual_proof(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    """Route payment screenshots to orders in the correct managed group.

    A screenshot is not verification of a successful payment.
    """
    msg=update.effective_message
    if not msg or update.effective_chat.type!='private' or not update.effective_user:return
    doc=msg.document
    if doc and not (doc.mime_type or '').startswith('image/'):
        return
    image=msg.photo[-1] if msg.photo else doc
    if not image:return
    matches=[]
    for gid,pool in tuple(ctx.application.bot_data['pools'].items()):
        pending=await pool.fetchval('''SELECT 1 FROM manual_vip_orders
                WHERE user_id=$1 AND status='awaiting_proof' LIMIT 1''',msg.from_user.id)
        if pending:matches.append((gid,pool))
    if not matches:
        await msg.reply_text('Aucune commande en attente de justificatif. Utilise /vips pour commencer.')
        return
    if len(matches)>1:
        selected=current_group()
        chosen=next(((gid,pool) for gid,pool in matches if gid==selected),None)
        if chosen is None:
            await msg.reply_text('Plusieurs commandes sont en attente dans différents groupes. '
                                 'Choisis le groupe avec /groups puis renvoie ton justificatif.')
            return
    else:
        chosen=matches[0]
    gid,db=chosen
    select_group(gid,GROUPS)
    order=await db.fetchrow('''UPDATE manual_vip_orders SET status='review',proof_file_id=$2,
                              proof_message_id=$3,proof_at=now() WHERE id=(
                              SELECT id FROM manual_vip_orders WHERE user_id=$1 AND status='awaiting_proof'
                              ORDER BY created_at DESC LIMIT 1 FOR UPDATE)
                              RETURNING *''',msg.from_user.id,image.file_id,msg.message_id)
    if not order:
        await msg.reply_text('La commande vient de changer de statut. Réessaie.')
        return
    oid=order['id']
    await msg.reply_text(f'✅ Justificatif reçu pour la commande {oid[:8]}. '
                         'En attente de vérification par un propriétaire.')
    method_name={'crypto':'Crypto','paypal':'PayPal','revolut':'Revolut',
                 'paysafecard':'Paysafecard (historique)',
                 'apple_pay':'Apple Pay (historique)'}.get(order['method'],order['method'])
    caption=(f'🧾 JUSTIFICATIF À VÉRIFIER\nCommande : {oid}\n'
             f'Groupe : {gid}\nUtilisateur : {msg.from_user.id}\nMéthode : {method_name}\n'
             f'Montant déclaré : {money(order["final_total_eur"])}\n'
             '⚠️ Vérifier la réception effective des fonds ; la capture seule ne suffit pas.')
    kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Paiement réellement reçu',callback_data=f'review:{oid}:approve',style='success')],
                             [InlineKeyboardButton('❌ Refuser',callback_data=f'review:{oid}:reject',style='danger')]])
    for owner in OWNERS:
        try:
            await ctx.bot.copy_message(owner,msg.chat_id,msg.message_id)
            await ctx.bot.send_message(owner,caption,reply_markup=kb)
        except TelegramError:
            log.exception('Cannot notify owner %s of manual VIP order %s',owner,oid)


async def decide_manual_order(bot,db,oid,actor,approved):
    '''Single-use manual decision, atomic membership grant on approval.'''
    status='approved' if approved else 'rejected'
    async with db.acquire() as conn:
        async with conn.transaction():
            order=await conn.fetchrow('''UPDATE manual_vip_orders SET status=$2,reviewed_at=now(),reviewed_by=$3
                    WHERE id=$1 AND status='review' AND proof_file_id IS NOT NULL RETURNING *''',oid,status,actor)
            if not order:return False
            if approved:
                items=await conn.fetch('SELECT * FROM manual_vip_items WHERE order_id=$1',oid)
                for item in items:
                    await conn.execute('''INSERT INTO subscriptions(user_id,vip_id,expires_at,charge_id)
                        VALUES($1,$2,CASE WHEN $3::int=0 THEN NULL ELSE now()+($3::int * interval '1 day') END,$4)
                        ON CONFLICT(user_id,vip_id) DO UPDATE SET
                        expires_at=CASE WHEN subscriptions.expires_at IS NULL OR $3::int=0
                            THEN NULL ELSE GREATEST(subscriptions.expires_at,now())+($3::int * interval '1 day') END,
                        charge_id=$4''',order['user_id'],item['vip_id'],item['days'],'manual:'+oid)
                    await conn.execute('UPDATE manual_vip_items SET subscribed_at=now() WHERE order_id=$1 AND vip_id=$2',oid,item['vip_id'])
    try:
        await bot.send_message(order['user_id'],
            ('✅ Paiement vérifié par un administrateur. Tes accès arrivent.' if approved else
             '❌ Justificatif refusé. Contacte /paysupport si tu penses qu’il s’agit d’une erreur.'))
    except TelegramError:
        log.warning('Cannot DM manual payment decision to user %s',order['user_id'])
    if approved:
        await deliver_manual_order(bot,db,oid,order['user_id'])
    return True

async def review_callbacks(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    q=update.callback_query
    if not q:return
    if not is_owner(q.from_user.id):
        await q.answer('Réservé au propriétaire.',show_alert=True)
        return
    match=re.fullmatch(r'review:([0-9a-f]{32}):(approve|reject)',q.data or '')
    if not match:
        await q.answer('Commande invalide.',show_alert=True)
        return
    oid,decision=match.groups()
    # Owner's selected panel group is irrelevant: route by order reference.
    matches=[]
    for gid,pool in tuple(ctx.application.bot_data['pools'].items()):
        if await pool.fetchval('SELECT 1 FROM manual_vip_orders WHERE id=$1',oid):
            matches.append((gid,pool))
    if len(matches)!=1:
        await q.answer('Commande introuvable ou ambiguë.',show_alert=True)
        return
    gid,db=matches[0]
    select_group(gid,GROUPS)
    success=await decide_manual_order(ctx.bot,db,oid,q.from_user.id,decision=='approve')
    await q.answer('Décision enregistrée.' if success else 'Déjà traité ou justificatif absent.',show_alert=True)
    if success:
        await q.edit_message_reply_markup(reply_markup=None)

async def deliver_manual_order(bot,db,oid,uid):
    order=await db.fetchrow('SELECT status FROM manual_vip_orders WHERE id=$1 AND user_id=$2',oid,uid)
    if not order or order['status']!='approved':return
    items=await db.fetch('SELECT * FROM manual_vip_items WHERE order_id=$1 AND delivered_at IS NULL ORDER BY vip_id',oid)
    for item in items:
        try:
            offer=await db.fetchrow('SELECT * FROM vip WHERE id=$1',item['vip_id'])
            if not offer:continue
            link=await issue_secure_vip_link(bot,db,uid,offer)
            await db.execute('UPDATE manual_vip_items SET invite_url=$3,invite_created_at=now() WHERE order_id=$1 AND vip_id=$2',oid,item['vip_id'],link)
            await bot.send_message(uid,f'✅ VIP {item["title"]}\n🔗 Lien personnel valable 24 h (demande d’adhésion vérifiée) :\n{link}')
            await db.execute('UPDATE manual_vip_items SET delivered_at=now() WHERE order_id=$1 AND vip_id=$2',oid,item['vip_id'])
        except TelegramError:
            log.exception('Manual VIP delivery failed order=%s offer=%s',oid,item['vip_id'])
            await notify_owner(bot,f'⚠️ Livraison VIP manuelle à vérifier : commande {oid}.')

async def manual_delivery_retry(ctx):
    db=await db_conn(ctx)
    if not await group_active(db):return
    rows=await db.fetch('''SELECT DISTINCT i.order_id,o.user_id FROM manual_vip_items i
                            JOIN manual_vip_orders o ON o.id=i.order_id
                            WHERE o.status='approved' AND i.delivered_at IS NULL LIMIT 20''')
    for row in rows:
        await deliver_manual_order(ctx.bot,db,row['order_id'],row['user_id'])

async def select_private_group(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    """Choose which of the managed groups the private bot menus control."""
    bind_group(update,ctx)
    if update.effective_chat.type!='private':return
    if not GROUPS:
        await update.effective_message.reply_text('Aucun groupe enregistré. Ajoute le bot comme admin dans un supergroupe et confirme son activation.')
        return
    rows=[]
    for i,gid in enumerate(GROUPS,1):
        rows.append([InlineKeyboardButton(f'🌍 Groupe {i}',callback_data=f'pick:{gid}')])
    await update.effective_message.reply_text('🌍 Choisis un groupe :',reply_markup=InlineKeyboardMarkup(rows))

async def private_group_callback(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q:return
    try:gid=int((q.data or '').split(':')[1])
    except (IndexError,ValueError):gid=0
    if gid not in GROUPS:
        await q.answer('Groupe non autorisé.',show_alert=True);return
    ctx.user_data['guard_selected_group']=gid
    select_group(gid,GROUPS)
    await q.answer('Groupe sélectionné.')
    await q.edit_message_text(f'✅ Groupe {GROUPS.index(gid)+1} sélectionné.')
    await send_vip_catalog(ctx,q.from_user.id,msg=q.message)

async def commands(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    bind_group(update,ctx)
    msg=update.effective_message;uid=update.effective_user.id
    if update.effective_chat.type!='private':return
    if not GROUPS:
        await msg.reply_text('🔐 Aucun groupe enregistré. Ajoute le bot comme administrateur dans un supergroupe puis confirme dans le groupe.')
        return
    db=await db_conn(ctx)
    name=msg.text.split()[0].split('@')[0].lower()
    if name=='/start':
        payload=ctx.args[0] if ctx.args else ''
        routed=parse_vip_payload(payload,GROUPS)
        if routed:
            action,gid=routed
            ctx.user_data['guard_selected_group']=gid
            select_group(gid,GROUPS)
            db=await db_conn(ctx)
            payload=action
        if not await group_active(db):
            await msg.reply_text('🔐 Bot en attente d’activation. Un administrateur du groupe doit lancer /setup et confirmer les droits.')
            return
        if payload=='invite':
            await send_invite_dashboard(ctx,uid,msg)
        elif payload=='vip':
            await send_vip_catalog(ctx,uid,msg)
        else:
            await display_admin(ctx,'home',msg=msg) if is_owner(uid) else await msg.reply_text('Bienvenue ! 💎 /vips pour les offres et 🔗 /invite pour ton lien personnel.')
        return
    if name=='/groups':
        await select_private_group(update,ctx)
        return
    if name=='/help':
        await display_admin(ctx,'home',msg=msg) if is_owner(uid) else await msg.reply_text('💎 /vips : catalogue VIP\n🔗 /invite : mon lien\n🏆 /top : classement\n🛟 /paysupport : assistance')
        return
    if name in ('/invite','/parrainage'):
        if not await group_active(db):
            await msg.reply_text('🔐 Groupe non activé : /setup dans le groupe.')
            return
        await send_invite_dashboard(ctx,uid,msg)
        return
    if name in ('/top','/classement'):
        if not await group_active(db):
            await msg.reply_text('🔐 Groupe non activé : /setup dans le groupe.')
            return
        await send_top(ctx,uid,msg)
        return
    if name in ('/vips','/vip'):
        if not await group_active(db):
            await msg.reply_text('🔐 Groupe non activé : /setup dans le groupe.')
            return
        await send_vip_catalog(ctx,uid,msg)
        return
    if name=='/paysupport':
        await msg.reply_text('🛟 En cas de problème avec une commande VIP, contacte un administrateur et communique l’identifiant de commande (sans mot de passe, code ni informations bancaires).')
        return
    if not await group_active(db):
        await msg.reply_text('🔐 Le groupe doit d’abord être activé par un administrateur avec /setup.')
        return
    if not is_owner(uid):return
    args=ctx.args
    try:
        if name=='/stats':out=await stats_text(ctx,db)
        elif name=='/schedules':
            r=await db.fetch('SELECT * FROM schedules ORDER BY id')
            out='\n'.join(f'{x["id"]}: {x["start_hm"]}–{x["end_hm"]}' for x in r) or 'Aucun créneau.'
        elif name=='/schedule':
            from logic import parse_hm
            parse_hm(args[0]);parse_hm(args[1])
            if args[0]==args[1]:raise ValueError('Heure de début et fin identiques')
            await db.execute('INSERT INTO schedules(start_hm,end_hm) VALUES($1,$2)',args[0],args[1]);out='Créneau ajouté.'
        elif name=='/delschedule':await db.execute('DELETE FROM schedules WHERE id=$1',int(args[0]));out='Créneau effacé.'
        elif name in ('/word','/name'):
            await db.execute(f'INSERT INTO {"banned_words" if name=="/word" else "banned_names"}(pattern) VALUES($1) ON CONFLICT DO NOTHING',' '.join(args));out='Filtre enregistré.'
        elif name in ('/unword','/unname'):
            await db.execute(f'DELETE FROM {"banned_words" if name=="/unword" else "banned_names"} WHERE pattern=$1',' '.join(args));out='Filtre effacé.'
        elif name in ('/trust','/untrust'):
            target=int(args[0])
            if target<=0:raise ValueError('Identifiant Telegram invalide')
            if name=='/trust':
                await db.execute('INSERT INTO trusted(user_id) VALUES($1) ON CONFLICT DO NOTHING',target)
                out=f'✅ {target} ajouté aux trusted.'
            else:
                await db.execute('DELETE FROM trusted WHERE user_id=$1',target)
                out=(f'⚠️ {target} est encore trusted via TRUSTED_USER_IDS sur Railway ; modifie cette variable.'
                     if target in TRUSTED_USER_IDS else f'✅ {target} retiré des trusted en base.')
        elif name=='/trusted':
            rows=await db.fetch('SELECT user_id FROM trusted ORDER BY user_id')
            out=('🔐 Trusted par Railway : '+(', '.join(map(str,sorted(TRUSTED_USER_IDS))) or 'aucun')+
                 '\nTrusted par base : '+(', '.join(str(r['user_id']) for r in rows) or 'aucun'))
        elif name=='/orders':
            rows=await db.fetch("SELECT id,user_id,method,final_total_eur FROM manual_vip_orders WHERE status='review' ORDER BY proof_at LIMIT 20")
            out='\n'.join(f'{o["id"]} · {o["user_id"]} · {o["method"]} · {money(o["final_total_eur"])}' for o in rows) or 'Aucun justificatif en attente.'
        elif name in ('/approve','/reject'):
            oid=args[0].strip().lower()
            if not re.fullmatch(r'[0-9a-f]{32}',oid):raise ValueError('Identifiant de commande invalide')
            success=await decide_manual_order(ctx.bot,db,oid,uid,name=='/approve')
            out='✅ Décision enregistrée.' if success else '⚠️ Déjà traitée ou justificatif manquant.'
        elif name=='/rules':await setcfg(db,'rules',' '.join(args));out='Règles enregistrées.'
        elif name in ('/forward','/nonlatin'):
            if args[0] not in ('on','off'):raise ValueError('on ou off')
            await setcfg(db,'allow_forwards' if name=='/forward' else 'ban_nonlatin',str(args[0]=='on').lower());out='Modifié.'
        elif name in ('/open','/close','/resume'):
            if name=='/open':
                await setcfg(db,'manual_closed_until','');await setcfg(db,'manual_until',(utcnow()+timedelta(hours=2)).isoformat());await setcfg(db,'manual_warned','false')
            elif name=='/close':
                await setcfg(db,'manual_until','')
                n=next_boundary(utcnow(),await get_rows(db),'open',TZ)
                await setcfg(db,'manual_closed_until',(n.astimezone(timezone.utc) if n else utcnow()+timedelta(hours=2)).isoformat())
            else:
                await setcfg(db,'manual_until','');await setcfg(db,'manual_closed_until','')
            await tick(ctx);out='État actualisé.'
        elif name=='/vipad' and args and args[0] in ('on','off'):
            await db.execute('UPDATE posts SET enabled=$1 WHERE kind=$2',args[0]=='on','vip')
            out='Publicité VIP '+('activée.' if args[0]=='on' else 'désactivée.')
        elif name in ('/post','/invitead','/vipad'):
            interval=int(args[0])
            if name=='/vipad' and interval not in (120,180,300,600):
                raise ValueError('VIP : 120, 180, 300 ou 600 minutes')
            if name!='/vipad' and interval not in (30,120,180,360):
                raise ValueError('Pub : 30, 120, 180 ou 360 minutes')
            body=' '.join(args[1:]).strip()
            if not body:raise ValueError('Texte de publication obligatoire')
            photo=msg.reply_to_message.photo[-1].file_id if msg.reply_to_message and msg.reply_to_message.photo else None
            await db.execute('INSERT INTO posts(kind,body,photo_file_id,interval_minutes) VALUES($1,$2,$3,$4)','invite' if name=='/invitead' else 'vip' if name=='/vipad' else 'ad',body,photo,interval)
            out='Publicité enregistrée.'
        elif name=='/posts':
            r=await db.fetch('SELECT id,body,interval_minutes FROM posts')
            out='\n'.join(f'{x["id"]} ({x["interval_minutes"]}min): {x["body"][:60]}' for x in r) or 'Aucune pub.'
        elif name=='/delpost':
            r=await db.fetchrow('DELETE FROM posts WHERE id=$1 RETURNING last_message_id',int(args[0]))
            if r and r['last_message_id']:await quiet_delete(ctx.bot,current_group(),r['last_message_id'])
            out='Pub supprimée.'
        elif name=='/vipadd':
            prefix,desc=msg.text.split('|',1)
            tokens=prefix.split(maxsplit=4)
            chat_id=int(tokens[1]);price=Decimal(tokens[2].replace(',','.'));days=int(tokens[3]);title=tokens[4].strip()
            if not title or price<=0 or price>100000 or price.as_tuple().exponent<-2 or days<0:raise ValueError('Prix EUR positif à 2 décimales maximum, durée >=0')
            botmember=await ctx.bot.get_chat_member(chat_id,ctx.bot.id)
            if botmember.status not in ('administrator','creator'):raise ValueError('Bot non admin du VIP')
            await db.execute('INSERT INTO vip(chat_id,title,description,price_stars,price_eur,days) VALUES($1,$2,$3,$4,$5,$6)',chat_id,title,desc.strip(),0,price,days)
            out=f'✅ VIP ajouté : {title} — {money(price)}.'
        elif name=='/vipprice':
            vipid=int(args[0]);price=Decimal(args[1].replace(',','.'))
            if price<=0 or price>100000 or price.as_tuple().exponent<-2:raise ValueError('Prix EUR invalide')
            updated=await db.execute('UPDATE vip SET price_eur=$2 WHERE id=$1',vipid,price)
            out='✅ Prix modifié.' if updated.endswith('1') else '⚠️ Offre introuvable.'
        elif name=='/vipoff':await db.execute('UPDATE vip SET enabled=false WHERE id=$1',int(args[0]));out='VIP désactivé.'
        else:out='Commande inconnue. /help'
    except (IndexError,ValueError,InvalidOperation,AssertionError,TelegramError) as e:
        out=f'Paramètres incorrects : {e}. Voir /help.'
    await msg.reply_text(out)

async def vip_expiry(ctx):
    db=await db_conn(ctx)
    reminders=await db.fetch("""SELECT s.user_id,s.vip_id,s.expires_at,v.days,v.title
         FROM subscriptions s JOIN vip v ON v.id=s.vip_id
         WHERE s.expires_at>now()
           AND s.expires_at<=now()+(CASE WHEN v.days=1 THEN interval '1 hour'
                                     ELSE interval '24 hours' END)
         AND NOT EXISTS(SELECT 1 FROM vip_expiry_notices n WHERE n.user_id=s.user_id
              AND n.vip_id=s.vip_id AND n.expires_at=s.expires_at)
         ORDER BY s.expires_at LIMIT 100""")
    for item in reminders:
        inserted=await db.fetchval("""INSERT INTO vip_expiry_notices(user_id,vip_id,expires_at)
           VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING user_id""",
           item['user_id'],item['vip_id'],item['expires_at'])
        if not inserted:continue
        remaining='1 heure' if item['days']==1 else '24 heures'
        try:
            await ctx.bot.send_message(item['user_id'],f'⏳ Ton accès {item["title"]} expire dans moins de {remaining}.')
        except TelegramError:pass
    expired=await db.fetch('SELECT s.user_id,s.vip_id,v.chat_id FROM subscriptions s JOIN vip v ON v.id=s.vip_id WHERE s.expires_at<=now() LIMIT 100')
    for r in expired:
        other=await db.fetchval('''SELECT 1 FROM subscriptions s JOIN vip v ON v.id=s.vip_id
              WHERE s.user_id=$1 AND v.chat_id=$2 AND s.vip_id<>$3 AND
              (s.expires_at IS NULL OR s.expires_at>now()) LIMIT 1''',r['user_id'],r['chat_id'],r['vip_id'])
        if other:
            await db.execute('DELETE FROM subscriptions WHERE user_id=$1 AND vip_id=$2 AND expires_at<=now()',r['user_id'],r['vip_id'])
            continue
        try:
            await ctx.bot.ban_chat_member(r['chat_id'],r['user_id'])
            await ctx.bot.unban_chat_member(r['chat_id'],r['user_id'],only_if_banned=True)
        except TelegramError:
            log.exception('Cannot revoke expired VIP user %s; will retry later',r['user_id'])
            continue
        await db.execute('DELETE FROM subscriptions WHERE user_id=$1 AND vip_id=$2 AND expires_at<=now()',r['user_id'],r['vip_id'])
        try:
            await ctx.bot.send_message(r['user_id'],'Ton accès VIP a expiré. Tu peux renouveler avec /vips.')
        except TelegramError:pass

async def on_error(update,ctx):log.exception('Unhandled Telegram handler error',exc_info=ctx.error)

async def is_registered_vip_chat(ctx,chat_id):
    for db in ctx.application.bot_data['pools'].values():
        if await db.fetchval('SELECT 1 FROM vip_registered_chats WHERE chat_id=$1',chat_id):
            return True
    return False

async def draw_raffle(db, actor_id):
    """Auditable, uniform draw: one ticket per eligible referrer. No winner auto-award."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(85511001)')
            minimum=int((await conn.fetchval("SELECT value FROM config WHERE key='raffle_min_credits'")) or '1')
            rows=await conn.fetch("""SELECT referrer_id AS user_id,count(*)::int AS n
              FROM referrals r JOIN members m ON m.user_id=r.referrer_id
          WHERE r.credited=true AND m.left_at IS NULL AND m.joined_at IS NOT NULL
          GROUP BY referrer_id HAVING count(*) >= $1
              ORDER BY referrer_id LIMIT 10000""",minimum)
            if not rows:return None
            selected=rows[secrets.randbelow(len(rows))]
            await conn.execute("""INSERT INTO raffle_draws(drawn_by,winner_id,validated_invites,pool_size)
              VALUES($1,$2,$3,$4)""",actor_id,selected['user_id'],selected['n'],len(rows))
            return selected

async def issue_secure_vip_link(bot,db,uid,offer):
    """Create a per-user join-request link; never trust the URL by itself."""
    existing=await db.fetchrow("""SELECT invite_link FROM vip_access_links
        WHERE user_id=$1 AND vip_id=$2 AND expires_at>now()
          AND revoked_at IS NULL AND used_at IS NULL ORDER BY created_at DESC LIMIT 1""",uid,offer['id'])
    if existing:return existing['invite_link']
    link=await bot.create_chat_invite_link(
        offer['chat_id'],name=f'Acces VIP {uid}'[:32],
        expire_date=utcnow()+timedelta(hours=24),creates_join_request=True)
    await db.execute("""INSERT INTO vip_access_links(invite_link,user_id,vip_id,expires_at)
        VALUES($1,$2,$3,now()+interval '24 hours') ON CONFLICT DO NOTHING""",
        link.invite_link,uid,offer['id'])
    return link.invite_link

async def claim_vip_by_invites(ctx,uid,vid,q):
    db=await db_conn(ctx)
    offer=await db.fetchrow('SELECT * FROM vip WHERE id=$1 AND enabled=true',vid)
    if not offer or not offer['invite_goal']:
        await q.answer('Accès par invitations non disponible.',show_alert=True);return
    try:
        status=await ctx.bot.get_chat_member(current_group(),uid)
        present=status.status not in ('left','kicked') and (status.status!='restricted' or status.is_member)
    except TelegramError:
        present=False
    n=await db.fetchval('SELECT count(*) FROM referrals WHERE referrer_id=$1 AND credited=true',uid)
    if not present:
        await q.answer('Tu dois être membre du groupe principal pour débloquer cette offre.',show_alert=True);return
    if n<offer['invite_goal']:
        await q.answer(f'Encore {offer["invite_goal"]-n} invitation(s) validée(s) nécessaires.',show_alert=True);return
    async with db.acquire() as conn:
        async with conn.transaction():
            claimed=await conn.fetchval("""INSERT INTO vip_invite_claims(user_id,vip_id)
                VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING user_id""",uid,vid)
            if claimed:
                # A reward is claimable ONCE, so repeated button clicks cannot
                # perpetually extend a 24-hour invitation-earned pass.
                await conn.execute("""INSERT INTO subscriptions(user_id,vip_id,expires_at,charge_id)
                  VALUES($1,$2,CASE WHEN $3::int=0 THEN NULL ELSE now()+($3::int*interval '1 day') END,$4)
                  ON CONFLICT(user_id,vip_id) DO UPDATE SET
                  expires_at=CASE WHEN subscriptions.expires_at IS NULL OR $3::int=0
                    THEN NULL ELSE GREATEST(subscriptions.expires_at,EXCLUDED.expires_at) END""",
                  uid,vid,offer['days'],f'invites:{current_group()}:{uid}:{vid}')
    has_sub=await db.fetchval('''SELECT 1 FROM subscriptions WHERE user_id=$1 AND vip_id=$2
            AND (expires_at IS NULL OR expires_at>now())''',uid,vid)
    if not has_sub:
        await q.answer('Ce bonus a déjà été utilisé et a expiré.',show_alert=True)
        return
    try:
        url=await issue_secure_vip_link(ctx.bot,db,uid,offer)
    except TelegramError:
        log.exception('Cannot create VIP join-request link %s',vid)
        await q.answer('Objectif atteint. Livraison en attente : réessaie ou contacte un administrateur.',show_alert=True)
        return
    await q.answer('🎉 Accès débloqué !')
    await show_screen(q,
       f'✅ <b>Objectif atteint !</b>\n\n'
       f'💎 {escape(offer["title"])}\n'
       f'🎯 {n} invitations validées\n'
       f'⏳ Accès : {"à vie" if offer["days"]==0 else str(offer["days"])+" jour(s)"}\n\n'
       'Ton lien privé ouvre une demande que le bot validera uniquement pour ton compte Telegram.',
       InlineKeyboardMarkup([[InlineKeyboardButton('🔗 Demander mon accès',url=url)],
                             [InlineKeyboardButton('↩ Retour aux VIP',callback_data='vip:catalog')]]),edit=True)

async def vip_join_request(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    """Approve only the intended Telegram account from an active subscription."""
    request=update.chat_join_request
    if not request or not request.invite_link:return
    link=request.invite_link.invite_link
    for gid,pool in ctx.application.bot_data['pools'].items():
        record=await pool.fetchrow("""SELECT a.user_id,a.vip_id,a.used_at,a.expires_at,a.revoked_at,
                 v.chat_id,s.expires_at AS subscription_expires
                 FROM vip_access_links a JOIN vip v ON v.id=a.vip_id
                 LEFT JOIN subscriptions s ON s.vip_id=a.vip_id AND s.user_id=a.user_id
                 WHERE a.invite_link=$1""",link)
        if not record:continue
        authorized=(request.from_user.id==record['user_id'] and request.chat.id==record['chat_id']
                    and record['revoked_at'] is None and record['used_at'] is None
                    and record['expires_at']>utcnow() and
                    (record['subscription_expires'] is None or record['subscription_expires']>utcnow()))
        # NULL means lifetime only if the subscription row exists.
        exists=await pool.fetchval('SELECT 1 FROM subscriptions WHERE user_id=$1 AND vip_id=$2',
                                    record['user_id'],record['vip_id'])
        authorized=authorized and bool(exists)
        try:
            if authorized:
                await ctx.bot.approve_chat_join_request(request.chat.id,request.from_user.id)
                await pool.execute('UPDATE vip_access_links SET used_at=now() WHERE invite_link=$1',link)
                try:await ctx.bot.revoke_chat_invite_link(request.chat.id,link)
                except TelegramError:pass
            else:
                await ctx.bot.decline_chat_join_request(request.chat.id,request.from_user.id)
        except TelegramError:
            log.exception('VIP join request processing failed chat=%s member=%s',request.chat.id,request.from_user.id)
        return

async def _run_for_all_groups(ctx, operation):
    """One Telegram polling process; each job tick uses its own group DB."""
    for gid in GROUPS:
        token=select_group(gid,GROUPS)
        try:
            await operation(ctx)
        except Exception:
            log.exception('Background job failed for group %s',gid)
        finally:
            from multigroup import reset_group
            reset_group(token)

async def check_vip_always_open(ctx):
    db=await db_conn(ctx)
    if not await group_active(db):return
    last=await cfg(db,'vip247_last_check')
    if last:
        try:
            if (utcnow()-datetime.fromisoformat(last)).total_seconds()<3600:return
        except ValueError:pass
    for offer in await db.fetch('SELECT id,chat_id FROM vip WHERE enabled=true AND always_open=true'):
        if offer['chat_id'] in GROUPS:
            log.error('VIP 24/24 %s also in managed group list; skipped',offer['chat_id'])
            continue
        try:
            member=await ctx.bot.get_chat_member(offer['chat_id'],ctx.bot.id)
            if member.status!='administrator' or not getattr(member,'can_restrict_members',False):
                log.error('VIP 24/24 insufficient rights group=%s',offer['chat_id'])
                continue
            await ctx.bot.set_chat_permissions(offer['chat_id'],OPEN_PERMS,use_independent_chat_permissions=True)
        except TelegramError:
            log.exception('VIP 24/24 rights check failed for %s',offer['chat_id'])
    await setcfg(db,'vip247_last_check',utcnow().isoformat())

async def group_tick(ctx):
    await tick(ctx)
    await check_vip_always_open(ctx)

async def periodic_tick(ctx):
    await _run_for_all_groups(ctx,group_tick)

async def periodic_expiry(ctx):
    await _run_for_all_groups(ctx,vip_expiry)

async def periodic_delivery(ctx):
    await _run_for_all_groups(ctx,manual_delivery_retry)

async def start_app():
    """One worker for many groups, discovered from Telegram updates.

    The public legacy schema is assigned using the durable primary-group id.
    Legacy GROUP_IDS is optional and only used to bootstrap V7 migrations.
    """
    registry=await asyncpg.create_pool(DB,min_size=1,max_size=2)
    async with registry.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS public.guard_registry (
            registry_key text PRIMARY KEY, first_group_id bigint NOT NULL)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS public.guard_groups (
            chat_id bigint PRIMARY KEY, title text NOT NULL,
            registered_at timestamptz NOT NULL DEFAULT now())''')
        existing=await conn.fetchval('''SELECT first_group_id FROM public.guard_registry
                        WHERE registry_key='primary_group' ''')
        if existing is not None and LEGACY_GROUP_IDS and int(existing)!=LEGACY_GROUP_IDS[0]:
            raise RuntimeError('Migration V7 : le premier GROUP_IDS ne correspond pas au groupe historique. '
                               'Corrige cette variable ou supprime-la : le registre PostgreSQL fait foi.')
        if existing is None and LEGACY_GROUP_IDS:
            await conn.execute('''INSERT INTO public.guard_registry(registry_key,first_group_id)
                           VALUES('primary_group',$1) ON CONFLICT DO NOTHING''',LEGACY_GROUP_IDS[0])
        for gid in LEGACY_GROUP_IDS:
            await conn.execute('''INSERT INTO public.guard_groups(chat_id,title)
                          VALUES($1,$2) ON CONFLICT DO NOTHING''',gid,f'Groupe migré {gid}')
        existing=await conn.fetchval('''SELECT first_group_id FROM public.guard_registry
                                WHERE registry_key='primary_group' ''')
        # V7 additional groups have numeric guard_<id> schemas. Recover them
        # from PostgreSQL even when GROUP_IDS has already been removed.
        old_schemas=await conn.fetch('''SELECT n.nspname FROM pg_namespace n
                WHERE n.nspname ~ '^guard_[0-9]{5,20}$'
                  AND EXISTS (SELECT 1 FROM information_schema.tables t
                    WHERE t.table_schema=n.nspname AND t.table_name='config')''')
        for row in old_schemas:
            gid=-int(row['nspname'].removeprefix('guard_'))
            await conn.execute('''INSERT INTO public.guard_groups(chat_id,title)
                VALUES($1,$2) ON CONFLICT DO NOTHING''',gid,f'Groupe récupéré {gid}')
        if existing is not None:
            await conn.execute('''INSERT INTO public.guard_groups(chat_id,title)
                      VALUES($1,$2) ON CONFLICT DO NOTHING''',existing,f'Groupe historique {existing}')
        entries=await conn.fetch('''SELECT chat_id,title FROM public.guard_groups
                   ORDER BY CASE WHEN chat_id=$1 THEN 0 ELSE 1 END,registered_at,chat_id''',existing)
    with open(os.path.join(os.path.dirname(__file__),'schema.sql'),encoding='utf-8') as f:
        schema_sql=f.read()
    app=Application.builder().token(TOKEN).build()
    app.bot_data['registry_pool']=registry
    app.bot_data['registration_lock']=asyncio.Lock()
    app.bot_data['schema_sql']=schema_sql
    app.bot_data['pools']={}
    GROUPS.clear()  # DB registry is authoritative after one-time migration.
    for row in entries:
        await register_group(app,int(row['chat_id']),row['title'])
    app.add_handler(CommandHandler('setup',group_setup_command))
    app.add_handler(CommandHandler('panel',panel))
    app.add_handler(CommandHandler('groups',select_private_group))
    app.add_handler(CommandHandler(['start','help','invite','parrainage','top','classement','vips','vip','paysupport'],commands))
    app.add_handler(CallbackQueryHandler(activation_callback,pattern=r'^onb:'))
    app.add_handler(CallbackQueryHandler(private_group_callback,pattern=r'^pick:'))
    app.add_handler(CallbackQueryHandler(commerce_callbacks,pattern=r'^(vip|ref):'))
    app.add_handler(CallbackQueryHandler(review_callbacks,pattern=r'^review:'))
    app.add_handler(CallbackQueryHandler(admin_callbacks,pattern=r'^adm:'))
    app.add_handler(CallbackQueryHandler(legacy_admin_callback,pattern=r'^(open|close|auto_on|auto_off|fw_on|fw_off|stats|panel)$'))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,owner_text_input))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE),owner_photo_or_proof))
    app.add_handler(ChatMemberHandler(bot_membership_update,ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(join_update,ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(vip_join_request))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,on_message))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(periodic_tick,60,first=3,name='tick_all')
    app.job_queue.run_repeating(periodic_expiry,60,first=60,name='vip_expiry_all')
    app.job_queue.run_repeating(periodic_delivery,300,first=90,name='delivery_all')
    try:
        async with app:
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False)
            log.info('Bot running (auto-discovery enabled; groups=%s)',GROUPS)
            try:await asyncio.Event().wait()
            finally:
                await app.updater.stop()
                await app.stop()
    finally:
        for pool in tuple(app.bot_data['pools'].values()):await pool.close()
        await registry.close()

if __name__=='__main__':asyncio.run(start_app())
