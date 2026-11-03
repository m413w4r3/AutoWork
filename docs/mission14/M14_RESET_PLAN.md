# M14 — plan de remise à zéro / base neuve (PRÊT, NON EXÉCUTÉ)

Rien de ce document n'a été exécuté, à l'exception de l'**archivage et de sa vérification** (§2). `make clean` n'a pas été lancé. Toute étape marquée « après autorisation » exige une décision explicite de l'utilisateur dans une mission distincte.

## 1. Faits vérifiés dans le code (2026-11-03)

- `make clean` → `scripts/ops/safe_local_reset.py` : exige le projet Compose `cti-bulletin`, les trois volumes `cti-bulletin_{postgres,redis,minio}_data` (labels Compose vérifiés), des bind mounts sous `var/` (`var/diagnostics/runs`, `var/workspaces/{editions,subjects}`, `var/diagnostics/events.jsonl`).
- Il **refuse** si des données existent (`production_runs`, `analyst_investigations`, `subjects`, `editions`) sauf `CONFIRM_INITIAL_RESET=yes`, et **refuse sans contournement** dès qu'une enveloppe G2 (`investigation_envelopes`) ou une invocation (`tool_invocations`) existe (« premier run supervisé démarré »). La base actuelle contient 22 enquêtes et 3 605 invocations : le reset est donc **voulu refusé**. Ne pas supprimer de lignes à la main, ne pas modifier le garde.
- Le volume `goodware_cache` n'est pas dans la liste de reset.
- `compose.yaml` a `name: cti-bulletin` ; ports hôte fixes : PostgreSQL de test 55432, MinIO 9000/9001, backend 8000, frontend 5173 ; `./var/...` est relatif au répertoire du compose.
- `compose.models.yaml` rattache backend/worker/job-recovery au réseau externe `metaharness-models` (Bridge/Web AI). Une seconde stack sans ce réseau n'a pas de Bridge.

## 2. Archive historique (FAITE, vérifiée)

Emplacement : `/home/nill/perso/work/AutoWork-backups/m14-pre-reset-20261103/` (dossier 0700, hors dépôt, ~1 Go).

| Élément | Fichier | Vérification effectuée |
| --- | --- | --- |
| PostgreSQL (22 enquêtes, 350 model_runs, 3 605 invocations) | `postgres.dump` (format custom, PostgreSQL 16) + `.sha256` + `postgres.toc` | **restauré dans une base jetable** du conteneur de test puis comparé : 105 tables, comptages de lignes identiques ; base jetable supprimée |
| Objets MinIO (volume) | `vol-minio_data.tgz` | 6 868 `xl.meta` dans l'archive = 6 858 objets + 10 système ; 6 858 lignes `blobs` en base. **Redémarrage d'un MinIO jetable sur l'archive : non fait** |
| Bridge, workspaces nommés | `vol-bridge_data.tgz`, `vol-{subject,edition}_workspaces.tgz` (vides pour les deux derniers) | SHA-256 |
| Workspaces et diagnostics `var/` | `var-workspaces-diagnostics.tgz` | SHA-256 ; `var/pdf-inspections/derived/*.cache` illisibles (propriété root, caches reconstructibles) |
| Rapports/traces de runs | `autowork-runs.tgz` (sans `goodware-index`, 2 Go reconstruits par `make goodware-build`) | SHA-256 ; un fichier modifié pendant la lecture (`m07c`, écriture concurrente) |

`volumes.sha256` et `postgres.dump.sha256` contrôlables par `sha256sum -c`. À refaire (script identique) juste avant tout reset, car l'état aura changé.

Limites : l'archive contient des preuves CTI et des échantillons : elle reste locale, hors dépôt, sans secrets applicatifs intentionnels (le dump contient les données de la base ; `.env` et `.secrets` ne sont PAS archivés — à sauvegarder séparément par l'utilisateur si souhaité). Les ~24 Go libres sur `/home/nill` (99 % occupé) sont la contrainte de place.

## 3. Option recommandée — A : stack neuve séparée (moindre risque de perte de preuves)

Principe : ne rien supprimer ; la stack historique `cti-bulletin` reste intacte (arrêtée ou en lecture), une nouvelle stack démarre sur de nouveaux volumes.

1. Fermeture : aucune invocation `needs_review`/`transport_unknown`/en vol côté historique (`SELECT status, count(*) FROM tool_invocations GROUP BY 1`, `model_runs` en cours) ; aucun opérateur ni relais actif.
2. Nouveau checkout (`git worktree add ../AutoWork-prod <sha M14 validé>`) → son propre `var/` ; nouveau `.env` dérivé de `.env.example` créé par l'utilisateur (secrets non copiés par un agent).
3. Nom de projet distinct et ports distincts : `docker compose -p cti-prod ...` avec `BACKEND_PORT`, `FRONTEND_PORT`, `MINIO_API_PORT`, `MINIO_CONSOLE_PORT`, `TEST_POSTGRES_PORT` différents. **À vérifier avant usage** : que `-p` prime bien sur `name: cti-bulletin` (`docker compose -p cti-prod -f compose.yaml config --no-interpolate | head -1`, sans afficher l'environnement) et que `container_name` n'est pas fixé.
4. Réseau modèles : rattacher la nouvelle stack à `metaharness-models` (compose.models.yaml) **uniquement** quand la décision d'activer Bridge est prise ; le relais Bridge est single-threaded, une seule stack à la fois doit l'utiliser.
5. Démarrage : `make up` équivalent avec le nouveau projet ; `alembic upgrade head` s'applique sur base vide (fresh install, ADR-0007) ; vérifier `/api/health/ready`.
6. Contrôles base neuve : `SELECT count(*)` = 0 sur `analyst_investigations`, `tool_invocations`, `model_runs`, `samples` ; `alembic_version` = head ; aucun objet dans le bucket MinIO neuf.
7. Capacités : `make goodware-build && make goodware-load && make goodware-status` — **seulement avec binaires bénins réels** pour la mesure de faux positifs (les index yarGen ne sont pas des fichiers bénins) ; vérifier VT (lecture d'un hash public déjà autorisé), Bridge (`/health`), YARA (compilation locale), Suricata (`network.suricata_validate_rules`) ; `python scripts/ops/stage_operator_cti_skills.py` dans un workspace opérateur neuf.
8. Nouvelle enquête : nouveau sujet/édition, enveloppe G2 approuvée par l'humain, opérateur DeepSeek Flash 1M `thinking=max` (profil « Orchestrator DeepSeek »), mandat métier naturel.
9. Livrable : avis brouillon → export d'annexes (lot F) → aperçu PDF → export d'audit. Approbation/publication = décisions humaines séparées.

Retour arrière : la stack historique n'a pas été modifiée ; `docker compose -p cti-prod down` (sans `-v`) puis rouvrir l'ancienne stack. Restauration complète de l'historique possible depuis l'archive §2 (`pg_restore` dans un PostgreSQL 16 neuf + extraction du tar MinIO dans un volume neuf).

## 4. Option B — reset de l'environnement existant (non recommandée)

Seulement si l'utilisateur décide explicitement d'abandonner l'historique en place. `safe_local_reset.py` refusera tant que des enveloppes G2/invocations existent ; il n'existe **pas** de procédure approuvée pour lever ce garde. Il faudrait une décision écrite (nouvel ADR) définissant une procédure de clôture/archivage reconnue par le script ; ne pas improviser de `DELETE`/`TRUNCATE`, ne pas retirer les volumes à la main. Préalables minimaux si cette option est un jour retenue : re-sauvegarde complète vérifiée (§2) hors du disque principal, restauration test d'un MinIO jetable, arrêt des workers, liste explicite des volumes (`cti-bulletin_{postgres,redis,minio}_data`, `goodware_cache`, `bridge_data`) et des répertoires `var/`.

## 5. Données

- **Conservées** : tout l'historique (archive §2 + stack historique intacte), documents et branches Git, `.autowork-runs/` (rapports), `AutoWork-backups/`.
- **Supprimables après décision** (jamais avant que l'archive soit restaurée à blanc sur la machine cible) : volumes de la stack historique, `var/workspaces/*`, `var/diagnostics/*`.
- **Dépendances externes** : VT (clé de l'utilisateur), Bridge (conteneurs `bridges-*`, réseau `metaharness-models`, extension navigateur à reconnecter), DeepSeek (profil Paseo), binaires bénins (non fournis).

## 6. Décisions qui reviennent à l'utilisateur

1. Option A ou B (A recommandée). 2. Autorisation de démarrer une seconde stack et des ports retenus. 3. Fourniture d'un jeu de binaires bénins pour le goodware. 4. Autorisation de la recherche CTI live (VT, Bridge) pour la qualification et l'enquête de production, avec l'article et les hashes initiaux. 5. Sauvegarde séparée de `.env`/secrets. 6. Mise en production et publication.
