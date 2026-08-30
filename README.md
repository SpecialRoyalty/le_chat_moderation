# GROSCHAT Central — réseau multi-groupes

Cette version transforme l'ancien bot mono-groupe en **un seul bot central** capable de piloter un nombre dynamique de groupes Telegram depuis une base PostgreSQL commune.

## Principes

- Les groupes ne sont pas codés dans les variables d'environnement.
- Quand le bot est ajouté à un groupe, celui-ci devient `PENDING`.
- Seuls les utilisateurs présents dans `ADMIN_IDS` peuvent accepter/refuser ce groupe depuis le privé.
- Un groupe approuvé peut être `ON` ou `OFF` indépendamment.
- Un seul groupe peut être **ACTIF/ouvert** à la fois.
- Un groupe **SÉLECTIONNÉ** reçoit le prochain vote / la prochaine ouverture.
- Les groupes fermés affichent automatiquement le groupe actif (ou sélectionné) avec un lien de redirection.
- Les groupes privés sans username public utilisent un deep-link du bot ; le bot revalide toujours la cible au clic.
- Le failover sélectionne un groupe de remplacement sain mais **ne l'ouvre pas automatiquement**.

## Portée des données

### Globales au réseau

- `/pedo` et bans graves.
- Restrictions/mutes provenant du moteur de modération.
- `media_hashes_test` (file_unique_id + SHA256).
- `media_fingerprints_test` (empreintes perceptuelles).
- Name Ban / Mot Ban / Mot Interdit globaux.
- Utilisateurs connus et historique central.
- Admins / Trusted.
- Publicités configurées (le ON/OFF de diffusion reste local).

### Propres à chaque groupe

- ON/OFF du groupe.
- État ouvert/fermé.
- Votes et sessions.
- Invitations et compteurs par lien/groupe.
- Arrivée récente et règle média < 60 secondes.
- Anti-repost ON/OFF.
- Publicités ON/OFF.
- Horaires/objectifs de vote.
- Message de statut/maintenance.
- Règles locales ajoutées aux règles globales.

## Perte d'un groupe

Le bot écoute `my_chat_member` et effectue aussi un contrôle de santé périodique. Un timeout Telegram isolé **ne déclare jamais un groupe perdu**.

Un groupe peut être déclaré `OFFLINE` automatiquement sur signal fort (bot expulsé, plus admin, accès durablement interdit) ou `LOST` manuellement par un ADMIN_ID.

Lors d'une perte :

1. la session et le vote du groupe sont annulés ;
2. toutes ses invitations actives sont invalidées immédiatement en PostgreSQL ;
3. le bot tente aussi de révoquer physiquement les liens chez Telegram ;
4. le groupe sort de la rotation ;
5. un autre groupe sain peut devenir la prochaine cible ;
6. les statuts des autres groupes sont mis à jour ;
7. les sanctions et hash-bans restent intacts car ils sont globaux.

Les membres déjà connus du réseau bénéficient d'une courte exemption de migration afin qu'une bascule vers un groupe de secours ne déclenche pas à tort la règle « média dans les 60 secondes ».

## Invitations

Les nouvelles invitations sont stockées dans `group_invite_links_test` avec leur `group_chat_id`. Une invitation d'un groupe ne valide rien dans un autre groupe.

La validation différée (5 minutes) est persistée dans `pending_invite_validations_test`, donc un redémarrage Railway ne fait plus perdre une validation en attente. Un membre parti avant les 5 minutes n'est pas crédité.

## Migration depuis l'ancienne base

**Ne supprime pas l'ancienne base.** Utilise exactement la même `DATABASE_URL` si tu veux conserver les hash-bans, empreintes perceptuelles, utilisateurs, règles et historique existants.

`Base.metadata.create_all()` ne supprime pas les tables existantes : il crée uniquement les nouvelles tables réseau manquantes.

Les tables historiques `media_hashes_test` et `media_fingerprints_test` gardent exactement le même schéma que dans la version `GROSCHAT-main-fast-optimized`.

Pour le premier déploiement seulement, tu peux laisser `MAIN_GROUP_ID` avec l'ID du groupe principal historique. Cela permet d'importer son état et ses anciens liens vers le registre multi-groupes. Un marqueur de migration empêche ensuite cette variable de ressusciter un groupe déclaré perdu.

Voir `MIGRATION.md` pour la procédure complète.

## Déploiement recommandé

Cette édition est conçue pour **un seul BOT_TOKEN administrateur de tous les groupes**. Si tu utilises actuellement trois bots différents, choisis le bot central, ajoute-le comme administrateur à tous les groupes, valide les groupes depuis le privé, puis arrête les anciens déploiements pour éviter qu'ils modifient les permissions en parallèle.

Droits administrateur minimum vérifiés avant activation d'un groupe :

- supprimer les messages ;
- restreindre/bannir les membres ;
- inviter des utilisateurs.

L'activation (`ON`) confirme aussi que le bot peut réellement maintenir le groupe fermé avant de modifier la base.

## Vérifications incluses

Le dossier `tests/verify_project.py` effectue des contrôles statiques/non destructifs : compilation Python, compatibilité des anciennes tables, conservation des tables de hash-ban, absence de migration destructive, logique d'isolation des mots, deep-links groupe et garde-fous réseau.

Ces tests ne remplacent pas un essai réel Telegram/Railway : les appels Bot API et PostgreSQL live nécessitent les identifiants et le réseau de production.

## Ban manuel global + redirection réseau

- Un membre banni avec `/pedo` est enregistré comme ban global puis banni dans tous les groupes approuvés, même s'il n'y est pas encore membre.
- Un ban effectué manuellement par un administrateur depuis l'interface Telegram (`chat_member -> kicked`) est également converti en ban global. Les updates produites par le bot lui-même sont ignorées pour éviter les boucles.
- Si un groupe était hors ligne au moment du ban, la sanction reste en PostgreSQL et est réappliquée à son retour ou à l'arrivée de l'utilisateur.
- Lorsqu'un nouveau groupe est approuvé, toutes les sanctions globales actives sont réconciliées avant son utilisation.
- Les anciens messages `MAINTENANCE` sont remplacés par des messages `CE GROUPE EST EN PAUSE` / `PROCHAINE SESSION`.
- Le bouton de redirection utilise automatiquement le lien public du groupe, son lien principal Telegram lorsqu'il est disponible, ou un lien direct réseau créé par le bot pour un groupe privé. Aucun réglage manuel n'est nécessaire dans le cas normal.
- Le lien direct réseau privé est invalidé lorsque le groupe devient indisponible ou est désactivé.
