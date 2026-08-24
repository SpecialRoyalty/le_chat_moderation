import asyncio
from sqlalchemy import select, update
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from app.db.session import SessionLocal
from app.db.models import TrackedMessage, TrustedAction, MediaHash, MediaFingerprint
from app.config import get_settings
from app.services.moderation import ban, restrict, delete
from app.services.hashban import album_messages_for, ban_hashes_from_messages, hash_diagnostic

TRUSTED_COMMANDS = {'/supprime', '/mineur', '/pasfr', '/pedo', '/hashdemande', '/clean', '/info'}


async def _notify_admins(bot: Bot, text: str):
    await asyncio.gather(
        *(bot.send_message(admin_id, text) for admin_id in get_settings().admin_id_set),
        return_exceptions=True,
    )


async def _log_action(msg: Message, cmd: str, target: Message | None):
    """Journal DB hors du chemin critique Telegram."""
    try:
        async with SessionLocal() as db:
            db.add(TrustedAction(
                trusted_user_id=msg.from_user.id,
                trusted_username=msg.from_user.username or msg.from_user.full_name or '',
                command=cmd,
                target_user_id=target.from_user.id if target and target.from_user else None,
            ))
            await db.commit()
    except Exception as exc:
        print(f'[TRUSTED] log action failed cmd={cmd}: {type(exc).__name__}: {exc}')


async def _delete_many(bot: Bot, chat_id: int, message_ids: list[int], concurrency: int = 8):
    """Supprime plusieurs messages en parallèle sans inonder l'API Telegram."""
    sem = asyncio.Semaphore(concurrency)

    async def one(mid: int):
        async with sem:
            try:
                await bot.delete_message(chat_id, mid)
                return True
            except TelegramBadRequest as exc:
                # Déjà supprimé = objectif atteint, ne pas le retenter plus tard.
                if 'message to delete not found' in str(exc).lower():
                    return True
                return False
            except Exception:
                return False

    if not message_ids:
        return []
    return await asyncio.gather(*(one(mid) for mid in message_ids))


async def trusted_command(bot: Bot, msg: Message):
    if not msg.from_user:
        return False

    text = msg.text or ''
    if not text.startswith('/'):
        return False

    cmd = text.split()[0].lower().split('@')[0]
    if cmd not in TRUSTED_COMMANDS:
        return False

    s = get_settings()
    if msg.from_user.id not in s.all_admin_ids:
        print(f'[TRUSTED] REFUSED uid={msg.from_user.id} cmd={cmd} trusted={sorted(s.trusted_id_set)} admins={sorted(s.admin_id_set)}')
        return False

    target = msg.reply_to_message

    # La disparition de la commande est prioritaire et ne dépend pas du journal DB.
    command_delete_task = asyncio.create_task(delete(bot, msg))

    if cmd == '/clean':
        n = 50
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            n = min(int(parts[1]), 300)
        ids = list(range(msg.message_id - 1, max(msg.message_id - n, 0), -1))
        await asyncio.gather(command_delete_task, _delete_many(bot, msg.chat.id, ids, 8))
        await _log_action(msg, cmd, target)
        return True

    if cmd == '/info':
        await command_delete_task
        if target and target.from_user:
            await bot.send_message(
                msg.from_user.id,
                f'👤 {target.from_user.full_name}\n@{target.from_user.username or "sans username"}\nID interne masqué dans le groupe.'
            )
        await _log_action(msg, cmd, target)
        return True

    if cmd == '/hashdemande':
        await command_delete_task
        if not target:
            await bot.send_message(msg.from_user.id, 'Réponds à un média avec /hashdemande.')
        else:
            await bot.send_message(msg.from_user.id, await hash_diagnostic(bot, target))
        await _log_action(msg, cmd, target)
        return True

    if not target or not target.from_user:
        await command_delete_task
        await _log_action(msg, cmd, target)
        return True

    target_uid = target.from_user.id

    if cmd == '/supprime':
        # Commande et cible supprimées en parallèle : aucune DB avant l'action visible.
        await asyncio.gather(command_delete_task, delete(bot, target))
        await _log_action(msg, cmd, target)
        return True

    if cmd in ('/mineur', '/pasfr'):
        # Suppression immédiate; restriction lancée en parallèle avec la suppression de la commande.
        await asyncio.gather(command_delete_task, delete(bot, target), restrict(bot, msg.chat.id, target_uid, 1))
        await _log_action(msg, cmd, target)
        return True

    if cmd == '/pedo':
        uid = target_uid
        album = album_messages_for(target)

        # Priorité absolue à la modération visible : ban + suppression du média ciblé + commande.
        # Le hashing perceptuel (potentiellement lourd) vient ENSUITE.
        await asyncio.gather(command_delete_task, delete(bot, target), ban(bot, msg.chat.id, uid))

        report = await ban_hashes_from_messages(album, bot)

        # Mise à jour DB rapide, puis fermeture de la connexion AVANT les appels
        # Telegram potentiellement lents.
        async with SessionLocal() as db:
            await db.execute(update(MediaHash).where(MediaHash.user_id == uid).values(banned=True))
            await db.execute(update(MediaFingerprint).where(MediaFingerprint.user_id == uid).values(banned=True))
            tracked_ids = list((await db.execute(select(TrackedMessage.message_id).where(
                TrackedMessage.chat_id == msg.chat.id,
                TrackedMessage.user_id == uid,
                TrackedMessage.deleted.is_(False),
            ))).scalars().all())
            await db.commit()

        results = await _delete_many(bot, msg.chat.id, tracked_ids, 8)
        deleted_ids = [mid for mid, ok in zip(tracked_ids, results) if ok]
        if deleted_ids:
            async with SessionLocal() as db:
                await db.execute(
                    update(TrackedMessage)
                    .where(
                        TrackedMessage.chat_id == msg.chat.id,
                        TrackedMessage.message_id.in_(deleted_ids),
                    )
                    .values(deleted=True)
                )
                await db.commit()

        await _log_action(msg, cmd, target)
        await _notify_admins(bot, report.admin_text())
        return True

    await command_delete_task
    await _log_action(msg, cmd, target)
    return True
