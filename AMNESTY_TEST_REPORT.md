# Rapport de vérification — campagnes de déban

## Test fonctionnel automatisé (sans serveur Telegram)

Script : `python tests/test_amnesty.py`

Environnement : SQLite temporaire + session SQLAlchemy simulant l'interface asynchrone + faux Bot Telegram.

Vérifié :

- Simulation avec 4 candidats admissibles, exclusions des motifs sensibles, des admins et des historiques anciens, et un ban sans sanction enregistrée.
- Exclus `/pedo`, hash-ban, ban manuel, mot interdit, raisons inconnues ; vérifie aussi historique trusted `/pedo` et registre média global.
- Aucune sanction modifiée ni aucun appel Telegram avant confirmation.
- Confirmation uniquement par `ADMIN_IDS` et non rejouable.
- Les sanctions libérées passent en `active=False`, `users_test.is_banned=False` ; les exclusions restent bannies.
- Déban dans les groupes actifs et OFF accessibles (`only_if_banned=True`).
- Groupe `LOST` : tâche conservée, reprise après retour du groupe.
- Nouveau ban entre confirmation et traitement : le déban est ignoré.
- Journal par groupe, succès, erreurs, opérations persistantes.
- Annulation d'une nouvelle simulation sans modification de sanction.

## Régression du projet

`python tests/verify_project.py` : 108 contrôles historiques réussis (séries 79 + 88 + 108 avec sous-ensembles chevauchants).

**Non testé :** vrai PostgreSQL Railway (concurrence/verrous et pool asynchrone), vrai Bot API Telegram, droits réels des groupes et charge sur plusieurs instances. Une simulation sur une copie de la base et un essai restreint sont recommandés avant une campagne massive.
