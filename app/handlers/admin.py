from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.config import get_settings
from app.db.models import GroupWordRule, WordRule
from app.db.session import SessionLocal
from app.keyboards.common import admin_kb, ads_admin_kb
from app.keyboards.common import (
    back_kb,
    broadcast_targets_kb,
    cleanup_kb,
    goal_kb,
    hashban_kb,
    invite_admin_kb,
    mod_kb,
    network_group_kb,
    network_groups_kb,
    network_lost_confirm_kb,
    rules_admin_kb,
    settings_kb,
    top_admin_kb,
)
from app.services import settings as st
from app.services.ads import (
    add_ad,
    ad_detail,
    ads_health_text,
    ads_list_kb,
    delete_ad,
    list_ads_text,
    send_ad_by_id,
    send_random_ad,
    set_ad_image,
    set_ad_text,
    toggle_ad,
)
from app.services.hashban import ban_hashes_from_messages, banned_hash_count
from app.services.health import health_text
from app.services.invites import (
    invite_health_text,
    send_invite_ad,
    send_invite_private,
    set_tiers_from_text,
    tiers_text,
    top_text,
)
from app.services.moderation import invalidate_word_cache
from app.services.network import (
    active_chat_id,
    approve_group,
    chat_id_from_start_arg,
    default_target_chat_id,
    get_group,
    get_network_state,
    group_display_name,
    group_health_check,
    invalidate_group_invites,
    invalidate_navigation_link,
    list_groups,
    mark_group_unavailable,
    network_dashboard_text,
    reject_group,
    selected_chat_id,
    set_failover_auto,
    set_fallback_group,
    set_group_enabled,
    set_group_public_link,
    refresh_group_navigation_link,
    set_selected_group,
)
from app.services.session_ops import cleanup_session, set_group_open, transfer_session
from app.services.state import ensure_all_status_messages, ensure_status_message, log_error, track

router = Router()


def is_admin(uid: int) -> bool:
    # Compatibilité : les TRUSTED_IDS gardent l'accès au panel comme avant.
    return uid in get_settings().all_admin_ids


def is_root_admin(uid: int) -> bool:
    # Les opérations de topologie réseau restent réservées aux vrais ADMIN_IDS.
    return uid in get_settings().admin_id_set


async def set_admin_state(uid: int, state: str):
    await st.set_value(f'admin_state:{uid}', state)


async def get_admin_state(uid: int):
    return await st.get_value(f'admin_state:{uid}', '')


async def clear_admin_state(uid: int):
    await st.set_value(f'admin_state:{uid}', '')


async def set_admin_target(uid: int, chat_id: int | None):
    await st.set_value(f'admin_target_group:{uid}', str(chat_id or ''))


async def get_admin_target(uid: int) -> int | None:
    raw = await st.get_value(f'admin_target_group:{uid}', '')
    if raw:
        try:
            gid = int(raw)
            group = await get_group(gid)
            if group and group.approved:
                return gid
        except Exception:
            pass
    return await active_chat_id() or await selected_chat_id() or await default_target_chat_id()


async def get_mod_scope(uid: int) -> str:
    value = await st.get_value(f'admin_mod_scope:{uid}', 'global')
    return value if value in {'global', 'group'} else 'global'


async def dashboard_text(uid: int) -> str:
    active = await active_chat_id()
    selected = await selected_chat_id()
    target = await get_admin_target(uid)
    return (
        '📊 PANEL ADMIN CENTRAL\n\n'
        f'🟢 Actif : {await group_display_name(active) if active else "aucun"}\n'
        f'🗳️ Sélectionné : {await group_display_name(selected) if selected else "aucun"}\n'
        f'🎯 Groupe cible panel : {await group_display_name(target) if target else "aucun"}\n\n'
        'Les sanctions graves et le hash-ban sont globaux.\n'
        'Invitations, règles locales, pubs, anti-repost et tracking restent propres à chaque groupe.'
    )


@router.message(CommandStart())
@router.message(Command('admin'))
async def start(msg: Message, bot: Bot):
    arg = ''
    if msg.text and len(msg.text.split(maxsplit=1)) > 1:
        arg = msg.text.split(maxsplit=1)[1].strip()
    if msg.chat.type != 'private' or not msg.from_user:
        return
    if arg == 'invite':
        await send_invite_private(bot, msg.from_user.id)
        return
    requested_group = chat_id_from_start_arg(arg)
    if requested_group is not None:
        # Un bouton maintenance ancien ne doit jamais créer un lien vers un
        # groupe qui n'est plus la destination du réseau.
        current_target = await default_target_chat_id()
        target = requested_group if requested_group == current_target else current_target
        await send_invite_private(bot, msg.from_user.id, target)
        return
    if is_admin(msg.from_user.id):
        await msg.answer(await dashboard_text(msg.from_user.id), reply_markup=admin_kb())
    else:
        await msg.answer('Bot actif.')


@router.callback_query(F.data.startswith('adm_'))
async def admin_cb(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer('Accès refusé', show_alert=True)
        return
    uid = cb.from_user.id
    data = cb.data
    target = await get_admin_target(uid)

    if data == 'adm_dashboard':
        await cb.message.answer(await dashboard_text(uid), reply_markup=admin_kb())
    elif data == 'adm_groups':
        groups = await list_groups(include_removed=False)
        state = await get_network_state()
        await cb.message.answer(await network_dashboard_text(), reply_markup=network_groups_kb(groups, state))
    elif data == 'adm_health':
        await cb.message.answer(await health_text(bot))
    elif data == 'adm_open':
        selected = await selected_chat_id()
        if not selected:
            await cb.answer('Aucun groupe sélectionné.', show_alert=True)
            return
        try:
            await set_group_open(bot, True, 'manual', chat_id=selected)
            await cb.message.answer(f'🟢 {await group_display_name(selected)} ouvert manuellement.')
        except Exception as exc:
            await cb.message.answer(f'❌ Ouverture impossible : {exc}')
    elif data == 'adm_close':
        active = await active_chat_id()
        if not active:
            await cb.answer('Aucun groupe actif.', show_alert=True)
            return
        try:
            await set_group_open(bot, False, 'manual', chat_id=active)
            await cb.message.answer('🔴 Groupe actif fermé.')
        except Exception as exc:
            await cb.message.answer(f'❌ Fermeture non confirmée : {exc}')
    elif data == 'adm_auto':
        cur = await st.auto_enabled()
        await st.set_value('auto_enabled', 'false' if cur else 'true')
        await ensure_all_status_messages(bot, recreate_on_change=True)
        await cb.message.answer(f'⏰ Horaire auto : {"OFF" if cur else "ON"}', reply_markup=admin_kb())
    elif data == 'adm_goal':
        if not target:
            await cb.answer('Aucun groupe cible.', show_alert=True)
            return
        await cb.message.answer(
            f'📦 {await group_display_name(target)}\nObjectif actuel : {await st.group_vote_goal(target)}',
            reply_markup=goal_kb(),
        )
    elif data == 'adm_cleanup':
        await cb.message.answer('🧹 Nettoyage du groupe actif/cible.', reply_markup=cleanup_kb())
    elif data == 'adm_suspects':
        await cb.message.answer('🕵️ Comptes suspects\n\nLes scores utilisateurs sont centralisés sur tout le réseau.', reply_markup=back_kb())
    elif data == 'adm_repost':
        if not target:
            await cb.answer('Aucun groupe cible.', show_alert=True)
            return
        cur = await st.group_bool(target, 'repost_enabled', False)
        await st.group_set_value(target, 'repost_enabled', 'false' if cur else 'true')
        await cb.message.answer(
            f'🔁 Anti-repost — {await group_display_name(target)} : {"OFF" if cur else "ON"}',
            reply_markup=admin_kb(),
        )
    elif data == 'adm_ads':
        if not target:
            await cb.answer('Aucun groupe cible.', show_alert=True)
            return
        enabled = await st.group_bool(target, 'ads_enabled', True)
        await cb.message.answer(
            f'📢 Publicités — {await group_display_name(target)}\n\nDiffusion automatique : {"ON" if enabled else "OFF"}',
            reply_markup=ads_admin_kb(),
        )
    elif data == 'adm_broadcast':
        groups = await list_groups(approved_only=True, include_removed=False)
        await cb.message.answer('📣 Choisis la destination du broadcast.', reply_markup=broadcast_targets_kb(groups))
    elif data == 'adm_invites':
        await cb.message.answer(
            f'🎁 Invitations — {await group_display_name(target) if target else "aucun groupe"}\n\n'
            'Les liens sont générés et suivis séparément pour chaque groupe.',
            reply_markup=invite_admin_kb(),
        )
    elif data == 'adm_top':
        await cb.message.answer(await top_text(), reply_markup=top_admin_kb())
    elif data == 'adm_mod':
        scope = await get_mod_scope(uid)
        await cb.message.answer(
            f'🛡️ Modération\nPortée actuelle : {"globale" if scope == "global" else "groupe cible"}.',
            reply_markup=mod_kb(scope),
        )
    elif data == 'adm_rules':
        await cb.message.answer(
            f'📜 Règles — groupe cible : {await group_display_name(target) if target else "aucun"}\n\n'
            'Les règles globales sont communes. Les règles locales s’ajoutent uniquement au groupe cible.',
            reply_markup=rules_admin_kb(),
        )
    elif data == 'adm_reports':
        await cb.message.answer('📊 Les rapports de fermeture indiquent désormais le groupe concerné.', reply_markup=back_kb())
    elif data == 'adm_hashban':
        await cb.message.answer('🚫 Hash ban GLOBAL\n\nUn média blacklisté ici est interdit dans tous les groupes.', reply_markup=hashban_kb())
    elif data == 'adm_settings':
        await cb.message.answer(
            f'⚙️ Paramètres — {await group_display_name(target) if target else "aucun groupe"}\nHoraires spécifiques au groupe cible.',
            reply_markup=settings_kb(),
        )
    await cb.answer()


@router.callback_query(F.data.startswith('net_group:'))
async def net_group_detail(cb: CallbackQuery):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    group = await get_group(chat_id)
    if not group:
        await cb.answer('Groupe introuvable', show_alert=True)
        return
    state = await get_network_state()
    text = (
        f'🏠 {group.title or group.chat_id}\n\n'
        f'ID : {group.chat_id}\nStatut : {group.status}\n'
        f'Approuvé : {"oui" if group.approved else "non"}\nON : {"oui" if group.enabled else "non"}\n'
        f'Lien maintenance : {group.public_link or ("@" + group.username if group.username else "non configuré")}\n'
        f'Échecs santé : {group.failure_count}'
    )
    await cb.message.answer(text, reply_markup=network_group_kb(group, state))
    await cb.answer()


@router.callback_query(F.data.startswith('net_approve:'))
async def net_approve(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    ok, text = await approve_group(bot, chat_id, cb.from_user.id)
    await cb.message.answer(text)
    if ok:
        await set_admin_target(cb.from_user.id, chat_id)
        # approve_group() a déjà confirmé la fermeture Telegram AVANT de
        # déclarer le groupe ON/CLOSED en base : aucun deuxième appel inutile.
        await ensure_all_status_messages(bot, recreate_on_change=True)
    await cb.answer()


@router.callback_query(F.data.startswith('net_reject:'))
async def net_reject(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    await reject_group(bot, chat_id, cb.from_user.id)
    await cb.message.answer('❌ Groupe refusé et bot retiré.')
    await cb.answer()


@router.callback_query(F.data.startswith('net_toggle:'))
async def net_toggle(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    group = await get_group(chat_id)
    desired_enabled = not bool(group and group.enabled)

    # Passage ON : on confirme AVANT la base que Telegram est joignable, que
    # le bot a les droits critiques et que le groupe peut être maintenu fermé.
    if desired_enabled:
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(chat_id, me.id)
            required = ('can_delete_messages', 'can_restrict_members', 'can_invite_users')
            missing = [name for name in required if getattr(member, name, False) is not True]
            if member.status != 'administrator' or missing:
                await cb.message.answer(
                    '❌ Impossible de mettre ON : bot non administrateur ou droits manquants : ' +
                    (', '.join(missing) if missing else member.status)
                )
                await cb.answer()
                return
            from app.services.session_ops import CLOSED_PERMS
            await bot.set_chat_permissions(chat_id, permissions=CLOSED_PERMS, request_timeout=10)
        except Exception as exc:
            await cb.message.answer(f'❌ Impossible de confirmer le groupe fermé : {type(exc).__name__}: {exc}')
            await cb.answer()
            return

    ok, state = await set_group_enabled(chat_id, desired_enabled, cb.from_user.id)
    if ok:
        if state == 'OFF':
            try:
                from app.services.session_ops import CLOSED_PERMS
                await bot.set_chat_permissions(chat_id, permissions=CLOSED_PERMS, request_timeout=10)
            except Exception:
                pass
            invalidated, revoked = await invalidate_group_invites(bot, chat_id, 'group_disabled')
            await invalidate_navigation_link(bot, chat_id)
            suffix = f' Invitations invalidées : {invalidated}.'
        else:
            suffix = ''
            # Si ce groupe revient après une panne, des sanctions ont pu être
            # créées pendant son absence. Elles sont réappliquées avant usage.
            try:
                from app.services.sanctions import reconcile_group_sanctions
                await reconcile_group_sanctions(bot, chat_id)
            except Exception as exc:
                await log_error(f'sanction_reconcile:{chat_id}', exc)
                suffix = ' ⚠️ Réconciliation des sanctions incomplète.'
            await refresh_group_navigation_link(bot, chat_id)
        await ensure_all_status_messages(bot, recreate_on_change=True)
        await cb.message.answer(f'✅ Groupe {state}.{suffix}')
    else:
        await cb.message.answer(f'❌ {state}')
    await cb.answer()


@router.callback_query(F.data.startswith('net_select:'))
async def net_select(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    if await set_selected_group(chat_id, cb.from_user.id):
        await set_admin_target(cb.from_user.id, chat_id)
        await ensure_all_status_messages(bot, recreate_on_change=True)
        await cb.message.answer(f'🗳️ Prochain vote/ouverture : {await group_display_name(chat_id)}')
    else:
        await cb.message.answer('❌ Groupe indisponible.')
    await cb.answer()


@router.callback_query(F.data.startswith('net_target:'))
async def net_target(cb: CallbackQuery):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    await set_admin_target(cb.from_user.id, chat_id)
    await cb.message.answer(f'🎯 Groupe cible du panel : {await group_display_name(chat_id)}')
    await cb.answer()


@router.callback_query(F.data.startswith('net_open:'))
async def net_open(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    try:
        await transfer_session(bot, chat_id)
        await set_admin_target(cb.from_user.id, chat_id)
        await cb.message.answer(f'🔄 Session transférée vers {await group_display_name(chat_id)}.')
    except Exception as exc:
        await cb.message.answer(f'❌ Transfert impossible : {exc}')
    await cb.answer()


@router.callback_query(F.data.startswith('net_fallback:'))
async def net_fallback(cb: CallbackQuery):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    if await set_fallback_group(chat_id, cb.from_user.id):
        await cb.message.answer(f'🛟 Groupe de secours : {await group_display_name(chat_id)}')
    else:
        await cb.message.answer('❌ Ce groupe ne peut pas servir de secours.')
    await cb.answer()


@router.callback_query(F.data.startswith('net_link:'))
async def net_link(cb: CallbackQuery):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    await set_admin_state(cb.from_user.id, f'network_link:{chat_id}')
    await cb.message.answer('🔗 Envoie le lien à afficher dans les groupes fermés (ex. https://t.me/...).\nEnvoie `off` pour le supprimer.')
    await cb.answer()


@router.callback_query(F.data.startswith('net_check:'))
async def net_check(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        required = ('can_delete_messages', 'can_restrict_members', 'can_invite_users')
        missing = [name for name in required if getattr(member, name, False) is not True]
        if member.status == 'administrator' and not missing:
            await cb.message.answer('✅ Telegram répond. Bot administrateur et droits critiques OK.')
        else:
            await cb.message.answer(
                f'⚠️ Statut bot : {member.status}\nDroits manquants : {", ".join(missing) if missing else "aucun"}'
            )
    except Exception as exc:
        await cb.message.answer(f'⚠️ Vérification échouée : {type(exc).__name__}: {exc}')
    await cb.answer()


@router.callback_query(F.data.startswith('net_lost_confirm:'))
async def net_lost_confirm(cb: CallbackQuery):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    await cb.message.answer(
        '⚠️ Cette action invalide toutes les invitations du groupe, annule son rôle actif/sélectionné et le retire de la rotation.',
        reply_markup=network_lost_confirm_kb(chat_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith('net_lost:'))
async def net_lost(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(':', 1)[1])
    replacement = await mark_group_unavailable(
        bot, chat_id, reason='déclaré manuellement par admin', lost=True, admin_id=cb.from_user.id,
    )
    await ensure_all_status_messages(bot, recreate_on_change=True)
    await cb.message.answer(
        f'💥 Groupe déclaré sauté.\nProchain groupe : {await group_display_name(replacement) if replacement else "aucun"}.'
    )
    await cb.answer()


@router.callback_query(F.data == 'net_failover_toggle')
async def net_failover_toggle(cb: CallbackQuery):
    if not cb.from_user or not is_root_admin(cb.from_user.id):
        return
    state = await get_network_state()
    await set_failover_auto(not state.failover_auto, cb.from_user.id)
    await cb.message.answer(f'🔄 Failover automatique : {"OFF" if state.failover_auto else "ON"}')
    await cb.answer()


@router.callback_query(F.data.startswith('goal_set:'))
async def cb_goal_set(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    target = await get_admin_target(cb.from_user.id)
    if not target:
        return
    value = int(cb.data.split(':')[1])
    await st.group_set_value(target, 'vote_goal', str(value))
    await ensure_status_message(bot, target)
    await cb.message.answer(f'✅ Objectif {await group_display_name(target)} : {value}', reply_markup=admin_kb())
    await cb.answer()


@router.callback_query(F.data.startswith('slot_set:'))
async def cb_slot(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    target = await get_admin_target(cb.from_user.id)
    if not target:
        return
    if await st.is_open(target):
        await cb.answer('Impossible pendant la session active de ce groupe.', show_alert=True)
        return
    slot = cb.data.split(':', 1)[1]
    await st.group_set_value(target, 'time_slot', slot)
    await ensure_status_message(bot, target)
    await cb.message.answer(f'✅ Horaire {await group_display_name(target)} : {slot}', reply_markup=admin_kb())
    await cb.answer()


@router.callback_query(F.data.startswith('await:'))
async def await_input(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    state = cb.data.split(':', 1)[1]
    await set_admin_state(cb.from_user.id, state)
    prompts = {
        'goal': 'Envoie le nouvel objectif en nombre.',
        'forbidden': 'Envoie le mot interdit à ajouter.',
        'banword': 'Envoie le mot BAN à ajouter.',
        'nameban': 'Envoie le mot interdit dans les noms.',
        'ad_text': 'Envoie le texte de la publicité.',
        'ad_image': 'Envoie l’image de la publicité avec texte en légende si besoin.',
        'hash_ban_media': 'Envoie le média à bannir globalement par hash.',
        'invite_text': 'Envoie le texte du message invitations.',
        'invite_image': 'Envoie l’image du message invitations.',
        'invite_tiers': 'Envoie les paliers : 1|Label|Lien GoFile',
    }
    await cb.message.answer('✍️ ' + prompts.get(state, 'Envoie la valeur.'))
    await cb.answer()


@router.callback_query(F.data == 'mod_scope_toggle')
async def mod_scope_toggle(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    current = await get_mod_scope(cb.from_user.id)
    new = 'group' if current == 'global' else 'global'
    await st.set_value(f'admin_mod_scope:{cb.from_user.id}', new)
    await cb.message.answer(
        f'🛡️ Portée modération : {"GLOBALE" if new == "global" else "GROUPE CIBLE"}',
        reply_markup=mod_kb(new),
    )
    await cb.answer()


@router.callback_query(F.data == 'cleanup_active')
async def cb_cleanup_active(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    target = await active_chat_id()
    d, f = await cleanup_session(bot, chat_id=target, all_known=False)
    await cb.message.answer(f'🧹 Nettoyage session terminé.\nSupprimés : {d}\nÉchecs : {f}')
    await cb.answer()


@router.callback_query(F.data == 'cleanup_all')
async def cb_cleanup_all(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    target = await get_admin_target(cb.from_user.id)
    d, f = await cleanup_session(bot, chat_id=target, all_known=True)
    await cb.message.answer(f'🧹 Nettoyage groupe cible terminé.\nSupprimés : {d}\nÉchecs : {f}')
    await cb.answer()


@router.callback_query(F.data == 'ads_toggle_global')
async def cb_ads_toggle_global(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    target = await get_admin_target(cb.from_user.id)
    if not target:
        return
    cur = await st.group_bool(target, 'ads_enabled', True)
    await st.group_set_value(target, 'ads_enabled', 'false' if cur else 'true')
    await cb.message.answer(f'📢 Publicités {await group_display_name(target)} : {"OFF" if cur else "ON"}', reply_markup=ads_admin_kb())
    await cb.answer()


@router.callback_query(F.data == 'ad_send')
async def cb_ad_send(cb: CallbackQuery, bot: Bot):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await get_admin_target(cb.from_user.id)
        mid = await send_random_ad(bot, force=True, chat_id=target)
        await cb.message.answer('📢 Pub envoyée.' if mid else 'Aucune pub active / aucun groupe cible.')
        await cb.answer()


@router.callback_query(F.data == 'ad_health')
async def cb_ad_health(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        await cb.message.answer(await ads_health_text(await get_admin_target(cb.from_user.id)))
        await cb.answer()


@router.callback_query(F.data == 'ad_list')
async def cb_ad_list(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        await cb.message.answer(await list_ads_text(), reply_markup=await ads_list_kb())
        await cb.answer()


@router.callback_query(F.data.startswith('ad_manage:'))
async def cb_ad_manage(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    ad_id = int(cb.data.split(':')[1])
    text, kb = await ad_detail(ad_id)
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith('ad_send_one:'))
async def cb_ad_send_one(cb: CallbackQuery, bot: Bot):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    target = await get_admin_target(cb.from_user.id)
    ad_id = int(cb.data.split(':')[1])
    mid = await send_ad_by_id(bot, ad_id, force=True, chat_id=target)
    await cb.message.answer('📢 Pub publiée dans le groupe cible.' if mid else 'Pub introuvable ou erreur.')
    await cb.answer()


@router.callback_query(F.data.startswith('ad_toggle:'))
async def cb_ad_toggle(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    ad_id = int(cb.data.split(':')[1])
    ok = await toggle_ad(ad_id)
    text, kb = await ad_detail(ad_id)
    await cb.message.answer(text if ok else 'Pub introuvable.', reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith('ad_delete:'))
async def cb_ad_delete(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    ad_id = int(cb.data.split(':')[1])
    ok = await delete_ad(ad_id)
    await cb.message.answer('🗑 Pub supprimée.' if ok else 'Pub introuvable.', reply_markup=await ads_list_kb())
    await cb.answer()


@router.callback_query(F.data == 'mod_lists')
async def cb_mod_lists(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    target = await get_admin_target(cb.from_user.id)
    async with SessionLocal() as db:
        globals_ = list((await db.execute(select(WordRule))).scalars().all())
        locals_ = []
        if target:
            locals_ = list((await db.execute(select(GroupWordRule).where(
                GroupWordRule.group_chat_id == target,
                GroupWordRule.enabled.is_(True),
            ))).scalars().all())
    lines = ['🛡️ Listes modération', '', '🌐 Globales :']
    lines += [f'• {row.kind}: {row.word}' for row in globals_[-60:]] or ['• aucune']
    lines += ['', f'🏠 Locales — {await group_display_name(target) if target else "aucun"} :']
    lines += [f'• {row.kind}: {row.word}' for row in locals_[-60:]] or ['• aucune']
    await cb.message.answer('\n'.join(lines))
    await cb.answer()


@router.callback_query(F.data == 'rules_edit_global')
async def rules_edit_global(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        await set_admin_state(cb.from_user.id, 'rules_text_global')
        await cb.message.answer('🌐 Envoie le nouveau texte des règles globales.')
        await cb.answer()


@router.callback_query(F.data == 'rules_edit_local')
async def rules_edit_local(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await get_admin_target(cb.from_user.id)
        if not target:
            await cb.answer('Aucun groupe cible.', show_alert=True)
            return
        await set_admin_state(cb.from_user.id, f'rules_text_local:{target}')
        await cb.message.answer(f'🏠 Envoie les règles spécifiques à {await group_display_name(target)}.')
        await cb.answer()


@router.callback_query(F.data == 'rules_clear_local')
async def rules_clear_local(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await get_admin_target(cb.from_user.id)
        if target:
            await st.group_set_value(target, 'rules_text_local', '')
            await cb.message.answer('🗑 Règles locales effacées.')
        await cb.answer()


@router.callback_query(F.data == 'rules_send')
async def cb_rules_send(cb: CallbackQuery, bot: Bot):
    if cb.from_user and is_admin(cb.from_user.id):
        from app.scheduler import rules_tick
        target = await get_admin_target(cb.from_user.id)
        await rules_tick(bot, force=True, chat_id=target)
        await cb.message.answer('📜 Règles publiées dans le groupe cible.')
        await cb.answer()


@router.callback_query(F.data == 'rules_health')
async def cb_rules_health(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await get_admin_target(cb.from_user.id)
        last = await st.group_get_value(target, 'last_rules_sent_at', 'jamais', inherit_global=False) if target else 'jamais'
        await cb.message.answer(f'📜 Règles\n\nGroupe : {await group_display_name(target) if target else "aucun"}\nDernier envoi : {last}')
        await cb.answer()


@router.callback_query(F.data == 'top_send')
async def cb_top_send(cb: CallbackQuery, bot: Bot):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await get_admin_target(cb.from_user.id)
        text = await top_text()
        if not target or 'Aucune statistique' in text:
            await cb.message.answer('🏆 Top inviteurs vide ou aucun groupe cible.')
        else:
            await bot.send_message(target, text)
            await st.group_set_value(target, 'last_top_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
            await cb.message.answer('🏆 Classement publié dans le groupe cible.')
        await cb.answer()


@router.callback_query(F.data == 'top_health')
async def cb_top_health(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await get_admin_target(cb.from_user.id)
        last = await st.group_get_value(target, 'last_top_sent_at', 'jamais', inherit_global=False) if target else 'jamais'
        await cb.message.answer('🏆 Top inviteurs\n\n' + await top_text() + f'\n\nDernier envoi groupe cible : {last}')
        await cb.answer()


@router.callback_query(F.data == 'hashban_stats')
async def cb_hashban_stats(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        await cb.message.answer(f'🚫 Hash bannis globaux : {await banned_hash_count()}')
        await cb.answer()


@router.callback_query(F.data == 'invite_send')
async def cb_invite_send(cb: CallbackQuery, bot: Bot):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await get_admin_target(cb.from_user.id)
        mid = await send_invite_ad(bot, force=True, chat_id=target)
        await cb.message.answer('🎁 Message invitations publié.' if mid else 'Erreur / aucun groupe cible.')
        await cb.answer()


@router.callback_query(F.data == 'invite_health')
async def cb_invite_health(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        await cb.message.answer(await invite_health_text(await get_admin_target(cb.from_user.id)))
        await cb.answer()


@router.callback_query(F.data == 'invite_tiers')
async def cb_invite_tiers(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        await cb.message.answer(await tiers_text(), reply_markup=invite_admin_kb())
        await cb.answer()


@router.callback_query(F.data.startswith('broadcast_target:'))
async def broadcast_target(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        return
    raw = cb.data.split(':', 1)[1]
    if raw == 'active':
        target = await active_chat_id()
        state = f'broadcast:{target}' if target else ''
    elif raw == 'selected':
        target = await selected_chat_id()
        state = f'broadcast:{target}' if target else ''
    elif raw == 'all':
        state = 'broadcast:all'
    else:
        target = int(raw)
        state = f'broadcast:{target}'
    if not state:
        await cb.answer('Aucun groupe correspondant.', show_alert=True)
        return
    await set_admin_state(cb.from_user.id, state)
    await cb.message.answer('📣 Envoie maintenant en privé le message à diffuser. Texte, photo, vidéo ou document seront copiés tels quels.')
    await cb.answer()


@router.message(F.chat.type == 'private')
async def admin_text_state(msg: Message, bot: Bot):
    if not msg.from_user or not is_admin(msg.from_user.id):
        return
    uid = msg.from_user.id
    state = await get_admin_state(uid)
    if not state:
        return
    target = await get_admin_target(uid)

    try:
        if state == 'goal':
            value = int(''.join(x for x in (msg.text or '') if x.isdigit()) or '0')
            if value > 0 and target:
                await st.group_set_value(target, 'vote_goal', str(value))
                await ensure_status_message(bot, target)
                await msg.answer(f'✅ Objectif {await group_display_name(target)} : {value}', reply_markup=admin_kb())
        elif state in {'forbidden', 'banword', 'nameban'}:
            kind = {'forbidden': 'forbidden', 'banword': 'ban', 'nameban': 'nameban'}[state]
            word = (msg.text or '').strip().lower()
            if word:
                scope = await get_mod_scope(uid)
                async with SessionLocal() as db:
                    if scope == 'group' and target:
                        db.add(GroupWordRule(group_chat_id=target, kind=kind, word=word, enabled=True))
                    else:
                        db.add(WordRule(kind=kind, word=word))
                    await db.commit()
                invalidate_word_cache()
                await msg.answer(
                    f'✅ Ajouté ({"global" if scope == "global" else await group_display_name(target)}) : {word}',
                    reply_markup=mod_kb(scope),
                )
        elif state == 'rules_text_global':
            await st.set_value('rules_text', msg.text or '')
            await msg.answer('✅ Règles globales sauvegardées.', reply_markup=rules_admin_kb())
        elif state.startswith('rules_text_local:'):
            chat_id = int(state.split(':', 1)[1])
            await st.group_set_value(chat_id, 'rules_text_local', msg.text or '')
            await msg.answer(f'✅ Règles locales {await group_display_name(chat_id)} sauvegardées.', reply_markup=rules_admin_kb())
        elif state.startswith('network_link:'):
            chat_id = int(state.split(':', 1)[1])
            value = (msg.text or '').strip()
            await set_group_public_link(chat_id, None if value.lower() == 'off' else value)
            await ensure_all_status_messages(bot, recreate_on_change=True)
            await msg.answer('✅ Lien maintenance mis à jour.')
        elif state.startswith('ad_edit_text:'):
            adid = int(state.split(':')[1])
            ok = await set_ad_text(adid, msg.text or '')
            await msg.answer('✅ Texte de la pub modifié.' if ok else 'Pub introuvable.', reply_markup=ads_admin_kb())
        elif state == 'ad_text':
            adid = await add_ad(text=msg.text or '')
            await msg.answer('✅ Publicité texte ajoutée.' if adid != -1 else 'Maximum 2 publicités configurées.', reply_markup=ads_admin_kb())
        elif state.startswith('ad_edit_image:'):
            adid = int(state.split(':')[1])
            if not msg.photo:
                await msg.answer('Envoie une image.')
                return
            ok = await set_ad_image(adid, msg.photo[-1].file_id)
            await msg.answer('✅ Image de la pub modifiée.' if ok else 'Pub introuvable.', reply_markup=ads_admin_kb())
        elif state == 'ad_image':
            if not msg.photo:
                await msg.answer('Envoie une image.')
                return
            adid = await add_ad(text=msg.caption or '', image_file_id=msg.photo[-1].file_id)
            await msg.answer('✅ Publicité image ajoutée.' if adid != -1 else 'Maximum 2 publicités configurées.', reply_markup=ads_admin_kb())
        elif state.startswith('broadcast:'):
            scope = state.split(':', 1)[1]
            if scope == 'all':
                groups = [g for g in await list_groups(approved_only=True, enabled_only=True) if g.status not in {'offline', 'lost', 'removed'}]
                targets = [g.chat_id for g in groups]
            else:
                targets = [int(scope)]
            sem = asyncio.Semaphore(4)
            async def copy_one(chat_id: int):
                async with sem:
                    try:
                        copied = await bot.copy_message(chat_id=chat_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
                        mid = getattr(copied, 'message_id', None)
                        if mid:
                            await track(chat_id, mid, None, 'broadcast', bool(msg.photo or msg.video or msg.document or msg.animation or msg.audio or msg.voice or msg.video_note))
                        return chat_id, True
                    except Exception as exc:
                        await log_error(f'broadcast:{chat_id}', exc)
                        return chat_id, False
            results = await asyncio.gather(*(copy_one(gid) for gid in targets)) if targets else []
            ok = sum(1 for _gid, success in results if success)
            await msg.answer(f'✅ Broadcast : {ok}/{len(targets)} groupe(s) servi(s).', reply_markup=admin_kb())
        elif state == 'invite_text':
            await st.set_value('invite_text', msg.text or '')
            await msg.answer('✅ Texte invitations sauvegardé.', reply_markup=invite_admin_kb())
        elif state == 'invite_image':
            if not msg.photo:
                await msg.answer('Envoie une image.')
                return
            await st.set_value('invite_image_file_id', msg.photo[-1].file_id)
            await msg.answer('✅ Image invitations sauvegardée.', reply_markup=invite_admin_kb())
        elif state == 'invite_tiers':
            ok = await set_tiers_from_text(msg.text or '')
            await msg.answer('✅ Paliers sauvegardés.' if ok else 'Format invalide.', reply_markup=invite_admin_kb())
        elif state == 'hash_ban_media':
            report = await ban_hashes_from_messages([msg], bot)
            if report.media_count:
                await msg.answer(report.admin_text('HASH BAN ADMIN GLOBAL'), reply_markup=hashban_kb())
            else:
                await msg.answer('Envoie une photo/vidéo/document à bannir par hash.')
                return
    finally:
        await clear_admin_state(uid)


@router.callback_query(F.data == 'manual_keep_open')
async def cb_manual_keep(cb: CallbackQuery):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await active_chat_id()
        if target:
            await st.group_set_values(target, {
                'manual_opened_at': datetime.utcnow().isoformat(),
                'manual_security_warned_at': '',
            })
        await cb.message.answer('✅ Le groupe actif reste ouvert. Nouveau contrôle dans 2h.')
        await cb.answer()


@router.callback_query(F.data == 'manual_security_close')
async def cb_manual_security_close(cb: CallbackQuery, bot: Bot):
    if cb.from_user and is_admin(cb.from_user.id):
        target = await active_chat_id()
        if target:
            await set_group_open(bot, False, 'security', chat_id=target)
        await cb.message.answer('🔒 Fermeture de sécurité exécutée.')
        await cb.answer()


@router.callback_query(F.data == 'noop')
async def cb_noop(cb: CallbackQuery):
    await cb.answer()
