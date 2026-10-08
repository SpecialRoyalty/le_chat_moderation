from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import tempfile
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message
from PIL import Image, ImageOps
from sqlalchemy import func, select, update

from app.db.models import GlobalMediaRegistry, MediaBanJob, MediaFingerprint, MediaHash
from app.db.session import SessionLocal
from app.services import settings as st

logger = logging.getLogger(__name__)

# FFmpeg/Pillow restent la partie coûteuse. Une seule analyse CPU lourde à la
# fois évite de rendre Railway inutilisable quand plusieurs vidéos arrivent.
# Le pipeline réduit désormais le travail avant d'entrer dans ce sémaphore.
_MEDIA_ANALYSIS_SEMAPHORE = asyncio.Semaphore(1)
_ALBUM_TTL_SECONDS = 30 * 60
_ALBUM_CACHE: dict[tuple[int, str], tuple[float, list[Message]]] = {}

# L'analyse entrante est volontairement plus légère que la création d'un
# /pedo. Un /pedo construit une empreinte riche (12 images) tandis qu'un média
# normal n'en extrait que 6. Les frames sont cherchées rapidement avec -ss.
_INCOMING_VIDEO_SAMPLE_COUNT = 6
_BAN_VIDEO_SAMPLE_COUNT = 12

# Seuils perceptuels. Le hash-ban est tolérant aux réencodages/recadrages ;
# l'anti-repost est plus strict pour éviter les faux positifs entre deux médias
# simplement ressemblants.
_BANNED_IMAGE_DISTANCE_LIMIT = 10
_BANNED_VIDEO_DISTANCE_LIMIT = 11
_BANNED_VIDEO_MATCH_RATIO = 0.50
_REPOST_IMAGE_DISTANCE_LIMIT = 6
_REPOST_VIDEO_DISTANCE_LIMIT = 7
_REPOST_VIDEO_MATCH_RATIO = 0.67

_BAN_CACHE_TTL_SECONDS = 60.0
_BANNED_FP_INDEX_TTL_SECONDS = 10 * 60.0
_SAFE_FP_INDEX_TTL_SECONDS = 24 * 60 * 60.0
_BANNED_EXACT_CACHE: tuple[float, set[str], set[str]] | None = None  # expiry, ids, sha
_BAN_CAP_CACHE: dict[str, tuple[float, bool, bool]] = {}

# Cache positif uniquement : on ne charge jamais tous les médias SAFE en RAM.
# Les 20k derniers identifiants connus suffisent à éviter énormément de SELECT
# sans faire grossir la mémoire indéfiniment.
_KNOWN_KEY_CACHE: OrderedDict[str, None] = OrderedDict()
_KNOWN_KEY_CACHE_MAX = 20_000


@dataclass
class HashBanReport:
    media_count: int = 0
    exact_keys: int = 0
    sha256_count: int = 0
    perceptual_count: int = 0
    retry_queued: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.exact_keys + self.perceptual_count

    def admin_text(self, title: str = '/PEDO — BLACKLIST CONFIRMÉE') -> str:
        lines = [
            f'🚫 {title}', '',
            f'Médias traités : {self.media_count}',
            f'Empreintes Telegram/SHA : {self.exact_keys}',
            f'SHA256 calculés : {self.sha256_count}',
            f'Empreintes perceptuelles : {self.perceptual_count}',
        ]
        if self.retry_queued:
            lines.append(f'🔁 Reprises automatiques planifiées : {self.retry_queued}')
        if self.errors:
            lines += ['', '⚠️ Analyse partielle :'] + [f'• {e}' for e in self.errors[:8]]
            if self.retry_queued:
                lines.append('Le file_unique_id est déjà bloqué ; SHA/visuel seront retentés automatiquement.')
        else:
            lines += ['', '✅ Blacklist globale enregistrée et vérifiée.']
        return '\n'.join(lines)


@dataclass
class MediaInspection:
    banned: bool = False
    repost: bool = False
    method: str = 'none'
    sha: str | None = None
    fingerprints: list[tuple[str, str, int]] = field(default_factory=list)
    error: str | None = None
    known_unique: bool = False
    known_sha: bool = False
    perceptual_source: str | None = None
    best_distance: int | None = None
    matched_frames: int = 0
    required_frames: int = 0
    ban_generation: int = 0

    def details(self) -> dict:
        return {
            'method': self.method,
            'sha': self.sha,
            'error': self.error,
            'known_unique': self.known_unique,
            'known_sha': self.known_sha,
            'source': self.perceptual_source,
            'best_distance': self.best_distance,
            'matched_frames': self.matched_frames,
            'required_frames': self.required_frames,
            'computed': len(self.fingerprints),
        }


def media_file_entries(msg: Message):
    if msg.photo:
        item = msg.photo[-1]
        return [(item.file_unique_id, item.file_id, 'photo', item.file_size)]
    if msg.video:
        return [(msg.video.file_unique_id, msg.video.file_id, 'video', msg.video.file_size)]
    if msg.document:
        return [(msg.document.file_unique_id, msg.document.file_id, 'document', msg.document.file_size)]
    if msg.animation:
        return [(msg.animation.file_unique_id, msg.animation.file_id, 'animation', msg.animation.file_size)]
    if msg.video_note:
        return [(msg.video_note.file_unique_id, msg.video_note.file_id, 'video_note', msg.video_note.file_size)]
    if msg.audio:
        return [(msg.audio.file_unique_id, msg.audio.file_id, 'audio', msg.audio.file_size)]
    if msg.voice:
        return [(msg.voice.file_unique_id, msg.voice.file_id, 'voice', msg.voice.file_size)]
    return []


def remember_album_message(msg: Message) -> None:
    if not msg.media_group_id or not media_file_entries(msg):
        return
    now = time.monotonic()
    for key, (created, _items) in list(_ALBUM_CACHE.items()):
        if now - created > _ALBUM_TTL_SECONDS:
            _ALBUM_CACHE.pop(key, None)
    key = (msg.chat.id, str(msg.media_group_id))
    created, items = _ALBUM_CACHE.get(key, (now, []))
    if not any(x.message_id == msg.message_id for x in items):
        items.append(msg)
    _ALBUM_CACHE[key] = (created, items)


def album_messages_for(msg: Message) -> list[Message]:
    if not msg.media_group_id:
        return [msg]
    cached = _ALBUM_CACHE.get((msg.chat.id, str(msg.media_group_id)))
    if not cached:
        return [msg]
    return sorted(cached[1], key=lambda x: x.message_id) or [msg]


async def _download_to_temp(bot: Bot, file_id: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix='groschat_media_', suffix=suffix)
    os.close(fd)
    try:
        await bot.download(file_id, destination=path, timeout=120)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError('téléchargement vide')
        return path
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise


def _sha256_path(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


async def file_sha256(bot: Bot, file_id: str) -> str | None:
    path = None
    try:
        path = await _download_to_temp(bot, file_id, '.bin')
        return 'sha256:' + await asyncio.to_thread(_sha256_path, path)
    except Exception as exc:
        logger.warning('[HASHBAN] SHA256 impossible file_id=%s: %s: %s', file_id, type(exc).__name__, exc)
        return None
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def _crop(image: Image.Image, ratio: float) -> Image.Image:
    if ratio <= 0:
        return image
    width, height = image.size
    left, top = int(width * ratio), int(height * ratio)
    right, bottom = int(width * (1 - ratio)), int(height * (1 - ratio))
    if right <= left or bottom <= top:
        return image
    return image.crop((left, top, right, bottom))


def _dhash(image: Image.Image, crop_ratio: float = 0.0, mirror: bool = False) -> str:
    image = image.convert('L')
    image = _crop(image, crop_ratio)
    if mirror:
        image = ImageOps.mirror(image)
    image = image.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    value = 0
    for row in range(8):
        for col in range(8):
            value <<= 1
            value |= pixels[row * 9 + col] > pixels[row * 9 + col + 1]
    return f'{value:016x}'


def _frame_fingerprints(image: Image.Image, prefix: str, frame_index: int, *, robust: bool) -> list[tuple[str, str, int]]:
    # Deux empreintes legacy garantissent la compatibilité avec la blacklist
    # existante. visual_v2 ajoute une famille commune de variantes permettant
    # de reconnaître un miroir ou un recadrage plus prononcé.
    rows = [
        (prefix, _dhash(image, 0.0, False), frame_index),
        (prefix + '_center', _dhash(image, 0.08, False), frame_index),
        (prefix + '_visual_v2', _dhash(image, 0.16, False), frame_index),
        (prefix + '_visual_v2', _dhash(image, 0.0, True), frame_index),
    ]
    if robust:
        rows += [
            (prefix + '_visual_v2', _dhash(image, 0.24, False), frame_index),
            (prefix + '_visual_v2', _dhash(image, 0.12, True), frame_index),
        ]
    # Ne stocke pas deux fois exactement le même hash/kind/frame.
    out: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        if row not in seen:
            seen.add(row)
            out.append(row)
    return out


def _image_fingerprints(path: str, *, robust: bool = False) -> list[tuple[str, str, int]]:
    with Image.open(path) as image:
        return _frame_fingerprints(image, 'dhash', 0, robust=robust)


def _ffmpeg_executable() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _video_duration(path: str) -> float:
    import subprocess
    proc = subprocess.run(
        [_ffmpeg_executable(), '-hide_banner', '-i', path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30,
    )
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', proc.stderr or '')
    if not match:
        raise RuntimeError('durée vidéo introuvable')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _extract_video_fingerprints(path: str, *, sample_count: int, robust: bool = False) -> list[tuple[str, str, int]]:
    import subprocess
    duration = _video_duration(path)
    if duration <= 0:
        raise RuntimeError('durée vidéo invalide')

    # On évite début/fin, souvent remplacés par une intro/outro. Les positions
    # sont relatives à la durée, donc une simple modification de métadonnées ou
    # de bitrate ne change pas le contenu échantillonné.
    positions = [duration * (i + 1) / (sample_count + 1) for i in range(sample_count)]
    result: list[tuple[str, str, int]] = []
    with tempfile.TemporaryDirectory(prefix='groschat_frames_') as frame_dir:
        for index, position in enumerate(positions):
            frame_path = os.path.join(frame_dir, f'{index:02d}.jpg')
            proc = subprocess.run(
                [
                    _ffmpeg_executable(), '-loglevel', 'error', '-ss', f'{position:.3f}',
                    '-i', path, '-frames:v', '1', '-vf', 'scale=160:-2', '-q:v', '5',
                    '-y', frame_path,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=25,
            )
            if proc.returncode != 0 or not os.path.exists(frame_path):
                continue
            with Image.open(frame_path) as image:
                result.extend(_frame_fingerprints(image, 'video_dhash', index, robust=robust))
    if not result:
        raise RuntimeError('aucune image vidéo extraite')
    return result


async def _analyse_file_once(bot: Bot, file_id: str, media_type: str, *, robust: bool) -> tuple[str | None, list[tuple[str, str, int]], str | None]:
    suffix = '.jpg' if media_type == 'photo' else ('.mp4' if media_type in {'video', 'animation', 'video_note'} else '.bin')
    path = None
    try:
        path = await _download_to_temp(bot, file_id, suffix)
        sha = 'sha256:' + await asyncio.to_thread(_sha256_path, path)
        fingerprints: list[tuple[str, str, int]] = []
        if media_type in {'photo', 'video', 'animation', 'video_note'}:
            async with _MEDIA_ANALYSIS_SEMAPHORE:
                if media_type == 'photo':
                    fingerprints = await asyncio.to_thread(_image_fingerprints, path, robust=robust)
                else:
                    count = _BAN_VIDEO_SAMPLE_COUNT if robust else _INCOMING_VIDEO_SAMPLE_COUNT
                    fingerprints = await asyncio.to_thread(
                        _extract_video_fingerprints, path, sample_count=count, robust=robust,
                    )
        return sha, fingerprints, None
    except Exception as exc:
        error = f'{media_type}: {type(exc).__name__}: {exc}'
        logger.warning('[MEDIA] analyse impossible: %s', error)
        return None, [], error
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


async def _analyse_media_once(bot: Bot, msg: Message, *, robust: bool) -> tuple[str | None, list[tuple[str, str, int]], str | None]:
    entries = media_file_entries(msg)
    if not entries:
        return None, [], 'aucun média compatible'
    _unique, file_id, media_type, _size = entries[0]
    return await _analyse_file_once(bot, file_id, media_type, robust=robust)


class _BKNode:
    __slots__ = ('value', 'payloads', 'children')

    def __init__(self, value: int, payload):
        self.value = value
        self.payloads = [payload]
        self.children: dict[int, _BKNode] = {}


class _BKTree:
    __slots__ = ('root',)

    def __init__(self):
        self.root: _BKNode | None = None

    def add(self, value: int, payload) -> None:
        if self.root is None:
            self.root = _BKNode(value, payload)
            return
        node = self.root
        while True:
            distance = (value ^ node.value).bit_count()
            if distance == 0:
                node.payloads.append(payload)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = _BKNode(value, payload)
                return
            node = child

    def query(self, value: int, radius: int):
        if self.root is None:
            return []
        hits = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = (value ^ node.value).bit_count()
            if distance <= radius:
                hits.extend((distance, payload) for payload in node.payloads)
            low, high = distance - radius, distance + radius
            stack.extend(child for edge, child in node.children.items() if low <= edge <= high)
        return hits


@dataclass
class _FingerprintIndex:
    expires_at: float
    trees: dict[str, _BKTree]
    source_count: int

    def add(self, source: str, kind: str, fingerprint: str, frame_index: int) -> None:
        tree = self.trees.setdefault(kind, _BKTree())
        tree.add(int(fingerprint, 16), (source, frame_index))


_FP_INDEX_CACHE: dict[tuple[str, bool], _FingerprintIndex] = {}
_FP_INDEX_LOCKS: dict[tuple[str, bool], asyncio.Lock] = {}


def _invalidate_banned_fingerprint_indexes() -> None:
    # L'index SAFE peut contenir énormément de médias. Il est maintenu
    # incrémentalement et ne doit surtout pas être reconstruit à chaque /pedo.
    # Un média promu BANNED sera de toute façon testé dans l'index blacklist
    # avant l'anti-repost.
    for key in [key for key in _FP_INDEX_CACHE if key[1] is True]:
        _FP_INDEX_CACHE.pop(key, None)


async def _fingerprint_index(media_type: str, banned: bool) -> _FingerprintIndex:
    key = (media_type, banned)
    now = time.monotonic()
    cached = _FP_INDEX_CACHE.get(key)
    if cached and now < cached.expires_at:
        return cached
    lock = _FP_INDEX_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _FP_INDEX_CACHE.get(key)
        now = time.monotonic()
        if cached and now < cached.expires_at:
            return cached
        async with SessionLocal() as db:
            rows = (await db.execute(select(
                MediaFingerprint.source_file_unique_id,
                MediaFingerprint.fingerprint_kind,
                MediaFingerprint.fingerprint,
                MediaFingerprint.frame_index,
            ).where(
                MediaFingerprint.media_type == media_type,
                MediaFingerprint.banned.is_(banned),
            ))).all()
            banned_sources: set[str] = set()
            if not banned and rows:
                banned_sources = set((await db.execute(select(MediaFingerprint.source_file_unique_id).where(
                    MediaFingerprint.media_type == media_type,
                    MediaFingerprint.banned.is_(True),
                ))).scalars().all())
        ttl = _BANNED_FP_INDEX_TTL_SECONDS if banned else _SAFE_FP_INDEX_TTL_SECONDS
        index = _FingerprintIndex(now + ttl, {}, 0)
        sources: set[str] = set()
        for source, kind, fingerprint, frame_index in rows:
            if not banned and source in banned_sources:
                continue
            try:
                index.add(source, kind, fingerprint, frame_index)
                sources.add(source)
            except (TypeError, ValueError):
                continue
        index.source_count = len(sources)
        _FP_INDEX_CACHE[key] = index
        return index


def _match_index(media_type: str, current: list[tuple[str, str, int]], index: _FingerprintIndex, *, banned: bool):
    details = {
        'computed': len(current), 'best_distance': None, 'matched_frames': 0,
        'required_frames': 0, 'source': None, 'error': None,
    }
    if not current or not index.trees:
        return False, details

    if media_type == 'photo':
        radius = _BANNED_IMAGE_DISTANCE_LIMIT if banned else _REPOST_IMAGE_DISTANCE_LIMIT
        # Pour un hash-ban, une seule forte correspondance suffit. Pour un
        # simple anti-repost, on exige deux variantes concordantes avec la
        # même source afin de réduire fortement les faux positifs.
        required_hits = 1 if banned else 2
        matched_by_source: dict[str, set[int]] = defaultdict(set)
        best_by_source: dict[str, int] = {}
        for current_pos, (kind, fingerprint, _idx) in enumerate(current):
            tree = index.trees.get(kind)
            if not tree:
                continue
            for distance, (source, _old_idx) in tree.query(int(fingerprint, 16), radius):
                matched_by_source[source].add(current_pos)
                old = best_by_source.get(source)
                if old is None or distance < old:
                    best_by_source[source] = distance
        if matched_by_source:
            source, hits = max(matched_by_source.items(), key=lambda item: (len(item[1]), -best_by_source.get(item[0], 999)))
            details.update(best_distance=best_by_source.get(source), matched_frames=len(hits), required_frames=required_hits, source=source)
            return len(hits) >= required_hits, details
        details['required_frames'] = required_hits
        return False, details

    radius = _BANNED_VIDEO_DISTANCE_LIMIT if banned else _REPOST_VIDEO_DISTANCE_LIMIT
    ratio = _BANNED_VIDEO_MATCH_RATIO if banned else _REPOST_VIDEO_MATCH_RATIO
    frame_positions = {idx for _kind, _fingerprint, idx in current}
    required = max(3, math.ceil(len(frame_positions) * ratio))
    details['required_frames'] = required

    matched_by_source: dict[str, set[int]] = defaultdict(set)
    best_by_source: dict[str, int] = {}
    for kind, fingerprint, current_idx in current:
        tree = index.trees.get(kind)
        if not tree:
            continue
        for distance, (source, _old_idx) in tree.query(int(fingerprint, 16), radius):
            matched_by_source[source].add(current_idx)
            old_best = best_by_source.get(source)
            if old_best is None or distance < old_best:
                best_by_source[source] = distance

    if not matched_by_source:
        return False, details
    source, positions = max(matched_by_source.items(), key=lambda item: (len(item[1]), -best_by_source.get(item[0], 999)))
    details.update(
        best_distance=best_by_source.get(source),
        matched_frames=len(positions),
        source=source,
    )
    return len(positions) >= required, details


def _remember_known_key(key: str | None) -> None:
    if not key:
        return
    _KNOWN_KEY_CACHE[key] = None
    _KNOWN_KEY_CACHE.move_to_end(key)
    while len(_KNOWN_KEY_CACHE) > _KNOWN_KEY_CACHE_MAX:
        _KNOWN_KEY_CACHE.popitem(last=False)


async def _known_exact_key(key: str) -> bool:
    if key in _KNOWN_KEY_CACHE:
        _KNOWN_KEY_CACHE.move_to_end(key)
        return True
    async with SessionLocal() as db:
        exists = (await db.execute(select(MediaHash.id).where(
            MediaHash.file_unique_id == key,
        ).limit(1))).scalar_one_or_none() is not None
    if exists:
        _remember_known_key(key)
    return exists


def _invalidate_ban_caches() -> None:
    global _BANNED_EXACT_CACHE
    _BANNED_EXACT_CACHE = None
    _BAN_CAP_CACHE.clear()
    _invalidate_banned_fingerprint_indexes()


async def _banned_exact_keys() -> tuple[set[str], set[str]]:
    global _BANNED_EXACT_CACHE
    now = time.monotonic()
    if _BANNED_EXACT_CACHE and now < _BANNED_EXACT_CACHE[0]:
        return _BANNED_EXACT_CACHE[1], _BANNED_EXACT_CACHE[2]
    async with SessionLocal() as db:
        keys = set((await db.execute(select(MediaHash.file_unique_id).where(
            MediaHash.banned.is_(True)
        ))).scalars().all())
    sha = {key for key in keys if key.startswith('sha256:')}
    ids = keys - sha
    _BANNED_EXACT_CACHE = (now + _BAN_CACHE_TTL_SECONDS, ids, sha)
    return ids, sha


async def _ban_capabilities(media_type: str) -> tuple[bool, bool]:
    now = time.monotonic()
    cached = _BAN_CAP_CACHE.get(media_type)
    if cached and now < cached[0]:
        return cached[1], cached[2]
    _ids, sha = await _banned_exact_keys()
    async with SessionLocal() as db:
        fp_exists = (await db.execute(select(MediaFingerprint.id).where(
            MediaFingerprint.banned.is_(True),
            MediaFingerprint.media_type == media_type,
        ).limit(1))).scalar_one_or_none() is not None
    result = (bool(sha), fp_exists)
    _BAN_CAP_CACHE[media_type] = (now + _BAN_CACHE_TTL_SECONDS, *result)
    return result


async def _ban_generation() -> int:
    raw = await st.get_value('media_ban_generation', '0')
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def _bump_ban_generation() -> int:
    generation = await _ban_generation() + 1
    await st.set_value('media_ban_generation', str(generation))
    _invalidate_ban_caches()
    return generation


async def _upsert_exact(db, *, key: str, user_id: int | None, file_id: str, media_type: str, banned: bool) -> None:
    rows = list((await db.execute(select(MediaHash).where(MediaHash.file_unique_id == key))).scalars().all())
    if rows:
        for row in rows:
            if banned:
                row.banned = True
            # Jamais de retour banned=True -> False.
            row.file_id = file_id or row.file_id
            row.media_type = media_type or row.media_type
            if user_id is not None and row.user_id is None:
                row.user_id = user_id
    else:
        db.add(MediaHash(
            user_id=user_id, file_unique_id=key, file_id=file_id,
            media_type=media_type, banned=banned,
        ))
    _remember_known_key(key)


async def _store_fingerprints(db, *, source: str, user_id: int | None, media_type: str,
                              fingerprints: list[tuple[str, str, int]], banned: bool) -> int:
    if not fingerprints:
        return 0
    existing_rows = list((await db.execute(select(MediaFingerprint).where(
        MediaFingerprint.source_file_unique_id == source,
    ))).scalars().all())
    existing = {(r.fingerprint_kind, r.fingerprint, r.frame_index): r for r in existing_rows}
    count = 0
    if banned:
        for row in existing_rows:
            row.banned = True
            if user_id is not None and row.user_id is None:
                row.user_id = user_id
    for kind, fingerprint, frame_index in fingerprints:
        key = (kind, fingerprint, frame_index)
        row = existing.get(key)
        if row:
            if banned:
                row.banned = True
            if user_id is not None and row.user_id is None:
                row.user_id = user_id
        else:
            db.add(MediaFingerprint(
                user_id=user_id,
                source_file_unique_id=source,
                media_type=media_type,
                fingerprint_kind=kind,
                fingerprint=fingerprint,
                frame_index=frame_index,
                banned=banned,
            ))
        count += 1
    return count


async def _upsert_registry(db, *, unique: str, file_id: str, media_type: str,
                           user_id: int | None, chat_id: int | None, message_id: int | None,
                           sha: str | None, banned: bool, perceptual_ready: bool,
                           generation: int, seen_increment: bool = False) -> GlobalMediaRegistry:
    row = await db.get(GlobalMediaRegistry, unique)
    now = datetime.utcnow()
    if row is None:
        row = GlobalMediaRegistry(
            file_unique_id=unique, file_id=file_id, sha256=sha, media_type=media_type,
            first_user_id=user_id, first_chat_id=chat_id, first_message_id=message_id,
            first_seen_at=now, last_seen_at=now, seen_count=1,
            analysis_state='banned' if banned else 'safe',
            checked_ban_generation=generation,
            perceptual_ready=perceptual_ready, banned=banned,
        )
        db.add(row)
    else:
        row.file_id = file_id or row.file_id
        row.sha256 = sha or row.sha256
        row.media_type = media_type or row.media_type
        row.last_seen_at = now
        if seen_increment:
            row.seen_count += 1
        row.checked_ban_generation = max(row.checked_ban_generation or 0, generation)
        row.perceptual_ready = row.perceptual_ready or perceptual_ready
        if banned:
            row.banned = True
            row.analysis_state = 'banned'
        elif not row.banned:
            row.analysis_state = 'safe'
    return row


async def _queue_ban_retry(*, unique: str, file_id: str, media_type: str, user_id: int | None,
                           chat_id: int | None, message_id: int | None, error: str | None) -> bool:
    async with SessionLocal() as db:
        existing = (await db.execute(select(MediaBanJob).where(
            MediaBanJob.source_file_unique_id == unique,
            MediaBanJob.status.in_(['pending', 'error']),
        ).order_by(MediaBanJob.id.desc()).limit(1))).scalar_one_or_none()
        if existing:
            existing.file_id = file_id
            existing.media_type = media_type
            existing.user_id = user_id
            existing.last_error = error
            existing.status = 'pending'
            existing.next_attempt_at = datetime.utcnow() + timedelta(minutes=1)
        else:
            db.add(MediaBanJob(
                source_file_unique_id=unique, file_id=file_id, media_type=media_type,
                user_id=user_id, source_chat_id=chat_id, source_message_id=message_id,
                status='pending', attempts=0, last_error=error,
                next_attempt_at=datetime.utcnow() + timedelta(minutes=1),
            ))
        await db.commit()
    return True


async def inspect_incoming_media(bot: Bot, msg: Message, *, repost_enabled: bool) -> MediaInspection:
    """Analyse unique d'un média pour hash-ban ET anti-repost global.

    Ordre rapide : ID Telegram -> historique exact -> SHA -> perceptuel. Une
    seule copie du fichier est téléchargée et les empreintes sont calculées une
    seule fois. L'anti-repost consulte l'historique de TOUS les groupes.
    """
    entries = media_file_entries(msg)
    if not entries:
        return MediaInspection()
    unique, file_id, media_type, _size = entries[0]
    generation = await _ban_generation()
    result = MediaInspection(ban_generation=generation)

    banned_ids, banned_sha = await _banned_exact_keys()
    if unique in banned_ids:
        result.banned = True
        result.method = 'telegram_id'
        return result

    # Anti-repost global exact. Les anciennes lignes MediaHash sont incluses,
    # ce qui conserve tout l'historique de la base actuelle.
    if repost_enabled and await _known_exact_key(unique):
        result.repost = True
        result.known_unique = True
        result.method = 'repost_telegram_id'
        return result

    # Un média déjà analysé SAFE sous la génération actuelle ne doit jamais
    # repasser par FFmpeg quand l'anti-repost local est OFF.
    async with SessionLocal() as db:
        registry = await db.get(GlobalMediaRegistry, unique)
    if registry and registry.banned:
        result.banned = True
        result.method = 'registry_banned'
        return result
    if (
        registry and not repost_enabled and registry.analysis_state == 'safe'
        and registry.checked_ban_generation >= generation
    ):
        result.method = 'safe_cache'
        return result

    sha_needed, banned_fp_exists = await _ban_capabilities(media_type)
    # Sans anti-repost et sans blacklist de contenu à comparer, le file ID
    # suffit. Cela évite tout téléchargement inutile sur une installation vide.
    if not repost_enabled and not sha_needed and not banned_fp_exists:
        result.method = 'no_deep_check_needed'
        return result

    sha, fingerprints, error = await _analyse_file_once(bot, file_id, media_type, robust=False)
    result.sha = sha
    result.fingerprints = fingerprints
    result.error = error

    if sha and sha in banned_sha:
        result.banned = True
        result.method = 'sha256'
        return result

    if repost_enabled and sha and await _known_exact_key(sha):
        result.repost = True
        result.known_sha = True
        result.method = 'repost_sha256'
        return result

    # Blacklist visuelle d'abord : un média interdit doit bannir, pas être
    # classé comme simple repost.
    if banned_fp_exists and fingerprints:
        banned_index = await _fingerprint_index(media_type, True)
        matched, details = _match_index(media_type, fingerprints, banned_index, banned=True)
        if matched:
            result.banned = True
            result.method = 'perceptual_banned'
            result.perceptual_source = details.get('source')
            result.best_distance = details.get('best_distance')
            result.matched_frames = details.get('matched_frames', 0)
            result.required_frames = details.get('required_frames', 0)
            return result

    if repost_enabled and fingerprints:
        safe_index = await _fingerprint_index(media_type, False)
        matched, details = _match_index(media_type, fingerprints, safe_index, banned=False)
        if matched:
            result.repost = True
            result.method = 'repost_perceptual'
            result.perceptual_source = details.get('source')
            result.best_distance = details.get('best_distance')
            result.matched_frames = details.get('matched_frames', 0)
            result.required_frames = details.get('required_frames', 0)
            return result

    result.method = 'new_media'
    return result


async def register_allowed_media(msg: Message, inspection: MediaInspection) -> None:
    entries = media_file_entries(msg)
    if not entries:
        return
    unique, file_id, media_type, _size = entries[0]
    user_id = msg.from_user.id if msg.from_user else None
    generation = inspection.ban_generation or await _ban_generation()

    async with SessionLocal() as db:
        await _upsert_exact(
            db, key=unique, user_id=user_id, file_id=file_id,
            media_type=media_type, banned=False,
        )
        if inspection.sha:
            await _upsert_exact(
                db, key=inspection.sha, user_id=user_id, file_id=file_id,
                media_type=media_type, banned=False,
            )
        await _store_fingerprints(
            db, source=unique, user_id=user_id, media_type=media_type,
            fingerprints=inspection.fingerprints, banned=False,
        )
        registry = await _upsert_registry(
            db, unique=unique, file_id=file_id, media_type=media_type,
            user_id=user_id, chat_id=msg.chat.id, message_id=msg.message_id,
            sha=inspection.sha, banned=False,
            perceptual_ready=bool(inspection.fingerprints), generation=generation,
            seen_increment=False,
        )
        # Une erreur réseau/FFmpeg n'est jamais mémorisée comme verdict SAFE.
        # Le même file_unique_id sera donc réanalysé au prochain passage.
        if inspection.error:
            registry.analysis_state = 'error'
            registry.checked_ban_generation = 0
        await db.commit()

    # Ajout incrémental au cache SAFE déjà construit : pas de rebuild complet
    # après chaque média.
    cached = _FP_INDEX_CACHE.get((media_type, False))
    if cached and time.monotonic() < cached.expires_at:
        for kind, fingerprint, frame_index in inspection.fingerprints:
            cached.add(unique, kind, fingerprint, frame_index)
        cached.source_count += 1


async def mark_media_seen(unique: str) -> None:
    async with SessionLocal() as db:
        row = await db.get(GlobalMediaRegistry, unique)
        if row:
            row.last_seen_at = datetime.utcnow()
            row.seen_count += 1
            await db.commit()


async def ban_hashes_from_messages(messages: list[Message], bot: Bot) -> HashBanReport:
    """Hash-ban robuste : ID immédiat, puis SHA + visuel riche + reprise.

    Le file_unique_id est blacklisté AVANT le téléchargement. Même si Telegram
    ou FFmpeg tombe en panne, le média ciblé est donc déjà bloqué. Les parties
    SHA/perceptuelles ratées sont persistées dans une file de reprise.
    """
    report = HashBanReport()
    unique_messages: list[Message] = []
    seen_ids: set[tuple[int, int]] = set()
    for message in messages:
        key = (message.chat.id, message.message_id)
        if key not in seen_ids and media_file_entries(message):
            seen_ids.add(key)
            unique_messages.append(message)

    if not unique_messages:
        return report

    generation = await _ban_generation() + 1
    # Phase 1 : blocage exact immédiat en une transaction.
    async with SessionLocal() as db:
        for msg in unique_messages:
            unique, file_id, media_type, _size = media_file_entries(msg)[0]
            user_id = msg.from_user.id if msg.from_user else None
            await _upsert_exact(
                db, key=unique, user_id=user_id, file_id=file_id,
                media_type=media_type, banned=True,
            )
            await db.execute(update(MediaFingerprint).where(
                MediaFingerprint.source_file_unique_id == unique,
            ).values(banned=True))
            await _upsert_registry(
                db, unique=unique, file_id=file_id, media_type=media_type,
                user_id=user_id, chat_id=msg.chat.id, message_id=msg.message_id,
                sha=None, banned=True, perceptual_ready=False,
                generation=generation,
            )
            report.media_count += 1
            report.exact_keys += 1
        await db.commit()
    await st.set_value('media_ban_generation', str(generation))
    _invalidate_ban_caches()

    # Phase 2 : enrichissement robuste sans garder de connexion DB pendant
    # Telegram/FFmpeg.
    analysed = []
    for msg in unique_messages:
        unique, file_id, media_type, _size = media_file_entries(msg)[0]
        sha, fingerprints, error = await _analyse_file_once(bot, file_id, media_type, robust=True)
        analysed.append((msg, unique, file_id, media_type, sha, fingerprints, error))

    async with SessionLocal() as db:
        for msg, unique, file_id, media_type, sha, fingerprints, error in analysed:
            user_id = msg.from_user.id if msg.from_user else None
            if sha:
                await _upsert_exact(
                    db, key=sha, user_id=user_id, file_id=file_id,
                    media_type=media_type, banned=True,
                )
                report.sha256_count += 1
                report.exact_keys += 1
            if fingerprints:
                report.perceptual_count += await _store_fingerprints(
                    db, source=unique, user_id=user_id, media_type=media_type,
                    fingerprints=fingerprints, banned=True,
                )
            await _upsert_registry(
                db, unique=unique, file_id=file_id, media_type=media_type,
                user_id=user_id, chat_id=msg.chat.id, message_id=msg.message_id,
                sha=sha, banned=True, perceptual_ready=bool(fingerprints),
                generation=generation,
            )
            if error or not sha or (media_type in {'photo', 'video', 'animation', 'video_note'} and not fingerprints):
                report.errors.append(error or f'{media_type}: empreinte incomplète')
        await db.commit()

    for msg, unique, file_id, media_type, sha, fingerprints, error in analysed:
        if error or not sha or (media_type in {'photo', 'video', 'animation', 'video_note'} and not fingerprints):
            await _queue_ban_retry(
                unique=unique, file_id=file_id, media_type=media_type,
                user_id=msg.from_user.id if msg.from_user else None,
                chat_id=msg.chat.id, message_id=msg.message_id, error=error,
            )
            report.retry_queued += 1

    _invalidate_ban_caches()
    return report


async def promote_user_media_banned(user_id: int) -> None:
    """Promote toutes les traces média déjà connues d'un utilisateur en ban.

    Utilisé par /pedo : même ses anciens médias déjà enregistrés deviennent
    interdits globalement, pas uniquement le message auquel l'admin répond.
    """
    async with SessionLocal() as db:
        await db.execute(update(MediaHash).where(MediaHash.user_id == user_id).values(banned=True))
        await db.execute(update(MediaFingerprint).where(MediaFingerprint.user_id == user_id).values(banned=True))
        await db.execute(update(GlobalMediaRegistry).where(
            GlobalMediaRegistry.first_user_id == user_id,
        ).values(banned=True, analysis_state='banned'))
        await db.commit()
    await _bump_ban_generation()


async def process_pending_hashban_jobs(bot: Bot, limit: int = 4) -> int:
    """Retente les SHA/fingerprints que /pedo n'a pas pu terminer."""
    now = datetime.utcnow()
    async with SessionLocal() as db:
        jobs = list((await db.execute(select(MediaBanJob).where(
            MediaBanJob.status.in_(['pending', 'error']),
            MediaBanJob.next_attempt_at <= now,
            MediaBanJob.attempts < 5,
        ).order_by(MediaBanJob.next_attempt_at.asc()).limit(limit))).scalars().all())
    if not jobs:
        return 0

    completed = 0
    generation = await _ban_generation()
    for job in jobs:
        sha, fingerprints, error = await _analyse_file_once(bot, job.file_id, job.media_type, robust=True)
        success = bool(sha) and (job.media_type not in {'photo', 'video', 'animation', 'video_note'} or bool(fingerprints))
        async with SessionLocal() as db:
            fresh = await db.get(MediaBanJob, job.id)
            if not fresh:
                continue
            fresh.attempts += 1
            fresh.last_error = error
            if sha:
                await _upsert_exact(
                    db, key=sha, user_id=job.user_id, file_id=job.file_id,
                    media_type=job.media_type, banned=True,
                )
            if fingerprints:
                await _store_fingerprints(
                    db, source=job.source_file_unique_id, user_id=job.user_id,
                    media_type=job.media_type, fingerprints=fingerprints, banned=True,
                )
            await _upsert_registry(
                db, unique=job.source_file_unique_id, file_id=job.file_id,
                media_type=job.media_type, user_id=job.user_id,
                chat_id=job.source_chat_id, message_id=job.source_message_id,
                sha=sha, banned=True, perceptual_ready=bool(fingerprints),
                generation=generation,
            )
            if success:
                fresh.status = 'done'
                completed += 1
            else:
                if fresh.attempts >= 5:
                    fresh.status = 'failed'
                else:
                    fresh.status = 'error'
                    delay = [2, 5, 15, 30, 60][min(fresh.attempts - 1, 4)]
                    fresh.next_attempt_at = datetime.utcnow() + timedelta(minutes=delay)
            await db.commit()
    if completed:
        await _bump_ban_generation()
    else:
        _invalidate_ban_caches()
    return completed


async def ban_hash_from_message(msg: Message, bot: Bot | None = None):
    if not bot:
        return 0
    report = await ban_hashes_from_messages([msg], bot)
    return report.total


async def contains_banned_hash(bot: Bot, msg: Message) -> tuple[bool, dict]:
    inspection = await inspect_incoming_media(bot, msg, repost_enabled=False)
    return inspection.banned, inspection.details()


async def exact_banned_match(bot: Bot, msg: Message) -> tuple[bool, dict]:
    entries = media_file_entries(msg)
    details = {'telegram_match': False, 'sha_match': False, 'sha': None, 'errors': []}
    if not entries:
        return False, details
    unique, file_id, _media_type, _size = entries[0]
    banned_ids, banned_sha = await _banned_exact_keys()
    details['telegram_match'] = unique in banned_ids
    if details['telegram_match']:
        return True, details
    sha = await file_sha256(bot, file_id)
    details['sha'] = sha
    if not sha:
        details['errors'].append('SHA256 non calculé')
        return False, details
    details['sha_match'] = sha in banned_sha
    return bool(details['sha_match']), details


async def perceptual_banned_match(bot: Bot, msg: Message) -> tuple[bool, dict]:
    entries = media_file_entries(msg)
    details = {'computed': 0, 'best_distance': None, 'matched_frames': 0, 'required_frames': 0, 'source': None, 'error': None}
    if not entries:
        return False, details
    _unique, _file_id, media_type, _size = entries[0]
    if media_type not in {'photo', 'video', 'animation', 'video_note'}:
        return False, details
    _sha, current, error = await _analyse_media_once(bot, msg, robust=False)
    details['computed'] = len(current)
    details['error'] = error
    if not current:
        return False, details
    index = await _fingerprint_index(media_type, True)
    matched, match_details = _match_index(media_type, current, index, banned=True)
    match_details['error'] = error
    return matched, match_details


async def hash_diagnostic(bot: Bot, msg: Message) -> str:
    entries = media_file_entries(msg)
    if not entries:
        return '❌ Réponds à une photo, une vidéo, une animation ou un document.'
    unique, file_id, media_type, file_size = entries[0]
    sha, fingerprints, error = await _analyse_media_once(bot, msg, robust=False)
    banned_ids, banned_sha = await _banned_exact_keys()
    exact_id = unique in banned_ids
    exact_sha = bool(sha and sha in banned_sha)

    perceptual = False
    pdetails = {'best_distance': None, 'matched_frames': 0, 'required_frames': 0, 'source': None}
    if fingerprints:
        pindex = await _fingerprint_index(media_type, True)
        perceptual, pdetails = _match_index(media_type, fingerprints, pindex, banned=True)

    known_unique = await _known_exact_key(unique)
    known_sha = bool(sha and await _known_exact_key(sha))
    safe_perceptual = False
    sdetails = {'source': None, 'best_distance': None}
    if fingerprints:
        sindex = await _fingerprint_index(media_type, False)
        safe_perceptual, sdetails = _match_index(media_type, fingerprints, sindex, banned=False)

    async with SessionLocal() as db:
        registry = await db.get(GlobalMediaRegistry, unique)
        pending_retry = (await db.execute(select(func.count(MediaBanJob.id)).where(
            MediaBanJob.source_file_unique_id == unique,
            MediaBanJob.status.in_(['pending', 'error']),
        ))).scalar() or 0
        failed_retry = (await db.execute(select(func.count(MediaBanJob.id)).where(
            MediaBanJob.source_file_unique_id == unique,
            MediaBanJob.status == 'failed',
        ))).scalar() or 0

    size_text = f'{file_size / 1024 / 1024:.2f} Mo' if file_size else 'inconnue'
    return '\n'.join([
        '🔎 /HASHDEMANDE — GLOBAL', '',
        f'Type : {media_type}',
        f'Taille Telegram : {size_text}', '',
        f'file_unique_id : {unique}',
        f'Déjà connu réseau : {"✅ OUI" if known_unique else "❌ NON"}',
        f'Blacklist ID : {"✅ OUI" if exact_id else "❌ NON"}', '',
        f'SHA256 : {sha or "NON CALCULÉ"}',
        f'Déjà connu SHA : {"✅ OUI" if known_sha else "❌ NON"}',
        f'Blacklist SHA : {"✅ OUI" if exact_sha else "❌ NON"}', '',
        f'Blacklist perceptuelle : {"✅ OUI" if perceptual else "❌ NON"}',
        f'Source blacklist proche : {pdetails.get("source") or "aucune"}',
        f'Meilleure distance ban : {pdetails.get("best_distance")}',
        f'Frames ban : {pdetails.get("matched_frames", 0)}/{pdetails.get("required_frames", 0)}', '',
        f'Repost perceptuel connu : {"✅ OUI" if safe_perceptual else "❌ NON"}',
        f'Source repost proche : {sdetails.get("source") or "aucune"}',
        f'État registre : {registry.analysis_state if registry else "absent"}',
        f'Reprises hash-ban en attente : {int(pending_retry)}',
        f'Reprises définitivement échouées : {int(failed_retry)}',
        f'Erreur analyse : {error or "aucune"}',
    ])


async def banned_hash_count():
    async with SessionLocal() as db:
        exact = int((await db.execute(select(func.count(MediaHash.id)).where(MediaHash.banned.is_(True)))).scalar() or 0)
        perceptual = int((await db.execute(select(func.count(MediaFingerprint.id)).where(MediaFingerprint.banned.is_(True)))).scalar() or 0)
        return exact + perceptual


async def media_registry_stats() -> dict[str, int]:
    async with SessionLocal() as db:
        total = int((await db.execute(select(func.count(GlobalMediaRegistry.file_unique_id)))).scalar() or 0)
        banned = int((await db.execute(select(func.count(GlobalMediaRegistry.file_unique_id)).where(
            GlobalMediaRegistry.banned.is_(True)
        ))).scalar() or 0)
        pending = int((await db.execute(select(func.count(MediaBanJob.id)).where(
            MediaBanJob.status.in_(['pending', 'error'])
        ))).scalar() or 0)
    return {'registry': total, 'registry_banned': banned, 'ban_retry_pending': pending}
