# ADR-0008 — Routage ARTICLE/BRIEF par sujet et PublicationDocumentV7

Statut : accepté — 2026-11-05 (amende ADR-0006 sur les points ci-dessous)

## Contexte

ADR-0006 fait de V4 le contrat unique, rejette un branchement « brève » et fige
un pipeline statique. La mission m15 exige un format explicite par sujet.

## Décision

1. `PublicationKind` (`domain/publication_kind.py`) : `article` | `brief`.
2. Après `RELEVANCE_PROJECTION`, une décision immuable
   `ProductionRoutingDecisionV1` (`domain/production_routing.py`, table
   `production_routing_decisions`, migration `0008`) est calculée de façon
   déterministe à partir des artifacts canoniques exacts (jamais par LLM).
   Champs : decision_id, production_run_id, pipeline_generation,
   extraction_artifact_id/hash, relevance_artifact_id/hash, input_hash,
   policy_version (`ROUTING_POLICY_VERSION`), status (`DECIDED`|`NEEDS_REVIEW`),
   publication_kind (None si NEEDS_REVIEW), reason_code, confirmed_ioc_count,
   contextual_ioc_count, ambiguous_ioc_count, critical_source_diagnostics,
   decided_at, decision_hash. Unicité `(production_run_id, pipeline_generation,
   input_hash)` = idempotence ; même clé + hash différent = conflit explicite.
   `ProductionRun.routing_decision_id` pointe vers la décision applicable ;
   mise à jour par CAS (version attendue + génération) ; refus d'une décision
   d'une génération antérieure.
3. Règle : ≥1 IOC `CONFIRMED_IOC` rattaché au sujet (DIRECT/CORROBORATION, règles
   de `publication_builder`, IOC originaux seuls exclus) → ARTICLE ; sinon si
   extraction ET projection complètes (SUCCEEDED/REUSED, artifacts valides) →
   BRIEF ; sinon NEEDS_REVIEW avec code explicite. Le helper d'éligibilité est
   extrait de `publication_builder` (pas de duplication).
4. Graphe : `next_stage(stage, *, publication_kind=None)` ; après SYNTHESIS,
   ARTICLE → EDITORIAL_ENRICHMENT → ASSEMBLY ; BRIEF → ASSEMBLY (aucun artifact
   d'enrichissement factice). Avant décision, SYNTHESIS exige une décision
   DECIDED. Assembly/QA exigent selon le format. Un changement de route
   invalide uniquement SYNTHESIS et ses descendants.
5. `PublicationDocumentV7` enveloppe V6 (réutilise ses modèles) + `publication_kind`,
   `routing_decision_hash`, `routing_policy_version`. V4–V6 restent lisibles ;
   l'ancienne heuristique de longueur (`BRIEF_MAX_WORDS`) ne subsiste que pour
   les documents V≤6 sans format explicite.
6. Édition : un seul `EditionComposition`/`EditionDocumentV2`/Typst ; la
   classification lit `publication_kind` ; numérotation continue articles puis
   brèves.
7. Prompt dédié `BRIEF_SYNTHESIS_PROMPT_VERSION` via `ModelGateway`, mêmes
   contraintes TLP/`external_llm_allowed`.

## Propriété des fichiers (workers)

B : `domain/production_routing.py`, repository/ORM/UOW, migration 0008, helper
d'éligibilité IOC. C : `production_pipeline.py`, `production.py`,
`production_workflow.py`, `production_jobs.py`, `subject_production.py`,
`production_synthesis.py`. D : `publication_*`, `typst_rendering.py` (V7).
E : `edition_*`, `chpTypst/RENDERER/`. F : API et frontend.
