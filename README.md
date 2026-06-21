# FINAL_CLEAN_V4_SESSION_FIX

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


## V3_SESSION_PUBLIC_AUTO
- ouverture/fermeture publient dans le groupe ;
- bouton ouverture auto ON/OFF remis ;
- état auto visible dans panel/info ;
- job auto présent sans horaires configurés.

## V4_SESSION_FIX

Corrections :
- un seul message de session : ouverture envoie/édite, fermeture édite le même message ;
- fermeture purge les messages de la session (`SESSION DELETE START/END`) ;
- callbacks du panel réalignés : mots bannis, usernames, pub partage, broadcast ;
- ouverture auto utilise `schedule_json` si configuré.
- Format `schedule_json` : `{"5":[["23:00","01:00"]],"6":[["22:30","00:15"]]}`
