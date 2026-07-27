from sqlalchemy import select, update
from aiogram import Bot
from aiogram.types import Message
from app.db.session import SessionLocal
from app.db.models import TrackedMessage, TrustedAction, MediaHash, MediaFingerprint
from app.config import get_settings
from app.services.moderation import ban, restrict, delete
from app.services.hashban import album_messages_for, ban_hashes_from_messages, hash_diagnostic


async def _notify_admins(bot: Bot, text: str):
    for admin_id in get_settings().admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def trusted_command(bot: Bot, msg: Message):
    if not msg.from_user or msg.from_user.id not in get_settings().all_admin_ids:
        return False

    cmd = (msg.text or '').split()[0].lower().split('@')[0]
    if cmd not in ['/supprime', '/mineur', '/pasfr', '/pedo', '/hashdemande', '/clean', '/info']:
        return False

    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    target = msg.reply_to_message
    async with SessionLocal() as db:
        db.add(TrustedAction(
            trusted_user_id=msg.from_user.id,
            trusted_username=msg.from_user.username or msg.from_user.full_name or '',
            command=cmd,
            target_user_id=target.from_user.id if target and target.from_user else None,
        ))
        await db.commit()

    if cmd == '/clean':
        n = 50
        parts = (msg.text or '').split()
        if len(parts) > 1 and parts[1].isdigit(): n = min(int(parts[1]), 300)
        for mid in range(msg.message_id - 1, max(msg.message_id - n, 0), -1):
            try: await bot.delete_message(msg.chat.id, mid)
            except Exception: pass
        return True

    if cmd == '/info' and target and target.from_user:
        await bot.send_message(msg.from_user.id, f'👤 {target.from_user.full_name}\n@{target.from_user.username or "sans username"}\nID interne masqué dans le groupe.')
        return True

    if cmd == '/hashdemande':
        if not target:
            await bot.send_message(msg.from_user.id, 'Réponds à un média avec /hashdemande.')
        else:
            await bot.send_message(msg.from_user.id, await hash_diagnostic(bot, target))
        return True

    if not target or not target.from_user:
        return True

    if cmd == '/supprime':
        await delete(bot, target)
    elif cmd == '/mineur':
        await delete(bot, target); await restrict(bot, msg.chat.id, target.from_user.id, 1)
    elif cmd == '/pasfr':
        await delete(bot, target); await restrict(bot, msg.chat.id, target.from_user.id, 1)
    elif cmd == '/pedo':
        uid = target.from_user.id
        album = album_messages_for(target)

        # Hash avant suppression : chaque élément connu de l'album reçoit ID + SHA + empreintes visuelles.
        report = await ban_hashes_from_messages(album, bot)

        async with SessionLocal() as db:
            await db.execute(update(MediaHash).where(MediaHash.user_id == uid).values(banned=True))
            await db.execute(update(MediaFingerprint).where(MediaFingerprint.user_id == uid).values(banned=True))

            tracked = list((await db.execute(select(TrackedMessage).where(
                TrackedMessage.chat_id == msg.chat.id,
                TrackedMessage.user_id == uid,
                TrackedMessage.deleted.is_(False),
            ))).scalars().all())
            for tracked_message in tracked:
                try:
                    await bot.delete_message(tracked_message.chat_id, tracked_message.message_id)
                    tracked_message.deleted = True
                except Exception:
                    pass
            await db.commit()

        await ban(bot, msg.chat.id, uid)
        await _notify_admins(bot, report.admin_text())

    return True
