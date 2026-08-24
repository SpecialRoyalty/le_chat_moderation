import re
import time
from datetime import datetime
from aiogram.types import User as TgUser
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import User


def display_name(u):
    if getattr(u, 'username', None):
        return '@' + u.username
    return (getattr(u, 'full_name', '') or 'Utilisateur').strip()


def anon_name(username: str | None, full_name: str = ''):
    name = ('@' + username) if username else (full_name or 'membre')
    if len(name) <= 3:
        return name[0] + '*'
    return name[:3] + '****'


def is_gibberish(name: str):
    n = re.sub(r'[^A-Za-z]', '', name or '')
    if len(n) < 4:
        return False
    if re.search(r'(.)\1{3,}', n):
        return True
    vowels = sum(c.lower() in 'aeiouy' for c in n)
    ratio = vowels / len(n)
    return ratio < 0.18 or ratio > 0.82 or bool(
        re.match(r'^[A-Z]?[a-z]{1,2}[a-z]{1,2}\s+[A-Z]?[a-z]{1,4}$', name or '')
    )


# last_seen n'a pas besoin d'être écrit en base plusieurs fois par seconde pour
# un membre actif. Les changements de pseudo/nom, eux, forcent immédiatement
# une mise à jour.
_USER_TOUCH_SECONDS = 30.0
_USER_TOUCH_CACHE: dict[int, tuple[float, str | None, str, bool, bool]] = {}


async def upsert_user(tgu: TgUser, force: bool = False):
    s = get_settings()
    username = tgu.username
    full_name = tgu.full_name or ''
    is_admin = tgu.id in s.admin_id_set
    is_trusted = tgu.id in s.trusted_id_set or is_admin
    now_mono = time.monotonic()

    cached = _USER_TOUCH_CACHE.get(tgu.id)
    if (
        not force
        and cached
        and now_mono - cached[0] < _USER_TOUCH_SECONDS
        and cached[1:] == (username, full_name, is_admin, is_trusted)
    ):
        return None

    async with SessionLocal() as db:
        u = await db.get(User, tgu.id)
        if not u:
            score = 0
            if not username:
                score += 10
            if is_gibberish(full_name):
                score += 20
            u = User(
                id=tgu.id,
                username=username,
                full_name=full_name,
                suspect_score=score,
            )
            db.add(u)
        u.username = username
        u.full_name = full_name
        u.last_seen = datetime.utcnow()
        u.is_admin = is_admin
        u.is_trusted = is_trusted
        await db.commit()

    _USER_TOUCH_CACHE[tgu.id] = (now_mono, username, full_name, is_admin, is_trusted)
    return u


async def protected(user_id: int):
    return user_id in get_settings().all_admin_ids
