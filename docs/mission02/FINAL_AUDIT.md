# Mission 02 V2 — audit final

**Date :** 2026-10-24. **Périmètre :** intégration locale, documentation finale.
**Base mission/S0 :** `e4abba495d9f5fdc296e2ba80b65442855625474`.
**SHA du code intégré audité et du gate final :**
`d4c4c2ed836d60eef150a0ce80f48ae171cee8e0`.

## Résultats par lot

| Lot | Statut | Résultat vérifié |
|---|---|---|
| S1 — preuves permissives | `TESTED_FAKE`; revue `APPROVE` | Preuves CTI autorisées transmises intactes au transport factice; secrets et refus de politique conservés. |
| S2 — P10 sans plafond | `TESTED_FAKE`; revue `REVISE`, boucle bornée fermée | Cap 96 000 et troncatures silencieuses supprimés; résidu JSON échappé décrit ci-dessous. |
| S3 — dispatch/effect | `BLOCKED_DESIGN` | Revue indépendante: CAS/lock d'invocation au consume, portée du permis Bridge et provenance des résultats réutilisés à spécifier. Aucune implémentation. |
| S4A — PostgreSQL jetable | `TESTED_POSTGRES_DISPOSABLE`, partiel | 7 tests passés sur contexte opérateur, persistance d'investigation et concurrence d'invariants; export audit réel-DB `UNKNOWN`/TODO. |
| S4B — PDF de run | `INSPECTED_REAL_PDF_READ_ONLY` | Un sujet, Blinder Tunnel; aucun changement du run. |
| S5 — scénarios CTI | `TESTED_FAKE`, partiel | 16 cas anti-slop et oracles S1 passés; scénario complet avec/sans échantillon non rejoué; Q2 reste une hypothèse. |
| S6 — rendu Typst | `INSPECTED_REAL_PDF_READ_ONLY`, `RENDERED_OFFLINE`, `EDITORIAL_DECISION_REQUIRED`; `BLOCKED_FONTS=no` | Défaut de chronologie corrigé et couvert; 43 tests Typst passés. |
| S7 — code mort | `REPLAYED_TARGETED_TESTS` | Deux symboles supprimés après vérification d'absence d'appel interne; 215 tests rejoués. |

## Décisions et changements

- Politique appliquée : ALLOW par défaut pour les preuves CTI dans une
  consultation déjà autorisée. Restent contraignants les identifiants et
  secrets opérationnels AutoWork, URL signées, `do_not_submit`,
  `external_llm_allowed=false`, TLP RED, restrictions au niveau échantillon,
  refus d'appel externe et G2/G3. Les clés d'implant, jetons attaquants et
  éléments déjà publiés restent des preuves CTI.
- IDs, SHA, offsets, longueurs, provenance et idempotence sont conservés;
  aucune troncature silencieuse n'est introduite. `AGENTS.md` et
  `backend/AGENTS.md` cadrent cette règle.
- S1 : commits `892e5f45`, `6f40b125`, `50992eaf`; revue indépendante
  `S1_PERMISSIVE_REVIEW.md` approuvée après trois passes. Retrait des refus
  512 octets/16 384 octets/Base64/hexdump et de `content_omission_codes`;
  contrôles explicites des restrictions d'observation, d'analyse dérivée et
  de finding. Au faux transport arrivent 600 octets DNS `raw`, Base64
  espacé/fragmenté, deux valeurs de 300 octets, extraits >512/>16 384 et
  preuves contradictoires. Chemins secret/`POLICY_BLOCKED` préservés.
  Source +267/−331 (net −64 LOC); tests +522/−294.
- S2 : commits `4a13c5d4`, `d298d181`, `44c1bfac`, `b344a185`, `1246b25f`;
  `P10_PROMPT_VERSION=4`. Suppression du cap 96 000 et des coupes de
  contexte, chaînes, listes et candidats. `_external_disclosure_allowed`
  applique `external_llm_allowed`, `do_not_submit` et TLP RED; ajout d'un
  détecteur structurel de credentials. Oracles conservent les cas autour de
  95 999/96 000/96 001 et 199 999/200 000/200 001 octets, ainsi que les
  valeurs MiB. Source +91/−90; tests +347/−5.
- Résidu S2 (`S2_P10_REVIEW.md`) : une valeur credential dans du texte libre
  JSON échappé par antislash peut encore survivre (ex. guillemets échappés
  autour de `auth`). La revue finale le classe comme résidu de sérialisation
  échappée; boucle close avec ce point documenté, sans cascade de décodeurs.
- S3 : design `67f8c689`, revue `6391159c`, verdict `BLOCKED_DESIGN`. Le
  consume doit verrouiller/CAS l'état persisté de l'invocation (et de la
  tentative Bridge); portée du permis sur retries/correctifs et filiation
  des résultats réutilisés restent à définir.
- S6 : commit `b102a9e6`. Statuts : `INSPECTED_REAL_PDF_READ_ONLY=yes`,
  `RENDERED_OFFLINE=yes`, `BLOCKED_FONTS=no`,
  `EDITORIAL_DECISION_REQUIRED=yes`. La figure dérivée ne contenait que 4 des 9 entrées
  datées et remplaçait la chronologie complète. Le template rend désormais
  les entrées non représentées après la figure; le test échoue sans le fix et
  passe avec lui. Vérification du render canonique : 22/22 paragraphes
  narratifs, 6/6 titres de section et 49/49 valeurs IOC sont dans le PDF.
  Exhaustivité des figures/sections au-delà de ces contrôles : `UNKNOWN`.
- S7 : commit `76aefdee`; suppressions de `_source_figure_candidates` et
  `OperatorToolInvocationService.get_context_summary`, 54 LOC source.
- Delta net estimé des lots : environ −99 LOC source (S1 −64, S2 +1, S7 −54,
  template S6 +18); aucun nouveau module ni moteur de politique.

## Résultats de vérification

- Gate backend final sur le code intégré : **4708 passed, 2 skipped,
  30 deselected** en 124 s avec Typst 0.15.1 fonctionnel et polices du
  checkout principal. Résultats ciblés : S1 171; S2 135; S6 43; S7 215;
  audit/L2/B1B 66; PostgreSQL jetable 7.
- Mypy ciblé propre sur `invariant_proposals.py`, `advisor_dossier.py`,
  `investigation_advisor_tools.py`, `publication_secret_guard.py`. Mypy
  repo-wide : 58 erreurs préexistantes dans 11 fichiers. Ruff propre sur les
  fichiers modifiés; formatage repo-wide : 10 fichiers préexistants.
- Les 33 échecs historiques liés à l'environnement Typst sont résolus sur
  cet hôte avec le binaire Typst indiqué dans `HANDOFF.md`; ce constat est
  local à cet hôte. Sans cet environnement, 5 tests Typst échouent sur le
  snap cassé `/snap/bin/typst` (`snap-confine`).
- Une exécution S1 antérieure inscrite dans `TASKS.md` a 135 passes et un
  échec dû au corpus `.autowork-runs/` absent; le replay ciblé final S1 sur
  main compte 171 passes. Ce sont deux exécutions distinctes.

## PDF réel et limites

- `subject_id=d22822d4…`, `production_run_id=ec7a6807…`, render `99e2bfc4…`;
  renderer `publication-v14-typst-v32-literal-notes`, template
  `chp-article-v33-literal-analyst-notes`, Typst 0.15.1. PDF A4, 14 pages,
  296 369 octets, SHA-256
  `d4abc34524e0514692e686bc52cd344c0384ce14f77be7109d88e20027ce89e6`;
  `nature=base_publication`, `opinion=None`.
- Copie d'inspection hors dépôt :
  `~/.cache/autowork-mission02/blinder-publication-latest-clean.pdf`.
  Aucun PDF, PNG, bundle de polices ou contenu massif de bulletin n'est
  ajouté au dépôt.
- Aucun appel fournisseur, aucune mutation du run, aucun accès de test à un
  fournisseur live. S4A ne couvre pas l'export audit réel-DB; S5 ne couvre
  pas le scénario complet avec/sans échantillon; la conclusion Q2 reste une
  hypothèse. L'équivalence/idempotence côté fournisseur reste `UNKNOWN`.

## Décisions encore ouvertes

- S2 : traiter ultérieurement le résidu JSON échappé, avec oracle ciblé.
- S3 : réviser le design avant toute implémentation; préciser CAS/lock,
  portée Bridge sur retry/correctif et provenance des réutilisations.
- S4A/S5 : définir un scénario autorisé pour l'export réel-DB et le replay
  complet avec/sans échantillon; aucune connexion fournisseur n'est établie.
- S6 : décision éditoriale requise sur identité, emplacement des références
  et pagination de provenance. Complétude de toutes les figures/sections
  demeure `UNKNOWN`.
