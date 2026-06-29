from aiogram import Router, Bot, F
from aiogram.types import Message, ChatMemberUpdated
from app.config import get_settings
from app.services.users import upsert_user
from app.services.actions import trusted_command
from app.services.moderation import moderate_message
from app.services.invites import on_join
from app.services.state import track
from app.services import settings as st

router=Router()

@router.my_chat_member()
async def bot_added(event:ChatMemberUpdated, bot:Bot):
    s=get_settings()
    allowed=[s.main_group_id]
    if s.log_group_id:
        allowed.append(s.log_group_id)
    if event.chat.id not in allowed:
        for aid in s.admin_id_set:
            try: await bot.send_message(aid,f'🚨 Tentative de raccordement pirate\nGroupe: {event.chat.title} ({event.chat.id})')
            except Exception: pass
        try: await bot.send_message(event.chat.id,'Tentative de raccordement pirate détectée 😭')
        except Exception: pass
        try: await bot.leave_chat(event.chat.id)
        except Exception: pass

@router.chat_member()
async def member_update(event:ChatMemberUpdated, bot:Bot):
    await on_join(event, bot)

@router.message(F.new_chat_members | F.left_chat_member)
async def delete_service_join_leave(msg:Message, bot:Bot):
    """Supprime immédiatement les notifications Telegram d’entrée/sortie du groupe principal.

    Exception volontaire : pendant la justice populaire, on garde les notifications
    de sortie afin que les suppressions restent visibles puis traçables/nettoyables.
    """
    s = get_settings()
    if msg.chat.id != s.main_group_id:
        return
    keep_removed = bool(msg.left_chat_member and await st.get_value('justice_running','false') == 'true')
    if keep_removed:
        await track(msg.chat.id, msg.message_id, getattr(msg.left_chat_member, 'id', None), 'justice_removed_notification', False)
        return
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

@router.message()
async def all_messages(msg:Message, bot:Bot):
    if msg.from_user: await upsert_user(msg.from_user)
    if msg.chat.id == get_settings().main_group_id and (msg.new_chat_members or msg.left_chat_member):
        keep_removed = bool(msg.left_chat_member and await st.get_value('justice_running','false') == 'true')
        if keep_removed:
            await track(msg.chat.id, msg.message_id, getattr(msg.left_chat_member, 'id', None), 'justice_removed_notification', False)
        else:
            try: await bot.delete_message(msg.chat.id, msg.message_id)
            except Exception: pass
        return
    if msg.chat.type=='private':
        return
    if msg.text and await trusted_command(bot,msg): return
    await moderate_message(bot,msg)
