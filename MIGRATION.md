# Migration vers GROSCHAT Central

## 1. Sauvegarder PostgreSQL

Avant le premier déploiement central, créer un backup/snapshot Railway de la base actuelle. Ce n'est pas parce que la migration supprime des données — elle ne le fait pas — mais parce qu'un backup avant évolution d'architecture est une précaution normale.

## 2. Conserver la même DATABASE_URL

Ne pas créer une base vide si les hash-bans historiques doivent être conservés.

Conserver exactement la `DATABASE_URL` de l'ancienne instance principale. Les tables suivantes sont réutilisées telles quelles :

- `media_hashes_test`
- `media_fingerprints_test`
- `users_test`
- `word_rules_test`
- `advertisements_test`
- `tracked_messages_test`
- autres tables historiques `_test`

De nouvelles tables réseau seront créées automatiquement si elles n'existent pas.

## 3. Variables Railway

Minimum :

```env
BOT_TOKEN=...
DATABASE_URL=...
ADMIN_IDS=...
TRUSTED_IDS=...
MAIN_GROUP_ID=-100...   # seulement pour la première migration, facultatif
TIMEZONE=Europe/Paris
```

Il n'y a pas de `GROUP_1_ID`, `GROUP_2_ID`, etc. Les groupes sont découverts dynamiquement.

## 4. Choisir un bot central

La version fournie fonctionne comme **un seul processus / un seul bot central**.

Ajouter ce bot comme administrateur dans chaque groupe que tu veux rattacher. Le bot envoie alors aux `ADMIN_IDS` une demande d'acceptation privée.

Ne pas faire tourner deux instances de polling avec le même `BOT_TOKEN` : Telegram produirait `TelegramConflictError`.

Si les anciens groupes utilisaient des BOT_TOKEN différents, ils ne créent pas de conflit de polling mais peuvent se battre sur les permissions et sanctions. Les arrêter/retirer une fois le bot central validé.

## 5. Valider chaque groupe

Dans le privé admin :

1. `✅ Ajouter au réseau`
2. le bot vérifie qu'il est administrateur ;
3. il vérifie les droits critiques ;
4. il confirme qu'il peut fermer les permissions du groupe ;
5. seulement ensuite le groupe passe `ON/CLOSED` en base.

## 6. Contrôler les hash-bans

Dans le panneau Santé, vérifier :

- `Médias connus`
- `Hash-ban exact`
- `Empreintes perceptuelles bannies`

Ces compteurs doivent être cohérents avec l'ancienne base. Aucun reset n'est effectué par la migration.

## 7. Tester le réseau avant la première vraie session

- Sélectionner Groupe A : le vote doit apparaître uniquement là.
- Groupe B/C : doivent rester fermés et rediriger vers A.
- Ouvrir A : B/C doivent rester fermés.
- Transférer vers B : A doit être fermé avant l'ouverture de B.
- Mettre C OFF : C doit sortir de la rotation et ses invitations actives sont invalidées.
- Tester un `/pedo` avec un compte de test : la sanction doit être enregistrée globalement et appliquée dans les groupes joignables.
- Tester un ancien hash-ban connu : le repost doit être bloqué sans recréer la blacklist.
- Tester une invitation A puis une invitation B : elles doivent être distinctes.

## 8. Tester le scénario « groupe sauté »

Sur un groupe de test :

1. créer une invitation ;
2. déclarer le groupe `LOST` depuis le panel ;
3. vérifier que le lien devient inactif dans la base et que Telegram est révoqué si le groupe reste joignable ;
4. vérifier qu'un groupe sain est seulement **sélectionné**, pas ouvert automatiquement ;
5. vérifier que les autres messages maintenance redirigent vers la nouvelle cible ;
6. vérifier qu'un ancien membre connu peut migrer sans faux ban média <60 s.

## Bases séparées actuelles

Si tes trois anciens bots utilisent **trois PostgreSQL différentes**, choisir une base comme base centrale ne récupère naturellement que les hash-bans de cette base. Il faudra alors fusionner/importer les lignes `media_hashes_test` et `media_fingerprints_test` des deux autres bases avant de les éteindre. Ne pas supprimer ces bases avant cette fusion.
