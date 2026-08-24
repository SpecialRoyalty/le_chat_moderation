from __future__ import annotations

import asyncio
import time

from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import Setting
from app.config import get_settings

# Les réglages sont lus sur quasiment chaque message. Sans cache, une simple
# photo peut provoquer plusieurs allers-retours PostgreSQL avant même la
# modération. Les écritures passent toutes par set_value(), qui met le cache à
# jour immédiatement. Le TTL garde malgré tout la possibilité de voir une
# modification manuelle de la base sans redémarrer le bot.
_CACHE_TTL_SECONDS = 10.0
_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_LOCK = asyncio.Lock()


def invalidate_cache(key: str | None = None) -> None:
    if key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key, None)


async def get_value(key: str, default: str = ''):
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[1] <= _CACHE_TTL_SECONDS:
        return cached[0]

    # Empêche plusieurs messages simultanés de recharger la même clé au même
    # instant lorsque le TTL vient d'expirer.
    async with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get(key)
        if cached and now - cached[1] <= _CACHE_TTL_SECONDS:
            return cached[0]
        async with SessionLocal() as db:
            obj = await db.get(Setting, key)
            if obj is None:
                return default
            value = obj.value
        _CACHE[key] = (value, now)
        return value


async def set_value(key: str, value: str):
    async with SessionLocal() as db:
        obj = await db.get(Setting, key)
        if not obj:
            obj = Setting(key=key, value=value)
            db.add(obj)
        else:
            obj.value = value
        await db.commit()
    _CACHE[key] = (value, time.monotonic())


async def set_values(values: dict[str, str]):
    """Écrit plusieurs réglages avec un seul commit PostgreSQL."""
    if not values:
        return
    async with SessionLocal() as db:
        rows = list((await db.execute(select(Setting).where(Setting.key.in_(list(values.keys()))))).scalars().all())
        by_key = {row.key: row for row in rows}
        for key, value in values.items():
            row = by_key.get(key)
            if row is None:
                db.add(Setting(key=key, value=value))
            else:
                row.value = value
        await db.commit()
    now = time.monotonic()
    for key, value in values.items():
        _CACHE[key] = (value, now)


async def init_defaults():
    s = get_settings()
    defaults = {
        'auto_enabled': str(s.auto_schedule_enabled).lower(),
        'time_slot': s.default_time_slot,
        'vote_goal': str(s.default_vote_goal),
        'group_open': 'false',
        'status_message_id': '',
        'active_session_id': '0',
        'rules_text': 'Respectez les règles. Pas de liens, pas de mentions, pas de commandes.',
        'ads_text': '📢 Publicité',
        'ads_enabled': 'true',
        'repost_enabled': 'false',
        'last_repost_blocked_at': 'jamais',
        'last_repost_blocked_user': '',
        'weekly_top_started': 'false',
        'weekly_top_start': '',
        'manual_security_warned_at': '',
        'manual_opened_at': '',
    }

    # Une seule lecture + un seul commit au démarrage, au lieu de 2 requêtes
    # par clé de configuration.
    async with SessionLocal() as db:
        existing_rows = list((await db.execute(select(Setting))).scalars().all())
        existing = {row.key: row.value for row in existing_rows}
        changed = False
        for key, value in defaults.items():
            if key not in existing or existing[key] == '':
                row = next((r for r in existing_rows if r.key == key), None)
                if row is None:
                    db.add(Setting(key=key, value=value))
                else:
                    row.value = value
                existing[key] = value
                changed = True
        if changed:
            await db.commit()

    now = time.monotonic()
    for key, value in existing.items():
        _CACHE[key] = (value, now)


async def is_open():
    return (await get_value('group_open', 'false')) == 'true'


async def set_open(v: bool):
    await set_value('group_open', 'true' if v else 'false')


async def auto_enabled():
    return (await get_value('auto_enabled', 'true')) == 'true'


async def time_slot():
    return await get_value('time_slot', get_settings().default_time_slot)


async def vote_goal():
    return int(await get_value('vote_goal', str(get_settings().default_vote_goal)))
