import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramServerError

from app.config import get_settings
from app.db.session import init_db
from app.handlers import admin, callbacks, group
from app.scheduler import start_scheduler
from app.services import settings as st
from app.services.network import bootstrap_network
from app.services.settings import init_defaults
from app.services.state import ensure_all_status_messages


async def get_me_with_retry(bot: Bot, attempts: int = 10):
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            return await bot.get_me()
        except (TelegramServerError, TelegramNetworkError) as exc:
            if attempt >= attempts:
                raise
            logging.warning(
                'Telegram indisponible au démarrage (tentative %s/%s): %s. Nouvel essai dans %ss.',
                attempt, attempts, exc, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def main():
    logging.basicConfig(level=logging.INFO)
    s = get_settings()

    await init_db()
    await init_defaults()

    bot = Bot(s.bot_token)
    try:
        me = await get_me_with_retry(bot)
        logging.info('Bot Telegram joignable: @%s (id=%s)', me.username, me.id)
        await st.set_values({
            'bot_id': str(me.id),
            'bot_username': me.username or '',
        })

        # Importe automatiquement l'ancien MAIN_GROUP_ID s'il existe, puis le
        # registre réseau devient la source de vérité pour tous les groupes.
        await bootstrap_network(bot)

        dp = Dispatcher()
        dp.include_router(admin.router)
        dp.include_router(callbacks.router)
        dp.include_router(group.router)

        start_scheduler(bot)

        try:
            await ensure_all_status_messages(bot)
        except Exception:
            logging.exception('network status init failed')

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
