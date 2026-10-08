# E2a — extension additive de sources pour une révision ciblée

Conception seulement · 2026-10-08 · synthèse d’un sujet existant

Dans les citations ci-dessous, application/ et domain/ désignent
backend/src/cti_app/application/ et backend/src/cti_app/domain/.

## 1. Existant réutilisable et constat du run

- E1 fournit le contrat immuable et ses fences dans
  backend/src/cti_app/domain/production_editorial_revision.py
  (TargetedEditorialRevisionRequest, cibles stables, modes et refus).
  production_synthesis_revision.py::preview/revise/_load_context vérifie
  l’artefact courant; revise compare ProductionSynthesisV1.extraction_hash à
  l’extraction canonique par is_production_extraction_hash. Le mode
  allow_source_extension est refusé et source_additions vaut []. La requête,
  le ModelRun et l’artefact de révision sont déterministes; la reprise vérifie
  les octets archivés avant d’adopter une réponse.
- collection.py::archive_manual_content archive les octets fournis par
  l’analyste avec URL finale, acteur et reçu. La collecte standard garde ses
  politiques d’accès. production_editorial_enrichment.py::
  build_editorial_resource_proposal_model_request/_search_resource_proposals
  fournit déjà une recherche web bornée au sujet et aux besoins typés; elle
  retourne des propositions d’URL, pas des preuves. Une URL opérateur évite
  cette recherche.
- production_references.py::build_production_reference_corpus conserve le
  corpus précédent et ajoute les sources choisies (URL canonique, date,
  collection, tier, kind, rôle éditorial, empreinte). Le calcul D
  domain/production_references.py::production_source_criticality classe
  CORE, PRIMARY et COUNTER_ANALYSIS critiques; une couverture inconnue reste
  critique.
- production_extraction.py::build_extraction_plan/execute et
  _read_checkpoint/_persist_checkpoint utilisent des checkpoints
  content-addressed (texte effectif, profil et versions). Un corpus modifié
  change le hash d’extraction, sans recalculer les anciennes sources.
  production_resume.py::plan_production_resume et
  production_artifact_reuse.py réutilisent les sorties vérifiées.
  production_source_evidence.py valide les propositions contre l’archive.
- production_repairs.py connaît SupplementalSourceRepairState et
  ARCHIVED_PENDING_REFERENCES: archiver n’intègre pas la source. Son chemin
  SOURCE_CORPUS marque cependant REFERENCES, EXTRACTION, SYNTHESIS,
  EDITORIAL_ENRICHMENT et PUBLICATION stale puis repart de REFERENCES. E2a
  réutilise acquisition/checkpoints sans déclencher cette invalidation globale.
- production_synthesis_revision.py::_rebase_enrichment_if_valid revalide
  ancres et evidence; _assemble_and_render appelle le chemin normal de
  publication. publication_builder.py::_project_publication_sources perd
  actuellement editorial_role, provenance et criticité;
  domain/publication.py::PublicationSourceV1 doit les porter pour E2a.

**Run lu sans écriture.** Sujet d4be821b-a70a-4902-ab1a-de16d8fe42e9,
« GTG-30004 — profilage OSINT et obfuscation d’un dérivé de NanoDump »;
run courant 3011987d-719f-4124-8df1-e90dd33a3d5a, génération 5, READY.

| Stage | Artefact/version · SHA-256 canonique (préfixe) |
|---|---|
| REFERENCES | bdf39022-502c-4377-a539-b5f0f50e2bd8 / v1 · 4e122a200eca |
| EXTRACTION | 21208e66-9157-42ff-97cf-604eecacebe3 / v1 · blob e5165550c7d3; hash domaine 3328c2e042df |
| RELEVANCE_PROJECTION | 96c1666a-5070-4baa-9c7a-3838c7021d65 / v2 · projection 06febb268de7 |
| SYNTHESIS | ae70c1aa-c336-4cd8-810e-3be4e8afb328 / v2 · ba6872045768; extraction_hash 3328c2e042df |
| EDITORIAL_ENRICHMENT | 80fdd2b8-7c21-415d-ac5f-8a57c4926ded / v2 · 0061724803d9 |
| PUBLICATION | 916391c5-88ea-401a-9891-18940fd12188 / v2 · 5aad1987effb; rendu Typst/PDF 06018a7e-a108-4b5a-9b25-d3053e060abe · output d8d1a42deaf7 |

L’archive Anthropic du 10-09-2026 (document
4d6df629-26e7-4d2d-83cc-23615d4a2d0f, SHA décodé
74edd47450dc1e5ed676b554ae8fab57e14008275b9987a9521280d261f544d8) contient
le passage NanoDump. Deux URL archivées (la seconde avec ?page=1) produisent
deux faits issus du même passage: ce n’est pas une corroboration. La synthèse
a trois sections; le paragraphe technique NanoDump fait 61 mots. Le corpus
contient aussi la contre-analyse Krypt3ia du 14-09, bloquée, omise comme
reference_not_eligible. D la classe critique; le garde E1 ne contrôle que
SOURCE_EXTRACTION_FAILED. E2a doit refuser ce run tant que cette source
critique n’est pas réparée ou qu’une dérogation auditée n’est pas définie.
Les checkpoints sont 00851367-348e-4123-9b2d-374e0bf7bd61 (réutilisé) et
c230e52d-e16e-42b7-af4e-420e2d802ded (nouvel appel).

## 2. Options et recommandation

| Option | Invalidations/rebases et appels | Risque |
|---|---|---|
| A. Artefact d’extension chaîné à l’extraction de base | REFERENCES/EXTRACTION inchangées; ajouter un delta d’evidence. Une extraction neuve et une révision. | Hash effectif composite et lectures spéciales dans relevance, synthesis, enrichment et publication; risque qu’un consommateur lise une synthèse sans ses preuves; fences E1 dupliquées. |
| B. Nouveau REFERENCES+EXTRACTION canonique, corpus additif et checkpoints réutilisés | Versions suivantes calculées depuis l’ancien corpus plus l’addition. Anciennes sources réutilisées; seul le document ajouté est extrait. Rebaser relevance, synthèse ciblée et enrichment admissible; réassembler publication. | Le hash d’extraction change: vérifier le hash de base via les dépendances de l’ancienne synthèse, puis n’adopter le hash effectif que dans la révision append-only contrôlée. |

**Recommandation: B**, avec promotion cohérente des artefacts. Les lecteurs aval
gardent un seul format canonique, sans hash composite. Snapshot, archives et
checkpoints anciens restent valides; les nouveaux artefacts REFERENCES et
EXTRACTION deviennent la tête sans rendre les versions antérieures stale.
L’extraction des anciennes sources est rebasée sans modèle; une seule source
neuve peut nécessiter un appel. Pas de force_recompute. Avant promotion,
l’extension demeure ARCHIVED_PENDING_REFERENCES et le run reste cohérent.

## 3. Machine à états, identités et fences

1. INTENT: vérifier sujet, cible E1, instruction, acteur et base.
2. CANDIDATES: discovery, EDITORIAL_RESOURCE_PROPOSAL borné ou URL fournie
   par l’opérateur. L’opérateur choisit; critère et acteur sont journalisés.
   Aucune proposition n’est une preuve.
3. SELECTED → ARCHIVED: canoniser URL, vérifier scope, date et politique.
   Collecte standard; en 403/robots, archive_manual_content ne prend que les
   octets exacts détenus et fournis par l’opérateur. Sans archive admissible:
   refus. En reprise, résoudre collection_id/reçu/hash existants et ne jamais
   réarchiver un contenu déjà archivé.
4. EXTRACTED → READY: construire les versions prospectives depuis le corpus
   précédent. Vérifier la réutilisation des checkpoints anciens, extraire la
   nouvelle archive une fois, valider chaque evidence sur son texte, puis
   rebaser la projection avec les nouvelles preuves classées contexte.
5. PREVIEW: sans écriture, montrer sources/hashes, criticité, versions
   projetées, stages conservés/rebasés/invalidés, bloqueurs et appels faits
   ou restants. Pour une source unique, afficher recherche optionnelle,
   extraction (1 si cache absent) et révision (1); après extraction, distinguer
   le ModelRun persisté de l’appel de révision restant.
6. APPLY: réconcilier le ModelRun existant puis appeler une fois le reviseur.
   Sous fence run/generation, promouvoir REFERENCES, EXTRACTION, projection
   et synthèse; vérifier que seul le target et sa lineage changent. Une
   réponse archivée est adoptée, jamais soumise de nouveau.
7. APPLIED ou REFUSED: versionner la synthèse, rebaser/refuser l’enrichment,
   assembler/rendre la publication sans modèle. Le refus porte
   revision_not_applicable et recommended_restart_stage.

Un scope hors sujet, une source critique absente ou un patch non étayé produit
revision_not_applicable (respectivement REFERENCES, REFERENCES/EXTRACTION ou
SYNTHESIS comme restart stage).

L’identité canonique du manifeste source_additions_hash couvre URL, hash
d’archive, collection/document, checkpoint, handles d’evidence, critère,
acteur et versions de policy. Les fences gardent subject/run/generation;
base synthesis ID/version/input hash/SHA canonique; IDs et hashes REFERENCES,
EXTRACTION et projection; before_hash de la cible; hash du manifeste et
versions prompt/parser/validator. Le request_identity UUIDv5 et le ModelRun
de révision incluent le manifeste; l’extraction garde sa clé checkpoint
content-addressed existante.

## 4. Contrat proposé et invalidation/rebase

Ajouter les StrEnum SourceAdditionState (candidate/selected/archived/
extracted/ready/refused) et SourceAdditionOrigin
(discovery/operator_url/manual_archive), puis SourceAdditionV1 immuable:
URL canonique/finale, titre/éditeur/date, origin, collection_id,
source_document_id, SHA encodé/décodé, tier/kind/role,
editorial_role=CONTEXT, criticité/raison D, critère/version de sélection,
profile/profil_reason_code issus de extraction_profile_decision (TECHNICAL
donne IOC_RULES), acteur sélectionneur, checkpoint/extraction/ModelRun IDs et
handles ajoutés. Persister chaque transition comme version immuable dans
production_source_additions (addition_id, version, idempotency key request+
source), pour reprendre après crash sans payload API ni conversation. Les
blobs restent dans MinIO; références et artefacts canoniques restent en
PostgreSQL. Copier le manifeste trié dans metadata.source_additions de
REFERENCES, EXTRACTION et revision.source_additions.

La requête E1 reçoit source_additions_hash. Le preview reçoit le manifeste,
les IDs/hashes de base et projetés, les bloqueurs, l’état des appels par
recherche/extraction/révision/rebase/rendu et le restart stage. Un ModelRun
réussi déjà archivé réduit à zéro les appels restants de son étape.

| Stage | État après promotion |
|---|---|
| COLLECTION/checkpoint | Nouvelle archive et checkpoint vérifiés; checkpoints Anthropic réutilisés. |
| REFERENCES | Version +1 = corpus précédent + addition; v1 conservée, pas stale. |
| EXTRACTION | Version +1; anciennes sources identiques, une nouvelle; hash recalculé. |
| RELEVANCE_PROJECTION | Version +1 rebasée sans modèle; décisions inchangées, nouvelle evidence en contexte. Si impossible: refus/reprise à RELEVANCE_PROJECTION. |
| SYNTHESIS | Nouvelle révision; extraction_hash passe du hash de base au hash promu; autres sections/evidence inchangées. |
| EDITORIAL_ENRICHMENT | Rebasé sans modèle si ancres/handles valides; sinon seules les données dépendantes deviennent stale pour E2b. |
| PUBLICATION | Nouvelle assemblée et nouveau rendu avec la source; aucun appel modèle. |

Étendre PublicationSourceV1 avec editorial_role, source_criticality,
criticality_reason et provenance vérifiable (collection/document/hash), en
conservant publisher/date/URL/tier/kind. Garder l’identité de l’acteur dans la
lineage d’audit.

## 5. Garde-fous anti-extrapolation

La source ajoutée porte tier TECHNICAL, kind TECHNICAL_RESOURCE et
editorial_role CONTEXT; chaque fait garde son source_document_id et son
handle. Ajouter EvidenceUseScope(StrEnum): TOOL_CONTEXT est dérivé
déterministement de la source CONTEXT; ACTOR_ACTIVITY exige une preuve
subject-scoped : le scope suit une décision `DIRECT` ou `CORROBORATION` de la
projection de pertinence. Les handles classés `CONTEXT` en sont exclus, y
compris ceux de la source ajoutée. Le wire du patch déclare claim_scope; le
prompt réserve la nouvelle source aux descriptions génériques de l’outil. Le
validateur refuse qu’un handle TOOL_CONTEXT soutienne une claim actor_activity;
une affirmation sur l’acteur exige au moins un handle issu d’une preuve du
Subject. URL proposée, recherche et critère de sélection ne sont jamais des
evidence.

Règle: « contexte sur NanoDump ≠ preuve que l’acteur a exécuté une chaîne non
décrite par la source ». Conserver la séparation des flux parallèles; ne pas
inventer intrusion, exécution, persistance ou C2. Les claims sans handle
vérifié et toute différence hors cible sont refusés par la validation E1.

## 6. Livraisons, parallélisme et décisions ouvertes

1. Acquisition/reuse: collection.py, production_references.py,
   production_extraction.py, production_source_evidence.py,
   production_repairs.py, production_resume.py, modèle/repository de
   production_source_additions et migrations/versions/0001_baseline.py.
   Tests clés: archive manuel/403, URL canonique, reprise de l’intent,
   checkpoints réutilisés, une extraction neuve, replay sans double ModelRun,
   D et omission critique.
2. Révision E2a: domain/production_editorial_revision.py,
   production_synthesis_revision.py, projection de relevance, publication.py
   et publication_builder.py. Tester base/effective extraction, fences
   concurrentes, non-cible inchangée, refus critique, lineage, publication et
   rebase enrichment. Réutiliser test_synthesis_canonical,
   test_production_synthesis_application, test_production_source_criticality
   et l’intégration Q2 reuse.
3. E2b peut avancer en parallèle dans production_enrichment_revision.py,
   domain/production_editorial_enrichment.py et ses tests après gel du format
   partagé EvidenceRef contexte/base. Sérialiser les edits à
   domain/production_editorial_revision.py, api/production.py et
   frontend/src/api/production.ts; UI après stabilisation du contrat.

Décisions: exiger la résolution de toute source critique bloquée
(recommandé), ou autoriser une dérogation explicite et auditée; définir la
sélection humaine et les règles date/licence des sources techniques; confirmer
si une recherche bornée compte séparément dans le preview. D classe aussi une
source TECHNICAL/CONTEXT non dupliquée CRITICAL/COVERAGE_UNKNOWN: conserver
cette règle prudente ou faire évoluer D explicitement. Pour le run observé,
Krypt3ia bloquée est à traiter à REFERENCES avant l’extension NanoDump.

## 7. E2a-1 : décisions d’implémentation

- `domain/production_source_additions.py` porte les enums `StrEnum`, le
  `SourceAdditionV1` immuable, les fences, le plan de promotion et le manifeste
  canonique trié. Un article devient `SUPPORTING/PUBLICATION`; une ressource
  technique devient `TECHNICAL/TECHNICAL_RESOURCE`. Toute addition conserve le
  rôle éditorial `CONTEXT`. `source_additions_hash` couvre l’URL finale, les
  deux SHA, la collection/document, le checkpoint/extraction/ModelRun, les
  handles, le critère/acteur et les versions de politique.
- `production_source_additions` est un journal append-only; les transitions
  et les plans READY sont des snapshots versionnés. L’identité idempotente est
  `(production_run_id, request_id, sha256(URL canonique))`. Le modèle est créé
  dans `0001_baseline`; l’UoW expose le repository typé.
- `SourceAdditionService.record_candidate/record_discovery_proposals` accepte
  aussi directement les URL de `EditorialResourceProposalV1` déjà produites
  par la recherche bornée existante; il ne relance pas cette recherche et ne
  transforme aucune proposition en preuve. `select` journalise le critère,
  sa version et l’acteur. `acquire` reprend la collection par URL canonique,
  puis vérifie le reçu exact, les SHA, l’URL finale, la date et la politique
  avant d’extraire. Un retour après crash doit retrouver les mêmes IDs et
  empreintes; une autre archive sous la même URL donne un refus typé.
- Le chemin standard reste `collection.py`. L’archive manuelle n’est proposée
  qu’après `ACCESS_BLOCKED` (403/robots), uniquement avec les octets fournis
  par l’opérateur. Une addition impossible devient `REFUSED` avec code et
  stage de reprise; elle ne crée pas une issue de réparation du run.
- Le corpus REFERENCES prospectif est l’ancien corpus plus les additions
  archivées. `ProductionExtractionService.plan_for_corpus` reçoit seulement
  les IDs des nouveaux documents; les anciennes sorties d’extraction et leurs
  checkpoints ne sont pas recalculés. L’échec isolé d’une nouvelle source est
  refusé. La projection de pertinence conserve ses décisions existantes et
  classe les nouvelles evidence `CONTEXT` sans modèle.
- Les nouveaux REFERENCES et EXTRACTION reçoivent le manifeste dans
  `metadata.source_additions`. Les anciens artifacts ne sont ni réécrits ni
  marqués stale. Un bloqueur critique du corpus courant (dont Krypt3ia) reste
  un bloqueur; seule une archive ou une décision D `continue_without_source`
  effective, raisonnée et attachée à la génération courante le lève.
- L’interface de promotion est transactionnelle et ne commit pas:

  ```python
  promoted = await source_addition_service.promote(
      uow,
      production_run_id=run_id,
      request_id=request_id,
  )
  await synthesis_revision_service.revise_in_uow(
      uow,
      production_run_id=run_id,
      source_addition_promotion=promoted,
      ...,
  )
  await uow.commit()
  ```

  La livraison 2 doit fournir `revise_in_uow` et adopter le hash effectif
  d’EXTRACTION retourné par `promoted`, après avoir vérifié la dépendance de
  SYNTHESIS sur l’ancien hash de base. Si l’ajout des deux étapes échoue, elle
  rollbacke le même UoW. Un appel qui promeut REFERENCES/EXTRACTION sans la
  nouvelle SYNTHESIS laisserait l’ancienne `ProductionSynthesisV1.extraction_hash`
  derrière l’extraction courante; la méthode ne doit donc pas être commitée
  seule. Les blobs prospectifs adressés par contenu peuvent rester orphelins
  après un rollback, mais aucun nouvel artifact ne les rend courants.

  Le préflight de promotion recalcule les bloqueurs et compare la génération,
  les IDs/versions/input hashes et SHA-256 canoniques des trois artifacts de
  base. La promotion append REFERENCES, EXTRACTION et RELEVANCE_PROJECTION
  dans cet ordre transactionnel; elle ne touche pas à SYNTHESIS, ENRICHMENT
  ou PUBLICATION. Le résultat renvoie les artifacts projetés à la livraison 2.
- E2a-1 n’ajoute pas l’UI/API HTTP, la révision narrative, les scopes de claim,
  l’enrichissement ou la publication. Ces changements restent dans les
  livraisons suivantes et leurs fichiers réservés.

## E2a-2 : décisions d’implémentation

- L’application publique compose `promote_in_uow` et
  `ProductionSynthesisRevisionService.revise_in_uow` dans le même UoW, puis
  commit une seule fois. La révision revérifie les fences de base et le hash
  du manifeste, n’adopte que l’extraction promue et refuse toute différence
  hors cible; une erreur annule les artefacts promus et la synthèse ensemble.
- Le preview combine le plan READY de l’addition, les bloqueurs D recalculés,
  les versions prospectives et les appels restants. La source ajoutée fournit
  seulement `TOOL_CONTEXT`; chaque claim du patch déclare son scope et
  `ACTOR_ACTIVITY` exige un handle subject-scoped.
- La lineage de révision conserve le manifeste complet et chaque paragraphe
  conserve son artifact d’origine. La publication projette rôle, criticité,
  raison et identité archive/document/hash; le contrat de rendu est
  versionné et les champs ajoutés restent optionnels à la lecture des anciens
  artifacts.
- L’API expose uniquement sélection/acquisition, preview et apply. Le rebuild
  publication/QA reste sans modèle; l’enrichissement est rebasé uniquement
  après revalidation des ancres et handles, sinon ses blocs dépendants sont
  stale.
