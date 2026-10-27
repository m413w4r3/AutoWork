# Mission 07 — handoff / registre (orchestrateur)

Parent : Claude Code `claude-opus-5-5` (non Luna). Enfants Paseo
`codex/gpt-6-luna`, `auto-review`, `xhigh`, une session neuve par cycle.
Les enfants n'atteignent pas PostgreSQL : le parent exécute tous les tests PG
(base jetable `make integration-db-up`, port 55432, venv
`uv sync --locked --python /usr/bin/python3.12`).

## Baseline vérifiée (2026-10-27)

- `main` = `origin/main` = `87093056`, CI #40491882109 success.
- Stack live `cti-bulletin` : images construites 2026-10-22, label
  `ea456083…` (absent du dépôt git) ; Alembic live **`0002_c2_experiments`**
  (code requiert `0005`). `make up` lance le service `migrate`
  (`alembic upgrade head`) : c'est donc une mutation live.
- Live (lecture seule) : inv `5f66ebfb` `running` (màj 2026-10-20),
  aucun job non terminal (jobs : 899 succeeded/10 failed/2 cancelled),
  19 invocations `needs_review`, 225/225 samples `do_not_submit=true`,
  12 production_runs `ready` sur le sujet `d22822d4` (dernier `ec7a6807`).
- `make smoke-live` inclut Shodan passif : **ne pas le lancer tel quel**.

## Lots phase A

| Lot | Branche @SHA | Agent(s) | Statut |
|---|---|---|---|
| A2 VT READ `do_not_submit` | `m07/vt-read-dns@ff7c4d1c` | fix `e2cc6a54`, r2 `969cdbca` ; parent `fc3e3d14`, `ff7c4d1c` ; revues `4062511c` REVISE → `ceb9021b` APPROVED | **intégré** `main@b4d5db8d` ; PG 89 passed, unit 365 |
| A3 E2E continu + D5 | `m07/e2e-continuous` … `ff991fb1` | enfants test `6b0f88fb`/`cbf086e3`/`95090678`/`b54e1495` ; parent fix `14888eac`,`78ff4c1a`,`fe414c74`,`ff991fb1` ; revues `cdf13334` REVISE → `df1777d5` REVISE → `d61d69f6` REVISE → `59e0a621` APPROVED | **intégré** `main@a506f481` (+`502dfae7` typage test) |

## A3 — clos (successeur #2, 2026-10-27)

Origine exacte du refus advisor (instrumentation PG) : `models.py` `_coordinated_create` ← coordinator `Refused(osint_policy_denied, PRE_CONSUME)` ← `SqlAlchemyVirusTotalPolicyGuard.authorize` → `_source_external_disclosure_allowed` vétait l'échantillon acquis (`sample_acquisition.py:689` : `do_not_submit=True`, `external_llm_allowed=True`). **D5 = défaut de production** présent aussi sur main/A2 : sous OSINT_POLICY, aucune consultation Bridge ne pouvait citer un échantillon téléchargé.
Correctif (guard uniquement, familles `bridge.`/`advisor.`) : ignore `do_not_submit` source ; `external_llm_allowed=False`, TLP RED, licence restent bloquants ; observations `sha256` et références `sample_association(_sample)` résolues comme le claim store. Non-Bridge inchangé.
Tests : E2E A3 vert (replay : 0 nouvel appel VT/Bridge, snapshots persistés/audit identiques) ; attentes E2E corrigées (tuples figés, `_vt_external_effect_source_refs`, clé composite de liens) ; PG `test_osint_guard_bridge_ignores_sample_do_not_submit_but_keeps_external_llm_veto` (6 cas, mutation vérifiée).

Gates `main@a506f481` (parent, recette D4) : lint OK ; test-backend 5143 passed ; test-integration 456 passed ; test-workflow-full 32/32 ; mypy 843 OK et J2 navigateur 1 passed sur `502dfae7`. CI non relancée (non poussé).

## Phase B — préparation (lecture seule sur le live)

- Sauvegarde live (pg_dump -Fc, lecture seule) :
  `/home/nill/perso/work/AutoWork-backups/live-pre-m07-20261027T154631Z.dump`
  (5,0 Mo, SHA-256 `413d5cdc94b1528561a00aa3b0935401a4bdad5c9544fd8bc297ec2a5ad72495`,
  98 TABLE DATA).
- Répétition sur copie restaurée (`m07_rehearsal`, postgres-test 55432) :
  `0002→0005` OK, puis `0005→0002→0005` OK ; 225 samples, 1510 invocations
  conservés.

## Défauts consignés

- D1 (A2, corrigé `fc3e3d14`) : le prédicat partagé réappliquait
  `do_not_submit` source au consume Bridge, contraire à la décision
  utilisateur du 2026-10-27.

- D2 (A2, corrigé `ff7c4d1c`, revue `4062511c`) : admission Bridge
  réappliquait `do_not_submit` source (incohérent avec le consume).
- D3 (opérationnel, config) : `advisor.consult` réserve jusqu'à
  `MAX_ADVISOR_TOOL_SECONDS=3600` (`investigation_advisor_tools.py:101`) ;
  défaut OSINT_OPEN `tool_seconds=3600` ⇒ aucune consultation Bridge après
  une analyse. **Activer la politique live avec `tool_seconds` ≥ 86 400**
  (max `osint_open_max_tool_seconds`).
- D4 (env, non code) : 5 tests Typst réels échouent sans la recette du
  binaire épinglé (`TYPST_BINARY`, `FONT_BUNDLE_ROOT`, `TMPDIR`) ; verts avec.

- D6 (**observation documentée, non bloquante — décision utilisateur 2026-10-27 : pas de veto `external_llm_allowed` supplémentaire au point d'envoi Bridge, aucune campagne de correction**) : le claim store (`repositories/external_effects.py` ~1622-1670, commit `15b36abe`) ne contrôle pas `external_llm_allowed` des samples et l'ignore pour les documents Bridge, y compris sous HUMAN_G2. Sous OSINT_POLICY le guard le contrôle désormais. Non bloquant pour le run.
- R1 (risque run) : un dossier Bridge citant une observation `sha256` de hash **non associé** à l'investigation (pivot VT non téléchargé) est refusé au consume (`evidence_observation_not_associated_with_investigation`, préexistant). Citer l'observation VT du rapport/pivot (type fichier) ou télécharger d'abord.
- R2 : 3 tests `test_confined_analysis_dispatch` (capa/MWCP) échouent localement hors `make` (identiques sur main) ; verts via `make test-backend`.

## Prochaines actions

- **B1 FAIT** : `main@22d03e22` poussé (diff vérifié : aucun secret, chemins backend/src, backend/tests, docs/mission07), CI #40556064348 verte (backend, frontend, compose, typst, W10+J2).
- Le label `org.opencontainers.image.revision=ea456083…` est celui de l'**image de base uv**, pas d'AutoWork. Contrôle du SHA déployé = manifeste SHA-256 des `.py` de `/app/src/cti_app` dans le conteneur == `git ls-files backend/src/cti_app` à `22d03e22` (aujourd'hui : live ≠, 55 fichiers diffèrent, `api/osint_policy.py` absent).
- `make smoke-live` (plan `89b7b4c2…`) inclut Shodan ⇒ exclu. `preflight-supervised-first-run-stack` exige d'attester la base jetable ⇒ **interdit sur le live**.
- Jeton opérateur : jamais en conversation ; appels API opérateur exécutés **dans le conteneur backend** (`docker exec … python`) lisant `OPERATOR_API_TOKEN` de l'environnement du processus, sans l'imprimer.

## PHASE B — MANDAT GLOBAL B2–B6 ACCORDÉ (utilisateur, 2026-10-27)

Exécuter B2→B6 d'une traite, sans redemander entre étapes ; seuils d'alerte = observation, pas demande d'autorisation. Interdits sans nouveau mandat : restaurer un dump, redémarrer une image incompatible avec le schéma migré, rejouer à l'aveugle un effet externe ambigu, Shodan, veto D6 / frictions / approbations par appel supplémentaires. TLP, licence, révocation, divulgation restent applicables. **Jeton opérateur : jamais en conversation** — appels API exécutés dans le conteneur backend (`docker exec cti-bulletin-backend-1 python …`) lisant `OPERATOR_API_TOKEN` de l'environnement du processus, sans l'imprimer (OPERATOR_API_TOKEN et OPERATOR_CLIENT_API_TOKEN sont définis, distincts ; l'API est en mode `authenticated` ; `_require_verified_human_operator` exige le scope HUMAN_OPERATOR, pas OPERATOR_CLIENT).

Budgets OSINT_OPEN validés : tool_seconds 86400 ; vt_units 10000 ; downloads 1000 ; bridge_turns 200 ; context_bytes 64 Mio (67108864) ; context_tokens 16 M (16000000) ; effets externes/enquête 500 ; fournisseurs VT + Bridge ; Shodan exclu (shodan_credits = 0/absent).

**Rôles (corrigé par l'utilisateur)** : orchestrateur = `claude/claude-sonnet-5-5`, thinking `medium`. Opérateurs (CTI-ANALYST), agents de correction/implémentation/revue/observation (AUDIT-OBSERVER, BRIDGE-EVALUATOR, FINAL-QA…) = `codex/gpt-6-luna` `xhigh`, mode `auto-review`, **session neuve par tâche**. Les enfants n'atteignent pas PostgreSQL/Docker/live : l'orchestrateur exécute lui-même tout ce qui touche le live. Si contexte > 70 % : mettre à jour ce fichier et lancer un nouvel orchestrateur Sonnet 5.5 medium.

### B2 — FAIT (2026-10-27 ~18:30 UTC)
- Sauvegarde fraîche : `/home/nill/perso/work/AutoWork-backups/live-pre-b2-20261027T182650Z.dump` (5,0 Mo, SHA-256 `040aa1a034247a8c17990361571c89956f37a43d68b44039628d092e96783afa`, 98 TABLE DATA) ; comptages pré-migration : `…/m07-pre-b2-counts.txt` (samples 225, tool_invocations 1510, jobs 911, blobs 3179, analyst_investigations 12, production_runs 12, 0 job non terminal). Dump précédent : `live-pre-m07-20261027T154631Z.dump` (`413d5cdc…`).
- Répétition sur copie restaurée (postgres-test, base `m07_rehearsal_b2`, supprimée) : `0002→0005` OK, comptages conservés.
- Images précédentes étiquetées `cti-bulletin-{backend,worker,analysis-worker,job-recovery,frontend}:pre-m07` (rollback applicatif ; **incompatible avec le schéma 0005 sans nouveau mandat**).
- `make up` depuis `22d03e22` (arbre propre ; seul un commit docs suit) rc=0 : `migrate` exited OK, tous les conteneurs healthy.
- Vérifié : Alembic live = **`0005_sample_acquisition_dispatch`** ; comptages identiques ; empreinte SHA-256 de tous les `.py` de `/app/src/cti_app` = `239f62da6a38b8ea` pour backend, worker, analysis-worker, job-recovery == `git ls-files backend/src/cti_app` à `22d03e22` ; `/api/health/ready` = 200 ; inv `5f66ebfb` toujours `running`.
- **Reste de B2 à contrôler** (non fait) : Bridge `GET /v1/bridge/capabilities` (sans tour de modèle), état Ghidra / analysis-worker prêt (healthcheck compose = OK), frontend servi.

### B2 contrôles restants / B3 / B4 — FAITS (successeur #3, 2026-10-27 ~20:08 UTC)
- B2 : Alembic live `0005` (head), `/api/health/ready` 200, frontend 200, analysis-worker prêt (Dramatiq ready), Bridge `GET /bridge/capabilities` OK sans tour (`extension_connected: true`, transport `chatgpt_web_ui`).
- B3 : inv `5f66ebfb` — avant : 0 external_effect_claim, 0 réservation, 0 job non terminal. Action : `POST /api/investigations/{id}/interrupt` (expected_version 779, Idempotency-Key `m07-b3-interrupt-5f66ebfb`) → `cancelled`, `stop_reason=user_interrupt`, version 781 ; ses 16 `needs_review` intacts. (Chemin applicatif `service.interrupt`→`conclude`, pas d'UPDATE SQL.)
- B4 : OSINT_OPEN activée (`POST /api/osint-open-policy/activate`, Idempotency-Key `m07-b4-activate-v1`) : policy `dc034634-0e36-57cd-ba4e-a61bb2bb736a` v1 active, event `a1c9fe67-69a5-42c2-a760-6fa5fc03859d`, scope subject_set `d22822d4…`, providers bridge+vt, budgets tool_seconds 86400 / vt_units 10000 / downloads 1000 / bridge_turns 200 / context_bytes 67108864 / context_tokens 16000000 / shodan_credits 0, 500 effets/enquête, TLP clear, do_not_submit=false, external_llm_allowed=true, acquire_hits_allowed=true.
- Écart de plan B5 : une enquête par run (`get_for_run`) ⇒ pas d'enquête de smoke distincte sur `ec7a6807` (déjà `cancelled`). Les smokes B5 seront les premiers appels observés de l'enquête B6 (mêmes outils/budgets).
- B6 : nouveau run via `POST /api/production/subjects/{subject}/production/restart-with-new-sources` (seul chemin « nouveau run » exposé) → run `27478dcc-69d6-4593-89fb-7bd45e9c3537` (remplace `ec7a6807`), en cours.
- Helper d'appel API (sans jeton) : `/tmp/m07/opcall.py` (via `docker exec -i cti-bulletin-backend-1 python - METHOD PATH [BODY] [IDEMKEY] < opcall.py`). Les outils opérateur exigent le scope OPERATOR_CLIENT (`_require_operator_client`) ; start/interrupt/policy = jeton humain.

### B6 — FAIT (2026-10-27 ~20:35 UTC)
- Run `27478dcc-69d6-4593-89fb-7bd45e9c3537` (run_number 13) `ready`. Incident : `restart-with-new-sources` repointe l'item de lot vers le nouveau run mais le lot `5247a50b…` est `completed` ⇒ `can_dispatch` False ⇒ job `references` annulé, puis `sources` échoue `InvalidJobTransitionError` (cancel d'un job déjà annulé, `production_jobs.py:271`). Contournement applicatif sans code : `POST /production/runs/{id}/cancel` puis `/resume` (génération 1, 11 appels modèle attendus) ⇒ chaîne complète, 6 artefacts `verified`. Défauts consignés (NON corrigés, non bloquants) : D7 restart→lot terminé ; D8 cancel sur job déjà annulé dans `StageChain.submit`. Aussi : matérialisation workspace `Errno 36 File name too long` (log WARN, non bloquant).
- Synthèse : artefact `4d37c738-d78f-4087-b251-3c38a56e8e53` v1 `verified`, réutilisée (content-addressed) de `b3e0c99e…` ; canonical sha256 `8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618` (même blob `69da99be…`). Quintuplet vérifié (subject d22822d4, run 27478dcc, artefact 4d37c738, v1, sha 8eb9cbe0…).
- Investigation `a8845772-5025-4702-8850-2b6be776855b` `operator_led` `running`, `envelope_approved`, providers bridge+vt, budgets complets (0 consommé), inheritance épinglée policy `dc034634` v1 / event `a1c9fe67`. Outils indisponibles (hors périmètre) : retrohunt, rule workbench, native.inspect, extract_configuration, set_qualification (G2 humaine).

### BLOCAGE B5/C — API live en identité locale, jetons opérateur VIDES (constaté 2026-10-27 ~20:45 UTC)
- Faits : `OPERATOR_API_TOKEN` et `OPERATOR_CLIENT_API_TOKEN` ont une valeur vide dans le conteneur backend (`/proc/1/environ`, `docker inspect`), `APP_ENV=development`. `compose.yaml:97-98` les prend de l'environnement du shell qui lance `make up` ; le `make up` de B2 a donc recréé le backend sans jetons (inférence : le shell n'en avait pas). Le handoff « authentifié, deux jetons distincts » était FAUX pour l'état actuel.
- Conséquences : (1) B3/B4/B6 ont été exécutés sous l'identité locale `dev-analyst` (scope HUMAN_OPERATOR local, API publiée seulement sur 127.0.0.1), pas sous un opérateur vérifié par jeton ; la validité de l'« autorisation OSINT_OPEN par opérateur vérifié » (M07 §8) est à confirmer par l'utilisateur ; (2) `POST /operator/tools/*` et `GET /operator/context` renvoient 403 `operator_client_scope_required` : aucun outil opérateur (VT, Bridge advisor, analyse) ne peut être appelé ⇒ B5 et phase C bloquées.
- Non fait volontairement : générer des jetons éphémères (usurperait l'identité humaine vérifiée / modifierait la config d'auth live et casserait l'UI) ; inspecter `.env`.
- Décision utilisateur requise : relancer le backend avec `OPERATOR_API_TOKEN` et `OPERATOR_CLIENT_API_TOKEN` exportés dans le shell de `make up`/`docker compose up -d backend` (même image, schéma 0005 inchangé), et confirmer/ratifier B3/B4/B6 exécutés en identité locale (ou ordonner de révoquer la politique `dc034634` et de la réactiver sous identité vérifiée).
- Relais prévu pour la suite (sans jeton côté enfants) : l'orchestrateur exécute un relais mécanique fichier→`docker exec` ; CTI-ANALYST (Luna) ne fait que décider.
- Défauts non corrigés : D7, D8 (voir B6).

### À FAIRE : B3 → B6 (voir plan présenté ; résumé)
- **B3** Réconcilier l'inv `5f66ebfb` (`running`, màj 2026-10-20, aucun job) : avant = aucun job/effet externe en cours ou ambigu pour elle (sinon STOP + diagnostic) ; action = passage terminal par action applicative motivée via API/service (CAS), historique + réservations conservés ; les 19 `needs_review` intacts. Trouver l'endpoint/méthode exacts (`conclude`/stop) dans `api/investigation.py` ; vérifier par lecture que ce n'est pas un UPDATE SQL direct.
- **B4** Activer OSINT_OPEN sur le sujet `d22822d4-27ce-47d2-9cc5-f84cf312986a` avec les budgets validés (service `OsintOpenPolicyService`, `api/osint_policy.py`) ; relire les valeurs et l'événement ACTIVATED/version épinglée.
- **B5** Smokes ciblés par le chemin applicatif, dans une enquête de smoke distincte sur le dernier run `ec7a6807` puis close : 1 `vt.file_report` ; 1 `advisor.consult` à dossier limité ; 1 `vt.download_sample` seulement si un échantillon admissible manque en quarantaine (sinon « validé hors ligne seulement ») ; 1 `sample.analyze` inerte. Observer requêtes physiques, claims, archives, coûts, erreurs, absence de secret. Pas de `make smoke-live` (Shodan), pas de preflight-supervised (exige base jetable).
- **B6** Nouveau `production_run` par le chemin normal (réutilisation content-addressée), synthèse `VERIFIED` + vérifier `(subject, run, synthesis_artifact_id, version, canonical_sha256)`, créer et démarrer une investigation `operator_led` rattachée à OSINT_OPEN ACTIVE ; ne rien supprimer/réinitialiser ; ne pas recopier la synthèse historique.
- Puis phase C : conduite de l'enquête avec CTI-ANALYST (Luna xhigh) et AUDIT-OBSERVER séparé (journal append-only), Bridge conseiller, dans les budgets ; livrables §9 de M07_MISSION.md.
- Risque R1 : un dossier Bridge citant une observation sha256 non associée à l'enquête est refusé (préexistant) → citer l'observation VT ou télécharger d'abord.
- Seuils d'observation : 80 % vt_units ; 3 refus consécutifs même raison ; 4 réservations advisor bloquées ; appel identique > 3 fois.

### Rapport final attendu
SHA déployé (`22d03e22`, empreinte `239f62da6a38b8ea`), Alembic, contrôles de santé, état de la nouvelle investigation, appels réellement effectués, budgets consommés/réservés, blocages.

### BLOCAGE JETONS (2026-10-27, cause établie par l'orchestrateur principal)
- Avant B2, le backend live avait `OPERATOR_API_TOKEN` et `OPERATOR_CLIENT_API_TOKEN` définis (len 54, vérifié). Le `make up` de B2 a été lancé depuis un shell qui ne les exportait pas ; `compose.yaml:97-98` (et ~582) les lit dans l'environnement du shell (`${VAR:-}`) ⇒ conteneurs recréés avec jetons vides ⇒ API en identité locale `dev-analyst` (loopback seulement : ports 127.0.0.1:8000/5173). **Erreur d'exécution de B2, non détectée par les contrôles post-B2** (on n'avait pas vérifié les jetons).
- Conséquence : outils opérateur 403 `operator_client_scope_required` ; B3/B4/B6 faits sous `dev-analyst`.
- Non fait volontairement : générer des jetons éphémères (changerait les identifiants de l'utilisateur), lire `.env`.
- Reprise : l'utilisateur relance `make up` (même image, schéma 0005) depuis un shell exportant ses deux jetons ; puis vérifier `len>0` dans backend/worker/job-recovery, puis ratification ou ré-activation de la politique sous identité vérifiée (voir rapport).
- Lacune de contrôle à ajouter à toute redéploiement : vérifier la présence des jetons AVANT et APRÈS.
