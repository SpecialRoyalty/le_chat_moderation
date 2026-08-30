# Rapport de vérification — GROSCHAT Central

Date de préparation : 2026-08-30

## Résultat

`tests/verify_project.py` : **48 contrôles OK / 0 échec**.

## Contrôles réalisés

- Compilation syntaxique de l'ensemble de `app/`.
- Comparaison du préfixe historique de `app/db/models.py` avec la version `GROSCHAT-main-fast-optimized` : **identique**.
- Vérification explicite de la conservation de `media_hashes_test` et `media_fingerprints_test`.
- Simulation SQLAlchemy `create_all()` sur une base SQLite créée avec le schéma historique : une ligne hash-ban bannie a été conservée et les nouvelles tables réseau ont été ajoutées.
- Recherche d'opérations destructives (`DROP TABLE`, `TRUNCATE`, `metadata.drop_all`, suppression massive des tables hash) : aucune trouvée.
- Vérification que les nouveaux groupes n'héritent jamais des anciens IDs de message/session ; la migration legacy est limitée au bootstrap de `MAIN_GROUP_ID`.
- Vérification que l'approbation/activation d'un groupe confirme les permissions Telegram avant de le déclarer opérationnel.
- Vérification de l'invalidation des invitations sur perte de groupe.
- Vérification du failover et du renouvellement propre du cycle de vote.
- Vérification de l'exclusivité des ouvertures via lock et fermeture confirmée des autres groupes ON.
- Vérification de la consommation des votes après ouverture.
- Vérification que `cp` correspond à un mot isolé mais pas à `jecpquoi`, `cp123`, `123cp`, `moncp`.
- Vérification des deep-links de groupe privé.
- Vérification que les sanctions globales sont écrites en PostgreSQL avant les appels Telegram.
- Vérification que les validations d'invitation sont persistées et qu'un membre parti avant 5 min n'est pas crédité.
- Vérification de tous les imports locaux `app.*`.
- Vérification de l'absence des anciens modules VIP/Crowdfunding/Justice.
- Test du parseur `ADMIN_IDS` / `TRUSTED_IDS` avec valeurs simples, CSV, guillemets et JSON-like.
- Test de conversion `postgresql://` vers `postgresql+asyncpg://`.
- Création complète des 21 tables SQLAlchemy dans une base SQLite de test.
- Vérification séparée : `app/services/hashban.py` est **identique octet pour octet** à la version `GROSCHAT-main-fast-optimized` utilisée comme base.

## Limite des tests locaux

L'environnement de préparation n'a pas accès au Bot API Telegram ni à la base Railway de production et ne contient pas le package `aiogram`. Il n'est donc pas possible d'exécuter ici un test live de polling, de permissions Telegram, d'invitation réelle ou de connexion PostgreSQL Railway.

Ces points ont été protégés par des garde-fous dans le code et doivent être validés par le smoke-test de déploiement décrit dans `MIGRATION.md` avant la première vraie session.
