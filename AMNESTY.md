# Campagne de déban réseau — mode d'emploi

## Sécurité avant tout

**Ne supprimez jamais PostgreSQL** : conservez `DATABASE_URL` et faites un snapshot/sauvegarde avant le premier déploiement. `create_all()` ajoute quatre tables et ne purge aucun hash.

La campagne n'est **pas** un déban automatique de tous les bannis Telegram (Telegram Bot API ne fournit aucune liste complète de bannis). Elle part des `global_sanctions_test` actifs et des ID utilisateurs que le bot connaît.

### Utilisateurs admissibles

Le bouton **🕊️ Campagne de déban** (en privé, `ADMIN_IDS` uniquement) prépare une simulation sans modifier la base ni envoyer d'appel `unbanChatMember`. Seules ces raisons de ban automatique sont admissibles :

- `vote_without_participation` — vote sans média ;
- `story` — story ;
- `media_under_60s` — média envoyé moins de 60 s après l'arrivée.

Les autres motifs restent exclus, notamment `/pedo`, `hashban`, `manual_admin_ban`, `legacy_migration`, `nameban_join`, `ban_word`, `link`, `moderation` et tout motif inconnu. Sont également exclus : `ADMIN_IDS`/`TRUSTED_IDS`, utilisateurs possédant des hash-bans, des empreintes visuelles bannies, des médias bannis dans le registre global, ou un ancien travail hash-ban, même si leur motif affiché paraît révisable.

**Limite des anciens logs :** `global_sanctions_test` remplaçait parfois le motif d'un ban existant. Les anciens motifs écrasés avant l'arrivée de `sanction_history_test` ne peuvent pas être reconstitués de manière certaine. Une campagne de masse peut donc présenter un risque résiduel ; examiner son aperçu avant confirmation. Depuis cette version, `sanction_history_test` conserve les motifs successifs.

### Déroulement

1. Ouvrir `⚙️ /admin` en privé, avec un `ADMIN_ID` de Railway.
2. `🕊️ Campagne de déban` → `🔎 Simuler une campagne`.
3. Examiner : utilisateurs concernés, motifs, exclusions, groupes ciblés, nombre d'opérations, exemples d'ID.
4. Cliquer **✅ CONFIRMER LES DÉBANS** dans les 15 min, ou **Annuler**.
5. La base désactive les sanctions centrales correspondantes et corrige `users_test.is_banned` dans une transaction, puis planifie les débans Telegram (`only_if_banned=True`).
6. Le worker ne traite que 30 opérations par minute au maximum, 4 en parallèle. Les résultats sont journalisés dans `amnesty_attempts_test` et `network_events_test`.
7. `📊 Journal / progression` : succès, erreurs, groupe concerné. Le worker retente les erreurs réseau ; après 6 essais, `🔁 Retenter les échecs` peut être utilisé manuellement.
8. Le bot ne réintègre pas les utilisateurs automatiquement : ils doivent rejoindre via une invitation valide.

### Groupes indisponibles

Tous les groupes du réseau approuvés, y compris OFF, sont prévus dans la campagne. Si un groupe est perdu/hors-ligne, sa tentative reste en attente puis échoue après les reprises automatiques. Lorsque le groupe revient, cliquer **Retenter les échecs** : la tentative sera réexécutée si les permissions de déban existent toujours.

Un ban nouveau apparu après confirmation est prioritaire : le worker saute alors le déban pour éviter de lever cette nouvelle sanction.

### Avant toute campagne de masse

- Désactiver temporairement `Vote = participation` dans les paramètres de **chaque** groupe concerné, ou corriger d'abord la cause des bans injustifiés ; autrement ils risquent de se reproduire.
- Faire une sauvegarde PostgreSQL.
- Vérifier que le bot est administrateur avec le droit de bannir les membres dans les groupes concernés.
- Commencer par la simulation et relire les exclusions. Aucune action n'est exécutée sans confirmation.

### Limitations

- Les comptes bannis directement dans Telegram mais **inconnus de la base du bot** ne peuvent pas être énumérés ou débannis en masse avec le Bot API.
- Les bots supprimés des groupes ne peuvent pas exécuter `unbanChatMember` avant de revenir avec les droits nécessaires.
- La protection contre les races entre sanction nouvelle et déban repose sur une relecture juste avant l'appel Telegram ; la sérialisation absolue des opérations administrateur simultanées nécessiterait un verrou externe commun.
- La taille d'une campagne est plafonnée à 4 000 candidats et 40 000 opérations Telegram pour éviter une charge excessive : une campagne qui dépasse ces limites est refusée explicitement, jamais exécutée partiellement en silence.

## Tables ajoutées (aucune table ancienne modifiée)

- `sanction_history_test` — historique append-only des nouvelles sanctions ;
- `amnesty_campaigns_test` — simulations/confirmations ;
- `amnesty_candidates_test` — candidats figés par campagne ;
- `amnesty_attempts_test` — une opération par groupe + utilisateur ;
- aucun changement structurel des anciennes tables de média ou d'invitations.

Les tables sont ajoutées par `create_all()` et n’affectent pas les hash-bans existants.
