from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from app.config import get_settings
from app.services import settings as st
from app.services.ads import send_random_ad
from app.services.invites import send_invite_ad, top_text, validate_invites
from app.services.network import (
    active_chat_id,
    default_target_chat_id,
    group_health_check,
    selected_chat_id,
)
from app.services.session_ops import security_close_if_manual, set_group_open
from app.services.state import ensure_all_status_messages, track, vote_count
from app.utils.time import in_slot


async def tick(bot: Bot):
    await ensure_all_status_messages(bot, recreate_on_change=True)
    if not await st.auto_enabled():
        return

    active = await active_chat_id()
    if active:
        slot = await st.group_time_slot(active)
        if not in_slot(slot, get_settings().timezone):
            await set_group_open(bot, False, 'auto', chat_id=active)
        return

    selected = await selected_chat_id()
    if not selected:
        return
    slot = await st.group_time_slot(selected)
    goal = await st.group_vote_goal(selected)
    votes = await vote_count(selected)
    if in_slot(slot, get_settings().timezone) and votes >= goal:
        await set_group_open(bot, True, 'auto', chat_id=selected)


async def rules_tick(bot: Bot, force: bool = False, chat_id: int | None = None):
    target = chat_id or await active_chat_id()
    if not target and force:
        target = await default_target_chat_id()
    if not target:
        return None
    if not force and not await st.is_open(target):
        return None

    old = await st.group_get_value(target, 'rules_message_id', '', inherit_global=False)
    try:
        if old:
            await bot.delete_message(target, int(old))
    except Exception:
        pass
    message = await bot.send_message(target, await st.group_rules_text(target))
    await track(target, message.message_id, None, 'rules', False)
    await st.group_set_values(target, {
        'rules_message_id': str(message.message_id),
        'last_rules_sent_at': datetime.utcnow().isoformat(timespec='seconds'),
    })
    return message.message_id


async def top_tick(bot: Bot):
    target = await active_chat_id()
    if not target:
        return
    text = await top_text()
    if 'Aucune statistique' in text:
        return
    message = await bot.send_message(target, text)
    await track(target, message.message_id, None, 'top', False)
    await st.group_set_value(target, 'last_top_sent_at', datetime.utcnow().isoformat(timespec='seconds'))


def start_scheduler(bot: Bot):
    scheduler = AsyncIOScheduler(
        timezone=get_settings().timezone,
        job_defaults={
            'coalesce': True,
            'max_instances': 1,
            'misfire_grace_time': 30,
        },
    )
    scheduler.add_job(tick, 'interval', minutes=1, args=[bot], id='tick')
    scheduler.add_job(validate_invites, 'interval', minutes=1, args=[bot], id='invite_validate')
    scheduler.add_job(rules_tick, 'interval', minutes=30, args=[bot], id='rules')
    scheduler.add_job(send_random_ad, 'cron', hour='22,0', minute='45,5', args=[bot], id='random_ads')
    scheduler.add_job(top_tick, 'cron', hour='0', minute='40', args=[bot], id='top')
    scheduler.add_job(send_invite_ad, 'cron', hour='23', minute='25', args=[bot], id='invite_ad')
    scheduler.add_job(security_close_if_manual, 'interval', minutes=5, args=[bot], id='security_close')
    scheduler.add_job(group_health_check, 'interval', minutes=5, args=[bot], id='network_health')
    scheduler.start()
    return scheduler
