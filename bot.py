import os, re, asyncio, tempfile, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import asyncpg
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ChatMemberHandler, ContextTypes, filters
try:
    import cv2
except Exception:
    cv2 = None

APP_VERSION='FINAL_CLEAN_V16_CLOSED_LOCKDOWN_REFERRAL_COUNTER'
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip(); DATABASE_URL=os.getenv('DATABASE_URL','').strip()
GROUP_ID=int(os.getenv('GROUP_ID','0') or '0'); BOT_USERNAME=os.getenv('BOT_USERNAME','').strip().lstrip('@')
TZ=ZoneInfo(os.getenv('TZ','Europe/Paris'))
ADMIN_IDS=[int(x) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()]
TRUSTED_IDS=[int(x) for x in os.getenv('TRUSTED_IDS','').split(',') if x.strip()]
SUPER_TRUSTED_IDS=[int(x) for x in os.getenv('SUPER_TRUSTED_IDS','').split(',') if x.strip()]
REDIFFUSION_GROUP_ID=int(os.getenv('REDIFFUSION_GROUP_ID','0') or '0')
MAX_HASH_DOWNLOAD_BYTES=int(os.getenv('MAX_HASH_DOWNLOAD_BYTES',str(20*1024*1024)))
GROUP_ANONYMOUS_BOT_ID=1087968824
db_pool=None

MSG_PARTICIPATION_REQUIRED='⚠️ {mention}, merci de participer avant d’envoyer un message.'
MSG_REPOST='♻️ Ce média a déjà été publié.'; MSG_LINK_FORBIDDEN='🔗 Les liens ne sont pas autorisés.'
MSG_FORWARD_FORBIDDEN='🚫 Les transferts texte ne sont pas autorisés.'; MSG_GENERIC_FORBIDDEN='🚫 Message non autorisé.'
MSG_FAKE_COMMAND='🔇 Commande réservée à la modération. Si vous essayez, vous êtes sanctionné.'
MSG_PASFR='⚠️ Merci d’envoyer uniquement du contenu FR.'; MSG_PUB_ATTEMPT='🚫 Tentative de publicité interdite.'

DEFAULT_SETTINGS={'participation':'on','silent_sanctions':'on','rediffusion_enabled':'off','leaderboard_enabled':'on','nonparticipant_enabled':'on','nonparticipant_threshold_open_days':'3','auto_schedule_enabled':'off','schedule_json':'{"0":[["22:00","00:00"]],"1":[["22:00","00:00"]],"2":[["22:00","00:00"]],"3":[["22:00","00:00"]],"4":[["22:00","00:00"]],"5":[["23:00","01:00"]],"6":[["22:30","00:15"]]}','session_status_message_id':'','session_status_chat_id':'','last_midscan_key':'','anti_repost_enabled':'on','auto_reminders_sent':'{}','share_pub_text':'🤝 Partagez le groupe pour monter dans le classement.\nCliquez ci-dessous pour recevoir votre lien personnel.','share_pub_photo_file_id':'','rule1_text':'','rule1_photo_file_id':'','rule2_text':'','rule2_photo_file_id':'','rules_posted_session_id':'','rules_message_ids':'[]','nonparticipant_kick_active_session_id':''}
TABLES=[
"CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)",
"CREATE TABLE IF NOT EXISTS admin_states(user_id BIGINT PRIMARY KEY,state TEXT,payload TEXT,updated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS sessions(id SERIAL PRIMARY KEY,opened_at TIMESTAMP,closed_at TIMESTAMP,is_open BOOLEAN DEFAULT FALSE,session_deletions INTEGER DEFAULT 0,session_exclusions INTEGER DEFAULT 0,session_mutes INTEGER DEFAULT 0)",
"CREATE TABLE IF NOT EXISTS messages(chat_id BIGINT,message_id BIGINT,user_id BIGINT,media_group_id TEXT,is_system BOOLEAN DEFAULT FALSE,created_at TIMESTAMP DEFAULT NOW(),PRIMARY KEY(chat_id,message_id))",
"CREATE TABLE IF NOT EXISTS participants(user_id BIGINT PRIMARY KEY,username TEXT,first_name TEXT,media_hash TEXT,created_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS pending_joins(user_id BIGINT PRIMARY KEY,referrer_id BIGINT,invite_link TEXT,created_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS banned_words(word TEXT PRIMARY KEY,created_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS banned_words_hard(word TEXT PRIMARY KEY,created_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS forbidden_usernames(pattern TEXT PRIMARY KEY,created_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS banned_hashes(hash TEXT PRIMARY KEY,created_at TIMESTAMP DEFAULT NOW(),added_by BIGINT)",
"CREATE TABLE IF NOT EXISTS media_hashes(hash TEXT PRIMARY KEY,user_id BIGINT,chat_id BIGINT,message_id BIGINT,media_type TEXT,created_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS media_fingerprints(hash TEXT,user_id BIGINT,chat_id BIGINT,message_id BIGINT,media_type TEXT,created_at TIMESTAMP DEFAULT NOW(),PRIMARY KEY(hash,message_id))",
"CREATE TABLE IF NOT EXISTS trusted_actions(id SERIAL PRIMARY KEY,session_id INTEGER,trusted_id BIGINT,action TEXT,target_user_id BIGINT,target_message_id BIGINT,created_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS trusted_strikes(user_id BIGINT PRIMARY KEY,count INTEGER DEFAULT 0,updated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS user_violations(user_id BIGINT,violation_type TEXT DEFAULT 'general',count INTEGER DEFAULT 0,updated_at TIMESTAMP DEFAULT NOW(),PRIMARY KEY(user_id,violation_type))",
"CREATE TABLE IF NOT EXISTS restricted_users(user_id BIGINT PRIMARY KEY,reason TEXT,restricted_until TIMESTAMP,created_at TIMESTAMP DEFAULT NOW(),updated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS danger_scores(user_id BIGINT PRIMARY KEY,score INTEGER DEFAULT 0,reason TEXT,updated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS referral_links(user_id BIGINT PRIMARY KEY,invite_link TEXT UNIQUE,username TEXT,first_name TEXT,created_at TIMESTAMP DEFAULT NOW(),revoked BOOLEAN DEFAULT FALSE)",
"CREATE TABLE IF NOT EXISTS referrals(id SERIAL PRIMARY KEY,referrer_id BIGINT,referred_id BIGINT UNIQUE,valid BOOLEAN DEFAULT TRUE,invalid_reason TEXT,created_at TIMESTAMP DEFAULT NOW(),validated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS referrer_abuse(referrer_id BIGINT PRIMARY KEY,bad_invites INTEGER DEFAULT 0,warned BOOLEAN DEFAULT FALSE,blacklisted BOOLEAN DEFAULT FALSE,updated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS private_users(user_id BIGINT PRIMARY KEY,username TEXT,first_name TEXT,last_name TEXT,created_at TIMESTAMP DEFAULT NOW(),updated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS leaderboard_rank_cache(user_id BIGINT PRIMARY KEY,rank INTEGER,count INTEGER,updated_at TIMESTAMP DEFAULT NOW())",
"CREATE TABLE IF NOT EXISTS system_messages(chat_id BIGINT,message_id BIGINT,created_at TIMESTAMP DEFAULT NOW(),PRIMARY KEY(chat_id,message_id))",
"CREATE TABLE IF NOT EXISTS nonparticipant_seen(user_id BIGINT,session_id INTEGER,username TEXT,first_name TEXT,last_seen_at TIMESTAMP DEFAULT NOW(),PRIMARY KEY(user_id,session_id))",
"CREATE TABLE IF NOT EXISTS nonparticipant_kick_messages(chat_id BIGINT,message_id BIGINT,session_id INTEGER,created_at TIMESTAMP DEFAULT NOW(),PRIMARY KEY(chat_id,message_id))"]

def is_admin(uid:int)->bool: return uid in ADMIN_IDS
def is_super_trusted(uid:int)->bool: return uid in SUPER_TRUSTED_IDS or is_admin(uid)
def is_trusted_or_super(uid:int)->bool: return uid in TRUSTED_IDS or uid in SUPER_TRUSTED_IDS or is_admin(uid)
def is_protected_user(uid:int)->bool: return is_admin(uid) or uid in TRUSTED_IDS or uid in SUPER_TRUSTED_IDS or uid==GROUP_ANONYMOUS_BOT_ID
def is_system_or_anonymous_user(user)->bool:
    return (not user) or getattr(user,'id',None)==GROUP_ANONYMOUS_BOT_ID or (getattr(user,'is_bot',False) and getattr(user,'username',None)=='GroupAnonymousBot')
def contains_forbidden_token(text:str,pattern:str)->bool:
    if not text or not pattern: return False
    pattern=pattern.lower().strip(); text=text.lower()
    return bool(pattern and re.search(rf'(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])',text,re.I))
def display(user):
    if not user: return 'Utilisateur inconnu'
    if getattr(user,'username',None): return '@'+user.username
    return (' '.join(x for x in [getattr(user,'first_name',None),getattr(user,'last_name',None)] if x)).strip() or 'Utilisateur inconnu'
def masked(name:str)->str:
    if not name: return 'Utilisateur'
    if name.startswith('@'): name=name[1:]; return '@'+(name[:2]+'**'+name[-1:] if len(name)>2 else '**')
    return name[:2]+'**' if len(name)>2 else '**'
def msg_text(msg): return (getattr(msg,'text',None) or getattr(msg,'caption',None) or '').strip()
def has_media(msg): return bool(getattr(msg,'photo',None) or getattr(msg,'video',None))
def media_type(msg): return 'photo' if getattr(msg,'photo',None) else ('video' if getattr(msg,'video',None) else 'other')
def is_forwarded(msg): return bool(getattr(msg,'forward_origin',None) or getattr(msg,'forward_from',None) or getattr(msg,'forward_from_chat',None) or getattr(msg,'forward_date',None))
def has_external_link(msg):
    text=msg_text(msg)
    if re.search(r'(https?://|t\.me/|telegram\.me/|www\.)',text,re.I): return True
    ents=(getattr(msg,'entities',None) or [])+(getattr(msg,'caption_entities',None) or [])
    return any(getattr(e,'type',None) in ('url','text_link') for e in ents)
def is_live_or_story(msg): return bool(getattr(msg,'video_chat_started',None) or getattr(msg,'video_chat_scheduled',None) or getattr(msg,'story',None))

def plain_name(user):
    return '@'+user.username if getattr(user,'username',None) else (getattr(user,'first_name',None) or 'Utilisateur')

async def init_db():
    global db_pool
    if not DATABASE_URL: raise RuntimeError('DATABASE_URL manquant')
    db_pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=5)
    async with db_pool.acquire() as con:
        for q in TABLES: await con.execute(q)
        await con.execute('ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS session_id INTEGER')
        await con.execute('ALTER TABLE IF EXISTS sessions ADD COLUMN IF NOT EXISTS status_message_id BIGINT')
        await con.execute('ALTER TABLE IF EXISTS sessions ADD COLUMN IF NOT EXISTS status_chat_id BIGINT')
        await con.execute("ALTER TABLE IF EXISTS user_violations ADD COLUMN IF NOT EXISTS violation_type TEXT DEFAULT 'general'")
        await con.execute("ALTER TABLE IF EXISTS user_violations ADD COLUMN IF NOT EXISTS count INTEGER DEFAULT 0")
        await con.execute("ALTER TABLE IF EXISTS user_violations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()")
        await con.execute("ALTER TABLE IF EXISTS user_violations ADD COLUMN IF NOT EXISTS user_id BIGINT")
        try: await con.execute('CREATE UNIQUE INDEX IF NOT EXISTS user_violations_user_type_idx ON user_violations(user_id,violation_type)')
        except Exception as e: print(f'USER_VIOLATIONS INDEX SKIPPED: {e}',flush=True)
        for k,v in DEFAULT_SETTINGS.items(): await con.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING',k,v)
        rows=await con.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        print('TABLES:',[r['tablename'] for r in rows],flush=True)
async def get_setting(k,d=''):
    async with db_pool.acquire() as con: v=await con.fetchval('SELECT value FROM settings WHERE key=$1',k)
    return v if v is not None else d
async def set_setting(k,v):
    async with db_pool.acquire() as con: await con.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2',k,v)
async def visible(): return await get_setting('silent_sanctions','on')=='on'
async def set_state(uid,state,payload=None):
    async with db_pool.acquire() as con:
        if state is None: await con.execute('DELETE FROM admin_states WHERE user_id=$1',uid)
        else: await con.execute('INSERT INTO admin_states(user_id,state,payload,updated_at) VALUES($1,$2,$3,NOW()) ON CONFLICT(user_id) DO UPDATE SET state=$2,payload=$3,updated_at=NOW()',uid,state,payload)
async def get_state(uid):
    async with db_pool.acquire() as con: return await con.fetchrow('SELECT state,payload FROM admin_states WHERE user_id=$1',uid)
async def save_message(chat_id,msg_id,uid,is_system=False,media_group_id=None):
    sid=None
    try:
        sess=await get_open_session()
        sid=int(sess['id']) if sess else None
    except Exception:
        sid=None
    async with db_pool.acquire() as con:
        await con.execute('INSERT INTO messages(chat_id,message_id,user_id,media_group_id,is_system,created_at,session_id) VALUES($1,$2,$3,$4,$5,NOW(),$6) ON CONFLICT(chat_id,message_id) DO UPDATE SET session_id=COALESCE(messages.session_id,$6)',chat_id,msg_id,uid,media_group_id,is_system,sid)

async def current_session():
    async with db_pool.acquire() as con:
        sid=await con.fetchval('SELECT id FROM sessions WHERE is_open=TRUE ORDER BY id DESC LIMIT 1')
        if sid: return sid
        sid=await con.fetchval('INSERT INTO sessions(opened_at,is_open) VALUES(NOW(),TRUE) RETURNING id'); print(f'SESSION OPEN {sid}',flush=True); return sid
async def inc_counter(field):
    if field not in {'session_deletions','session_exclusions','session_mutes'}: return
    sid=await current_session()
    async with db_pool.acquire() as con: await con.execute(f'UPDATE sessions SET {field}={field}+1 WHERE id=$1',sid)
async def add_danger(uid,score,reason):
    async with db_pool.acquire() as con: await con.execute('INSERT INTO danger_scores(user_id,score,reason,updated_at) VALUES($1,$2,$3,NOW()) ON CONFLICT(user_id) DO UPDATE SET score=danger_scores.score+$2,reason=$3,updated_at=NOW()',uid,score,reason)
async def violation(uid,typ):
    async with db_pool.acquire() as con: v=await con.fetchval('INSERT INTO user_violations(user_id,violation_type,count,updated_at) VALUES($1,$2,1,NOW()) ON CONFLICT(user_id,violation_type) DO UPDATE SET count=user_violations.count+1,updated_at=NOW() RETURNING count',uid,typ)
    return int(v or 1)
async def has_participated(uid):
    async with db_pool.acquire() as con: return bool(await con.fetchval('SELECT 1 FROM participants WHERE user_id=$1',uid))
async def mark_participated(user,h):
    async with db_pool.acquire() as con: await con.execute('INSERT INTO participants(user_id,username,first_name,media_hash,created_at) VALUES($1,$2,$3,$4,NOW()) ON CONFLICT(user_id) DO NOTHING',user.id,getattr(user,'username',None),getattr(user,'first_name',None),h)

async def delete_safe(ctx,chat_id,msg_id):
    try: await ctx.bot.delete_message(chat_id,msg_id)
    except BadRequest as e: print(f'DELETE SKIPPED/CLEANED {msg_id}: {e}',flush=True)
    except Exception as e: print(f'DELETE ERROR {msg_id}: {e}',flush=True)
async def delete_later(ctx,chat_id,msg_id,seconds): await asyncio.sleep(seconds); await delete_safe(ctx,chat_id,msg_id)
async def warning(ctx,text,seconds=30,force=False):
    if not force and not await visible(): return None
    try:
        m=await ctx.bot.send_message(GROUP_ID,text); await save_message(GROUP_ID,m.message_id,None,True); ctx.application.create_task(delete_later(ctx,GROUP_ID,m.message_id,seconds)); return m
    except Exception as e: print(f'WARNING SEND ERROR: {e}',flush=True); return None
async def participation_warning(ctx,user):
    await warning(ctx,MSG_PARTICIPATION_REQUIRED.format(mention=display(user)),30,True); print(f'PARTICIPATION WARNING SENT user={user.id}',flush=True)
async def send_always_warning(ctx, text: str, seconds: int = 30):
    try:
        msg = await ctx.bot.send_message(GROUP_ID, text)
        await save_message(GROUP_ID, msg.message_id, None, True)
        ctx.application.create_task(delete_later(ctx, GROUP_ID, msg.message_id, seconds))
        return msg
    except Exception as e:
        print(f'ALWAYS WARNING ERROR: {e}', flush=True)
        return None


async def notify_admins(ctx,text):
    for uid in set(ADMIN_IDS)|set(SUPER_TRUSTED_IDS):
        try: await ctx.bot.send_message(uid,text)
        except Exception as e: print(f'ADMIN ALERT SEND ERROR user={uid}: {e}',flush=True)
async def alert_ban(ctx,user,reason,detected=None):
    det=f'\nÉlément détecté : {detected}' if detected else ''
    await notify_admins(ctx,f'🚨 Ban automatique\n\nMotif : {reason}\nUtilisateur : {display(user)}{det}\nAction : ban direct')

async def unban_later(ctx, user_id:int, seconds:int):
    await asyncio.sleep(max(1, seconds))
    try:
        await ctx.bot.unban_chat_member(GROUP_ID, user_id, only_if_banned=True)
        print(f'RESTRICTION VISIBILITY RESTORE user={user_id}', flush=True)
    except Exception as e:
        print(f'RESTRICTION VISIBILITY RESTORE ERROR user={user_id}: {e}', flush=True)


async def restrict_days(ctx, uid, days, reason):
    until = datetime.now(TZ) + timedelta(days=days)

    try:
        await ctx.bot.restrict_chat_member(
            GROUP_ID,
            uid,
            ChatPermissions(can_send_messages=False),
            until_date=until
        )
    except Exception as e:
        print(f'RESTRICT ERROR user={uid}: {e}', flush=True)

    try:
        await ctx.bot.ban_chat_member(GROUP_ID, uid, until_date=until)
        seconds = int((until - datetime.now(TZ)).total_seconds())
        ctx.application.create_task(unban_later(ctx, uid, seconds))
        print(f'RESTRICTION VISIBILITY BAN user={uid} days={days} reason={reason}', flush=True)
    except Exception as e:
        print(f'RESTRICTION VISIBILITY BAN ERROR user={uid}: {e}', flush=True)

    try:
        async with db_pool.acquire() as con:
            await con.execute("""
                INSERT INTO restricted_users(user_id,reason,restricted_until,created_at,updated_at)
                VALUES($1,$2,$3,NOW(),NOW())
                ON CONFLICT(user_id) DO UPDATE SET reason=$2,restricted_until=$3,updated_at=NOW()
            """, uid, reason, until.replace(tzinfo=None))
    except Exception as e:
        print(f'RESTRICT DB ERROR user={uid}: {e}', flush=True)


async def purge_user(ctx,uid):
    async with db_pool.acquire() as con: rows=await con.fetch('SELECT chat_id,message_id FROM messages WHERE user_id=$1 AND is_system=FALSE ORDER BY created_at DESC LIMIT 500',uid)
    c=0
    for r in rows: await delete_safe(ctx,r['chat_id'],r['message_id']); c+=1; await asyncio.sleep(.01)
    return c
async def punish_ban(update,ctx,reason,public_msg=MSG_GENERIC_FORBIDDEN):
    user=update.effective_user; msg=update.effective_message
    if not user or not msg: return
    if is_protected_user(user.id): await delete_safe(ctx,GROUP_ID,msg.message_id); print(f'PROTECTED AUTO BAN SKIPPED user={user.id} reason={reason}',flush=True); return
    try: await ctx.bot.ban_chat_member(GROUP_ID,user.id); await inc_counter('session_exclusions'); print(f'AUTO BAN user={user.id} reason={reason}',flush=True)
    except Exception as e: print(f'BAN ERROR user={user.id}: {e}',flush=True)
    await purge_user(ctx,user.id); await delete_safe(ctx,GROUP_ID,msg.message_id); await add_danger(user.id,10,reason); await warning(ctx,public_msg,30)
async def punish_word(update,ctx,word):
    user=update.effective_user; msg=update.effective_message
    if not user or not msg or is_protected_user(user.id): return
    await delete_safe(ctx,GROUP_ID,msg.message_id); days=1 if await has_participated(user.id) else 3
    try: await restrict_days(ctx,user.id,days,f'mot interdit:{word}'); await inc_counter('session_mutes'); print(f'WORD FORBIDDEN MATCH user={user.id} word={word} mute_days={days}',flush=True)
    except Exception as e: print(f'WORD MUTE ERROR user={user.id}: {e}',flush=True)
    await add_danger(user.id,3,f'mot interdit:{word}'); await warning(ctx,MSG_GENERIC_FORBIDDEN,30)
async def fake_command(update,ctx):
    user=update.effective_user; msg=update.effective_message
    if not user or not msg: return
    await delete_safe(ctx,GROUP_ID,msg.message_id)
    if is_protected_user(user.id): return
    try: await restrict_days(ctx,user.id,2,'fake command'); await inc_counter('session_mutes')
    except Exception as e: print(f'FAKE COMMAND MUTE ERROR user={user.id}: {e}',flush=True)
    await warning(ctx,MSG_FAKE_COMMAND,30,True)


# sessions / nonparticipants / info
async def get_open_session():
    async with db_pool.acquire() as con:
        return await con.fetchrow('SELECT * FROM sessions WHERE is_open=TRUE ORDER BY id DESC LIMIT 1')
async def send_or_edit_session_status(ctx, sid:int, text:str):
    mid_raw = await get_setting('session_status_message_id','')
    chat_raw = await get_setting('session_status_chat_id','')
    mid = int(mid_raw) if str(mid_raw).isdigit() else None
    chat_id = int(chat_raw) if str(chat_raw).lstrip('-').isdigit() else GROUP_ID
    if mid:
        try:
            await ctx.bot.edit_message_text(chat_id=chat_id, message_id=mid, text=text)
            print(f'SESSION GLOBAL STATUS EDIT sid={sid} msg={mid}', flush=True)
            return mid
        except Exception as e:
            print(f'SESSION GLOBAL STATUS EDIT ERROR sid={sid} msg={mid}: {e}', flush=True)
            # If Telegram cannot edit old/deleted message, create a new global one.
    msg = await ctx.bot.send_message(GROUP_ID, text)
    await save_message(GROUP_ID, msg.message_id, None, True)
    await set_setting('session_status_message_id', str(msg.message_id))
    await set_setting('session_status_chat_id', str(GROUP_ID))
    async with db_pool.acquire() as con:
        await con.execute('UPDATE sessions SET status_message_id=$2,status_chat_id=$3 WHERE id=$1', sid, msg.message_id, GROUP_ID)
    print(f'SESSION GLOBAL STATUS SEND sid={sid} msg={msg.message_id}', flush=True)
    return msg.message_id

async def purge_session_messages(ctx,sid:int):
    global_mid_raw = await get_setting('session_status_message_id','')
    global_mid = int(global_mid_raw) if str(global_mid_raw).isdigit() else None
    async with db_pool.acquire() as con:
        status_mid=await con.fetchval('SELECT status_message_id FROM sessions WHERE id=$1',sid)
        rows=await con.fetch('SELECT chat_id,message_id,is_system FROM messages WHERE session_id=$1 ORDER BY created_at ASC',sid)
    total=0
    print(f'SESSION DELETE START sid={sid} candidates={len(rows)}',flush=True)
    for r in rows:
        mid = int(r['message_id'])
        if (status_mid and mid==int(status_mid)) or (global_mid and mid==global_mid):
            continue
        await delete_safe(ctx,r['chat_id'],r['message_id'])
        total+=1
        await asyncio.sleep(0.015)
    print(f'SESSION DELETE END sid={sid} total={total}',flush=True)
    return total

async def send_super_trusted_report(ctx, title: str):
    if not SUPER_TRUSTED_IDS:
        return
    try:
        stats = await build_trusted_stats_text(ctx, None)
    except Exception:
        stats = "Aucune statistique disponible."
    text = f"{title}\n\n{stats}"
    for uid in SUPER_TRUSTED_IDS:
        try:
            await ctx.bot.send_message(uid, text)
        except Exception as e:
            print(f"SUPER TRUSTED REPORT SEND ERROR user={uid}: {e}", flush=True)


async def open_session_admin(ctx=None):
    async with db_pool.acquire() as con:
        row=await con.fetchrow('SELECT id FROM sessions WHERE is_open=TRUE ORDER BY id DESC LIMIT 1')
        if row:
            sid=int(row['id'])
        else:
            sid=int(await con.fetchval('INSERT INTO sessions(opened_at,is_open,session_deletions,session_exclusions,session_mutes) VALUES(NOW(),TRUE,0,0,0) RETURNING id'))
    print(f'SESSION OPEN #{sid}',flush=True)
    if ctx:
        try:
            await send_or_edit_session_status(ctx,sid,'🟢 Session ouverte\n\nBienvenue à tous. La participation est obligatoire pendant cette session.')
        except Exception as e:
            print(f'SESSION PUBLIC OPEN STATUS ERROR #{sid}: {e}',flush=True)
        try:
            await send_super_trusted_report(ctx,f'🟢 Session ouverte #{sid}')
        except Exception as e:
            print(f'SUPER TRUSTED OPEN REPORT ERROR: {e}',flush=True)
    return int(sid)

async def close_session_admin(ctx=None):
    async with db_pool.acquire() as con:
        row=await con.fetchrow('SELECT id FROM sessions WHERE is_open=TRUE ORDER BY id DESC LIMIT 1')
        if not row:
            return None
        sid=int(row['id'])
    print(f'SESSION CLOSE #{sid}',flush=True)
    deleted=0
    if ctx:
        try:
            deleted=await purge_session_messages(ctx,sid)
        except Exception as e:
            print(f'SESSION PURGE ERROR #{sid}: {e}',flush=True)
        try:
            await cleanup_nonparticipant_kick_messages(ctx,sid)
            await cleanup_rules_messages(ctx,sid)
        except Exception as e:
            print(f'NONPARTICIPANT CLEANUP CLOSE ERROR: {e}',flush=True)
        try:
            await send_or_edit_session_status(ctx,sid,'🔴 Session fermée\n\nMerci à tous les participants.')
        except Exception as e:
            print(f'SESSION PUBLIC CLOSE STATUS ERROR #{sid}: {e}',flush=True)
        try:
            await send_super_trusted_report(ctx,f'🔴 Session fermée #{sid}')
        except Exception as e:
            print(f'SUPER TRUSTED CLOSE REPORT ERROR: {e}',flush=True)
    async with db_pool.acquire() as con:
        await con.execute('UPDATE sessions SET is_open=FALSE,closed_at=NOW() WHERE id=$1',sid)
    return sid

async def track_open_session_presence(user):
    if not user or is_system_or_anonymous_user(user) or is_protected_user(user.id): return
    sess=await get_open_session()
    if not sess: return
    async with db_pool.acquire() as con:
        await con.execute('INSERT INTO nonparticipant_seen(user_id,session_id,username,first_name,last_seen_at) VALUES($1,$2,$3,$4,NOW()) ON CONFLICT(user_id,session_id) DO UPDATE SET username=$3,first_name=$4,last_seen_at=NOW()',user.id,int(sess['id']),getattr(user,'username',None),getattr(user,'first_name',None))
async def eligible_nonparticipants(limit=None):
    th=int(await get_setting('nonparticipant_threshold_open_days','3') or '3')
    sql="""SELECT ns.user_id,MAX(ns.username) username,MAX(ns.first_name) first_name,COUNT(DISTINCT ns.session_id) open_days,MIN(ns.session_id) first_session FROM nonparticipant_seen ns LEFT JOIN participants p ON p.user_id=ns.user_id WHERE p.user_id IS NULL GROUP BY ns.user_id HAVING COUNT(DISTINCT ns.session_id)>=$1 ORDER BY MIN(ns.session_id),ns.user_id"""
    if limit: sql+=f' LIMIT {int(limit)}'
    async with db_pool.acquire() as con: rows=await con.fetch(sql,th)
    print(f'NON_PARTICIPANT_SCAN threshold={th} eligible={len(rows)}',flush=True); return rows
async def count_nonparticipant_seen():
    async with db_pool.acquire() as con: return int(await con.fetchval('SELECT COUNT(DISTINCT user_id) FROM nonparticipant_seen') or 0)
async def auto_schedule_status_text():
    return '🟢 ON' if await get_setting('auto_schedule_enabled','off')=='on' else '🔴 OFF'


def parse_hhmm(value):
    h,m=value.split(':')
    return int(h),int(m)


def dt_for_day_and_hhmm(base,hhmm):
    h,m=parse_hhmm(hhmm)
    return base.replace(hour=h,minute=m,second=0,microsecond=0)


async def get_today_schedule_window():
    raw=await get_setting('schedule_json','{}')
    try:
        data=json.loads(raw or '{}')
    except Exception:
        data={}
    now=datetime.now(TZ)
    candidates=[]
    for offset in (-1,0):
        day=now.date()+timedelta(days=offset)
        wd=str(day.weekday())
        for pair in data.get(wd,[]):
            if not isinstance(pair,list) or len(pair)!=2:
                continue
            start=dt_for_day_and_hhmm(datetime.combine(day,datetime.min.time(),tzinfo=TZ),pair[0])
            end=dt_for_day_and_hhmm(datetime.combine(day,datetime.min.time(),tzinfo=TZ),pair[1])
            if end<=start:
                end+=timedelta(days=1)
            candidates.append((start,end))
    for start,end in candidates:
        if start<=now<=end:
            return start,end
    future=[]
    for add in range(0,8):
        day=now.date()+timedelta(days=add)
        wd=str(day.weekday())
        for pair in data.get(wd,[]):
            start=dt_for_day_and_hhmm(datetime.combine(day,datetime.min.time(),tzinfo=TZ),pair[0])
            end=dt_for_day_and_hhmm(datetime.combine(day,datetime.min.time(),tzinfo=TZ),pair[1])
            if end<=start:
                end+=timedelta(days=1)
            if start>=now:
                future.append((start,end))
    if future:
        return min(future,key=lambda x:x[0])
    return None


async def auto_reminder_seen(key: str) -> bool:
    raw = await get_setting('auto_reminders_sent','{}')
    try:
        data = json.loads(raw or '{}')
    except Exception:
        data = {}
    return bool(data.get(key))


async def mark_auto_reminder_seen(key: str):
    raw = await get_setting('auto_reminders_sent','{}')
    try:
        data = json.loads(raw or '{}')
    except Exception:
        data = {}
    data[key] = True
    if len(data) > 500:
        data = dict(list(data.items())[-300:])
    await set_setting('auto_reminders_sent', json.dumps(data))


async def send_auto_reminder_once(ctx, key: str, text: str):
    if await auto_reminder_seen(key):
        return
    await mark_auto_reminder_seen(key)
    try:
        msg = await ctx.bot.send_message(GROUP_ID, text)
        await save_message(GROUP_ID, msg.message_id, None, True)
        ctx.application.create_task(delete_later(ctx, GROUP_ID, msg.message_id, 30))
        print(f'AUTO REMINDER SENT key={key}', flush=True)
    except Exception as e:
        print(f'AUTO REMINDER ERROR key={key}: {e}', flush=True)


def reminder_minute_bucket(delta_seconds: float):
    mins = int(delta_seconds // 60)
    if mins > 60 and mins % 60 == 0:
        return ('hour', mins // 60)
    if mins in (30,10,5,4,3,2,1):
        return ('minute', mins)
    return None


async def handle_opening_reminders(ctx, start):
    now = datetime.now(TZ)
    delta = (start-now).total_seconds()
    if delta <= 0:
        return
    bucket = reminder_minute_bucket(delta)
    if not bucket:
        mins=int(delta//60)
        print(f'AUTO OPENING REMINDER CHECK no_send opening_in_min={mins}', flush=True)
        return
    typ, val = bucket
    key = f'open:{start.isoformat()}:{typ}:{val}'
    text = f'🕒 Le groupe ouvre dans {val} h.' if typ == 'hour' else f'🕒 Le groupe ouvre dans {val} min.'
    await send_auto_reminder_once(ctx, key, text)


async def handle_closing_reminders(ctx, end):
    now = datetime.now(TZ)
    delta = (end-now).total_seconds()
    if delta <= 0:
        return
    mins = int(delta // 60)
    if mins not in (30,15,5,4,3,2,1):
        return
    key = f'close:{end.isoformat()}:minute:{mins}'
    await send_auto_reminder_once(ctx, key, f'⏳ Le groupe ferme dans {mins} min.')


async def maybe_mid_session_nonparticipant_prompt(ctx, start, end):
    if await get_setting('nonparticipant_enabled','on')!='on':
        return
    now=datetime.now(TZ)
    duration=(end-start).total_seconds()
    if duration <= 0:
        return
    middle=start + timedelta(seconds=duration/2)
    # Trigger within the minute after midpoint.
    if not (middle <= now < middle + timedelta(seconds=70)):
        return
    key=f"{start.isoformat()}_{end.isoformat()}"
    if await get_setting('last_midscan_key','')==key:
        return
    await set_setting('last_midscan_key',key)
    rows=await eligible_nonparticipants()
    print(f'NON_PARTICIPANT MIDSCAN eligible={len(rows)} key={key}',flush=True)
    for admin_id in ADMIN_IDS:
        try:
            await send_nonparticipant_prompt(ctx, admin_id)
        except Exception as e:
            print(f'NON_PARTICIPANT MIDSCAN PROMPT ERROR admin={admin_id}: {e}',flush=True)


async def auto_schedule_tick(ctx):
    if await get_setting('auto_schedule_enabled','off')!='on':
        return
    window=await get_today_schedule_window()
    if not window:
        print('AUTO SCHEDULE TICK: ON but no schedule_json configured',flush=True)
        return
    start,end=window
    now=datetime.now(TZ)
    print(f'AUTO SCHEDULE DEBUG now={now.isoformat()} start={start.isoformat()} end={end.isoformat()} auto=on', flush=True)
    sess=await get_open_session()

    if now < start:
        await handle_opening_reminders(ctx, start)
        mins=int((start-now).total_seconds()//60)
        print(f'AUTO SCHEDULE COUNTDOWN opening_in_min={mins}',flush=True)
        return

    if start<=now<=end:
        await handle_closing_reminders(ctx, end)
        await maybe_mid_session_nonparticipant_prompt(ctx,start,end)
        if not sess:
            print(f'AUTO SCHEDULE OPEN start={start} end={end}',flush=True)
            sid = await open_session_admin(ctx)
            await maybe_publish_auto_rules(ctx, sid, start, end)
        return

    if now>end and sess:
        print(f'AUTO SCHEDULE CLOSE end={end}',flush=True)
        await close_session_admin(ctx)
        return

async def auto_schedule_tick(ctx):
    if await get_setting('auto_schedule_enabled','off')!='on':
        return
    print('AUTO SCHEDULE TICK: ON but schedule hours not configured yet',flush=True)

async def send_nonparticipant_prompt(ctx,admin_id):
    rows=await eligible_nonparticipants(); await set_state(admin_id,'nonparticipant_kick_count')
    await ctx.bot.send_message(admin_id,f'📊 Non-participants détectés\n\n{len(rows)} comptes éligibles.\n\nCe bouton sert à forcer maintenant le scan non-participants. En automatique, le bot vous contacte au milieu de la session.\n\nCombien souhaitez-vous expulser ?\nRépondez par un chiffre. Si vous ne répondez rien, aucune action.')
async def kick_nonparticipants_public(ctx,count):
    rows=await eligible_nonparticipants(count); sess=await get_open_session(); sid=int(sess['id']) if sess else None; kicked=0
    if sid:
        await set_setting('nonparticipant_kick_active_session_id', str(sid))
    for r in rows:
        uid=int(r['user_id'])
        if is_protected_user(uid): continue
        name='@'+r['username'] if r['username'] else (r['first_name'] or 'Utilisateur')
        try:
            m=await ctx.bot.send_message(GROUP_ID,f'🚪 Expulsion pour non participation\n\nUtilisateur : {name}')
            await save_message(GROUP_ID,m.message_id,None,True)
            if sid:
                async with db_pool.acquire() as con: await con.execute('INSERT INTO nonparticipant_kick_messages(chat_id,message_id,session_id,created_at) VALUES($1,$2,$3,NOW()) ON CONFLICT(chat_id,message_id) DO NOTHING',GROUP_ID,m.message_id,sid)
            await ctx.bot.ban_chat_member(GROUP_ID,uid); await asyncio.sleep(.2); await ctx.bot.unban_chat_member(GROUP_ID,uid,only_if_banned=True)
            kicked+=1; print(f'NON_PARTICIPANT_KICKED user={uid}',flush=True); await asyncio.sleep(.4)
        except Exception as e: print(f'NON_PARTICIPANT_KICK ERROR user={uid}: {e}',flush=True)
    try:
        m=await ctx.bot.send_message(GROUP_ID,'⚠️ La participation est obligatoire.\n\nSi vous ne voulez pas être expulsé prochainement, merci de participer.')
        await save_message(GROUP_ID,m.message_id,None,True)
        if sid:
            async with db_pool.acquire() as con: await con.execute('INSERT INTO nonparticipant_kick_messages(chat_id,message_id,session_id,created_at) VALUES($1,$2,$3,NOW()) ON CONFLICT(chat_id,message_id) DO NOTHING',GROUP_ID,m.message_id,sid)
    except Exception as e: print(f'NON_PARTICIPANT FINAL WARNING ERROR: {e}',flush=True)
    await set_setting('nonparticipant_kick_active_session_id','')
    return kicked
async def cleanup_nonparticipant_kick_messages(ctx, sid:int):
    async with db_pool.acquire() as con:
        rows = await con.fetch('SELECT chat_id,message_id FROM nonparticipant_kick_messages WHERE session_id=$1', sid)
    total = 0
    for r in rows:
        await delete_safe(ctx, r['chat_id'], r['message_id'])
        total += 1
        await asyncio.sleep(0.02)
    async with db_pool.acquire() as con:
        await con.execute('DELETE FROM nonparticipant_kick_messages WHERE session_id=$1', sid)
    await set_setting('nonparticipant_kick_active_session_id','')
    print(f'NONPARTICIPANT KICK CLEANUP sid={sid} total={total}', flush=True)


async def build_system_info(ctx):
    lines=['ℹ️ Info système','',f'Version : {APP_VERSION}']
    try:
        async with db_pool.acquire() as con:
            await con.fetchval('SELECT 1')
            tables=await con.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            lines+=['Database : ✅ OK',f'Tables : {len(tables)}']
            for t in ['banned_words','banned_words_hard','forbidden_usernames','banned_hashes','media_hashes','participants','private_users','nonparticipant_seen']:
                try:
                    c=int(await con.fetchval(f'SELECT COUNT(*) FROM {t}') or 0)
                except Exception:
                    c=-1
                lines.append(f'{t} : {c}')
    except Exception as e:
        lines.append(f'Database : ❌ ERROR {e}')
    try:
        me=await ctx.bot.get_me()
        m=await ctx.bot.get_chat_member(GROUP_ID,me.id)
        lines.append(f'Groupe principal : ✅ {m.status}')
    except Exception as e:
        lines.append(f'Groupe principal : ❌ {e}')
    if REDIFFUSION_GROUP_ID:
        ok,msg=await validate_rediff(ctx)
        lines.append(f"Rediffusion : {'✅' if ok else '❌'} {msg}")
    else:
        lines.append('Rediffusion : ⚪ non configurée')
    sess=await get_open_session()
    st='🟢 ouverte #'+str(sess['id']) if sess else '🔴 fermée'
    lines.append(f'Session : {st}')
    lines.append(f'Ouverture auto : {await auto_schedule_status_text()}')
    lines.append(f'Anti-repost : {await get_setting("anti_repost_enabled","on")}')
    lines.append(f'Horaires JSON : {await get_setting("schedule_json","{}")}')
    try:
        lines.append(f'Non-participants suivis : {await count_nonparticipant_seen()}')
        lines.append(f'Éligibles expulsion : {len(await eligible_nonparticipants())}')
    except Exception as e:
        lines.append(f'Non-participants : ❌ {e}')
    return '\n'.join(lines)

async def run_admin_autotest(ctx):
    tests=[]
    def add(n,ok,d=''): tests.append((n,bool(ok),d))
    try:
        async with db_pool.acquire() as con:
            await con.fetchval('SELECT 1'); existing={r['tablename'] for r in await con.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        add('Database',True)
        for t in ['settings','sessions','messages','participants','banned_words','banned_words_hard','forbidden_usernames','media_hashes','banned_hashes','nonparticipant_seen']: add('Table '+t,t in existing)
    except Exception as e: add('Database',False,str(e))
    try:
        me=await ctx.bot.get_me(); m=await ctx.bot.get_chat_member(GROUP_ID,me.id); add('Bot admin groupe',m.status in ('administrator','creator'),m.status)
    except Exception as e: add('Bot admin groupe',False,str(e))
    if REDIFFUSION_GROUP_ID:
        ok,msg=await validate_rediff(ctx); add('Rediffusion',ok,msg)
    else: add('Rediffusion',True,'non configurée')
    add('Token snap',contains_forbidden_token('tu as son snap','snap')); add('Pas Mathias/hi',not contains_forbidden_token('Mathias','hi')); add('Admins commandes trusted',all(is_trusted_or_super(x) for x in ADMIN_IDS) if ADMIN_IDS else True)
    okc=sum(1 for _,ok,_ in tests if ok); bad=sum(1 for _,ok,_ in tests if not ok)
    return '\n'.join(['🧪 Auto-test','',f'🟢 OK : {okc}',f'🔴 Erreurs : {bad}','']+[f"{'✅' if ok else '❌'} {n}"+(f' — {d}' if d else '') for n,ok,d in tests])

# hash

def ahash(img):
    img=img.convert('L').resize((8,8)); px=list(img.getdata()); avg=sum(px)/len(px); bits=''.join('1' if p>avg else '0' for p in px); return hex(int(bits,2))[2:].zfill(16)
def img_hash(data):
    try:
        from io import BytesIO
        return 'imgahash:'+ahash(Image.open(BytesIO(data)))
    except Exception as e: print(f'IMAGE HASH ERROR: {e}',flush=True); return None
def vid_hashes(data):
    if cv2 is None: print('VIDEO HASH SKIPPED: cv2 unavailable',flush=True); return []
    path=None; out=[]
    try:
        f=tempfile.NamedTemporaryFile(suffix='.mp4',delete=False); f.write(data); f.close(); path=f.name
        cap=cv2.VideoCapture(path); ok,frame=cap.read()
        if ok and frame is not None: out.append('vidfirst:'+ahash(Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))))
        total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total>1:
            cap.set(cv2.CAP_PROP_POS_FRAMES,max(0,total-1)); ok,frame=cap.read()
            if ok and frame is not None: out.append('vidlast:'+ahash(Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))))
        cap.release()
    except Exception as e: print(f'VIDEO HASH ERROR: {e}',flush=True)
    finally:
        if path:
            try: os.remove(path)
            except Exception: pass
    return list(dict.fromkeys(out))
async def media_keys(ctx,msg):
    if getattr(msg,'photo',None): o=msg.photo[-1]; file_id=o.file_id; uniq=o.file_unique_id; size=getattr(o,'file_size',None); typ='photo'
    elif getattr(msg,'video',None): o=msg.video; file_id=o.file_id; uniq=o.file_unique_id; size=getattr(o,'file_size',None); typ='video'
    else: return [],False
    keys=[f'file:{uniq}'] if uniq else []
    if size and size>MAX_HASH_DOWNLOAD_BYTES: print('HASH SKIPPED: file too big',flush=True); return keys,False
    try: data=bytes(await (await ctx.bot.get_file(file_id)).download_as_bytearray())
    except Exception as e: print(f'HASH DOWNLOAD ERROR: {e}',flush=True); return keys,False
    if typ=='photo': h=img_hash(data); keys=([h] if h else [])+keys; return list(dict.fromkeys(keys)),bool(h)
    hs=vid_hashes(data); keys=hs+keys; return list(dict.fromkeys(keys)),bool(hs)
async def any_banned(keys):
    if not keys: return None
    async with db_pool.acquire() as con: r=await con.fetchrow('SELECT hash FROM banned_hashes WHERE hash=ANY($1::text[]) LIMIT 1',keys)
    return r['hash'] if r else None
async def any_existing(keys):
    if not keys: return None
    async with db_pool.acquire() as con: r=await con.fetchrow('SELECT hash FROM media_hashes WHERE hash=ANY($1::text[]) LIMIT 1',keys)
    return r['hash'] if r else None
async def record_media(keys,uid,chat_id,msg_id,typ):
    async with db_pool.acquire() as con:
        for h in keys:
            await con.execute('INSERT INTO media_hashes(hash,user_id,chat_id,message_id,media_type,created_at) VALUES($1,$2,$3,$4,$5,NOW()) ON CONFLICT(hash) DO NOTHING',h,uid,chat_id,msg_id,typ)
            await con.execute('INSERT INTO media_fingerprints(hash,user_id,chat_id,message_id,media_type,created_at) VALUES($1,$2,$3,$4,$5,NOW()) ON CONFLICT(hash,message_id) DO NOTHING',h,uid,chat_id,msg_id,typ)
async def ban_hashes_from_user(uid,actor):
    async with db_pool.acquire() as con:
        rows=await con.fetch('SELECT DISTINCT hash FROM media_fingerprints WHERE user_id=$1',uid); c=0
        for r in rows: await con.execute('INSERT INTO banned_hashes(hash,created_at,added_by) VALUES($1,NOW(),$2) ON CONFLICT(hash) DO NOTHING',r['hash'],actor); c+=1
    return c

# trusted commands
async def log_trusted(actor,action,target,msgid):
    sid=await current_session()
    async with db_pool.acquire() as con: await con.execute('INSERT INTO trusted_actions(session_id,trusted_id,action,target_user_id,target_message_id,created_at) VALUES($1,$2,$3,$4,$5,NOW())',sid,actor,action,target,msgid)
    print(f'TRUSTED ACTION actor={actor} action={action} target={target}',flush=True)
async def require_trusted(update,ctx):
    u=update.effective_user
    if u and is_trusted_or_super(u.id): return True
    await fake_command(update,ctx); return False
async def delete_block(ctx,msg):
    if not msg: return 0
    if getattr(msg,'media_group_id',None):
        async with db_pool.acquire() as con: rows=await con.fetch('SELECT chat_id,message_id FROM messages WHERE media_group_id=$1',msg.media_group_id)
        for r in rows: await delete_safe(ctx,r['chat_id'],r['message_id'])
        return len(rows)
    await delete_safe(ctx,msg.chat_id,msg.message_id); return 1
async def trusted_supprime(update,ctx):
    if not await require_trusted(update,ctx): return
    msg=update.message; actor=update.effective_user; target=msg.reply_to_message
    if not target or not target.from_user: await delete_safe(ctx,GROUP_ID,msg.message_id); return
    await delete_block(ctx,target); await delete_safe(ctx,GROUP_ID,msg.message_id); await inc_counter('session_deletions'); await log_trusted(actor.id,'supprime',target.from_user.id,target.message_id)
    if not is_protected_user(target.from_user.id):
        c=await violation(target.from_user.id,'trusted_supprime')
        if c>=2: await restrict_days(ctx,target.from_user.id,c-1,'trusted_supprime'); await inc_counter('session_mutes')
async def trusted_pasfr(update,ctx):
    if not await require_trusted(update,ctx): return
    msg=update.message; actor=update.effective_user; target=msg.reply_to_message
    if not target or not target.from_user: await delete_safe(ctx,GROUP_ID,msg.message_id); return
    await delete_block(ctx,target); await delete_safe(ctx,GROUP_ID,msg.message_id); await inc_counter('session_deletions'); await log_trusted(actor.id,'pasfr',target.from_user.id,target.message_id); await warning(ctx,MSG_PASFR,30,True)
async def trusted_ban(update,ctx):
    if not await require_trusted(update,ctx): return
    msg=update.message; actor=update.effective_user; target=msg.reply_to_message
    if not target or not target.from_user: await delete_safe(ctx,GROUP_ID,msg.message_id); return
    u=target.from_user; await delete_safe(ctx,GROUP_ID,msg.message_id)
    if is_protected_user(u.id): await delete_block(ctx,target); return
    await log_trusted(actor.id,'ban',u.id,target.message_id); await purge_user(ctx,u.id)
    try: await ctx.bot.ban_chat_member(GROUP_ID,u.id); await inc_counter('session_exclusions')
    except Exception as e: print(f'TRUSTED BAN ERROR user={u.id}: {e}',flush=True)
async def trusted_pedo(update,ctx):
    if not await require_trusted(update,ctx): return
    msg=update.message; actor=update.effective_user; target=msg.reply_to_message
    if not target or not target.from_user: await delete_safe(ctx,GROUP_ID,msg.message_id); return
    u=target.from_user; await delete_safe(ctx,GROUP_ID,msg.message_id)
    if is_protected_user(u.id): await delete_block(ctx,target); return
    await log_trusted(actor.id,'pedo',u.id,target.message_id); c=await ban_hashes_from_user(u.id,actor.id); await purge_user(ctx,u.id)
    try: await ctx.bot.ban_chat_member(GROUP_ID,u.id); await inc_counter('session_exclusions')
    except Exception as e: print(f'TRUSTED PEDO ERROR user={u.id}: {e}',flush=True)
    print(f'TRUSTED PEDO banned_hashes={c}',flush=True)
async def handle_group_command(update,ctx):
    u=update.effective_user
    if u and not is_protected_user(u.id): await fake_command(update,ctx)

# username/referrals simplified
async def track_private(user):
    if not user: return
    async with db_pool.acquire() as con: await con.execute('INSERT INTO private_users(user_id,username,first_name,last_name,created_at,updated_at) VALUES($1,$2,$3,$4,NOW(),NOW()) ON CONFLICT(user_id) DO UPDATE SET username=$2,first_name=$3,last_name=$4,updated_at=NOW()',user.id,user.username,user.first_name,user.last_name)
async def username_match(user):
    if not user or is_system_or_anonymous_user(user) or is_protected_user(user.id): return None
    full=' '.join([getattr(user,'username','') or '',getattr(user,'first_name','') or '',getattr(user,'last_name','') or '']).lower()
    async with db_pool.acquire() as con: rows=await con.fetch('SELECT pattern FROM forbidden_usernames')
    for r in rows:
        p=(r['pattern'] or '').strip().lower()
        if p and contains_forbidden_token(full,p): return p
    return None
async def ban_for_username(ctx,user):
    p=await username_match(user)
    if not p: return False
    try: await ctx.bot.ban_chat_member(GROUP_ID,user.id); await add_danger(user.id,20,f'username interdit:{p}'); await inc_counter('session_exclusions'); await alert_ban(ctx,user,'username interdit',p); print(f'USERNAME BAN MATCH user={user.id} pattern={p}',flush=True); return True
    except Exception as e: print(f'USERNAME BAN ERROR user={getattr(user,"id",None)}: {e}',flush=True); return False
async def get_ref_link(ctx,user):
    async with db_pool.acquire() as con:
        row=await con.fetchrow('SELECT invite_link,revoked FROM referral_links WHERE user_id=$1',user.id)
        if row and row['invite_link'] and not row['revoked']: return row['invite_link']
    try: link=(await ctx.bot.create_chat_invite_link(GROUP_ID,name=f'ref_{user.id}',creates_join_request=False)).invite_link
    except Exception as e: print(f'REFERRAL LINK CREATE ERROR user={user.id}: {e}',flush=True); return None
    async with db_pool.acquire() as con: await con.execute('INSERT INTO referral_links(user_id,invite_link,username,first_name,created_at,revoked) VALUES($1,$2,$3,$4,NOW(),FALSE) ON CONFLICT(user_id) DO UPDATE SET invite_link=$2,username=$3,first_name=$4,revoked=FALSE',user.id,link,user.username,user.first_name)
    return link
async def share_count(uid):
    async with db_pool.acquire() as con: return int(await con.fetchval('SELECT COUNT(*) FROM referrals WHERE referrer_id=$1 AND valid=TRUE',uid) or 0)
async def share_rank(uid):
    async with db_pool.acquire() as con: rows=await con.fetch('SELECT referrer_id,COUNT(*) total,MIN(validated_at) first FROM referrals WHERE valid=TRUE GROUP BY referrer_id ORDER BY total DESC,first ASC NULLS LAST,referrer_id ASC')
    for i,r in enumerate(rows,1):
        if r['referrer_id']==uid: return i
    return None
async def send_share(update,ctx):
    u=update.effective_user; await track_private(u); link=await get_ref_link(ctx,u)
    if not link: await update.message.reply_text('❌ Impossible de créer votre lien.'); return
    rank=await share_rank(u.id); await update.message.reply_text(f'🤝 Votre lien personnel\n\n{link}\n\n✅ Invitations validées : {await share_count(u.id)}\n🏆 Votre rang actuel : {"#"+str(rank) if rank else "non classé"}\n\nPartagez ce lien pour monter dans le classement.')

# group handler priority
def is_join_left_service_message(msg) -> bool:
    return bool(
        getattr(msg, "new_chat_members", None)
        or getattr(msg, "left_chat_member", None)
        or getattr(msg, "new_chat_title", None)
        or getattr(msg, "new_chat_photo", None)
        or getattr(msg, "delete_chat_photo", None)
        or getattr(msg, "group_chat_created", None)
        or getattr(msg, "supergroup_chat_created", None)
        or getattr(msg, "channel_chat_created", None)
    )


def has_any_content_v16(msg) -> bool:
    return bool(
        getattr(msg,'text',None) or getattr(msg,'caption',None) or
        getattr(msg,'photo',None) or getattr(msg,'video',None) or
        getattr(msg,'animation',None) or getattr(msg,'document',None) or
        getattr(msg,'sticker',None) or getattr(msg,'voice',None) or
        getattr(msg,'video_note',None) or getattr(msg,'audio',None) or
        getattr(msg,'contact',None) or getattr(msg,'location',None) or
        getattr(msg,'venue',None) or getattr(msg,'poll',None) or
        getattr(msg,'dice',None) or getattr(msg,'game',None) or
        getattr(msg,'invoice',None) or getattr(msg,'successful_payment',None)
    )


def has_any_media_or_attachment_v16(msg) -> bool:
    return bool(
        getattr(msg,'photo',None) or getattr(msg,'video',None) or
        getattr(msg,'animation',None) or getattr(msg,'document',None) or
        getattr(msg,'sticker',None) or getattr(msg,'voice',None) or
        getattr(msg,'video_note',None) or getattr(msg,'audio',None)
    )


def has_admin_or_any_mention_v16(msg) -> bool:
    txt = raw_message_text(msg) if 'raw_message_text' in globals() else (getattr(msg,'text',None) or getattr(msg,'caption',None) or '')
    if '@' in (txt or ''):
        return True
    ents = (getattr(msg,'entities',None) or []) + (getattr(msg,'caption_entities',None) or [])
    for ent in ents:
        if getattr(ent,'type',None) in ('mention','text_mention'):
            return True
    return False


def is_any_slash_command_v16(msg) -> bool:
    txt = (raw_message_text(msg) if 'raw_message_text' in globals() else (getattr(msg,'text',None) or getattr(msg,'caption',None) or '')).strip()
    return txt.startswith('/')


async def increment_referral_counter_v16(con, row):
    cols = set(row.keys())
    referrer_col = None
    for c in ('referrer_id','inviter_id','user_id','owner_id'):
        if c in cols:
            referrer_col = c
            break
    if not referrer_col:
        print(f'REFERRAL COUNTER NO_REFERRER_COL cols={cols}', flush=True)
        return False
    referrer_id = row[referrer_col]
    # Recalcule compteur depuis referrals validés plutôt que d'incrémenter une colonne inconnue.
    try:
        await con.execute("""
            INSERT INTO leaderboard_rank_cache(user_id,score,updated_at)
            VALUES($1, COALESCE((SELECT COUNT(*) FROM referrals WHERE referrer_id=$1 AND COALESCE(validated,false)=true),0), NOW())
            ON CONFLICT(user_id) DO UPDATE SET score=EXCLUDED.score, updated_at=NOW()
        """, referrer_id)
        print(f'REFERRAL COUNTER UPDATED referrer={referrer_id}', flush=True)
        return True
    except Exception as e:
        print(f'REFERRAL COUNTER UPDATE SKIPPED referrer={referrer_id}: {e}', flush=True)
        return False


async def closed_session_restrict_silent(ctx, user_id:int, days:int, reason:str):
    until = datetime.now(TZ) + timedelta(days=days)
    try:
        await ctx.bot.restrict_chat_member(GROUP_ID, user_id, ChatPermissions(can_send_messages=False), until_date=until)
    except Exception as e:
        print(f'CLOSED SILENT RESTRICT ERROR user={user_id}: {e}', flush=True)
    try:
        await ctx.bot.ban_chat_member(GROUP_ID, user_id, until_date=until)
        seconds = int((until - datetime.now(TZ)).total_seconds())
        ctx.application.create_task(unban_later(ctx, user_id, seconds))
        print(f'CLOSED SILENT VISIBILITY BAN user={user_id} days={days} reason={reason}', flush=True)
    except Exception as e:
        print(f'CLOSED SILENT VISIBILITY BAN ERROR user={user_id}: {e}', flush=True)
    try:
        async with db_pool.acquire() as con:
            await con.execute("""
                INSERT INTO restricted_users(user_id,reason,restricted_until,created_at,updated_at)
                VALUES($1,$2,$3,NOW(),NOW())
                ON CONFLICT(user_id) DO UPDATE SET reason=$2,restricted_until=$3,updated_at=NOW()
            """, user_id, reason, until.replace(tzinfo=None))
    except Exception as e:
        print(f'CLOSED SILENT RESTRICT DB ERROR user={user_id}: {e}', flush=True)


async def closed_session_ban_silent(update, ctx, reason:str):
    user = update.effective_user
    msg = update.effective_message
    if not user:
        return
    try:
        if msg:
            await delete_safe(ctx, GROUP_ID, msg.message_id)
        await ctx.bot.ban_chat_member(GROUP_ID, user.id)
        await purge_user(ctx, user.id)
        print(f'CLOSED SILENT BAN user={user.id} reason={reason}', flush=True)
    except Exception as e:
        print(f'CLOSED SILENT BAN ERROR user={user.id}: {e}', flush=True)


async def closed_session_block(update, ctx) -> bool:
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return True
    if await get_open_session():
        return False

    # Session fermée = aucun contenu ne doit rester.
    # Aucun rappel participation. Seulement pièges silencieux.
    if user and not is_system_or_anonymous_user(user) and not is_protected_user(user.id):
        txt = raw_message_text(msg)

        # Commandes admin/trusted ou n'importe quelle commande slash en session fermée:
        # restriction silencieuse.
        if is_any_slash_command_v16(msg):
            await delete_safe(ctx, GROUP_ID, msg.message_id)
            c = await violation(user.id, 'closed_fake_command')
            await closed_session_restrict_silent(ctx, user.id, 2 if c <= 1 else min(30, 1+c), 'commande en session fermée')
            print(f'CLOSED SESSION COMMAND RESTRICT user={user.id} msg={msg.message_id}', flush=True)
            return True

        # Mentions @admin ou @n'importe qui en session fermée: restriction silencieuse.
        if has_admin_or_any_mention_v16(msg):
            await delete_safe(ctx, GROUP_ID, msg.message_id)
            c = await violation(user.id, 'closed_mention')
            await closed_session_restrict_silent(ctx, user.id, 1 if c <= 1 else min(30, c), 'mention en session fermée')
            print(f'CLOSED SESSION MENTION RESTRICT user={user.id} msg={msg.message_id}', flush=True)
            return True

        # Tous médias/attachments : si hash banni -> ban; sinon suppression silencieuse.
        if has_any_media_or_attachment_v16(msg):
            try:
                # Hash uniquement pour médias hashables; autres types supprimés quand même.
                if has_media(msg):
                    keys, hashable = await media_keys(ctx, msg)
                    banned = await any_banned(keys)
                    if banned:
                        print(f'CLOSED SESSION BANNED HASH user={user.id} hash={banned}', flush=True)
                        await closed_session_ban_silent(update, ctx, 'média hash interdit session fermée')
                        return True
                    if keys:
                        await record_media(keys, user.id, GROUP_ID, msg.message_id, media_type(msg))
            except Exception as e:
                print(f'CLOSED SESSION HASH CHECK ERROR user={user.id}: {e}', flush=True)

        # Lien : piège et ban silencieux.
        if has_link_v10(msg):
            print(f'CLOSED SESSION LINK BAN user={user.id} text={raw_message_text(msg)[:80]}', flush=True)
            await closed_session_ban_silent(update, ctx, 'lien interdit session fermée')
            return True

        # Mots bannis/interdits.
        if txt:
            try:
                async with db_pool.acquire() as con:
                    hard = await con.fetch('SELECT word FROM banned_words_hard')
                for r in hard:
                    w = (r['word'] or '').strip().lower()
                    if w and contains_forbidden_token(txt, w):
                        print(f'CLOSED SESSION WORD BAN user={user.id} word={w}', flush=True)
                        await closed_session_ban_silent(update, ctx, 'mot banni session fermée')
                        return True

                async with db_pool.acquire() as con:
                    words = await con.fetch('SELECT word FROM banned_words')
                for r in words:
                    w = (r['word'] or '').strip().lower()
                    if w and contains_forbidden_token(txt, w):
                        print(f'CLOSED SESSION WORD FORBIDDEN user={user.id} word={w}', flush=True)
                        await delete_safe(ctx, GROUP_ID, msg.message_id)
                        c = await violation(user.id, 'closed_forbidden_word')
                        await closed_session_restrict_silent(ctx, user.id, 3 if c == 1 else min(30, 2+c), 'mot interdit session fermée')
                        return True
            except Exception as e:
                print(f'CLOSED SESSION WORD CHECK ERROR user={user.id}: {e}', flush=True)

    # Tout le reste: suppression silencieuse totale.
    await delete_safe(ctx, GROUP_ID, msg.message_id)
    if user and not is_system_or_anonymous_user(user):
        print(f'CLOSED SESSION DELETE ANY user={user.id} msg={msg.message_id}', flush=True)
    else:
        print(f'CLOSED SESSION DELETE ANY msg={msg.message_id}', flush=True)
    return True


def raw_message_text(msg) -> str:
    return (getattr(msg,'text',None) or getattr(msg,'caption',None) or '').strip()


def has_link_v10(msg) -> bool:
    txt = raw_message_text(msg)
    if re.search(r'(https?://|t\.me/|telegram\.me/|www\.|\.com\b|\.net\b|\.org\b|\.fr\b)', txt, re.I):
        return True
    ents = (getattr(msg,'entities',None) or []) + (getattr(msg,'caption_entities',None) or [])
    for ent in ents:
        if getattr(ent,'type',None) in ('url','text_link'):
            return True
    return False


async def process_media_priority_v8(update, ctx, keys, hashable):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not keys:
        return 'ok'

    banned = await any_banned(keys)
    if banned:
        print(f'V10 HASH PRIORITY: BANNED_HASH FIRST user={user.id} hash={banned}', flush=True)
        if is_protected_user(user.id):
            await delete_safe(ctx, GROUP_ID, msg.message_id)
            return 'stop'
        await punish_ban(update, ctx, 'média hash interdit', MSG_GENERIC_FORBIDDEN)
        return 'stop'

    if await get_setting('anti_repost_enabled','on') == 'on':
        existing = await any_existing(keys)
        if existing:
            print(f'V10 HASH PRIORITY: ANTI_REPOST AFTER_BANNED_CLEAR user={user.id} hash={existing}', flush=True)
            await delete_safe(ctx, GROUP_ID, msg.message_id)
            await inc_counter('session_deletions')
            await warning(ctx, f'{plain_name(user)}, ce média a déjà été publié. Ce soir pas de recyclage, sors tes médias du placard !', 30)
            await add_danger(user.id, 2, 'repost média')
            return 'stop'

    return 'ok'


async def ban_for_forbidden_username(ctx, user):
    if 'ban_for_username' in globals():
        return await ban_for_username(ctx, user)
    if 'username_forbidden_match' in globals():
        pat = await username_forbidden_match(user)
        if pat:
            try:
                await ctx.bot.ban_chat_member(GROUP_ID, user.id)
                print(f'USERNAME BAN MATCH user={user.id} pattern={pat}', flush=True)
                return True
            except Exception as e:
                print(f'USERNAME BAN ERROR user={getattr(user,"id",None)}: {e}', flush=True)
    return False


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Retour', callback_data='info')]])


async def handle_group_message(update,ctx):
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return

    if is_join_left_service_message(msg):
        # Pendant le kick non-participants, on laisse visibles les notifications Telegram natives
        # du type "Bot removed X", mais on les enregistre pour suppression à la fermeture.
        active_sid = await get_setting('nonparticipant_kick_active_session_id','')
        if getattr(msg, 'left_chat_member', None) and active_sid:
            try:
                await save_message(GROUP_ID, msg.message_id, None, True)
                async with db_pool.acquire() as con:
                    await con.execute('''
                        INSERT INTO nonparticipant_kick_messages(chat_id,message_id,session_id,created_at)
                        VALUES($1,$2,$3,NOW())
                        ON CONFLICT(chat_id,message_id) DO NOTHING
                    ''', GROUP_ID, msg.message_id, int(active_sid))
                print(f'NONPARTICIPANT KICK NOTICE KEPT sid={active_sid} msg={msg.message_id}', flush=True)
            except Exception as e:
                print(f'NONPARTICIPANT KICK NOTICE TRACK ERROR msg={msg.message_id}: {e}', flush=True)
            return

        await delete_safe(ctx, GROUP_ID, msg.message_id)
        print(f"JOIN_LEFT SERVICE DELETE msg={msg.message_id}", flush=True)
        if getattr(msg, "new_chat_members", None):
            for joined in msg.new_chat_members:
                if is_system_or_anonymous_user(joined):
                    continue
                if getattr(joined, "is_bot", False) and not is_protected_user(joined.id):
                    try:
                        await ctx.bot.ban_chat_member(GROUP_ID, joined.id)
                        print(f"BOT JOIN BAN user={joined.id}", flush=True)
                    except Exception as ex:
                        print(f"BOT JOIN BAN ERROR: {ex}", flush=True)
                    continue
                await ban_for_forbidden_username(ctx, joined)
                await schedule_referral_validation_10min(ctx, joined.id)
        return

    if not user or is_system_or_anonymous_user(user):
        return

    if await closed_session_block(update, ctx):
        return

    await save_message(GROUP_ID,msg.message_id,user.id,False,getattr(msg,'media_group_id',None))
    await track_open_session_presence(user)

    text=msg_text(msg)
    protected=is_protected_user(user.id)
    keys=[]
    hashable=False

    if has_media(msg):
        keys,hashable=await media_keys(ctx,msg)
        if await process_media_priority_v8(update,ctx,keys,hashable) == 'stop':
            return

    if text and not protected:
        async with db_pool.acquire() as con:
            hard=await con.fetch('SELECT word FROM banned_words_hard')
        for r in hard:
            w=(r['word'] or '').strip().lower()
            if w and contains_forbidden_token(text,w):
                print(f'WORD BAN MATCH user={user.id} word={w}',flush=True)
                await punish_ban(update,ctx,'mot banni',MSG_GENERIC_FORBIDDEN)
                await alert_ban(ctx,user,'mot banni dans le message',w)
                return

        async with db_pool.acquire() as con:
            words=await con.fetch('SELECT word FROM banned_words')
        for r in words:
            w=(r['word'] or '').strip().lower()
            if w and contains_forbidden_token(text,w):
                await punish_word(update,ctx,w)
                return

    if not protected:
        if has_link_v10(msg):
            print(f'LINK BAN MATCH user={user.id} text={raw_message_text(msg)[:80]}', flush=True)
            await punish_ban(update,ctx,'lien interdit',MSG_LINK_FORBIDDEN)
            return
        if is_forwarded(msg) and not has_media(msg):
            await punish_ban(update,ctx,'forward texte interdit',MSG_FORWARD_FORBIDDEN)
            return
        if is_live_or_story(msg):
            await punish_ban(update,ctx,'live/story interdit',MSG_GENERIC_FORBIDDEN)
            return

    if not protected and has_media(msg) and '@' in (getattr(msg,'caption','') or ''):
        await delete_safe(ctx,GROUP_ID,msg.message_id)
        c=await violation(user.id,'media_mention_ad')
        await restrict_days(ctx,user.id,max(1,min(c,30)),'media_mention_ad')
        await inc_counter('session_mutes')
        await warning(ctx,MSG_PUB_ATTEMPT,30)
        return

    if await get_setting('participation','on')=='on' and not protected and not await has_participated(user.id):
        if not has_media(msg):
            await delete_safe(ctx,GROUP_ID,msg.message_id)
            await inc_counter('session_deletions')
            print(f'PARTICIPATION REQUIRED TRIGGERED user={user.id}',flush=True)
            await participation_warning(ctx,user)
            await add_danger(user.id,1,'message avant participation')
            return

    if await get_setting('participation','on')=='on' and not protected and has_media(msg) and hashable and keys and not await has_participated(user.id):
        await mark_participated(user,keys[0])

    if has_media(msg) and keys:
        await record_media(keys,user.id,GROUP_ID,msg.message_id,media_type(msg))
        await rediffuse(ctx,msg)

async def schedule_referral_validation_10min(ctx, invited_user_id:int):
    print(f'REFERRAL 10MIN SCHEDULE user={invited_user_id}', flush=True)
    async def later():
        await asyncio.sleep(600)
        await validate_referral_after_10min(ctx, invited_user_id)
    ctx.application.create_task(later())


async def validate_referral_after_10min(ctx, invited_user_id:int):
    try:
        member = await ctx.bot.get_chat_member(GROUP_ID, invited_user_id)
        if member.status in ('left','kicked'):
            print(f'REFERRAL 10MIN FAILED_LEFT user={invited_user_id} status={member.status}', flush=True)
            return False
    except Exception as e:
        print(f'REFERRAL 10MIN CHECK ERROR user={invited_user_id}: {e}', flush=True)
        return False

    async with db_pool.acquire() as con:
        row = None
        id_col = None
        # Compatibilité colonnes connues.
        for candidate in ('invited_id','referred_id'):
            try:
                row = await con.fetchrow(f"""
                    SELECT * FROM referrals
                    WHERE {candidate}=$1 AND COALESCE(validated,false)=false
                    ORDER BY created_at DESC
                    LIMIT 1
                """, invited_user_id)
                if row:
                    id_col = candidate
                    break
            except Exception:
                continue

        if not row:
            print(f'REFERRAL 10MIN NO_PENDING user={invited_user_id}', flush=True)
            return False

        cols = set(row.keys())
        try:
            if 'validated_at' in cols:
                await con.execute(f"UPDATE referrals SET validated=true, validated_at=NOW() WHERE {id_col}=$1", invited_user_id)
            else:
                await con.execute(f"UPDATE referrals SET validated=true WHERE {id_col}=$1", invited_user_id)
            await increment_referral_counter_v16(con, row)
            print(f'REFERRAL VALIDATED 10MIN user={invited_user_id}', flush=True)
            return True
        except Exception as e:
            print(f'REFERRAL 10MIN UPDATE ERROR user={invited_user_id}: {e}', flush=True)
            return False


async def chat_member_update(update,ctx):
    cm=update.chat_member
    if not cm: return
    user=cm.new_chat_member.user
    if cm.new_chat_member.status in ('member','restricted'):
        await track_open_session_presence(user)
        await ban_for_username(ctx,user)

# rediffusion
async def rediffuse_media_if_enabled(update, ctx):
    if await get_setting('rediffusion_enabled','off')!='on':
        return
    msg = update.effective_message
    if not msg or not REDIFFUSION_GROUP_ID or not message_has_photo_video(msg):
        return
    try:
        await ctx.bot.copy_message(chat_id=REDIFFUSION_GROUP_ID, from_chat_id=GROUP_ID, message_id=msg.message_id)
        print(f'REDIFFUSION COPY OK msg={msg.message_id} target={REDIFFUSION_GROUP_ID}', flush=True)
    except Exception as e:
        print(f'REDIFFUSION COPY ERROR msg={msg.message_id} target={REDIFFUSION_GROUP_ID}: {e}', flush=True)


async def validate_rediff(ctx):
    if not REDIFFUSION_GROUP_ID:
        return False, 'REDIFFUSION_GROUP_ID manquant'
    try:
        me = await ctx.bot.get_me()
        member = await ctx.bot.get_chat_member(REDIFFUSION_GROUP_ID, me.id)
        if member.status not in ('administrator','creator'):
            return False, f'bot non admin dans cible ({member.status})'
        return True, 'OK'
    except Exception as e:
        return False, str(e)

async def rediffuse(ctx,msg):
    if await get_setting('rediffusion_enabled','off')!='on' or not REDIFFUSION_GROUP_ID: return
    try: await ctx.bot.copy_message(REDIFFUSION_GROUP_ID,GROUP_ID,msg.message_id); print(f'REDIFFUSION COPY OK msg={msg.message_id}',flush=True)
    except Exception as e: print(f'REDIFFUSION COPY ERROR msg={msg.message_id}: {e}',flush=True)

# panels (clean minimal)
def back(): return InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Retour',callback_data='info')]])
async def main_kb():
    part = '🟢 Participation ON' if await get_setting('participation','on')=='on' else '🔴 Participation OFF'
    auto = '🟢 Ouverture auto ON' if await get_setting('auto_schedule_enabled','off')=='on' else '🔴 Ouverture auto OFF'
    red = '🟢 Rediffusion ON' if await get_setting('rediffusion_enabled','off')=='on' else '🔴 Rediffusion OFF'
    vis = '🟢 Sanctions visibles ON' if await get_setting('silent_sanctions','on')=='on' else '🔴 Sanctions visibles OFF'
    lb = '🟢 Classement ON' if await get_setting('leaderboard_enabled','on')=='on' else '🔴 Classement OFF'
    repost = '🟢 Anti-repost ON' if await get_setting('anti_repost_enabled','on')=='on' else '🔴 Anti-repost OFF'
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(part,callback_data='toggle_participation')],
        [InlineKeyboardButton('🟢 Ouvrir session',callback_data='open_session')],
        [InlineKeyboardButton('🔴 Fermer session',callback_data='close_session')],
        [InlineKeyboardButton(auto,callback_data='toggle_auto_schedule')],
        [InlineKeyboardButton(repost,callback_data='toggle_anti_repost')],
        [InlineKeyboardButton('👢 Non-participants',callback_data='nonparticipants_prompt')],
        [InlineKeyboardButton('🚫 Mots interdits',callback_data='words_menu')],
        [InlineKeyboardButton('⛔ Mots bannis',callback_data='hard_menu')],
        [InlineKeyboardButton('👤 Usernames interdits',callback_data='users_menu')],
        [InlineKeyboardButton('📣 Publicité partage',callback_data='share_menu')],
        [InlineKeyboardButton('📢 Broadcast privé',callback_data='broadcast_set')],
        [InlineKeyboardButton('📣 Broadcast groupe',callback_data='group_broadcast_set')],
        [InlineKeyboardButton('📜 Règles',callback_data='rules_menu')],
        [InlineKeyboardButton('🧬 Hash média',callback_data='hash_media_set')],
        [InlineKeyboardButton(red,callback_data='toggle_rediffusion')],
        [InlineKeyboardButton(vis,callback_data='toggle_silent')],
        [InlineKeyboardButton(lb,callback_data='toggle_leaderboard')],
        [InlineKeyboardButton('🧪 Auto-test',callback_data='autotest')],
        [InlineKeyboardButton('ℹ️ Info système',callback_data='system_info')],
    ])

async def st_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton('👀 Voir mots interdits',callback_data='st_words')],[InlineKeyboardButton('➕ Ajouter mot interdit',callback_data='st_add')],[InlineKeyboardButton('📊 Stats 7 jours',callback_data='st_stats7')],[InlineKeyboardButton('📈 Historique complet',callback_data='st_statsall')]])
async def panel_text():
    sess = await get_open_session()
    st = '🟢 ouverte #' + str(sess['id']) if sess else '🔴 fermée'
    auto = await get_setting('auto_schedule_enabled','off')
    anti = await get_setting('anti_repost_enabled','on')
    return f'🛠️ Panel admin\n\nSession {st}\nOuverture auto {auto}\nAnti-repost {anti}\nParticipation {await get_setting("participation","on")}\nMessages visibles {await get_setting("silent_sanctions","on")}\nRediffusion {await get_setting("rediffusion_enabled","off")}\nClassement {await get_setting("leaderboard_enabled","on")}\nNon-participants {await get_setting("nonparticipant_enabled","on")}\n\nHoraires auto : Lun-Ven 22h-00h / Sam 23h-01h / Dim 22h30-00h15\n\nVersion: {APP_VERSION}'

async def start(update,ctx):
    u=update.effective_user
    if update.effective_chat.type=='private': await track_private(u)
    if ctx.args and ctx.args[0]=='share': await send_share(update,ctx); return
    if is_admin(u.id): await set_state(u.id,None); await update.message.reply_text(await panel_text(),reply_markup=await main_kb())
    elif u.id in SUPER_TRUSTED_IDS: await set_state(u.id,None); await update.message.reply_text('🛡️ Panel Super Trusted',reply_markup=await st_kb())
    else: await send_share(update,ctx)
async def list_values(table,col):
    async with db_pool.acquire() as con: rows=await con.fetch(f'SELECT {col} v FROM {table} ORDER BY {col}')
    return '📋 Liste\n\n'+('\n'.join('• '+r['v'] for r in rows) if rows else 'Vide.')
async def add_banned_hashes_from_message_v14(ctx, msg, actor_id:int):
    if not has_media(msg):
        return 0, 0, 'Aucun média détecté.'
    try:
        keys, hashable = await media_keys(ctx, msg)
    except Exception as e:
        print(f'HASH MEDIA WAIT ERROR media_keys actor={actor_id}: {e}', flush=True)
        return 0, 0, f'Erreur calcul hash: {e}'
    if not keys:
        return 0, 0, 'Hash impossible.'

    new_count = 0
    duplicate_count = 0
    async with db_pool.acquire() as con:
        for h in keys:
            exists = await con.fetchval('SELECT 1 FROM banned_hashes WHERE hash=$1', h)
            if exists:
                duplicate_count += 1
                continue
            try:
                await con.execute('INSERT INTO banned_hashes(hash,created_at,added_by) VALUES($1,NOW(),$2)', h, actor_id)
                new_count += 1
            except Exception:
                duplicate_count += 1

    print(f'HASH MEDIA WAIT STORED actor={actor_id} new={new_count} duplicate={duplicate_count} keys={keys}', flush=True)
    return new_count, duplicate_count, 'OK'


async def add_banned_hashes_from_message_v10(ctx, msg, actor_id:int):
    if not has_media(msg):
        return 0, 'Aucun média détecté.'
    try:
        keys, hashable = await media_keys(ctx, msg)
    except Exception as e:
        return 0, f'Erreur calcul hash: {e}'
    if not keys:
        return 0, 'Hash impossible.'
    async with db_pool.acquire() as con:
        count = 0
        for h in keys:
            await con.execute('INSERT INTO banned_hashes(hash,created_at,added_by) VALUES($1,NOW(),$2) ON CONFLICT(hash) DO NOTHING', h, actor_id)
            count += 1
    print(f'MANUAL HASH BAN ADDED actor={actor_id} count={count} keys={keys}', flush=True)
    return count, 'OK'


async def send_configured_rule(ctx, rule_no:int):
    text = await get_setting(f'rule{rule_no}_text','')
    photo = await get_setting(f'rule{rule_no}_photo_file_id','')
    if not text and not photo:
        print(f'RULE {rule_no} SKIPPED: empty', flush=True)
        return None
    try:
        if photo:
            msg = await ctx.bot.send_photo(GROUP_ID, photo=photo, caption=text or None)
        else:
            msg = await ctx.bot.send_message(GROUP_ID, text)
        await save_message(GROUP_ID, msg.message_id, None, True)
        print(f'RULE {rule_no} POSTED msg={msg.message_id}', flush=True)
        return msg.message_id
    except Exception as e:
        print(f'RULE {rule_no} POST ERROR: {e}', flush=True)
        return None


async def maybe_publish_auto_rules(ctx, sid:int, start, end):
    # Publie chaque règle une seule fois pendant la session auto.
    posted = await get_setting('rules_posted_session_id','')
    if posted == str(sid):
        return
    await set_setting('rules_posted_session_id', str(sid))
    ids = []
    # "Aléatoirement" pendant la session: on choisit 2 délais pseudo-aléatoires mais bornés.
    # Si session trop courte, les règles partent vite mais une seule fois.
    import random
    duration = max(60, int((end-start).total_seconds()))
    delays = sorted([
        random.randint(60, max(61, min(duration-60, duration//3))) if duration > 180 else 20,
        random.randint(max(60, duration//2), max(61, duration-60)) if duration > 180 else 40,
    ])
    async def later(rule_no, delay):
        await asyncio.sleep(delay)
        # Ne publie que si la session est toujours ouverte.
        sess = await get_open_session()
        if not sess or int(sess['id']) != int(sid):
            return
        mid = await send_configured_rule(ctx, rule_no)
        if mid:
            raw = await get_setting('rules_message_ids','[]')
            try:
                arr = json.loads(raw or '[]')
            except Exception:
                arr = []
            arr.append([GROUP_ID, mid, sid])
            await set_setting('rules_message_ids', json.dumps(arr))
    ctx.application.create_task(later(1, delays[0]))
    ctx.application.create_task(later(2, delays[1]))
    print(f'RULES AUTO SCHEDULED sid={sid} delays={delays}', flush=True)


async def cleanup_rules_messages(ctx, sid:int):
    raw = await get_setting('rules_message_ids','[]')
    try:
        arr = json.loads(raw or '[]')
    except Exception:
        arr = []
    keep = []
    for item in arr:
        try:
            chat_id, mid, item_sid = item
            if int(item_sid) == int(sid):
                await delete_safe(ctx, int(chat_id), int(mid))
                print(f'RULE MESSAGE DELETE sid={sid} msg={mid}', flush=True)
            else:
                keep.append(item)
        except Exception as e:
            print(f'RULE MESSAGE DELETE ERROR item={item}: {e}', flush=True)
    await set_setting('rules_message_ids', json.dumps(keep))


async def broadcast_group(ctx, text:str, photo_file_id:str=''):
    try:
        if photo_file_id:
            msg = await ctx.bot.send_photo(GROUP_ID, photo=photo_file_id, caption=text or None)
        else:
            msg = await ctx.bot.send_message(GROUP_ID, text)
        await save_message(GROUP_ID, msg.message_id, None, True)
        print(f'GROUP BROADCAST SENT msg={msg.message_id}', flush=True)
        return msg.message_id
    except Exception as e:
        print(f'GROUP BROADCAST ERROR: {e}', flush=True)
        return None


async def callbacks(update,ctx):
    q=update.callback_query
    try: await q.answer()
    except Exception: pass
    u=q.from_user; data=q.data
    if data=='st_words' and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await q.edit_message_text(await list_values('banned_words','word'),reply_markup=await st_kb()); return
    if data=='st_add' and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await set_state(u.id,'st_add'); await q.edit_message_text('Envoie le mot interdit.',reply_markup=back()); return
    if data in ('st_stats7','st_statsall') and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await q.edit_message_text(await trusted_stats(ctx,7 if data=='st_stats7' else None),reply_markup=await st_kb()); return
    if not is_admin(u.id): return
    if data=='toggle_anti_repost':
        cur=await get_setting('anti_repost_enabled','on')
        await set_setting('anti_repost_enabled','off' if cur=='on' else 'on')
        await q.edit_message_text(await panel_text(),reply_markup=await main_kb())
        return

    if data=='toggle_auto_schedule':
        cur=await get_setting('auto_schedule_enabled','off')
        await set_setting('auto_schedule_enabled','off' if cur=='on' else 'on')
        await q.edit_message_text(await panel_text(),reply_markup=await main_kb())
        return
    if data=='info': await q.edit_message_text(await panel_text(),reply_markup=await main_kb()); return
    if data=='toggle_participation': await set_setting('participation','off' if await get_setting('participation','on')=='on' else 'on'); await q.edit_message_text(await panel_text(),reply_markup=await main_kb()); return
    if data=='open_session': sid=await open_session_admin(ctx); await q.edit_message_text(f'🟢 Session ouverte #{sid}',reply_markup=await main_kb()); return
    if data=='close_session': sid=await close_session_admin(ctx); await q.edit_message_text(f'🔴 Session fermée #{sid}' if sid else 'ℹ️ Aucune session ouverte.',reply_markup=await main_kb()); return
    if data=='nonparticipants_prompt': await send_nonparticipant_prompt(ctx,u.id); await q.edit_message_text('📩 Demande envoyée en privé. Répondez avec le nombre à expulser.',reply_markup=await main_kb()); return
    if data=='system_info': await q.edit_message_text(await build_system_info(ctx),reply_markup=await main_kb()); return
    if data=='autotest': await q.edit_message_text(await run_admin_autotest(ctx),reply_markup=await main_kb()); return
    if data=='toggle_rediffusion':
        cur=await get_setting('rediffusion_enabled','off')
        if cur=='on':
            await set_setting('rediffusion_enabled','off')
            await q.edit_message_text(await panel_text(),reply_markup=await main_kb())
            return
        ok,msg=await validate_rediff(ctx)
        if not ok:
            await q.edit_message_text('❌ Rediffusion impossible\n\n'+str(msg),reply_markup=await main_kb())
            return
        await set_setting('rediffusion_enabled','on')
        await q.edit_message_text(await panel_text(),reply_markup=await main_kb())
        return

    if data=='toggle_silent': await set_setting('silent_sanctions','off' if await get_setting('silent_sanctions','on')=='on' else 'on'); await q.edit_message_text(await panel_text(),reply_markup=await main_kb()); return
    if data=='toggle_leaderboard': await set_setting('leaderboard_enabled','off' if await get_setting('leaderboard_enabled','on')=='on' else 'on'); await q.edit_message_text(await panel_text(),reply_markup=await main_kb()); return
    if data=='toggle_rediff':
        if await get_setting('rediffusion_enabled','off')=='on': await set_setting('rediffusion_enabled','off'); await q.edit_message_text('📡 Rediffusion OFF',reply_markup=await main_kb())
        else:
            ok,msg=await validate_rediff(ctx)
            if ok: await set_setting('rediffusion_enabled','on')
            await q.edit_message_text('📡 Rediffusion ON' if ok else msg,reply_markup=await main_kb())
        return
    menus={'words_menu':('banned_words','word','words'),'hard_menu':('banned_words_hard','word','hard'),'users_menu':('forbidden_usernames','pattern','users')}
    if data in menus:
        _,_,p=menus[data]; await q.edit_message_text('Menu',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('➕ Ajouter',callback_data=p+'_add')],[InlineKeyboardButton('➖ Supprimer',callback_data=p+'_del')],[InlineKeyboardButton('📋 Voir',callback_data=p+'_list')],[InlineKeyboardButton('⬅️ Retour',callback_data='info')]])); return
    if data.endswith('_add') or data.endswith('_del'): await set_state(u.id,data); await q.edit_message_text('Envoie la valeur.',reply_markup=back()); return
    if data.endswith('_list'):
        table,col={'words_list':('banned_words','word'),'hard_list':('banned_words_hard','word'),'users_list':('forbidden_usernames','pattern')}[data]; await q.edit_message_text(await list_values(table,col),reply_markup=await main_kb()); return
    if data=='hash_media_set':
        await set_state(q.from_user.id,'hash_media_wait')
        print(f'HASH MEDIA WAIT SET admin={q.from_user.id}', flush=True)
        await q.edit_message_text('🧬 Envoie maintenant le média à ajouter aux hash bannis.\n\nEnsuite, toute personne qui publie ce média sera bannie.',reply_markup=back_kb())
        return

    if data=='group_broadcast_set':
        await set_state(q.from_user.id,'group_broadcast')
        await q.edit_message_text('📣 Envoie le message à publier dans le groupe. Tu peux envoyer texte ou photo avec légende.',reply_markup=back_kb())
        return

    if data=='rules_menu':
        await q.edit_message_text('📜 Règles automatiques\n\nChaque règle peut avoir un texte et/ou une image. En ouverture AUTO, chaque règle est publiée une fois à un moment aléatoire de la session, puis supprimée à la fermeture.',reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('1️⃣ Règle 1',callback_data='rule1_menu')],
            [InlineKeyboardButton('2️⃣ Règle 2',callback_data='rule2_menu')],
            [InlineKeyboardButton('⬅️ Retour',callback_data='info')]
        ]))
        return

    if data in ('rule1_menu','rule2_menu'):
        n='1' if data=='rule1_menu' else '2'
        await q.edit_message_text(f'📜 Règle {n}',reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('✏️ Texte',callback_data=f'rule{n}_text_set')],
            [InlineKeyboardButton('🖼️ Image',callback_data=f'rule{n}_photo_set')],
            [InlineKeyboardButton('👀 Aperçu',callback_data=f'rule{n}_preview')],
            [InlineKeyboardButton('⬅️ Retour',callback_data='rules_menu')]
        ]))
        return

    if data in ('rule1_text_set','rule2_text_set','rule1_photo_set','rule2_photo_set'):
        await set_state(q.from_user.id,data)
        kind='texte' if 'text' in data else 'image'
        await q.edit_message_text(f'Envoie le {kind} pour {data[:5]}.',reply_markup=back_kb())
        return

    if data in ('rule1_preview','rule2_preview'):
        n=1 if data=='rule1_preview' else 2
        text=await get_setting(f'rule{n}_text','')
        photo=await get_setting(f'rule{n}_photo_file_id','')
        status=f'Texte: {"✅" if text else "❌"}\nImage: {"✅" if photo else "❌"}'
        await q.edit_message_text(f'👀 Aperçu règle {n}\n\n{status}\n\n{text or "(aucun texte)"}',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Retour',callback_data=f'rule{n}_menu')]]))
        return

    if data=='share_menu': await q.edit_message_text('Publicité partage',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✏️ Texte',callback_data='share_text')],[InlineKeyboardButton('🖼️ Image',callback_data='share_photo')],[InlineKeyboardButton('📣 Publier',callback_data='share_publish')],[InlineKeyboardButton('⬅️ Retour',callback_data='info')]])); return
    if data in ('share_text','share_photo','broadcast_set'): await set_state(u.id,data); await q.edit_message_text('Envoie maintenant.',reply_markup=back()); return
    if data=='share_publish': await publish_share(ctx); await q.edit_message_text('✅ Pub publiée',reply_markup=await main_kb()); return
async def private_admin(update,ctx):
    u=update.effective_user; msg=update.message; await track_private(u); st=await get_state(u.id)
    if not st: return
    state=st['state']; text=(msg.text or msg.caption or '').strip()
    if state=='nonparticipant_kick_count':
        if not is_admin(u.id): return
        if not text.strip().isdigit(): await set_state(u.id,None); await msg.reply_text('ℹ️ Aucune action effectuée.'); return
        kicked=await kick_nonparticipants_public(ctx,int(text.strip())); await set_state(u.id,None); await msg.reply_text(f'✅ Expulsions non-participants terminées : {kicked}'); return
    if state=='st_add' and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await add_value('banned_words','word',text); await set_state(u.id,None); await msg.reply_text('✅ Ajouté.'); return
    if not is_admin(u.id): return
    maps={'words_add':('banned_words','word','add'),'hard_add':('banned_words_hard','word','add'),'users_add':('forbidden_usernames','pattern','add'),'words_del':('banned_words','word','del'),'hard_del':('banned_words_hard','word','del'),'users_del':('forbidden_usernames','pattern','del')}
    if state in maps:
        t,c,a=maps[state]; await (add_value(t,c,text) if a=='add' else del_value(t,c,text)); await set_state(u.id,None); await msg.reply_text('✅ OK.'); return
    if state=='share_text': await set_setting('share_pub_text',text); await set_state(u.id,None); await msg.reply_text('✅ OK.'); return
    if state=='share_photo' and msg.photo: await set_setting('share_pub_photo_file_id',msg.photo[-1].file_id); await set_state(u.id,None); await msg.reply_text('✅ OK.'); return
    if state=='broadcast_set': await set_state(u.id,None); await msg.reply_text(f'✅ Envoyé à {await broadcast(ctx,text)} utilisateurs.'); return
    if state=='hash_media_wait':
        if not is_admin(u.id):
            return
        if not has_media(msg):
            await msg.reply_text('❌ Envoie une photo ou une vidéo à ajouter aux hash bannis.')
            return

        new_count, duplicate_count, detail = await add_banned_hashes_from_message_v14(ctx, msg, u.id)

        if new_count > 0:
            await set_state(u.id, None)
            await msg.reply_text(
                f'✅ Média ajouté aux hash bannis.\n'
                f'Nouveaux hash : {new_count}\n'
                f'Déjà présents : {duplicate_count}\n\n'
                f'Toute republication entraînera un bannissement automatique.'
            )
            return

        if duplicate_count > 0:
            await msg.reply_text('ℹ️ Hash identique : ce média est déjà dans les hash bannis. Envoie un autre média ou retourne au panel.')
            return

        await msg.reply_text(f'❌ Aucun hash ajouté. {detail}')
        return

async def add_value(table,col,val):
    val=(val or '').strip().lower()
    if table in {'banned_words','banned_words_hard','forbidden_usernames'} and val:
        async with db_pool.acquire() as con: await con.execute(f'INSERT INTO {table}({col},created_at) VALUES($1,NOW()) ON CONFLICT({col}) DO NOTHING',val)
async def del_value(table,col,val):
    val=(val or '').strip().lower()
    if table in {'banned_words','banned_words_hard','forbidden_usernames'} and val:
        async with db_pool.acquire() as con: await con.execute(f'DELETE FROM {table} WHERE {col}=$1',val)
async def publish_share(ctx):
    text=await get_setting('share_pub_text',DEFAULT_SETTINGS['share_pub_text']); photo=await get_setting('share_pub_photo_file_id',''); url=f'https://t.me/{BOT_USERNAME}?start=share' if BOT_USERNAME else 'https://t.me/'
    kb=InlineKeyboardMarkup([[InlineKeyboardButton('🤝 Je partage',url=url)]])
    m=await (ctx.bot.send_photo(GROUP_ID,photo=photo,caption=text,reply_markup=kb) if photo else ctx.bot.send_message(GROUP_ID,text,reply_markup=kb)); await save_message(GROUP_ID,m.message_id,None,True)
async def broadcast(ctx,text):
    async with db_pool.acquire() as con: rows=await con.fetch('SELECT user_id FROM private_users')
    c=0
    for r in rows:
        try: await ctx.bot.send_message(r['user_id'],text); c+=1; await asyncio.sleep(.03)
        except Exception: pass
    return c
async def trusted_stats(ctx,days=None):
    where=f"WHERE created_at >= NOW() - INTERVAL '{int(days)} days'" if days else ''
    async with db_pool.acquire() as con: rows=await con.fetch(f'SELECT trusted_id,COUNT(*) total FROM trusted_actions {where} GROUP BY trusted_id ORDER BY total DESC')
    return ('📊 Stats' if days else '📈 Historique')+'\n\n'+('\n'.join(f'• {r["trusted_id"]}: {r["total"]}' for r in rows) if rows else 'Aucune intervention.')

async def error_handler_v13(update, ctx):
    try:
        print(f'ERROR HANDLED: {ctx.error}', flush=True)
    except Exception:
        pass


def build_app():
    if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN manquant')
    app=Application.builder().token(BOT_TOKEN).build(); app.post_init=lambda app:init_db()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('supprime',trusted_supprime,filters=filters.Chat(GROUP_ID))); app.add_handler(CommandHandler('supprimer',trusted_supprime,filters=filters.Chat(GROUP_ID)))
    app.add_handler(CommandHandler('pasfr',trusted_pasfr,filters=filters.Chat(GROUP_ID))); app.add_handler(CommandHandler('ban',trusted_ban,filters=filters.Chat(GROUP_ID))); app.add_handler(CommandHandler('pedo',trusted_pedo,filters=filters.Chat(GROUP_ID)))
    app.add_handler(CallbackQueryHandler(callbacks)); app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,private_admin))
    app.add_handler(MessageHandler(filters.Chat(GROUP_ID) & filters.COMMAND,handle_group_command)); app.add_handler(MessageHandler(filters.Chat(GROUP_ID) & ~filters.COMMAND,handle_group_message))
    app.add_handler(ChatMemberHandler(chat_member_update,ChatMemberHandler.CHAT_MEMBER));
    if app.job_queue: app.job_queue.run_repeating(auto_schedule_tick,interval=60,first=20)
    app.add_error_handler(error_handler_v13)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL), private_admin))
    return app

def main(): print(f'STARTING {APP_VERSION}',flush=True); build_app().run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=='__main__': main()
