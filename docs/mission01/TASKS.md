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
| M01-B0A | S0 | Chaîne de complétude : cartographie B0.1, contrat par question B0.2, suppression plafond renderer 96k B0.3, préflight preuves B0.5, oracles B0.8 (1–4, 7) | Luna Implementer xhigh | 058bb4a | `backend/src/cti_app/application/{bridge_investigator,advisor_dossier,advisor_wire,investigation_advisor_tools,research_context_builder}.py`, tests ciblés | tests offline : preuve manquante/récupérée, contradiction multi-claim, statuts cohérents, N−1/N/N+1, >200k simulé, hexdump borné, reparse | worktree write, tests offline | `9774680` → `df1bd8f`, intégré `66be3f8` | R3 livré (141 tests), revue fallback Claude en cours | UNKNOWN |
| M01-B0A-REVIEW | B0A | Revue critique indépendante de B0A : omissions silencieuses, manifeste, contradictions, pas de Bridge réimplémenté, tests, complexité | Luna Audit xhigh | 058bb4a | `docs/mission01/B0A_REVIEW.md` | constats + verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `6411e1e`, intégré `a9c5a22` | DONE : FAIL, 2 HIGH + 4 MEDIUM | UNKNOWN |
| M01-B0A-R1 | B0A-REVIEW | Réparation B0A : 24 échecs, multi-claim, statuts MISSING/RETRIEVABLE, sélection lexicale, troncature IOC, hexdump | Luna Implementer xhigh | `9774680` | `advisor_dossier.py`, `bridge_investigator.py`, `investigation_advisor_tools.py`, tests | suite trois fichiers verte ; 116 passed ; zéro omission silencieuse | worktree write | `fbc0f24`, intégré `9761998` | DONE, revue R2 FAIL F6 | UNKNOWN |
| M01-B0A-REVIEW-R2 | B0A-R1 | Re-revue B0A après réparation | Luna Audit xhigh | `fbc0f24` | `docs/mission01/B0A_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `09f1c38`, intégré `3da31ec` | DONE : FAIL, F1–F4 OK, F6 HIGH, F5 UNKNOWN | UNKNOWN |
| M01-B0A-R2 | B0A-REVIEW-R2 | Réparation F6 (hex legacy + content générique) ; F5 compte IOC différé | Luna Implementer xhigh | `fbc0f24` | `investigation_advisor_tools.py`, `advisor_dossier.py`, tests | valeur complète absente du prompt ; mesure courte préservée | worktree write | `c561f04`, intégré `690a813` | DONE F6a ; F5 UNKNOWN documenté | UNKNOWN |
| M01-B0A-REVIEW-R3 | B0A-R2 | Vérification finale F6 et acceptation F5 UNKNOWN | Luna Audit xhigh | `c561f04` | `docs/mission01/B0A_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL | lecture seule | `a00d2b8`, intégré `710e861` | DONE : FAIL, F6b/F6c HIGH | UNKNOWN |
| M01-B0A-R3 | B0A-REVIEW-R3 | Dernière tentative F6 : budget cumulatif + détection base64 générique | Luna Implementer xhigh | `c561f04` | `advisor_dossier.py`, `investigation_advisor_tools.py`, tests | listes neutres et base64 bornées ; zéro transport ; mesures courtes préservées | worktree write | `df1bd8f`, intégré `66be3f8` | DONE (141 tests), revue fallback Claude en cours | UNKNOWN |
| M01-B0B | B0A | Observabilité corrélée B0.4/B0.6 et exports d'audit B0.7 : RUN_TRACE.md, RUN_EVENTS.jsonl.gz, RUN_MANIFEST.json, index | Luna Implementer xhigh | `9774680` | nouveaux modules projection/export + CLI, tests | reconstruction déterministe sur ≥778 invocations synthétiques ; zéro fuite ; reprise | worktree write | `41c8869` + `5add5df`, intégré `a772cc9` | PASS_WITH_CHANGES : F1–F4 fermés, texte corrigé, 14 tests ; PostgreSQL réel UNKNOWN | UNKNOWN |
| M01-B0B-REVIEW | B0B | Revue critique B0B : corrélations, UNKNOWN honnêtes, exports, reprise, fuites, complexité | Luna Audit xhigh | `289903b` | `docs/mission01/B0B_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `63577f7`, intégré `7c7971c` | DONE : FAIL, F1/F2 HIGH | UNKNOWN |
| M01-B0B-R1 | B0B-REVIEW | Réparation : métadonnées advisor allowlistées, ledger décisions, input pack, libellé non-observation, oracles | Luna Implementer xhigh | `289903b` | `investigation_audit_export.py`, `investigation_audit_reader.py`, tests | métadonnées persistées projetées ; pas de texte/corps ; décisions typées ; input pack lié | worktree write | `41c8869`, intégré `d30cced` | DONE (14 tests), re-revue PASS_WITH_CHANGES | UNKNOWN |
| M01-B0B-REVIEW-R2 | B0B-R1 | Re-revue B0B après réparation | Luna Audit xhigh | `41c8869` | `docs/mission01/B0B_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL | lecture seule | `230380d`, intégré `d92f6d6` | PASS_WITH_CHANGES : texte `RUN_TRACE.md` à corriger | UNKNOWN |
| M01-B0B-R2 | B0B-REVIEW-R2 | Corriger le texte statique `RUN_TRACE.md` contredisant les métadonnées exportées | Luna Implementer xhigh | `41c8869` | `investigation_audit_export.py`, tests | texte cohérent ; 14 tests | worktree write | `5add5df`, intégré `a772cc9` | DONE, 14 tests vérifiés parent | UNKNOWN |
| M01-B1-SCOUT | S0 | Carte admission/budget : chemin réel, source budget, incident 266/3600, durées distinctes, fixtures a–e | Luna Audit xhigh | 058bb4a | `docs/mission01/B1_ADMISSION_MAP.md` | paths:lines ; faits vs inférences vs UNKNOWN | lecture seule | `ad4da76`, intégré `464836a` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-B1A | — | SUPERSEDED par la revue `7a40943` : scindé en B1A1 (durabilité/unicité) et B1A2 (politique de coût bornée) | — | 058bb4a | — | — | — | — | SUPERSEDED | — |
| M01-B1B | B1A | Reprise/temporalité : réponse tardive, 429/Retry-After, timeout, pacer Bridge, needs_review, crash-reprise, statut inconnu | Luna Implementer xhigh | B1A SHA | `bridge_investigator.py` (pacer/Retry-After), `model_conversations.py`, `model_gateway.py`, tests fake clock | aucun double appel externe en simulation ; états explicites ; `production_pacing.py` n'est PAS le pacer Bridge | worktree write | — | PENDING | UNKNOWN |
| M01-L-SCOUT | S0 | 42 limites : consommateur, décision motivée, plan de tests | Luna Audit xhigh | 058bb4a | `docs/mission01/LIMITS_DECISIONS.md` | paths:lines ; classification corrigée si preuve contraire | lecture seule | `7494406`, intégré `50eeb1c` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-L1 | S0 | L06 variables sans consommateur ; P10 relèvement ABANDONNÉ et documenté DEFER | Luna Implementer xhigh | 058bb4a | `compose.yaml`, `.env.example`, playbook, `test_limits_mission01.py` | diff base = 4 fichiers L06 ; 2 + 41 tests verts ; G2 intact | worktree write | `e4772fa`, intégré `0483f61` | PASS_WITH_CHANGES (L06) ; P10 DEFER préexistant | UNKNOWN |
| M01-L1-REVIEW | L1 | Revue critique indépendante de L1 (borne P10, retrait variables, G2) | Luna Audit xhigh | 058bb4a | `docs/mission01/L1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | `49b4de6` + `b541b75`, intégré `0d72923` | DONE : FAIL R2, 1 HIGH (politique sources P10) | UNKNOWN |
| M01-L1-R1 | L1-REVIEW | Réparation : override dev-only, politique P10 agrégée (TLP RED/do_not_submit), parse strict, tests frontières | Luna Implementer xhigh | `3b43c9f` | `compose.yaml`, `.env.example`, `invariant_proposals.py`, tests | refus avant transport si politique bloquée ; défaut 96k sans drapeau dev ; max exact/max+1 | worktree write | `0194648` | DONE, re-revue FAIL | UNKNOWN |
| M01-L1-R2 | L1-REVIEW-2 | Inclure la politique des SourceDocuments des claims dans la garde P10 ; fail-closed source introuvable ; tests zéro transport | Luna Implementer xhigh | `0194648` | `invariant_proposals.py`, tests | source RED/do_not_submit/interdite → zéro routage/transport ; sources vertes → passe | worktree write | `a3d17f8` | DONE, re-revue R3 FAIL (2 HIGH) | UNKNOWN |
| M01-L1-R3 | L1-REVIEW-3 | Dernière réparation : provenance des invariants existants + rejeu de snapshot historique ; tests fail-closed | Luna Implementer xhigh | `a3d17f8` | `invariant_proposals.py`, tests | invariant/claim restrictive → zéro transport ; rejeu snapshot devenu restrictif → refus | worktree write | `8d7c969` | FAIL re-revue R4 (2 HIGH) → P10 ABANDONNÉ | UNKNOWN |
| M01-L1-FALLBACK | L1-REVIEW-4 | Repli : restauration P10 à la base, conservation L06 seul, P10 classé DEFER préexistant | Luna Implementer xhigh | `8d7c969` | `compose.yaml`, `.env.example`, playbook, test L06 | diff base = 4 fichiers L06 ; aucun code P10 ; 43 tests | worktree write | `e4772fa` | DONE, validation finale en cours | UNKNOWN |
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
| M01-Q | B0A, L, A | Éval anti-slop baseline vs patch, jeu versionné, métriques et diff de complexité | Luna Implementer xhigh | SHA intégration | `docs/mission01/`, tests eval | oracle déterministe faits/ref ; zéro fuite ; comparaison baseline/patch | worktree write | — | PENDING | UNKNOWN |
| M01-T | S0 | Parité Typst : inventaire SHA/versions, fixture identique, comparaison texte/sections/figures/IOC + captures, classification | Luna Audit/Implementer xhigh | 058bb4a | `docs/mission01/TYPST_REFERENCE_PARITY.md`, renderer ciblé | rapport par page/section + captures ; aucun comptage de pages seul | worktree write | `cae3115`, intégré `7dc4b0e` | PASS rapport ; restauration V4 BLOCKED (validation éditoriale) ; pipeline réel UNKNOWN (font bundle) | UNKNOWN |
| M01-A1 | S0 | Projection read-only stratégie CTI : issues, source_refs conditionnels, raisons de blocage, API lecture, 5 scénarios offline | Luna Implementer xhigh | 058bb4a | `domain/investigation_experiment.py`, `application/investigation_experiments.py`, `application/investigation_operator_view.py`, `api/investigation.py`, tests | partial/missing → UNKNOWN, jamais REFUTED ; zéro réseau ; métriques YARA avec dénominateurs | worktree write | `cd34474` → `4a71f45`, intégré `00a78b8` | R4 livré (63 backend + 3 frontend + typecheck), revue fallback en cours | UNKNOWN |
| M01-A1-REVIEW | A1 | Revue critique A1 : sémantique verdicts, source_ref, API read-only, scénarios, interaction B0A | Luna Audit xhigh | 058bb4a | `docs/mission01/A1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL ; tests rejoués | lecture seule | `5c81019`, intégré `4ec85aa` | DONE : FAIL, 1 BLOCKER + 2 HIGH | UNKNOWN |
| M01-A1-R1 | A1-REVIEW | Réparation A1 : cohérence claims/stratégie, critère de falsification, projection expurgée, test route réelle | Luna Implementer xhigh | `cd34474` | `investigation_experiments.py`, `investigation_experiment.py`, `api/investigation.py`, tests | claims.status ne peut pas être refuted sans preuve ; zéro contenu libre sensible ; tests route | worktree write | `da79486` | DONE, re-revue FAIL (A1-R2/R3 HIGH) | UNKNOWN |
| M01-A1-R2 | A1-REVIEW-2 | Réparation : projeter record/list, supprimer scalaires génériques, valider input_id, timeline sûr, lifespan UNKNOWN | Luna Implementer xhigh | `da79486` | `investigation_experiments.py`, `api/investigation.py`, tests | record/list sans refuted non prouvé ; aucune valeur/ID sensible ; lifespan documenté | worktree write | `a0aaee6` | DONE, re-revue FAIL (step_ids HIGH) | UNKNOWN |
| M01-A1-R3 | A1-REVIEW-3 | Validation canonique de step_ids et métadonnées identifiantes, libellé assertion | Luna Implementer xhigh | `a0aaee6` | `investigation_experiments.py`, `api/investigation.py`, tests | identifiants arbitraires absents des trois sorties ; verdict étiqueté assertion | worktree write | `27db077`, intégré `3473413` | DONE, re-revue FAIL (SHA non résolus HIGH) | UNKNOWN |
| M01-A1-R4 | A1-REVIEW-4 | Durcissement final : `[redacted]` pour SHA/IDs non résolus et métadonnées outil non canoniques ; libellé frontend | Luna Implementer xhigh | `27db077` | `investigation_experiments.py`, `api/investigation.py`, frontend, tests | références non résolues absentes ; tool metadata canoniques seulement | worktree write | `4a71f45`, intégré `00a78b8` | DONE (63 backend + 3 frontend + typecheck), revue fallback en cours | UNKNOWN |
| M01-FINAL | tous | FINAL_AUDIT.md : tests réellement exécutés, restes, coûts, diff complexité, branches/approbations | Parent | intégration | `docs/mission01/FINAL_AUDIT.md` | matrice vérifiée ; aucun PASS sans exécution | lecture/intégration | — | PENDING | UNKNOWN |

## Incident de capacité (2026-10-23)

Le quota Codex/Luna a été épuisé en cours de mission (reset annoncé le
2026-10-24 à 00:41). Les lots B1A1-R2, B1A2-R2-FIX et les revues finales
B0A/A1 ont été confiés à des sessions **Claude Opus 5.5 xhigh** via Paseo,
conformément à la règle de fallback Claude autorisée. Ces revues sont
explicitement identifiées comme non-Luna dans leurs rapports. Les travaux
Luna déjà livrés restent inchangés.

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
