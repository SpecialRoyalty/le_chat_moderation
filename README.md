# Telegram Guard Bot V8 — correction des paiements et groupes automatiques

> **Version de développement.** Syntaxe Python et tests locaux vérifiés ; aucun test de bout en bout n'a été réalisé avec Telegram, PostgreSQL ou Railway. Faites un essai sur un groupe isolé **avant** de remplacer un bot en production.

## Corrections V8

- **Crypto, PayPal, Revolut** sont les trois méthodes de commande manuelle proposées. Les frais Apple Pay de 15 % sont supprimés. Les méthodes non configurées dans Railway ne produisent pas de bouton inutilisable.
- Le panneau de paiement affiche un montant en euros et le bot demande une image du justificatif. **Seul un OWNER peut approuver**, après avoir lui-même vérifié la réception effective des fonds ; le screenshot ne valide jamais un achat.
- **Groupes détectés automatiquement** : lorsqu'un administrateur Telegram ajoute le bot dans un supergroupe, un enregistrement est créé en PostgreSQL puis le bot affiche une confirmation. Les droits sont revérifiés au clic. Sans cette confirmation, il n'active aucune modération.
- Chaque supergroupe principal dispose d'un schéma PostgreSQL distinct ; les menus privés permettent de choisir le groupe.
- Le bot peut démarrer **sans `GROUP_ID` ni `GROUP_IDS`**. Il relit le registre à chaque démarrage. Pour une migration V7, il récupère aussi les anciens schémas `guard_<id>` et conserve le groupe associé à `public`.
- Les justificatifs et leurs boutons d'approbation sont désormais retrouvés **dans le groupe de la commande**, même si le propriétaire a sélectionné un autre groupe dans son panneau.
- Les anciens achats en Paysafecard / Apple Pay sont conservés comme **historique** en base, mais aucun nouveau checkout ne les propose.

## Variables d'environnement sur Railway

```dotenv
BOT_TOKEN=123456:VOTRE_TOKEN_BOTFATHER
DATABASE_URL=${{Postgres.DATABASE_URL}}
OWNER_IDS=123456789
TRUSTED_USER_IDS=234567890,345678901
TZ=Europe/Paris

PAYMENT_CRYPTO_DETAILS=Adresse et réseau crypto à afficher
PAYMENT_PAYPAL_DETAILS=Instructions PayPal à afficher
PAYMENT_REVOLUT_DETAILS=Instructions Revolut à afficher
```

- **Obligatoires** : `BOT_TOKEN`, `DATABASE_URL`, `OWNER_IDS`.
- **Facultatives** : `TRUSTED_USER_IDS`, `TZ` et les 3 variables `PAYMENT_*` ; une méthode sans instruction n'est pas affichée au client.
- **Inutiles en utilisation normale** : `GROUP_ID` et `GROUP_IDS`. Pour un ancien déploiement **V6 qui n'avait pas encore de registre de groupes**, fournissez temporairement le `GROUP_ID` original lors de la première mise à jour pour associer correctement le schéma `public` et préserver ses données. Pour une V7, le registre `public.guard_registry` suffit normalement.
- Retirer de Railway `PAYMENT_PAYSAFECARD_DETAILS` et `PAYMENT_APPLE_PAY_DETAILS` : ils ne servent plus à rien.
- Ne jamais publier le token Telegram, les chaînes de connexion, adresses privées ou captures de paiements.

## Mise à jour V7 → V8 sans mélanger les données

1. **Sauvegarder PostgreSQL** (snapshot/export Railway) et conserver l'archive de la V7 pour revenir en arrière si nécessaire.
2. Remplacer les sources par celles de la V8 ; conserver **la même base PostgreSQL**, pas une base neuve.
3. Ajouter `PAYMENT_PAYPAL_DETAILS` et `PAYMENT_REVOLUT_DETAILS`, conserver `PAYMENT_CRYPTO_DETAILS`. Supprimer les 2 anciennes variables de paiement.
4. Vous pouvez retirer `GROUP_IDS` : le registre des groupes V7 est relu en base et les schémas `guard_<id>` existants sont découverts. Si vous venez directement de V6, garder `GROUP_ID` pour le premier démarrage de migration.
5. Déployer **une seule instance Worker** (`python -m app`) ; un seul polling Telegram par token.
6. Dans un nouveau **supergroupe**, ajouter le bot en tant qu'administrateur ; accorder les droits de suppression, restriction, invitation et épinglage. Le bot doit proposer `✅ Confirmer`, `🔄 Revérifier`, `❌ Annuler` ; si le message n'arrive pas, un admin envoie `/setup` dans le groupe.
7. Dans le chat privé du bot, le propriétaire utilise `/panel` puis **🌍 Choisir le groupe**. Les paramètres, VIP, invitations et statistiques ne sont pas communs entre groupes principaux.
8. Tester sur un groupe de test : **création**, **confirmation**, **ouverture et fermeture**, **modération**, **parrainage**, **commande VIP**, **preuve**, **approbation**, **demande d'adhésion**, **expiration**, puis **redémarrage Railway**.

> **Important** : La détection et la confirmation automatiques concernent actuellement les **groupes principaux gérés par horaires**. Les groupes de destination VIP restent déclarés dans la configuration d'une offre (panneau 💎 VIP) afin d'éviter de les fermer comme un groupe principal. Il n'existe pas encore de sélecteur automatique de rôle `principal / VIP / logs` au moment de l'ajout. Cela reste une limitation fonctionnelle.

## Fonctions du bot conservées

- Panneau **propriétaire à boutons** (`/panel`) : horaires, auto ON/OFF, ouverture manuelle avec sécurité, fermeture, décomptes, modération, filtres, trusted, publicités, règles, VIP, commandes, médias et santé.
- Modération et empreintes médias : identifiant Telegram, SHA-256, hash perceptuel photos, vidéo sur plusieurs images via FFmpeg, anti-repost et contrôles des nouvelles arrivées.
- Trusted users : commandes réservées (`/pedo`, `/mineur`, etc.). Leurs droits sont différents des propriétaires et ne donnent pas accès à l'approbation des paiements.
- Invitations : lien personnel, points attribués après présence, tableau de bord, TOP 10 masqué et tirage au sort.
- VIP : sélection multiple, prix en euros, durée personnalisable (dont **24 h**), accès à vie, groupe 24/24, déblocage par invitations ou commande manuelle, lien d'adhésion avec vérification de l'identifiant Telegram, notification/expiration.

## Paiement : avertissement Telegram

**Telegram exige officiellement Telegram Stars (`XTR`) pour vendre des biens et accès numériques *dans les bots*.** Le parcours externe Crypto / PayPal / Revolut de cette V8 peut contrevenir aux règles Telegram et provoquer une restriction du bot. Le fait de présenter une commande comme « manuelle » ne change pas cette obligation. Cette version fournit le flux demandé mais **n'est pas un système de vente d'accès numériques conforme à Telegram**. Pour mettre ce parcours en production dans Telegram, il faut utiliser Stars. Voir https://core.telegram.org/bots/payments-stars et https://telegram.org/tos/bot-developers.

Une capture d'écran de transfert est falsifiable. L'OWNER doit vérifier la réception dans son compte PayPal/Revolut ou la confirmation on-chain avant d'appuyer sur le bouton de validation.

## Limites et cohérence

- Les bans **ne se synchronisent pas automatiquement entre plusieurs groupes**. Le panneau et les listes d'empreintes bannies sont actuellement propres à chaque groupe principal.
- Les anciens membres inconnus ne peuvent pas tous être listés par l'API Bot Telegram. Le suivi entre/sorties est basé sur les événements observés, les effectifs réels utilisent l'API compteur de Telegram.
- Effacer des messages anciens reste soumis aux limitations de suppression de l'API Telegram ; l'analyse perceptuelle des grosses vidéos est asynchrone et non instantanée.
- La base crée un petit pool de connexions **par groupe** : surveiller le nombre de connexions PostgreSQL si vous ajoutez des dizaines de groupes.
- La détection automatique ne doit pas être confondue avec une fonction d'administration globale de rôles VIP/logs. Un groupe VIP ne doit pas être activé par erreur comme un groupe principal à horaires.
- Les droits admin du bot et le paramètre Privacy Mode (BotFather) doivent être testés sur Telegram réel ; la compilation ne peut pas prouver leur bon fonctionnement.
- Si un ancien bot V6 utilisait déjà le schéma `public` sans la table `guard_registry`, **ne lancez pas V8 sur un nouveau groupe avec cette ancienne base sans indiquer d'abord le `GROUP_ID` historique**.

## Tests locaux

```bash
python run_tests.py
python -m compileall -q .
```

Les tests de logique et de câblage sont exécutables sans identifiants. **Les dépendances `python-telegram-bot` et `asyncpg` ne sont pas présentes dans l'environnement de validation utilisé ici** ; un test réel de l'API Telegram et des migrations PostgreSQL reste à faire.
