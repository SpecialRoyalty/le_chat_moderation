from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from app.config import get_settings
from app.services.state import add_vote, ensure_status_message, vote_count
from app.services import settings as st
from app.services.session_ops import set_group_open
from app.utils.time import in_slot
from app.services.invites import send_invite_private

router=Router()

@router.callback_query(F.data=='vote_open')
async def vote(cb:CallbackQuery,bot:Bot):
    added=await add_vote(cb.message.chat.id,cb.from_user.id)
    goal=await st.vote_goal(); votes=await vote_count(cb.message.chat.id)
    if votes>=goal and in_slot(await st.time_slot(), get_settings().timezone) and not await st.is_open():
        await set_group_open(bot,True,'auto_vote')
    else:
        await ensure_status_message(bot,cb.message.chat.id)
    await cb.answer('Vote pris en compte ✅' if added else 'Vote déjà compté ✅')

@router.callback_query(F.data=='invite_private')
async def invite_private(cb:CallbackQuery, bot:Bot):
    try:
        await send_invite_private(bot, cb.from_user.id)
        await cb.answer('Lien envoyé en privé ✅')
    except (TelegramForbiddenError, TelegramBadRequest):
        username=get_settings().public_bot_username.strip().lstrip('@')
        if username: await cb.answer(url=f'https://t.me/{username}?start=invite')
        else: await cb.answer('Démarre le bot en privé puis reclique.', show_alert=True)
