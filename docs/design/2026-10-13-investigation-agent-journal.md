# Journal — agent d'investigation et « Avis de l'analyste » (2026-10-13)

Spécification : `nimbalyst-local/reference/AutoWork_prompt_maitre_Nimbalyst_Luna_xhigh_2026-10-13.md`
(non versionnée). Ce journal consigne ce qui est **réellement implémenté, branché et vérifié**.

## Ce qui existe dans le produit

| Capacité | Où | Invocation produit |
|---|---|---|
| Boucle agentique pilotée par le Bridge (tours JSON typés, registre d'outils, budgets multi-dimensionnels, reprise idempotente) | `application/investigation_service.py`, `bridge_investigator.py`, `research_context_builder.py`, `investigation_tools.py` | `POST /api/subjects/{id}/investigations` + job `investigation_run` |
| Enveloppe G2 (fournisseurs, budget, acquisition, divulgation) ; qualification G3 humaine | `investigation_governance.py`, `investigation_acquisition*.py` | `POST …/envelope/approve\|extend\|revoke`, `POST …/samples/{id}/qualify` |
| Outils VT (`vt.file_report`, `vt.relationships`, `vt.intelligence_search`, `vt.behaviour`, `vt.download_sample`) | `investigation_vt_tools.py`, `virustotal*.py` (port étendu selon l'audit vtUI) | registre composé par `investigation_composition.py` |
| Outils Shodan passifs (`shodan.host/search/domain/count`) + archive brute | `shodan.py`, `investigation_shodan_tools.py` | idem |
| Analyse locale (`sample.analyze`, `local.run_command`) et Ghidra en lecture seule (`ghidra.*`) | `local_analysis.py`, `investigation_local_tools.py`, `ghidra_mcp.py` | idem |
| RuleWorkbench YARA natif (`rule.propose/test/revise/compare/get`), runner isolé | `rule_workbench.py`, `yara_runner*.py`, `investigation_rule_tools.py` | idem |
| Preuves typées, findings versionnés, nouveauté recalculée côté serveur | `evidence_recorder.py`, `investigation_findings.py`, `article_novelty.py` | boucle |
| Avis : brouillon → révision (CAS) → approbation d'une version exacte → publication `PublicationDocumentV6` | `analyst_opinion_service.py`, `analyst_publication_service.py`, `publication_augmentation.py` | `…/opinion/*`, `POST /api/opinions/{id}/publish` |
| Rendu : PDF d'article et édition composée multibatch ; repin humain explicite | `typst_rendering.py`, `edition_typst_rendering.py`, `chpTypst/RENDERER/*` | composition existante |
| Revue humaine et archive des règles approuvées liées au manifeste | `detection_review_service.py`, `edition_rule_archive.py` | `POST /api/detections/{id}/approve\|reject` |
| UI : onglet Enquête / Avis de l'analyste (G2, G3, analyse ciblée, avis, règles) | `frontend/src/features/investigation/**` | page sujet |

## Démonstrations D1–D7 → preuves exécutées

| D | Preuve (PostgreSQL réel, services réels, Bridge/fournisseurs factices, aucun réseau) |
|---|---|
| D1 découverte | `workflow_emulator/test_w12_*` (S2 `new_to_article`, limite « absence mondiale » imposée en code, S1 jamais nouveau, S2 `candidate` jusqu'à G3) ; `integration/test_investigation_vertical_postgres.py` |
| D2 chaîne d'infection | W12 : transition `dropped_files`/`execution_parents` étayée ; rôle final laissé hypothèse |
| D3 affirmation | W12 : C1 `qualified` avec preuves favorables et contradictoires ; `supported` sans observation rétrogradé |
| D4 infrastructure seule | `test_w13_*::d4` : IOC IP seulement, `shodan.host`, limites d'attribution, capacité VT absente locale |
| D5 détection évolutive | `test_w13_*::d5` : v1 → v2 différente, `rule.compare`, règle bruitée non publiée, avis publiable, archive liée au manifeste |
| D6 publication progressive | W11, W12, J2 navigateur : avis approuvé sans repin ⇒ manifeste/PDF v1 inchangés ; repin humain ⇒ manifeste V2 + PDF v2 avec la rubrique pour A seulement |
| D7 reprise / autonomie | `test_w13_*::d7` : doublon de tour dédupliqué, tour Bridge ambigu non resoumis, budget épuisé ⇒ refus de l'opération visée seulement, quota ⇒ `unavailable` local |

Navigateur réel (Playwright + FastAPI + PostgreSQL jetable + Typst/Poppler) :
`make test-workflow-browser-investigation` (parcours complet G2 → G3 → avis → publication V6 → repin → PDF,
vérification SQL des hashes historiques et du texte PDF).

## Validation globale (2026-10-13)

| Commande | Résultat |
|---|---|
| `make lint-backend` / `make typecheck-backend` | verts (668 fichiers formatés, 645 sources mypy) |
| `make test-backend` | 3384 passés |
| `make test-integration` (suite complète PostgreSQL) | 305 passés |
| `tests/integration/test_investigation_vertical_postgres.py` | 3 passés |
| `make test-workflow` / `make test-workflow-full` | 10/10 et 16/16 (Typst/Poppler réels) |
| `make test-workflow-browser` (W10) / `…-investigation` (J2) | verts |
| frontend `pnpm test --run` / `lint` / `typecheck` | 285 tests, verts |
| Playwright (API simulée) `pnpm exec playwright test` | 13/13 |

## Décisions et défauts trouvés à l'intégration

- **G2 / G3** : G2 = approbation d'enveloppe (`PIVOT_APPROVE`), l'acquisition d'un hit est automatique dans cette
  enveloppe ; G3 = qualification humaine (`MEMBER_VALIDATE/REJECT`), jamais automatique.
- **Stages** : `AnalystInvestigationStage` et `max_cycles` ne bloquent plus ; « 0 nouveau membre » n'épuise plus
  l'enquête (test unitaire mis à jour : changement de comportement voulu §2.2).
- **Défauts réels corrigés** : conflit d'écriture de l'agrégat pendant une acquisition ; commit durable avant tout
  appel fournisseur ; relecture de l'enveloppe G2 avant chaque appel externe (révocation concurrente) ; réponse Bridge
  invalide au schéma (tour correctif borné) ; réconciliation explicite d'une invocation `needs_review` ; nouveauté
  non fiable proposée par le Bridge recalculée côté serveur (hash de narration inclus) ; `supported` exige une
  observation sur l'objet visé ; verdicts français (`ClaimVerdict`) ignorés par le service d'avis ; CHECK `jobs`
  dupliqué dans la baseline ; **console de composition en lecture seule quand une version plus récente est
  disponible** (empêchait le repin par l'UI) ; l'API d'artefact de publication rejetait V6.
- **Revues indépendantes** (lecture seule) : sécurité et robustesse ; constats traités sauf ceux documentés ci-dessous.
- **Baseline** : `0001_baseline` mutable enrichie (tables d'investigation, types de job, décisions de détection) ;
  **aucun** stamp, ALTER ni accès à la base live.
- **Dépendance ajoutée** : `yara-python==4.5.0` (groupe `analysis`, lock mis à jour), identique au CLI hôte.

## Limites connues et points non vérifiés

- **Aucun appel fournisseur réel** : VT/proxy vtUI, Shodan, Bridge, GhidraMCP n'ont pas été contactés. Les endpoints
  HTTP de GhidraMCP et sa version compatible sont **à confirmer** par un humain (chemins vides par défaut).
- **Identité** : l'application entière utilise `LocalIdentityProvider` (`dev-analyst`) ; un déploiement exposé doit
  installer un fournisseur authentifié avant d'accepter G2/G3/avis/règles comme décisions humaines
  (`docs/investigation_operations.md`).
- **Envoi au Bridge sans G2** : suit la politique de la source (`external_llm_allowed`, TLP, `do_not_submit`),
  réévaluée à chaque tour ; G2 gouverne VT/Shodan/acquisition/volume.
- **Confinement du runner local** : seccomp réseau en repli ; Bubblewrap (réseau + fichiers) préféré quand il
  fonctionne ; `local_analysis_require_filesystem_confinement` vaut `False` par défaut — à activer en production.
- **Pertinence sémantique** d'une observation citée : vérifiée par identité d'objet, pas par compréhension ; la
  revue humaine de l'avis reste la barrière.
- **Base live** : les nouvelles tables nécessitent le gate de stabilisation de la baseline (décision séparée) ;
  rien n'a été appliqué.
- **Non fait / hors périmètre** : push, déploiement, opération sur la base réelle, approbations analyste.
