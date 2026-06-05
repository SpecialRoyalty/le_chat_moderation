# Telegram Railway Bot - FINAL_COMPLETE_V39_SHARE_RANKING

Version cohérente nettoyée.

## Corrections V18

- Suppression totale de l'affichage `Vidéos : x/60`.
- Suppression du bouton/ancienne logique `Vidéos 60/60`.
- Suppression du message `Vidéo ajoutée : x/60`.
- Le système de récompenses utilise uniquement `🔗 Liens récompenses`.
- La publicité est bloquée tant que les 4 liens ne sont pas remplis :
  - palier 1
  - palier 10
  - palier 50
  - palier 60
- Le panel affiche maintenant :
  - `🔗 Liens récompenses : ✅ complets` ou `❌ incomplets`
- Le reste de la V17 est conservé :
  - horaires dynamiques ;
  - countdown corrigé ;
  - trusted moderation ;
  - bilan trusted fin de session ;
  - participation ;
  - ban hash ;
  - anti-repost ;
  - règles auto ;
  - mode RAID ;
  - sanctions silencieuses.

## Vérification Railway

Dans les logs :

STARTING FINAL_COMPLETE_V39_SHARE_RANKING

Si tu vois encore `Vidéos : x/60`, c'est que Railway tourne encore sur une ancienne version.


## V19 - Priorité de modération corrigée

Ordre appliqué :
1. `/ban` et `/supprime` trusted en priorité via CommandHandler.
2. Commandes `/` normales supprimées ; récidive mute 1 mois.
3. Admins/owner exemptés.
4. Liens interdits = ban direct.
5. Transferts interdits = ban direct.
6. Photo + mention = ban direct.
7. Hash interdit = ban direct.
8. Repost média = suppression.
9. Mots interdits = sanctions.
10. Participation obligatoire seulement en dernier.

Donc si un non-participant envoie un lien, il est banni pour lien.


## V20 - Kick non-participants contrôlé

Ajout :
- bouton `🥾 Kick non-participants ON/OFF`
- si OFF : seulement dissuasion et identification
- si ON : kick automatique après 3 jours sans participation
- limite : 20 kicks / jour
- nettoyage de l'état participant après kick
- bilan privé envoyé aux ADMIN_IDS après kick

Message d'avertissement enrichi :
- obligation d'envoyer 1 photo ou 1 vidéo jamais déjà postée
- une seule participation suffit à vie
- compteur total déjà supprimés
- état kick automatique ON/OFF
- limite 20 suppressions/jour


## V21 - Suppression robuste + DB propre

- Si Telegram répond `Message to delete not found`, le bot nettoie l'entrée PostgreSQL et continue.
- Plus de blocage de fermeture à cause d'anciens messages déjà supprimés.
- Ajout d'un durcissement du schéma au démarrage avec `ADD COLUMN IF NOT EXISTS`.
- Compatible base neuve et ancienne base partiellement migrée.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V22 - Correction complète SQL / hash / anti-repost

Correction complète :
- `media_hashes.hash` existe toujours.
- `banned_hashes.hash` existe toujours.
- `hash` est en TEXT.
- `hash` est UNIQUE.
- `ON CONFLICT(hash)` fonctionne.
- anti-repost refonctionne.
- ban hash refonctionne.
- participation avec média nouveau refonctionne.
- nettoyage automatique des vieilles lignes nulles/doublons.
- réparation automatique des anciennes DB Railway partiellement migrées.

Important :
- si tu veux repartir 100% propre, tu peux supprimer toutes les tables puis relancer.
- sinon V22 tente de réparer automatiquement le schéma.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING

Test rapide :
1. envoie une photo ;
2. renvoie exactement la même photo ;
3. le bot doit supprimer la deuxième avec `♻️ C’est du vu et déjà vu.`


## V23 - Modération silencieuse premium

Changements :
- Les non-trusted utilisant `/ban` ou `/supprimer` :
  - commande supprimée ;
  - mute 2 jours ;
  - warning discret auto-delete.

- Les trusted :
  - `/ban` silencieux ;
  - `/supprimer` silencieux ;
  - commandes supprimées automatiquement.

- Suppression des messages techniques :
  - plus de message hash ;
  - plus de validation participation ;
  - plus de spam mode raid.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V24 - Textes runtime corrigés

La logique V23 est conservée. Cette version corrige les textes réellement utilisés par les handlers :

- participation obligatoire ;
- anti-repost ;
- lien interdit ;
- transfert interdit ;
- mots interdits ;
- fake commandes modération ;
- avertissement non-participants ;
- lien privé ;
- récompense débloquée.

Messages techniques masqués :
- pas de mention hash en public ;
- pas de message mode raid ;
- pas de validation participation publique.


## V25 - Transferts autorisés

Changement :
- les messages transférés ne sont plus sanctionnés ;
- aucun ban/mute pour un forward ;
- tout le reste reste actif :
  - anti-liens ;
  - anti-repost ;
  - ban hash ;
  - participation obligatoire ;
  - trusted moderation ;
  - fake commandes `/ban` et `/supprimer` ;
  - kick non-participants.

Correction incluse :
- fix `MSG_FAKE_COMMAND` si la V24 contenait l'auto-référence cassée.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V26 - Fix punish_ban + rapports admin

Corrections :
- `punish_ban()` accepte maintenant `custom_message=None`.
- Corrige l'erreur : `punish_ban() takes 3 positional arguments but 4 were given`.
- Les logs `Chat not found` pour les rapports admin sont clarifiés :
  l'admin doit ouvrir le bot en privé au moins une fois.
- Transferts toujours autorisés comme en V25.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V27 - Hash robuste + message dissuasion modération

Ajouts :
- `MAX_HASH_FILE_MB=20`
- Si un média est trop gros :
  - pas de crash ;
  - pas de hash ;
  - participation non validée avec ce média ;
  - log : `HASH SKIPPED: file too big`.

Message dissuasion :
- toutes les 20 minutes pendant ouverture si sanctions > 0 ;
- supprimé automatiquement après 3 minutes ;
- affiche suppressions, exclusions et restrictions.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V28 - Fix trusted mute

Correction :
- Les TRUSTED_IDS ne sont plus touchés par le mute anti-commandes `/`.
- `/supprimer` devient un alias officiel de `/supprime`.
- `/ban`, `/supprime` et `/supprimer` restent réservés aux trusted/admins.
- Un non-trusted qui tente ces commandes reste mute 2 jours.
- Un trusted qui utilise ces commandes n'est jamais mute par ce système.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V29 - Priorité média interdit + trusted silencieux

Corrections :
- `banned_hashes` est vérifié AVANT `media_hashes`.
- Si un média interdit est republié : ban direct, même s'il était déjà connu comme repost.
- Quand un admin ajoute un média interdit, le même hash est retiré de `media_hashes`.
- `/ban` trusted : totalement silencieux dans le groupe.
- `/supprime` / `/supprimer` trusted : totalement silencieux dans le groupe.
- Aucune phrase publique ne parle de hash.
- Trusted jamais mute par les commandes.
- `/supprimer` reste alias officiel de `/supprime`.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V30_FRAMEHASH - Hash visuel photo + première frame vidéo

Cette version part de la V29 et change la logique média :

- Photo : hash visuel de l'image.
- Vidéo MP4/MOV : extraction de la toute première frame réelle, puis hash visuel.
- Le SHA fichier et file_unique_id restent en bonus, mais ne sont plus le coeur de la détection.
- Un média interdit est toujours vérifié avant le repost simple.
- Les tables existantes ne cassent pas : les nouveaux hash sont stockés dans les mêmes colonnes TEXT existantes.
- Les anciens hash restent en base, mais les nouveaux médias seront enregistrés avec les nouvelles empreintes.

Dépendances ajoutées :
- Pillow
- opencv-python-headless

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING


## V31_PURGE - purge complète après média interdit

Ajout :
- quand un utilisateur est banni :
  - tous ses messages session sont supprimés ;
  - tous ses médias session sont supprimés ;
  - purge silencieuse automatique ;
  - fonctionne aussi après détection média interdit.

La base de données reste compatible.


## V32_SCHEDULE_FIX - correction horaires après minuit

Bug corrigé :
- samedi 23h00 -> dimanche 01h00 reste bien la session du samedi après minuit ;
- dimanche 22h30 -> lundi 00h15 reste bien la session du dimanche après minuit ;
- le bot ne bascule plus sur l'horaire du nouveau jour trop tôt.

Rappels fermeture :
- 30 minutes avant ;
- 15 minutes avant ;
- 5/4/3/2/1 minutes avant.

Vérification Railway :
STARTING FINAL_COMPLETE_V39_SHARE_RANKING

## V33_VIP_ADS

Ajouts :
- `/pasfr` pour TRUSTED_IDS : supprime la commande, supprime le message ciblé, mute 2 jours, sans limite par session.
- Publicité 1 ON/OFF + texte.
- Publicité 2 ON/OFF + texte.
- Pub image + bouton `Mon lien`.
- Publication immédiate de la pub Mon lien.
- Publicités auto toutes les 10 minutes pendant ouverture.
- Classement inviteurs ON/OFF.
- Bouton `Mon lien` donne le lien unique en privé.
- Compatible DB existante.


## V34_REDIFFUSION

Nouveau système récompense :
- Objectif unique : 50 invitations validées.
- Récompense : lien GoFile de la rediffusion complète.
- Chaque nouveau lien GoFile crée une nouvelle campagne.
- Les compteurs repartent à zéro pour la nouvelle campagne.
- Les anciennes récompenses restent conservées dans `campaign_rewards`.
- Pub Mon lien et Pub campagne sont coordonnées : même texte/image/bouton.

Tables ajoutées non destructives :
- reward_campaigns
- campaign_rewards

Base existante compatible.


## V35_AUDIT_FIX

Corrections après audit global :
- ban automatique sur média interdit purge maintenant toute la session utilisateur ;
- `/ban` trusted conservé : rapide, silencieux, purge + médias interdits ;
- message participation obligatoire supprimé après 3 minutes ;
- messages refus/limite trusted supprimés après 3 minutes ;
- pas de modification SQL manuelle nécessaire.

Base :
- aucune table à supprimer ;
- créations non destructives uniquement avec `CREATE TABLE IF NOT EXISTS` et `ALTER TABLE IF EXISTS ... ADD COLUMN IF NOT EXISTS`.


## V37_GLOBAL_FIX

Version corrigée globale basée sur V35 stable.

Corrections :
- `/start` restauré et vérifié ;
- campagne rediffusion complète stable ;
- objectif configurable depuis le panel ;
- texte + image campagne configurables ;
- pub “Mon lien” coordonnée avec la campagne ;
- pas d’envoi privé massif lors d’un nouveau lien GoFile ;
- anciens textes “vidéos” supprimés ;
- vrais retours ligne Telegram ;
- `/ban` trusted rapide, silencieux, purge + médias interdits ;
- ban auto média interdit purge toute la session ;
- horaires V32 conservés ;
- migrations non destructives uniquement.

Aucune commande SQL manuelle obligatoire.


## V38_CALLBACK_FIX

Correction :
- ignore proprement les anciens callbacks Telegram expirés :
  `Query is too old and response timeout expired or query id is invalid`.
- protège les edits de vieux panels inline.
- vérifie que le bouton objectif campagne existe.
- conserve la V37 globale.

## V39_SHARE_RANKING

Nouveau système :
- un seul bouton admin `📣 Publicité partage` ;
- configuration texte + image ;
- publication avec bouton `🤝 Je partage` ;
- clic -> bot privé -> lien unique ;
- classement basé sur toutes les invitations validées existantes ;
- égalité départagée par score, première validation, création du lien, puis user_id ;
- Top 10 envoyé au milieu de session si classement ON ;
- notification privée seulement quand un utilisateur entre dans le Top 10 ;
- broadcast privé aux personnes qui ont déjà lancé le bot.

Aucune remise à zéro.
