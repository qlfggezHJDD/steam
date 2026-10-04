# SideQuest · Dead game scanner

Outil qui repère les jeux Steam réellement morts (0 joueur, 0 review, plus aucune activité), les classe par probabilité d'accepter une offre de placement sur leur page, et exporte la liste en Excel.

Stack : FastAPI, SQLite (aiosqlite), aiohttp, openpyxl, un dashboard HTML/JS sans build.

---

## 1. Comment ça marche

```
Liste Discord (jeux détectables avec un SKU Steam)
   │
   ├─ 1. Joueurs actuels (API officielle Steam)     → si > max_players : vivant, fin (+ reviews si activé)
   ├─ 2. Reviews + date de la dernière review       → si > dying_max_reviews : vivant, fin
   ├─ 3. appdetails (type, sortie, dev, éditeur,    → page retirée : "unknown" ; pas un jeu : "ignored"
   │      contact, prix, Early Access)
   ├─ 4. Dernière news Steam (optionnel)
   └─ 5. Classement dead / dying / alive + score    → alerte Discord si nouveau "dead" assez bien noté
```

Les appels bon marché passent en premier : `appdetails` (limité à environ 200 appels par 5 minutes) n'est appelé que pour les candidats.

### Statuts

| Statut | Règle par défaut |
|---|---|
| `dead` | joueurs ≤ 0, reviews ≤ 0, sorti depuis ≥ 180 jours, aucune review ni news depuis ≥ 365 jours |
| `dying` | reviews ≤ 10 et (inactif ≥ 365 jours, ou ≥ 180 jours) |
| `alive` | tout le reste, y compris les jeux pas encore sortis ou trop récents |
| `unknown` | page retirée, ou développeur/éditeur exclu |
| `ignored` | n'est pas un jeu (DLC, outil…) : jamais re-testé |

Un échec réseau ne change **jamais** le statut d'un jeu (il garde son état précédent).

### Score (0 à 100)

Chaque critère s'active ou se désactive, avec ses points. Le total est ramené sur 100 selon les critères activés.

| Critère | Points par défaut |
|---|---|
| Email de contact | 30 |
| Dev = éditeur (même nom) | 20 |
| Inactif longtemps (≥ 2 ans = 100 %, ≥ 1 an = 50 %) | 15 |
| Site ou page support | 10 |
| 0 review | 10 |
| Gratuit ou ≤ 5 $ | 10 |
| Early Access abandonné | 10 |
| 0 joueur sur plusieurs scans (max 5) | 5 |

Les règles personnalisées (champ + comparaison + valeur + points) s'ajoutent dans Settings, onglet Score.

---

## 2. Fichiers

| Fichier | Rôle |
|---|---|
| `server.py` | API FastAPI, WebSocket de progression, sert le dashboard |
| `scanner.py` | Pipeline de scan par étapes, retry/backoff, classification |
| `scoring.py` | Critères du score, règles perso, normalisation (source unique) |
| `database.py` | Schéma SQLite + migrations automatiques, filtres, config |
| `export.py` | Génération de l'Excel (une colonne de points par critère activé) |
| `discord_notify.py` | Alertes webhook Discord |
| `static/index.html` | Dashboard noir et blanc, filtres, panneau Settings |
| `render.yaml` | Déploiement Render |

## 3. Configuration

Tout se règle dans le dashboard (Settings → Score / Scanner, panneau Filters). Les variables d'environnement ne servent que de **valeurs de départ**.

| Variable | Défaut | Rôle |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Webhook des alertes |
| `SCAN_INTERVAL_HOURS` | — | Fréquence du scan automatique |
| `DB_PATH` | — | Chemin de la base SQLite |
| `MAX_REVIEWS` | 0 | Reviews max pour `dead` |
| `DYING_MAX_REVIEWS` | 10 | Reviews max pour `dying` |
| `MIN_AGE_DAYS` | 180 | Âge minimum du jeu |
| `MIN_INACTIVE_DAYS` | 365 | Durée sans review ni news |
| `RECHECK_ALIVE_DAYS` | 7 | Délai avant de re-tester un jeu vivant |
| `NOTIFY_MIN_SCORE` | 40 | Score minimum pour une alerte Discord |
| `CRON_KEY` | — | Clé secrète du cron externe : `GET /api/cron/scan?key=…` réveille Render et lance un scan, sans mot de passe |
| `ADMIN_PASSWORD` | — | Mot de passe du dashboard (HTTP Basic, n'importe quel identifiant). **À définir sur Render** |

### API

| Route | Rôle |
|---|---|
| `GET /api/games` | Liste filtrée (13 filtres, tri, pagination) |
| `GET /api/export.xlsx` | Excel avec les mêmes filtres |
| `GET` / `PUT /api/criteria` | Critères du score (intégrés + perso), recalcul immédiat |
| `GET` / `PUT /api/scanner` | Réglages du scanner (appliqués au prochain scan) |
| `GET /api/stats` | Compteurs |
| `POST /api/scan/start` · `/api/scan/stop` | Lancer / arrêter un scan |
| `PATCH /api/games/{id}/status` | Forcer un statut |
| `WS /ws` | Progression en direct |

---

## 4. État réel : ce qui est vérifié et ce qui ne l'est pas

Tout a été écrit sans accès réseau.

| | État |
|---|---|
| Syntaxe Python | Vérifiée (`py_compile`) |
| Appels à Discord et Steam | **Jamais exécutés** |
| Génération de l'Excel | **Jamais exécutée** |
| Dashboard dans un navigateur | **Jamais ouvert** |
| Migration sur ta vraie base | **Jamais testée** |

**Première chose à faire :** lancer un scan sur 50 jeux, ouvrir l'Excel, vérifier que les colonnes sont remplies, et regarder les logs Render pour les lignes `[!]`.

---

## 5. Analyse : problèmes connus

### Priorité 0 (à régler avant d'utiliser sérieusement)

- [x] **Aucune authentification.** N'importe qui avec l'URL peut lancer des scans, changer les réglages, forcer des statuts et télécharger tes leads (emails inclus). Ajouter un mot de passe (HTTP Basic ou token via variable d'environnement) sur l'API, l'export et le WebSocket.
- [x] **Risque de flood Discord au premier scan.** Une alerte par jeu : sur une base vide (ou remise à zéro par Render gratuit), tout est « nouveau » et le webhook peut renvoyer des 429 (messages perdus). Regrouper en un message par lot de 10 embeds, limiter le débit, et un mode « premier scan silencieux ».
- [x] **Les échecs sont comptés comme « checked ».** La barre peut atteindre 100 % sans avoir tout vérifié. Ajouter un compteur `failed / skipped` dans l'UI, le résumé et l'alerte de fin de scan.
- [x] **`gitignore` et `_env.example` sans le point** dans le zip reçu. Si c'est le cas dans le repo, rien n'est ignoré : `games.db` (et surtout un futur `.env` avec le webhook) peut être commité. Renommer en `.gitignore` et `.env.example`.

### Priorité 1 (fiabilité des résultats)

- [x] **Un seul instant à 0 joueur ne prouve rien.** `zero_streak` existe mais n'est pas exigé. Ajouter un réglage `min_zero_streak`. Attention : sur Render gratuit la base repart de zéro, donc le compteur aussi.
- [x] **Dates de sortie non reconnues** (ex. « Q4 2018 », « To be announced »). Avec « âge minimum » activé, ces jeux ne sont jamais classés `dead` (faux négatifs).
- [x] **Le statut forcé à la main est écrasé** au scan suivant. Ajouter un verrou (`pinned`) pour garder un statut manuel.
- [x] **Les jeux `unknown` sont re-testés à chaque scan** (donc un `appdetails` à chaque fois). Mémoriser une date de re-test (ex. 30 jours).
- [ ] **Un jeu qui répond toujours 403 bloque la file `appdetails`** jusqu'à une trentaine de secondes (retries sur un sémaphore unique). Ajouter un disjoncteur et un backoff global.
- [ ] **Le scan n'est pas reprenable.** Un redémarrage au milieu de 17 000 jeux repart de zéro. File de tâches avec point de reprise, et priorité aux jeux jamais testés.
- [x] **« Auto-édité » défini de deux façons** : intersection de noms dans le score, égalité exacte dans le filtre SQL. Stocker une colonne `self_pub` calculée une fois.
- [x] **Détection email/site par `"@"`** dans l'UI : une URL contenant `@` s'affiche comme un mail. L'UI doit utiliser les colonnes `email` et `website`.
- [ ] **Poids du score non calibrés** : ce sont des estimations, pas des mesures. Voir le suivi des offres ci-dessous.
- [ ] **User-Agent « Mozilla/5.0 »** : à remplacer par un User-Agent clair. `appdetails` n'est pas une API documentée, elle peut changer.

### Priorité 2 (confort)

- [ ] Tri par colonne (le tri est fixé sur le score), lignes navigables au clavier, vue mobile en cartes.
- [ ] Filtres sauvegardables (presets) et conservés dans l'URL.
- [ ] Interface entièrement en une seule langue (anglais dans l'UI, libellés des critères en français).
- [ ] Aucun test automatisé, aucun `/api/health` pour Render.

---

## 6. Ce que je rajouterais

### A. Suivi des offres (le plus gros gain)

Aujourd'hui l'outil s'arrête à la liste. Ajouter un mini-CRM :

- Statut par lead : `new → contacted → replied → accepted / declined / no answer`, notes, date de relance.
- **Modèles de message** avec variables (nom du jeu, nom du dev, prix proposé) et boutons « copier » / « ouvrir dans le mail » préremplis, en FR et EN.
- **Statistiques de conversion** par critère du score. Au bout de quelques dizaines de réponses, les poids se calibrent sur des résultats réels au lieu de mes estimations.

### B. Regrouper par développeur

Un dev avec plusieurs jeux morts = un seul mail pour plusieurs placements. Ajouter une vue « par développeur », une colonne « autres jeux morts du même dev » et un bonus de score.

### C. Meilleur contact

- Détecter les liens sociaux et Discord/Twitter/itch.io du dev quand ils sont accessibles via la page.
- Marquer clairement les contacts « utilisables » (un email) par rapport à un simple site vitrine.

### D. Historique

- Table de relevés de joueurs par date, avec purge automatique (taille de la base).
- Courbes par jeu et vraie métrique « mort depuis N jours ».
- Résumé quotidien Discord (top N par score) à la place d'une alerte par jeu.

### E. Données et hébergement

- Détail du score par critère dans la ligne dépliée (le calcul existe déjà, il manque l'affichage).
- Élargir la source : aujourd'hui seuls les jeux de la liste Discord avec SKU Steam sont scannés, ce qui exclut la plupart des jeux Steam morts. Une option « liste complète Steam » est possible mais le scan devient très long.
- Base persistante : disque Render payant, ou base externe, ou sauvegardes régulières. À vérifier selon les conditions actuelles du plan gratuit de Render.
- Scan planifié par un cron externe qui appelle l'API (nécessite l'authentification de la priorité 0).

---

## 7. Questions ouvertes

1. Veux-tu garder la liste Discord comme seule source, ou ouvrir à tout Steam ?
2. Le contact se fait par mail uniquement, ou aussi par Discord/réseaux ?
3. Budget hébergement : toujours 0 € ? Cela décide de la persistance de la base.
4. Quel prix proposes-tu en général ? Il pourrait entrer dans les modèles de message.

## 8. Notes

- **Prospection** : l'envoi de mails commerciaux à des professionnels en France/UE est encadré (identité de l'expéditeur, objet clair, possibilité de refuser). À garder en tête avant d'automatiser les envois.
- **Steam** : les endpoints utilisés sont limités en débit, et `appdetails` n'est pas officiel. Le scanner ralentit volontairement (retries, une seule file de détails).


## 9. Ajouté depuis la première version

Mot de passe (`ADMIN_PASSWORD`), `/api/health`, alertes Discord groupées (max par scan réglable), compteur d'échecs, `min_zero_streak`, verrou des statuts manuels, re-test des `unknown` tous les 30 jours, suivi des offres (statut, note, relance, modèles FR/EN, entonnoir), détail du score par critère, nombre de jeux morts du même dev, `smoke.py` (`python smoke.py 50` pour tester pour de vrai en local).

Pas encore fait : historique des joueurs, presets de filtres, tri par colonne, reprise de scan, disjoncteur 403, source élargie, vue mobile en cartes, tests automatisés.

## 10. Cron externe (cron-job.org)

- **Ne pas** pointer le cron sur `/` : avec `ADMIN_PASSWORD`, la page répond 401 et cron-job.org désactive le job après trop d'échecs.
- Pour garder Render éveillé : `https://TON-SERVICE.onrender.com/api/health` (pas de mot de passe).
- Pour lancer un scan automatique : définir `CRON_KEY` sur Render, puis `https://TON-SERVICE.onrender.com/api/cron/scan?key=TA_CLE`.
- Un scan s'arrête désormais avec un message clair si Discord ou Steam ne répondent pas (après 40 échecs sans aucun succès), au lieu de tourner dans le vide.
