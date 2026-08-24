# Telegram Railway Bot — version test propre

Version nettoyée et optimisée :

- Suppression des modules VIP, Pass soirée, Pass total, VIP JAVANA, Pass gratuit et Crowdfunding.
- Suppression définitive de la justice populaire et des grâces présidentielle/ministérielle.
- Conservation des sessions, votes d’ouverture, modération, invitations, publicités, règles, rapports et santé.
- ON/OFF Publicités pour les diffusions automatiques.
- ON/OFF Repost : si activé, un média déjà vu est supprimé et un avertissement est envoyé.
- Tables SQL suffixées avec `_test`. Le nom de la base PostgreSQL n’est pas réécrit.
- `/pedo` : hash Telegram + SHA256 + empreintes perceptuelles, albums et nettoyage des médias connus.
- Stories interdites : suppression + ban.
- Nouveau membre envoyant un média dans les 60 secondes : suppression + ban.
- Name ban / mot ban / mot interdit : correspondance uniquement sur mot/expression isolé(e).

## Optimisations de cette version

- Cache court des réglages afin d’éviter plusieurs lectures PostgreSQL par message.
- Cache compilé des règles de mots/Name Ban.
- Mise à jour utilisateur limitée à une fois toutes les 30 secondes si son profil n’a pas changé.
- Cache du statut « a déjà envoyé un média » et des arrivées récentes.
- Tracking des messages avec `INSERT ... ON CONFLICT DO NOTHING`.
- Votes avec `INSERT ... ON CONFLICT DO NOTHING`.
- Hash-ban : un seul téléchargement du média pour SHA256 + analyse perceptuelle.
- Un repost SHA256 exact est bloqué avant de lancer FFmpeg.
- Empreintes bannies mises en cache brièvement en mémoire.
- Aucun téléchargement/FFmpeg n’est effectué en gardant une connexion PostgreSQL ouverte.
- `/pedo` et nettoyage de session : suppressions Telegram parallélisées avec concurrence limitée.
- Les messages déjà supprimés sont considérés comme nettoyés pour éviter des tentatives répétées.
- Les notifications admins sont envoyées en parallèle.
- Les timeouts Telegram sur le message de statut restent non bloquants.
