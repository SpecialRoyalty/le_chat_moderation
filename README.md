# FINAL_CLEAN_V2_SESSIONS_NONPARTICIPANTS

Version propre recodée depuis le cahier des charges validé.

## Variables Railway

```env
BOT_TOKEN=
DATABASE_URL=
GROUP_ID=
BOT_USERNAME=
ADMIN_IDS=
TRUSTED_IDS=
SUPER_TRUSTED_IDS=
TZ=Europe/Paris
REDIFFUSION_GROUP_ID=
MAX_HASH_DOWNLOAD_BYTES=20971520
MISE_PYTHON_GITHUB_ATTESTATIONS=false
```

## Auto-test avant déploiement

```bash
python -m py_compile bot.py
python selftest.py
```

Résultat attendu :

```text
SELFTEST OK
```

## Tests manuels avant ouverture

1. `snap` dans `banned_words`, envoyer `tu as son snap` avec un membre normal → suppression + mute.
2. `zzhardtest` dans `banned_words_hard`, envoyer `zzhardtest` → ban + alerte admin.
3. `hi` dans `forbidden_usernames`, faire rejoindre `Mathias` → pas de ban.
4. Faire rejoindre un compte nommé `hi` → ban username.
5. Envoyer un média puis `/pedo` en réponse avec admin/trusted → ban + hash stocké.
6. Réenvoyer le même média avec autre compte → `BANNED HASH MATCH` + ban.
7. Admin ID utilise `/ban` en réponse → commande acceptée.
8. Membre normal tape `/ban` → mute 2 jours.
9. Participation ON : compte sans média envoie texte → suppression + rappel 10 secondes.
10. Forward média → autorisé. Forward texte → ban.

## Priorités

1. Hash média.
2. `banned_hashes` avant `media_hashes`.
3. Mots bannis.
4. Mots interdits.
5. Liens / forwards texte / bots / live.
6. Média + caption `@`.
7. Participation.
8. Repost normal.
9. Rediffusion.
10. Enregistrement média.

## Supprimé définitivement

- Grâce présidentielle.
- Grâce ministérielle.
- Réparation anciennes restrictions.
- Anciens liens récompenses.
- Ancienne campagne GoFile.
- Ancien doublon publier publicité.

## V2
Sessions, info système, auto-test, non-participants, messages 30s.
