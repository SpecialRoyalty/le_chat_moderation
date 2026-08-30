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


async def auto_enabled():
    return (await get_value('auto_enabled', 'true')) == 'true'


async def time_slot():
    return await get_value('time_slot', get_settings().default_time_slot)


async def vote_goal():
    return int(await get_value('vote_goal', str(get_settings().default_vote_goal)))

# ---------------------------------------------------------------------------
# Réglages scoppés par groupe (héritage global si aucune valeur locale)
# ---------------------------------------------------------------------------
def group_key(chat_id: int, key: str) -> str:
    return f'group:{int(chat_id)}:{key}'


async def group_get_value(chat_id: int, key: str, default: str = '', *, inherit_global: bool = True) -> str:
    sentinel = '__GROSCHAT_MISSING__'
    value = await get_value(group_key(chat_id, key), sentinel)
    if value != sentinel:
        return value
    if inherit_global:
        return await get_value(key, default)
    return default


async def group_set_value(chat_id: int, key: str, value: str) -> None:
    await set_value(group_key(chat_id, key), value)


async def group_set_values(chat_id: int, values: dict[str, str]) -> None:
    await set_values({group_key(chat_id, key): value for key, value in values.items()})


async def group_bool(chat_id: int, key: str, default: bool) -> bool:
    raw = await group_get_value(chat_id, key, 'true' if default else 'false')
    return raw == 'true'


async def group_vote_goal(chat_id: int) -> int:
    return int(await group_get_value(chat_id, 'vote_goal', str(get_settings().default_vote_goal)))


async def group_time_slot(chat_id: int) -> str:
    return await group_get_value(chat_id, 'time_slot', get_settings().default_time_slot)


async def group_rules_text(chat_id: int) -> str:
    global_text = await get_value('rules_text', 'Respectez les règles.')
    local_text = await group_get_value(chat_id, 'rules_text_local', '', inherit_global=False)
    if local_text.strip():
        return f'{global_text.strip()}\n\n📍 Règles spécifiques à ce groupe\n{local_text.strip()}'
    return global_text


async def is_open(chat_id: int | None = None):
    # La source de vérité est désormais network_state_test.
    from app.services.network import active_chat_id
    active = await active_chat_id()
    return bool(active and (chat_id is None or int(chat_id) == int(active)))


async def set_open(v: bool):
    # Compatibilité avec d'anciens appels. L'ouverture réelle passe par
    # session_ops.set_group_open().
    await set_value('group_open', 'true' if v else 'false')
