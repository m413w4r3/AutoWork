# M24.1 — Rapport final (durcissement et finalisation de l'atelier d'investigation M24)

Orchestrateur #2 : Claude Sonnet 5.5 (high). Sous-agents d'écriture et de revue : DeepSeek Flash max (`deepseek-claude/deepseek-flash[1m]`) — **aucune revue n'a été faite par GPT-6 Luna** ; chaque revue ci-dessous est étiquetée « DeepSeek Flash max ». Date : 2026-11-23.

Légende : **DONE** (prouvé par une commande rejouée), **PARTIAL**, **BLOCKED** (cause démontrée), **FAIL**, **UNKNOWN** (non observé). Niveaux de preuve : *unitaire*, *PG réel* (PostgreSQL jetable 127.0.0.1:55432), *navigateur réel* (FastAPI + PG + sidecar UDS + Typst réels, fournisseurs factices), *CLI réel* (binaire réel sur cet hôte, spike H0), *fournisseur réel CTI* (jamais exercé).

## 1. SHA de départ et SHA final
- Départ : `main == origin/main == 1d52106f2672570c3e90255ce18a5645dc0909b1` (inchangé).
- Final : branche locale `m24-1/integration`, SHA à lire par `git rev-parse HEAD` (dernier SHA connu à l'écriture : `234b77ee`, puis le commit du présent rapport). 167 commits locaux, 274 fichiers, +60 331/−2 087 lignes.
- **Rien n'est poussé, rien n'est fusionné dans `main`, rien n'est déployé, aucun DDL sur base vivante, la stack Docker en cours n'a jamais été touchée.** Seul conteneur démarré : le PG de test jetable `cti-bulletin-postgres-test-1` (`docker start`, tmpfs, profil `integration-test`).

## 2. Résumé exécutif et verdict
Le parcours cible (article → workspace → collecte VT autorisée → plan Bridge → tâche Codex/Claude → notes livrées → transfert → consultation et rédaction Bridge → ancre visuelle → blocs vérifiés → validation humaine → PDF Typst réel) est **prouvé de bout en bout dans le navigateur réel avec fournisseurs et CLI factices** (24 étapes + G2) et **prouvé en CLI réels pour la session minimale, l'egress, le relais MCP, l'interruption et le nettoyage** (spike H0). **Verdict : PARTIAL — GO pour revue/intégration locale ; NO-GO pour une exploitation avec VT/Bridge/Codex/Claude réels** tant que les autorisations de la §21 ne sont pas données (voir §20 pour ce qui n'a pas été ré-attesté en réel après les derniers correctifs).

## 3. Problèmes reproduits, causes racines, fichiers (extraits ; détail dans `.autowork-runs/m24-1/REGISTRY.md`)
| Problème | Cause racine (VÉRIFIÉE) | Correctif |
|---|---|---|
| CI rouge au départ | tests dépendants de l'horloge à l'import, AF_UNIX >108 octets, shellcheck | F0-A/B/C : fixtures seulement |
| CLI réel sans accès au broker / egress libre | shim en loopback + NAT slirp | relais UDS broker, proxy d'egress CONNECT à allowlist, netns sans route (`sidecars/workspace_agent/sessions/{relay,egress,entrypoint,process}.py`) ; `host_network_unsafe` supprimé |
| `analysis.run_script` évadable | worker sans bwrap, seccomp laissait `setsid` | subreaper + Landlock + seccomp ; batterie conteneur réelle 11/11 (`scripts/ops/verify_run_script_confinement_container.sh`) |
| File d'instructions jamais drainée | aucune livraison | migration 0027, service + boucle, API pull/ack strict/cancel, shim `instruction.pending/.ack` |
| Capacité `next_run_instruction` non annoncée, manifeste d'outils jamais posté | `set_tool_manifest` sans appelant | WIRE-1 |
| Handoff créé sans `task.handoff_ref` | `InvestigationHandoffService.create` ne l'estampillait pas | `set_handoff_ref` à la création seulement (F7a, durci) |
| Bwrap tuait chaque sandbox | `PR_SET_PDEATHSIG` lié au thread forkeur | thread d'ancrage (H0) |
| TLS CLI impossible, shim absent, credentials Claude | pas de `/etc/ssl`, `catalog.py` non copié, `--bare` sans OAuth | adaptateurs corrigés (H0) |
| Copie de credentials survit à close/crash ; `collect` persiste un token fournisseur | purge absente ; filtre limité au secret D7 | R2BFIX |
| Instruction annoncée livrée alors que l'hôte tronque ; famine du drain ; sessions `lost` jamais libérées ; `delivering` terminal jamais réconcilié ; famine de réconciliation | requêtes non filtrées / borne en-tête / `closed_at` | R4FIX, R4FIX2, R4FIX3 |
| `task_context` brut → 422 ; rejeu idempotent 409 | garde avant sanitizer ; empreinte fondée sur l'état courant | R1FIX-A, R3FIX-B (`operator_request_identity`) |
| Note `article_element` acceptée puis non publiable | augmentation V9/V10 → V8 perdait les placements | R3FIX (préserve V9/V10, allowlists édition) |
| Éditions de bulletin refusaient `ioc_table`/`canonical_code` | projection sans contexte | B5c |
| Aperçu d'avis 503 sur base V9/V10 enrichie ; table IOC dates/empreinte superposées | corps V5 sans enveloppe ; poids de colonnes | R6FIX |
| Route VT 404 classée `not_found` | mapping codes route/fichier | VT-FAKE |
| Routes D7 `collect/reconcile` en 401 ; run `lost` non réconciliable ; refus pré-envoi VT classé ambigu | allowlist trop étroite ; modèle de fil ; preuve pré-envoi | E2E-ABC |
| Enum advisor sans `interpretation/critique` ; 2e requête de Note refusée `stale_working_state` | schéma d'outil ; CAS non invalidé côté front | F7b |
| UX : débordement 163 px, cases pleine largeur, jetons machine, onglets hors écran | CSS/labels | R5FIX |
| mypy rouge (15 erreurs) | tests M24.1 mal typés — **les rapports d'agents les disaient « préexistants » : c'était faux** | TYPEFIX |

## 4. Diagrammes réseau/IPC et confinement
CLI (bwrap `--unshare-net`, interface `lo` seule, aucune route) → entrypoint/forwarder `127.0.0.1:3128` → `egress.sock` (UDS) → proxy d'egress hôte (CONNECT uniquement, port 443, hôte exact de l'allowlist, résolution unique validée, adresses loopback/LAN/Docker refusées) → fournisseur. Broker : shim MCP → `broker.sock` → relais UDS hôte (3 routes fixes, en-têtes fermés, corps re-lié à la session) → backend en loopback, HMAC D7 vérifié, session active, binding tâche, bail courant. Scripts : subreaper + Landlock + seccomp, sans réseau.

## 5. Politique d'egress
Fichier de profil `autowork-session-egress.v1`. Hôtes observés par le spike H0 : Codex → `chatgpt.com` seul ; Claude → `api.anthropic.com` seul. Télémétrie/téléversement (`ab.chatgpt.com`, `sdmntpr*.oaiusercontent.com`) refusés par défaut. IP-literal et port ≠ 443 refusés ; hôte hors liste 403 `host_not_allowed` ; adresse résolue interdite 403 `address_forbidden`.

## 6. Tests d'interruption, concurrence, notes, instructions
Interruption SIGTERM réelle (Codex et Claude) → `stopped`, aucun processus résiduel ; kill du superviseur ⇒ bwrap et descendants disparus ; sessions `lost` réconciliables et libérées (R4FIX) ; CAS/fence/bail revalidés en transaction ; instructions : refus hôte plutôt que troncature, livraison au run suivant, ack strict, réconciliation du journal, non-famine (2 lectures bornées). Scénarios A/B/C PG (continuité, sécurité/reprise, dégradation) : 5 tests (24 étapes + A + B + C1 + C2).

## 7. Profils Codex et Claude (spike H0 — `docs/operations/host-cli-spike-2026-11.md`)
- **Codex 0.163.0 : ATTESTÉ** (modèle `gpt-6-luna`) : session réelle via superviseur + netns + proxy ; `HTTPS_PROXY` respecté ; 220 CONNECT autorisés sur 9 sessions proxifiées, 7 `host_not_allowed` (télémétrie) ; MCP `instruction.pending` puis `ack` aller-retour par relais UDS, HMAC D7 valide ; interruption, close, nettoyage. 10 sessions réelles au total (dont 5 tuées avant le correctif PDEATHSIG) ; coût en USD **UNKNOWN** (plan ChatGPT ; 153 943 tokens entrée / 1 457 sortie sur le run réussi). Modèle `gpt-5.1-codex-mini` : non supporté (400).
- **Claude Code 2.1.284 : ATTESTÉ** (modèle `claude-haiku-4-5-20251001`) : OAuth de l'utilisateur par copie 0600 dans le profil jetable ; 12 CONNECT / 4 sessions, `api.anthropic.com` seul ; MCP pull+ack ; `Write` → `result.md` collecté ; interruption et close propres ; **coût mesuré USD 0,1021**. 4 sessions réelles.
- **Écart à l'autorisation** : l'autorisation prévoyait « un seul prompt trivial par CLI » ; le spike a lancé 10 (Codex) + 4 (Claude) sessions pour diagnostiquer des défauts réels, **sous la borne de coût, sans donnée CTI**. À signaler honnêtement.

## 8. État VT (fournisseur factice uniquement)
`docs/operations/m24-1-vt-fake-capabilities.md` : 5 seeds (3 disponibles / 2 indisponibles), IP, domaines, rapports partiels, refus de téléchargement, quota, replay, révocation, export workspace, plafonds visibles. Défaut corrigé : route 404 → `route_unavailable`. Refus de route avant envoi → refus exact `UNAVAILABLE`. **Aucune requête VT réelle.** Non couvert au niveau PG : mapping route/fichier, arrêt sur quota, replay multi-seed avec clé neuve.

## 9. Parcours navigateur réel et captures
`make test-investigation-browser` (rejoué sur le HEAD final) : 34 tests du serveur de test + 1 test Playwright « parcours atelier M24 étapes 1–24 : workspace, sessions, Note, PDF, G2 » (~50 s). Backend FastAPI réel, PG migré à la tête, sidecar UDS réel, Typst réel ; VT/Bridge/CLI factices ; Bridge auto-répondeur déterministe incluant une réponse à deux paragraphes (conservation des sauts assertée dans le PDF). **28 captures** `docs/m24/captures-m24-1/` (1280×800, 1440×900, 1920×1080 : cockpit, tâche Codex/Claude, opérateurs, sélecteur d'ancre, revue de Note, workspace/notes, aperçu PDF, G2 révoqué).
Limites : l'ordre des étapes 16–18 est aménagé (ADR-0011 D3 interdit un transfert depuis `awaiting_review`/`completed`) ; le bloc `article_element` n'est **pas** exercé dans le navigateur (le seed n'a pas d'éléments M23 — couvert par tests PDF réels côté backend) ; les tests Playwright « interceptés » (`e2e/investigation-workshop.spec.ts`, 20 tests) ne comptent pas comme preuve de bout en bout.

## 10. Rédaction Bridge (tests)
Consultation task-scoped (`POST …/tasks/{id}/consultations`, un seul `advisor.consult`, attribution humaine `context_task_id`, idempotence par identité de requête), demande de plan avec `context_task_id`, proposition de Note (pin de base + hash de paragraphe, `stale_base`, `note_anchor_text_changed`), correction, acceptation par version/hash CAS. Bridge reste seul auteur de la prose (`extra=forbid`, aucun champ de prose humaine ; légendes inline déterministes).

## 11. PDF, ancres et blocs
Helper unique `publication_note_body_anchors` (V9/V10 alignées sur le rendu). Blocs : `article_element` (cross-référence sans duplication), `canonical_code` (octets exacts du blob), `ioc_table`, `protocol_figure`. PDF réel Typst 0.15.1 : deux Notes à deux ancres, provenance déterministe, bulletin byte-identique à l'aperçu pour les mêmes blocs. Décisions : l'ancre interne et l'UUID d'observation restent imprimés dans la ligne de provenance (R6-F3 conservé).

## 12. Résultats par gate (commande, environnement, commit, preuve)
Environnement : Python 3.12.3 hors Conda, Typst 0.15.1 épinglé, PG de test jetable, un seul pytest à la fois.
| Gate | Statut | Preuve rejouée par l'orchestrateur |
|---|---|---|
| Lint backend (`ruff check`, `ruff format --check`) | DONE | vert, 1347 fichiers |
| Typecheck backend (`mypy src tests`) | DONE | 0 erreur, 1289 fichiers |
| Unitaires backend (`--ignore=tests/integration --ignore=tests/workspace_agent -m "not integration"`) | DONE après correction | 1ʳᵉ exécution (@595662e0) : 8 429 passed, 5 skipped, 2 failed (B13 : conflit VT-FAKE/E2E-ABC ; oracle D05 de la matrice supprimé par ID-3) → **corrigés** (@9b52d0fb) ; les 2 tests + `test_bootstrap_envelope` rejoués : 42 passed. **La suite unitaire complète n'a pas été relancée en entier après ces 2 corrections de tests.** |
| Sidecar `tests/workspace_agent` (bwrap/netns réels) | DONE | 712 passed (150 s) |
| Intégration PG complète `tests/integration` | DONE | 799 passed (12 min 56 s) |
| Scénario d'acceptation PG (24 étapes + A/B/C) | DONE | 5 passed (~80 s, Typst réel) |
| Frontend : lint, typecheck, `pnpm test --run`, `pnpm build` | DONE | 828 tests / 117 fichiers, build ok |
| Playwright par défaut (`pnpm exec playwright test`) | DONE | 33 passed (interceptés) |
| `make test-editorial-browser` (régression M23) | DONE | 8 passed (`CHOKIDAR_USEPOLLING=1`) |
| `make test-investigation-browser` | DONE | 34 + 1 passed |
| `make test-workflow-full` | **UNKNOWN / non exécuté** | couvert pour l'essentiel par l'intégration PG complète ; non lancé |
| CI GitHub, shellcheck | **UNKNOWN** | non rejoués ; `shellcheck` absent de cet hôte ; les tests de confinement sont *skipped* sur un runner sans userns |

Flakes observés sous charge, passant seuls puis en suite : `test_claude_resume_roundtrip_through_the_supervisor_when_proven`, `test_revoke_cancels_the_in_flight_backend_request`, `test_sync_resyncs_after_a_generation_change` (vu une fois par H0). Non expliqués au-delà de la charge machine : **UNKNOWN**.

## 13. Les six revues indépendantes (toutes DeepSeek Flash max, pas Luna)
| Revue | Verdict | Findings et disposition |
|---|---|---|
| R1 architecture | GO-avec-réserves | F1 (task_context → 422), F3, F6, F7 corrigés (R1FIX-A) ; F4/F5 (R3FIX) ; F8 (R1FIX-B) ; F2/F9 docs (DOC-1) ; F10 info |
| R2 réseau/confinement (+R2b delta H0) | GO-avec-réserves ×2 | R2B-1/2/3 corrigés (R2BFIX) ; **R2B-4 accepté** (bind `/etc/resolv.conf`+`/etc/hosts` fuit un domaine tailnet) ; R2B-5 (Write/Edit confinés par le CLI seulement) **NON PROUVÉ** ; R2B-6/7 info |
| R3 provenance | GO-avec-réserves | F1 MAJEUR (article_element non publiable) corrigé ; F2/F3/F4 corrigés ; F5–F8 info |
| R4 concurrence | **NO-GO → NO-GO (R4b) → GO-avec-réserves (R4c)** | F1–F6, F9, R4B-F1/F2, R4-F2, F12 corrigés et re-revus ; R4C-F1 (famine réconciliation) corrigé par R4FIX3 — **non re-revu par un agent frais** (rejoué par moi : 125 unit + 18 PG) ; R4-F7/F8 (I/O hôte dans une transaction) documentés, non corrigés |
| R5 UX | GO-avec-réserves | F1–F5 + mineurs corrigés (R5FIX), mesures Chromium avant/après ; captures régénérées ; non re-revue |
| R6 livrable éditorial | GO-avec-réserves | F1 (aperçu V9/V10), F2 (table IOC) corrigés (R6FIX, PDF réel) ; F3 conservé ; non re-revue |
Les corrections R2b/R3/R5/R6 n'ont **pas** été re-revues par un agent frais ; elles sont vérifiées par les tests rejoués ci-dessus.

## 14. Capacités démontrées en factice vs attestées en réel
| Capacité | Factice (navigateur/PG réels) | CLI réel (H0) | Fournisseur réel CTI |
|---|---|---|---|
| Workspace, plan, tâches, passations, instructions livrées, transfert, notes, blocs, PDF | DONE | — | — |
| Session Codex / Claude, egress, relais MCP, interruption, nettoyage | DONE (fake CLI) | ATTESTÉ (voir §7) | — |
| VT (seeds, IP/domaines, quota, révocation) | DONE (VT factice) | — | **UNKNOWN (jamais exercé)** |
| Bridge (plan, avis, rédaction) | DONE (auto-répondeur) | — | **UNKNOWN** |

## 15. Ce qui n'est PAS ré-attesté en réel
Après le spike H0, ont changé sans nouvelle session réelle : la purge des credentials de profil (prouvée sur des copies **synthétiques** aux chemins que les adaptateurs écrivent), le filtre `collect` des credentials (mapping de clés INFÉRÉ des formats documentés), la re-purge à `_finalize_exit`, le refus (au lieu de la troncature) d'un bloc d'instruction trop grand. Un CLI qui écrirait son credential ailleurs (keychain, autre fichier) ne serait ni purgé ni détecté. Une ré-attestation d'une session par CLI (un prompt) est nécessaire — **autorisation à redonner**.

## 16. Migrations
Chaîne linéaire additive `0027_instruction_delivery`, `0028_task_context_consultations`, `0029_note_anchor_identity` (pins de base + `content_blocks jsonb`). Politique : base repartie de zéro, donc migrations simples, downgrades refusés admis. `0001_baseline` et 0019–0026 non modifiées. Test `test_task_context_preflight_accepts_an_already_conforming_shape` repositionné sur sa propre révision.

## 17. Documentation
DOC-1 : SESSION-RUNNER, WORKSPACE-AGENT, ADR-0011 (addendum D7), COCKPIT-F4, GLOSSARY, README, BROKER-RELAY-EGRESS alignés sur le code livré. Nouveaux : `docs/operations/host-cli-spike-2026-11.md`, `docs/operations/m24-1-vt-fake-capabilities.md`.

## 18. Risques résiduels
- Famine résiduelle de la population *active* du lot de livraison (sans rotation) — bornée par l'expiration des baux.
- R4-F7/F8 : I/O hôte dans une transaction DB ouverte ; écriture workspace sous verrou d'enquête.
- Rejeu idempotent d'une consultation servi sans revalidation de divulgation (la ligne est déjà lisible) ; une clé enregistrée avant M24.1 rejouée après déploiement renverrait 409.
- Surface D7 `collect/reconcile` élargie (HMAC vérifié dans les handlers, sans consommateur du relais).
- Un seul `# type: ignore[misc]` justifié dans un test sidecar.
- `canonical_code` : l'humain fournit offset/longueur/SHA (aucune route front de lecture des octets) ; le serveur reste vérificateur.
- Mock Playwright intercepté ≠ preuve ; périmètre navigateur réel limité à un seul sujet seedé.

## 19. NO-GO immédiats de la mission — état
Aucun n'est déclenché au HEAD final d'après les revues R2b/R4c et les gates : accès libre LAN/internes (netns vérifié), broker inaccessible (attesté en réel), contournement du confinement des scripts (batterie 11/11), processus persistants après interruption (vérifié), secrets exposés (purge+filtre, avec la réserve de la §15), tâche modifiée avec bail obsolète (revalidation `database_now`), instruction annoncée traitée mais non livrée (R4-F1 corrigé et re-revu), finding promu sans vérification, prose non-Bridge acceptée, Notes mal ancrées, PDF non conforme (R6), CI rouge sans disposition (**la CI GitHub n'a pas été rejouée : UNKNOWN**), revue critique manquante présentée comme faite (aucune).

## 20. Écarts et honnêteté
(1) Plusieurs rapports d'agents ont déclaré « 15 erreurs mypy préexistantes » : faux, elles venaient de tests M24.1 (corrigées). (2) Le spike H0 a dépassé « un prompt par CLI » (§7). (3) Tous les verdicts de revue viennent de DeepSeek Flash max, pas de Luna. (4) La suite unitaire complète n'a pas été relancée après les 2 dernières corrections de tests (§12).

## 21. Autorisations / décisions humaines encore nécessaires
Ré-attestation en réel après les correctifs (un prompt par CLI) ; politique/activation de `M24_AGENT_SESSION_KEY` ; tout appel VT/Bridge/Shodan réel ; décision sur R2B-4 (bind `/etc/resolv.conf`) ; CI GitHub et shellcheck ; push/merge dans `main` ; déploiement/redémarrage de la stack et DDL (0027–0029) sur base vivante ; format `delivery_code` ; rétention des blobs/journaux.

## 22. Fichiers de preuve
`.autowork-runs/m24-1/{REGISTRY,R1-REVIEW,R2-F1-REVIEW,R2B-REVIEW,R3-REVIEW,R4-REVIEW,R4B-REVIEW,R4C-REVIEW,R5-REVIEW,R6-REVIEW}.md` (non versionnés), `docs/m24/captures-m24-1/*.png`, `docs/operations/host-cli-spike-2026-11.md`.

## 23. Recettes de rejeu
`cd backend && uv sync --locked --python /usr/bin/python3.12` ; Typst : `TYPST_BINARY=$HOME/.local/share/autowork-tools/typst PATH=$HOME/.local/share/autowork-tools/bin:$PATH FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst` ; sidecar sans `TMPDIR` (AF_UNIX 108 octets), un pytest à la fois ; PG de test : `docker start cti-bulletin-postgres-test-1` puis `flock /tmp/m24-pg.lock env TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres …` ; parcours réel : `make test-investigation-browser` (réécrit les captures).

## 24. Registre des lots
Voir `.autowork-runs/m24-1/REGISTRY.md` (tâche, branche, agent, statut, SHA, preuves rejouées) : F0-A/B/C, F1 (RLY/EGR/NET/ADP/LIFE), R2 + R2FIX-A/B, F2-A/B/C/D, F3 (ID-1…7), WIRE-1, B1–B7 (+B5c), F1–F7b (frontend), H0, VT-FAKE, E2E-ABC, DOC-1, R1FIX-A/B, R2BFIX, R3FIX/R3FIX-B, R4FIX/2/3, R5FIX, R6FIX, TYPEFIX.

## 25. Verdict
**PARTIAL.** Intégration locale : GO. Exploitation réelle : NO-GO jusqu'aux autorisations de la §21 et à une ré-attestation CLI après correctifs. Rien n'est poussé, fusionné ni déployé.
