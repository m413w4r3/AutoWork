# Traces d'analyse Mission 07 — pièces pour audit externe

Instantané pris le **2026-10-30 ~15:00 UTC** par l'orchestrateur #6 à la demande de l'utilisateur (perte de contrôle sur l'orchestrateur #7 ; audit externe prévu).
Les originaux vivent dans `.autowork-runs/` (ignoré par Git) ; ce dossier en est une copie versionnée. **L'enquête `782ff20c` (m07c) était encore active** : ses fichiers
(`journal.jsonl`, `operator_log.md`, `raw_*.tar.gz`) sont un état partiel à cette heure.

## État du dépôt / de la plateforme au moment de l'instantané
- `main` = commit poussé juste après cet instantané ; code déployé : empreinte `436f27d316f70854` (identique à `git_fingerprint.py` de `main`), Alembic **`0007_vt_retry_trust`** (migration ajoutée par l'orchestrateur #7 ; sauvegarde DB exclue de ce dossier).
- Contenu validé : tests/gates lancés par l'orchestrateur #6 jusqu'au commit `c921848c` (unitaires 5 337 + PG/workflows 499 verts avant le déploiement `b450663f`). Les 28 commits suivants (`c921848c..7b1460d7`, préfixe `m07c`) sont ceux de l'orchestrateur #7 et de ses sous-agents Luna ; je ne les ai **pas** revalidés.
  L'orchestrateur #7 indique dans `m07c-782ff20c/CHECKPOINT_ORCH7.md` avoir exécuté des suites PG ciblées (118-119 tests).
- Lecture recommandée avant les traces : `../M07_ORCH6_REPORT.md` (ce qui a été fait, leçons, dette), `../M07_HANDOFF_ORCH7.md` (mission), `../../operations/m07-operator-behavior-audit.md`, `../reports-orch6/` (comparaison, rapports d'observateurs, QA PDF).

## Contenu
| Dossier | Enquête | Agents | Contenu |
|---|---|---|---|
| `m07-a8845772/` | `a8845772-5025-4702-8850-2b6be776855b` (run 13) | orchestrateurs #5, analystes CTI 1-10 (Luna) | `analyst_log.md`, `checkpoint.md`, `journal.jsonl` (relais), `observer/` (rapports d'audit + évaluation Bridge), `editorial/*.md` (brief éditeur, QA PDF), scripts d'actions humaines préparées (`*.sh`), `raw_relay_requests_responses.tar.gz` |
| `m07b-86ece81f/` | `86ece81f-33bf-4230-9c5b-be55ea67855e` (run 14) | orchestrateur #6, opérateurs A-D (Luna), observateurs #1-#3, comparateur | `OPERATOR_BRIEF.md`, `operator_log.md`, `FINAL_CHECKPOINT*.md`, `journal.jsonl`, `observer/`, `observer2/`, `observer3/`, `comparison/report.md`, `editorial/pdf_qa_004.md`, `opclient.py`, `raw_relay_requests_responses.tar.gz` |
| `m07c-782ff20c/` | `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03` (run 15) | orchestrateur #7, opérateurs A-E…, sous-agents (workers/), observateur | brief, `operator_log.md`, `FINAL_CHECKPOINT*.md`, `STATE_NOTE_*.md`, `journal.jsonl`, `observer/`, `audit/` (3 audits VT/Bridge/tools), `diag/`, `workers/`, `dynamic_requests/` + `dynamic_results/` (exécution dynamique autorisée par l'utilisateur), `orchestrator_requests/`, `DYNAMIC_AUTH.md`, `CHECKPOINT_ORCH7.md`, `raw_relay_requests_responses.tar.gz` |

### Lire une trace
- `journal.jsonl` : une ligne par requête relayée (`ts`, `id`, `agent`, `method`, `path`, `purpose`, `context`, `http`, `response_sha256`, `response_bytes`, `ambiguous`, `invocation_id`, `invocation_status`, `working_state_version`).
- `raw_relay_requests_responses.tar.gz` : `processed/<id>.json` = requête exacte de l'agent ; `outbox/<id>.json` = réponse brute du backend (corps ≤ pages de 16 Ko d'observation) ; `inbox/` (m07 seulement) = demandes non traitées. Le `response_sha256` du journal = SHA-256 du champ `body` de l'`outbox`.
- `operator_log.md` / `analyst_log.md` : journal de travail des agents (hypothèse → pivot → résultat attendu → coût → décision, puis résultat). Ce sont des notes d'agents, **pas des preuves CTI** : la base canonique est PostgreSQL + MinIO (non exportée ici).
- Les rapports `observer*/` et `comparison/` sont des audits indépendants par des agents Luna ; leurs faits/inférences sont séparés dans chaque rapport.

## Ce qui est volontairement EXCLU
- `.autowork-runs/m07c/backup/tool_invocations_pre0007.dump` (dump de base de données), `relay.log`, `relay-status.json`, copies de `relay.py`, répertoires `inbox/`/`inflight/` vides, PDF et images de pages (`editorial/*.pdf`, `pages-*/`), `.autowork-runs/run12-editorial-2026-10-20/` (mission antérieure, 311 Mo), `.env`, secrets, `var/`.
- Aucun octet d'échantillon n'est copié par ce script ; les corps d'observation peuvent toutefois contenir des extraits hexadécimaux/texte d'échantillons ou de rapports VT, comme tout `outbox/`.

## Contrôles de confidentialité effectués (2026-10-30)
- Les valeurs de `OPERATOR_API_TOKEN` et `OPERATOR_CLIENT_API_TOKEN` (lues dans le conteneur backend, jamais affichées) ont été recherchées en chaîne fixe (`grep -F -f -`) dans `.autowork-runs/m07*`, `docs/`, `backend/src`, `scripts` : **0 occurrence**.
- Les variables d'environnement du backend/worker dont le nom contient KEY/TOKEN/PASSWORD/SECRET/DSN/SIGN : aucune valeur non vide retrouvée dans les traces (deux variables vides, `WEBAI_API_KEY`/`QWEN_API_KEY`, donnent un faux positif de `grep` sur motif vide).
- Motifs génériques (x-apikey, `apikey=…`, `Authorization: Bearer …`, signatures d'URL signées, clés privées, `AKIA…`, `sk-…`, `ghp_…`) : 0 occurrence dans ces dossiers.
- **Limite** : ces contrôles ne couvrent pas le contenu des archives `.tar.gz` par décompression octet à octet des corps JSON ; ils ont porté sur les mêmes fichiers avant archivage. Par politique du projet, les matériaux d'implant/CTI (clés, jetons d'attaquant, URL de commande publiés ou extraits) sont conservés en clair ; seuls les identifiants opérationnels d'AutoWork sont proscrits. Relire avant toute diffusion publique.
