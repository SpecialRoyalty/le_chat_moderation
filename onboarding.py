"""Telegram supergroup activation helpers.

Pure helpers live here so authorization and required permissions can be
unit-tested without contacting Telegram or installing runtime dependencies.
"""

# Only rights needed by the functions actually implemented in this bot.
# Telegram grants other privileges (e.g. promote admins), but they are not
# necessary and should not be requested merely to say "all permissions".
REQUIRED_ADMIN_RIGHTS = (
    ('can_delete_messages', 'Supprimer les messages'),
    ('can_restrict_members', 'Restreindre / bannir les membres et ouvrir / fermer le groupe'),
    ('can_invite_users', 'Créer les liens d’invitation'),
    ('can_pin_messages', 'Épingler les règles'),
)


def admin_status(member):
    return getattr(member, 'status', None) in ('administrator', 'creator')


def bot_permission_gaps(member):
    """Return missing rights (display labels), or 'admin' if not admin."""
    if not admin_status(member):
        return ['Être administrateur du supergroupe']
    if member.status == 'creator':
        return []
    return [label for field, label in REQUIRED_ADMIN_RIGHTS
            if not bool(getattr(member, field, False))]


def can_request_activation(actor_member, actor_user):
    """Only a real, non-bot Telegram group admin may activate a group."""
    return bool(actor_user and not getattr(actor_user, 'is_bot', False)
                and admin_status(actor_member))


def activation_text(chat_title, missing):
    rights = '\n'.join('• ' + label for _, label in REQUIRED_ADMIN_RIGHTS)
    status = ('⚠️ Droits manquants :\n' + '\n'.join('❌ ' + p for p in missing)
              if missing else '✅ Tous les droits nécessaires sont présents.')
    return (
        f'🔐 Configuration du bot — {chat_title}\n\n'
        'Un administrateur doit confirmer l’activation dans ce groupe.\n'
        'Le bot ne modérera personne avant la confirmation.\n\n'
        'Droits requis :\n' + rights + '\n\n' + status + '\n\n'
        'L’activation permet d’appliquer les horaires, modérer, supprimer les '
        'messages et gérer les invitations.\n'
        'Seul un administrateur du groupe peut valider.'
    )
