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

### RÉSOLU — jetons opérateur chargés (utilisateur, 2026-10-27 ~20:55 UTC) ; phase C lancée
- L'utilisateur a ajouté les jetons au `.env` et relancé `make up`. Vérifié sans afficher de valeur : deux jetons de 64 car. distincts ; `/api/identity/status` humain = authenticated/human_identity true ; client = authenticated/human_identity false ; `operator-status` + `operator/context?view=summary` OK avec le client ; Alembic 0005 ; inv `a8845772` running, 0 invocation ; politique `dc034634` active. B3/B4/B6 restent attribués à `dev-analyst` (identité locale d'origine) dans l'audit.
- Relais opérateur : `.autowork-runs/m07/relay.py` (processus de l'orchestrateur, sans jeton ; whitelist GET /api/investigations/<inv>/… et POST …/operator/tools/<tool> ; jamais de retry) ; `inbox/`→`outbox/`, journal append-only `.autowork-runs/m07/journal.jsonl`. Si le relais est arrêté : `cd .autowork-runs/m07 && nohup python3 relay.py &`.
- CTI-ANALYST : Paseo `83a9aeb8-bd9f-4aee-b0b5-57dde569027e`, codex/gpt-6-luna xhigh auto-review, workspace wks_9c25e352c5ccd0a5 ; journal analytique `analyst_log.md`, `checkpoint.md` dans le même dossier. Isolation analytique : ne lit pas l'investigation historique.

### D9 BLOQUANT — premier VT read d'un hash documenté refusé en enquête fraîche (2026-10-27 ~21:03 UTC)
- Fait : `a023` `vt.file_report` hash `6e7d9b33…` → invocation `9aaec14d…` `needs_review` `virustotal_external_effect_refused_source_references_mismatch`, 0 envoi VT, refs stockées `[]`. Scout `24aa8178` (lecture seule) : `_trusted_vt_source_references` (`investigation_service.py:1952`) ne lit que associations/observations de l'enquête ; le handoff n'en crée pas ; le test A3 pré-seede l'observation. Aucun contournement opérateur légitime.
- Correctif délégué : TECH-FIX `abbe829b…` (Luna xhigh) branche `m07/vt-documented-hash-refs` worktree `wks_1fa7f95c6f0b483b` ; ensuite : tests PG par l'orchestrateur, revue indépendante Luna xhigh, merge `--no-ff`, redéploiement (`make up`, schéma 0005 inchangé), puis « REPRISE » au CTI-ANALYST (mis en pause : pas d'avis final, pas de clôture).
- Autres faits : `advisor.consult` ×3 refusés au preflight `advisor_secret_field_remaining` (0 tour Bridge, `confirmed_non_execution`) ; `local.run_command` `policy_denied` (aucun échantillon associé). Findings internes proposés (h1-h4) sans pivot.

### D9/D10 — correctifs en cours (2026-10-27 ~22:00 UTC)
- Exigence utilisateur : plus aucune friction « référence de confiance » sous OSINT_OPEN (tout hash VT interrogeable/téléchargeable), et `advisor_secret_field_remaining` jamais levé pour rien.
- Branche `m07/vt-documented-hash-refs` (HEAD `feedfe1e`, 5 commits) : OSINT actif+pin enquête+provider vt ⇒ hash arbitraire OK (invocation + consume), `vt.intelligence_search` peut citer observations VT/outil de l'enquête ; HUMAN_G2 strict inchangé. PG : `test_investigation_vertical_postgres.py` 14 passed ; groupes offline verts. Revue Luna `1527d69c` : REVISE ; arbitrage orchestrateur = NON BLOQUANTS consignés : (a) `intelligence_search` accepte une citation `advisor_call` (chemin préexistant sur main, recherche = requête seule) ; (b) exception hash codée dans les repositories infra (dette de design, mimétique de l'existant).
- Branche `m07/advisor-secret-false-positive` : faux positifs du préflight advisor (chemins d'hôte en sous-chaîne, paramètres d'URL génériques) + diagnostic chemin JSON/type sans valeur. PG `test_e2e_phase3_adversarial_postgres.py` + vertical OK (241 passed). Revue `154dff3f` : REVISE (DSN `postgresql+asyncpg://` non détectées ; formes encodées de valeurs configurées courtes) → fixer `8bf45c8a` en cours. Limite acceptée : secret configuré fragmenté sur plusieurs champs.
- Ensuite : nouvelle revue, merge `--no-ff` des 2 branches, gates (make test-backend/lint/typecheck, PG), `make up` (jetons dans l'environnement, schéma 0005 inchangé), vérification empreinte, puis « REPRISE » au CTI-ANALYST `83a9aeb8` (en pause).
- Incident sécurité de session : une commande de diagnostic de l'orchestrateur a imprimé les valeurs opérationnelles des réglages (jetons opérateur, clés API, mots de passe locaux) dans la transcription de session (pas dans un fichier) ⇒ rotation recommandée avant tout partage.

### ÉTAT 2026-10-28 ~00:30 UTC — phase C en cours (orchestrateur #3, contexte élevé ⇒ relève recommandée)
- **Déployé** : `main@8c7f62b0` (empreinte 83e783abd99ad3dd sur backend/worker/analysis-worker/job-recovery), Alembic 0005, jetons opérateur chargés (dans `.env`, l'utilisateur), OSINT_OPEN `dc034634` v1 active, run `27478dcc`, inv `a8845772` `running` (état v54+). Gates sur main : ruff/mypy OK, unit 5254 passed (+3 échecs d'environnement connus `test_confined_analysis_dispatch`), integ 456 passed.
- **Correctifs mergés** (tous revus par Luna ; arbitrages consignés) : D9 refs VT sous OSINT (tout hash) ; D10 faux positifs secret-preflight advisor (+ diagnostic chemin/type) ; D11 corps d'observations citées chargés dans le dossier advisor ; D12 refus pré-envoi non ambigus. Résiduels acceptés : secret configuré fragmenté sur plusieurs champs non détecté ; `FrameworkNoSendProof` falsifiable par un port VT défectueux (non câblé) ; `intelligence_search` accepte citation `advisor_call` ; exception hash codée dans les repositories (dette) ; seuil 16 car. pour variantes encodées.
- **Correctifs EN COURS** (branches non mergées) : (1) `m07/advisor-contradiction-not-blocking` (agent `4846bbea`, wks_5536b65b08c23b82) : le préflight advisor exige un finding `refuted` par claim dès que la question contient « contradiction/refute… » ⇒ INSUFFICIENT_EVIDENCE impossible à lever ⇒ **0 consultation Bridge réalisée à ce jour** ; (2) `m07/historical-presend-by-claim-absence` (agent `4cabcc97`, wks_99f378dbeb3d4e8c) : l'invocation `9aaec14d…` (VT hash ZIP 6e7d9b33…, refusée AVANT envoi) reste `needs_review` ⇒ 409 `framework_proof_payload_mismatch` ; preuve forte = aucun `external_effect_claim` pour l'invocation. Procédure à la fin de chaque fixer : tests PG par l'orchestrateur (recette Typst D4 ; `PYTHONPATH=<worktree>/backend/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest …`, `TEST_POSTGRES_ADMIN_DSN`, TYPST_BINARY/FONT_BUNDLE_ROOT/TMPDIR), revue Luna indépendante, merge `--no-ff`, gates, `make up`, vérification empreinte (manifeste SHA-256 `.py` /app/src/cti_app vs git), puis nouvelle session CTI-ANALYST (jamais réutiliser une finie).
- **CTI-ANALYST** : #1 `83a9aeb8` (fini), #2 `5dfc8246` (fini : pivots VT réels), #3 `f17709a3` (fini : 2 downloads réutilisés `FlightManager.csproj`, `PsProxy.dll`, `sample.analyze` partial, `strings.list` PsProxy, findings H1 supported / H2–H4 qualified, `opinion.compose` → avis `128f991d…` v1 (prématuré : sans Bridge), 0 Bridge, budget VT 22/10000, DL 0, Bridge 0/200). Lecture de l'avis par le client = 403 `operator_client_scope_denied` (route `/opinion` : utiliser `operator/context` section avis, ou l'API humaine) ; ne pas confondre : à investiguer si bloquant.
- **Journaux** : `.autowork-runs/m07/{journal.jsonl,analyst_log.md,checkpoint.md,relay.py}` (relais PID vivant ; relancer : `cd .autowork-runs/m07 && nohup python3 relay.py &`). Observateur indépendant AUDIT-OBSERVER non encore lancé (à instancier après clôture analytique ou en cours, lecture seule du journal).
- **Prochaines étapes** : fixers → merge/redeploy → analyste #4 (Bridge plan_investigation, pivot ZIP 6e7d, downloads/analyses locales pertinents, revue Bridge, révision des findings/avis) → AUDIT-OBSERVER → EDITOR + TYPST-PDF (références `docs/references/CN2609_typ` et `docs/references/chpTypst`, vérifier existence/contenu réels ; relation avec `/home/nill/perso/work/AutoWork/chpTypst`) → FINAL-REVIEW → COMPARISON (après gel du livrable, avec 5f66ebfb) → verdicts §14 du contrat utilisateur (OPERATOR_AUTHENTICATION, OSINT_OPEN, NEW_INVESTIGATION, VT_REAL_PIVOTS, VT_DOWNLOADS, LOCAL_STATIC_ANALYSIS, BRIDGE_CONSULTATIONS, DECISION_AUDIT, ANALYTICAL_FINDINGS, FINAL_CTI_OPINION, TYPST_REFERENCE_COMPLIANCE, FINAL_PDF_QA, INDEPENDENT_REVIEW, HISTORICAL_COMPARISON, REAL_ANALYST_RERUN).
- **Verdicts provisoires (preuves)** : OPERATOR_AUTHENTICATION PASS (identity/status humain+client ok) ; OSINT_OPEN ACTIVE (dc034634 v1, activée sous identité locale `dev-analyst` AVANT jetons : à l'audit) ; NEW_INVESTIGATION RUNNING (a8845772) ; VT_REAL_PIVOTS PARTIAL→PASS probable (≥20 appels VT réels succeeded, voir journal) ; VT_DOWNLOADS REUSED_ONLY (2 samples réutilisés, 0 nouveau téléchargement) ; LOCAL_STATIC_ANALYSIS PARTIAL ; BRIDGE_CONSULTATIONS BLOCKED (0 tour, 6 refus préflight) ; reste non démarré.
- **Sécurité session** : l'orchestrateur #3 a imprimé des valeurs opérationnelles de réglages (jetons, clés, mots de passe locaux) dans sa transcription ⇒ rotation recommandée à l'utilisateur avant tout partage.

### ÉTAT 2026-10-28 ~07:30 UTC — RELÈVE vers orchestrateur #4 (contexte #3 saturé)
- **Contrat utilisateur phase C** : `docs/mission07/M07_CONTRACT_PHASE_C.md` (fait autorité avec M07_MISSION.md). Rôles : orchestrateur Sonnet 5.5 medium ; tous les autres Luna xhigh auto-review, session neuve par tâche.
- **Déployé** : `main@9d7bf7d4` (empreinte 7c48d54… voir ci-dessous), Alembic 0005, jetons chargés, OSINT_OPEN dc034634 v1 active, inv `a8845772` `running`, état v69+. Correctifs mergés et déployés : D9 (VT tout hash sous OSINT), D10 (secret-preflight faux positifs + diagnostic), D11 (corps d'observations citées chargés), D12 (refus pré-envoi non ambiguës), D13/D15 (une réfutation n'est JAMAIS un prérequis bloquant). Gates sur main (avant D15) : ruff/mypy OK, unit 5259 passed (+3 échecs d'environnement `test_confined_analysis_dispatch`), integ 456 passed ; D15 : advisor+PG ciblés 308 passed. Branche rejetée volontairement : `m07/historical-presend-by-claim-absence` (faux feu vert possible pour invocations d'avant le coordinateur ; revue ee7074b8) ⇒ l'invocation `9aaec14d` (VT hash ZIP 6e7d9b33…, refusée avant envoi) reste `needs_review` : seul un humain la réconcilie ; l'opérateur traite ce hash via d'autres outils.
- **BLOCAGE ACTUEL n°1 : Bridge n'a JAMAIS répondu (0 tour consommé / 200)**. Dernier refus : `e015` (invocation 11db4592…) : préflight de preuves OK (65/65) puis `external_effect_authority_required` (`authority_context_missing`, pré-envoi) : `bridge_investigator.py` ~376-410 `submit_bridge_consultation_once` : le transport de production exige l'autorité d'effet mais `build_external_effect_binding` absent/None ⇒ défaut de câblage de production jamais exercé en live. **Correctif en cours** : TECH-FIX `2f45949c-f8b9-41fe-8e11-b5aca0d2cd22` (wks_44fa83e6df5542df, branche `m07/advisor-effect-authority-wiring`, base 9d7bf7d4) ; à son retour : tests PG par l'orchestrateur (recette D4 : `PYTHONPATH=<worktree>/backend/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest …`, `TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres`, `TYPST_BINARY=$HOME/.local/share/autowork-tools/typst`, `PATH=$HOME/.local/share/autowork-tools/bin:$PATH`, `TMPDIR=$HOME/.cache/m06tmp`, `FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst`), revue Luna indépendante (session neuve), merge `--no-ff` dans main, gates (`.venv/bin/python -m ruff check src tests`, `ruff format --check`, `mypy src`, `pytest tests --ignore=tests/integration`, `pytest tests/integration`), `make up` (jetons dans `.env` ; schéma 0005 inchangé ; vérifier aucun job/invocation en cours avant), vérifier empreinte (manifeste SHA-256 `.py` de `/app/src/cti_app` dans backend/worker/analysis-worker/job-recovery == `git ls-files backend/src/cti_app`), puis NOUVEL analyste (#6).
- **Autre friction à surveiller** : (a) `e009` : préflight `TECHNICAL_BLOCKED` « Required canonical blob is unreadable or its read capability was not verified » sur 3 observations VT (ids 16ee954c…, 46931ae8…, b2a7192e…) — l'analyste a contourné en les retirant ; à diagnostiquer si cela revient ; (b) `opinion.revise` → `citations_invalid` sans détail : cause = blocs `fact` citant des findings seulement `qualified` (règle : `fact` exige un finding `supported`) ; détail des violations masqué par `OperatorOpinionToolAdapter.execute` (analyst_opinion_service.py ~1480) — défaut non bloquant, correctif possible (exposer `citation_violations`) ; (c) le client opérateur n'a pas de lecture directe `/opinion` (403) : utiliser `operator/context` section avis ; (d) un claim `vt.relationships bundled_files` du ZIP est `NEEDS_REVIEW` réel (invocation 36114580…) : ne pas rejouer à l'aveugle ; (e) le préflight Bridge a créé un blob interne `b23aa970…` (dossier base64) non transmis ; (f) pagination d'`operator/context/section` : curseur + `expected_version` + `expected_sha256` = `working_state_sha256`, limite ≤ 20.
- **Analystes** : #1 83a9aeb8, #2 5dfc8246, #3 f17709a3, #4 429e815e, #5 24467818 (tous finis). Acquis réels (journal `.autowork-runs/m07/journal.jsonl`, `analyst_log.md`, `checkpoint.md`) : ≥ 25 appels VT READ réussis (hashes documentés ; relations/behaviours ; Blackwood.dll, PsProxy.dll, RuntimeBroker.dll ; parents MSInfo.7z/Client.zip/Public - Copy.zip), 2 samples réutilisés + ZIP 6e7d… (134 661 o) téléchargé/réutilisé et analysé (`sample.analyze` partial : capa/FLOSS incompatibles, SMDA indisponible ; répertoire central 53 entrées via xxd), findings : H1 `qualified` (f6f0a34e…), H2 2b02047e…, H3 dd157c98…, H4 746907e4… `qualified` ; avis `128f991d…` v1 (composé prématurément, sans Bridge, à REVISER). Budgets : VT 22/10 000 consommées, DL 0/1 000, Bridge 0/200 (13 réservés), tool_seconds 31/86 400.
- **Relais** : `.autowork-runs/m07/relay.py` (PID vivant ; relancer : `cd .autowork-runs/m07 && nohup python3 relay.py &`), inbox/outbox/processed/journal.jsonl ; whitelist GET `/api/investigations/<inv>/…` + POST `…/operator/tools/<tool>` avec jeton CLIENT lu dans le conteneur ; l'orchestrateur lit les observations de refus par le relais (agent `ORCHESTRATOR`). Monitor du journal : `tail -F journal.jsonl` filtré (re-armer, expire 30 min).
- **Prompt analyste type** (réutiliser/adapter celui des sessions #3–#5, protocole relais, ids `f###`, situation, ordre : Bridge plan_investigation d'abord, puis exécution du plan, revues Bridge, findings/avis, rapport court). Ne jamais réutiliser un agent fini.
- **Suite après analyste** : AUDIT-OBSERVER (lecture seule du journal + états canoniques) ; EDITOR + TYPST-PDF (références `docs/references/CN2609_typ`, `docs/references/chpTypst` : vérifier existence/contenu réels) ; FINAL-REVIEW ; COMPARISON (après gel, avec 5f66ebfb) ; verdicts §14 du contrat.
- **Verdicts provisoires** : OPERATOR_AUTHENTICATION PASS (identity/status humain+client authentifiés) ; OSINT_OPEN ACTIVE (activée sous `dev-analyst` avant jetons : à l'audit) ; NEW_INVESTIGATION RUNNING ; VT_REAL_PIVOTS PASS (≥25 appels réels) ; VT_DOWNLOADS REUSED_ONLY ; LOCAL_STATIC_ANALYSIS PARTIAL ; BRIDGE_CONSULTATIONS BLOCKED ; DECISION_AUDIT PARTIAL (journal relais + analyst_log, pas encore d'observateur) ; ANALYTICAL_FINDINGS PARTIAL ; FINAL_CTI_OPINION BLOCKED (v1 prématuré) ; TYPST_REFERENCE_COMPLIANCE/FINAL_PDF_QA/INDEPENDENT_REVIEW/HISTORICAL_COMPARISON non démarrés ; REAL_ANALYST_RERUN IN_PROGRESS.
- **Sécurité session** : l'orchestrateur #3 a imprimé des valeurs opérationnelles de réglages (jetons opérateur, clés API, mots de passe locaux) dans sa transcription : rotation recommandée à l'utilisateur ; ne JAMAIS inspecter `.env`/réglages ; ne jamais utiliser le jeton humain pour une décision humaine.
- **Commits locaux non poussés** : tout depuis `22d03e22` (merges D9–D15 + docs). Aucun push n'a été demandé.

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

### ÉTAT 2026-10-28 ~08:00 UTC — orchestrateur #4
- TECH-FIX `2f45949c` terminé : seulement un test de régression (bd99f2ad), aucun défaut de câblage trouvé offline. Diagnostic live (lecture SQL) : réservation de `e015` (11db4592) `reserved`, montants == reserved_budget ⇒ l'autorité d'effet est probablement construite ; c'est le builder de binding du transport qui renvoie None/refuse.
- Fuite de réservations CONFIRMÉE (inv a8845772) : `reserved` bridge_turns 13, tool_seconds 47040, context_bytes 1378325, context_tokens 344586, vt_units 13 (2 ops) ; refus advisor pré-envoi ne libèrent pas.
- Fixers lancés (Luna xhigh, worktrees neufs base main@7dd4ac18) : `5aa341be` branche m07/advisor-binding-live-diagnosis (prédicat live du binding) ; `67aee1fe` branche m07/advisor-preflight-release-reservation (libération unique des réservations sur refus advisor pré-envoi). Suite : tests PG par moi, revue Luna neuve, merge --no-ff, gates, make up (jetons vérifiés avant/après), empreinte, analyste #6.
