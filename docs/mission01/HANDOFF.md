# Mission 01 V3.2 — handoff d'orchestration

**Mis à jour :** 2026-10-23, intégration locale `599df9d`.
**Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`.
**Branche d'intégration :** `chore/mission01-integration`, worktree
`/home/nill/.paseo/worktrees/0y1qsxa1/chore-mission01-integration`.
**Interdits maintenus :** aucun push, merge sur `main`, déploiement, accès
live, appel fournisseur, donnée CTI privée, secret. Les artefacts Mission 00
restent en lecture seule dans `/home/nill/perso/work/AutoWork/docs/references/mission00-2026-10-23/`.

## Recette de test déterministe (worktrees)

```sh
ln -sfn /home/nill/perso/work/AutoWork/backend/.venv <worktree>/backend/.venv
ln -sfn /home/nill/perso/work/AutoWork/.autowork-runs <worktree>/.autowork-runs
cd <worktree>/backend
PYTHONPATH=$PWD/src .venv/bin/python -m pytest <tests> -q --tb=short
```

`PYTHONPATH=$PWD/src` est indispensable pour tester le code du worktree.
Jamais `uv run` (sélectionne Miniconda et tente un réseau), jamais Conda actif,
jamais `python`/`pytest` nus. Typst réel : `TMPDIR=$HOME/tmp`.

## État des lots

| Lot | État | SHA / preuve | Reste |
|---|---|---|---|
| S0 baseline | PASS (runtime UNKNOWN) | `e26c9a7` + `0074706`, intégré `48ba143` | — |
| B0A bridge/complétude | réparation R2 en cours | `9774680` → `fbc0f24` ; revue `09f1c38` FAIL | F6 hex legacy + `content` générique à borner ; F5 compte IOC omis |
| B0B audit exports | réparation R1 en cours | `289903b`, revue `63577f7` FAIL | projeter métadonnées `operator-advisor-call.v1`, `AnalystDecisionRow`, input pack ; libellé non-observation |
| B1A1 admission | réparation R1 en cours | `3a745de`, revue `5fb65f5` FAIL | BLOCKER preuve `pre_submission` forgeable ; HIGH empreinte sémantique ; test extension G2 réelle |
| B1A2 borne G2 | réparation R1 en cours | `458b96c`, fusion `02999f8` | conflit croisé `advisor_rate_limited` vs `advisor_unavailable` ; puis revue indépendante |
| L1 limites | PASS_WITH_CHANGES (L06) ; P10 DEFER | `e4772fa`, intégré `0483f61` ; revue finale `bb7b8b4` | P10 provenance/rejeu = défaut préexistant, lot dédié |
| A1 stratégie CTI | réparation R3 en cours | `a0aaee6`, revue `80d06ab` FAIL | HIGH `step_ids`/métadonnées identifiantes non validées ; LOW libellé verdict |
| T Typst | PASS rapport ; restauration V4 BLOCKED | `cae3115`, intégré `7dc4b0e` | validation éditoriale V4 ; bundle CascadiaCode absent ; pipeline réel UNKNOWN |

## Travaux non démarrés

- **B1B** : réponse tardive, 429/Retry-After borné, timeout, pacer, `needs_review`,
  crash-reprise, statut inconnu ; baser sur B1A2 final.
- **L2** : pagination/keyset des sections de contexte (`research_context_builder.py`,
  `operator_context_summary.py`) après stabilisation B0A ; conserver CAS et manifeste
  d'omissions, ne pas retirer les protections.
- **Q** : éval anti-slop baseline vs patch, métriques, diff de complexité.
- **FINAL** : matrice des tests réellement exécutés, restes `BLOCKED/UNKNOWN`,
  coûts UNKNOWN, branches/commits locaux, approbations requises.

## Décisions déjà prises

- **P10** : l'override de développement a été retiré après trois revues FAIL ;
  défaut 96 000 restauré. Le défaut de divulgation P10 (provenance d'invariants,
  rejeu de snapshot, provenance tronquée) est **préexistant** et `DEFER`.
- **B1A1 atomicité révocation/dispatch** : correctement `BLOCKED` (pas de verrou
  ou transaction partagée) ; ne pas prétendre l'avoir fermée.
- **B1A2 3 600/7 200** : la borne dure est implémentée et propagée ; le seuil
  minimal de 60 s est un choix d'implémentation à faire valider.
- **T** : ne pas restaurer `category`/numéro/`overview` sans validation éditoriale ;
  le test de pipeline réel reste `UNKNOWN` faute de bundle de polices complet.
- **B0A** : suppression du plafond renderer 96 k confirmée ; aucune réimplémentation
  Bridge, aucun reçu fournisseur inventé, mode fichier >200 k simulé seulement.

## Prochaine action recommandée

1. Attendre les cinq correctifs en cours ; relancer chaque revue indépendante.
2. Intégrer uniquement après verdict `PASS`/`PASS_WITH_CHANGES` sans HIGH/BLOCKER.
3. Lancer B1B puis L2, ensuite Q.
4. Rédiger `FINAL_AUDIT.md` avec les statuts réels ; ne convertir aucun `UNKNOWN`
   en `PASS`.

## Coûts

Coûts fournisseur et tokens par session : **UNKNOWN**. Aucun appel live.
