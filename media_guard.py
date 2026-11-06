"""Safe bounded media fingerprinting for Telegram cloud API.

Inspired by the *user-provided* anti-javana hashban implementation:
file_unique_id => SHA256 => photo pHash / multi-frame video dHash.
No media bytes are stored in PostgreSQL; temporary files are deleted.

Use a local Bot API server to process files above the public 20 MiB limit.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

VIDEO_FRAME_LIMIT = 8
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
VIDEO_AVERAGE_DISTANCE = 7.0
VIDEO_MAX_DISTANCE = 14
PHOTO_MAX_DISTANCE = 15  # 256-bit imagehash pHash; intentionally conservative

# Limited CPU and memory: avoid ffmpeg storms on a small Railway worker.
_DOWNLOAD_GATE = asyncio.Semaphore(2)
_VIDEO_GATE = asyncio.Semaphore(1)


@dataclass(frozen=True)
class MediaInfo:
    media_type: str
    unique_id: str
    file_id: str
    file_size: Optional[int]
    duration: Optional[int]
    width: Optional[int]
    height: Optional[int]


def from_message(msg):
    item = msg.photo[-1] if msg.photo else msg.video
    if not item:
        return None
    return MediaInfo('photo' if msg.photo else 'video', item.file_unique_id,
                     item.file_id, getattr(item, 'file_size', None),
                     getattr(item, 'duration', None),
                     getattr(item, 'width', None), getattr(item, 'height', None))


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def image_phash(path: str) -> str:
    from PIL import Image, ImageOps
    import imagehash
    with Image.open(path) as obj:
        obj = ImageOps.exif_transpose(obj)
        obj.thumbnail((1200, 1200))
        return str(imagehash.phash(obj.convert('RGB'), hash_size=16))


def frame_dhash(path: str) -> str:
    """Compatibility with the old hashban's 64-bit horizontal dHash."""
    from PIL import Image
    with Image.open(path) as obj:
        obj = obj.convert('L').resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(obj.getdata())
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (pixels[row*9+col] > pixels[row*9+col+1])
    return f'{value:016x}'


def _ffmpeg_executable() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_fingerprint(path: str, duration: Optional[int]) -> Optional[str]:
    """Up to 8 evenly-spaced-ish frames; old vphash serialization retained.

    ffmpeg runs in a constrained subprocess. Frames never leave a temp folder.
    """
    short = duration is not None and duration <= 2
    interval = max(1, int((duration or 30) / 6))
    vf = 'fps=4,scale=192:-2' if short else f'fps=1/{interval},scale=192:-2'
    with tempfile.TemporaryDirectory(prefix='guard_frames_') as folder:
        pattern = str(Path(folder) / 'frame_%03d.jpg')
        subprocess.run([_ffmpeg_executable(), '-nostdin', '-hide_banner', '-loglevel', 'error',
                        '-threads', '1', '-i', path, '-vf', vf, '-frames:v', str(VIDEO_FRAME_LIMIT),
                        '-q:v', '3', pattern], check=True, timeout=45,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        hashes = [frame_dhash(str(p)) for p in sorted(Path(folder).glob('frame_*.jpg'))]
    if not hashes:
        return None
    if len(hashes) == 1:
        hashes *= 2
    return 'vphash:' + ','.join(hashes)


def bit_distance(left: str, right: str) -> int:
    if len(left) != len(right) or not left:
        raise ValueError('Incompatible fingerprint widths')
    return (int(left, 16) ^ int(right, 16)).bit_count()


def photo_matches(left: str, right: str) -> bool:
    if not left or not right or len(left) != 64 or len(right) != 64:
        return False
    try:
        return bit_distance(left, right) <= PHOTO_MAX_DISTANCE
    except ValueError:
        return False


def _sample(hashes: list[str], count: int) -> list[str]:
    if count <= 1:
        return hashes[:1]
    return [hashes[round(i*(len(hashes)-1)/(count-1))] for i in range(count)]


def video_matches(left: str, right: str, *, duration_a: Optional[int] = None,
                  duration_b: Optional[int] = None, ratio_a: Optional[float] = None,
                  ratio_b: Optional[float] = None) -> tuple[bool, float]:
    """Return (match, % similarity); avoid auto-bans on a single frame.

    Uses the legacy dHash format so existing fingerprints can be imported.
    """
    if not (left.startswith('vphash:') and right.startswith('vphash:')):
        return False, 0.0
    if duration_a is not None and duration_b is not None:
        if abs(duration_a-duration_b) > max(3, round(max(duration_a, duration_b)*.05)):
            return False, 0.0
    if ratio_a and ratio_b and abs(ratio_a-ratio_b)/max(ratio_a,ratio_b) > .08:
        return False, 0.0
    a = left[7:].split(',')
    b = right[7:].split(',')
    count = min(len(a), len(b), 6)
    if count < 2 or any(len(x)!=16 for x in a+b):
        return False, 0.0
    try:
        diffs = [bit_distance(x, y) for x,y in zip(_sample(a,count), _sample(b,count))]
    except ValueError:
        return False, 0.0
    average = sum(diffs)/len(diffs)
    similarity = max(0.0, 100.0*(1-average/64))
    # Exact fingerprint is reliable even for 2-frame short videos. A perceptual
    # ban of a different fingerprint needs 3 distinct sampling positions.
    exact = (left == right)
    ok = exact or (count >= 3 and average <= VIDEO_AVERAGE_DISTANCE
                   and max(diffs) <= VIDEO_MAX_DISTANCE)
    return ok, similarity


async def get_media_file(bot, info: MediaInfo) -> Optional[str]:
    """Download into bounded temporary storage; return path owned by caller.

    None means unscannable, NOT safe/approved. Exact ID checks remain active.
    """
    if info.file_size is not None and info.file_size > MAX_DOWNLOAD_BYTES:
        return None
    async with _DOWNLOAD_GATE:
        fd, path = tempfile.mkstemp(prefix='guard_media_', suffix='.mp4' if info.media_type=='video' else '.img')
        os.close(fd)
        success = False
        try:
            f = await bot.get_file(info.file_id)
            if f.file_size is not None and f.file_size > MAX_DOWNLOAD_BYTES:
                return None
            await f.download_to_drive(custom_path=path, read_timeout=45, write_timeout=45)
            success = bool(0 < os.stat(path).st_size <= MAX_DOWNLOAD_BYTES)
            return path if success else None
        except Exception:
            return None
        finally:
            if not success:
                try:os.unlink(path)
                except OSError:pass


async def fingerprints(path: str, info: MediaInfo) -> tuple[str, Optional[str], Optional[str]]:
    sha = await asyncio.to_thread(sha256_file, path)
    if info.media_type == 'photo':
        try:
            photo = await asyncio.to_thread(image_phash, path)
        except Exception:
            photo = None
        return sha, photo, None
    try:
        async with _VIDEO_GATE:
            video = await asyncio.to_thread(video_fingerprint, path, info.duration)
    except Exception:
        video = None
    return sha, None, video


def file_cleanup(path: Optional[str]) -> None:
    if path:
        try:os.unlink(path)
        except OSError:pass
