# Mission 01 V3.2 — handoff final de reprise

**Statut :** **PARTIAL**, clôturé le 2026-10-24. Voir `FINAL_AUDIT.md` pour
la matrice complète, les tests réellement exécutés, les verdicts et les
résidus.
**Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`.
**Branche :** `chore/mission01-integration`.
**État code vérifié :** `9523cc4c` (merges B1B, L2, A1, Q1) ; documentation
committée dans le lot suivant.
**Orchestrateur :** agent Paseo `3df5ffd3-3915-49b1-8308-cb00d26908f2`
(DeepSeek Flash max), unique parent de la reprise.

## Interdits maintenus

Aucun push/merge `main`, déploiement, accès live, appel fournisseur/CTI,
donnée CTI privée ni secret. Les artefacts Mission 00 restent en lecture seule
dans `/home/nill/perso/work/AutoWork/docs/references/mission00-2026-10-23/`.

## Lots acceptés dans cette reprise

- **B1B temporalité/reprise : PASS.** `e4850d2` → `fd397de` → `4604ac1` →
  `a94986f`, intégré `10b0e35`. Revue finale `772a15d5` PASS. Oracle 241
  passed, 294 passed/6 skipped élargi.
- **L2 pagination/keyset : PASS_WITH_CHANGES.** `ef1bc61` → `4f2a941` →
  `6143099` → `0c54ed9`, intégré `36eb5326`. Un MEDIUM résiduel documenté
  (catégorie d'omission reprise comme attendue, limite de 3 réparations).
  Oracle 342 passed, 556 combiné B1B+L2.
- **A1 résiduel provenance/type : PASS.** Résiduel `fbe5d36` + `bd35278` +
  `749a3f2`, intégré `69d3b904`. Revue finale `cdb8f69e` PASS. 79 passed,
  194 combiné.
- **Q1 éval anti-slop : PASS_WITH_CHANGES.** `dd925ff` → `e1779f8` →
  `a3976c3`, intégré `9523cc4`. 16 cas, SHA corpus `a73fdb9…684e`, baseline
  et patch 2 passed.

## Restes explicitement non acceptés

- **F6** : `BLOCKED/PARTIAL` après 3 tentatives. Branche `83aa615` non
  intégrée. 3 HIGH (chemin `dns.raw`, base64 avec espaces, budget décodé non
  cumulatif) + 2 MEDIUM ; voir `F6_REVIEW.md`.
- **B1A1 clé canonique** : `BLOCKED`. Seule `ModelRun run.id:aN` par tentative ;
  `operation_key` client + fingerprint heuristique. Ne pas rouvrir
  l'heuristique.
- **B1A1 atomicité révocation/dispatch** : `BLOCKED`. Branche
  `feat/mission01-b1a1-atomic-dispatch` non intégrée (`699200b` → `7af1d62` →
  WIP `32513ca`). La séquence SQL du service/dispatcher est correcte, mais le
  guard advisor repose sur des booléens forgeables ; un capability/lease non
  forgeable créé par le dispatcher est requis.
- **P10** : `DEFER` préexistant, aucun override ; garde incomplète.
- **Q2** : `DEFERRED/NO-GO offline` ; aucune preuve sémantique sans évaluation
  modèle autorisée.
- **T** : `BLOCKED_MISSING_REFERENCE` ; aucun bundle `CascadiaCode` complet
  dans le dépôt, `var/` non inspectable ; V4 non restauré.

## Porte finale

Suite backend complète sur l'intégration B1B+L2+A1+Q1 :
**4667 passed, 34 failed, 2 skipped, 30 deselected**. Sur `main` local fusionné
(`db9aa1d3`, non poussé) : **4668 passed, 33 failed, 2 skipped, 30
deselected** ; un test Typst (`w17`) passe sur main, aucun échec nouveau. Les
échecs restants sont les tests Typst (font bundle absent) + les 2
confined/mwcp préexistants sur `058bb4a`. PostgreSQL, transport Bridge réel et
providers restent `UNKNOWN`.

Frontend : **293 passed + typecheck rejoués sur `main` fusionné**.

**Merge local :** `main` @ `db9aa1d3` contient l'intégration acceptée (B1B,
L2, A1, Q1). F6 et B1A1 atomicité sont **exclus** et restent sur leurs
branches. `origin/main` reste `31ceeac` : aucun push effectué.

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

Coûts fournisseur, tokens par session et coûts Paseo : **UNKNOWN**. Aucun
reçu fournisseur ni facture indépendante consulté. Aucun appel live.
