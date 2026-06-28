# Telegram Railway Bot — version test propre

Version nettoyée :

- Suppression des modules VIP, Pass soirée, Pass total, VIP JAVANA, Pass gratuit et Crowdfunding.
- Conservation de la justice populaire, sessions, modération, invitations, publicités, règles, rapports et santé.
- Ajout d’un ON/OFF Publicités pour bloquer les diffusions automatiques.
- Ajout d’un ON/OFF Repost : si activé, un média déjà vu dans n’importe quelle session est supprimé et un avertissement est envoyé.
- Tables SQL suffixées avec `_test`.
- URL PostgreSQL forcée vers une base dont le nom finit par `_test`.
