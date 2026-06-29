from __future__ import annotations
import re
from datetime import datetime, timedelta
from sqlalchemy import select
from aiogram import Bot
from aiogram.types import Message
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import WordRule, MediaHash, TrustedAction, User
from app.services.users import protected, display_name
from app.services.state import track, log_error
from app.services.hashban import file_sha256
from app.services import settings as st

def has_link(text:str): return bool(re.search(r'(https?://|t\.me/|www\.|\.com\b|\.net\b|\.io\b)', text or '', re.I))
def has_mention(text:str): return '@' in (text or '')
def has_command(text:str): return (text or '').strip().startswith('/')
def is_media(msg:Message): return bool(msg.photo or msg.video or msg.document or msg.animation or msg.audio or msg.voice or msg.video_note)
def file_ids(msg:Message):
    if msg.photo: return [(msg.photo[-1].file_unique_id,msg.photo[-1].file_id,'photo')]
    if msg.video: return [(msg.video.file_unique_id,msg.video.file_id,'video')]
    if msg.document: return [(msg.document.file_unique_id,msg.document.file_id,'document')]
    if msg.animation: return [(msg.animation.file_unique_id,msg.animation.file_id,'animation')]
    if msg.video_note: return [(msg.video_note.file_unique_id,msg.video_note.file_id,'video_note')]
    return []
async def words(kind):
    async with SessionLocal() as db:
        res=await db.execute(select(WordRule).where(WordRule.kind==kind)); return [x.word.lower() for x in res.scalars().all()]
async def text_has_word(kind,text):
    t=(text or '').lower()
    return any(w and w in t for w in await words(kind))
async def restrict(bot:Bot, chat_id:int, user_id:int, days:int):
    if await protected(user_id): return
    until=datetime.utcnow()+timedelta(days=days)
    try:
        await bot.restrict_chat_member(chat_id,user_id,permissions={'can_send_messages':False},until_date=until)
        async with SessionLocal() as db:
            u=await db.get(User,user_id)
            if u: u.is_restricted=True
            await db.commit()
    except Exception as e: await log_error('restrict',e)
async def ban(bot:Bot, chat_id:int, user_id:int):
    if await protected(user_id): return
    try:
        await bot.ban_chat_member(chat_id,user_id)
        async with SessionLocal() as db:
            u=await db.get(User,user_id)
            if u: u.is_banned=True
            await db.commit()
    except Exception as e: await log_error('ban',e)
async def delete(bot:Bot,msg:Message):
    try: await bot.delete_message(msg.chat.id,msg.message_id)
    except Exception: pass
async def record_media(msg:Message, banned=False):
    for unique,file_id,typ in file_ids(msg):
        async with SessionLocal() as db:
            old=await db.execute(select(MediaHash).where(MediaHash.file_unique_id==unique))
            mh=old.scalar_one_or_none()
            if not mh:
                mh=MediaHash(user_id=msg.from_user.id if msg.from_user else None,file_unique_id=unique,file_id=file_id,media_type=typ,banned=banned); db.add(mh)
            if banned: mh.banned=True
            u=await db.get(User,msg.from_user.id) if msg.from_user else None
            if u and not banned:
                u.media_count+=1; u.last_media_session=int(await st.get_value('active_session_id','0') or '0')
            await db.commit()

async def contains_known_media(msg:Message):
    ids=[x[0] for x in file_ids(msg)]
    if not ids: return False
    async with SessionLocal() as db:
        res=await db.execute(select(MediaHash).where(MediaHash.file_unique_id.in_(ids)))
        return res.scalar_one_or_none() is not None

async def contains_banned_hash(bot:Bot,msg:Message):
    entries=file_ids(msg)
    ids=[x[0] for x in entries]
    for _unique,file_id,_typ in entries:
        sha=await file_sha256(bot,file_id)
        if sha: ids.append(sha)
    if not ids: return False
    async with SessionLocal() as db:
        res=await db.execute(select(MediaHash).where(MediaHash.file_unique_id.in_(ids),MediaHash.banned==True))
        return res.scalar_one_or_none() is not None
async def moderate_message(bot:Bot,msg:Message):
    if not msg.from_user: return
    await track(msg.chat.id,msg.message_id,msg.from_user.id,'message',is_media(msg))
    if msg.chat.id != get_settings().main_group_id: return
    uid=msg.from_user.id; text=msg.text or msg.caption or ''
    trusted=uid in get_settings().trusted_id_set; admin=uid in get_settings().admin_id_set
    if not await st.is_open() and not (trusted or admin):
        await delete(bot,msg); return
    if is_media(msg):
        if await contains_banned_hash(bot,msg):
            await delete(bot,msg); await ban(bot,msg.chat.id,uid); return
        if (await st.get_value('repost_enabled','false'))=='true' and await contains_known_media(msg):
            await delete(bot,msg)
            await st.set_value('last_repost_blocked_at', datetime.utcnow().isoformat(timespec='seconds'))
            await st.set_value('last_repost_blocked_user', str(uid))
            warn=await bot.send_message(msg.chat.id, f'{display_name(msg.from_user)}, média déjà posté : repost interdit.')
            await track(msg.chat.id,warn.message_id,None,'temp',False)
            return
        await record_media(msg)
    # Liens interdits pour tout le monde sauf admins; trusted supprimé sans sanction
    if has_link(text):
        await delete(bot,msg)
        if not (trusted or admin): await ban(bot,msg.chat.id,uid)
        return
    if trusted or admin: return
    if has_command(text):
        await delete(bot,msg); await restrict(bot,msg.chat.id,uid,1); return
    if msg.video_note:
        await delete(bot,msg); await restrict(bot,msg.chat.id,uid,1); return
    if has_mention(text):
        await delete(bot,msg); await restrict(bot,msg.chat.id,uid,2); return
    if await text_has_word('ban',text):
        await delete(bot,msg); await ban(bot,msg.chat.id,uid); return
    if await text_has_word('forbidden',text):
        await delete(bot,msg); await restrict(bot,msg.chat.id,uid,1); return
    if text and not is_media(msg):
        async with SessionLocal() as db:
            u=await db.get(User,uid)
            if u and u.media_count<=0:
                await delete(bot,msg)
                warn=await bot.send_message(msg.chat.id,f'{display_name(msg.from_user)}, envoie d’abord un média avant d’écrire.')
                await track(msg.chat.id,warn.message_id,None,'temp',False)
