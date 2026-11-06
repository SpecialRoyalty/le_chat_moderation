"""Group-scoped routing for one Telegram bot process and independent PostgreSQL schemas.

Every managed supergroup gets an isolated schema so the mature V6 SQL statements
keep working. A single process polls Telegram; never launch a polling process per
chat with the same token. ContextVar isolates concurrent PTB update/job tasks.
"""
from __future__ import annotations

from contextvars import ContextVar
import re
from dataclasses import dataclass

_ACTIVE_GROUP: ContextVar[int | None] = ContextVar('guard_active_group', default=None)


def parse_group_ids(value: str) -> tuple[int, ...]:
    ids = []
    for raw in value.replace(';', ',').split(','):
        raw = raw.strip()
        if not raw:
            continue
        if not re.fullmatch(r'-\d{5,20}', raw):
            raise ValueError('GROUP_IDS doit contenir des IDs Telegram négatifs, séparés par des virgules.')
        gid = int(raw)
        if gid not in ids:
            ids.append(gid)
    # Empty config is valid: groups are discovered at runtime.
    return tuple(ids)


def schema_for(chat_id: int, default_id: int) -> str:
    """Keep V6 public schema for the first group; never interpolate raw user strings."""
    return 'public' if chat_id == default_id else f'guard_{abs(int(chat_id))}'


def select_group(chat_id: int, allowed: tuple[int, ...] | list[int]):
    if int(chat_id) not in allowed:
        raise ValueError('Groupe non autorisé')
    return _ACTIVE_GROUP.set(int(chat_id))


def reset_group(token):
    _ACTIVE_GROUP.reset(token)


def current_group(default_id: int) -> int:
    return _ACTIVE_GROUP.get() or default_id


def private_group_selection(data: dict, allowed: tuple[int, ...] | list[int]) -> int:
    gid = int(data.get('guard_selected_group', allowed[0]))
    return gid if gid in allowed else allowed[0]


def vip_payload(group_id: int, kind: str) -> str:
    if kind not in ('vip', 'invite'):
        raise ValueError('Invalid start payload')
    return f'{kind}_{abs(group_id)}'


def parse_vip_payload(payload: str, allowed: tuple[int, ...] | list[int]):
    """Return (kind, chat) only for explicitly allowlisted chat IDs."""
    match = re.fullmatch(r'(vip|invite)_(\d{5,20})', payload)
    if not match:
        return None
    chat_id = -int(match.group(2))
    return (match.group(1), chat_id) if chat_id in allowed else None
