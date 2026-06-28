from sqlalchemy import select
from aiogram import Bot
from aiogram.types import Message
from app.db.session import SessionLocal
from app.db.models import TrackedMessage, TrustedAction
from app.config import get_settings
from app.services.moderation import ban, restrict, record_media, delete
from app.services.state import log_error

async def trusted_command(bot:Bot,msg:Message):
    if not msg.from_user or msg.from_user.id not in get_settings().all_admin_ids: return False
    cmd=(msg.text or '').split()[0].lower()
    if cmd not in ['/supprime','/mineur','/pasfr','/pedo','/clean','/info']: return False
    try: await bot.delete_message(msg.chat.id,msg.message_id)
    except Exception: pass
    target=msg.reply_to_message
    async with SessionLocal() as db:
        db.add(TrustedAction(trusted_user_id=msg.from_user.id,trusted_username=msg.from_user.username or msg.from_user.full_name or '',command=cmd,target_user_id=target.from_user.id if target and target.from_user else None)); await db.commit()
    if cmd=='/clean':
        n=50
        parts=(msg.text or '').split()
        if len(parts)>1 and parts[1].isdigit(): n=min(int(parts[1]),300)
        for mid in range(msg.message_id-1, max(msg.message_id-n,0), -1):
            try: await bot.delete_message(msg.chat.id,mid)
            except Exception: pass
        return True
    if cmd=='/info' and target and target.from_user:
        await bot.send_message(msg.from_user.id, f'👤 {target.from_user.full_name}\n@{target.from_user.username or "sans username"}\nID interne masqué dans le groupe.')
        return True
    if not target: return True
    if cmd=='/supprime': await delete(bot,target)
    elif cmd=='/mineur': await delete(bot,target); await restrict(bot,msg.chat.id,target.from_user.id,1)
    elif cmd=='/pasfr': await delete(bot,target); await restrict(bot,msg.chat.id,target.from_user.id,1)
    elif cmd=='/pedo':
        uid=target.from_user.id if target.from_user else None
        if uid:
            await ban(bot,msg.chat.id,uid)
            async with SessionLocal() as db:
                res=await db.execute(select(TrackedMessage).where(TrackedMessage.chat_id==msg.chat.id,TrackedMessage.user_id==uid,TrackedMessage.deleted==False))
                for tm in res.scalars().all():
                    try: await bot.delete_message(tm.chat_id,tm.message_id); tm.deleted=True
                    except Exception: pass
                await db.commit()
            await record_media(target,banned=True)
    return True
