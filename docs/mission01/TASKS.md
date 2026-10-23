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
| M01-B0A | S0 | Chaîne de complétude : cartographie B0.1, contrat par question B0.2, suppression plafond renderer 96k B0.3, préflight preuves B0.5, oracles B0.8 (1–4, 7) | Luna Implementer xhigh | 058bb4a | `backend/src/cti_app/application/{bridge_investigator,advisor_dossier,advisor_wire,investigation_advisor_tools,research_context_builder}.py`, tests ciblés | tests offline : preuve manquante/récupérée, contradiction absente, N−1/N/N+1 autour 96k puis >200k côté contrat Bridge simulé, reparse sans nouvelle facturation | worktree write, tests offline | — | RUNNING | UNKNOWN |
| M01-B0B | B0A | Observabilité corrélée B0.4/B0.6 et exports d'audit B0.7 : RUN_TRACE.md, RUN_EVENTS.jsonl.gz, RUN_MANIFEST.json, index | Luna Implementer xhigh | B0A SHA | événements/projections existants, CLI/export, tests | reconstruction déterministe sur ≥778 invocations synthétiques ; zéro fuite ; reprise | worktree write | — | PENDING | UNKNOWN |
| M01-B1-SCOUT | S0 | Carte admission/budget : chemin réel, source budget, incident 266/3600, durées distinctes, fixtures a–e | Luna Audit xhigh | 058bb4a | `docs/mission01/B1_ADMISSION_MAP.md` | paths:lines ; faits vs inférences vs UNKNOWN | lecture seule | `ad4da76`, intégré `464836a` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-B1A | — | SUPERSEDED par la revue `7a40943` : scindé en B1A1 (durabilité/unicité) et B1A2 (politique de coût bornée) | — | 058bb4a | — | — | — | — | SUPERSEDED | — |
| M01-B1B | B1A | Reprise/temporalité : réponse tardive, 429/Retry-After, timeout, pacer Bridge, needs_review, crash-reprise, statut inconnu | Luna Implementer xhigh | B1A SHA | `bridge_investigator.py` (pacer/Retry-After), `model_conversations.py`, `model_gateway.py`, tests fake clock | aucun double appel externe en simulation ; états explicites ; `production_pacing.py` n'est PAS le pacer Bridge | worktree write | — | PENDING | UNKNOWN |
| M01-L-SCOUT | S0 | 42 limites : consommateur, décision motivée, plan de tests | Luna Audit xhigh | 058bb4a | `docs/mission01/LIMITS_DECISIONS.md` | paths:lines ; classification corrigée si preuve contraire | lecture seule | `7494406`, intégré `50eeb1c` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-L1 | S0 | L06 variables sans consommateur + L01/P02 override borné du prompt P10 ; tests P02/P06 | Luna Implementer xhigh | 058bb4a | `compose.yaml`, `.env.example`, `invariant_proposals.py`, docs actifs, nouveau test | défaut P10 96k inchangé ; override borné/dev-only ; politique P10 complète ; refus explicite N+1 ; G2 intact | worktree write | `3b43c9f`, intégré `f280514` | FAIL revue → réparation R1 en cours | UNKNOWN |
| M01-L1-REVIEW | L1 | Revue critique indépendante de L1 (borne P10, retrait variables, G2) | Luna Audit xhigh | 058bb4a | `docs/mission01/L1_REVIEW.md` | verdict PASS/PASS_WITH_CHANGES/FAIL + tests rejoués | lecture seule | `49b4de6`, intégré `0d72923` | DONE : FAIL, 1 HIGH + 1 MEDIUM | UNKNOWN |
| M01-L1-R1 | L1-REVIEW | Réparation : override dev-only, politique P10 agrégée (TLP RED/do_not_submit), parse strict, tests frontières | Luna Implementer xhigh | `3b43c9f` | `compose.yaml`, `.env.example`, `invariant_proposals.py`, tests | refus avant transport si politique bloquée ; défaut 96k sans drapeau dev ; max exact/max+1 | worktree write | — | RUNNING | UNKNOWN |
| M01-A-SCOUT | S0 | Primitive CTI existantes, matrice hypothèses/preuves, 5 scénarios, trous réels | Luna Audit xhigh | 058bb4a | `docs/mission01/CTI_STRATEGY_MAP.md` | paths:lines ; pas de nouveau framework | lecture seule | `54750b1`, intégré `f8f9575` | PASS (coût UNKNOWN) | UNKNOWN |
| M01-B1-REVIEW | B1-SCOUT | Revue critique indépendante de la carte admission/budget avant B1A | Luna Audit xhigh | 058bb4a | `docs/mission01/B1_ADMISSION_REVIEW.md` | constats CONFIRMED/PLAUSIBLE/REJECTED + verdict ADEQUATE* | lecture seule | `94d6706`, intégré `7a40943` | DONE : ADEQUATE_WITH_CHANGES, 1 BLOCKER + 4 HIGH | UNKNOWN |
| M01-B1A1 | B1-REVIEW | Durabilité admission : coût calculé une fois, réserve orpheline crash, double appel nouvelle clé, CAS/révocation, oracles offline | Luna Implementer xhigh | 058bb4a | `investigation_service.py`, `investigation_tools.py`, `operator_tool_invocation.py`, nouveau test | zéro transport avant réconciliation ; réserves demandée/persistée/consommée/libérée distinctes ; CAS stale et révocation bloquent | worktree write | — | RUNNING | UNKNOWN |
| M01-B1A2 | B0A + B1A1 | Politique `tool_seconds` bornée par type, compatible G2 ; borne dure ≤ budget approuvé et propagée au transport ; pas d'inférence durée historique | Luna Implementer xhigh | B0A SHA | `investigation_advisor_tools.py`, `model_gateway.py`, tests | borne ≤ réserve ; timeout research 7200 confronté au budget ; refus explicite sinon | worktree write | — | PENDING (après B0A) | UNKNOWN |
| M01-L | B0A (L1) | 42 restrictions : décisions motivées, suppression des plafonds arbitraires démontrés (priorité renderer 96k), conservation des protections | Luna Implementer xhigh | B0A SHA | `docs/mission01/LIMITS_DECISIONS.md`, code limites ciblé | N−1/N/N+1, mémoire, SHA, aucune troncature silencieuse ; au moins une restriction arbitraire supprimée ou mesure bloquante | worktree write | — | PENDING | UNKNOWN |
| M01-A | B0A | Stratégie CTI : matrice claim/hypothèse/preuve, scénarios sans sample/avec sample/contradiction/YARA/goodware ; réutiliser runtime existant | Luna Implementer xhigh | B0A SHA | `investigation_*`, `rule_workbench`, tests scénarios | 5 scénarios offline déterministes avec raisons d'abandon | worktree write | — | PENDING | UNKNOWN |
| M01-Q | B0A, L, A | Éval anti-slop baseline vs patch, jeu versionné, métriques et diff de complexité | Luna Implementer xhigh | SHA intégration | `docs/mission01/`, tests eval | oracle déterministe faits/ref ; zéro fuite ; comparaison baseline/patch | worktree write | — | PENDING | UNKNOWN |
| M01-T | S0 | Parité Typst : inventaire SHA/versions, fixture identique, comparaison texte/sections/figures/IOC + captures, classification | Luna Audit/Implementer xhigh | 058bb4a | `docs/mission01/TYPST_REFERENCE_PARITY.md`, renderer ciblé | rapport par page/section + captures ; aucun comptage de pages seul | worktree write | `cae3115`, intégré `7dc4b0e` | PASS rapport ; restauration V4 BLOCKED (validation éditoriale) ; pipeline réel UNKNOWN (font bundle) | UNKNOWN |
| M01-A1 | S0 | Projection read-only stratégie CTI : issues, source_refs conditionnels, raisons de blocage, API lecture, 5 scénarios offline | Luna Implementer xhigh | 058bb4a | `domain/investigation_experiment.py`, `application/investigation_experiments.py`, `application/investigation_operator_view.py`, `api/investigation.py`, tests | partial/missing → UNKNOWN, jamais REFUTED ; zéro réseau ; métriques YARA avec dénominateurs | worktree write | — | RUNNING | UNKNOWN |
| M01-FINAL | tous | FINAL_AUDIT.md : tests réellement exécutés, restes, coûts, diff complexité, branches/approbations | Parent | intégration | `docs/mission01/FINAL_AUDIT.md` | matrice vérifiée ; aucun PASS sans exécution | lecture/intégration | — | PENDING | UNKNOWN |

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
