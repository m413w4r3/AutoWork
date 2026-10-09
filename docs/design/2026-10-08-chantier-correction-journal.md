# Chantier de correction structurelle — matrice et journal

Commit de départ : `f92cc18` (main, arbre propre). Audit : `2026-10-07-audit-production-courante.md`.
Référence éditoriale : `docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf`
(le chemin `…CN-2609.pd` sans `f` n'existe pas ; le `.pdf` est la référence canonique).

## Matrice de travail (diagnostic au 2026-10-08)

| Issue | État du code | Toujours présent ? | Correction | Couches | Dépend de | Stage minimal |
| --- | --- | --- | --- | --- | --- | --- |
| G3 / 6.1 headings | Prompt `production_prompts.py:57` interdit les titres ; parseur `production_synthesis.py:396-411` force `heading=""` + warning `synthesis_section_heading_dropped` ; `publication_builder.py:167` force `heading=""` ; `publication_qa.py:254` rejette tout heading ; Typst `publication_helpers.typ:169` ne rend que `SYNTHÈSE` + blocs | **Oui, perdu à chaque couche** | Chantier A (chaîne unique) | prompt → parseur → domaine → builder → QA → publication_document → Typst | — | SYNTHESIS → … (headings perdus en amont) |
| G2 timeline | `publication_builder.py:796` `include_timeline=False` | Oui | Décision produit D (hors périmètre mission) | builder | A | R0 |
| T1/T2/T3 titres | Prompt `synthesis-draft-v10-actor-grounded-title` déployé, règle de repli déployée | Probablement corrigé, **à valider sur artifacts** | Validation uniquement ; correction ciblée du titre via canal E | prompt | — | titre : revision ciblée |
| S1 / 6.3 doublons références | `domain/production_references.py` dédoublonne **seulement par URL canonique** ; aucun usage de `content_sha256`/`duplicate_content` à la publication | **Oui** | Chantier B | references domain + publication_builder (liste lecteur) | A (même `publication_builder.py`) | PUBLICATION seul (R0) |
| S2/S3/S4 / 6.4 criticité | `ProductionRepairAction.CONTINUE_WITHOUT_SOURCE` existe, aucun concept de criticité (grep `criticality` : 0 résultat) | **Oui** | Chantier D | domain/production.py, production_repairs.py, workflow | — | REFERENCES/EXTRACTION concernés |
| C1/C2 / 6.6 tableaux IOC | Seul garde-fou : prompt. Aucun validateur déterministe | **Oui** | Chantier C | enrichment domain/application | A (ancrage sections) | EDITORIAL_ENRICHMENT (ou revision ciblée) |
| D2 / 6.5 diagrammes triviaux | Prompt ligne ~4531 `NO USEFUL ENRICHMENT` existe ; aucun seuil/validateur déterministe sur la valeur analytique | **Oui** (partiel) | Chantier C | idem | A | idem |
| D1 chevauchement SVG | Non audité dans ce lot | À vérifier | hors scope initial | diagram compilation | — | — |
| P1 adoption d'artifacts compatibles | Ouvert | Oui | Hors scope sauf invalidation minimale E | — | E | — |
| 6.7 sujets minces | Pas un défaut ; couvert par E (ajout ciblé de section) | — | Chantier E | — | A, C | — |
| 7 canal de revision | **Existe pour l'enrichment seulement** : `ProductionEditorialEnrichmentRevisionService` (`production_enrichment_revision.py`) : base_artifact_id/version/hash, identité déterministe, action/instruction, rebuild publication. Aucun équivalent pour la synthèse (titre/section/paragraphe), pas de mode `allow_source_extension`, lineage partiel | Partiel | Chantier E : généraliser, ne pas dupliquer | domain/application revision, synthesis, bridge, reconciliation, invalidation | A, C | — |

## Graphe de dépendances et parallélisation

```
Vague 1 : A (headings)  ||  D (criticité des sources)
Vague 2 : B (dédup refs)  ||  C (qualité enrichment)      — après A
Vague 3 : E (revision ciblée)                               — après A, C
Vague 4 : F (interface minimale)                            — après E
```

Vague 1 :

```
Chantier A files: production_prompts.py (SYNTHESIS_*), application/production_synthesis.py,
  domain/production_synthesis.py, publication_builder.py, publication_assembly.py, publication_qa.py,
  domain/publication_document.py, chpTypst/RENDERER/*, frontend/src/api/production.ts (types)
Chantier D files: domain/production.py, domain/production_references.py (criticité),
  application/production_repairs.py, application/production_workflow.py, application/production_references.py
Overlap: none (D ne touche pas synthèse/publication/Typst ; A ne touche pas repairs/workflow)
Shared invariant: none (D = sort d'une source inaccessible ; A = structure du texte publié)
Safe to parallelize: yes (worktrees séparés)
```

B est séquencé après A (même `publication_builder.py`). C est séquencé après A (les enrichissements
s'ancrent sur les sections de synthèse dont A change la représentation). E après A et C
(modèles de synthèse + validateurs d'enrichment réutilisés).

## Journal

(complété au fil des chantiers)

### Chantier A — titres de synthèse (revu, commit 5ce0c4b sur `worktree/ageless-brook`)
- Cause racine confirmée : titres supprimés à chaque couche (prompt, parseur, builder, QA, Typst).
- Worker : openai-codex:gpt-6-luna xhigh. Séquentiel avec E ; parallèle avec D.
- Revue superviseur : chaîne complète OK ; défaut trouvé — un titre invalide/vide/dupliqué rejetait toute la section (perte de claims valides). Correction demandée et appliquée : section conservée sans titre + warning `synthesis_section_heading_dropped_{invalid,empty,duplicate,mispositioned}` ; validation stricte conservée côté domaine/QA.
- Vérifié par le superviseur : 448 tests ciblés passés (dont Typst réels, TMPDIR=$HOME/typst-tmp), `make lint-backend` OK, `make typecheck-backend` OK.
- Validation réelle : PDF Mirage/NodeRabbit/PollCat avec sous-titres rendus ; brève GTG-30004 inchangée (titres injectés à titre illustratif, aucun appel modèle).
- Invalidation : REFERENCES / EXTRACTION / RELEVANCE_PROJECTION valides ; SYNTHESIS à recalculer pour produire des titres (prompt v11, contrat v4, parseur v5, validateur v6) ; ENRICHMENT/PUBLICATION suivent.
- Rapport final du worker non reçu (deux relances) ; revue faite sur le diff.

### Chantiers B (références) et C (enrichissement) — lancés en parallèle
```
Chantier B files: publication_builder.py (références), éventuellement publication_assembly.py, domain/production_references.py, tests
Chantier C files: domain+application production_editorial_enrichment.py, constantes EDITORIAL_ENRICHMENT_* de production_prompts.py, enrichment_revision (si nécessaire), tests
Overlap: none (production_prompts.py : régions disjointes)
Shared invariant: none
Safe to parallelize: yes (worktrees séparés, base = commit A)
```

### Chantier B — dédup références (revu, commit 4a6287d sur la branche du worktree frosty-delta)
- Règle : fusion uniquement sur `content_sha256` identique ou checkpoint partagé avec état `duplicate_content` ; représentant déterministe (tier, absence de query, longueur d'URL, ordre lexical) ; lignage d'évidence conservé sur le représentant ; `ASSEMBLY_POLICY_VERSION` = 9 (seule PUBLICATION à reconstruire).
- Vérifié par le superviseur : tests publication + Typst réels OK, lint OK, typecheck OK.
- **Constat réel (lecture seule, sujets 4 et 7)** : `…report` et `…report?page=1` ont des `content_sha256` DIFFÉRENTS, des checkpoints différents, pas de `duplicate_content`, et des notes de synthèse différentes : ce sont deux pages distinctes d'un même rapport paginé, pas un doublon. Le diagnostic S1 de l'audit était donc faux pour ces sujets ; la règle (ne pas supprimer `page=1`) est confirmée. Aucune fusion n'a lieu — comportement correct. Sujet 6 : reconstruction impossible en l'état (hash de synthèse ≠ extraction canonique courante, artifacts du run non alignés).
- Résiduel (présentation, pas un défaut de dédup) : deux lignes de référence quasi identiques (même éditeur/titre/date) pour deux pages différentes d'un rapport ; envisager de libeller la page (« page 2/n ») dans la note de référence. Décision produit, non implémentée.
- Rapport final du worker non reçu ; revue faite sur le diff et les données réelles.

### Chantier C — qualité des enrichissements (revu, commits 1d356de + 1954f34, branche worktree/light-birch)
- Règles : tableau rejeté (`editorial_enrichment_table_ioc_republication`) si une seule colonne contient des IOC projetés sur toutes les lignes et que les autres colonnes sont constantes/génériques ; diagramme rejeté (`editorial_enrichment_diagram_trivial`) si chemin/étoile dont les libellés répètent une seule assertion de l'ancre (pas de quota de nœuds) ; rejets de BLOC ; prompt durci ; action `REMOVE_ELEMENT` ajoutée à la revision d'enrichment.
- Revalidation réelle (9 artifacts, lecture seule) : rejetés = tableau IP du sujet 5 (`reported_ip_indicators`), diagrammes des sujets 4 (`flux_paralleles_gtg_30004`) et 7 (`architecture_arman`) ; acceptés = les 7 autres éléments (diagrammes 1, 6, 8, 9 ; tableaux 2, 3, 5 (protocoles), 6, 9). Aucun faux positif constaté.
- Invalidation : seuls EDITORIAL_ENRICHMENT puis PUBLICATION à recomputer.

### Intégration A+B+C+D — branche locale `chantier/integration` (worktree AutoWork_worktrees/integration)
- Merges sans conflit ; `make test-backend` complet : 2761 passés ; lint OK ; typecheck OK.
- Chantier D (revu) : suite complète 2721 verts sur sa branche ; correction de 9 tests de planner/desk par règle legacy documentée ; garde-fou de migration retiré.

### Chantier E1 — lancé (revision ciblée de synthèse, mode existing_evidence_only) — worker Luna xhigh, worktree snowy-deer

### Chantier D — validation réelle (matrice complète : `2026-10-08-source-criticality-real-run.md`)
- 12 pertes de sources du run réel évaluées avec `source-criticality-v1` : CISA AA26-097A + STIX JSON/XML (rôle PRIMARY), FBI (CORE), FBI/EPA et NCSC (contre-analyse), Krypt3ia (contre-analyse), IranWire Alaj (CONTEXT, couverture inconnue) → CRITIQUES (bloquants/review-required sans décision analyste fraîche) ; Reuters (S6) et IranWire (S9) → SECONDAIRES (continue avec diagnostic durable).
- Constats : (1) NCSC (`extraction_source_output_invalid`/`q2_no_payload`) n'a aucune décision en DB → désormais bloquant, plus de disparition silencieuse ; (2) décision S7 liée à la mauvaise justification (texte parle d'un CSV Anthropic) → nouvelle décision analyste à prendre, historique non réécrit ; (3) avertissement `q2_no_payload` du sujet S9 sans URL ni `omitted_sources` → limite de traçabilité à investiguer ; (4) aucun waiver du run 1 ne traverse vers le run 2.

### Chantier E1 — revision ciblée de synthèse (revu, accepté, fast-forward dans `chantier/integration`, HEAD aa305e4)
- Livré : contrat de domaine `production_editorial_revision.py`, service `production_synthesis_revision.py` (preview/apply/lineage, patch borné, IDs par contenu, fences CAS, reprise ModelRun, rebase d'enrichment, refus typé `revision_not_applicable`), API fine + types front, doc `2026-10-08-targeted-editorial-revision.md`.
- Vérifié par le superviseur : `make test-backend` 2785 passés, lint OK, typecheck OK ; test d'intégration Postgres des fences CAS présent.
- Simulation réelle (lecture seule, gateway simulé, 0 écriture DB, 0 appel Bridge) : sujet 1 titre (sections byte-identiques), sujet 4 paragraphe réécrit (frères/sections identiques), sujet 5 refus déterministe `revision_not_applicable` (restart extraction : source critique `source_extraction_failed`) ; REFERENCES/EXTRACTION/RELEVANCE intacts, PUBLICATION reconstruite, enrichment rebasé en mémoire.
- Rapport textuel final du worker non reçu ; revue faite sur diff, tests et simulation.
- Restent : E2 (mode `allow_source_extension`, opérations d'enrichment create/delete/rewrite raccordées au contrat/lineage commun, démos NanoDump + diagramme), F (UI/agent minimal), démos Bridge réel après déploiement.

### E2a — conception de l'extension de sources (docs/design/2026-10-08-source-extension-design.md, branche du worktree dusty-lynx, commit 41caecd)
- Option retenue (B) : nouvelles versions canoniques REFERENCES + EXTRACTION (+ RELEVANCE rebasée) = ancien corpus + addition, checkpoints existants réutilisés (0 appel modèle), une seule extraction neuve ; promotion atomique avec la révision de synthèse dans une seule transaction (le hash d'extraction change : la synthèse doit être révisée dans la même transaction).
- Garde-fous anti-extrapolation : source ajoutée = TECHNICAL/CONTEXT ; `EvidenceUseScope` (TOOL_CONTEXT vs ACTOR_ACTIVITY) ; `claim_scope` dans le wire ; un handle de contexte ne peut pas soutenir une affirmation sur l'activité de l'acteur.
- **Bloqueur observé sur le run réel (sujet 4)** : la contre-analyse Krypt3ia est CRITIQUE et non archivée (décision périmée : génération 0 vs run génération 2) → la promotion est refusée tant qu'elle n'est pas résolue par une décision de réparation à la génération courante (archivage manuel ou `continue_without_source` raisonnée par un ANALYSTE). Décision analyste à prendre par l'utilisateur avant la démonstration NanoDump (voir « Questions ouvertes »).
- Découpage : E2a-1 (acquisition + promotion atomique, lancé : worktree young-badger) ∥ E2b (enrichissements, worktree quiet-gorge) ; E2a-2 (révision de synthèse en mode allow_source_extension + publication) après fusion de E2b (fichiers partagés : contrat, api/production.py, production.ts).

### Chantier E2b — révision ciblée d'enrichissements (revu, accepté, fast-forward dans `chantier/integration`, HEAD 930dd17)
- Contrat commun E1 étendu aux enrichissements (rewrite/delete/create table|diagram|chart, replace/delete source figure) ; lineage au format commun (lecture des anciennes révisions conservée) ; delete = 0 appel modèle ; create = refus typé si preuves insuffisantes ; validateurs du chantier C appliqués ; SYNTHESIS byte-identique, seule PUBLICATION invalidée.
- Superviseur : suite complète 2797 passés, lint OK, typecheck OK.
- Simulation réelle (lecture seule, modèle simulé, 0 écriture, 0 Bridge) : sujet 4 `flux_paralleles_gtg_30004` delete (0 appel) + rewrite non trivial accepté ; sujet 7 `architecture_arman` delete + rewrite ; sujet 8 create_diagram accepté (seul élément ajouté, après le lead) ; sujet 4 create_diagram → refus typé `revision_not_applicable` (preuves insuffisantes pour le pipeline demandé). QA de publication OK ; SYNTHESIS inchangée.
- Constat : enrichment historique du sujet 9 a des `extraction_hash`/`synthesis_hash` qui diffèrent des hashes recalculés (écart historique rapporté, non utilisé en révision).
- Rapport textuel final non reçu ; revue sur diff/tests/recette.

### Chantier E2a-1 — acquisition additive de sources (revu, accepté, mergé dans `chantier/integration`, HEAD 59eedb4)
- Livré : `domain/production_source_additions.py`, `application/production_source_additions.py` (`SourceAdditionService` : intent → candidats/URL opérateur → archivage (manuel seulement après 403/robots, avec octets opérateur) → extraction de la seule nouvelle source → versions prospectives REFERENCES/EXTRACTION/RELEVANCE → preview → `promote`/`promote_in_uow` atomique), table `production_source_additions` (baseline modifiée), repository/UoW.
- Superviseur : suite complète 2807 passés sur `chantier/integration`, lint OK, typecheck OK ; test d'intégration Postgres `test_production_source_additions.py` OK.
- Recette réelle (sujet 4, source simulée, lecture seule) : 2 checkpoints Anthropic réutilisés (0 appel pour l'existant), hash d'extraction projeté 737b4ddd… (≠ 3328c2e0… de la synthèse actuelle : d'où la transaction unique avec la révision), preview `ready=false` avec bloqueur Krypt3ia (critique/counter_analysis/access_blocked), promotion refusée (`source_addition_promotion_blocked`), puis OK après levée simulée par décision D en mémoire.
- Rapport final texte du worker non reçu (2 relances) ; revue sur diff/tests/recette.

### Chantier E2a-2 — lancé (worktree noble-lake) : révision de synthèse `allow_source_extension` + garde-fous `EvidenceUseScope` + publication de la source ajoutée + atomicité promotion/révision.

### Chantier E2a-2 — révision de synthèse `allow_source_extension` (revu, accepté, fast-forward dans `chantier/integration`, HEAD 687975f)
- Livré : `revise_in_uow` (promotion REFERENCES/EXTRACTION/PROJECTION + nouvelle SYNTHESIS dans UNE transaction), `EvidenceUseScope` (TOOL_CONTEXT vs ACTOR_ACTIVITY ; « subject-scoped » = décisions DIRECT/CORROBORATION de la projection, sources CONTEXT exclues), `claim_scope` dans le wire, `PublicationSourceV1` avec rôle/criticité/provenance, lineage `source_additions`, preview unifié, refus typé si bloqueur critique.
- Superviseur : suite complète 2813 passés ; lint OK ; typecheck OK ; test d'intégration `test_synthesis_canonical.py` : 3 passés, 1 échec **préexistant sur main (f92cc18)** : `test_canonical_synthesis_pipeline_persists_reloads_and_assembles` (`synthesis.title == snapshot.subject_title` : titre avec préfixe `[Groupe]`) — non lié aux chantiers, à traiter à part.
- Recette réelle (sujet 4, source NanoDump simulée https://github.com/fortra/nanodump, 0 écriture DB, 0 Bridge) : refus initial `revision_not_applicable` (restart REFERENCES, Krypt3ia critique) ; après levée simulée : section « Description de NanoDump », sections précédentes byte-identiques, `extraction_hash` seul champ global modifié, TOOL_CONTEXT ne peut pas soutenir actor_activity, enrichment rebasé sans modèle, publication QA OK (source ajoutée « Contexte · Fortra »), provenance paragraphe = révision, chaînage v2→v3 (section)→v4 (titre) avec `source_additions` héritées, rejeu idempotent (0 appel).
- Résiduel : la source ajoutée est classée `critical/coverage_unknown` par la règle D par défaut (source technique non dupliquée) ; sans conséquence bloquante pour une addition extraite, mais à reconsidérer.

### Chantier F — interface minimale (revu, accepté, fast-forward dans `chantier/integration`, HEAD aa71936)
- Panneau « Révision ciblée » (`TargetedEditorialRevisionPanel.tsx`, intégré à `ProductionArtifactView.tsx`) ; cibles/opérations fournies par l'API (`revision_targets`), preview → apply avec fences, bloqueurs/refus typés/conflits, lineage ; interface agent documentée (`2026-10-08-targeted-revision-agent-interface.md`).
- Superviseur : défaut de lint backend trouvé (tri d'imports + ligne trop longue ; le worker n'avait pas pu lancer `make lint-backend`) corrigé (commit aa71936) ; frontend 260 tests, typecheck et lint OK.

## État final de `chantier/integration` (HEAD aa71936, base f92cc18, 73 fichiers, +19 587/−1 073)
- Backend : `make test-backend` 2817 passés ; `make lint-backend` OK ; `make typecheck-backend` OK. Frontend : 260 tests passés, typecheck OK, lint OK.
- Échec d'intégration préexistant sur main (`test_canonical_synthesis_pipeline_persists_reloads_and_assembles`) : inchangé, non lié.
- Branche locale fusionnable en fast-forward dans `main` ; rien n'est encore dans `main`, rien n'est déployé, aucun stage relancé, aucune écriture dans la DB du run réel, aucun appel Bridge réel pendant tout le chantier.

## Verrous avant toute démonstration réelle / recompute
1. **Schéma** : la baseline `0001` a été modifiée (table `production_source_additions`, colonnes de criticité sur `production_repair_decisions`, CHECK du type d'issue `source_extraction_failed`). Conformément à backend/AGENTS.md, aucun `0002`/compat : la DB du run réel (documents des 9 sujets = jeu de régression) ne reçoit PAS ces changements sans DDL manuel additif ou recréation de la base (destructif).
2. **Décision analyste Krypt3ia (sujet 4)** : contre-analyse critique non archivée, décision de dérogation périmée (génération 0 vs 2) → toute extension de sources est refusée tant qu'une décision analyste à la génération courante n'existe pas (ou archivage manuel).
3. Recompute minimal nécessaire pour bénéficier de A/B/C (titres, enrichissement) sur les 9 sujets : SYNTHESIS (titres) → ENRICHMENT → PUBLICATION ; ou révisions ciblées (titre, diagramme) ; aucun REFERENCES/EXTRACTION/RELEVANCE.

## Passage à la base réelle (préparation, rien d'appliqué)
- `main` fast-forward sur `chantier/integration` (aa71936) à la demande de l'utilisateur ; conteneurs inchangés (images non reconstruites).
- DDL additif pour la base réelle : `docs/design/2026-10-08-live-db-additive-ddl.sql` (généré par diff ORM f92cc18→aa71936 ; base réelle à la révision alembic `0002_production_batch_pause`). Contenu : 4 colonnes nullables + 5 CHECK nouveaux/élargis sur `production_repair_decisions`, nouvelle table `production_source_additions` (+2 index, trigger append-only). Testé sur une copie jetable du schéma réel (pg_dump -s en lecture seule) : appliqué sans erreur. Non appliqué à la base réelle : attend relecture utilisateur + sauvegarde `pg_dump`.
- Décision Krypt3ia : l'utilisateur décide la dérogation ; texte exact de la raison et identité de l'analyste à fournir avant enregistrement.

## Mise en service réelle (2026-10-08, après accord utilisateur)
- Sauvegarde `pg_dump -Fc` : `~/backups-autowork/cti_app_pre_chantier_20261008.dump` (636 entrées, vérifiée par `pg_restore -l`).
- DDL additif appliqué sur la base réelle (transaction unique, sans erreur) : 4 colonnes + CHECK élargis sur `production_repair_decisions`, table `production_source_additions` (+ index, trigger append-only) ; 59 décisions existantes intactes. Stack reconstruite (`make up`), healthy ; alembic reste `0002_production_batch_pause`.
- Krypt3ia : récupéré par `curl` (User-Agent navigateur, HTTP 200, 189 Ko, titre « Weekly ALL-SOURCE Cyber Warfare Intelligence Brief 9.14.26 | Krypt3ia ») puis archivé par l'endpoint d'archivage manuel (`POST /api/subjects/{id}/sources/{collection}/content`) → collection 8a16f2fd… `archived`, document fdf3415f…. N'est PAS encore intégré à REFERENCES (état `archived_pending_references`) : l'intégrer imposerait le chemin de réparation SOURCE_CORPUS (REFERENCES→…→PUBLICATION du sujet 4).
- Aperçus réels OK (titre, diagramme). Constat chronologie du sujet 4 : l'artifact de synthèse contient des entrées de chronologie d'autres cas (Xiaomi, DeepSeek, Radio Lengo Songo…) — non publiées (`include_timeline=False`) mais à traiter (défaut d'extraction/synthèse de timeline, à investiguer).
- **Défauts trouvés en conditions réelles lors du premier apply (les simulations ne pouvaient pas les voir)** :
  1. `rewrite_title` : le validateur de la révision imposait `[publisher]` comme groupe (`_editorial_title_group`, repli du pipeline d'origine) → rejette le titre correct `[GTG-30004] …` proposé par le modèle (`synthesis_title_actor_ungrounded`). Worker de correction lancé (steep-cloud).
  2. `rewrite_diagram` : conflit `figure catalog no longer matches` : le hash d'inventaire de figures stocké (7447c159…, 0 figure) ≠ inventaire actuel (112 figures archivées pendant l'étape d'enrichissement, hash cfab55f8…) ; la fence est structurellement instable pour tout sujet avec médias. Worker de correction lancé (copper-gate).

## Phase 2 — Plan de mise à niveau du livrable par TARGETED REVISION (état réel du run, 2026-10-08)
Aucune relance de pipeline ; chaque correction = révision ciblée (artifacts REFERENCES/EXTRACTION/RELEVANCE conservés). Prérequis : correctifs steep-cloud (règle de titre) et copper-gate (fence inventaire de figures), puis `make up`.

| # | État actuel | Révision ciblée prévue | Appels modèle |
|---|---|---|---|
| 1 Bitcoin/Iran | `[Acteurs présumés liés au Ministry of Intelligence iranien] … via OP_RETURN` (T2) ; 757 mots, 7 sections sans titre | rewrite_title (acteur grounded, sans détail technique non établi pour le cas) ; titres de sections | 1 + 7 |
| 2 Mirage Kitten | titre OK ; 1341 mots, 8 sections sans titre | rename_section ×8 (accès initial, chaîne d'exécution, persistance, évasion, capacités, infra/C2, détection, attribution) | 8 |
| 3 CHOSEN BRICK/HEAVYGRAM | titre OK ; 1188 mots, 8 sections | rename_section ×8 | 8 |
| 4 GTG-30004 | `[Anthropic] …` (T1) ; 255 mots ; diagramme trivial ; timeline contaminée (62 entrées d'autres cas, non publiée) | rewrite_title ; rewrite/delete diagramme ; section NanoDump (allow_source_extension) | 1 + 0/1 + extraction + 1 |
| 5 AA26-097A | `[Cyber Advisors]` (T1/S4) ; tableau IP redondant (C1) ; 845 mots | rewrite_title ; delete_table `reported_ip_indicators` ; rename_section ×7 | 1 + 0 + 7 |
| 6 GTG-30005 | `[Anthropic] … systèmes…` tronqué (T1/T3) ; brève 422 mots | rewrite_title | 1 |
| 7 GTG-34007 | titre OK ; diagramme `architecture_arman` trivial | delete_diagram | 0 |
| 8 MuddyWater | `[Daylight] MuddyWater Un faux support…` (T1, ponctuation) ; 1207 mots, 6 sections | rewrite_title ; rename_section ×6 | 1 + 6 |
| 9 GTG-30006 | `[Anthropic]` (T1) ; 846 mots, 7 sections | rewrite_title ; rename_section ×7 | 1 + 7 |

Non traité par révision (hors canal) : timeline contaminée des sujets 4, 6, 7, 9 (53–62 entrées, non publiée) → défaut de synthèse/extraction à investiguer ; Krypt3ia (sujet 4) archivée mais non intégrée aux références.

## Phase 2 — exécution réelle des révisions ciblées (journal, 2026-10-08)
Premier défaut corrigé et intégré dans `main` (68986a7) : règle de titre de la révision (groupe acteur accepté, éditeur rejeté si acteur établi).

| Sujet | Avant | Après | Résultat |
|---|---|---|---|
| 4 | `[Anthropic] GTG-30004 Automatise…` | `[GTG-30004] Automatise le profilage OSINT et obfusque un dérivé de NanoDump` | OK ; seul `title` modifié ; REFERENCES/EXTRACTION/RELEVANCE inchangés ; enrichment rebasé v3 ; publication v3 reconstruite |
| 1 | `[Acteurs présumés liés au Ministry of Intelligence iranien] … via OP_RETURN` | `[Opérateur présumé lié au Ministry of Intelligence iranien] Inscrit des données de routage C2 sur Bitcoin` | OK (détail OP_RETURN retiré) |
| 6 | `[Anthropic] GTG-30005 — … systèmes…` (tronqué) | `[GTG-30005] Mène une reconnaissance navale et développe des capacités de surveillance` | OK (2e tentative : 1re rejetée `synthesis_title_invalid`) |
| 8 | `[Daylight] MuddyWater Un faux support…` | `[MuddyWater] Une opération présumée du groupe usurpe le support Teams pour livrer DinDoor` | OK (3e tentative : sortie du modèle sans enveloppe `@@…@@` → `revision_wire_missing_block`) |
| 9 | `[Anthropic] GTG-30006 développe SECOMS64…` | `[GTG-30006] Développe des malwares et un portail de phishing ciblant des Iraniens` | OK, grounded (chapeau cite malwares et portail de phishing) |
| 5 | `[Cyber Advisors] …` | inchangé | REFUSÉ par le garde D : 2 contre-analyses critiques en échec d'extraction (`extraction_source_output_invalid`) + sources CISA/FBI 403 (waivers antérieurs) → `revision_not_applicable`, restart `extraction`. À régler par ré-extraction de ces 2 sources (pas de dérogation qui masquerait une source critique manquante). |

Défauts de la chaîne trouvés en conditions réelles (les simulations ne pouvaient pas les voir) :
1. règle de titre (corrigé, main) ;
2. fence inventaire de figures instable → 409 sur toute révision d'enrichissement (worker copper-gate) ;
3. `rename_section` : AFTER en texte brut → heading vide → no-op silencieux qui crée une version de SYNTHESIS + publication inutiles ; garde `no-change` manquant (worker pure-delta). Conséquence : sujet 2 a une version v2 identique à v1 (sans effet) ;
4. archive manuelle d'une source ajoutée : `archive_receipt_incomplete` car `fetch_policy_snapshot_id` de la collection est vide pour une archive manuelle (worker late-falcon).
Constat méthodologique : chaque défaut vient d'un faux de test qui ne reproduisait pas le comportement des vrais services ; les workers suivants doivent tester contre les vrais repositories/services.

### Phase 2 — suite (main = 85cc735 ; 2849 tests backend, lint, typecheck OK ; stack reconstruite)
Correctifs intégrés à `main` après découverte en conditions réelles :
- fence enrichment (copper-gate) : catalogue de figures admises vérifié figure par figure, anciens artifacts (générateur v8) acceptés seulement si toutes les données d'entrée sont inchangées, la révision n'archive plus de médias (lecteur de médias persistés) ; défaut en plus : le bump de version du générateur (chantier C) aurait rendu TOUS les enrichissements existants non révisables.
- rename_section / garde no-change (pure-delta) : `revision_no_change` refusé avant toute écriture ; parseur tolérant sur AFTER texte brut.
- reçu d'archive manuelle (late-falcon) : reprise sur le reçu de politique persisté.
Scénario 2 (diagramme seul) exécuté en réel : `rewrite_diagram` sujet 4 → refus propre (le modèle a proposé un diagramme rejeté par les validateurs : libellés hors budget, relations vers nœuds inconnus ; refus typé `revision_not_applicable` mais avec un `recommended_restart_stage: extraction` trompeur — à corriger) ; `delete_diagram` sujets 4 (`flux_paralleles_gtg_30004`) et 7 (`architecture_arman`) appliqués en 2,7 s, 0 appel modèle, SYNTHÈSE strictement identique (hash), tous les autres blocs identiques (seules les annotations sémantiques du diagramme supprimé changent), publication reconstruite.
Défauts résiduels notés : (a) l'API renvoie 500 sur `BridgeTransportError` (« payload du composer non prêt ») au lieu d'une erreur typée retryable ; (b) restart_stage trompeur pour un rejet de validation du modèle ; (c) sujet 2 a une version v2 sans effet (créée avant le garde no-change).

### Phase 2 — sous-titres appliqués (main = 4941711, stack reconstruite)
Résultat vérifié par comparaison de la dernière version de SYNTHESIS à la dernière version SANS métadonnée de révision (contenu hors titre/sous-titres/extraction_hash strictement identique : textes, evidence_refs, sources, timeline, source_notes) :
| Sujet | Révisions | Sous-titres | Contenu hors titre/sous-titres |
|---|---|---|---|
| 1 Bitcoin | 8 (titre + 7 sections) | 7/7 | identique |
| 2 Mirage Kitten | 9 | 8/8 | identique |
| 3 HEAVYGRAM | 8 | 8/8 | identique |
| 4 GTG-30004 | 1 (titre) | 0/3 (brève) | identique |
| 6 GTG-30005 | 1 (titre) | 0/3 (brève) | identique |
| 8 MuddyWater | 7 (titre + 6) | 6/6 | identique |
| 9 GTG-30006 | 8 | 7/7 | identique |
Rendu PDF contrôlé (sujet 2) : sous-titres affichés, mise en page correcte.
Défaut supplémentaire trouvé puis corrigé : le validateur de sous-titre rejetait « C2 », « S3 »… (sigles génériques avec chiffre) → liste fermée de termes CTI génériques + contrôle des noms CamelCase (nightly-cloud, commit 4941711, 2873 tests).
Diagrammes triviaux supprimés (sujets 4 et 7). 
NanoDump (sujet 4, scénario 1) : archivage manuel du README fortra/nanodump OK, extraction OK (`ioc_rules`, 5 indicateurs), aperçu OK, révision Bridge réelle : section produite par le modèle (portée tool_context, aucune attribution à l'acteur) mais REFUSÉE par le garde-fou `synthesis_relation_not_evidenced` — à juste titre : les preuves sont des noms de fichiers, pas des faits décrivant l'outil. Lacune de conception : une source de contexte destinée à EXPLIQUER un outil doit pouvoir être extraite en profil FULL (faits) tout en restant `context`/`tool_context`. Worker hollow-vapor lancé.
Aussi observé : 3 échecs de transport Bridge (« payload du composer non prêt avant soumission », ModelRun `not_submitted`, API → HTTP 500 non typé) — transitoires mais bruyants.

### INCIDENT — perte de blocs d'enrichissement après les révisions de sous-titres (détecté 2026-10-08, pendant la comparaison avec la référence)
Les `rename_section` appliqués aux sujets 1, 2, 3, 8, 9 ont fait disparaître tous leurs diagrammes/tableaux/figures de la version courante d'ENRICHMENT (sujet 8 : `revision_rebase.stale_blocks = section_anchor_changed`). Cause : l'ancre de bloc = id de section (hash incluant le heading) → renommer change l'ancre → bloc écarté. Données historiques intactes (append-only). Remédiation : worker foggy-crane (ancre indépendante du heading + cas d'usage de restauration des blocs depuis les versions antérieures vérifiées, sans appel modèle, sans restaurer les `delete_*` volontaires ni les blocs que les validateurs du chantier C rejettent) puis application sur les 5 sujets. Les PDF des sujets 1, 2, 3, 8, 9 sont donc TEMPORAIREMENT sans diagrammes/tableaux jusqu'à restauration. Leçon : tout enchaînement de révisions doit contrôler le nombre de blocs d'enrichissement avant/après (garde à ajouter : une révision ne doit pas réduire le nombre de blocs sans opération `delete_*` explicite).
Chantier style : worker timely-ember (diagrammes D2 → style de la référence ChapsVision : horizontal, compact, icônes, cadres en pointillés, flèches noires ; alignement des tableaux).
Écart avec la référence (bulletin CN-2609) : manquent « Chronologie » (puces datées d'événements + notes de bas de page), « Vue d'ensemble » (encadré Victimologie / Arsenal / Objectif), « Note de l'analyste », figures/captures et blocs de code commentés (G1–G4 de l'audit, décisions produit).

### NanoDump (sujet 4) — état après hollow-vapor (main = 4567ecf, stack reconstruite)
- Extraction descriptive intégrée (`extraction_intent=descriptive_facts`, profil FULL, raison `operator_descriptive_context`) : 29 evidence `fact` (au lieu de 5 indicateurs), rôle CONTEXT conservé.
- Révision `insert_section` (Bridge réel) : le modèle produit une section de 3 claims (2 `tool_context`, 1 `actor_activity` qui distingue explicitement outil générique / activité attribuée par Anthropic). Refus `synthesis_relation_not_evidenced` : la règle d'E1 exige que CHAQUE mot ≥ 4 lettres (prose française comprise) figure dans le texte des evidence citées → faux positif systématique sur toute prose rédigée. Worker ageless-badger : ancrage par jetons DISTINCTIFS (noms, nombres, identifiants techniques, options, API, IOC) au lieu du lexique complet ; remontée de `unsupported_terms` dans le refus.
- La réponse du modèle est archivée : rejouer la même requête après correctif réadopte la sortie sans resoumission au Bridge.

### Incident enrichment — RÉSOLU (main = 93fb0fb ; 2881 tests backend, 261 frontend, lint/typecheck OK ; stack reconstruite)
- Correctif foggy-crane : ancre d'enrichissement indépendante du heading (`_enrichment_section_anchor_id`, compat de lecture de l'ancien format) ; `stale_blocks` conservent `source_artifact_id` ; nouveau cas d'usage `restore` (preview + apply, sans appel modèle ; ne restaure pas les blocs supprimés par `delete_*` ni ceux que les validateurs rejettent) + API `/production/artifacts/editorial_enrichment/restore[/preview]`.
- Restauration appliquée en réel (aucun appel modèle ; synthèses inchangées ; publications reconstruites) : sujet 1 (diagramme + figure), 2 (2 tableaux), 3 (tableau + figure), 8 (diagramme), 9 (diagramme + tableau). PDF du sujet 8 vérifié : diagramme présent (ancien style, vertical).
- À ajouter (recommandation) : garde « une révision ne réduit pas le nombre de blocs d'enrichissement sans `delete_*` explicite ».

### NanoDump — 3e tentative (main = 0183c4a, stack reconstruite)
- Ancrage par jetons distinctifs intégré (ageless-badger, 2889 tests) ; le refus remonte `claim_code` + `unsupported_terms` à l'opérateur.
- Requête avec exigence de citation précise (Beacon, WerFault.exe, PssNtCaptureSnapshot → handle contenant l'élément ; pas de `--valid` absent des faits) : la section produite est correcte et bien citée ; un seul terme refusé : `Cobalt Strike Ces` = séquence de mots capitalisés assemblée à travers « . » (faux positif du nouveau validateur). Worker spinning-mist lancé ; la réponse du modèle étant archivée, la même requête sera rejouée sans nouvel appel Bridge (requête sauvée dans /tmp/nano_req2.json).

### Décisions utilisateur (2026-10-08, tour suivant)
- Pas de « Vue d'ensemble » ni de « Chronologie » pour cette livraison.
- Articles bloquants : le Bridge y a accès pour la plupart → consultation consultative (pas d'intégration sans archivage tracé). Canal : chemin `draft` avec navigation (script `consult_gateway.py`), la conversation libre `/api/model-conversations` échoue (`external_turn_identity_unavailable`, chat temporaire). Résultat sujet 6 (Reuters) : « rien d'utile pour GTG-30005 : on peut passer » (contexte iranien sans rapport direct). Sujets 5, 3, 7 en cours.
- Nouveau pipeline Typst (`typstPipeline/`, ignoré par git) = référence du livrable : `article.csv` par article (`[Groupe|Titre|Éditeur|URL]` + `isNew,type,value`), `legacy_iocs_<projet>.txt`, sommaire (Résumé de l'actualité [Articles] + Autres brèves [Brèves], liens), Suivi des indicateurs, fichiers intégrés. Écart constaté : AutoWork n'a ni export CSV, ni legacy, ni brèves, ni sommaire cliquable. Worker weekly-gate lancé.
- Priorités : IOC pertinents récupérés ; bonne section Références ; synthèses de grande qualité opérationnelle CTI enrichies (diagrammes, tableaux, images/captures, éléments externes : précisions sur les malwares…).

### NanoDump appliqué + passe qualité (main = 5d90d96)
- NanoDump (sujet 4) APPLIQUÉ : synthèse v4 (section « Fonctionnement générique de NanoDump », insert_section allow_source_extension), sections 0-2 identiques (vérifié canoniquement), seul `extraction_hash` change ; source ajoutée visible en `sources` (rôle context). Défauts : section placée après la conclusion d'attribution ; source absente de la liste des références (loyal-mesa en cours).
- Consultation Bridge des sources bloquantes : s6 Reuters → rien d'utile ; s5 : CISA ×2 + FBI illisibles (403/délai), FBI/EPA (MicroLogix 1100/1400, 7 États, IP/mots de passe PLC, impacts physiques) et NCSC (périmètre UK, durcissement) = apports utiles, non attribués à l'Iran ; s3 : le rapport FBI FLASH `ic3.gov/CSA/2026/260915.pdf` est DÉJÀ dans nos références et nos faits extraits (114 faits, 161 indicateurs : AnyDesk, Add-MpPreference -ExclusionPath, mutex, LoLoLoLo, KeyLog…) mais sous-exploité par la synthèse ; s7 : erreur de consultation.
- Passe qualité sujet 3 (3 révisions × 3 tentatives) : 9/9 REFUSÉES par `synthesis_relation_not_evidenced` : faux positifs (noms de sources FBI/AIVD/NCSC/Group-IB, nom du sujet HEAVYGRAM, PowerShell sous un autre handle du même document, mots courants « chaque », « celles-ci »). Worker soaring-dusk : politique d'ancrage v3 (ancrage au niveau du DOCUMENT cité + contexte sujet + étiquettes de source ; valeurs techniques toujours ancrées dans les documents cités).
- IOC sujet 3 : 14 chemins/noms d'artefacts hôte du rapport FBI rejetés `source_evidence_missing` (coupures de ligne PDF) ; `verify-replacement` ne les valide pas ; à repêcher après rendu de la vérification tolérante aux retours à la ligne.

### Intégré à main = 4e882b1 (stack reconstruite) : style des diagrammes (timely-ember)
- Rendu D2 aligné sur la référence : flux horizontal compact, nœuds blancs bordure fine, flèches noires étiquetées, accent orange malware, typographie 8–9 pt ; politique de direction d'ENRICHISSEMENT inchangée (4 tests rétablis : le changement est limité au rendu) ; suite 2924 tests, lint, typecheck OK. Limites : pas d'icônes ni de cadres en pointillés dans les cas réels (aucun groupe dans les spécifications). Publication à reconstruire (assemblage seul, 0 appel modèle) pour appliquer le style aux 9 sujets : à faire UNE FOIS après les révisions de qualité.
- Workers en cours : soaring-dusk (ancrage v3), loyal-mesa (références de contexte), weekly-gate (export CSV IOC + sommaire ; typage V5 et 2 tests à corriger), young-vapor (vérification tolérante aux coupures de ligne PDF → récupération de 14 artefacts hôte du rapport FBI).

### main = 3847bd7 : références (loyal-mesa) intégrées
- Les ressources techniques de contexte CITÉES par la synthèse apparaissent dans la liste de références (libellé « Contexte technique », note de repli déterministe, date inconnue tolérée) ; non citées : absentes ; doublons Anthropic (deux pages) inchangés. Résolution de conflit de versions avec le style de diagrammes : `PUBLICATION_RENDERER_VERSION = publication-v7-typst-v5-chapsvision-cited-context`, `PUBLICATION_RENDER_POLICY_VERSION = typst-publication-v7-chapsvision-cited-context`. 2928 tests, lint, typecheck OK ; stack reconstruite.
- En attente : soaring-dusk (ancrage v3), weekly-gate (export CSV/sommaire : typage V5 + 2 tests), young-vapor (IOC : vérification tolérante PDF).

### IOC sujet 3 récupérés (main = 78dcb1f ; 2939 tests, lint, typecheck OK ; stack reconstruite)
- young-vapor (vérification tolérante PDF) intégré à dcc78f5 ; les 14 clés restaient `source_evidence_unavailable` : le blob PDF archivé (5,4 Mo) dépassait la limite de lecture par défaut des artifacts (4 Mio) dans `_archived_source_document` → lecture avec `MAX_ARCHIVED_SOURCE_BYTES` (25 Mio, comme l'extraction) + test de non-régression (58ee16e).
- Nouveau défaut découvert à l'apply : `assembly_inputs_mismatch` — la synthèse d'origine porte le hash d'extraction « ancienne forme » (sans `scope`) ; la vérification de lignée de `production_repairs` ne comparait qu'au hash courant → accepte désormais les hashes historiques (`is_production_extraction_hash`), idem dans `production_synthesis` (2 sites) + test (968f5d8).
- Décisions analyste appliquées via l'API (aucun appel modèle) : valeurs corrigées depuis le texte archivé du PDF FBI (barre oblique perdue avant `{GUID}`, `\_*`, `\*`, `\Phones\*`…) : 9 `replace` vérifiés « literal / whitespace_canonicalized », 4 `include` (chemin WhatsApp, URL `ams1.vultrobjects.com/desktop-…-tron5`, `%USERPROFILE%\Downloads\Telegram Desktop\`, `ZP_…part2.rar`), 2 exclusions de doublons, hash à caractère en trop laissé exclu (absent de la source). `repairs/apply` : extraction v4, synthèse v10 (rebase de lignée), publication v11. Une seule IOC de publication gagnée (l'URL) : les chemins sont des artefacts hôte, hors types de publication (domain/hash/url), donc exploitables via la synthèse (technical literals ancrés) et non via l'annexe IOC.
- Sujet 2 : 3 chemins rejetés `source_evidence_not_text_verifiable` (`node_modules/.cache/.320697f1/index.js`, `.git/hooks/post-checkout`, `%USERPROFILE%\.vscode\extensions`) vérifiés dans les sources ; décisions posées, puis `repairs/apply` → 500 (`127.0.0.1:48739` typé IP dans l'extraction de base : `canonical_indicator_key` levait ValueError) corrigé (78dcb1f) ; puis 409 `synthesis_relevance_projection_changed` INDÉPENDANT des décisions (idem avec exclusions) : la projection de pertinence recalculée classe désormais autrement une référence citée par la synthèse. Décisions remises à `exclude` (état inchangé) ; résiduel à investiguer, faible valeur (chemins génériques de dev).
- Autres sujets : aucune réparation IOC en attente (sujets 1, 8 : 0 ; 4, 6, 7, 9, 3 : seulement des `supplemental_source_unarchived` ; sujet 5 : 2 `source_extraction_failed`).

### Enrichissements ciblés (canal TARGETED REVISION, enrichment) — passe qualité
- Sujet 7 : tableau comparatif des deux unités (rattachement, mode opératoire, usage de Claude, outils, volumes) créé via `create_table` (1 appel Bridge, 153 s) ; contenu relu : fidèle aux preuves.
- Sujet 4 : diagramme de flux (profilage OSINT + dérivé de NanoDump) créé via `create_diagram` (1 appel Bridge, 235 s) ; rendu à vérifier après reconstruction.
- Sujet 3 : `create_diagram` (flux C2/exfiltration Telegram + stockage objet) REFUSÉ en preview `409 evidence pack no longer matches the base artifact` : après `repairs/apply`, l'enrichissement `lineage_rebased` garde l'`evidence_pack_hash`/`input_hash` de l'ancienne lignée. Les révisions de synthèse du même sujet restent possibles. Worker keen-spruce lancé (fence des artifacts rebasés).
- Workers en cours : soaring-dusk (ancrage v3, relancé pour finir), weekly-gate (export CSV IOC/sommaire, relancé pour finir), keen-spruce (fence enrichissement rebasé).

### Grounding v3 + weekly-gate intégrés (main = merge soaring-dusk + weekly-gate ; 2962 tests backend, lint, typecheck OK ; stack reconstruite)
- soaring-dusk : politique d'ancrage v3 (étiquettes de source/éditeur, contexte sujet + synthèse de base, ancrage au niveau du document cité, mots courants non distinctifs, assertions de portée « toutes/tous/jamais… » conservées) ; un test périmé (« Foo-bar ») aligné.
- weekly-gate : `GET /api/editions/{id}/release/iocs.zip` (article.csv par publication + legacy_iocs_<id>.txt + manifest.json depuis la release figée), sommaire cliquable articles/brèves/suivi/annexes. Validation live (release actuelle) : 51 domaines, 167 « file » (hashes), 14 IP, 26 URL ; `isNew=1` seulement sujet 8 (`original_indicators`). Douteux à arbitrer : api.telegram.org (sujets 3 et 9), gofile.io, outlook.office.com (sujet 9), `ams1.vultrobjects.com` seul (sujet 3), oracle-challenge S3 (sujet 2), listes de 126 hashes (sujet 3). La release doit être refigée pour intégrer les corrections ultérieures.
- Sujet 3, passe qualité (Bridge, modèle Luna/ChatGPT) : 3/3 révisions APPLIQUÉES après durcissement des consignes (v13) : AnyDesk dans « leurres » (+251 mots), capacités de collecte (128 → 605 mots), nouvelle section « Évasion Defender et artefacts hôte » (391 mots) avec commandes Add-MpPreference, mutex, chemins, S3. Refus intermédiaires conformes : `toutes` (portée absolue non évidenciée), `winappx.exe` cité sous un handle qui ne le contient pas, `_spk.mp3` (source : `spk.mp3`), `plain_text_violation` (masques `*`/`_` interprétés comme markdown), 3 échecs de transport Bridge (`payload du composer non prêt`).

### main = 94418d8 : fences de révision corrigées (keen-spruce, brave-grove) ; 2973 tests, lint, typecheck OK ; stack reconstruite
- keen-spruce : un enrichissement `lineage_rebased` (après `repairs/apply`) redevient révisable ; exception limitée aux artifacts rebasés dont les ids/hashes courants (extraction, projection, synthèse) concordent, politique d'accès et inventaire de figures toujours stricts ; hashes recalculés à la révision. (Le doublon loyal-quartz, lancé sans effort xhigh et arrêté, n'est PAS intégré.)
- brave-grove : révision de synthèse possible quand aucun paragraphe du chapeau n'est admis (4 sujets sur 9 touchés : 1, 5, 8, 9 ; cause : le chapeau mêle un fait admis et une incertitude issue d'une source `IOC_RULES` non retenue dans le pack) ; `@@LEAD@@` facultatif UNIQUEMENT pour les patches ciblés ; chapeau d'origine préservé ; validations de la cible inchangées ; versions parseur/contrat/validateur v4/v5.
- Sujet 9 (GTG-30006) : 2 révisions APPLIQUÉES après le correctif : « Implant SECOMS64 et modules associés » 76 → 495 mots, « Géofiltrage et leurres de phishing » 65 → 356 mots (v11). Blocs d'enrichissement des 9 sujets vérifiés après coup (diagrammes/tableaux conservés ; aucun `stale`).
- Sujet 3 : `create_diagram` (flux C2/exfiltration) REFUSÉ typé `revision_not_applicable` (`editorial_enrichment_diagram_relation_endpoint_unsupported` ×3 → aucun bloc valide) : les relations proposées ne sont pas étayées par les preuves ; la figure source existante de la chaîne HEAVYGRAM couvre déjà le besoin. Pas de contournement.
- PDF vérifiés : sujet 4 (diagramme de flux, nouveau style horizontal, légende), sujet 7 (tableau comparatif des deux unités). L'édition 5defd15d est toujours `open` : pas de release figée, `preview` indisponible (`edition_preview_unavailable`) ; le gel de la release (diffusion) reste une décision utilisateur.

### Services légitimes retirés des IOC + reconstruction des 9 publications (main = 8334f41 ; 2999 tests, lint, typecheck OK)
- Décision utilisateur : `api.telegram.org`, `gofile.io`, `outlook.office.com` et équivalents ne sont pas des IOC. singing-birch : registre versionné `data/legitimate_services_v1.json` (hôtes génériques exacts, domaine nu ou URL racine sans requête/fragment ; aucune correspondance par suffixe ; `micbucket.ams1.vultrobjects.com` et URL à chemin spécifique conservés), filtre dans la sélection d'IOC de `publication_builder`, filtre défensif dans l'export (`legitimate_service` au manifest). Décision `isNew` : `isNew=1` ⇔ `original_indicators` (convention du pipeline Typst « notes analyste »), décidée par le superviseur à la demande de l'utilisateur.
- Défaut découvert à la reconstruction : l'assemblage recompile les diagrammes compilés avec une ancienne politique, mais le QA recevait l'enrichissement stocké → `exact_projection` False (sujets 1, 6, 8, 9). ivory-haven : l'assemblage retourne `PublicationAssemblyResult(artifact, editorial_enrichment)`, workflow et retry passent l'enrichissement rafraîchi au QA ; `EDITORIAL_ENRICHMENT` et ses fences inchangés ; check `exact_projection` conservé. Un fake de test de keen-spruce aligné par le superviseur.
- Reconstruction (assemblage seul, 0 appel modèle) des 9 publications : toutes `ready` (versions 12, 12, 15, 7, 2, 4, 5, 13, 14). IOC publiés : domaines 50, hashes 167, URL 25, IP 14, e-mail 1 ; aucun service légitime restant ; sujets 1, 4, 6, 7, 9 sans IOC (sujet 9 : ses 3 domaines étaient tous des services légitimes).

### Livrable IR2609 figé (2026-10-09)
- Décisions utilisateur : exigence levée pour le sujet 5 (NCSC/CISA/FBI) ; tableau IP redondant du sujet 5 supprimé ; sources non archivables (FBI sujet 3, IranWire sujets 7 et 9, Krypt3ia sujet 4) levées par décision analyste motivée (consultation Bridge IranWire : apport périphérique, aucun IOC).
- Défauts corrigés au passage : garde de criticité ignorant la renonciation analyste (noble-canyon) ; décisions de source résolues contre un ancien run (swift-marten) ; typage des gardes (superviseur). main = 3003 tests, lint, typecheck OK.
- Passe qualité : titres (groupe = acteur) sujets 1, 2, 5 ; section « Chronologie » renommée (sujet 3) ; sections enrichies via TARGETED REVISION sujets 3, 4, 6, 7, 9 ; diagramme C2 (sujet 3) et chaîne d'infection (sujet 2) refusés par la validation de relations (non étayés). Sujet 6 : la 1re section reprend en partie le chapeau (redondance acceptée).
- Acceptation de l'édition (manifeste 8a7bc1fe, release 113b9ce4) puis export : `livrable-IR2609/` = PDF 45 p., ZIP CSV (50 domaines, 167 hashes, 14 IP, 25 URL), projet Typst brut recompilable (`typst-source/build.sh`), LISEZMOI.md.

### Diagrammes sujets 2 et 3 réintroduits ; livrable v2 (2026-10-09)
- « Relation non étayée » = une relation doit citer un handle de preuve partagé avec CHACUN de ses deux nœuds (et existant dans le pack). Faux rejets trouvés par dim-moose : (1) handles de la catégorie « reserve » du pack non résolus par `_edge_projection_rejection` ; (2) validation de texte brut qui prenait « Storage (S3) » pour un marqueur de citation `(S3)` ; (3) `output_invalid` sans sous-code (désormais exposé). Régressions rattrapées par le superviseur : l'incrément de `EDITORIAL_ENRICHMENT_POLICY_VERSION` rendait les artifacts stockés « non supportés », celui de `EDITORIAL_ENRICHMENT_VALIDATOR_VERSION` cassait la fence de révision ciblée → revertés ; règle documentée : un assouplissement pur n'incrémente pas ces versions.
- Sujet 2 (chaîne d'infection, 8 relations) accepté avec la règle de cohérence des preuves dans la consigne ; sujet 3 (flux C2/exfiltration, 7 nœuds, 8 relations) accepté après correction du validateur (rejeu de la sortie archivée, 0 appel Bridge). main = 3009 tests, lint, typecheck OK.
- Nouvelle release (manifeste e7642af0, release 41755ee7) ; ancienne conservée (113b9ce4). Livrable v2 : `livrable-IR2609/` (PDF 45 p., ZIP CSV identique : 50 domaines, 167 hashes, 14 IP, 25 URL, Typst brut avec 8 médias) ; v1 conservé dans `livrable-IR2609-v1-sans-diagrammes/`.
