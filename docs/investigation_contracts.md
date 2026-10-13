# Contrats de l’agent d’investigation

Ces contrats forment le socle partagé par la boucle Bridge, les outils et la
persistance. Le domaine reste pur : aucun appel réseau, accès base ou modèle
SQLAlchemy. Le lot A2 branche les Protocols de
`application/investigation_persistence.py` sur le Unit of Work et ajoute leurs
membres à `application/persistence.py`. Le lot A2b branche également le dépôt
d’état de boucle défini par le lot B.

## Domaine

`domain/investigation.py` expose les états typés `ToolInvocationStatus`,
`EvidenceKind`, `FindingKind`, `FindingAssessment`, `NoveltyClass`,
`SampleOrigin`, `SampleQualification`, `DetectionCandidateStatus`,
`BudgetDimension` et `AgentNextAction`. Les cinq décisions Bridge sont
`continue`, `switch_hypothesis`, `draft_opinion`, `finish` et
`request_permission`.

- Une invocation porte une identité d’opération déterministe (`tool_operation_key`),
  des paramètres JSON normalisés sans secrets, les coûts réservés/consommés,
  son état et les références de réponse. Les transitions sont versionnées.
- Une observation référence un blob et son SHA-256, une source/version, une date,
  l’objet exact et les limites. Elle est immuable.
- Un finding est versionné et append-only (`supersedes`). Un finding `supported`
  doit citer au moins une observation. La nouveauté est relative au snapshot de
  l’article (`NoveltyClass`), jamais une affirmation d’unicité mondiale.
- Une association de sample conserve l’origine et l’historique append-only des
  changements de qualification. Passer à `confirmed` exige l’identité d’un acteur
  humain ; un résultat Bridge ou une acquisition ne vaut pas validation G3.
- Une version YARA vérifie le SHA-256 de sa source et référence ses motifs et tests.
  Aucune règle universelle de nombre minimal de samples n’est imposée.
- `InvestigationBudget` réserve, engage et libère des dimensions configurables de
  façon idempotente. `BudgetExceeded.dimension` nomme le plafond atteint.
  `investigation_budget_from_loop_budget` projette seulement `max_cycles` vers les
  tours Bridge ; `LoopBudget` reste intact.

## Avis et citations

`AnalystOpinionV1` pointe vers les identités exactes de la synthèse et de la
publication de base. Ses sept types de blocs suivent le §1.1. Les blocs vides ne
sont pas rendus ; un seul bloc renseigné suffit. `content_sha256` couvre une
projection JSON canonique triée et stable.

`verify_opinion_citations` signale toute référence absente. Un bloc qualifié de
fait doit citer un finding `supported` dont toutes les observations existent.
Les inférences et hypothèses restent explicitement qualifiées et peuvent porter
leurs réserves sans être promues au rang de fait.

## Bridge et outils

`InvestigationContextV1` sépare les vues `full` et `delta`. Le delta ne contient
pas le texte de l’article ; tailles, listes et chaînes sont bornées. Les payloads
refusent octets, champs de secrets et URLs signées. `AgentTurnDecisionV1` garde
les arguments de tool libres : seul le schéma JSON réellement enregistré les
valide dans `RegistryToolDispatcher`.

`InMemoryToolRegistry` rend visibles les outils dont l’adaptateur et la capacité
sont présents. Ajouter ou retirer un tool ne change pas le dispatcher. Une
capacité absente ou un tool inconnu rend `unavailable` localement ; permissions,
effets et budgets refusent uniquement l’opération concernée et proposent les
alternatives disponibles. Le validateur convertit le sous-ensemble utilisé des
JSON Schema Draft 2020-12 en validateurs Pydantic stricts (types, propriétés,
tableaux, contraintes et combinateurs). Il refuse à l’enregistrement les mots-clés
qu’il ne valide pas, comme `$ref` et `patternProperties`, au lieu de les ignorer.

## G2 / G3 et périmètre

- **G2** : `InvestigationEnvelope` enregistre fournisseurs autorisés, budget,
  droit d’acquérir des hits, bornes TLP/divulgation et identité de la décision et
  de son acteur. Les pivots passifs compris dans cette enveloppe n’exigent pas un
  clic par hypothèse.
- **G3** : la qualification d’un hit comme membre confirmé reste une décision
  humaine identifiée, inscrite dans `QualificationChange`.

Le lot B a livré les types, ports, fakes en mémoire et tests purs, sans modèle
SQLAlchemy pour l’état de boucle. Il ne livre pas les outils VT, Shodan ou YARA,
ni API, UI ou publication V6.

## Persistance

Le lot A2 branche les Protocols sur PostgreSQL dans les tables
`tool_invocations`, `evidence_observations`, `investigation_finding_versions`,
`investigation_finding_observations`, `investigation_finding_claims`,
`sample_associations`, `sample_association_versions`,
`sample_association_qualification_changes`, `detection_candidate_versions`,
`detection_candidate_motifs`, `detection_candidate_motif_observations`,
`detection_candidate_test_refs`, `analyst_opinion_versions`,
`investigation_envelopes`, `investigation_budget_records`,
`investigation_budget_limits`, `investigation_budget_reservations` et
`investigation_budget_reservation_amounts`. Les tables d'enquête référencent
`analyst_investigations`; les observations référencent `blobs`, et une
association de sample lie `samples` quand le SHA identifie un sample sans
ambiguïté.

Le lot A2b ajoute `investigation_working_states`, une ligne par enquête. La
colonne JSONB `state` contient le document canonique compact
`investigation-state.v1`; `investigation_id`, `version`, `turn_index` et
`stop_reason` sont aussi exposés en colonnes, avec `updated_at` pour le suivi.
Des contraintes vérifient le schéma, la cohérence des colonnes recopiées et les
valeurs des enums d’arrêt et de statut d’hypothèse. `save_if_version` conserve
la sémantique du dépôt mémoire : une sauvegarde identique est idempotente, la
première version est v1 et une mise à jour exige la version attendue puis avance
d’exactement un cran. La lecture reconstruit l’état typé et rejette un JSON qui
diverge de ses colonnes indexées.

Les contextes Bridge en attente ne reçoivent pas de table SQL supplémentaire.
`ProductionArtifactContextSnapshotRepository` stocke déjà leur JSON canonique
dans le stockage immuable de blobs sous une identité de contenu; l’état garde le
pointeur et le SHA-256 pour permettre la reprise après relecture.

Les clés d'idempotence et les versions sont portées par la base :

- `ToolInvocation.operation_key` est unique ; les transitions comparent la
  version attendue.
- Une observation est unique par enquête, SHA-256, type et objet. Ses liens de
  blob et d'invocation sont des clés étrangères. Une réutilisation d'identifiant
  avec un contenu différent est refusée.
- Chaque finding est une version identifiée, reliée à son prédécesseur par
  `supersedes_id`. Les observations sont en table de liaison ordonnée et les
  références de claims sont conservées séparément.
- L'identité d'association sample est séparée de ses versions et des changements
  de qualification append-only. La base refuse une qualification confirmée sans
  acteur humain identifié ; le domaine valide aussi l'historique au chargement.
- Les versions de détection sont uniques par `(candidate_id, version)` et
  référencent leur parent. Les versions d'avis sont uniques par
  `(opinion_id, version)` et enregistrent le statut et `content_sha256`.
- `InvestigationEnvelope` et `InvestigationBudgetRecord` comparent leur version
  avec un CAS. Les limites, réservations et engagements budgétaires sont
  reconstruits en `InvestigationBudget` depuis des lignes typées.

Les rapports bruts et sorties d'analyse volumineuses restent dans MinIO ;
`EvidenceObservation` ne garde que `blob_id`, SHA-256, provenance, objet et
limites. Les paramètres d'outil et les blocs d'avis sont des métadonnées JSONB.
Le contrat A1 ne fournit pas de référence MinIO pour le texte source YARA ni de
limites de taille pour ce texte, les paramètres et les blocs d'avis. La version
candidate conserve donc la source YARA avec son SHA-256 dans
`detection_candidate_versions.source` ; ces champs restent un écart à traiter
si leur taille devient importante.
