# Rapport de vérification — GROSCHAT Central

Date de préparation : 2026-08-30

## Résultat

`tests/verify_project.py` : **60 contrôles OK / 0 échec**.

## Contrôles réalisés

- Compilation syntaxique de l'ensemble de `app/`.
- Conservation stricte du schéma historique, dont `media_hashes_test` et `media_fingerprints_test`.
- Simulation additive de `create_all()` avec conservation d'un ancien hash-ban `banned=True`.
- Absence de migration destructive (`DROP TABLE`, `TRUNCATE`, `metadata.drop_all`, suppression des blacklists).
- Approbation sécurisée des groupes et migration legacy limitée au bootstrap.
- Un seul groupe actif et cycle de vote propre au groupe sélectionné.
- Invalidation des invitations et failover en cas de perte d'un groupe.
- Mots isolés (`cp` ne correspond pas à `jecpquoi`, `cp123`, etc.).
- Sanctions globales persistées avant les appels Telegram.
- `/pedo` utilise le ban global.
- Ban manuel Telegram (`kicked`) capturé et propagé à tout le réseau sans boucle avec les bans du bot.
- Les groupes OFF mais encore joignables reçoivent eux aussi les bans globaux.
- Un groupe ajouté plus tard réapplique les sanctions historiques lors de son approbation.
- Une arrivée dans un groupe réapplique immédiatement un ban global encore actif.
- Nouveau wording de statut sans `MAINTENANCE`.
- Lien de navigation automatique vers le groupe actif/sélectionné, y compris pour les groupes privés.
- Lien direct privé invalidé si le groupe devient indisponible.
- Validation des invitations persistante et contrôle de présence après 5 minutes.
- Tous les imports locaux `app.*` existent.
- Absence des anciens modules VIP/Crowdfunding/Justice.

## Limite des tests locaux

Les appels réels au Bot API Telegram et à la PostgreSQL Railway de production ne sont pas reproduits localement. Les chemins réseau disposent néanmoins de timeouts, persistance DB et mécanismes de réconciliation. Un smoke-test Telegram réel reste recommandé avant une vraie session.
