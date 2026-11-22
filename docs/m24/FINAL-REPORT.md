# M24 — Rapport final de clôture (atelier d'investigation CTI hybride)

Date : 2026-11-22 · Branche : `m24/integration` (base `main` 33891bf8, 128 commits locaux, 377 fichiers, +140 k/−0,2 k lignes) · **Rien n'est poussé, rien n'est fusionné dans `main`, rien n'est déployé, aucun DDL sur base vivante.** Légende : DONE / PARTIAL / BLOCKED / FAIL / UNKNOWN ; niveaux de preuve : *implémenté* (code+tests unitaires), *simulé* (faux VT/Bridge/CLI), *réel local* (PostgreSQL, sidecar sur UDS tmp, Typst, bwrap/slirp locaux), *attestation locale*, *fournisseur réel* (jamais exercé ici).

## 1. HEAD et périmètre
`m24/integration` — SHA à lire par `git rev-parse` (dernier commit connu à l'écriture : voir `git log -1`). Branche locale uniquement.

## 2. Architecture avant / après
Avant : enquête legacy (opérateur Bridge, un avis de fin). Après : atelier `creation_profile="m24_workshop"` (⇒ `bridge_only` + `OPERATOR_LED`) avec workspace hôte, plan Bridge, tâches à bail/fence, passations versionnées, sessions Codex/Claude, contributions importées, Notes de l'analyste ancrées multiples, cockpit à 5 vues. Une autorité canonique par type de décision (ADR-0011 D1) ; aucun second pipeline preuve/finding/Bridge/publication (R02, R04 : VERIFIED).

## 3. ADR
ADR-0011 (ACCEPTED, 2 revues indépendantes R01/R01b) + amendement d'implémentation 2026-11-22 (0026, D8 lectures opérateur, sweeper, ouverture en 3 phases, ancres, portes). Design : `docs/design/investigation-workshop/{WORKSPACE-AGENT,SESSION-RUNNER,GLOSSARY}.md`.

## 4. Modules (principaux)
`sidecars/workspace_agent/**` (agent hôte UDS, sessions/superviseur, shim MCP, profils Codex/Claude/fake, scripts confinés) · backend : `application/{investigation_workspace,workspace_notes,investigation_bootstrap*,investigation_plans*,investigation_tasks*,investigation_handoffs,agentic_sessions,agentic_tool_broker,work_contributions,opinion_note_proposals,agent_session_disclosure,host_session_release}` + API correspondantes · frontend `features/investigation/{api,shell,overview,plan,tasks,samples,workspace,opinion}` · tests `backend/tests/acceptance/**`, `frontend/e2e/investigation-workshop.spec.ts`.

## 5. Migrations
Chaîne linéaire additive `0019_workspace_bindings` → `0026_workspace_note_annotations` (tête). Rejouées : `tests/integration/test_migrations.py` 171 passed ; `test_alembic_chain.py` verte ; test de forme partielle corrigé pour la FK 0026.

## 6. Matrice de capacités
| Capacité | Statut | Niveau de preuve |
|---|---|---|
| Workspace hôte (ensure/sync/watch/conflits/exports, base_samples 0440) | DONE | réel local (sidecar UDS tmp, PG) |
| Dégradation « Workspace local indisponible » (fail-closed) | DONE | réel local + e2e interceptée |
| Bootstrap VT (inventaire, plan, snapshot, enveloppe G2 initiale) | PARTIAL | simulé (faux VT) ; routes VT réelles download/PCAP NON vérifiées |
| Plan Bridge + tâches + machine à états + bail/fence | DONE | réel local PG ; Bridge simulé |
| Sweeper de baux, libération de session hôte, ouverture en 3 phases, récupération des sessions orphelines | DONE | réel local PG |
| Passations versionnées, transferts, instructions | PARTIAL | file d'instructions écrite mais jamais drainée (R5-M4) |
| Sessions Codex/Claude (profils, supervisor, shim, HMAC D7) | PARTIAL | simulé/fake CLI ; transport réel NON prouvé (spike H0) |
| Broker agentique (outils registre + `analysis.run_script`, `note.capture`, `contribution.import`) | DONE | réel local PG |
| Contributions (jamais preuve primaire, jamais `supported`, G3) | DONE | réel local PG |
| Notes d'atelier + annotations (0026) | DONE | réel local PG |
| Propositions de Notes Bridge (hash/version, same-anchor = remplacement, ancre rendable unique) | DONE | réel local PG + Typst |
| PDF réel avec ≥2 Notes à deux ancres, placement asserté | DONE | réel local (Typst 0.15.1) |
| Notes avec tableaux/figures M23 | NOT SUPPORTED (descope) | texte seulement |
| Cockpit 5 vues (desktop) | DONE sous réserves (§12) | implémenté + e2e réseau intercepté |
| Cockpit contre backend M24 réel dans le navigateur | UNKNOWN / non fait | — |
| Réseau CLI isolé (bwrap `--unshare-net` + slirp `--disable-host-loopback`) | PARTIAL | attestation locale (probe réel bwrap+slirp) |
| Fournisseurs réels (VT, Bridge, Codex, Claude) | UNKNOWN | jamais exercés (interdit) |

## 7. Démonstrations
Scénario synthétique 24 étapes `tests/integration/test_acceptance_scenario_postgres.py::test_full_24_step_scenario` (5 hashes, 2 domaines, 2 IPs ; faux VT 3 dispo/2 indispo ; faux Bridge ; faux Codex/Claude H1 ; PG réel ; sidecar réel UDS tmp ; Typst réel) : **24/24 étapes**, rejoué par l'orchestrateur après chaque fusion (35–45 s ; 5/5 consécutifs). Journal : `docs/m24/acceptance-scenario.md`. **Étape 08 : attestation réseau SIMULÉE** par le profil factice. Captures cockpit 1440×900 (5 vues) lues par les revues (non versionnées : `frontend/test-results/` ignoré).

## 8. Tests / gates (observés, orchestrateur)
- Backend : `ruff check` OK ; `ruff format --check` OK (1276) ; `mypy src tests` OK (1256 fichiers).
- Unitaires (env Typst) : 8183 passed, 4 failed = 3 `test_confined_analysis_dispatch` (capa/mwcp, échouent aussi sur main) + `test_investigation_transfers::test_real_sidecar_instruction_reports_the_modified_human_note` qui n'échoue QU'AVEC un `TMPDIR` long (AF_UNIX >108 octets) : 61 passed avec `TMPDIR` par défaut. `tests/workspace_agent` seul : 382 passed. Recette : lancer `--ignore=tests/workspace_agent` avec l'env Typst et `tests/workspace_agent` séparément sans `TMPDIR`.
- Intégration PostgreSQL (`make test-integration` complet) : 727 passed, 2 failed, 26 errors → 26 erreurs = mon drapeau `-p no:logging` (fixture `caplog` absente ; les 6 fichiers rejoués sans le drapeau : 120+32+1+15+12+3 passed) ; 1 échec réel (FK 0026 dans le test de migration) corrigé (3 passed) ; 1 échec `test_agentic_tool_broker_postgres::test_concurrent_identical_signed_calls_create_one_invocation` (409 une fois en exécution complète, 6/6 isolé) : **flake non expliqué, UNKNOWN**. Après FIX-C2, rejeu isolé de tous les fichiers PG M24 + scénario + migrations : tous verts.
- `make test-workflow-full` : rc=0. `make test-editorial-browser` : 8 passed (nécessite `CHOKIDAR_USEPOLLING=1` ici : limite inotify de 128 instances saturée par les processus d'agents).
- Frontend : `pnpm lint`, `pnpm typecheck`, `pnpm test --run` (733 tests / 107 fichiers), `pnpm build` OK ; Playwright `investigation-workshop` 13/13 ×3 (serveur `vite preview`, config inchangée ; le `webServer` dev échoue en EMFILE ici).
- Matrice d'acceptation A01–F10 : 49 scénarios PASS d'après `docs/m24/acceptance-matrix.md` (rejoué par l'agent ; l'orchestrateur a rejoué la collecte `test_a01_to_f10` et les oracles PG/unitaires touchés, pas les 49 un par un). 6 oracles ADR renommés vers les vrais tests.
- Non exécuté : CI GitHub (le correctif P0 M24-00 n'y est pas revérifié), tout appel fournisseur réel.

## 9. Revues indépendantes (6 + re-revues)
R2 architecture : GO-réserves. R3 sécurité : NO-GO → corrigé partiellement (voir §10). R4 provenance : GO-réserves (F1 corrigé par `contribution.import`). R5 concurrence : NO-GO → corrigé (FIX-A, FIX-C2) ; R5b restait NO-GO avant FIX-C2 (2 blockers désormais corrigés, **non re-revus**). R6 UX : NO-GO → corrigé (FIX-U/U2/U3) ; R6b/R7b : GO-avec-réserves. R7 éditorial : NO-GO → corrigé (FIX-E) ; re-revue GO-réserves. Rapports : `.autowork-runs/m24/R0*.md`.

## 10. Blockers et résiduels — état honnête
- **BLOCKED pour fournisseurs/CLI réels (décision humaine + spike H0 requis)** :
  - BL1 : le shim MCP appelle `http://127.0.0.1:8000` depuis le sandbox ; `isolated_egress` bloque le loopback ⇒ les sessions réelles ne peuvent pas appeler le broker (fail-closed). Il faut un relais via l'UDS du superviseur.
  - BL2 : la NAT slirp laisse joindre l'IP LAN de l'hôte, les bridges Docker (PG/MinIO/Redis par IP de conteneur, INFÉRÉ) et Internet sans allowlist ⇒ pas d'egress « fournisseur seul ». Tant qu'une politique d'egress n'est pas décidée, **n'activez pas `M24_AGENT_SESSION_KEY`** (les sessions restent en 503 par défaut).
  - Le proxy Vite injecte `OPERATOR_API_TOKEN` : en mode `host_network_unsafe` un CLI peut agir comme humain. Override dev uniquement.
- `analysis.run_script` : le worker n'a pas bwrap, seccomp ne refuse pas `setsid` ⇒ échappement possible de `killpg`.
- R5 non traités : `interrupt` sous verrou de ligne, registre d'adaptateurs volatil au redémarrage, revalidation sous verrou de `note.capture`/outils registre, file d'instructions jamais drainée.
- Flake broker concurrent (§8), un `HostSessionStateConflictError` observé une fois à l'étape 22 avant FIX-A (non reproduit en 5+ rejeux).

## 11. Risques résiduels
Attestation réseau et transports CLI non prouvés avec un vrai Codex/Claude ; capacité VT download/PCAP non vérifiée ; chronologie sans passations/contributions/sessions ; `note.capture` hors `tool_invocations` ; aucune CI GitHub rejouée.

## 12. Réserves fonctionnelles du cockpit
Actions « Demander un avis à Bridge / une rédaction » par tâche absentes (aucune route task-scoped) ; ancres d'avis saisies à la main (pas de lecture des `publication_anchors`) ; panneau opérateurs sans ops/budget ; G2 extend/revoke absents ; tableaux/figures dans les Notes non supportés ; transport multi-paragraphes Bridge non prouvé ; messages `invalid_observation_anchor` non traduits.

## 13–19. Décisions de l'orchestrateur à confirmer
`delivery_code = IR+YYYYMM` de `Edition.period_start` (non confirmé ; l'exemple était IR2609) ; `bridge_only` limité aux enquêtes `m24_workshop` ; même ancre = remplacement ; scripts v1 python3 seul, entrée RO, sans réseau ; redémarrage du superviseur ⇒ sessions `lost`, jamais relancées ; D8 : les agents ne lisent pas l'API par GET signés ; identité locale : ouverture de session refusée sauf override dev ; réglages fail-closed (caps VT/acquisition = 0, clé de session absente ⇒ 503, `AGENT_SESSIONS_CLI_NETWORK_MODE=isolated_egress`).

## 20. Verdict
**PARTIAL / NO-GO pour une exploitation avec CLI et fournisseurs réels** ; **GO pour une revue de l'intégration locale** : le chemin humain + Bridge simulé + workspace + PG + Typst est prouvé de bout en bout, les portes fail-closed sont en place, mais les transports et l'egress des sessions agentiques réelles, la capacité VT réelle et le navigateur contre le backend M24 réel ne sont pas prouvés. Autorisations/décisions humaines requises : spike H0 (CLI réels, MCP, credentials, egress), politique d'egress, activation de `M24_AGENT_SESSION_KEY`, capacité VT réelle, format `delivery_code`, rétention des blobs/journaux importés, plafond de tours Bridge de l'enveloppe initiale, configuration des caps VT/acquisition, CI GitHub, tout push/merge/déploiement.
