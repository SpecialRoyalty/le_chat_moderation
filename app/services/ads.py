from __future__ import annotations

import random
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.db.models import Advertisement
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.network import active_chat_id, default_target_chat_id, get_group
from app.services.state import track


async def add_ad(text: str = '', image_file_id: str | None = None):
    async with SessionLocal() as db:
        count = int((await db.execute(select(func.count(Advertisement.id)))).scalar() or 0)
        if count >= 2:
            return -1
        ad = Advertisement(title='Pub', text=text, image_file_id=image_file_id, active=True)
        db.add(ad)
        await db.commit()
        return ad.id


async def list_ads_text():
    async with SessionLocal() as db:
        ads = list((await db.execute(select(Advertisement).order_by(Advertisement.id.desc()).limit(20))).scalars().all())
    if not ads:
        return '📢 Aucune publicité configurée.'
    return '📢 Publicités configurées\n\nClique sur une pub pour la gérer.'


async def ads_list_kb():
    async with SessionLocal() as db:
        ads = list((await db.execute(select(Advertisement).order_by(Advertisement.id.desc()).limit(20))).scalars().all())
    rows = []
    for ad in ads:
        label = f'#{ad.id} {"🟢" if ad.active else "🔴"} ' + ((ad.text or '[image seule]')[:28])
        rows.append([InlineKeyboardButton(text=label, callback_data=f'ad_manage:{ad.id}')])
    rows.append([InlineKeyboardButton(text='⬅️ Retour pubs', callback_data='adm_ads')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ad_detail(ad_id: int):
    async with SessionLocal() as db:
        ad = await db.get(Advertisement, ad_id)
    if not ad:
        return 'Pub introuvable.', None
    text = ad.text or '[sans texte]'
    msg = f'📢 Pub #{ad.id}\n\nStatut : {"active" if ad.active else "off"}\nImage : {"oui" if ad.image_file_id else "non"}\n\nTexte :\n{text}'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📤 Publier maintenant', callback_data=f'ad_send_one:{ad.id}')],
        [
            InlineKeyboardButton(text='📝 Modifier texte', callback_data=f'await:ad_edit_text:{ad.id}'),
            InlineKeyboardButton(text='🖼 Modifier image', callback_data=f'await:ad_edit_image:{ad.id}'),
        ],
        [InlineKeyboardButton(text='🟢/🔴 Activer/Désactiver', callback_data=f'ad_toggle:{ad.id}')],
        [InlineKeyboardButton(text='🗑 Supprimer cette pub', callback_data=f'ad_delete:{ad.id}')],
        [InlineKeyboardButton(text='📋 Retour liste pubs', callback_data='ad_list')],
    ])
    return msg, kb


async def toggle_ad(ad_id: int):
    async with SessionLocal() as db:
        ad = await db.get(Advertisement, ad_id)
        if not ad:
            return False
        ad.active = not ad.active
        await db.commit()
        return True


async def delete_ad(ad_id: int):
    async with SessionLocal() as db:
        ad = await db.get(Advertisement, ad_id)
        if not ad:
            return False
        await db.delete(ad)
        await db.commit()
        return True


async def set_ad_text(ad_id: int, text: str):
    async with SessionLocal() as db:
        ad = await db.get(Advertisement, ad_id)
        if not ad:
            return False
        ad.text = text
        await db.commit()
        return True


async def set_ad_image(ad_id: int, image_file_id: str):
    async with SessionLocal() as db:
        ad = await db.get(Advertisement, ad_id)
        if not ad:
            return False
        ad.image_file_id = image_file_id
        await db.commit()
        return True


async def _send_ad(bot: Bot, ad: Advertisement, chat_id: int):
    kb = None
    if ad.button_text and ad.button_url:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=ad.button_text, url=ad.button_url),
        ]])
    if ad.image_file_id:
        message = await bot.send_photo(chat_id, ad.image_file_id, caption=ad.text or None, reply_markup=kb)
    else:
        message = await bot.send_message(chat_id, ad.text or '📢 Publicité', reply_markup=kb)
    await track(chat_id, message.message_id, None, 'ad', bool(ad.image_file_id))
    await st.group_set_values(chat_id, {
        'last_ad_sent_at': datetime.utcnow().isoformat(timespec='seconds'),
        'last_ad_message_id': str(message.message_id),
        'last_ad_id': str(ad.id),
    })
    return message.message_id


async def send_ad_by_id(bot: Bot, ad_id: int, force: bool = True, chat_id: int | None = None):
    target = chat_id or await active_chat_id() or (await default_target_chat_id() if force else None)
    if not target:
        return None
    if not force and not await st.is_open(target):
        return None
    async with SessionLocal() as db:
        ad = await db.get(Advertisement, ad_id)
    if not ad:
        return None
    return await _send_ad(bot, ad, target)


async def send_random_ad(bot: Bot, force: bool = False, chat_id: int | None = None):
    target = chat_id or await active_chat_id() or (await default_target_chat_id() if force else None)
    if not target:
        return None
    if not force and not await st.group_bool(target, 'ads_enabled', True):
        return None
    if not force and not await st.is_open(target):
        return None
    async with SessionLocal() as db:
        ads = list((await db.execute(select(Advertisement).where(Advertisement.active.is_(True)))).scalars().all())
    if not ads:
        return None
    return await _send_ad(bot, random.choice(ads), target)


async def ads_health_text(chat_id: int | None = None):
    target = chat_id or await active_chat_id() or await default_target_chat_id()
    if not target:
        return '📢 Publicités\n\nAucun groupe cible.'
    group = await get_group(target)
    last = await st.group_get_value(target, 'last_ad_sent_at', 'jamais', inherit_global=False)
    mid = await st.group_get_value(target, 'last_ad_message_id', '-', inherit_global=False)
    state = 'ouvert' if await st.is_open(target) else 'fermé'
    enabled = 'ON' if await st.group_bool(target, 'ads_enabled', True) else 'OFF'
    return (
        f'📢 Publicités — {group.title if group else target}\n\n'
        f'Automatique : {enabled}\nGroupe : {state}\nDernier envoi : {last}\n'
        f'Dernier message ID : {mid}\nProchain envoi automatique : pendant ouverture selon planning si ON.'
    )
