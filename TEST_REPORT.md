# Rapport de vérification — GROSCHAT Central Média V2

Date de préparation : 2026-10-08

## Résultat

`tests/verify_project.py` : **78 contrôles OK / 0 échec**.

Un test comportemental supplémentaire du BK-tree a également validé :

- correspondance photo à faible distance de Hamming ;
- refus vidéo à 4/6 frames pour le seuil anti-repost strict ;
- détection vidéo à 5/6 frames ;
- calcul correct du seuil anti-repost vidéo.

## Contrôles Média V2

- compilation de tout `app/` ;
- schéma historique strictement inchangé ;
- conservation simulée d'un ancien hash-ban pendant `create_all()` ;
- création additive de `global_media_registry_test` et `media_ban_jobs_test` ;
- pipeline unique hash-ban + anti-repost ;
- anti-repost réseau par ID Telegram, SHA256 et perceptuel ;
- un seul téléchargement du fichier pour SHA + fingerprint ;
- index BK-tree au lieu d'un scan perceptuel linéaire complet ;
- 6 frames pour l'analyse entrante et 12 pour la création `/pedo` ;
- variantes visuelles recadrées et miroir sur les nouveaux hash-bans ;
- reprise persistante d'une analyse `/pedo` échouée, jusqu'à 5 essais ;
- promotion de tous les anciens médias connus d'un utilisateur lors de `/pedo` ;
- scheduler de reprise hash-ban ;
- conservation de la propagation globale des bans ;
- compteur d'invitations cumulatif simple ;
- suppression des paliers/récompenses dans le code et le panel ;
- maintien de l'isolation des mots bannis ;
- maintien des protections réseau, invitations par groupe et failover.

## Limites des tests locaux

Les vrais téléchargements Telegram, les droits administrateur live et la PostgreSQL Railway de production nécessitent les credentials de production et ne sont pas simulés ici. Le code contient des timeouts, une file de reprise persistante et des chemins de dégradation afin qu'un échec d'analyse ne perde pas le `file_unique_id` déjà blacklisté.


## Correctif admins/trusted + média avant texte — 2026-10-09

- ADMIN_IDS inclus automatiquement dans le rôle trusted.
- Commandes trusted inchangées (`/supprime`, `/mineur`, `/pasfr`, `/pedo`, `/hashdemande`, `/clean`, `/info`).
- Écriture admins/trusted possible quand le groupe est fermé via exceptions Telegram individuelles.
- Les membres ordinaires restent bloqués par les permissions générales du groupe fermé.
- Toggle par groupe `media_before_text_enabled` ajouté dans `⚙️ Paramètres`, ON par défaut.
- `actions.py` et `hashban.py` sont inchangés par rapport à Media V2.
- Suite de vérification : 87/87 contrôles OK.
