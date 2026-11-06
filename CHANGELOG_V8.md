# Correctifs et vérifications V8

| Sujet | Erreur constatée en V7 | Correction V8 | Vérification locale |
|---|---|---|---|
| Moyens de paiement | Paysafecard / Apple Pay / Crypto | Crypto / PayPal / Revolut | Test de calcul + code du menu |
| Frais | +15 % Apple Pay | Aucun surcoût automatique | Test `payment_total` |
| Instructions Railway | Noms obsolètes | `PAYMENT_CRYPTO_DETAILS`, `PAYMENT_PAYPAL_DETAILS`, `PAYMENT_REVOLUT_DETAILS` | Test de configuration |
| Historique d'achats | Contrainte SQL bloquait PayPal/Revolut | Migration de contrainte, ancien historique conservé | Contrôle statique SQL ; **non testé sur PostgreSQL** |
| Multi-groupes | `GROUP_IDS` imposé + groupe inconnu rejeté | Détection `my_chat_member`, contrôle admin, confirmation | Tests structurels ; **non testé sur Telegram** |
| Données anciennes | 1er groupe lié à `public` | Registre primaire persistant, schémas V7 secondaires récupérés | Contrôle logique/SQL ; **non testé sur PostgreSQL** |
| Modération dynamique | Handler de messages filtré sur les IDs au démarrage | Handler groupes dynamique et vérification de groupe enregistré | Test de câblage |
| Preuves VIP multi-groupes | Recherche seulement dans groupe sélectionné | Recherche de la commande dans les groupes enregistrés | Test structurel |
| Validation par le propriétaire | Mauvais groupe si panneau modifié | Recherche par identifiant de commande sur tous les groupes | Test structurel |
| Menus de paiement vides | Boutons ouvrant une erreur | Seules méthodes avec instruction affichées | Vérification de code |

**Limites connues :** pas de tests d'intégration DB/Telegram ; pas de sélection automatique du rôle VIP/logs ; pas de synchronisation globale des bans ; paiements externes pour accès numériques incompatibles avec les exigences Stars de Telegram.
