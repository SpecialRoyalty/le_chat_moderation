from aiogram import Bot, F, Router
from aiogram.types import ChatMemberUpdated, Message

from app.services.actions import trusted_command
from app.services.hashban import remember_album_message
from app.services.invites import on_join
from app.services.moderation import moderate_message
from app.services.sanctions import capture_manual_admin_ban
from app.services.network import (
    handle_bot_membership_update,
    is_approved_group,
    membership_seen,
    register_seen_group,
    notify_admins,
    approval_keyboard,
)
from app.services.users import upsert_user

router = Router()
_SEEN_MEMBERSHIPS: set[tuple[int, int]] = set()


@router.my_chat_member()
async def bot_added(event: ChatMemberUpdated, bot: Bot):
    await handle_bot_membership_update(event, bot)


@router.chat_member()
async def member_update(event: ChatMemberUpdated, bot: Bot):
    # D'abord mettre à jour l'appartenance locale, puis propager un éventuel
    # ban fait manuellement par un administrateur depuis l'interface Telegram.
    await on_join(event, bot)
    await capture_manual_admin_ban(bot, event)


@router.message(F.new_chat_members | F.left_chat_member)
async def delete_service_join_leave(msg: Message, bot: Bot):
    if not await is_approved_group(msg.chat.id):
        return
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass


@router.message()
async def all_messages(msg: Message, bot: Bot):
    if msg.chat.type == 'private':
        return

    if not await is_approved_group(msg.chat.id):
        # Cas rare : l'update my_chat_member n'a pas encore été consommée.
        try:
            row, should_notify = await register_seen_group(msg.chat)
            if should_notify:
                await notify_admins(
                    bot,
                    f'➕ Groupe détecté par activité\n\n{msg.chat.title or "Sans titre"}\nID : {msg.chat.id}\n\nEn attente d’autorisation.',
                    await approval_keyboard(msg.chat.id),
                )
        except Exception:
            pass
        return

    # Commandes trusted avant les écritures non urgentes.
    if msg.text and await trusted_command(bot, msg):
        return

    if msg.from_user:
        await upsert_user(msg.from_user)
        key = (msg.from_user.id, msg.chat.id)
        if key not in _SEEN_MEMBERSHIPS:
            try:
                await membership_seen(msg.from_user.id, msg.chat.id, joined=False)
                _SEEN_MEMBERSHIPS.add(key)
            except Exception:
                pass

    if msg.new_chat_members or msg.left_chat_member:
        try:
            await bot.delete_message(msg.chat.id, msg.message_id)
        except Exception:
            pass
        return

    remember_album_message(msg)
    allowed = await moderate_message(bot, msg)
    if not allowed:
        return
