# Mission 01 V3.2 — handoff final

**Mis à jour :** 2026-10-24, intégration **`8fdb7a7`** (+ ce commit documentaire).
**Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`.
**Branche :** `chore/mission01-integration`.
**Statut de mission :** **PARTIAL**, clôturée avec réserves. Voir
`FINAL_AUDIT.md` pour la matrice complète. Aucun push ni merge sur `main`.

## Interdits maintenus

Aucun push/merge `main`, déploiement, accès live, appel fournisseur, donnée
CTI privée ni secret. Les artefacts Mission 00 restent en lecture seule dans
`/home/nill/perso/work/AutoWork/docs/references/mission00-2026-10-23/`.

## Résultats finaux vérifiés

- **Backend hors intégration** : `4602 passed, 34 failed, 2 skipped,
  30 deselected` sur `8fdb7a7`.
  - 32 échecs Typst : `CascadiaCode/ttf/CascadiaCode.ttf` absent
    (environnement, reproduit sur `058bb4a`).
  - 2 échecs préexistants : `test_confined_analysis_dispatch`
    (CAPA indisponible, parseur MWCP absent), reproduits sur `058bb4a`.
- **Frontend** : `293 passed`, `pnpm typecheck` OK.
- **Suite ciblée fusionnée** : `246 passed` sur `c15166e`.
- Les 5 régressions réelles sont fermées (`89eb3a5`, fusion `c15166e`,
  intégration `bbb9f32`) : hex court, alias C01 `T1`, retry d'acquisition,
  préflight advisor minimal, résultat `PARTIAL`.

## Verdicts indépendants finaux

| Lot | Verdict | Référence |
|---|---|---|
| S0 baseline | PASS (runtime `UNKNOWN`) | `BASELINE.md` |
| B0A | **FAIL/PARTIAL** : F1–F5 acceptés ; F6 arrêté après 3 tentatives (URL dans titre, percent sans `;base64`, base64 fragmenté) | `B0A_REVIEW.md`, `b149aa3` |
| B0B | PASS_WITH_CHANGES | `B0B_REVIEW.md`, `230380d` |
| B1A1 | **FAIL/PARTIAL** : protections principales confirmées ; équivalence sémantique résiduelle ; atomicité révocation/transport `BLOCKED` | `B1A1_REVIEW.md`, `4317f46` |
| B1A2 | PASS_WITH_CHANGES : plancher conservateur, `BridgeInvestigator.turn` hors borne, état ambigu en chaîne | `B1A2_REVIEW.md`, `8597053` |
| L1 | PASS_WITH_CHANGES (L06) ; P10 `DEFER` | `L1_REVIEW.md`, `bb7b8b4` |
| A1 | PASS_WITH_CHANGES ; résiduel MEDIUM `capture_sha256`/`next_pivots` | `A1_REVIEW.md`, `2e75d94` |
| T | Rapport PASS ; restauration V4 `BLOCKED` (validation éditoriale) ; pipeline réel `UNKNOWN` (font bundle) | `TYPST_REFERENCE_PARITY.md`, `cae3115` |

## Restes explicitement non faits

- **B1B** temporalité/reprise (réponse tardive, 429/Retry-After, timeout,
  pacer, `needs_review`, crash-reprise) : `PENDING`.
- **L2** pagination/keyset des sections de contexte : `PENDING`.
- **Q** éval anti-slop baseline vs patch : `PENDING`.
- **F6** : lot dédié de liste blanche stricte des champs d'évidence (titres,
  champs fragmentés, double encodage) ; ne pas reprendre l'heuristique actuelle.
- **B1A1** : clé d'effet canonique fournie par l'adaptateur (pas une heuristique
  de normalisation), et primitive atomique révocation/dispatch.
- **P10** : défaut préexistant de politique de divulgation, `DEFER` avec preuve
  des revues ; override retiré, défaut 96 000 restauré.
- **T** : validation éditoriale V4 et bundle de polices complet avant tout
  rendu final/G3.

## Recette de test

```sh
ln -sfn /home/nill/perso/work/AutoWork/backend/.venv <worktree>/backend/.venv
ln -sfn /home/nill/perso/work/AutoWork/.autowork-runs <worktree>/.autowork-runs
cd <worktree>/backend && PYTHONPATH=$PWD/src .venv/bin/python -m pytest <tests> -q --tb=short
```

Frontend : lier `frontend/node_modules`, `pnpm test --run`, `pnpm typecheck`.
Jamais `uv run`, Conda actif, `python`/`pytest` nus. Typst réel :
`TMPDIR=$HOME/tmp`.

## Coûts

Coûts fournisseur, tokens par session et coûts Paseo : **UNKNOWN**. Aucun reçu
fournisseur ni facture indépendante consulté. Aucun appel live.
