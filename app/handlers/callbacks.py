from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery

from app.config import get_settings
from app.services import settings as st
from app.services.invites import send_invite_private
from app.services.network import active_chat_id, default_target_chat_id, is_enabled_group, selected_chat_id
from app.services.session_ops import set_group_open
from app.services.state import add_vote, ensure_status_message, vote_count
from app.utils.time import in_slot

router = Router()


@router.callback_query(F.data == 'vote_open')
async def vote(cb: CallbackQuery, bot: Bot):
    if not cb.message:
        await cb.answer('Vote invalide.', show_alert=True)
        return
    chat_id = cb.message.chat.id
    selected = await selected_chat_id()
    active = await active_chat_id()
    if active or selected != chat_id or not await is_enabled_group(chat_id):
        await cb.answer('Ce vote n’est plus actif. Le réseau a changé de groupe.', show_alert=True)
        try:
            await ensure_status_message(bot, chat_id)
        except Exception:
            pass
        return

    added = await add_vote(chat_id, cb.from_user.id)
    goal = await st.group_vote_goal(chat_id)
    votes = await vote_count(chat_id)
    slot = await st.group_time_slot(chat_id)
    if votes >= goal and in_slot(slot, get_settings().timezone) and not await st.is_open():
        await set_group_open(bot, True, 'auto_vote', chat_id=chat_id)
    else:
        await ensure_status_message(bot, chat_id)
    await cb.answer('Vote pris en compte ✅' if added else 'Vote déjà compté ✅')


@router.callback_query(F.data == 'invite_private')
async def invite_private(cb: CallbackQuery, bot: Bot):
    target = None
    if cb.message and await is_enabled_group(cb.message.chat.id):
        target = cb.message.chat.id
    if not target:
        target = await default_target_chat_id()
    try:
        await send_invite_private(bot, cb.from_user.id, target)
        await cb.answer('Lien envoyé en privé ✅')
    except (TelegramForbiddenError, TelegramBadRequest):
        username = get_settings().public_bot_username.strip().lstrip('@') or (await st.get_value('bot_username', '')).strip().lstrip('@')
        if username:
            await cb.answer(url=f'https://t.me/{username}?start=invite')
        else:
            await cb.answer('Démarre le bot en privé puis reclique.', show_alert=True)
    except Exception as exc:
        await cb.answer(f'Invitation indisponible : {exc}', show_alert=True)
