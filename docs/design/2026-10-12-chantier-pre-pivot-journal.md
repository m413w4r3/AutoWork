# Chantier pré-pivot — journal, nettoyage, contrôle agentique et validation

Date : 2026-10-12. Baseline examinée : `806d214` (main). Branche de chantier :
`chantier/pre-pivot` (worktree `AutoWork_worktrees/pre-pivot`). Rien n'est poussé,
rien n'est fusionné dans `main`, rien n'est déployé, aucune base réelle n'a été
touchée, aucun appel à un fournisseur de modèle réel n'a été effectué.

## 1. Diagnostic initial vérifié

- HEAD au démarrage : `806d214`, identique à la baseline. Seule modification
  locale non liée : `typstPipeline/` ajouté à `.gitignore` (laissée en l'état).
- CI GitHub du 2026-10-12 sur `806d214` (run `38216840005`) : backend 14 échecs /
  3 243 passés ; frontend 1 échec E2E ; smoke Typst en échec.
- Classification vérifiée des échecs :

| Échec | Classe | Cause racine |
| --- | --- | --- |
| `test_lot34_repair_atomicity_postgres` ×2 | fixture incompatible | titre brut `LOT 34` refusé par le contrôle QA `[Groupe] Titre` |
| `test_production_source_additions` ×2 | fixture incompatible | édition réutilisée (même pays/période) avec un autre UUID → FK `subjects_edition_id_fkey` |
| `test_editorial_enrichment_pipeline_postgres` | fixture incompatible | faux compilateur de diagrammes à une ancienne version de politique ; tableau d'une ligne rejeté par `editorial_enrichment_table_single_row` |
| `test_pipeline_happy_path`, `test_synthesis_canonical` | assertion périmée | version du schéma de synthèse (2) et titre `[Groupe] Titre` |
| `test_pipeline_invariants` ×2 | fixture / assertion périmée | `effective_evidence_sha256` remplace `source_content_sha256` dans les requêtes batch ; un doublon de contenu ne répète plus les indicateurs de son représentant (`production_extraction.py:1247-1275`) |
| `test_production_state_v5_postgres`, `test_production_reuse_postgres` ×2 | fixture / collision de checkpoints | octets de capture non uniques sur la base de session partagée ; titres sans groupe |
| `test_typst_chp_parity`, `test_typst_compiler_runtime` | dépendance CI absente | `pdftoppm` (poppler-utils) non installé sur le runner |
| smoke Typst | fixture périmée | JSON inline sans `content_sections` (contrat renderer v19) |
| E2E `article-publication-workflow` | **vrai défaut d'affichage** + mocks périmés | les aperçus Synthèse et Publication n'affichaient pas `section.heading` |

Aucun test n'a été supprimé, aucune règle métier assouplie, aucun skip/xfail ajouté.

## 2. Journal des travaux

Sous-sessions : ChatGPT Luna (`openai-codex:gpt-6-luna`), effort xhigh, un
worktree par lot. L'orchestrateur a relu chaque diff, rejoué les tests sur la
branche d'intégration et appliqué les changements (les workers n'ont pas pu
commiter : limites de débit sur l'outil de commit).

| Lot | Objet | Résultat | Commits sur `chantier/pre-pivot` |
| --- | --- | --- | --- |
| A1 | échecs d'intégration pipeline | accepté, tests seulement | `d08c6c3` |
| A2 | réparations + additions de source | accepté ; **durci par l'orchestrateur** (codes pays aléatoires → allocateur déterministe) | `3a13e4a`, `bf96a67`, `2817b57` |
| A3 | poppler en CI, smoke Typst, E2E | accepté ; garde de contrat ajoutée | `f6fc51a`, `bdb8117`, `5840a3a`, `ee4bfe9`, `2381e96` |
| B | baseline Alembic unique | accepté | `6d9aee0` |
| C / C2 | audit puis suppressions prouvées | accepté (8 suppressions) | `0371486`, `94164a9` … `d2364a4`, `0effc27` |
| D | audit surfaces agentiques | accepté | `803baa9` |
| D1 | `control-view` lecture seule | accepté | `524600a` |
| D2 | erreurs de révision typées | accepté | `412e917` |
| E | audit inspection du livrable | accepté | `1d010bd` |
| E1 | API d'inspection PDF par hash | accepté | `f75d2b5` |
| E2 | mapping bloc + boucle de correction | accepté ; **cache partagé corrigé par l'orchestrateur** | `73b11ef`, `d112098` |
| — | README (workflow actuel) | orchestrateur | `8187ab6` |

Défauts trouvés par l'orchestrateur lors de la revue (non vus par les workers) :

1. Collisions de codes pays aléatoires dans `test_production_source_additions.py`
   (≈ 15 % d'échec par exécution complète) : remplacé par
   `reserve_edition_code()`, l'allocateur déterministe du projet.
2. Le fichier de sidecar de localisation d'E2 vivait dans le répertoire temporaire
   propre à chaque processus : une localisation produite par le worker était
   invisible pour l'API en Compose. Racine partagée `PDF_INSPECTION_CACHE_ROOT`
   (bind mount `./var/pdf-inspections`).
3. Le JSON inline du smoke CI dupliquait une constante de version : test de
   garde `test_ci_typst_smoke_fixture.py`.
4. Le README affirmait encore qu'aucun article principal n'était généré
   (l'auditeur n'avait pas détecté cette formulation) : réécrit.

### Décisions d'architecture

- Alembic est conservé avec une unique baseline (`0001_baseline`). Un bootstrap
  maison aurait dupliqué fonctions, triggers et contrôles sans simplifier.
- La projection de contrôle est une **route de lecture agrégée** (pas un client
  documenté) car 97 lectures sont nécessaires aujourd'hui pour 9 sujets et deux
  informations (stages d'un run historique, additions de source en attente) ne
  sont pas composables.
- L'inspection du PDF est dérivée du PDF identifié par SHA-256 ; ni PNG, ni
  localisation n'est une vérité persistée.
- Aucun nouvel endpoint de mutation : la correction passe par les canaux existants.
- Un échec de `typst query` (localisation) est journalisé en `warning` et n'invalide
  jamais le PDF vérifié.

## 3. Nettoyage

- **Migrations** : `0002_production_batch_pause` supprimée. Preuve : schémas
  « 0001 + 0002 » et « 0001 seule » identiques sur PostgreSQL 16 jetable
  (73 tables, 969 colonnes, 592 contraintes, 243 index, 10 fonctions,
  37 triggers ; dumps `pg_dump -s` normalisés de même SHA-256
  `97924484…18f9`). L'ORM contenait déjà `paused_at`, `paused_by`, le CHECK
  `paused` et le CHECK d'identité jointe. Nouveaux tests : identité de pause,
  refus d'un schéma incompatible (SQLSTATE 42P07), head == `0001_baseline`.
- **Code supprimé** (preuve par candidat : `2026-10-12-dead-code-removals.md`) :
  `french_typography.py`, `_ImageElementParser`, `mapping_to_tuple`,
  `assert_adapter_contract`, `ExtractionResult`, `hash_bundle_files`,
  `StructuredModelUnavailableError`, `_target_value_for_display`.
- **Conservé volontairement** : `normalize_discovery_output` (compatibilité
  `discovery-json-v1`), `analyst_handoff`, `invariant_proposals`, goodware,
  `sample_acquisition`, reprise/réconciliation, panneau frontend de
  conversations (non monté, peut servir au pivot).
- **Documentation** : README (workflow courant), notes sur la baseline unique,
  bandeau HISTORIQUE sur le plan de récupération du 2 octobre.
- **Risques résiduels** : l'analyse statique n'a pas pu utiliser Vulture/Knip ;
  les kinds de jobs persistés en base n'ont pas été inspectés.

## 4. Contrôle agentique

Matrice complète des capacités existantes (endpoints, identifiants, gardes,
limites) : `2026-10-12-agentic-control-audit.md` §1. Améliorations
implémentées :

| Besoin | Changement | Documentation avec exemples exacts |
| --- | --- | --- |
| Vision globale bornée | `GET /api/editions/{edition_id}/production/control-view?cursor=&limit=` et `GET /api/production/runs/{run_id}/control-view` (`agent-control.v1`) | `2026-10-12-agent-control-view.md` |
| Erreurs de révision prévisibles | `503`/`409` typés pour le transport Bridge (`retryable` seulement si `ModelRun.submission_state == NOT_SUBMITTED`), `refusal_reason` + `next_safe_action` distincts de `recommended_restart_stage` | `2026-10-08-targeted-revision-agent-interface.md` |
| Inspection du PDF rendu | `GET /api/pdf-inspections/{pdf_sha256}`, `.../pages/{page}.png`, `.../pages/{page}/thumbnail.png` ; en-tête `X-PDF-SHA256` sur le PDF de preview d'édition | `2026-10-12-pdf-inspection-api.md` |
| Défaut → cible canonique | `mapping.items[].target` (subject, artifact id/version/input_hash, `block_type`, `block_key`) pour tables, diagrammes, graphiques, figures et titre ; diagnostic `block_too_small` localisé | idem |

Garde-fous : lecture seule (aucune écriture, aucun job, aucun appel modèle, aucun
blob lu pour la vue de contrôle) ; listes bornées (25 sujets, 20 éléments) ;
actions `permitted: "unknown"` quand une précondition n'est pas décidable en
lecture ; sous-processus Poppler sans shell, délai 15 s, limites mémoire/CPU,
concurrence 2, profils fixes ; la correction n'emprunte que les routes typées
existantes, avec fences d'artefact. Les décisions humaines (sources critiques,
acceptation, release) restent à des endpoints distincts.

Limites connues : pas de contrôle TLP propre sur les routes d'inspection (même
règle que les routes PDF existantes) ; le mapping ne couvre pas paragraphes,
sections ni cellules ; une localisation perdue n'est régénérée qu'au prochain
compile ; l'identité reste `dev-analyst` (pas d'authentification de production) ;
estimation du coût d'un retry générique non implémentée.

## 5. Démonstration « production → inspection → correction → nouveau rendu »

Test `tests/integration/test_agentic_correction_loop_postgres.py` (PostgreSQL
réel jetable, vrais services, modèles simulés) ; description complète et
preuves dans `2026-10-12-correction-loop-demo.md`.

1. Pipeline simulé jusqu'à `PUBLICATION` READY avec un diagramme et un tableau à clé.
2. Inspection : hash du PDF, nombre de pages, PNG avant, diagnostics.
3. Défaut déterministe `block_too_small` (diagramme 30×10) avec `block_key` et cible.
4. Cible → opération `delete_diagram` ; preview : 0 appel modèle,
   REFERENCES/EXTRACTION/RELEVANCE_PROJECTION/SYNTHESIS préservés.
5. Apply fencé (id, version, input_hash, hash canonique, clé, opération).
6. Seuls EDITORIAL_ENRICHMENT et PUBLICATION obtiennent de nouvelles identités ;
   aucun `ModelRun` ajouté.
7. Nouveau PDF (nouveau SHA-256), l'ancien reste résolu ; PNG avant/après
   conservés ; diagnostic disparu ; le tableau est remappé.
8. Un re-rendu réutilise la sortie (0 compile, 0 appel modèle) ; rejouer la fence
   périmée est refusé (conflit typé, aucune écriture).

## 6. Rapport de validation (HEAD final de `chantier/pre-pivot`)

Commandes réellement exécutées par l'orchestrateur sur la branche d'intégration :

- `pytest` backend complet (CI-like : PostgreSQL jetable, Typst/D2 locaux,
  polices copiées, poppler local) : 3 293 passés / 0 échec sur le HEAD final.
- `make test-integration` : 252 passés.
- `make lint-backend`, `make typecheck-backend` : OK (538 / 516 fichiers).
- Frontend : `pnpm lint`, `pnpm typecheck`, `pnpm test --run` (261 tests),
  `pnpm build`, `pnpm exec playwright test` (11 tests) : OK.
- Smoke Typst (JSON inline de `ci.yml` compilé avec le Typst local, 1 page),
  `scripts/typst_edition_smoke.py` (exit 0), `docker compose config --quiet` : OK.

Scénarios obligatoires :

| Scénario | Preuves (tests rejoués) |
| --- | --- |
| S1 contrôle de production | `test_production_control_view_postgres.py`, `test_production_control_api.py`, `test_production_pacing_recovery.py`, `test_production_api.py` — 137 passés (batch running/paused, run READY / NEEDS_REVIEW, job échoué, run historique) |
| S2 correction éditoriale | `test_production_synthesis_application.py`, `test_production_editorial_enrichment_application.py`, `test_targeted_revision_transport_postgres.py` — 443 passés (cible, preview, apply, lineage) |
| S3 correction visuelle | `test_agentic_correction_loop_postgres.py` + `test_pdf_inspection.py` — 15 passés |
| S4 invariants | même boucle (fence périmée), refus `revision_not_applicable` sur preuves insuffisantes, `test_production_source_additions.py`, `test_lot34_repair_atomicity_postgres.py` — 22 passés |
| S5 coût et reprise | même boucle (rerender sans compile ni modèle ; delete = 0 appel ; seuls ENRICHMENT/PUBLICATION recalculés) |
| S6 bootstrap vierge | `test_migrations.py`, `test_alembic_chain.py` — 86 passés |

Non vérifié / non exécuté :

- La CI GitHub n'a pas été relancée (aucun push autorisé) : l'étape `apt` de
  poppler sur le runner et le job `typst-renderer-smoke` (build Docker avec
  Poppler) ne sont validés que localement ou par analyse.
- Aucune validation visuelle sur le livrable réel (`CN2609`) au-delà de l'audit
  de référence ; aucun run réel, aucun Bridge.
- Le restamp Alembic de la base réelle n'est pas appliqué (voir §7).
- Le comportement Compose du cache partagé (`./var/pdf-inspections`) n'a pas
  été exercé avec la pile réelle.

## 7. Conclusion

**Avant toute remise en service de la pile** : la base réelle est estampillée
`0002_production_batch_pause`. Le service `migrate` (`alembic upgrade head`)
échouera tant que le restamp n'est pas fait. Procédure préparée, non appliquée,
qui requiert l'accord explicite de l'utilisateur :
`2026-10-12-baseline-collapse-live-db-note.md`.

**Préparation au pivot** : bonne. Le chemin produire → observer → corriger →
vérifier est démontré. Les points d'extension (provenance, identité de
l'analyste, décisions humaines, artefacts versionnés, révision ciblée) sont
intacts. Couplages à lever avant l'« avis de l'analyste » :

- `publication_qa.py:311-314` impose la liste exacte et ordonnée des sections
  (`references`, `synthesis`, `technical_annex`) : une section d'avis exigera
  d'étendre ce contrôle.
- Les validateurs d'ancrage de la synthèse rejettent toute affirmation non
  étayée par une preuve ; un avis d'analyste devra être un type de bloc
  distinct, hors de ces validateurs, avec sa propre provenance.
- `TargetedEditorialRevisionTargetKind` énumère les cibles (titre, chapeau,
  section, paragraphe, élément d'enrichissement) : un nouveau type de contenu
  demandera sa cible et ses opérations.
- Le renderer Typst échoue volontairement (`panic`) sur un type de bloc inconnu.
- L'identité est un fournisseur local (`dev-analyst`), non une authentification.

**À trancher avant le pivot** : modèle d'identité et d'authentification des
analystes ; statut éditorial d'un avis (hors preuve primaire) ; place dans le
manifeste et la release.

**Recommandations différées (valeur / coût)** :

1. Recalcul à la demande de la localisation (`typst query` seul, sans
   recompiler le PDF) — valeur haute, coût moyen.
2. Prévisualisation du plan d'un retry générique (stage minimal, invalidations,
   appels attendus) — valeur moyenne, coût moyen.
3. Contrôle d'accès/TLP explicite sur les routes d'inspection — valeur moyenne,
   coût faible.
4. Mapping des paragraphes et cellules (identité métier stable à introduire) —
   valeur moyenne, coût élevé.
5. Outils statiques (Vulture, Knip) en CI pour le code mort — valeur faible,
   coût faible.
