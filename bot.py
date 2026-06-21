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

APP_VERSION='FINAL_CLEAN_V1_FROM_SCRATCH'
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

MSG_PARTICIPATION_REQUIRED='⚠️ {mention}, merci de participer avant d’envoyer un message.\nEnvoyez au moins 1 photo ou 1 vidéo jamais publiée.'
MSG_REPOST='♻️ Ce média a déjà été publié.'; MSG_LINK_FORBIDDEN='🔗 Les liens ne sont pas autorisés.'
MSG_FORWARD_FORBIDDEN='🚫 Les transferts texte ne sont pas autorisés.'; MSG_GENERIC_FORBIDDEN='🚫 Message non autorisé.'
MSG_FAKE_COMMAND='🔇 Commande réservée à la modération. Si vous essayez, vous êtes sanctionné.'
MSG_PASFR='⚠️ Merci d’envoyer uniquement du contenu FR.'; MSG_PUB_ATTEMPT='🚫 Tentative de publicité interdite.'

DEFAULT_SETTINGS={'participation':'on','silent_sanctions':'on','rediffusion_enabled':'off','leaderboard_enabled':'on','share_pub_text':'🤝 Partagez le groupe pour monter dans le classement.\nCliquez ci-dessous pour recevoir votre lien personnel.','share_pub_photo_file_id':''}
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
"CREATE TABLE IF NOT EXISTS system_messages(chat_id BIGINT,message_id BIGINT,created_at TIMESTAMP DEFAULT NOW(),PRIMARY KEY(chat_id,message_id))"]

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

async def init_db():
    global db_pool
    if not DATABASE_URL: raise RuntimeError('DATABASE_URL manquant')
    db_pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=5)
    async with db_pool.acquire() as con:
        for q in TABLES: await con.execute(q)
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
    async with db_pool.acquire() as con: await con.execute('INSERT INTO messages(chat_id,message_id,user_id,media_group_id,is_system,created_at) VALUES($1,$2,$3,$4,$5,NOW()) ON CONFLICT(chat_id,message_id) DO NOTHING',chat_id,msg_id,uid,media_group_id,is_system)
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
async def warning(ctx,text,seconds=180,force=False):
    if not force and not await visible(): return None
    try:
        m=await ctx.bot.send_message(GROUP_ID,text); await save_message(GROUP_ID,m.message_id,None,True); ctx.application.create_task(delete_later(ctx,GROUP_ID,m.message_id,seconds)); return m
    except Exception as e: print(f'WARNING SEND ERROR: {e}',flush=True); return None
async def participation_warning(ctx,user):
    await warning(ctx,MSG_PARTICIPATION_REQUIRED.format(mention=display(user)),10,True); print(f'PARTICIPATION WARNING SENT user={user.id}',flush=True)
async def notify_admins(ctx,text):
    for uid in set(ADMIN_IDS)|set(SUPER_TRUSTED_IDS):
        try: await ctx.bot.send_message(uid,text)
        except Exception as e: print(f'ADMIN ALERT SEND ERROR user={uid}: {e}',flush=True)
async def alert_ban(ctx,user,reason,detected=None):
    det=f'\nÉlément détecté : {detected}' if detected else ''
    await notify_admins(ctx,f'🚨 Ban automatique\n\nMotif : {reason}\nUtilisateur : {display(user)}{det}\nAction : ban direct')

async def restrict_days(ctx,uid,days,reason):
    until=datetime.now(TZ)+timedelta(days=days)
    await ctx.bot.restrict_chat_member(GROUP_ID,uid,ChatPermissions(can_send_messages=False),until_date=until)
    async with db_pool.acquire() as con: await con.execute('INSERT INTO restricted_users(user_id,reason,restricted_until,created_at,updated_at) VALUES($1,$2,$3,NOW(),NOW()) ON CONFLICT(user_id) DO UPDATE SET reason=$2,restricted_until=$3,updated_at=NOW()',uid,reason,until.replace(tzinfo=None))
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
    await purge_user(ctx,user.id); await delete_safe(ctx,GROUP_ID,msg.message_id); await add_danger(user.id,10,reason); await warning(ctx,public_msg)
async def punish_word(update,ctx,word):
    user=update.effective_user; msg=update.effective_message
    if not user or not msg or is_protected_user(user.id): return
    await delete_safe(ctx,GROUP_ID,msg.message_id); days=1 if await has_participated(user.id) else 3
    try: await restrict_days(ctx,user.id,days,f'mot interdit:{word}'); await inc_counter('session_mutes'); print(f'WORD FORBIDDEN MATCH user={user.id} word={word} mute_days={days}',flush=True)
    except Exception as e: print(f'WORD MUTE ERROR user={user.id}: {e}',flush=True)
    await add_danger(user.id,3,f'mot interdit:{word}'); await warning(ctx,MSG_GENERIC_FORBIDDEN)
async def fake_command(update,ctx):
    user=update.effective_user; msg=update.effective_message
    if not user or not msg: return
    await delete_safe(ctx,GROUP_ID,msg.message_id)
    if is_protected_user(user.id): return
    try: await restrict_days(ctx,user.id,2,'fake command'); await inc_counter('session_mutes')
    except Exception as e: print(f'FAKE COMMAND MUTE ERROR user={user.id}: {e}',flush=True)
    await warning(ctx,MSG_FAKE_COMMAND)

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
    await delete_block(ctx,target); await delete_safe(ctx,GROUP_ID,msg.message_id); await inc_counter('session_deletions'); await log_trusted(actor.id,'pasfr',target.from_user.id,target.message_id); await warning(ctx,MSG_PASFR)
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
async def handle_group_message(update,ctx):
    msg=update.effective_message; user=update.effective_user
    if not msg or not user: return
    await save_message(GROUP_ID,msg.message_id,user.id,False,getattr(msg,'media_group_id',None))
    if is_system_or_anonymous_user(user): return
    text=msg_text(msg); protected=is_protected_user(user.id)
    if getattr(msg,'new_chat_members',None):
        for j in msg.new_chat_members:
            if is_system_or_anonymous_user(j): continue
            if getattr(j,'is_bot',False) and not is_protected_user(j.id):
                try: await ctx.bot.ban_chat_member(GROUP_ID,j.id)
                except Exception as e: print(f'BOT JOIN BAN ERROR: {e}',flush=True)
            else: await ban_for_username(ctx,j)
        return
    keys=[]; hashable=False
    if has_media(msg):
        keys,hashable=await media_keys(ctx,msg)
        banned=await any_banned(keys)
        if banned:
            print(f'BANNED HASH MATCH user={user.id} hash={banned}',flush=True)
            if protected: await delete_safe(ctx,GROUP_ID,msg.message_id); return
            await punish_ban(update,ctx,'média interdit',MSG_GENERIC_FORBIDDEN); return
    if text and not protected:
        async with db_pool.acquire() as con: hard=await con.fetch('SELECT word FROM banned_words_hard')
        for r in hard:
            w=(r['word'] or '').strip().lower()
            if w and contains_forbidden_token(text,w): print(f'WORD BAN MATCH user={user.id} word={w}',flush=True); await punish_ban(update,ctx,'mot banni',MSG_GENERIC_FORBIDDEN); await alert_ban(ctx,user,'mot banni dans le message',w); return
        async with db_pool.acquire() as con: words=await con.fetch('SELECT word FROM banned_words')
        for r in words:
            w=(r['word'] or '').strip().lower()
            if w and contains_forbidden_token(text,w): await punish_word(update,ctx,w); return
    if not protected:
        if has_external_link(msg): await punish_ban(update,ctx,'lien interdit',MSG_LINK_FORBIDDEN); return
        if is_forwarded(msg) and not has_media(msg): await punish_ban(update,ctx,'forward texte interdit',MSG_FORWARD_FORBIDDEN); return
        if is_live_or_story(msg): await punish_ban(update,ctx,'live/story interdit',MSG_GENERIC_FORBIDDEN); return
    if not protected and has_media(msg) and '@' in (getattr(msg,'caption','') or ''):
        await delete_safe(ctx,GROUP_ID,msg.message_id); c=await violation(user.id,'media_mention_ad'); await restrict_days(ctx,user.id,max(1,min(c,30)),'media_mention_ad'); await inc_counter('session_mutes'); await warning(ctx,MSG_PUB_ATTEMPT); return
    if await get_setting('participation','on')=='on' and not protected and not await has_participated(user.id):
        if not has_media(msg): await delete_safe(ctx,GROUP_ID,msg.message_id); await inc_counter('session_deletions'); print(f'PARTICIPATION REQUIRED TRIGGERED user={user.id}',flush=True); await participation_warning(ctx,user); await add_danger(user.id,1,'message avant participation'); return
    if has_media(msg) and keys:
        existing=await any_existing(keys)
        if existing: await delete_safe(ctx,GROUP_ID,msg.message_id); await inc_counter('session_deletions'); await warning(ctx,MSG_REPOST); await add_danger(user.id,2,'repost média'); return
    if await get_setting('participation','on')=='on' and not protected and has_media(msg) and hashable and keys and not await has_participated(user.id): await mark_participated(user,keys[0])
    if has_media(msg) and keys: await record_media(keys,user.id,GROUP_ID,msg.message_id,media_type(msg)); await rediffuse(ctx,msg)
async def chat_member_update(update,ctx):
    cm=update.chat_member
    if not cm: return
    user=cm.new_chat_member.user
    if cm.new_chat_member.status in ('member','restricted'): await ban_for_username(ctx,user)

# rediffusion
async def validate_rediff(ctx):
    if not REDIFFUSION_GROUP_ID: return False,'❌ REDIFFUSION_GROUP_ID n’est pas configuré.'
    try:
        me=await ctx.bot.get_me(); m=await ctx.bot.get_chat_member(REDIFFUSION_GROUP_ID,me.id)
        return (m.status in ('administrator','creator')),('✅ Rediffusion connectée.' if m.status in ('administrator','creator') else '❌ Le bot doit être admin dans le groupe de rediffusion.')
    except Exception as e: return False,f'❌ Rediffusion non connectée : {e}'
async def rediffuse(ctx,msg):
    if await get_setting('rediffusion_enabled','off')!='on' or not REDIFFUSION_GROUP_ID: return
    try: await ctx.bot.copy_message(REDIFFUSION_GROUP_ID,GROUP_ID,msg.message_id); print(f'REDIFFUSION COPY OK msg={msg.message_id}',flush=True)
    except Exception as e: print(f'REDIFFUSION COPY ERROR msg={msg.message_id}: {e}',flush=True)

# panels (clean minimal)
def back(): return InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Retour',callback_data='info')]])
async def main_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton('🚫 Mots interdits',callback_data='words_menu')],[InlineKeyboardButton('⛔ Mots bannis',callback_data='hard_menu')],[InlineKeyboardButton('👤 Usernames interdits',callback_data='users_menu')],[InlineKeyboardButton('📣 Publicité partage',callback_data='share_menu')],[InlineKeyboardButton('📢 Broadcast privé',callback_data='broadcast_set')],[InlineKeyboardButton('📡 Rediffusion ON/OFF',callback_data='toggle_rediff')],[InlineKeyboardButton('🔇 Sanctions visibles ON/OFF',callback_data='toggle_silent')],[InlineKeyboardButton('🏆 Classement ON/OFF',callback_data='toggle_leaderboard')]])
async def st_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton('👀 Voir mots interdits',callback_data='st_words')],[InlineKeyboardButton('➕ Ajouter mot interdit',callback_data='st_add')],[InlineKeyboardButton('📊 Stats 7 jours',callback_data='st_stats7')],[InlineKeyboardButton('📈 Historique complet',callback_data='st_statsall')]])
async def panel_text(): return f'🛠️ Panel admin\n\nParticipation {await get_setting("participation","on")}\nMessages visibles {await get_setting("silent_sanctions","on")}\nRediffusion {await get_setting("rediffusion_enabled","off")}\nClassement {await get_setting("leaderboard_enabled","on")}\n\nVersion: {APP_VERSION}'
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
async def callbacks(update,ctx):
    q=update.callback_query
    try: await q.answer()
    except Exception: pass
    u=q.from_user; data=q.data
    if data=='st_words' and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await q.edit_message_text(await list_values('banned_words','word'),reply_markup=await st_kb()); return
    if data=='st_add' and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await set_state(u.id,'st_add'); await q.edit_message_text('Envoie le mot interdit.',reply_markup=back()); return
    if data in ('st_stats7','st_statsall') and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await q.edit_message_text(await trusted_stats(ctx,7 if data=='st_stats7' else None),reply_markup=await st_kb()); return
    if not is_admin(u.id): return
    if data=='info': await q.edit_message_text(await panel_text(),reply_markup=await main_kb()); return
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
    if data=='share_menu': await q.edit_message_text('Publicité partage',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✏️ Texte',callback_data='share_text')],[InlineKeyboardButton('🖼️ Image',callback_data='share_photo')],[InlineKeyboardButton('📣 Publier',callback_data='share_publish')],[InlineKeyboardButton('⬅️ Retour',callback_data='info')]])); return
    if data in ('share_text','share_photo','broadcast_set'): await set_state(u.id,data); await q.edit_message_text('Envoie maintenant.',reply_markup=back()); return
    if data=='share_publish': await publish_share(ctx); await q.edit_message_text('✅ Pub publiée',reply_markup=await main_kb()); return
async def private_admin(update,ctx):
    u=update.effective_user; msg=update.message; await track_private(u); st=await get_state(u.id)
    if not st: return
    state=st['state']; text=(msg.text or msg.caption or '').strip()
    if state=='st_add' and (u.id in SUPER_TRUSTED_IDS or is_admin(u.id)): await add_value('banned_words','word',text); await set_state(u.id,None); await msg.reply_text('✅ Ajouté.'); return
    if not is_admin(u.id): return
    maps={'words_add':('banned_words','word','add'),'hard_add':('banned_words_hard','word','add'),'users_add':('forbidden_usernames','pattern','add'),'words_del':('banned_words','word','del'),'hard_del':('banned_words_hard','word','del'),'users_del':('forbidden_usernames','pattern','del')}
    if state in maps:
        t,c,a=maps[state]; await (add_value(t,c,text) if a=='add' else del_value(t,c,text)); await set_state(u.id,None); await msg.reply_text('✅ OK.'); return
    if state=='share_text': await set_setting('share_pub_text',text); await set_state(u.id,None); await msg.reply_text('✅ OK.'); return
    if state=='share_photo' and msg.photo: await set_setting('share_pub_photo_file_id',msg.photo[-1].file_id); await set_state(u.id,None); await msg.reply_text('✅ OK.'); return
    if state=='broadcast_set': await set_state(u.id,None); await msg.reply_text(f'✅ Envoyé à {await broadcast(ctx,text)} utilisateurs.'); return
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

def build_app():
    if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN manquant')
    app=Application.builder().token(BOT_TOKEN).build(); app.post_init=lambda app:init_db()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('supprime',trusted_supprime,filters=filters.Chat(GROUP_ID))); app.add_handler(CommandHandler('supprimer',trusted_supprime,filters=filters.Chat(GROUP_ID)))
    app.add_handler(CommandHandler('pasfr',trusted_pasfr,filters=filters.Chat(GROUP_ID))); app.add_handler(CommandHandler('ban',trusted_ban,filters=filters.Chat(GROUP_ID))); app.add_handler(CommandHandler('pedo',trusted_pedo,filters=filters.Chat(GROUP_ID)))
    app.add_handler(CallbackQueryHandler(callbacks)); app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,private_admin))
    app.add_handler(MessageHandler(filters.Chat(GROUP_ID) & filters.COMMAND,handle_group_command)); app.add_handler(MessageHandler(filters.Chat(GROUP_ID) & ~filters.COMMAND,handle_group_message))
    app.add_handler(ChatMemberHandler(chat_member_update,ChatMemberHandler.CHAT_MEMBER)); return app

def main(): print(f'STARTING {APP_VERSION}',flush=True); build_app().run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=='__main__': main()
