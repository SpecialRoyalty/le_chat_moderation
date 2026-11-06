> **Inventaire historique V6.** Les choix de la V7 sont dans [DECISIONS_V7.md](DECISIONS_V7.md). Les modules exclus par le propriétaire ne constituent plus une liste de fonctionnalités à porter.

# Audit comparatif — ancien ANTIJAVANA vs Telegram Guard V6

**Source examinée** : archive `antijavana_chat_bot_nouveau-main.zip` fournie par l'utilisateur. L'examen couvre les modules `app/services`, `app/handlers`, les modèles SQLAlchemy et les notes de mise à jour. Il s'agit d'un **audit de code et de documentation**, pas d'un test réel du bot ancien sur Telegram. Les fonctionnalités documentées peuvent comporter des bugs de production.

Légende : **V6** = implémentation présente dans le ZIP actuel ; **Partiel** = seulement une fraction ; **À porter** = ancienne fonction repérée mais non intégrée à la V6 ; **Non applicable** = conflit avec architecture V6 mono-groupe.

## 1 — Modération, médias et sécurité

| Fonction de l'ancien bot | Preuve dans la source | V6 | Points à vérifier avant production |
|---|---|---|---|
| Hash-ban `file_unique_id` Telegram | `services/hashban.py` | **V6** | Révoquer les droits bot si nécessaire ; droits admin pour bannir |
| SHA-256 du fichier téléchargé | `services/hashban.py` | **V6** | Le Bot API Telegram public limite le téléchargement à env. 20 MiB ; au-delà l'ID exact seul fonctionne |
| Vidéos perceptuelles multi-frames (FFmpeg, dHash) | `services/hashban.py`, `PERCEPTUAL_HASH_UPDATE.md` | **V6** | Traitement asynchrone ; certaines vidéos visibles avant décision ; tester des réencodages réels |
| Photo perceptuelle pHash | **Absent** de l'ancien (V5 mieux fournie) | **V6** | Seuil 15 bits / 256 ; faux positifs possibles |
| Traitement des albums | `services/hashban.py`, `HASHBAN_AUDIT_UPDATE.md` | **Partiel** | Suppression/blacklist des éléments déjà reçus seulement, pas garantie des éléments arrivant plus tard |
| Audit `/hashdemande` avec empreintes et statut | `services/actions.py` | **V6** | Informations des médias effectivement suivis ; pas de fichier déjà effacé/non observé |
| Ajout Hash-ban admin avec preuves d'écriture | `services/hashban.py` | **Partiel** | V6 a « Empreintes → Bannir » et `/pedo`, sans transaction complète sur scan de fichier avant ban |
| Anti-repost ON/OFF ; suppression sans sanction | `services/anti_repost.py` | **V6**, partiel perceptuel | Comparaison approximative sur médias récents ; aucun index complet de proximité sur l'historique |
| Anti-publication immédiate ON/OFF (1/3/5/10/15 min) | `services/anti_fast_join.py` | **V6** | Protège seulement les arrivées observées et les médias reconnus (photo/vidéo) |
| Suppression de médias de l'auteur lors d'une sanction | `services/actions.py` | **V6** | Limitée par Telegram et les messages enregistrés |
| Mots *complets* Unicode NFKC, `casefold` | `services/moderation.py`, `EXACT_WORDS_UPDATE.md` | **Partiel** | V6 emploie encore un regex simple ; améliorer normalisation pour correspondance équivalente |
| Trois listes de mots (nameban, ban direct, forbidden mute) | `services/moderation.py` | **Partiel** | V6 a mots interdits mute et fragments de noms bannis ; **liste mots = ban direct** à porter |
| Détection des mentions et liens / restrictions de formats | `services/moderation.py` | **Partiel** | Vérifier toutes variantes de liens, invitations Telegram et entités cachées |
| Trusted `/pedo`, `/mineur` | `services/actions.py` | **V6** | Différences historiques : `/mineur` ancien mute 1j, demande utilisateur 3j, V6 suit la demande récente |
| Trusted `/supprime`, `/pasfr`, `/clean`, `/info`, `/hashdemande` | `services/actions.py` | **V6** | `clean` borné à 300 messages ; journalisation et droits des trusted |
| Journal des commandes trusted | `app/db/models.py::TrustedAction` | **V6** | Rétention 90 jours, sans conservation du texte intégral |
| Anonymisation, score de suspicion, suivi de présence | `services/users.py`, `app/db/models.py` | **Partiel** | Les métriques des anciennes sessions ne sont pas migrées |
| Ban/mute manuels synchronisés, blocage en liste globale | `services/multigroup.py` | **À porter** | Nécessite architecture multi-groupes et audit de propagation de permissions |

## 2 — Sessions, groupe, « justice »

| Fonction | Source | Statut V6 |
|---|---|---|
| Mode groupe A / groupe B / aucune ouverture | `services/multigroup.py` | **À porter** : V6 gère un seul `GROUP_ID` |
| Enregistrement des groupes inconnus en attente et attribution de rôle | `services/multigroup.py` | **Partiel** : V6 demande la confirmation d'un admin Telegram mais refuse les autres `GROUP_ID` |
| Fermeture du groupe secondaire avec redirection | `services/multigroup.py::sync_redirections` | **À porter** |
| Failover dégradé / indisponible, plusieurs erreurs avant de déclarer un chat mort | `services/multigroup.py::health_monitor_tick` | **À porter** |
| Plages horaires, compte à rebours, auto, ouverture manuelle et fermeture | `services/session_ops.py`, `app/scheduler.py` | **V6**, implémentation différente |
| Alerte admin + bouton « Maintenir ouvert » après 2h, fermeture 5min ensuite | `services/session_ops.py::security_close_if_manual` | **Partiel** : V6 ferme à 2h, avertit 15min avant ; pas de prolongation par bouton ancien |
| Suivi des messages, nettoyage de session, rapports | `services/session_ops.py` | **Partiel** : suppression et stats présents ; ancien rapport détaillé non porté |
| Vote d'ouverture / objectif paramétrable | `keyboards/common.py::vote_kb`, `services/settings.py` | **À porter** |
| Justice des inactifs (3 sessions sans média ou 14 sans participation) | `services/justice.py` | **À porter** : V6 conserve stats mais pas la purge candidate par session |
| Aperçu des candidats, limite, traitement une fois par session, réessai des kicks | `services/justice.py` | **À porter** |
| Pause 5min du groupe lors de la justice | `services/justice.py` | **À porter** ; impact sur horaires à concevoir avant activation |
| « Grâce présidentielle » : lever tous les bans actifs | `services/session_ops.py` | **À porter** ; action dangereuse, confirmation double nécessaire |
| « Grâce ministérielle » : lever tous les mutes actifs | `services/session_ops.py` | **À porter** ; confirmation double nécessaire |

## 3 — Publicités, VIP et finance

| Fonction | Source | Statut V6 |
|---|---|---|
| Publicités multi-campagnes texte, photo, légende, diffusion manuelle | `services/ads.py` | **V6** |
| Broadcast groupe / privé à `/start` | `services/broadcast.py` | **Partiel** : groupe via publications, broadcast privé de masse à porter |
| Broadcast VIP Pass soirée / Pass Total / VIP JAVANA selon accès actifs | `services/broadcast.py` | **À porter** |
| Groupe actif A/B comme cible, publication manuelle A/B/les deux même fermé | `MULTIGROUP_CENTRAL_UPDATE.md` | **À porter** |
| Pubs et règles ciblées, statut de diffusion/erreurs | `services/ads.py`, `services/health.py` | **Partiel** |
| Cagnotte crowdfunding (objectif, barre de progression, preuve, admin validation) | `services/crowdfunding.py` | **À porter** |
| Accès VIP communs, achat et justificatif | `services/vip.py` | **Partiel** : V6 catalogue multi-VIP en EUR et justification manuelle, pas logique multi-groupes |
| Cycle spécifique Pass soirée payé (preuves avant/après 06h, délivrance 12h, expiration 06h) | `services/vip.py`, `PASS_SOIREE_CYCLE_UPDATE.md` | **À porter** |
| Expiration VIP confirmée par Telegram avant validation en base, retries | `services/vip.py` | **Partiel** : V6 tente le kick puis supprime l'accès à l'échéance |
| Test réel accès VIP : création/révocation de lien, envoi/suppression | `services/multigroup.py`, `services/vip.py` | **À porter** |
| Copie automatique de médias publiés vers les groupes VIP | `services/vip.py::copy_media_to_vip` | **À porter**, choisir filtrage préalable et destination avec consentement approprié |
| Récompenses Pass soirée **gratuits** avec places, cooldown, activité/invitations | `services/freepass.py` | **À porter** |
| Verrouillage de l'offre gratuite, réservation, bénéficiaires, redistribution | `services/freepass.py` | **À porter** |
| « Repost VIP » ON/OFF | `services/vip.py`, `VIP_MODERATION_REPOST_FIX.md` | **À porter** |

> Attention : Telegram impose normalement le paiement en Stars pour les produits/services numériques vendus dans un bot. Le parcours manuel externe V6 hérité de la V5 ne constitue pas une garantie de conformité, et un justificatif image n'est pas une preuve de règlement bancaire.

## 4 — Invitations, engagement et fiabilité

| Fonction | Source | Statut V6 |
|---|---|---|
| Lien unique, crédit après 5min et classement TOP10 | `services/invites.py` | **V6** pour un seul groupe |
| Un seul lien propriétaire A/B, même score global, migration lors d'indisponibilité | `services/invites.py` | **À porter** |
| Notification privée du point + position en direct | `services/invites.py` | **À porter** |
| TOP 10 deux fois/session (après justice et vers 75% de l'ouverture) | `app/scheduler.py` | **À porter** |
| TOP3 récompensé VIP par contact administrateur | `services/invites.py` | **À porter** |
| Archive + remise à zéro des concours | `services/invites.py`, `handlers/admin.py` | **À porter** |
| Révocation du lien individuel après bannissement/indisponibilité | `services/invites.py` | **À porter** |
| Health : erreurs, sujets suspects, sanctions, liens, suivi, reports | `services/health.py` | **Partiel** : V6 menu Santé minimal |
| Test infrastructure multi-groupes et VIP | `services/multigroup.py` | **À porter** |
| État « degraded » plutôt que failover sur erreur Telegram ponctuelle | `services/multigroup.py` | **À porter** |
| Rétention/maintenance et réduction des requêtes par cache | `services/settings.py`, `FAST_OPTIMIZATIONS.md` | **Partiel** : V6 rétention pour messages/médias, cache ancien non repris |

## Décisions d'intégration

- **Dès cette V6** : hashban exact/sha/photo/vidéo, anti-repost ON/OFF (y compris certains rapprochements perceptuels), anti-publication immédiate, audit médias dans le panneau, commandes trusted historiques, journal trusted, import des bans médias de l'ancienne base en mode simulation.
- **Conservés depuis la V5** : panneau propriétaire à boutons, groupe sécurisé, horaires, ouverture manuelle, pub, VIP, invitations.
- **Pas annoncés comme réalisés** : multi-groupes A/B, justice avancée, crowdfunding, freepass, broadcast privé/VIP, vote, grâce globale, failover, copie vers VIP, concours TOP3, notifications de rang. Ils requièrent modèles DB, UI, états d'exécution et tests complémentaires. Ne pas activer un système destructeur non testé.

## Import des bans historiques

`python scripts/import_legacy_hashes.py` affiche ce qui serait importé, **sans écrire**. Ajouter `--apply --backup-confirmed` après sauvegarde. Seuls les `media_hashes.banned = true` et `video_fingerprints.banned = true` sont importés ; les données de membres/VIP ne sont pas écrasées. Aucun autre état des anciens groupes n'est migré automatiquement.
