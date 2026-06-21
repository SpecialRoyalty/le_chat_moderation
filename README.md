# FINAL_CLEAN_V13_NAME_FIXES_AUTO_LOGS

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

## V5_SESSION_CLOSED_REDIF_FIX

Corrections :
- session fermée : tout message/service est supprimé immédiatement ;
- join/left/service messages supprimés ;
- plus d'avertissement participation au bot lui-même ;
- fermeture édite le message de session sans afficher le nombre de suppressions au public ;
- boutons ON/OFF avec vert/rouge ;
- `send_super_trusted_report` restauré ;
- rediffusion renforcée avec logs `REDIFFUSION COPY OK/ERROR`.

## V6_GLOBAL_STATUS_AUTO_MIDSCAN

Corrections :
- un seul message global de session, même après plusieurs ouvertures/fermetures ;
- fermeture et ouverture éditent toujours ce message global ;
- rediffusion ON affiche une erreur claire si le bot n'est pas admin ou si REDIFFUSION_GROUP_ID est mauvais ;
- auto ON : si `schedule_json` est configuré, le bot ouvre/ferme automatiquement ;
- au milieu d'une session automatique, le bot contacte les admins pour proposer le kick non-participants ;
- le bouton `👢 Non-participants` sert seulement à forcer ce scan manuellement.

## V7_AUTO_REMINDERS_REPOST

Ajouté/corrigé :
- horaires auto par défaut :
  - lundi à vendredi : 22:00 → 00:00
  - samedi : 23:00 → dimanche 01:00
  - dimanche : 22:30 → lundi 00:15
- rappels ouverture : toutes les heures, puis 30, 10, 5, 4, 3, 2, 1 min.
- rappels fermeture : 30, 15, 5, 4, 3, 2, 1 min.
- fermeture auto/manuelle purge tous les messages depuis la dernière ouverture.
- même session fermée : mot banni/hash banni piègent et bannissent, mot interdit sanctionne.
- bouton anti-repost ON/OFF.
- anti-repost ON : repost supprimé + message personnalisé 30s.
- anti-repost OFF : hash stocké mais pas de suppression repost.

## V8_HASH_PRIORITY_SAFE

Correction sécurité :
- ordre hash forcé dans `process_media_priority_v8` :
  1. `banned_hashes` via `any_banned`
  2. anti-repost via `any_existing`
  3. stockage hash après acceptation
- même logique en session ouverte et fermée.

## V9_RULES_GROUP_BROADCAST

Ajouté :
- bouton `📣 Broadcast groupe` : admin publie texte ou photo+légende dans le groupe.
- bouton `📜 Règles` :
  - règle 1 texte/image
  - règle 2 texte/image
  - aperçu
- En ouverture AUTO, chaque règle configurée est publiée une fois à un moment aléatoire de la session.
- Les messages de règles sont supprimés à la fermeture.

## V10_RULES_LINK_HASH_FIX

Corrections :
- lien détecté => ban direct (`LINK BAN MATCH`), pas restriction.
- session fermée : lien => ban direct aussi.
- hash banni => ban direct avant anti-repost (`V10 HASH PRIORITY: BANNED_HASH FIRST`).
- bouton `🧬 Hash média` dans le panel admin :
  - admin envoie un média en privé au bot ;
  - ses hash sont ajoutés à `banned_hashes` ;
  - si quelqu'un publie ce média ensuite => ban.
- commande bonus admin `/hashmedia` en réponse à un média dans le groupe.

## V11_RESTRICT_VISIBILITY

Corrections :
- suppression de la commande admin `/hashmedia`;
- le bouton `🧬 Hash média` reste disponible dans le panel admin;
- restriction = mute + retrait d'accès temporaire jusqu'à fin de restriction;
- admins / super trusted / trusted restent protégés des sanctions auto.

## V12_KICK_NOTICES_CLEANUP

Correction kick non-participants :
- pendant l'expulsion, les notifications Telegram natives `Bot removed X` restent visibles ;
- elles sont enregistrées dans `nonparticipant_kick_messages`;
- à la fermeture, elles sont supprimées avec les autres messages de kick/règles/rappels ;
- les join/left normaux hors kick restent supprimés immédiatement.
Logs :
- `NONPARTICIPANT KICK NOTICE KEPT`
- `NONPARTICIPANT KICK CLEANUP`

## V13_NAME_FIXES_AUTO_LOGS

Corrections :
- alias `ban_for_forbidden_username` ajouté pour supprimer le NameError au join ;
- `back_kb()` ajouté pour supprimer le NameError du bouton `🧬 Hash média` ;
- error handler ajouté pour éviter les logs `No error handlers are registered`;
- logs auto-horaire ajoutés :
  - `AUTO SCHEDULE DEBUG`
  - `AUTO OPENING REMINDER CHECK no_send`

Note : dimanche, avec ouverture 22h30, il n'y a pas de rappel à 21h00. Premier rappel attendu : 21h30.
