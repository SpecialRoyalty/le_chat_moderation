from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message
from PIL import Image
from sqlalchemy import func, select

from app.db.models import MediaFingerprint, MediaHash
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# L'analyse lourde est sérialisée afin de ne pas saturer Railway.
_MEDIA_ANALYSIS_SEMAPHORE = asyncio.Semaphore(1)
_ALBUM_TTL_SECONDS = 30 * 60
_ALBUM_CACHE: dict[tuple[int, str], tuple[float, list[Message]]] = {}
_VIDEO_SAMPLE_COUNT = 10
_IMAGE_DISTANCE_LIMIT = 10
_VIDEO_DISTANCE_LIMIT = 11
_VIDEO_MATCH_RATIO = 0.45

# Cache court des capacités de blacklist et des empreintes bannies. Les hashes
# perceptuels changent uniquement lors d'un hash-ban, donc les relire entièrement
# depuis PostgreSQL pour chaque vidéo est inutile.
_BAN_CACHE_TTL_SECONDS = 30.0
_BAN_CAP_CACHE: dict[str, tuple[float, bool, bool]] = {}
_BANNED_EXACT_CACHE: tuple[float, set[str]] | None = None
_BANNED_FP_CACHE: dict[str, tuple[float, dict[str, dict[str, list[int]]]]] = {}


@dataclass
class HashBanReport:
    media_count: int = 0
    exact_keys: int = 0
    sha256_count: int = 0
    perceptual_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.exact_keys + self.perceptual_count

    def admin_text(self, title: str = '/PEDO — BLACKLIST CONFIRMÉE') -> str:
        lines = [
            f'🚫 {title}',
            '',
            f'Médias traités : {self.media_count}',
            f'Empreintes Telegram/SHA : {self.exact_keys}',
            f'SHA256 calculés : {self.sha256_count}',
            f'Empreintes perceptuelles : {self.perceptual_count}',
        ]
        if self.errors:
            lines += ['', '⚠️ Erreurs :'] + [f'• {e}' for e in self.errors[:8]]
        else:
            lines += ['', '✅ Enregistrement vérifié en base.']
        return '\n'.join(lines)


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
    return []


def remember_album_message(msg: Message) -> None:
    """Mémorise temporairement les éléments d'un album Telegram."""
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
    key = (msg.chat.id, str(msg.media_group_id))
    cached = _ALBUM_CACHE.get(key)
    if not cached:
        return [msg]
    items = sorted(cached[1], key=lambda x: x.message_id)
    return items or [msg]


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


async def file_sha256(bot: Bot, file_id: str) -> str | None:
    """Calcule le SHA256 sans masquer silencieusement les erreurs."""
    path = None
    try:
        path = await _download_to_temp(bot, file_id, '.bin')
        digest = await asyncio.to_thread(_sha256_path, path)
        return 'sha256:' + digest
    except Exception as exc:
        logger.warning('[HASHBAN] SHA256 impossible file_id=%s: %s: %s', file_id, type(exc).__name__, exc)
        return None
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def _sha256_path(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _dhash(image: Image.Image, center_crop: bool = False) -> str:
    image = image.convert('L')
    if center_crop:
        width, height = image.size
        left, top = int(width * 0.08), int(height * 0.08)
        right, bottom = int(width * 0.92), int(height * 0.92)
        if right > left and bottom > top:
            image = image.crop((left, top, right, bottom))
    image = image.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    value = 0
    for row in range(8):
        for col in range(8):
            value <<= 1
            value |= pixels[row * 9 + col] > pixels[row * 9 + col + 1]
    return f'{value:016x}'


def _image_fingerprints(path: str) -> list[tuple[str, str, int]]:
    with Image.open(path) as image:
        return [
            ('dhash', _dhash(image, False), 0),
            ('dhash_center', _dhash(image, True), 0),
        ]


def _ffmpeg_executable() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _video_duration(path: str) -> float:
    import subprocess
    proc = subprocess.run(
        [_ffmpeg_executable(), '-hide_banner', '-i', path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', proc.stderr or '')
    if not match:
        raise RuntimeError('durée vidéo introuvable')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _extract_video_fingerprints(path: str) -> list[tuple[str, str, int]]:
    import subprocess
    duration = _video_duration(path)
    if duration <= 0:
        raise RuntimeError('durée vidéo invalide')

    # Évite le tout début et la toute fin, souvent modifiés par une intro/outro.
    positions = [duration * (i + 1) / (_VIDEO_SAMPLE_COUNT + 1) for i in range(_VIDEO_SAMPLE_COUNT)]
    result: list[tuple[str, str, int]] = []
    with tempfile.TemporaryDirectory(prefix='groschat_frames_') as frame_dir:
        for index, position in enumerate(positions):
            frame_path = os.path.join(frame_dir, f'{index:02d}.jpg')
            proc = subprocess.run(
                [
                    _ffmpeg_executable(), '-loglevel', 'error', '-ss', f'{position:.3f}',
                    '-i', path, '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '4',
                    '-y', frame_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if proc.returncode != 0 or not os.path.exists(frame_path):
                continue
            with Image.open(frame_path) as image:
                result.append(('video_dhash', _dhash(image, False), index))
                result.append(('video_dhash_center', _dhash(image, True), index))
    if not result:
        raise RuntimeError('aucune image vidéo extraite')
    return result


async def perceptual_fingerprints(bot: Bot, msg: Message) -> tuple[list[tuple[str, str, int]], str | None]:
    entries = media_file_entries(msg)
    if not entries:
        return [], 'aucun média compatible'
    _unique, file_id, media_type, _size = entries[0]
    if media_type not in {'photo', 'video', 'animation', 'video_note'}:
        return [], None

    suffix = '.jpg' if media_type == 'photo' else '.mp4'
    path = None
    try:
        # Le réseau n'est pas sérialisé : seuls FFmpeg/Pillow, plus coûteux en
        # CPU, passent dans le sémaphore. Plusieurs téléchargements Telegram
        # peuvent donc avancer en parallèle.
        path = await _download_to_temp(bot, file_id, suffix)
        async with _MEDIA_ANALYSIS_SEMAPHORE:
            if media_type == 'photo':
                values = await asyncio.to_thread(_image_fingerprints, path)
            else:
                values = await asyncio.to_thread(_extract_video_fingerprints, path)
        return values, None
    except Exception as exc:
        error = f'{media_type}: {type(exc).__name__}: {exc}'
        logger.warning('[HASHBAN] Empreinte perceptuelle impossible: %s', error)
        return [], error
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


async def _analyse_media_once(bot: Bot, msg: Message, need_perceptual: bool = True):
    """Télécharge un média une seule fois puis calcule SHA + perceptuel.

    Avant cette optimisation, une vidéo normale pouvait être téléchargée une
    fois pour le SHA256 puis une seconde fois pour le fingerprint vidéo.
    """
    entries = media_file_entries(msg)
    if not entries:
        return None, [], 'aucun média compatible'
    _unique, file_id, media_type, _size = entries[0]
    suffix = '.jpg' if media_type == 'photo' else ('.mp4' if media_type in {'video','animation','video_note'} else '.bin')
    path = None
    errors: list[str] = []
    try:
        path = await _download_to_temp(bot, file_id, suffix)
        sha = 'sha256:' + await asyncio.to_thread(_sha256_path, path)
        fingerprints: list[tuple[str, str, int]] = []
        if need_perceptual and media_type in {'photo', 'video', 'animation', 'video_note'}:
            try:
                async with _MEDIA_ANALYSIS_SEMAPHORE:
                    if media_type == 'photo':
                        fingerprints = await asyncio.to_thread(_image_fingerprints, path)
                    else:
                        fingerprints = await asyncio.to_thread(_extract_video_fingerprints, path)
            except Exception as exc:
                errors.append(f'{media_type}: {type(exc).__name__}: {exc}')
        return sha, fingerprints, '; '.join(errors) if errors else None
    except Exception as exc:
        error = f'{media_type}: {type(exc).__name__}: {exc}'
        logger.warning('[HASHBAN] Analyse média impossible: %s', error)
        return None, [], error
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def _invalidate_ban_caches() -> None:
    global _BANNED_EXACT_CACHE
    _BAN_CAP_CACHE.clear()
    _BANNED_FP_CACHE.clear()
    _BANNED_EXACT_CACHE = None


async def _banned_exact_keys() -> set[str]:
    global _BANNED_EXACT_CACHE
    now = time.monotonic()
    if _BANNED_EXACT_CACHE and now < _BANNED_EXACT_CACHE[0]:
        return _BANNED_EXACT_CACHE[1]
    async with SessionLocal() as db:
        keys = set((await db.execute(select(MediaHash.file_unique_id).where(
            MediaHash.banned.is_(True)
        ))).scalars().all())
    _BANNED_EXACT_CACHE = (now + _BAN_CACHE_TTL_SECONDS, keys)
    return keys


async def _ban_capabilities(media_type: str) -> tuple[bool, bool]:
    now = time.monotonic()
    cached = _BAN_CAP_CACHE.get(media_type)
    if cached and now < cached[0]:
        return cached[1], cached[2]
    keys = await _banned_exact_keys()
    sha_exists = any(key.startswith('sha256:') for key in keys)
    async with SessionLocal() as db:
        fp_exists = (await db.execute(select(MediaFingerprint.id).where(
            MediaFingerprint.banned.is_(True),
            MediaFingerprint.media_type == media_type,
        ).limit(1))).scalar_one_or_none() is not None
    _BAN_CAP_CACHE[media_type] = (now + _BAN_CACHE_TTL_SECONDS, sha_exists, fp_exists)
    return sha_exists, fp_exists


async def _banned_fingerprint_groups(media_type: str) -> dict[str, dict[str, list[int]]]:
    now = time.monotonic()
    cached = _BANNED_FP_CACHE.get(media_type)
    if cached and now < cached[0]:
        return cached[1]
    async with SessionLocal() as db:
        rows = (await db.execute(select(
            MediaFingerprint.source_file_unique_id,
            MediaFingerprint.fingerprint_kind,
            MediaFingerprint.fingerprint,
        ).where(
            MediaFingerprint.banned.is_(True),
            MediaFingerprint.media_type == media_type,
        ))).all()
    grouped: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for source, kind, fingerprint in rows:
        grouped[source][kind].append(int(fingerprint, 16))
    # Convertit les defaultdict imbriqués en dict simples pour le cache.
    plain = {source: {kind: list(values) for kind, values in by_kind.items()} for source, by_kind in grouped.items()}
    _BANNED_FP_CACHE[media_type] = (now + _BAN_CACHE_TTL_SECONDS, plain)
    return plain


def _match_fingerprints(media_type: str, current: list[tuple[str, str, int]], grouped: dict[str, dict[str, list[int]]]):
    details = {'computed': len(current), 'best_distance': None, 'matched_frames': 0, 'required_frames': 0, 'source': None, 'error': None}
    # Conversion hex -> int une seule fois par empreinte courante. Avant, cette
    # conversion était répétée pour chaque vidéo bannie comparée.
    prepared = [(kind, int(value, 16), idx) for kind, value, idx in current]
    best_distance: int | None = None
    for source, by_kind in grouped.items():
        if media_type == 'photo':
            matches = 0
            for kind, value, _idx in prepared:
                olds = by_kind.get(kind, [])
                if not olds:
                    continue
                distance = min((value ^ old).bit_count() for old in olds)
                best_distance = distance if best_distance is None else min(best_distance, distance)
                if distance <= _IMAGE_DISTANCE_LIMIT:
                    matches += 1
            if matches >= 1:
                details.update(best_distance=best_distance, matched_frames=matches, required_frames=1, source=source)
                return True, details
            continue

        matched_positions: set[int] = set()
        frame_positions = {idx for _kind, _value, idx in prepared}
        required = max(3, math.ceil(len(frame_positions) * _VIDEO_MATCH_RATIO))
        for kind, value, idx in prepared:
            olds = by_kind.get(kind, [])
            if not olds:
                continue
            distance = min((value ^ old).bit_count() for old in olds)
            best_distance = distance if best_distance is None else min(best_distance, distance)
            if distance <= _VIDEO_DISTANCE_LIMIT:
                matched_positions.add(idx)
        if len(matched_positions) >= required:
            details.update(best_distance=best_distance, matched_frames=len(matched_positions), required_frames=required, source=source)
            return True, details
        details['required_frames'] = required
        details['matched_frames'] = max(details['matched_frames'], len(matched_positions))

    details['best_distance'] = best_distance
    return False, details


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


async def _upsert_exact_banned(db, *, key: str, user_id: int | None, file_id: str, media_type: str) -> None:
    rows = list((await db.execute(select(MediaHash).where(MediaHash.file_unique_id == key))).scalars().all())
    if rows:
        for row in rows:
            row.banned = True
            row.user_id = user_id if user_id is not None else row.user_id
            row.file_id = file_id
            row.media_type = media_type
    else:
        db.add(MediaHash(
            user_id=user_id,
            file_unique_id=key,
            file_id=file_id,
            media_type=media_type,
            banned=True,
        ))


async def ban_hashes_from_messages(messages: list[Message], bot: Bot) -> HashBanReport:
    report = HashBanReport()
    unique_messages: list[Message] = []
    seen_ids: set[tuple[int, int]] = set()
    for message in messages:
        key = (message.chat.id, message.message_id)
        if key not in seen_ids and media_file_entries(message):
            seen_ids.add(key)
            unique_messages.append(message)

    # Toutes les opérations lentes Telegram/FFmpeg sont réalisées sans garder
    # une connexion PostgreSQL ouverte.
    analysed = []
    for msg in unique_messages:
        unique, file_id, media_type, _size = media_file_entries(msg)[0]
        sha, fingerprints, error = await _analyse_media_once(bot, msg, need_perceptual=True)
        analysed.append((msg, unique, file_id, media_type, sha, fingerprints, error))

    async with SessionLocal() as db:
        for msg, unique, file_id, media_type, sha, fingerprints, error in analysed:
            report.media_count += 1
            user_id = msg.from_user.id if msg.from_user else None
            keys = [unique]
            if sha:
                keys.append(sha)
                report.sha256_count += 1
            else:
                report.errors.append(f'{media_type}: SHA256 non calculé')
            if error:
                report.errors.append(error)

            for key in keys:
                await _upsert_exact_banned(db, key=key, user_id=user_id, file_id=file_id, media_type=media_type)
                report.exact_keys += 1

            for kind, fingerprint, frame_index in fingerprints:
                rows = list((await db.execute(select(MediaFingerprint).where(
                    MediaFingerprint.fingerprint == fingerprint,
                    MediaFingerprint.fingerprint_kind == kind,
                    MediaFingerprint.source_file_unique_id == unique,
                ))).scalars().all())
                if rows:
                    for row in rows:
                        row.banned = True
                        row.user_id = user_id if user_id is not None else row.user_id
                        row.frame_index = frame_index
                else:
                    db.add(MediaFingerprint(
                        user_id=user_id,
                        source_file_unique_id=unique,
                        media_type=media_type,
                        fingerprint_kind=kind,
                        fingerprint=fingerprint,
                        frame_index=frame_index,
                        banned=True,
                    ))
                report.perceptual_count += 1
        await db.commit()

    _invalidate_ban_caches()
    return report


async def ban_hash_from_message(msg: Message, bot: Bot | None = None):
    """Compatibilité avec l'ancien appel. Avec bot, ajoute exact + perceptuel."""
    if not bot:
        return 0
    report = await ban_hashes_from_messages([msg], bot)
    return report.total


async def exact_banned_match(bot: Bot, msg: Message) -> tuple[bool, dict]:
    entries = media_file_entries(msg)
    details = {'telegram_match': False, 'sha_match': False, 'sha': None, 'errors': []}
    if not entries:
        return False, details
    unique, file_id, _media_type, _size = entries[0]
    async with SessionLocal() as db:
        telegram_match = (await db.execute(select(MediaHash.id).where(
            MediaHash.file_unique_id == unique,
            MediaHash.banned.is_(True),
        ).limit(1))).scalar_one_or_none() is not None
    details['telegram_match'] = telegram_match
    if telegram_match:
        return True, details

    # Le téléchargement peut durer : aucune connexion DB n'est gardée pendant ce temps.
    sha = await file_sha256(bot, file_id)
    details['sha'] = sha
    if not sha:
        details['errors'].append('SHA256 non calculé')
        return False, details
    async with SessionLocal() as db:
        sha_match = (await db.execute(select(MediaHash.id).where(
            MediaHash.file_unique_id == sha,
            MediaHash.banned.is_(True),
        ).limit(1))).scalar_one_or_none() is not None
    details['sha_match'] = sha_match
    return sha_match, details


async def perceptual_banned_match(bot: Bot, msg: Message) -> tuple[bool, dict]:
    entries = media_file_entries(msg)
    details = {'computed': 0, 'best_distance': None, 'matched_frames': 0, 'required_frames': 0, 'source': None, 'error': None}
    if not entries:
        return False, details
    _unique, _file_id, media_type, _size = entries[0]
    if media_type not in {'photo', 'video', 'animation', 'video_note'}:
        return False, details

    grouped = await _banned_fingerprint_groups(media_type)
    if not grouped:
        return False, details
    current, error = await perceptual_fingerprints(bot, msg)
    details['computed'] = len(current)
    details['error'] = error
    if not current:
        return False, details
    matched, match_details = _match_fingerprints(media_type, current, grouped)
    match_details['error'] = error
    return matched, match_details


async def contains_banned_hash(bot: Bot, msg: Message) -> tuple[bool, dict]:
    entries = media_file_entries(msg)
    if not entries:
        return False, {'method': 'none'}
    unique, file_id, media_type, _size = entries[0]

    # 1) Identifiant Telegram : cache mémoire, aucun aller-retour DB par média.
    banned_keys = await _banned_exact_keys()
    if unique in banned_keys:
        return True, {'method': 'telegram_id', 'telegram_match': True, 'sha_match': False}

    # 2) S'il n'existe ni SHA ni fingerprint banni, aucune raison de télécharger.
    sha_exists, fp_exists = await _ban_capabilities(media_type)
    if not sha_exists and not fp_exists:
        return False, {'method': 'none', 'telegram_match': False, 'sha_match': False}

    # 3) Téléchargement UNIQUE. On teste le SHA avant de lancer FFmpeg : un
    # repost exact est donc bloqué sans payer le coût de l'analyse perceptuelle.
    suffix = '.jpg' if media_type == 'photo' else ('.mp4' if media_type in {'video','animation','video_note'} else '.bin')
    path = None
    sha = None
    error = None
    try:
        path = await _download_to_temp(bot, file_id, suffix)
        sha = 'sha256:' + await asyncio.to_thread(_sha256_path, path)
        if sha_exists and sha in banned_keys:
            return True, {
                'method': 'sha256', 'telegram_match': False, 'sha_match': True,
                'sha': sha, 'error': None,
            }

        current: list[tuple[str, str, int]] = []
        if fp_exists and media_type in {'photo', 'video', 'animation', 'video_note'}:
            try:
                async with _MEDIA_ANALYSIS_SEMAPHORE:
                    if media_type == 'photo':
                        current = await asyncio.to_thread(_image_fingerprints, path)
                    else:
                        current = await asyncio.to_thread(_extract_video_fingerprints, path)
            except Exception as exc:
                error = f'{media_type}: {type(exc).__name__}: {exc}'
                logger.warning('[HASHBAN] Analyse perceptuelle impossible: %s', error)

        if fp_exists and current:
            grouped = await _banned_fingerprint_groups(media_type)
            matched, details = _match_fingerprints(media_type, current, grouped)
            details.update({
                'method': 'perceptual' if matched else 'none',
                'telegram_match': False, 'sha_match': False, 'sha': sha, 'error': error,
            })
            return matched, details

        return False, {
            'method': 'none', 'telegram_match': False, 'sha_match': False,
            'sha': sha, 'error': error, 'computed': len(current),
        }
    except Exception as exc:
        error = f'{media_type}: {type(exc).__name__}: {exc}'
        logger.warning('[HASHBAN] Analyse média impossible: %s', error)
        return False, {
            'method': 'none', 'telegram_match': False, 'sha_match': False,
            'sha': sha, 'error': error, 'computed': 0,
        }
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


async def hash_diagnostic(bot: Bot, msg: Message) -> str:
    entries = media_file_entries(msg)
    if not entries:
        return '❌ Réponds à une photo, une vidéo, une animation ou un document.'
    unique, _file_id, media_type, file_size = entries[0]
    exact, exact_details = await exact_banned_match(bot, msg)
    perceptual, perceptual_details = await perceptual_banned_match(bot, msg)

    async with SessionLocal() as db:
        id_rows = list((await db.execute(select(MediaHash).where(MediaHash.file_unique_id == unique))).scalars().all())
        sha_rows = []
        if exact_details.get('sha'):
            sha_rows = list((await db.execute(select(MediaHash).where(
                MediaHash.file_unique_id == exact_details['sha']
            ))).scalars().all())

    size_text = f'{file_size / 1024 / 1024:.2f} Mo' if file_size else 'inconnue'
    return '\n'.join([
        '🔎 /HASHDEMANDE',
        '',
        f'Type : {media_type}',
        f'Taille Telegram : {size_text}',
        '',
        f'file_unique_id : {unique}',
        f'Présent en base : {"✅ OUI" if id_rows else "❌ NON"}',
        f'Blacklist ID : {"✅ OUI" if any(x.banned for x in id_rows) else "❌ NON"}',
        '',
        f'SHA256 : {exact_details.get("sha") or "NON CALCULÉ"}',
        f'Présent en base SHA : {"✅ OUI" if sha_rows else "❌ NON"}',
        f'Blacklist SHA : {"✅ OUI" if any(x.banned for x in sha_rows) else "❌ NON"}',
        '',
        f'Correspondance exacte : {"✅ OUI" if exact else "❌ NON"}',
        f'Correspondance perceptuelle : {"✅ OUI" if perceptual else "❌ NON"}',
        f'Empreintes calculées : {perceptual_details.get("computed", 0)}',
        f'Meilleure distance : {perceptual_details.get("best_distance")}',
        f'Images concordantes : {perceptual_details.get("matched_frames", 0)}/{perceptual_details.get("required_frames", 0)}',
        f'Erreur : {perceptual_details.get("error") or "aucune"}',
    ])


async def banned_hash_count():
    async with SessionLocal() as db:
        exact = int((await db.execute(select(func.count(MediaHash.id)).where(MediaHash.banned.is_(True)))).scalar() or 0)
        perceptual = int((await db.execute(select(func.count(MediaFingerprint.id)).where(MediaFingerprint.banned.is_(True)))).scalar() or 0)
        return exact + perceptual
