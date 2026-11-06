"""PostgreSQL media ingestion, hash-ban enforcement, and moderator utilities.

The cheap Telegram ID check happens before downloading. CPU-intensive analysis
runs in an application-managed task; the user may see a media item briefly while
it is fingerprinted. On errors we log *incomplete coverage*, never claim clean.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import Counter

from media_guard import (from_message, get_media_file, fingerprints, file_cleanup,
                         photo_matches, video_matches)

log = logging.getLogger('guard.media')


def photo_buckets(value: str) -> list[str]:
    """16 x 16-bit buckets. Distance <=15 must share >=1 bucket."""
    if not value or len(value) != 64:
        return []
    return [f'{i:02x}:{value[i*4:i*4+4]}' for i in range(16)]


async def id_is_banned(db, unique_id: str) -> bool:
    return bool(await db.fetchval('''SELECT EXISTS(
       SELECT 1 FROM media_ban_keys WHERE key=$1
       UNION ALL SELECT 1 FROM banned_media WHERE media_id=$1)''', unique_id))


async def id_previously_seen(db, unique_id: str) -> bool:
    return bool(await db.fetchval('SELECT 1 FROM media_catalog WHERE unique_id=$1', unique_id))


async def mark_message(db, msg, info):
    await db.execute('''UPDATE messages SET media_id=$1, media_group_id=$2,
        media_type=$3 WHERE chat_id=$4 AND message_id=$5''', info.unique_id,
        str(msg.media_group_id) if msg.media_group_id else None,
        info.media_type, msg.chat_id, msg.message_id)


async def photo_banned(db, ph: str | None) -> bool:
    if not ph:
        return False
    candidates = await db.fetch('''SELECT DISTINCT b.phash
       FROM media_photo_buckets b WHERE b.bucket=ANY($1::text[])''',photo_buckets(ph))
    if any(photo_matches(ph, row['phash']) for row in candidates):
        return True
    # Backward-compatible V5 blacklist, which may not have been migrated yet.
    previous = await db.fetch('SELECT phash FROM banned_media WHERE phash IS NOT NULL')
    return any(photo_matches(ph,row['phash']) for row in previous)


async def video_banned(db, vid: str | None, duration, width, height) -> bool:
    if not vid:
        return False
    if duration is None:
        rows = await db.fetch('SELECT fingerprint,duration,width,height FROM media_video_bans')
    else:
        delta = max(3, round(duration*.05))
        rows = await db.fetch('''SELECT fingerprint,duration,width,height FROM media_video_bans
                                WHERE duration BETWEEN $1 AND $2 OR duration IS NULL''',
                              max(0,duration-delta),duration+delta)
    ratio = width/height if width and height else None
    for r in rows:
        ref_ratio = r['width']/r['height'] if r['width'] and r['height'] else None
        ok,_ = video_matches(vid, r['fingerprint'], duration_a=duration,
                             duration_b=r['duration'], ratio_a=ratio,ratio_b=ref_ratio)
        if ok:
            return True
    return False


async def store_fingerprints(db, msg, info, sha, ph, vid, status):
    await db.execute('''INSERT INTO media_catalog(unique_id,sha256,photo_phash,video_phash,
            media_type,user_id,file_size,duration,width,height,scan_status,last_seen)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now())
            ON CONFLICT(unique_id) DO UPDATE SET
             sha256=COALESCE(EXCLUDED.sha256,media_catalog.sha256),
             photo_phash=COALESCE(EXCLUDED.photo_phash,media_catalog.photo_phash),
             video_phash=COALESCE(EXCLUDED.video_phash,media_catalog.video_phash),
             last_seen=now(),scan_status=EXCLUDED.scan_status''',
             info.unique_id,sha,ph,vid,info.media_type,msg.from_user.id,
             info.file_size,info.duration,info.width,info.height,status)
    await db.execute('''UPDATE messages SET sha256=$1,phash=$2,video_phash=$3,
               scan_status=$4 WHERE chat_id=$5 AND message_id=$6''',
               sha,ph,vid,status,msg.chat_id,msg.message_id)


async def add_photo_ban(db, ph: str, user_id=None):
    if not ph or len(ph)!=64:
        return
    await db.execute('''INSERT INTO media_photo_bans(phash, banned_by) VALUES($1,$2)
                        ON CONFLICT(phash) DO NOTHING''',ph,user_id)
    await db.executemany('''INSERT INTO media_photo_buckets(bucket,phash) VALUES($1,$2)
                            ON CONFLICT DO NOTHING''',[(b,ph) for b in photo_buckets(ph)])


async def add_video_ban(db, vid: str, duration=None, width=None, height=None, user_id=None):
    if vid and vid.startswith('vphash:'):
        await db.execute('''INSERT INTO media_video_bans(fingerprint,duration,width,height,banned_by)
                          VALUES($1,$2,$3,$4,$5) ON CONFLICT(fingerprint) DO NOTHING''',
                         vid,duration,width,height,user_id)


async def ban_known_media(db, *, target_user_id=None, message_id=None, actor=None) -> dict:
    """Ban all known fingerprints from user OR the chosen single media/album.

    No download here: all recorded IDs are blocked immediately. Additional
    sha/perceptuals are copied from the catalog when available. In-flight scans
    inherit the ban state as soon as they finish.
    """
    if (target_user_id is None)==(message_id is None):
        raise ValueError('Provide target_user_id OR message_id')
    if target_user_id is not None:
        rows = await db.fetch('''SELECT DISTINCT m.media_id,COALESCE(m.sha256,c.sha256) AS sha,
          COALESCE(m.phash,c.photo_phash) AS ph,
          COALESCE(m.video_phash,c.video_phash) AS vf,
          c.duration,c.width,c.height FROM messages m
          LEFT JOIN media_catalog c ON c.unique_id=m.media_id
          WHERE m.user_id=$1 AND m.media_id IS NOT NULL''', target_user_id)
    else:
        origin = await db.fetchrow('''SELECT chat_id,media_group_id FROM messages
                                    WHERE message_id=$1 AND media_id IS NOT NULL ORDER BY created_at DESC LIMIT 1''', message_id)
        if not origin:
            return {'media':0,'exact':0,'photo':0,'video':0}
        rows = await db.fetch('''SELECT DISTINCT m.media_id,COALESCE(m.sha256,c.sha256) AS sha,
          COALESCE(m.phash,c.photo_phash) AS ph,
          COALESCE(m.video_phash,c.video_phash) AS vf,
          c.duration,c.width,c.height FROM messages m
          LEFT JOIN media_catalog c ON c.unique_id=m.media_id
          WHERE m.chat_id=$1 AND m.media_id IS NOT NULL
          AND (m.message_id=$2 OR ($3::text IS NOT NULL AND m.media_group_id=$3))''',
            origin['chat_id'],message_id,origin['media_group_id'])
    keys = set()
    for r in rows:
        if r['media_id']:keys.add(r['media_id'])
        if r['sha']:keys.add(r['sha'])
    if keys:
        await db.executemany('''INSERT INTO media_ban_keys(key,reason,banned_by)
           VALUES($1,'moderator',$2) ON CONFLICT(key) DO NOTHING''',[(k,actor) for k in keys])
    pset={r['ph'] for r in rows if r['ph']}
    for ph in pset:
        await add_photo_ban(db,ph,actor)
    vset={r['vf']:(r['duration'],r['width'],r['height']) for r in rows if r['vf']}
    for vid,(duration,width,height) in vset.items():
        await add_video_ban(db,vid,duration,width,height,actor)
    return {'media':len(rows),'exact':len(keys),'photo':len(pset),'video':len(vset)}


async def ban_from_message(db, message_id: int, actor) -> dict:
    return await ban_known_media(db,message_id=message_id,actor=actor)


async def match_sha(db, sha):
    return bool(sha and await db.fetchval('SELECT 1 FROM media_ban_keys WHERE key=$1',sha))


async def maybe_delete_album(bot,db,msg):
    """Best-effort delete every tracked item of the current album (48h API limit)."""
    ids=[msg.message_id]
    if msg.media_group_id:
        rows=await db.fetch('''SELECT message_id FROM messages WHERE chat_id=$1
                                AND media_group_id=$2''',msg.chat_id,str(msg.media_group_id))
        ids=list(set(ids+[r['message_id'] for r in rows]))
    async def remove(mid):
        try:await bot.delete_message(msg.chat_id,mid)
        except Exception:pass
    await asyncio.gather(*(remove(mid) for mid in ids))


async def background_scan(ctx, msg, *, ban_callback, anti_repost=False):
    db = ctx.application.bot_data['pools'][msg.chat_id]
    info=from_message(msg)
    if not info:
        return
    path = None
    try:
        path=await get_media_file(ctx.bot,info)
        if path is None:
            await store_fingerprints(db,msg,info,None,None,None,'unscannable')
            return
        sha,ph,vid=await fingerprints(path,info)
        # New /pedo might have banned this media while it was being processed.
        already_banned=await id_is_banned(db,info.unique_id)
        forbidden = (already_banned or await match_sha(db,sha) or
                     (await photo_banned(db,ph) if ph else False) or
                     (await video_banned(db,vid,info.duration,info.width,info.height) if vid else False))
        repost=False
        if not forbidden and anti_repost:
            # Perceptual repost check with thresholds, not a naïve string match.
            # Previous stored hashes must belong to an earlier different Telegram ID.
            candidates=await db.fetch('''SELECT sha256, photo_phash, video_phash,duration,width,height
                    FROM media_catalog
                    WHERE unique_id<>$1 AND media_type=$2
                    ORDER BY last_seen DESC LIMIT 250''', info.unique_id,info.media_type)
            for candidate in candidates:
                if (sha and candidate['sha256']==sha) or (ph and photo_matches(ph,candidate['photo_phash'])):
                    repost=True;break
                if vid and candidate['video_phash']:
                    x,y=info.width,info.height
                    xr,yr=candidate['width'],candidate['height']
                    yes,_=video_matches(vid,candidate['video_phash'], duration_a=info.duration,
                         duration_b=candidate['duration'],ratio_a=x/y if x and y else None,
                         ratio_b=xr/yr if xr and yr else None)
                    if yes: repost=True;break
        await store_fingerprints(db,msg,info,sha,ph,vid,'banned' if forbidden else 'repost' if repost else 'ok')
        if forbidden:
            # Propagate all measured keys only after a positive blacklist match.
            # This ensures the next re-upload can be caught at the fast-ID stage.
            await ban_known_media(db,message_id=msg.message_id,actor=None)
        if forbidden:
            await maybe_delete_album(ctx.bot,db,msg)
            await ban_callback(ctx.bot,msg.from_user.id)
        elif repost:
            await maybe_delete_album(ctx.bot,db,msg)  # no sanction
    except Exception:
        log.exception('Media scan failed (chat=%s msg=%s)',msg.chat_id,msg.message_id)
        try:await db.execute('UPDATE messages SET scan_status=$1 WHERE chat_id=$2 AND message_id=$3',
                             'error',msg.chat_id,msg.message_id)
        except Exception:log.exception('Cannot record media scan error')
    finally:
        file_cleanup(path)
