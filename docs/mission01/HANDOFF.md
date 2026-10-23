# Mission 01 V3.2 — handoff de reprise

**Mis à jour :** 2026-10-23 ~21:45, intégration locale `2cafb37`.
**Reprise prévue :** 2026-10-24 **00:50** avec des sous-agents **Luna xhigh**
(quota Codex/Luna épuisé, reset annoncé 00:41). Le quota Claude est épuisé
jusqu'au 2026-10-25 17:00 ; ne pas relancer de fallback Claude avant.
**Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`.
**Branche d'intégration :** `chore/mission01-integration`.
**Branche WIP :** `wip/mission01-parent-fixes` (`849cfa0`) — à relire/cherry-picker,
pas à considérer comme validée.

## Interdits maintenus

Aucun push, merge sur `main`, déploiement, accès live, appel fournisseur,
donnée CTI privée, secret. Les artefacts Mission 00 restent en lecture seule
dans `/home/nill/perso/work/AutoWork/docs/references/mission00-2026-10-23/`.

## Recette de test

```sh
ln -sfn /home/nill/perso/work/AutoWork/backend/.venv <worktree>/backend/.venv
ln -sfn /home/nill/perso/work/AutoWork/.autowork-runs <worktree>/.autowork-runs
cd <worktree>/backend && PYTHONPATH=$PWD/src .venv/bin/python -m pytest <tests> -q --tb=short
```

Frontend : lier `/home/nill/perso/work/AutoWork/frontend/node_modules` puis
`pnpm test --run` et `pnpm typecheck`. Jamais `uv run`, Conda actif, pytest nu.
Typst réel : `TMPDIR=$HOME/tmp` (et le bundle CascadiaCode reste absent).

## Derniers résultats réellement exécutés

- Suite croisée des 9 lots : **220 passed**.
- Backend hors intégration : **4579 passed, 39 failed, 2 skipped**.
  - 24 échecs Typst réels : bundle `CascadiaCode/ttf/CascadiaCode.ttf` absent
    (environnement documenté par T), pas une régression de code.
  - 2 échecs préexistants prouvés sur `058bb4a` :
    `test_confined_analysis_dispatch.py::test_tool_registry_dispatch_runs_capa_and_strings_through_worker`
    et `::test_mwcp_allowlisted_fixture_parser_is_invoked_in_worker`.
  - 5 régressions réelles à traiter (voir ci-dessous).
- Frontend : **293 passed**, `pnpm typecheck` OK.

## Régressions réelles à traiter en priorité

1. `tests/test_advisor_wire.py::test_content_excerpt_extraction_keeps_whole_methods_decoded_values_and_offset_hex`
   — corrigé dans WIP `849cfa0` (hex court avec offset préservé, dumps complets
   toujours rejetés). À relire.
2. `tests/test_advisor_wire.py::test_archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers`
   — C01 : `T1` (avis versionné) est rendu dans `DOSSIER DE FAITS` mais absent
   de `advisor_wire_aliases(replay)["evidence"]`. La regex du test a été
   élargie dans WIP, mais l'assertion de résolution échoue encore. Décider :
   inclure l'alias `T1` dans le résolveur de wire (probable) ou exclure les
   cibles d'avis du manifeste d'évidence, sans casser la traçabilité d'avis.
3. `tests/test_investigation_acquisition_tools.py::test_invalid_download_retry_is_bounded`
   — une invocation n'est plus `FAILED` (probablement `NEEDS_REVIEW`) depuis la
   preuve `pre_submission` typée B1A1-R1. Décider si le test doit attendre
   `NEEDS_REVIEW` sans réponse externe ou si un chemin framework doit rester
   `FAILED`; ne pas réintroduire de marqueur libre d'adaptateur.
4. `tests/test_operator_tool_invocation.py::test_operator_dispatcher_wires_minimal_advisor_request_to_strict_bridge_transport`
   — le préflight B0A renvoie `insufficient_evidence`
   (`required_canonical_evidence_not_in_payload`). Compléter la fixture avec une
   preuve canonique requise, sans affaiblir le préflight.
5. `tests/test_operator_tool_invocation.py::test_advisor_recommendations_record_acceptance_refusal_and_empirical_outcomes`
   — le résultat advisor est désormais `NEEDS_REVIEW` au lieu de `PARTIAL`.
   Décider si un `PARTIAL` parsé est un résultat obtenu (doit rester `PARTIAL`)
   ou une ambiguïté (doit être `NEEDS_REVIEW`) ; corriger le code ou le test en
   conséquence, sans affaiblir la garde d'unicité.

## État des lots

| Lot | État | SHA / preuve | Reste |
|---|---|---|---|
| S0 baseline | PASS (runtime UNKNOWN) | `e26c9a7` + `0074706` | — |
| B0A bridge/complétude | R3 livré, **revue finale non exécutée** | `df1bd8f`, intégré `66be3f8` ; 141 tests | régression C01 + revue finale Luna (BLOCKED_REVIEW_CAPABILITY) |
| B0B audit exports | PASS_WITH_CHANGES | `41c8869` + `5add5df`, intégré `a772cc9` ; 14 tests | PostgreSQL réel UNKNOWN |
| B1A1 admission | R2 parent-implemented, non revu | `2f88a42`, intégré `2cafb37` ; 127 tests | revue finale Luna + régressions acquisition/operator |
| B1A2 borne G2 | R2 + test croisé, non revu | `01346e2` + `5484375`, intégré `591eeca` ; 220 tests croisés | revue finale Luna |
| L1 limites | PASS_WITH_CHANGES (L06) ; P10 DEFER | `e4772fa`, intégré `0483f61` | P10 préexistant, lot dédié |
| A1 stratégie CTI | R4 livré, **revue finale non exécutée** | `4a71f45`, intégré `00a78b8` ; 63 backend + 3 frontend + typecheck | revue finale Luna |
| T Typst | PASS rapport ; V4 BLOCKED | `cae3115`, intégré `7dc4b0e` | validation éditoriale V4 ; font bundle absent |

## Prochaine action recommandée (00:50, Luna xhigh)

1. Démarrer une session fraîche Luna xhigh sur `chore/mission01-integration`.
2. Lui donner les 5 régressions ci-dessus + la branche WIP `849cfa0` en entrée.
3. Faire corriger C01 et les 3 régressions operator/acquisition avec tests
   ciblés, puis relancer la suite backend hors intégration.
4. Revues finales indépendantes Luna xhigh : B0A-R3 (`df1bd8f`), A1-R4
   (`4a71f45`), B1A1-R2 (`2f88a42`), B1A2-R2 (`01346e2`+`5484375`).
5. Seulement ensuite : B1B, L2, Q, `FINAL_AUDIT.md`. Ne pas déclarer PASS sans
   revue effective ; consigner `BLOCKED_REVIEW_CAPABILITY` pour tout gate non
   revu.

## Coûts

Coûts fournisseur et tokens par session : **UNKNOWN**. Aucun appel live.
