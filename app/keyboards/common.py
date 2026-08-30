from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def vote_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='🔓 Voter pour ouvrir le groupe', callback_data='vote_open')
    ]])


def group_redirect_kb(url: str, label: str = '➡️ Rejoindre le groupe actif'):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])


def admin_kb():
    rows = [
        [('🌐 Groupes / Réseau', 'adm_groups'), ('🟢 Santé', 'adm_health')],
        [('🔓 Ouvrir sélectionné', 'adm_open'), ('🔒 Fermer actif', 'adm_close')],
        [('⏰ Auto ON/OFF', 'adm_auto'), ('📦 Objectif', 'adm_goal')],
        [('🧹 Nettoyage', 'adm_cleanup'), ('🕵️ Suspects', 'adm_suspects')],
        [('🔁 Repost groupe', 'adm_repost'), ('📢 Publicités', 'adm_ads')],
        [('📣 Broadcast', 'adm_broadcast')],
        [('🎁 Invitations', 'adm_invites'), ('🏆 Top inviteurs', 'adm_top')],
        [('🛡️ Modération', 'adm_mod'), ('📜 Règles', 'adm_rules')],
        [('🚫 Hash ban global', 'adm_hashban')],
        [('📊 Rapports', 'adm_reports'), ('⚙️ Paramètres', 'adm_settings')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in rows
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='⬅️ Retour panel', callback_data='adm_dashboard')
    ]])


def goal_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='1', callback_data='goal_set:1'),
            InlineKeyboardButton(text='10', callback_data='goal_set:10'),
            InlineKeyboardButton(text='50', callback_data='goal_set:50'),
            InlineKeyboardButton(text='120', callback_data='goal_set:120'),
        ],
        [InlineKeyboardButton(text='✍️ Objectif personnalisé', callback_data='await:goal')],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='22h30 → 00h45', callback_data='slot_set:22:30-00:45')],
        [InlineKeyboardButton(text='22h00 → 00h00', callback_data='slot_set:22:00-00:00')],
        [InlineKeyboardButton(text='23h00 → 01h00', callback_data='slot_set:23:00-01:00')],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def cleanup_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🧹 Nettoyer session active', callback_data='cleanup_active')],
        [InlineKeyboardButton(text='🧹 Nettoyer groupe cible', callback_data='cleanup_all')],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def mod_kb(scope: str = 'global'):
    scope_label = '🌐 Portée : GLOBALE' if scope == 'global' else '🏠 Portée : GROUPE CIBLE'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=scope_label, callback_data='mod_scope_toggle')],
        [
            InlineKeyboardButton(text='➕ Mot interdit', callback_data='await:forbidden'),
            InlineKeyboardButton(text='➕ Mot ban', callback_data='await:banword'),
        ],
        [
            InlineKeyboardButton(text='➕ Nom ban', callback_data='await:nameban'),
            InlineKeyboardButton(text='📋 Voir listes', callback_data='mod_lists'),
        ],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def ads_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🟢/🔴 Publicités du groupe ON/OFF', callback_data='ads_toggle_global')],
        [
            InlineKeyboardButton(text='➕ Ajouter pub texte', callback_data='await:ad_text'),
            InlineKeyboardButton(text='🖼 Ajouter pub image', callback_data='await:ad_image'),
        ],
        [
            InlineKeyboardButton(text='📤 Publier dans groupe cible', callback_data='ad_send'),
            InlineKeyboardButton(text='📋 Liste pubs', callback_data='ad_list'),
        ],
        [InlineKeyboardButton(text='🩺 Vérifier diffusion', callback_data='ad_health')],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def rules_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📤 Publier dans groupe cible', callback_data='rules_send')],
        [InlineKeyboardButton(text='🌐 Modifier règles globales', callback_data='rules_edit_global')],
        [InlineKeyboardButton(text='🏠 Modifier règles locales', callback_data='rules_edit_local')],
        [InlineKeyboardButton(text='🗑 Effacer règles locales', callback_data='rules_clear_local')],
        [InlineKeyboardButton(text='🩺 Vérifier diffusion', callback_data='rules_health')],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def hashban_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='➕ Ajouter hash ban', callback_data='await:hash_ban_media')],
        [InlineKeyboardButton(text='📊 Stats hash ban', callback_data='hashban_stats')],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def top_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📤 Publier groupe cible', callback_data='top_send')],
        [InlineKeyboardButton(text='🩺 Vérifier top inviteurs', callback_data='top_health')],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def invite_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='📤 Publier groupe cible', callback_data='invite_send'),
            InlineKeyboardButton(text='🩺 Vérifier invitations', callback_data='invite_health'),
        ],
        [
            InlineKeyboardButton(text='📝 Modifier texte', callback_data='await:invite_text'),
            InlineKeyboardButton(text='🖼 Modifier image', callback_data='await:invite_image'),
        ],
        [
            InlineKeyboardButton(text='🎁 Voir paliers', callback_data='invite_tiers'),
            InlineKeyboardButton(text='✏️ Modifier paliers', callback_data='await:invite_tiers'),
        ],
        [InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')],
    ])


def network_groups_kb(groups, state):
    rows = []
    for g in groups:
        prefix = '🟢' if state.active_chat_id == g.chat_id else ('🗳️' if state.selected_chat_id == g.chat_id else '▫️')
        if not g.approved:
            prefix = '🟡'
        elif not g.enabled:
            prefix = '⚫'
        label = f'{prefix} {(g.title or str(g.chat_id))[:34]}'
        rows.append([InlineKeyboardButton(text=label, callback_data=f'net_group:{g.chat_id}')])
    rows += [
        [InlineKeyboardButton(text='🔄 Failover ON/OFF', callback_data='net_failover_toggle')],
        [InlineKeyboardButton(text='⬅️ Retour panel', callback_data='adm_dashboard')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def network_group_kb(group, state):
    rows = []
    if not group.approved:
        rows.append([
            InlineKeyboardButton(text='✅ Accepter', callback_data=f'net_approve:{group.chat_id}'),
            InlineKeyboardButton(text='❌ Refuser', callback_data=f'net_reject:{group.chat_id}'),
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                text='⚫ Mettre OFF' if group.enabled else '🟢 Mettre ON',
                callback_data=f'net_toggle:{group.chat_id}',
            )
        ])
        if group.enabled:
            rows.append([InlineKeyboardButton(text='🗳️ Sélectionner', callback_data=f'net_select:{group.chat_id}')])
            rows.append([InlineKeyboardButton(text='🔓 Ouvrir / transférer ici', callback_data=f'net_open:{group.chat_id}')])
            rows.append([InlineKeyboardButton(text='🛟 Définir secours', callback_data=f'net_fallback:{group.chat_id}')])
        rows.append([InlineKeyboardButton(text='🎯 Utiliser comme groupe cible', callback_data=f'net_target:{group.chat_id}')])
        rows.append([InlineKeyboardButton(text='🔗 Définir lien maintenance', callback_data=f'net_link:{group.chat_id}')])
        rows.append([InlineKeyboardButton(text='🔍 Vérifier maintenant', callback_data=f'net_check:{group.chat_id}')])
        rows.append([InlineKeyboardButton(text='🚨 Déclarer SAUTÉ', callback_data=f'net_lost_confirm:{group.chat_id}')])
    rows.append([InlineKeyboardButton(text='⬅️ Liste groupes', callback_data='adm_groups')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def network_lost_confirm_kb(chat_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🚨 CONFIRMER SAUTÉ', callback_data=f'net_lost:{chat_id}')],
        [InlineKeyboardButton(text='Annuler', callback_data=f'net_group:{chat_id}')],
    ])


def broadcast_targets_kb(groups):
    rows = [
        [InlineKeyboardButton(text='🟢 Groupe actif', callback_data='broadcast_target:active')],
        [InlineKeyboardButton(text='🗳️ Groupe sélectionné', callback_data='broadcast_target:selected')],
        [InlineKeyboardButton(text='🌐 Tous les groupes ON', callback_data='broadcast_target:all')],
    ]
    for g in groups[:30]:
        if g.approved:
            rows.append([InlineKeyboardButton(text=f'🏠 {(g.title or str(g.chat_id))[:38]}', callback_data=f'broadcast_target:{g.chat_id}')])
    rows.append([InlineKeyboardButton(text='⬅️ Retour', callback_data='adm_dashboard')])
    return InlineKeyboardMarkup(inline_keyboard=rows)
