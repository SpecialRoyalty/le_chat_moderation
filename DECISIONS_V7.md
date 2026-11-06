# Décisions fonctionnelles après comparaison de l'ancien bot

| Module | Choix | État V7 |
|---|---|---|
| Ancienne justice des inactifs | **Retirée** : conserver la méthode d'inactivité du nouveau bot | Aucun ancien module porté |
| Multi-groupes A/B | **Oui, plusieurs groupes à la fois**, sans alternance automatique | Routage multi-groupes, schémas PostgreSQL séparés, panneau sélecteur, onboarding individuel. **Tests Telegram réels nécessaires** |
| Pass gratuits historiques | **Exclus** | Aucun ajout |
| Cagnotte participative | **Exclue** | Aucun ajout |
| Concours invitation | **Oui**, classement actuel + un tirage | Tirage uniforme avec seuil et journal en base, sans gain automatique |
| VIP soirée / 24 h | **Oui** | Durée 1 jour, notification avant fin, révocation vérifiée par job toutes les 60 s |
| Surveillance technique | **Oui** | Page 🩺 groupe + scanning bloqué + livraisons VIP + expirations |
| Filtres mots complets historiques | **Non**, garder le filtre de la nouvelle version | Pas de second système |
| VIP groupe 24/24 | **Oui** | Offre avec permission 24/24, réapplication horaire, groupe distinct |
| VIP soit prix, soit parrainages | **Oui** | Objectif configurable, crédits validés, bouton de déblocage, claim unique, abonnement et join request personnel |
| VIP liens sécurisés | **Oui** | Vérification de l'ID Telegram du demandeur à l'adhésion |
| Anti-hash médias ancien + nouveau | **Conserver** | Reprise V6 |
| Sanctions transversales entre groupes | **Pas demandé explicitement pour cette étape** | NON implémenté (voir README) |
| Anciens broadcasts privés/VIP | **Pas demandé explicitement pour cette étape** | NON implémenté |

**Attention** : cet inventaire distingue les changements réellement codés des fonctions non reprises. La V7 n'a pas été testée en conditions réelles et ne doit pas être présentée comme une version de production validée.
