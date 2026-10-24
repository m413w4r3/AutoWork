# Mission 04 / Phase 2 — Validation et gates

**Base :** `e120a36f1afa08261fdf0276e0200872973ad8f2`
**Branche d'intégration :** `mission04/implementation`
**SHA d'intégration code/tests :** `e8b80c5d35e2c14a23cb521e9d899f1301a15056` (le commit documentaire final lui est postérieur)

## 1. Matrice invariants / contrats

| Invariant | Où il est préservé | Preuve réellement exécutée | État |
|---|---|---|---|
| PostgreSQL/MinIO canoniques, SHA-256, versions, CAS, provenance | Aucune écriture de production ; L3 lit une base jetable ; L20 pur ; L2 parser/gate ; L6 fakes | L3 : `1 passed` sur `postgres-test` ; L20 : 16 tests ; L2 : 69 tests ; L6 : 18 tests | préservé |
| G2/G3, TLP, `external_llm_allowed`, `do_not_submit`, restrictions échantillon | Aucun changement de politique ; L20 sans autorisation ; L6 sans Bridge | Tests advisor/P10 existants + nouveaux cas ; revues xhigh | préservé |
| Preuves CTI permissives (raw/hex/Base64/réseau/contradictions) | L1 conserve CVE/marqueurs/URL bénigne ; L20 réutilise le dossier permissif | Tests L1/L20 ; revue R4 note une perte résiduelle sur claim non fermé | **partiel (L1)** |
| Secrets opérationnels AutoWork protégés | L1 redige les affectations sensibles ; L20 réutilise le secret guard | Tests L1/L20 ; aucune valeur réelle utilisée | préservé |
| Aucun egress provider/Bridge/VT/Shodan/Retrohunt | Tous les tests sont offline/fakes | Aucune commande réseau ; revues confirment zéro appel | préservé |
| Aucune migration, aucun changement live | Aucun fichier de migration ; aucune base live touchée | `git diff --name-only` ; revues ; PostgreSQL jetable uniquement | préservé |
| Schémas/versions wire et idempotence | L2 ajoute un outcome interne ; `ProposalResponse` et wire inchangés ; L6 version de parseur ; L20 bytes/SHA | Tests L2/L6/L20 ; `git diff --check` | préservé |
| Replay P09 sans double ligne canonique | L2 gate partagé frais/replay | Test de replay refusé + source `ON CONFLICT DO NOTHING` (revue) | préservé au niveau source ; non rejoué sur PostgreSQL |
| Aucun push/merge `main`/déploiement | Branches locales uniquement | `git log`, `git status` ; aucun remote push | préservé |

## 2. Commandes et résultats réellement exécutés

### Baselines au SHA de départ

| Commande | Résultat |
|---|---|
| `cd backend && uv run --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short` | **58 passed in 0.53s** |
| `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_operator_context_summary_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"` | **1 passed in 1.90s** |

### L1 — branche `mission04/l1-p10-escaped` (non intégrée)

| Étape | Résultat |
|---|---|
| RED initial (credential échappé) | **1 failed** ; valeur visible dans le prompt |
| GREEN initial | **59 passed** |
| RED réparation 1 (espaces/guillemets internes/délimiteur) | **3 failed** |
| GREEN réparation 1 | **64 passed** |
| RED réparation 2 (marqueur, coût, antislash final) | **3 failed in 3.48s** |
| GREEN réparation 2 | **67 passed** |
| RED réparation 3 (guillemets internes, clé mal formée, terminateur) | **6 failed** |
| GREEN réparation 3 (état examiné par R4) | **83 passed in 0.60s** ; 20 ciblés ; Ruff check/format verts |
| Performance scanner (25 613 car., 1 600 labels) | **0,0043 s** normalisation→rendu ; secret de queue absent |
| Revue R4 | **REVISE** : claim non fermé + LF + CVE → repli jusqu'à EOF ; preuve CTI effacée |

### L2 — branche `mission04/l2-p10-refusal` (intégrée)

| Étape | Résultat |
|---|---|
| RED initial | **4 failed, 1 passed, 58 deselected** |
| GREEN initial | **64 passed** ; 6 ciblés |
| RED réparation 1 | **2 failed, 1 passed, 64 deselected** |
| GREEN réparation 1 | **67 passed** ; 9 ciblés |
| RED réparation 2 | **1 failed, 1 passed, 67 deselected** |
| GREEN final | **69 passed in 0.63s** ; 12 ciblés ; Ruff check/format verts ; mypy ciblé `Success` |
| Revue R3 | **APPROVED** |

### L3 — branche `mission04/l3-audit-pg` (intégrée)

| Étape | Résultat |
|---|---|
| Premier run PostgreSQL après écriture | **1 failed** : `InvestigationAgenda` exige 2–6 questions |
| Après correction de fixture | **1 passed in 1.25s** |
| Après réparation R1 | **1 failed in 1.15s** : attendu de ressources incomplet |
| Après réparation R2 | **1 passed in 1.02s** |
| Revalidation sur la branche d'intégration | **1 passed in 1.19s**, **1.30s**, **1.36s** |
| Revue R2 | **APPROVED** |

### L20 — branche `mission04/l20-advisor-prepare` (intégrée)

| Étape | Résultat |
|---|---|
| RED initial | collection impossible (module absent) ; puis **2 failed, 9 passed** |
| GREEN initial | **11 passed** |
| RED réparation (collision mesure) | **4 failed, 12 passed** |
| GREEN final | **16 passed in 0.54s** ; régressions advisor **168 passed / 1 deselected** ; Ruff vert |
| Revue R2 | **APPROVED** |

### L6 — branche `mission04/l6-suricata-reparse` (intégrée)

| Étape | Résultat |
|---|---|
| RED avant correctif | **5 failed** (`advisor_reparse_original_not_found`) |
| GREEN ciblé | **7 passed** |
| GREEN reparse + composition | **18 passed in 0.93s** ; Ruff/format/mypy ciblés verts |
| Revue | **APPROVED** avec P3 |

### Validation croisée finale sur `mission04/implementation`

| Commande | Résultat |
|---|---|
| `pytest tests/test_invariant_proposals.py tests/test_advisor_output_reparse.py tests/test_investigation_composition.py tests/test_advisor_consultation.py -q` | **103 passed in 1.46s** |
| `pytest tests/test_advisor_completeness.py tests/test_advisor_wire.py tests/test_publication_secret_guard.py tests/test_investigation_advisor_tools.py -k 'not archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers' -q` | **152 passed, 1 deselected in 1.25s** |
| `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_audit_export_postgres.py` | **1 passed in 1.30s** |
| `make lint-backend` | **All checks passed** ; **845 files already formatted** |
| `uv run --python 3.12 mypy` sur `invariant_proposals.py`, `domain/invariant_proposals.py`, `investigation_advisor_tools.py`, `advisor_consultation.py` | **Success: no issues found in 4 source files** |
| `git diff --check e120a36f..HEAD` | exit 0, aucune sortie |

Le test `archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers`
est désélectionné car il dépend de données locales non suivies
(`.autowork-runs/...`) absentes des worktrees ; il passe au SHA de base dans
le checkout principal. C'est une dépendance d'environnement préexistante, pas
une régression des lots.

## 3. PostgreSQL jetable

- Service `cti-bulletin-postgres-test-1` (profil `integration-test`, tmpfs,
  port local 55432), déjà `healthy` ; aucune base live touchée.
- Le fixture crée une base au nom aléatoire, migre `head`, puis la supprime.
- L3 est le seul lot avec une preuve PostgreSQL réelle ; L2/L20/L6/L1 sont
  hors ligne/purs/fakes.
- Aucune preuve MinIO réelle : le cas de hash incohérent L3 utilise un
  artifact reader synthétique.
- Aucun test PostgreSQL de concurrence S3 n'a été exécuté (design seulement).

## 4. Tests manquants et limites

1. **L1** : cas R4 `auth` non fermé + LF/CRLF + CVE ; le correctif de fuite
   existe sur la branche mais la perte de preuve empêche l'intégration.
2. **L2** : tests permanents pour alias identiques et fallback (finding
   non bloquant de la revue R3) ; le comportement `STATUS` absent est
   documenté mais reste permissif.
3. **L3** : intégration MinIO réelle non prouvée.
4. **L6** : exposition au runner worker standard non satisfaite (hors scope,
   documentée) ; forme partielle Suricata dédiée sur échec de parsing (P3).
5. **L20** : test de collision portant uniquement sur le SHA de la ref
   d'observation source (non bloquant).
6. **L5** : non implémenté ; polices absentes et lineage reviewer/approver
   nécessitant une décision éditoriale.
7. **L4** : non lancé ; contrat humain non tranché.
8. **S3** : aucun test runtime ; les oracles du contrat sont futurs.
9. **Replay CTI complet offline** : non exécuté dans cette mission.

## 5. Compatibilité

- `P10_PROMPT_VERSION` reste `"4"` ; aucun bump silencieux.
  `NEEDS_DECISION_P10_VERSION` est une décision humaine distincte.
- Le schéma wire P10, `ProposalResponse`, les IDs de candidat et les codes
  de refus sont inchangés ; l'outcome L2 est additif sur le résultat.
- L3 n'a ajouté qu'un test d'intégration ; aucune production modifiée.
- L20 est un module dormant, sans caller de production.
- L6 modifie deux ensembles d'admission et ajoute une garde locale ; aucun
  changement de writer, Bridge, lecteur de preuves, autre type ou mode worker.
- Aucune migration, aucun schéma, aucun contrat d'API modifié.

## 6. Faux positifs / faux négatifs et décisions

| Cas | Traitement |
|---|---|
| L1 : repli fail-closed sur claim non fermé | Condition d'arrêt ; lot `PARTIAL`, non intégré ; correction R4 documentée |
| L1 : labels `auth`/`api_key` CTI génériques | Politique P10 existante conservée ; pas d'élargissement du matcher ; risque documenté |
| L2 : `STATUS` absent | Compatibilité historique documentée (succès + warning + P09 possible) |
| L2 : statut inconnu/vide | Rejeté par `ProposalOutputValidationError` avant P09 |
| L3 : hash incohérent | `unavailable` explicite ; artifact reader synthétique, pas MinIO |
| L20 : valeur dotenv-only | Précondition caller : préfiltrage runtime avant `prepare` |
| L6 : contexte présent mais incomplet | Refus explicite avant `record_json`, zéro écriture |
| L6 : sortie Suricata non parsable | Fallback générique `figure_plan=None`, résultat non utilisable (P3) |

## 7. Gates séparés

| Gate | Verdict | Justification |
|---|---|---|
| Correctifs — L1 | **PARTIAL** | Fuites corrigées et 83 tests verts sur la branche, mais R4 `REVISE` sur la perte de preuve CTI ; non intégré |
| Correctifs — L2 | **NARROW_FIXES_VERIFIED** | Refus terminal frais/replay, tests, revue R3 `APPROVED`, intégré |
| Correctifs — L3 | **NARROW_FIXES_VERIFIED** | Oracle PostgreSQL réel, assertions indépendantes, revue R2 `APPROVED`, intégré |
| Advisor — L20 | **L20_PURE_PREPARE_VERIFIED** | Module pur, non branché, zéro I/O, revue R2 `APPROVED`, intégré |
| Advisor — L21/L22 | **NON AUTORISÉS** | Cutover/parse dual-path hors autorisation |
| S3 | **S3_BLOCKED_DESIGN** | Revue indépendante du contrat : F1–F4 PARTIAL, N1/N3/N4/N5 PARTIAL ; 5 findings HIGH |
| Replay | **BLOCKED_OFFLINE_E2E** | L1 non intégré (fuite de credential échappé encore présente sur la base) et aucun scénario offline complet exécuté |
| Global | **GO_NARROW_PARTIAL** | L2/L3/L20/L6 intégrés et revus ; L1 incomplet ; S3 bloque ; replay non prouvé |

Aucun gate ne vaut `S3_PRODUCTION_READY`, `L20_CUTOVER` ou `READY_TO_DEPLOY`.
