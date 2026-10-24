# Mission 02 V2 — registre de lots

**Base locale :** `main@e4abba495d9f5fdc296e2ba80b65442855625474` (2026-10-24).
**Politique :** ALLOW par défaut pour les preuves CTI dans une consultation déjà autorisée ;
les restrictions explicites (secrets, `do_not_submit`, `external_llm_allowed=false`, TLP,
autorisation d'appel, G2) restent actives. Aucun push, déploiement ou mutation live.

| TASK_ID | Dépendances | Oracle | Propriétaire | État |
|---|---|---|---|---|
| S0 état initial | — | HEAD propre, 4 références Typst présentes, PDF réel identifié | parent | DONE |
| S1 preuves permissives | S0 | preuves autorisées intactes au faux transport ; secrets/refus toujours bloqués | Luna xhigh `M02-S1` | DONE `892e5f45` ; revue indépendante en cours |
| S2 P10 sans plafond | S0 | 95 999/96 000/96 001, 199 999/200 000/200 001, MiB intacts ; idempotence | Luna xhigh `M02-S2` | DONE `d298d181` ; revue REVISE traitée, re-revue en cours |
| S3 dispatch/effect identité | S0 | spec design + verdict IMPLEMENTABLE/BLOCKED | Luna xhigh `M02-S3` | DESIGN `67f8c689` ; revue indépendante **BLOCKED_DESIGN** `6391159c` |
| S6 PDF réel vs 4 références | S0, S4B | `TYPST_REAL_PDF_AUDIT.md`, corrections démontrées testées | Luna xhigh `M02-S6` | DONE `b102a9e6` ; 1 défaut démontré corrigé (chronologie) |
| S7 code mort | S0 | inventaire preuve + suppressions atomiques testées | Luna xhigh `M02-S7` | DONE `76aefdee` ; 215 tests rejoués par le parent |
| S4B PDF run (lecture) | S0 | PDF copié hors dépôt, SHA vérifié | parent | DONE |
| S4A PostgreSQL jetable | S1/S2/S3 | tests ciblés sur base jetable | parent | TODO |
| S5 scénarios CTI | S1/S2 | scénarios avec/sans échantillon, Bridge alimenté | parent/reviewer | TODO |

## Faits de revue (2026-10-24)

- **S1** : suppression nette de 697→183 lignes sur le lot ; 600 octets DNS
  `raw`, Base64 espacé/fragmenté, deux valeurs de 300 octets, extraits
  >512/>16 384 et preuves contradictoires atteignent le transport factice ;
  refus secret/`POLICY_DENIED` conservés. Tests parent : 135 passed,
  1 échec uniquement dû au corpus `.autowork-runs/` absent du worktree.
- **S2** : plafond 96 000 supprimé, troncatures silencieuses retirées,
  `P10_PROMPT_VERSION` passé à 4. La revue indépendante a confirmé le no-cap
  mais trouvé trois défauts (gate `do_not_submit`/TLP, `auth=` non rédigé,
  perte du contexte autour d'une URL signée) ; correctif `d298d181` + tests.
- **S3** : la revue indépendante conclut **BLOCKED_DESIGN** (CAS de l'état
  d'invocation au consume, portée du permis Bridge multi-egress, provenance
  des résultats réutilisés). Aucune implémentation B1A1 dans cette mission.
- **S6** : PDF réel `INSPECTED_REAL_PDF_READ_ONLY` (SHA `d4abc345…`, 14 pages,
  Typst 0.15.1), rendu synthétique V4/V5/V6 hors ligne réussi avec les polices
  du checkout principal. Défaut démontré par le parent : 5 des 9 entrées de
  chronologie étaient invisibles ; corrigé dans le template et couvert par un
  test Typst réel (43 passed).
- **S7** : deux suppressions prouvées (`_source_figure_candidates`,
  `get_context_summary`), 54 LOC source en moins ; 215 tests ciblés rejoués
  par le parent.


## S0 — faits vérifiés (2026-10-24)

- `git status` propre, HEAD `e4abba49` (`codebase-desing skill`), au-dessus de `dbe3308`.
- Les quatre répertoires `docs/references/{RU2609_typ,chpTypst,CN2609_typ,typstPipeline}`
  existent sur le checkout hôte (gitignorés, absents des worktrees).
- Run réel, lecture seule, UN sujet : « Blinder Tunnel », `subject_id=d22822d4…`,
  `production_run_id=ec7a6807…`, render `99e2bfc4…`.
  PDF : `publication-v14-typst-v32-literal-notes`, template
  `chp-article-v33-literal-analyst-notes`, `chp-fonts-v1`, Typst 0.15.1,
  14 pages A4, 296 369 octets, SHA-256
  `d4abc34524e0514692e686bc52cd344c0384ce14f77be7109d88e20027ce89e6`.
  Copie locale hors dépôt : `~/.cache/autowork-mission02/blinder-publication-latest-clean.pdf`.
  Statut : `INSPECTED_REAL_PDF_READ_ONLY` — ce n'est pas un bulletin final validé.
- Aucune mutation du run, aucun appel fournisseur.

## Règles d'intégration

- Parent : vérifie le diff et rejoue les oracles avant merge local sur `main`.
- Chaque lot validé est mergé séparément ; un lot FAIL/BLOCKED reste sur sa branche.
- `docs/mission02/` ne contient que ce registre, `FINAL_AUDIT.md`, `HANDOFF.md` et les
  petits rapports ciblés (`TYPST_REAL_PDF_AUDIT.md`, `S3_DISPATCH_DESIGN.md`,
  `S7_DEAD_CODE.md`).
