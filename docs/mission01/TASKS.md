# Mission 01 V3.2 — registre des tâches

Base de départ : `main@058bb4af2278e3941956e523c5a1d560208de228`
(2026-10-23). Toute branche de lot part de ce SHA. Aucun push, merge sur
`main`, déploiement ni accès live. Commits locaux de branche autorisés.

Sources factuelles Mission 00 (lecture seule, gitignorées, ne pas committer) :
`/home/nill/perso/work/AutoWork/docs/references/mission00-2026-10-23/`.
Elles ne sont pas des preuves CTI canoniques et ne doivent pas être copiées
verbatim dans les livrables.

## DAG

```text
S0 ──┬── B0 ── B1 ── L ── A ── Q ── FINAL
     └── T ────────────────────────┘
```

`T` est parallèle dès S0. `B1` peut démarrer son volet ledger/budget sur
fixtures synthétiques sans attendre la fin de B0 ; l'intégration finale exige
B0, B1, L, A, Q, T.

## Rôles

- Parent : orchestration, contrats, intégration locale, vérification des
  oracles, registre. Ne rédige pas les gros audits lui-même.
- Workers : GPT-6 Luna `xhigh` par défaut ; `high` seulement pour les tâches
  mécaniques bornées. Un worktree par writer. Reviewer distinct de
  l'implémenteur pour les revues critiques.
- Aucun worker ne crée d'enfants, ne pousse, ne merge, ne touche au live, ne
  reçoit de secret ni de corps CTI privé.

## Registre

| TASK_ID | Dep. | Question / livrable | Propriétaire | Base SHA | Fichiers cibles | Oracle | Droits | Résultat SHA | Statut | Coût |
|---|---|---|---|---|---|---|---|---|---|---|
| M01-S0 | — | BASELINE.md : SHA local/runtime, écarts, skills/profils, matrice corpus, contradictions doc prouvées | Luna Audit xhigh | 058bb4a | `docs/mission01/BASELINE.md` | faits référencés `paths:lines` ; liens docs valides ; statut PASS/FAIL/BLOCKED/UNKNOWN | worktree lecture + 1 fichier | `e26c9a7` + `0074706`, intégré `48ba143` | PASS (runtime UNKNOWN) | UNKNOWN |
| M01-B0A | S0 | Chaîne de complétude : cartographie B0.1, contrat par question B0.2, suppression plafond renderer 96k B0.3, préflight preuves B0.5, oracles B0.8 (1–4, 7) | Luna Implementer xhigh | 058bb4a | `backend/src/cti_app/application/{bridge_investigator,advisor_dossier,advisor_wire,investigation_advisor_tools,research_context_builder}.py`, tests ciblés | tests offline : preuve manquante/récupérée, contradiction multi-claim, statuts cohérents, N−1/N/N+1, >200k simulé, hexdump borné, reparse | worktree write, tests offline | `9774680` → `cd69eca` + format `498c2da`, intégré `f67181e` | F1–F5 acceptés (153 tests) ; **F6 FAIL/PARTIAL arrêté après 3 tentatives** : URL dans titre, percent sans `;base64`, base64 fragmenté documentés | UNKNOWN |
| M01-B0A-REVIEW | B0A | Revue critique indépendante de B0A : omissions silencieuses, manifeste, contradictions, pas de Bridge réimplémenté, tests, complexité | Luna Audit xhigh | 058bb4a | `docs/mission01/B0A_REVIEW.md` | constats + verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `6411e1e`, intégré `a9c5a22` | DONE : FAIL, 2 HIGH + 4 MEDIUM | UNKNOWN |
| M01-B0A-R1 | B0A-REVIEW | Réparation B0A : 24 échecs, multi-claim, statuts MISSING/RETRIEVABLE, sélection lexicale, troncature IOC, hexdump | Luna Implementer xhigh | `9774680` | `advisor_dossier.py`, `bridge_investigator.py`, `investigation_advisor_tools.py`, tests | suite trois fichiers verte ; 116 passed ; zéro omission silencieuse | worktree write | `fbc0f24`, intégré `9761998` | DONE, revue R2 FAIL F6 | UNKNOWN |
| M01-B0A-REVIEW-R2 | B0A-R1 | Re-revue B0A après réparation | Luna Audit xhigh | `fbc0f24` | `docs/mission01/B0A_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `09f1c38`, intégré `3da31ec` | DONE : FAIL, F1–F4 OK, F6 HIGH, F5 UNKNOWN | UNKNOWN |
| M01-B0A-R2 | B0A-REVIEW-R2 | Réparation F6 (hex legacy + content générique) ; F5 compte IOC différé | Luna Implementer xhigh | `fbc0f24` | `investigation_advisor_tools.py`, `advisor_dossier.py`, tests | valeur complète absente du prompt ; mesure courte préservée | worktree write | `c561f04`, intégré `690a813` | DONE F6a ; F5 UNKNOWN documenté | UNKNOWN |
| M01-B0A-REVIEW-R3 | B0A-R2 | Vérification finale F6 et acceptation F5 UNKNOWN | Luna Audit xhigh | `c561f04` | `docs/mission01/B0A_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL | lecture seule | `a00d2b8`, intégré `710e861` | DONE : FAIL, F6b/F6c HIGH | UNKNOWN |
| M01-B0A-R3 | B0A-REVIEW-R3 | Dernière tentative F6 : budget cumulatif + détection base64 générique | Luna Implementer xhigh | `c561f04` | `advisor_dossier.py`, `investigation_advisor_tools.py`, tests | listes neutres et base64 bornées ; zéro transport ; mesures courtes préservées | worktree write | `df1bd8f`, intégré `66be3f8` | DONE (141 tests), revue fallback Claude en cours | UNKNOWN |
| M01-F6 | B0A | Lot dédié : liste blanche stricte des champs d'évidence (titres `strings[].name`, data URLs/percent/double encodage, base64 fragmenté), budget cumulatif après décodage | Luna Implementer xhigh | `78b2b1d` | `advisor_dossier.py`, `investigation_advisor_tools.py`, éventuel `evidence_field_policy.py`, tests advisor/F6 | valeur complète absente du dossier/prompt, omission typée, zéro transport factice, mesures courtes préservées ; max 3 tentatives sinon BLOCKED avec preuve | worktree write | `83aa615` (branche `feat/mission01-f6-evidence-allowlist`, **non intégrée**) | **BLOCKED/PARTIAL après 3 tentatives** : la revue indépendante `2e22ca1b` trouve 3 HIGH (chemin `dns.raw`, base64 avec espaces, budget non cumulatif) + 2 MEDIUM ; arrêt sans 4e patch à l'aveugle, preuves dans `F6_REVIEW.md` | UNKNOWN |
| M01-F6-REVIEW | F6 | Revue critique F6 : allowlist fermée, 3 contournements, normalisation/decoding, faux positifs, zéro transport, régressions | Luna Audit xhigh | `83aa615` | `docs/mission01/F6_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués + entrées synthétiques | lecture seule | agent `2e22ca1b`, rapport intégré | DONE : **FAIL**, 3 HIGH + 2 MEDIUM ; 162 passed rejoués ; F6 arrêté BLOCKED | UNKNOWN |
| M01-B0B | B0A | Observabilité corrélée B0.4/B0.6 et exports d'audit B0.7 : RUN_TRACE.md, RUN_EVENTS.jsonl.gz, RUN_MANIFEST.json, index | Luna Implementer xhigh | `9774680` | nouveaux modules projection/export + CLI, tests | reconstruction déterministe sur ≥778 invocations synthétiques ; zéro fuite ; reprise | worktree write | `41c8869` + `5add5df`, intégré `a772cc9` | PASS_WITH_CHANGES : F1–F4 fermés, texte corrigé, 14 tests ; PostgreSQL réel UNKNOWN | UNKNOWN |
| M01-B0B-REVIEW | B0B | Revue critique B0B : corrélations, UNKNOWN honnêtes, exports, reprise, fuites, complexité | Luna Audit xhigh | `289903b` | `docs/mission01/B0B_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `63577f7`, intégré `7c7971c` | DONE : FAIL, F1/F2 HIGH | UNKNOWN |
| M01-B0B-R1 | B0B-REVIEW | Réparation : métadonnées advisor allowlistées, ledger décisions, input pack, libellé non-observation, oracles | Luna Implementer xhigh | `289903b` | `investigation_audit_export.py`, `investigation_audit_reader.py`, tests | métadonnées persistées projetées ; pas de texte/corps ; décisions typées ; input pack lié | worktree write | `41c8869`, intégré `d30cced` | DONE (14 tests), re-revue PASS_WITH_CHANGES | UNKNOWN |
| M01-B0B-REVIEW-R2 | B0B-R1 | Re-revue B0B après réparation | Luna Audit xhigh | `41c8869` | `docs/mission01/B0B_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL | lecture seule | `230380d`, intégré `d92f6d6` | PASS_WITH_CHANGES : texte `RUN_TRACE.md` à corriger | UNKNOWN |
| M01-B0B-R2 | B0B-REVIEW-R2 | Corriger le texte statique `RUN_TRACE.md` contredisant les métadonnées exportées | Luna Implementer xhigh | `41c8869` | `investigation_audit_export.py`, tests | texte cohérent ; 14 tests | worktree write | `5add5df`, intégré `a772cc9` | DONE, 14 tests vérifiés parent | UNKNOWN |
| M01-B1-SCOUT | S0 | Carte admission/budget : chemin réel, source budget, incident 266/3600, durées distinctes, fixtures a–e | Luna Audit xhigh | 058bb4a | `docs/mission01/B1_ADMISSION_MAP.md` | paths:lines ; faits vs inférences vs UNKNOWN | lecture seule | `ad4da76`, intégré `464836a` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-B1A | — | SUPERSEDED par la revue `7a40943` : scindé en B1A1 (durabilité/unicité) et B1A2 (politique de coût bornée) | — | 058bb4a | — | — | — | — | SUPERSEDED | — |
| M01-B1B | B1A | Reprise/temporalité : réponse tardive, 429/Retry-After, timeout, pacer Bridge, needs_review, crash-reprise, statut inconnu | Luna Implementer xhigh | `6b19512` | `bridge_investigator.py` (pacer/Retry-After), `model_conversations.py`, `model_gateway.py`, `investigation_advisor_tools.py` si deadline, `tests/test_b1b_temporal_recovery.py` | aucun double appel externe en simulation ; états explicites ; `production_pacing.py` n'est PAS le pacer Bridge ; suites B1A1/B1A2 vertes | worktree write | `e4850d2` → `fd397de` → `4604ac1` → `a94986f`, intégré `10b0e35` | **PASS** après R4 (aucun constat) ; oracle 241 passed et 294 passed/6 skipped rejoués sur l'intégration ; résiduels `turn` sans deadline (≤300 s), PostgreSQL concurrent et transport réel `UNKNOWN` | UNKNOWN |
| M01-B1B-REVIEW | B1B | Revue critique indépendante B1B : deadline/pacer, 429, clé de retry, réponse tardive, crash-reprise, qualité des tests | Luna Audit xhigh | `e4850d2` | `docs/mission01/B1B_REVIEW.md` | verdict FAIL/PASS + tests rejoués + scénarios d'échec concrets | lecture seule | agent `b1a2f600`, rapport intégré `B1B_REVIEW.md` | DONE : **FAIL**, 1 BLOCKER + 1 HIGH + 1 MEDIUM + 1 LOW ; 170 passed rejoués | UNKNOWN |
| M01-B1B-R1 | B1B-REVIEW | Réparation : verrou pacer compatible deadline, Retry-After plafonné/HTTP-date, clé de retry explicitée + non-retry ambigu, wrapper `_PermissionAware` avec `timeout_seconds`, StrEnum états, sleeps réduits | Luna Implementer xhigh | `e4850d2` | `bridge_investigator.py`, `investigation_composition.py`, `domain/model_runs.py`, tests B1B/Bridge/composition/adapters | oracle élargi tout vert, zéro test supprimé ; contention pacer et ambiguïté 429 couvertes ; wrapper réellement testé | worktree write | `fd397de` (branche `feat/mission01-b1b-temporal-recovery`) | DONE (236 passed oracle, 289 passed/6 skipped élargi) ; revue R2 `66c9488e` en cours | UNKNOWN |
| M01-B1B-REVIEW-R2 | B1B-R1 | Seconde revue indépendante : fermeture réelle des 1 BLOCKER/1 HIGH/1 MEDIUM/1 LOW, nouveaux défauts du patch, tests | Luna Audit xhigh | `fd397de` | rapport intégré dans `B1B_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués + scénarios | lecture seule | agent `66c9488e` | DONE : **PASS_WITH_CHANGES** ; 236 passed + 289/6 rejoués ; 1 MEDIUM cooldown partagé Retry-After | UNKNOWN |
| M01-B1B-R2 | B1B-REVIEW-R2 | Séparer plafond de retry local et plafond de cooldown partagé ; appliquer le cooldown avant tout retour ; tests Retry-After 5/10000/inéligible/budget insuffisant/backoff | Luna Implementer xhigh | `fd397de` | `bridge_investigator.py`, `tests/test_b1b_temporal_recovery.py`, `tests/test_bridge_investigator.py` | cooldown ≥ Retry-After sous le plafond partagé, plafond 300 s, aucun retry inéligible, zéro sleep >50 ms | worktree write | `4604ac1` (branche `feat/mission01-b1b-temporal-recovery`) | DONE (239 passed oracle, 292 passed/6 skipped élargi) ; revue ciblée R3 en cours | UNKNOWN |
| M01-B1B-REVIEW-R3 | B1B-R2 | Revue ciblée du cooldown partagé et du chemin `BridgeInvestigator.turn` restant | Luna Audit xhigh | `4604ac1` | rapport intégré dans `B1B_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués + scénario | lecture seule | agent `e8bd922a` | DONE : **PASS_WITH_CHANGES** ; advisor fermé ; 1 MEDIUM `turn` (Retry-After brut 10 000 s) + 1 LOW assertion pacer ; 239/292 tests rejoués | UNKNOWN |
| M01-B1B-R3 | B1B-REVIEW-R3 | Dernière réparation (3/3) : plafonner le cooldown partagé dans `turn`, ne pas retenter au-delà du plafond, assertion pacer sur 429 ambigu | Luna Implementer xhigh | `4604ac1` | `bridge_investigator.py`, `tests/test_b1b_temporal_recovery.py` | `Retry-After=10000` → `blocked_until ≤ now+300`, zéro second envoi ; `Retry-After=5` → cooldown 5 s + retry unique ; oracle vert | worktree write | `a94986f` (branche `feat/mission01-b1b-temporal-recovery`) | DONE (241 passed oracle, 294 passed/6 skipped élargi) ; revue finale R4 en cours | UNKNOWN |
| M01-B1B-REVIEW-R4 | B1B-R3 | Vérification finale ciblée du delta `turn`/pacer et du LOW R3 | Luna Audit xhigh | `a94986f` | rapport intégré dans `B1B_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | agent `772a15d5` | DONE : **PASS**, aucun constat ; 241/294 tests rejoués | UNKNOWN |
| M01-L-SCOUT | S0 | 42 limites : consommateur, décision motivée, plan de tests | Luna Audit xhigh | 058bb4a | `docs/mission01/LIMITS_DECISIONS.md` | paths:lines ; classification corrigée si preuve contraire | lecture seule | `7494406`, intégré `50eeb1c` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-L1 | S0 | L06 variables sans consommateur ; P10 relèvement ABANDONNÉ et documenté DEFER | Luna Implementer xhigh | 058bb4a | `compose.yaml`, `.env.example`, playbook, `test_limits_mission01.py` | diff base = 4 fichiers L06 ; 2 + 41 tests verts ; G2 intact | worktree write | `e4772fa`, intégré `0483f61` | PASS_WITH_CHANGES (L06) ; P10 DEFER préexistant | UNKNOWN |
| M01-L1-REVIEW | L1 | Revue critique indépendante de L1 (borne P10, retrait variables, G2) | Luna Audit xhigh | 058bb4a | `docs/mission01/L1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | `49b4de6` + `b541b75`, intégré `0d72923` | DONE : FAIL R2, 1 HIGH (politique sources P10) | UNKNOWN |
| M01-L1-R1 | L1-REVIEW | Réparation : override dev-only, politique P10 agrégée (TLP RED/do_not_submit), parse strict, tests frontières | Luna Implementer xhigh | `3b43c9f` | `compose.yaml`, `.env.example`, `invariant_proposals.py`, tests | refus avant transport si politique bloquée ; défaut 96k sans drapeau dev ; max exact/max+1 | worktree write | `0194648` | DONE, re-revue FAIL | UNKNOWN |
| M01-L1-R2 | L1-REVIEW-2 | Inclure la politique des SourceDocuments des claims dans la garde P10 ; fail-closed source introuvable ; tests zéro transport | Luna Implementer xhigh | `0194648` | `invariant_proposals.py`, tests | source RED/do_not_submit/interdite → zéro routage/transport ; sources vertes → passe | worktree write | `a3d17f8` | DONE, re-revue R3 FAIL (2 HIGH) | UNKNOWN |
| M01-L1-R3 | L1-REVIEW-3 | Dernière réparation : provenance des invariants existants + rejeu de snapshot historique ; tests fail-closed | Luna Implementer xhigh | `a3d17f8` | `invariant_proposals.py`, tests | invariant/claim restrictive → zéro transport ; rejeu snapshot devenu restrictif → refus | worktree write | `8d7c969` | FAIL re-revue R4 (2 HIGH) → P10 ABANDONNÉ | UNKNOWN |
| M01-L1-FALLBACK | L1-REVIEW-4 | Repli : restauration P10 à la base, conservation L06 seul, P10 classé DEFER préexistant | Luna Implementer xhigh | `8d7c969` | `compose.yaml`, `.env.example`, playbook, test L06 | diff base = 4 fichiers L06 ; aucun code P10 ; 43 tests | worktree write | `e4772fa` | DONE, validation finale en cours | UNKNOWN |
| M01-L2 | L1 | Pagination/keyset des sections de contexte : pages CAS, manifeste d'omissions typées, mémoire bornée à la source, protections et preuves récupérables | Luna Implementer xhigh | `6b19512` | `research_context_builder.py`, `operator_context_summary.py`, `investigation_persistence.py`, `infrastructure/database/repositories/investigation.py`, `operator_tool_invocation.py`, `investigation_contracts.py`, `context_pagination.py`, tests L2 | ordre keyset stable, bornes N−1/N/N+1, SHA CAS et falsification, sélectionnés+omis=total, aucun item sensible, appelants verts ; adaptateur SQL réel `UNKNOWN` si non exécutable offline | worktree write | `ef1bc61` → `4f2a941` → `6143099` → `0c54ed9`, intégré `36eb5326` | **PASS_WITH_CHANGES** : 3 HIGH/3 MEDIUM R1 fermés, 2 MEDIUM R2 fermés ; R4 PASS_WITH_CHANGES avec 1 MEDIUM résiduel documenté (catégorie d'omission reprise comme attendue ; limite de 3 réparations atteinte) ; 342 passed, **556 passed combiné B1B+L2** ; PostgreSQL réel `UNKNOWN` | UNKNOWN |
| M01-L2-REVIEW | L2 | Revue critique L2 : keyset SQL, curseur/CAS, manifeste, mémoire bornée, protections, compatibilité offset, complexité | Luna Audit xhigh | `ef1bc61` | `docs/mission01/L2_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués + scénarios concrets | lecture seule | agent `fd227a85`, rapport intégré `L2_REVIEW.md` | DONE : **FAIL**, 3 HIGH (curseur forgeable, CAS incomplet, hash avant rédaction) + 3 MEDIUM ; 322 passed rejoués | UNKNOWN |
| M01-L2-R1 | L2-REVIEW | Réparation : CAS complet après rédaction, curseur position non authentifiée, omissions PAGINATION, preuve mémoire, fallback dépôts, compatibilité `section_cursors`, tests SQL asc/desc | Luna Implementer xhigh | `ef1bc61` | `context_pagination.py`, `research_context_builder.py`, `operator_tool_invocation.py`, `operator_context_summary.py`, `investigation_contracts.py`, `investigation_persistence.py`, tests L2 | falsification de chaque champ CAS, SHA rédigé, raison PAGINATION, spy mémoire, ancien payload v1, prédicat asc/desc | worktree write | `4f2a941` (branche `feat/mission01-l2-context-pagination`) | DONE (189 oracle worker, **325 passed parent**) ; revue R2 `d676bd6a` en cours ; PostgreSQL réel `UNKNOWN` | UNKNOWN |
| M01-L2-REVIEW-R2 | L2-R1 | Seconde revue indépendante : fermeture des 3 HIGH/3 MEDIUM, nouveaux défauts, compatibilité, mémoire, tests | Luna Audit xhigh | `4f2a941` | rapport intégré dans `L2_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués + scénarios | lecture seule | agent `d676bd6a` | DONE : **PASS_WITH_CHANGES** ; 3 HIGH/3 MEDIUM R1 fermés ; 2 MEDIUM résiduels (manifeste `source_ref`/`page_sha256` non liés ; dépôts mémoire `source_bounded=True` à tort) ; 325 passed rejoués | UNKNOWN |
| M01-L2-R2 | L2-REVIEW-R2 | Réparation ciblée : `verify_manifest_entry` liant source_ref/page_sha256, falsification testée ; `source_bounded=False` honnête pour les dépôts mémoire ; test de parsing ancien payload page | Luna Implementer xhigh | `4f2a941` | `context_pagination.py`, `investigation_persistence.py`, `operator_tool_invocation.py`, tests L2/builder | falsifier source_ref/page_sha256 → False ; mémoire False+raison ; faux dépôt borné True ; ancien payload parse | worktree write | `6143099` (branche `feat/mission01-l2-context-pagination`) | DONE (326 passed parent) ; revue finale R3 `45b350c4` en cours | UNKNOWN |
| M01-L2-REVIEW-R3 | L2-R2 | Vérification finale ciblée du manifeste, de la mémoire honnête et du parsing ancien payload | Luna Audit xhigh | `6143099` | rapport intégré dans `L2_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | agent `45b350c4` | DONE : **PASS_WITH_CHANGES** ; 2 MEDIUM R2 fermés ; 1 MEDIUM résiduel (constructeurs sans vérification, `source_ref` comparé à lui-même) ; 326 passed rejoués | UNKNOWN |
| M01-L2-R3 | L2-REVIEW-R3 | Dernière réparation (3/3) : `assert_manifest_consistency` recalculant page SHA/source_ref aux trois constructeurs + tests de refus | Luna Implementer xhigh | `6143099` | `context_pagination.py`, `research_context_builder.py`, `operator_tool_invocation.py`, tests L2/builder | manifeste falsifié refusé aux 3 constructeurs, manifeste cohérent accepté, oracle vert | worktree write | `0c54ed9` (branche `feat/mission01-l2-context-pagination`) | DONE (342 passed parent) ; revue de clôture R4 `24b810f2` en cours | UNKNOWN |
| M01-L2-REVIEW-R4 | L2-R3 | Revue de clôture : vérificateur recalculé, quatre constructeurs, tests de refus, régressions | Luna Audit xhigh | `0c54ed9` | rapport intégré dans `L2_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | agent `24b810f2` | DONE : **PASS_WITH_CHANGES** ; 342 passed rejoués ; 1 MEDIUM résiduel (tautologie de catégorie d'omission) ; intégré avec résidu documenté | UNKNOWN |
| M01-A-SCOUT | S0 | Primitive CTI existantes, matrice hypothèses/preuves, 5 scénarios, trous réels | Luna Audit xhigh | 058bb4a | `docs/mission01/CTI_STRATEGY_MAP.md` | paths:lines ; pas de nouveau framework | lecture seule | `54750b1`, intégré `f8f9575` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-B1-REVIEW | B1-SCOUT | Revue critique indépendante de la carte admission/budget avant B1A | Luna Audit xhigh | 058bb4a | `docs/mission01/B1_ADMISSION_REVIEW.md` | constats CONFIRMED/PLAUSIBLE/REJECTED + verdict ADEQUATE* | lecture seule | `94d6706`, intégré `7a40943` | DONE : ADEQUATE_WITH_CHANGES, 1 BLOCKER + 4 HIGH | UNKNOWN |
| M01-B1A1 | B1-REVIEW | Durabilité admission : coût calculé une fois, réserve orpheline crash, double appel nouvelle clé, CAS/révocation, oracles offline | Luna Implementer xhigh | 058bb4a | `investigation_service.py`, `investigation_tools.py`, `operator_tool_invocation.py`, nouveau test | zéro transport avant réconciliation ; réserves demandée/persistée/consommée/libérée distinctes ; CAS stale et révocation bloquent | worktree write | `3a745de`, intégré `fe68c85` | PARTIAL : portes fermées, 121 tests ; atomicité révocation/dispatch BLOCKED ; revue en cours | UNKNOWN |
| M01-B1A1-REVIEW | B1A1 | Revue critique B1A1 : coût/clé, crash, nouvelle clé, preuve pre_submission, CAS/révocation, régressions | Luna Audit xhigh | 058bb4a | `docs/mission01/B1A1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL ; 121 tests rejoués | lecture seule | `5fb65f5`, intégré `0eee6a7` | DONE : FAIL, 1 BLOCKER + 2 HIGH | UNKNOWN |
| M01-B1A1-R1 | B1A1-REVIEW | Réparation : preuve pre_submission non forgeable, enums typés, empreinte sémantique, test extension G2 réelle | Luna Implementer xhigh | `3a745de` | `investigation_service.py`, `operator_tool_invocation.py`, `investigation_tools.py`, `domain/investigation.py`, tests | marqueur adaptateur ne libère plus ; variantes de question bloquées ; extension via service | worktree write | `d1580a9`, intégré `3c954a3` | DONE/BLOCKED atomicité (124 tests), re-revue en cours | UNKNOWN |
| M01-B1A1-REVIEW-R2 | B1A1-R1 | Re-revue B1A1 : preuve typée, empreinte, extension G2, atomicité BLOCKED | Luna Audit xhigh | `d1580a9` | `docs/mission01/B1A1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL | lecture seule | `7af9e98`, intégré `91928ef` | DONE : FAIL, HIGH canonicalisation (3 contournements) | UNKNOWN |
| M01-B1A1-R2 | B1A1-REVIEW-R2 | Canonicalisation hash/UUID/défauts review_text ; tests négatifs preuve ; dettes documentées | Claude Opus 5.5 xhigh (fallback quota Luna) | `d1580a9` | `operator_tool_invocation.py`, tests | 3 contournements bloqués ; preuve croisée/partielle/SHA invalide refusée | worktree write | — | RUNNING (fallback) | UNKNOWN |
| M01-B1A2 | B0A + B1A1 | Borne dure `tool_seconds` ≤ capacité G2, propagée au transport ; pas d'inférence durée historique ; timeout Research 7200 confronté à la réserve | Luna Implementer xhigh | `fe68c85` | `investigation_advisor_tools.py`, `model_gateway.py`, `bridge_investigator.py`, tests fake clock | transport reçoit la borne ; réponse tardive ambiguë ; consommation ≤ réserve ; refus si capacité insuffisante | worktree write | `458b96c`, fusion `02999f8` | DONE (153 pass/23 fail base), conflit croisé B0A-R1 → R1 | UNKNOWN |
| M01-B1A2-R1 | B1A2 | Rebaser sur intégration + corriger classification `rate_limited` vs `unavailable` sans affaiblir le test | Luna Implementer xhigh | `02999f8` | `investigation_advisor_tools.py`, `bridge_investigator.py`, tests | suite croisée verte ; aucun échec introduit | worktree write | `3c2ff0e`, intégré `723b761` | DONE (133 tests croisés) | UNKNOWN |
| M01-B1A2-REVIEW | B1A2 | Revue critique B1A2 : borne G2, propagation transport, consommation, classification, tests | Luna Audit xhigh | `3c2ff0e` | `docs/mission01/B1A2_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL | lecture seule | `2ca858e`, intégré `7252d55` | DONE : FAIL, pacer hors borne + seuil 60 s | UNKNOWN |
| M01-B1A2-R2 | B1A2-REVIEW | Réparation : inclure le pacing dans la deadline, dériver le seuil minimal, compteur transport | Luna Implementer xhigh | `3c2ff0e` | `investigation_advisor_tools.py`, `bridge_investigator.py`, tests | pacer + transport ≤ borne ; refus si dépassement ; un seul envoi | worktree write | `01346e2`, intégré `91db0f3` | DONE (138 tests), conflit croisé `pre_submission_reason` en correction fallback | UNKNOWN |
| M01-B1A2-R2-FIX | B1A2-R2 | Fusion intégration + corriger le test `pre_submission_reason` après preuve typée B1A1-R1 | Claude Opus 5.5 xhigh (fallback quota Luna) | `91db0f3` | `tests/test_b1_g2_bounded_timeout.py`, code si défaut | suite croisée verte ; aucun marqueur libre réintroduit | worktree write | — | RUNNING (fallback) | UNKNOWN |
| M01-B1A2-REVIEW-FINAL | B1A2-R2-FIX | Revue finale B1A2 après correction croisée | Claude Opus 5.5 xhigh (fallback quota Luna) | SHA final | `docs/mission01/B1A2_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL | lecture seule | — | PENDING | UNKNOWN |
| M01-L | B0A (L1) | 42 restrictions : décisions motivées, suppression des plafonds arbitraires démontrés (priorité renderer 96k), conservation des protections | Luna Implementer xhigh | B0A SHA | `docs/mission01/LIMITS_DECISIONS.md`, code limites ciblé | N−1/N/N+1, mémoire, SHA, aucune troncature silencieuse ; au moins une restriction arbitraire supprimée ou mesure bloquante | worktree write | — | PENDING | UNKNOWN |
| M01-A | B0A | Stratégie CTI : matrice claim/hypothèse/preuve, scénarios sans sample/avec sample/contradiction/YARA/goodware ; réutiliser runtime existant | Luna Implementer xhigh | B0A SHA | `investigation_*`, `rule_workbench`, tests scénarios | 5 scénarios offline déterministes avec raisons d'abandon | worktree write | — | PENDING | UNKNOWN |
| M01-Q | B0A, L, A | Éval anti-slop baseline vs patch, jeu versionné, métriques et diff de complexité | Luna Implementer xhigh | SHA intégration | `docs/mission01/`, tests eval | oracle déterministe faits/ref ; zéro fuite ; comparaison baseline/patch | worktree write | Q1 `9523cc4` | **PARTIAL** : Q1 corpus/harness/baseline PASS_WITH_CHANGES intégré ; Q2 patch prompt `DEFER/NO-GO offline` documenté (aucun oracle sémantique sans modèle ; changerait `INVESTIGATION_PROMPT_VERSION`/idempotence) | UNKNOWN |
| M01-Q2 | Q | Patch prompt anti-slop borné et comparaison baseline/patch | — | Q1 | — | — | — | — | **DEFERRED/NO-GO offline** : le protocole à réponses fixes ne mesure pas la réduction sémantique; un patch production changerait la version d'idempotence sans preuve déterministe. Nécessite une évaluation modèle autorisée séparément. | UNKNOWN |
| M01-Q-SCOUT | Q | Cartographie et plan exécutable offline : définition du slop, baseline, patch minimal, schéma du jeu de cas versionné, métriques/seuils, oracle, fuites, complexité | Luna Audit xhigh | `3fd9664` | `docs/mission01/Q_PLAN.md` | plan GO/NO_GO avec paths:lines et ce qui reste UNKNOWN | lecture seule | agent `a4991603`, plan intégré | DONE : **GO conditionnel** corpus/oracle/patch étroit ; **NO-GO** pour conclure à une réduction sémantique en offline | UNKNOWN |
| M01-Q1 | Q-SCOUT | Corpus anti-slop versionné (16–32 cas) + manifeste SHA-256 + harness pytest déterministe (métriques par dimension, zéro fuite, baseline prompts) ; aucun code de production | Luna Implementer xhigh | `b2a5c57` | `tests/fixtures/advisor_eval/anti_slop.v1.jsonl`, `manifest.v1.json`, `tests/test_advisor_anti_slop_eval.py`, `docs/mission01/Q_ANTI_SLOP_EVAL.md` | manifeste cohérent, seuils par dimension, zéro secret/byte, métriques baseline mesurées | worktree write | `dd925ff` → `e1779f8` → `a3976c3`, intégré `9523cc4` | **PASS_WITH_CHANGES** : 16 cas, SHA corpus `a73fdb9…684e`, 2 passed baseline **et** patch, tous seuils verts ; review finale PASS_WITH_CHANGES, 2 MEDIUM corrigés | UNKNOWN |
| M01-Q1-REVIEW | Q1 | Revue d'intégrité de l'éval : corpus synthétique, manifeste, métriques non tautologiques, parseurs réels, réutilisabilité Q2, fuites, complexité | Luna Audit xhigh | `dd925ff` | rapport intégré dans `Q_ANTI_SLOP_EVAL.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + oracle rejoué | lecture seule | agent `1c095748` | DONE : **PASS_WITH_CHANGES** ; 1 passed ; corps/manifeste/parseurs réels confirmés ; 1 MEDIUM (fausse contradiction) + 2 LOW (rapport par cas/variante, base64 isolé) | UNKNOWN |
| M01-Q1-R1 | Q1-REVIEW | Vraie contradiction même sujet/propriété ; rapport par cas + champ `variant` ; détection base64 long ; SHA corpus/manifeste recalculés | Luna Implementer xhigh | `dd925ff` | corpus JSONL, manifeste, harness, doc Q | métriques vertes, manifeste cohérent, rapport individuel/variante, zéro fuite | worktree write | `e1779f8` (branche `feat/mission01-q1-anti-slop-eval`) | DONE : 2 passed, SHA corpus `a73fdb9…684e`, 16 cas ; revue PASS_WITH_CHANGES, 2 MEDIUM corrigés en Q1-R2 ; intégré `9523cc4` | UNKNOWN |
| M01-Q1-REVIEW-R2 | Q1-R1 | Vérification finale : contradiction réelle, rapport par cas/variante, base64, manifeste, non-tautologie | Luna Audit xhigh | `e1779f8` | rapport intégré dans `Q_ANTI_SLOP_EVAL.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + oracle rejoué | lecture seule | agent `cac542a0` | DONE : **PASS_WITH_CHANGES** ; 2 passed ; contradiction réelle confirmée ; 2 MEDIUM (variant non paramétrable, empreinte doc périmée) + réserve valeurs | UNKNOWN |
| M01-Q1-R2 | Q1-REVIEW-R2 | Variante configurable baseline/patch, empreinte doc corrigée, assertion des deux valeurs de contradiction | Luna Implementer high | `e1779f8` | corpus, manifeste, harness, doc Q | Q2 peut émettre `variant: patch`; SHA cohérent; métriques vertes | worktree write | `a3976c3`, intégré `9523cc4` | DONE : 2 passed baseline + 2 passed patch, SHA corpus `a73fdb9…684e` | UNKNOWN |
| M01-B1A1-ATOMIC | B1A1-KEY | Primitive atomique révocation/dispatch : verrou de ligne tenu du contrôle final au retour transport ; révocation avant → 0 appel ; dispatch avant → révocation attend ; G2 dans le verrou ; clé ModelRun stable | Luna Implementer xhigh | `a69831a` | `investigation_service.py`, `investigation_tools.py`, `investigation_advisor_tools.py`, `infrastructure/database/repositories/investigation.py`, tests B1A1/B1B | ordre sérialisé asserté, 0 transport si révocation d'abord, aucune migration, tests existants verts | worktree write | `699200b` → `7af1d62` → WIP `32513ca`, **non intégré** | **BLOCKED** : R1 148 passed, revue R2 FAIL (advisor `EXTERNAL_READ`) ; R2 s'arrête après 2 tentatives sans progrès : refus direct OK mais chemin positif gardé bloqué au préflight et flags de guard forgeables. Nécessite un capability/lease non forgeable créé par le dispatcher. Branche non intégrée. | UNKNOWN |
| M01-B1A1-ATOMIC-REVIEW | B1A1-ATOMIC | Revue critique de concurrence : ordre commit/lock, admission avant dispatch, release sur tous chemins, contournement du guard, deadlocks, régressions | Luna Audit xhigh | `699200b` | rapport intégré dans `B1A1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + scénarios d'ordre + tests rejoués | lecture seule | agent `5d82f90d` | DONE : **FAIL**, séquence SQL correcte mais guard facultatif au dispatcher/advisor (1 HIGH) + lock I/O (1 MEDIUM) ; 144 passed rejoués | UNKNOWN |
| M01-B1A1-ATOMIC-R1 | B1A1-ATOMIC-REVIEW | Fail-closed : guard obligatoire pour tout effet externe, refus si dépôt sans `dispatch_guard`, tests de refus dispatcher/advisor/service | Luna Implementer xhigh | `699200b` | `investigation_tools.py`, `investigation_service.py`, `investigation_advisor_tools.py`, `investigation_persistence.py`, tests B1A1/service | 0 transport sans guard, refus typé testé, 148 tests verts | worktree write | `7af1d62` (branche `feat/mission01-b1a1-atomic-dispatch`) | DONE (148 passed) ; revue R2 FAIL advisor ; voir BLOCKED final | UNKNOWN |
| M01-B1A1-ATOMIC-REVIEW-R2 | B1A1-ATOMIC-R1 | Revue ciblée fail-closed : détermination de l'effet externe, refus service/advisor, guard mémoire, tests de refus, régressions | Luna Audit xhigh | `7af1d62` | rapport intégré dans `B1A1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | agent `c69e51e3` | DONE : **FAIL**, 1 HIGH advisor `EXTERNAL_READ` encore appelable sans guard + test non valide ; dispatcher/service/guard mémoire confirmés ; 148 passed rejoués | UNKNOWN |
| M01-B1A1-ATOMIC-R2 | B1A1-ATOMIC-REVIEW-R2 | Advisor : guard obligatoire à l'entrée pour tout `EXTERNAL_READ`, refus avant divulgation/transport, test avec requête valide et transport espion | Luna Implementer xhigh | `7af1d62` | `investigation_advisor_tools.py`, `tests/test_b1_admission_durability.py`, éventuellement `investigation_tools.py` | 0 transport sans guard, preuve typée, chemin avec guard OK, 148+ tests verts | worktree write | WIP `32513ca` (non intégré) | **BLOCKED** après 2 tentatives sans progrès : refus direct avec preuve typée OK ; chemin positif gardé échoue `advisor_preflight_insufficient_evidence` ; flags `dispatch_guard_*` forgeables | UNKNOWN |
| M01-B1A1-KEY | B1A1 | Faisabilité offline d'une clé d'effet canonique fournie par l'adaptateur et/ou d'une primitive atomique révocation/dispatch ; pas de nouvelle heuristique | Luna Audit xhigh | `5f25b46` | rapport intégré dans `B1A1_REVIEW.md` | verdict FEASIBLE_OFFLINE/BLOCKED/PARTIAL avec primitive manquante, paths:lines | lecture seule | agent `95f180aa` | DONE : **PARTIAL** ; clé canonique inter-appels absente (seule `ModelRun run.id:aN` par tentative, `operation_key` client + fingerprint heuristique) ; primitive atomique faisable par verrou tenu pendant le dispatch | UNKNOWN |
| M01-B1A1-R1 | B1A1-KEY | Primitive atomique révocation/dispatch : verrou de ligne tenu du contrôle final au retour transport ; révocation avant → 0 appel ; dispatch avant → révocation attend ; G2 dans le verrou ; clé ModelRun stable | Luna Implementer xhigh | `a69831a` | `investigation_service.py`, `investigation_tools.py`, `investigation_advisor_tools.py`, `bridge_investigator.py`, `repositories/investigation.py`, tests B1A1/B1B | ordre sérialisé asserté, 0 transport si révocation d'abord, aucune migration, tests existants verts | worktree write | — | RUNNING (agent `12a25977`, worktree `m01-b1a1-atomic`) | UNKNOWN || M01-T | S0 | Parité Typst : inventaire SHA/versions, fixture identique, comparaison texte/sections/figures/IOC + captures, classification | Luna Audit/Implementer xhigh | 058bb4a | `docs/mission01/TYPST_REFERENCE_PARITY.md`, renderer ciblé | rapport par page/section + captures ; aucun comptage de pages seul | worktree write | `cae3115`, intégré `7dc4b0e` | PASS rapport ; restauration V4 BLOCKED (validation éditoriale) ; **BLOCKED_MISSING_REFERENCE** pour la suite : aucun bundle `CascadiaCode` complet dans le dépôt, `var/` non inspectable ; pipeline réel UNKNOWN | UNKNOWN |
| M01-A1 | S0 | Projection read-only stratégie CTI : issues, source_refs conditionnels, raisons de blocage, API lecture, 5 scénarios offline | Luna Implementer xhigh | 058bb4a | `domain/investigation_experiment.py`, `application/investigation_experiments.py`, `application/investigation_operator_view.py`, `api/investigation.py`, tests | partial/missing → UNKNOWN, jamais REFUTED ; zéro réseau ; métriques YARA avec dénominateurs | worktree write | `cd34474` → `1f0e813`, intégré `545d32a` | **PASS_WITH_CHANGES** (64 tests, Ruff) ; résiduel MEDIUM `capture_sha256`/`next_pivots` documenté | UNKNOWN |
| M01-A1-REVIEW | A1 | Revue critique A1 : sémantique verdicts, source_ref, API read-only, scénarios, interaction B0A | Luna Audit xhigh | 058bb4a | `docs/mission01/A1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `5c81019`, intégré `4ec85aa` | DONE : FAIL, 1 BLOCKER + 2 HIGH | UNKNOWN |
| M01-A1-R1 | A1-REVIEW | Réparation A1 : cohérence claims/stratégie, critère de falsification, projection expurgée, test route réelle | Luna Implementer xhigh | `cd34474` | `investigation_experiments.py`, `investigation_experiment.py`, `api/investigation.py`, tests | claims.status ne peut pas être refuted sans preuve ; zéro contenu libre sensible ; tests route | worktree write | `da79486` | DONE, re-revue FAIL (A1-R2/R3 HIGH) | UNKNOWN |
| M01-A1-R2 | A1-REVIEW-2 | Réparation : projeter record/list, supprimer scalaires génériques, valider input_id, timeline sûr, lifespan UNKNOWN | Luna Implementer xhigh | `da79486` | `investigation_experiments.py`, `api/investigation.py`, tests | record/list sans refuted non prouvé ; aucune valeur/ID sensible ; lifespan documenté | worktree write | `a0aaee6` | DONE, re-revue FAIL (step_ids HIGH) | UNKNOWN |
| M01-A1-R3 | A1-REVIEW-3 | Validation canonique de step_ids et métadonnées identifiantes, libellé assertion | Luna Implementer xhigh | `a0aaee6` | `investigation_experiments.py`, `api/investigation.py`, tests | identifiants arbitraires absents des trois sorties ; verdict étiqueté assertion | worktree write | `27db077`, intégré `3473413` | DONE, re-revue FAIL (SHA non résolus HIGH) | UNKNOWN |
| M01-A1-R4 | A1-REVIEW-4 | Durcissement final : `[redacted]` pour SHA/IDs non résolus et métadonnées outil non canoniques ; libellé frontend | Luna Implementer xhigh | `27db077` | `investigation_experiments.py`, `api/investigation.py`, frontend, tests | références non résolues absentes ; tool metadata canoniques seulement | worktree write | `4a71f45`, intégré `00a78b8` | DONE (63 backend + 3 frontend + typecheck), revue fallback en cours | UNKNOWN |
| M01-A1-RES | A1 | Résiduel MEDIUM : cohérence type/provenance des observations liées (`capture_sha256`/`next_pivots`), tests de version d'invocation | Luna Implementer xhigh | `a1d263a` | `investigation_experiments.py`, `tests/test_investigation_experiment_api.py`, éventuellement domaine/tests | `vt_file_report` lié non présenté comme capture ; sample/capture corrects ; invocation version exacte visible, autre version masquée | worktree write | `fbe5d36` (branche `feat/mission01-a1-residual`) | DONE (57 passed) ; revue PASS_WITH_CHANGES (1 MEDIUM `kind`/`object_type`) ; réparation R1 en cours | UNKNOWN |
| M01-A1-RES-REVIEW | A1-RES | Revue critique du résiduel A1 : type/provenance, next_pivots, versions d'invocation, sur-masquage, régressions | Luna Audit xhigh | `fbe5d36` | rapport intégré dans `A1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | agent `fe7f765f` | DONE : **PASS_WITH_CHANGES** ; 57 passed rejoués ; 1 MEDIUM (`kind` non recoupé avec `object_type`) | UNKNOWN |
| M01-A1-RES-R1 | A1-RES-REVIEW | Recouper `kind`/`object_type` pour sample/capture, masquer les combinaisons incohérentes, tests dédiés | Luna Implementer xhigh | `fbe5d36` | `investigation_experiments.py`, tests API/experiments + fixtures | `vt_file_report` + object_type sample → masqué ; sample/capture valides visibles ; oracle vert | worktree write | `bd35278` + `749a3f2`, intégré `69d3b904` | **PASS** après revue R2 (`cdb8f69e`, aucun constat) ; 79 passed, 194 passed combiné sur l'intégration ; PostgreSQL réel `UNKNOWN` (3 tests collectés) | UNKNOWN |
| M01-A1-RES-REVIEW-R2 | A1-RES-R1 | Revue finale du mapping strict kind/object_type et des fixtures | Luna Audit xhigh | `749a3f2` | rapport intégré dans `A1_REVIEW.md` | verdict + tests rejoués | lecture seule | agent `cdb8f69e` | DONE : **PASS**, aucun constat ; 79 passed rejoués | UNKNOWN |
| M01-FINAL | tous | FINAL_AUDIT.md : tests réellement exécutés, restes, coûts, diff complexité, branches/approbations | Parent | intégration | `docs/mission01/FINAL_AUDIT.md` | matrice vérifiée ; aucun PASS sans exécution | lecture/intégration | code `9523cc4` ; main local `db9aa1d3` | DONE (audit V3.2 reprise) ; **PARTIAL** : B1B/L2/A1/Q1 acceptés ; F6/B1A1 clé+atomicité/P10/Q2/T restes documentés ; suite 4667/34 intégration, **4668/33 sur main** (échecs env.) ; merge local non poussé | UNKNOWN |

## Reprise orchestrateur unique — 2026-10-24

**Base d'intégration :** `6b195122`, branche `chore/mission01-integration`
(commit documentaire Paseo au-dessus de `a81b96c` ; état code/tests vérifié
`8fdb7a7`). Tous les lots de reprise partent de `6b19512`.

DAG de reprise (priorité décroissante) :

```text
6b19512 ─┬─ B1B (temporalité/reprise) ─┬─ F6 (allowlist preuves, advisor tools)
         │                              ├─ B1A1-clé canonique (adaptateur)
         │                              └─ P10 (politique divulgation, après L1)
         ├─ L2 (pagination keyset) ────── Q (éval anti-slop baseline vs patch)
         ├─ A1-résiduel MEDIUM (fichiers expériences/API, disjoint)
         └─ T (BLOCKED_MISSING_REFERENCE tant que bundle polices/validation
              éditoriale absents)
```

Règles : un worktree par writer, aucun chevauchement de fichiers ; B1B et L2
d'abord ; F6/B1A1/P10 attendent l'intégration B1B car ils partagent
`investigation_advisor_tools.py`/l'adaptateur ; revue indépendante Luna xhigh
distincte de l'implémenteur pour chaque lot critique ; le parent rejoue
l'oracle après intégration. Aucun `PASS` sans test exécuté et revue effective.

| Session Paseo | TASK_ID | Worktree | Objet |
|---|---|---|---|
| `f4339c14` | M01-B1B | `m01-b1b` | Temporalité, 429/Retry-After, timeout, pacing, crash-reprise |
| `b1a2f600` | M01-B1B-REVIEW | `m01-b1b-review` | Revue critique B1B — FAIL, 1 BLOCKER |
| `da50bf98` | M01-B1B-R1 | `m01-b1b` | Réparation pacer/Retry-After/wrapper/clé |
| `66c9488e` | M01-B1B-REVIEW-R2 | `m01-b1b-review-r2` | Seconde revue — PASS_WITH_CHANGES |
| `4e1023bb` | M01-B1B-R2 | `m01-b1b` | Cooldown partagé Retry-After |
| `e8bd922a` | M01-B1B-REVIEW-R3 | `m01-b1b-review-r3` | Revue ciblée du cooldown |
| `82ac4997` | M01-B1B-R3 | `m01-b1b` | Dernière réparation cooldown `turn` |
| `772a15d5` | M01-B1B-REVIEW-R4 | `m01-b1b-review-r4` | Vérification finale ciblée |
| `c538867e` | M01-F6 | `m01-f6` | Allowlist preuves, titres/encodages/fragments |
| `2e22ca1b` | M01-F6-REVIEW | `m01-f6-review` | Revue critique F6 |
| `6b9fefa0` | M01-L2 (v1) | `m01-l2` | BLOCKED périmètre, zéro modification |
| `0a268b54` | M01-L2 (v2) | `m01-l2` | Pagination/keyset, pages CAS, manifeste d'omissions |
| `fd227a85` | M01-L2-REVIEW | `m01-l2-review` | Revue critique L2 — FAIL, 3 HIGH |
| `131a1d1a` | M01-L2-R1 | `m01-l2` | Réparation CAS/rédaction/omissions/compatibilité |
| `d676bd6a` | M01-L2-REVIEW-R2 | `m01-l2-review-r2` | Seconde revue L2 — PASS_WITH_CHANGES |
| `984c7bc9` | M01-L2-R2 | `m01-l2` | Manifeste/mémoire honnête |
| `45b350c4` | M01-L2-REVIEW-R3 | `m01-l2-review-r3` | Vérification finale L2 — PASS_WITH_CHANGES |
| `581bca1e` | M01-L2-R3 | `m01-l2` | Vérification aux constructeurs |
| `24b810f2` | M01-L2-REVIEW-R4 | `m01-l2-review-r4` | Revue de clôture L2 |
| `3386741e` | M01-A1-RES | `m01-a1-res` | Résiduel provenance/type A1 |
| `fe7f765f` | M01-A1-RES-REVIEW | `m01-a1-res-review` | Revue du résiduel A1 |
| `cfffee10` | M01-A1-RES-R1 | `m01-a1-res` | Recoupement kind/object_type |
| `a4991603` | M01-Q-SCOUT | `m01-q-scout` | Plan éval anti-slop |
| `47c8a52a` | M01-Q1 | `m01-q1` | Corpus + harness anti-slop |
| `1c095748` | M01-Q1-REVIEW | `m01-q1-review` | Revue d'intégrité Q1 |
| `9145b1e7` | M01-Q1-R1 | `m01-q1` | Vraie contradiction + rapport variante |
| `cac542a0` | M01-Q1-REVIEW-R2 | `m01-q1-review-r2` | Vérification finale Q1 |
| `2e8105ef` | M01-Q1-R2 | `m01-q1` | Variante Q2 + valeurs contradiction |
| `95f180aa` | M01-B1A1-KEY | `m01-b1a1-scout` | Faisabilité clé canonique |
| `12a25977` | M01-B1A1-R1 | `m01-b1a1-atomic` | Verrou dispatch/révocation |
| `5d82f90d` | M01-B1A1-ATOMIC-REVIEW | `m01-b1a1-atomic-review` | Revue concurrence — FAIL |
| `7b906ad4` | M01-B1A1-ATOMIC-R1 | `m01-b1a1-atomic` | Guard fail-closed |
| `c69e51e3` | M01-B1A1-ATOMIC-REVIEW-R2 | `m01-b1a1-atomic-review-r2` | Revue fail-closed — FAIL |
| `95a3ad51` | M01-B1A1-ATOMIC-R2 | `m01-b1a1-atomic` | Guard advisor obligatoire |
| `ac1bfc58` | M01-A1-RES-R1-FIX | `m01-a1-res` | Fixture canonique A1 |
| `0bbe1e0e` | M01-A1-RES-R1-FIX2 | `m01-a1-res` | Canonicalisation 2 fixtures |
| `cdb8f69e` | M01-A1-RES-REVIEW-R2 | `m01-a1-res-review-r2` | Revue finale A1 |

## Porte finale — suite backend complète (avant B1A1 atomique)

Exécutée le 2026-10-24 sur `chore/mission01-integration` (B1B + L2 + A1 + Q1
intégrés), commande :

```sh
cd backend && PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  --ignore=tests/integration -m "not integration" -q --tb=line
```

Résultat : **4667 passed, 34 failed, 2 skipped, 30 deselected** en 64,50 s.
Les 34 échecs sont les échecs d'environnement déjà documentés : 32 tests
Typst réels (`FontBundleInvalidError: Manifest-listed file is missing:
'CascadiaCode/ttf/CascadiaCode.ttf'`) et 2 tests confined/mwcp préexistants
sur `058bb4a` (`test_confined_analysis_dispatch`). Aucune régression nouvelle.
Les 2 skips sont `test_confined_analysis_image` (image non construite) et
`test_ghidra_service_real` (Ghidra absent). Les tests d'intégration PostgreSQL
sont hors de cette commande et restent `UNKNOWN`.

## Incident de capacité (2026-10-23)

Le quota Codex/Luna a été épuisé en cours de mission (reset annoncé le
2026-10-24 à 00:41). Les lots B1A1-R2, B1A2-R2-FIX et les revues finales
B0A/A1 ont été confiés à des sessions **Claude Opus 5.5 xhigh** via Paseo,
conformément à la règle de fallback Claude autorisée. Ces revues sont
explicitement identifiées comme non-Luna dans leurs rapports. Les travaux
Luna déjà livrés restent inchangés.

## Reprise Luna 2026-10-24 00:50 — sessions terminées

| Session | TASK_ID | Worktree | Objet |
|---|---|---|---|
| `98446181` | M01-REG-FIX | `fix/mission01-regressions` (`wks_cbcdaa104d853d74`) | 5 régressions fermées `89eb3a5` (4589 passed / 34 env.) ; fusion intégration en cours |
| `75a283e7` | M01-B0A-R4 | `feat/mission01-b0-bridge-chain` | data URL base64 >512 + allocation avant check — livré `cd69eca`, 153 tests |
| `dbed9da5` | M01-B0A-REVIEW-R5 | `audit-mission01-b0a-review` | re-revue ciblée data URL `cd69eca` |
| `03cab396` | M01-A1-R5 | `feat/mission01-a1-strategy-projection` | UUID/SHA canoniques non liés à l'expérience — livré `1f0e813`, 64 tests |
| `534b4bd1` | M01-A1-REVIEW-R6 | `audit-mission01-a1-review` | re-revue ciblée des références liées `1f0e813` |
| `24a6d5ba` | M01-B1A2-REVIEW-R2 | `audit-mission01-b1a2-review` | verdict final `01346e2`/`5484375` |

**B1A1-REVIEW-R3 : FAIL** (`4317f46`). Protections confirmées : preuve
`pre_submission` typée, coût/clé, crash→`NEEDS_REVIEW`, extension G2, CAS ;
atomicité révocation/transport `BLOCKED` ; dette `object_type` documentée.
Résiduels HIGH : équivalences `review_text` (`anchor`/`block_type`), UUID
imbriqués `draft_text`, doublons `finding_refs` avec casse différente, champs
normalisés d'autres outils VT. **Décision parent : arrêt de l'heuristique
après 3 tentatives** ; B1A1 reste `FAIL/PARTIAL` sur l'équivalence sémantique
et exige une clé d'effet canonique fournie par l'adaptateur (lot dédié). Les
deux échecs `required_canonical_evidence_not_in_payload` sont traités par
M01-REG-FIX.

Verdicts déjà reçus : **A1-REVIEW-R6 PASS_WITH_CHANGES** (`2e75d94`,
64 tests ; résiduel MEDIUM : un `vt_file_report` lié peut ressortir comme
`capture_sha256`/`next_pivots` avec un type/provenance mal représenté, tests
de version d'invocation à compléter) ; **B0A-REVIEW-R4 FAIL** (`ca78ded`,
data URL base64 ; corrigé `cd69eca`, re-revue en cours) ; **B1A1-REVIEW-R3
FAIL** (`4317f46`, équivalence sémantique résiduelle, arrêt après 3
tentatives) ; **B1A2-REVIEW-R2 PASS_WITH_CHANGES** (`8597053`, 217/220 tests ;
plancher conservateur, `BridgeInvestigator.turn` hors borne et état ambigu en
chaîne documentés).

Aucun push/live. Les verdicts ne seront intégrés qu'après examen du compte
rendu ; `PASS` seulement si tests verts et revue effective.

## Régressions en attente (reprise Luna 2026-10-24 00:50)

La suite backend hors intégration donne 4579 passed / 39 failed. 24 échecs
sont Typst (bundle CascadiaCode absent), 2 sont préexistants sur `058bb4a`
(worker confined/mwcp), et **5 régressions réelles** restent à traiter :
hex court `advisor_wire` (corrigé WIP `849cfa0`), alias C01 `T1` non résolu
par `advisor_wire_aliases`, retry d'acquisition devenu `NEEDS_REVIEW`,
préflight advisor minimal sans preuve canonique, et résultat advisor `PARTIAL`
devenu `NEEDS_REVIEW`. Détail et SHA dans `HANDOFF.md`. Ne pas déclarer PASS
avant correction, suite verte et revue indépendante Luna xhigh.

## Conventions de compte rendu

Chaque worker renvoie au parent : TASK_ID, statut
(`DONE/FAIL/BLOCKED/UNKNOWN`), chemins:lignes, SHA du commit local, liste
exacte des tests exécutés avec résultat, risques, inconnues, action attendue.
Pas de transcript, pas de dump de logs, pas de corps CTI privé.

## Statuts autorisés

`PASS` uniquement après oracle exécuté. `UNKNOWN` n'est jamais converti en
`PASS` par absence de preuve. `BLOCKED` documente la dépendance humaine
(approbation live, G2/G3, publication, migration) sans paralyser les lots
indépendants.
