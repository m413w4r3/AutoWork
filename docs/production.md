# Production canonique

## Pipeline et artifacts

L'étape du pipeline, son artifact et un rendu sont des notions distinctes. Le graphe canonique
est défini dans `domain/production_pipeline.py` :

| Étape | Artifact canonique |
| --- | --- |
| `SOURCES` | Aucun |
| `REFERENCES` | `REFERENCES` |
| `EXTRACTION` | `EXTRACTION` |
| `SYNTHESIS` | `SYNTHESIS` |
| `EDITORIAL_ENRICHMENT` | `EDITORIAL_ENRICHMENT` |
| `ASSEMBLY` | `PUBLICATION` |

```text
ProductionInputSnapshot
  ↓
REFERENCES → ProductionReferenceCorpusV1
  ↓
EXTRACTION → ProductionExtractionV1
  ↓
SYNTHESIS → ProductionSynthesisV1
  ↓
EDITORIAL_ENRICHMENT → EditorialEnrichmentV1
  ↓
ASSEMBLY → PublicationDocumentV4
        ↓
future frontière RENDER
```

`ASSEMBLY` écrit `PublicationDocumentV4` dans l'artifact `PUBLICATION`. `ASSEMBLY` ne compile aucun
média et ne rend aucun document ; la frontière `RENDER` future consomme `PublicationDocumentV4`
sans relire Synthesis ni Editorial Enrichment. La QA canonique passée, le run devient `READY`.

## Modèle

`Subject` est l’identité éditoriale stable matérialisée par Selection. Il conserve sa relation
avec l’édition, sa provenance Discovery/Fusion, ses sources et ses artifacts.

`ProductionRun` est une tentative historique de produire un `Subject`. Un sujet peut donc avoir
plusieurs runs, par exemple après un échec, une annulation ou une nouvelle génération. Un nouveau
run n’écrase jamais un run, un artifact ou une décision antérieurs.

`ProductionInputSnapshot` est créé dans la même transaction que son run : jamais de run sans
snapshot, jamais de snapshot sans run. Il fige exactement ce que cette tentative savait au moment
où elle a commencé. Une trigger PostgreSQL interdit tout `UPDATE` et tout `DELETE` de la table
`production_input_snapshots`.

En résumé :

- Selection crée un `Subject` ; elle ne crée jamais de `ProductionRun`, de batch ni de job.
- Production crée un `ProductionRun`, uniquement sur une action explicite de l’opérateur.
- Le `ProductionRun` fige son entrée.
- Une nouvelle vague Discovery, une Fusion ou un renommage du Subject n’altère jamais un run
  existant.
- Un nouveau run peut être créé ultérieurement sur le nouvel état ; il reçoit le `run_number`
  suivant du Subject (`UNIQUE(subject_id, run_number)`).

### Construction du snapshot

La résolution est toujours :

```text
ProductionRun.subject_id → Subject → SubjectDiscoveryOrigin
  → resolve_canonical_subject(origin.discovery_subject_id)
  → DiscoverySnapshot actif → member_candidate_ids → DiscoveryCandidate → SourceCandidate
```

Le snapshot capture :

- `subject_version`, `subject_title`, `subject_tlp` : un renommage ultérieur ne réécrit pas le run ;
- `selection_decision_id`, `origin_discovery_subject_id` (identité sélectionnée) et
  `canonical_discovery_subject_id` (identité courante après merges éventuels) ;
- `discovery_snapshot_id` et `discovery_snapshot_version` : le membership exact utilisé ;
- `member_candidate_ids`, uniquement des `DiscoveryCandidate.id` ;
- `discovery_summary`, issu du résumé du `DiscoverySubject` canonique ;
- `actor_or_campaign`, dédupliqué de façon déterministe depuis les candidates membres ;
- la période de l’édition et la `research_date` ;
- les `core_sources`, identifiées par `(discovery_candidate_id, source_candidate_id)`, avec URL,
  rôle, TLP, sensibilité et politique de modèle externe. Le `discovery_batch_id` n’est qu’une
  provenance de collecte.

Aucun titre normalisé, `local_ref` ou contenu de `DiscoveryBatch` ne sert de source fonctionnelle.

### Hashes

`input_hash` est le SHA-256 déterministe de toute l’entrée fonctionnelle, `research_date`
comprise. `reuse_basis_hash` couvre la même entrée sans `research_date` : c’est la base
d’égalité qui permet de réutiliser raisonnablement un stage coûteux entre deux runs. Aucun des
deux n’inclut d’identité technique (run, snapshot, job, conversation, horodatages) ; les listes
sont triées avant sérialisation. Chaque stage ajoute sa version de policy à son hash fonctionnel.

Tous les stages construisent leur contexte depuis ce snapshot. Son absence est une erreur
`production_input_snapshot_missing` ; il n’existe aucune lecture de repli.

### REFERENCES et corpus de production

Dans AW-010, l’artifact canonique de `REFERENCES` est `ProductionReferenceCorpusV1` : il décrit
les sources retenues pour ce run, leur provenance, tier, type, état de collecte, document archivé
exact, hash du contenu et éligibilité à l’extraction. Les sources du snapshot sont conservées
comme `CORE`; la recherche web peut ajouter des références `SUPPORTING` ou des ressources
`TECHNICAL`. Une source inaccessible reste dans le corpus avec son état et n’est pas éligible.

Le blob RAW conserve le wire format de recherche historique, notamment
`editorial-title` et `EVENT`, pour la lecture des anciens imports. Ces champs ne font pas partie du
corpus canonique. `REFERENCES` appelle `ModelGateway` sans conversation canonique. Le hash
fonctionnel de l’étape permet la réutilisation d’un artifact compatible entre runs; le corpus
réutilisé ne porte donc pas d’identité de `ProductionRun`.

Le `Reference corpus` du domaine malware/investigation et `ProductionReferenceCorpusV1` de la
production éditoriale sont deux contrats distincts : ils ne partagent ni module ni service.
La projection historique `ReferenceReport` reste confinée à certaines fonctions du Repair Desk.
Le Production State V5 transporte les quatre artefacts canoniques vérifiés avant Assembly :
`ProductionReferenceCorpusV1`, `ProductionExtractionV1`, `ProductionSynthesisV1` et
`EditorialEnrichmentV1`. L’import restaure ces artefacts puis place le run en revue à `ASSEMBLY` ;
son retry reconstruit `PUBLICATION` et exécute la QA sans rejouer les étapes antérieures.
Le chemin courant REFERENCES → EXTRACTION → SYNTHESIS → EDITORIAL_ENRICHMENT
→ ASSEMBLY lit directement
les contrats canoniques et n'utilise plus les `EVENT` Q1 comme identité de publication.

### EXTRACTION et contrat canonique

`REFERENCES` choisit et archive le corpus ; `EXTRACTION` ne collecte rien. Le stage lit
exclusivement `ProductionReferenceCorpusV1`, résout chaque source éligible par son
`source_document_id` exact, vérifie le SHA-256 du blob archivé contre `content_sha256` avant tout
appel, puis analyse le contenu archivé. Aucune URL n’est rouverte sur le Web, aucune source n’est
rafraîchie, et le RAW de REFERENCES n’est jamais une entrée d’Extraction.

Le profil est déterminé par le tier figé par REFERENCES :

```text
CORE                → FULL
SUPPORTING          → IOC_RULES
TECHNICAL           → IOC_RULES
```

`SourceRole` ne décide plus du profil. Une source `eligible_for_extraction == False` ne provoque
aucun appel modèle : elle reste visible dans le corpus et devient une omission contrôlée du plan
(`reference_not_eligible`). La politique est versionnée
(`EXTRACTION_PROFILE_POLICY_VERSION = "production-reference-tier-v1"`) et participe aux hashes
fonctionnels.

Aucune proposition modèle ne devient canonique sans preuve locale : chaque fait, événement,
indicateur et règle est vérifié contre la représentation déterministe du document archivé exact
(`SOURCE_TEXT_CONTRACT_VERSION`). Une règle doit réellement être publiée dans la publication
analysée ; un simple lien vers une règle ne suffit pas. La chronologie (`event_date`, `date_text`,
`text`, preuve) fait partie de l’extraction FULL : c’est désormais la source de la timeline
d’AW-012, qui n’a plus besoin des `EVENT` Q1 de REFERENCES.

Le résultat canonique du stage est `ProductionExtractionV1` : sources canoniques avec
`source_document_id`, `canonical_url`, `content_sha256`, `tier`, `kind`, `role`, `profile`,
checkpoint et `reuse_state`, plus les omissions et les warnings. Le run-level `ProductionArtifact`
pointe sur ce blob versionné, porte des compteurs bornés et laisse `model_run_id` vide : la
provenance modèle appartient aux checkpoints source-level.

Les checkpoints `source_extractions` sont content-addressed et indépendants du Subject et du
`ProductionRun` : même contenu, même profil et mêmes versions de contrat, prompt, parser,
verifier, texte source et policies ⇒ réutilisation sans appel modèle. Un contenu, un contrat, un
parser, un verifier, un profil ou une policy différent ⇒ miss. Un checkpoint `IOC_RULES` ne
satisfait jamais `FULL` ; la projection inverse (`FULL` → `IOC_RULES`) est déterministe et
testée. Deux URLs au contenu identique restent deux sources canoniques : la déduplication porte
sur le calcul, jamais sur la provenance.

Extraction est provider-agnostic : le stage demande une capacité structurée à `ModelGateway`
(profil, TLP, `external_llm_allowed`, `do_not_submit`, taille) et le router choisit l’adapter
autorisé. Le domaine ne connaît ni provider ni nom de modèle.

Chaque élément canonique porte la citation locale qui le prouve, calculée par le gate de preuve à
partir de l’archive exacte ; la citation éventuellement proposée par le modèle n’est qu’un point
d’ancrage à retrouver. Un événement daté n’est retenu que si la même zone structurelle du document
énonce cette date (ISO, formes anglaises et françaises usuelles) : une date n’est jamais estimée
ni empruntée à un paragraphe voisin. L’agrégation ne supprime aucune source : un élément publié
par plusieurs documents reste dans l’entrée de chacun et nomme l’union des documents qui le
publient.

Échecs : une source `CORE` sans extraction FULL vérifiée bloque le stage
(`extraction_core_source_failed`, avec `source_document_id`, `canonical_url` et
`source_failure_code`) avant tout appel pour les sources complémentaires. Une source
`SUPPORTING`/`TECHNICAL` en échec devient une omission `source_extraction_failed` portant son code
d’erreur, et la production continue. Une panne prouvée avant soumission n’empêche pas les autres
sources d’atteindre leur checkpoint durable ; le stage est ensuite rejoué avec les mêmes identités
de `ModelRun`. Une soumission possiblement acceptée arrête tout et passe en `NEEDS_REVIEW` avec
l’identité exacte à réconcilier.

`FULL` est toujours traité source par source ; les petites captures `IOC_RULES` peuvent être
regroupées dans un lot, uniquement entre captures soumises à la même politique de diffusion. Les
handles `B#` du lot sont temporaires : une réponse non attribuable sans ambiguïté est rejetée pour
la source concernée, qui est relue seule, et aucun IOC n’est redistribué entre publications.

La progression publiée par le stage liste chaque source du corpus avec son tier, son profil et son
verdict (`succeeded`, `cached`, `failed`, `omitted`).

Les consommateurs historiques (export d’état et certaines fonctions du Repair Desk) peuvent
encore lire `TechnicalExtraction` via la frontière legacy `application/production_extraction.py`.
Cette frontière projette `ProductionExtractionV1` vers le contrat legacy, jamais l’inverse, et
peut encore utiliser les labels `S1`… de REFERENCES. Synthesis ne passe plus par cette projection.

### SYNTHESIS : rédaction fondée sur l’extraction canonique

Le flux AW-012 est :

```text
REFERENCES sélectionne et archive les sources
        ↓
EXTRACTION établit les faits structurés et prouvés
        ↓
ProductionExtractionV1
        ↓
SYNTHESIS organise et rédige exclusivement à partir de cette vérité factuelle
        ↓
ProductionSynthesisV1
        ↓
EDITORIAL_ENRICHMENT produit EditorialEnrichmentV1
        ↓
ASSEMBLY lit aussi ProductionReferenceCorpusV1
        ↓
PublicationDocumentV4 → QA canonique → READY
```

`ProductionExtractionV1` est l’unique vérité factuelle de Synthesis. Le stage construit un pack
d’évidence déterministe à partir de l’extraction et du contexte éditorial figé dans le snapshot ;
chaque affirmation factuelle canonique doit citer une `ExtractionEvidenceRefV1`. Synthesis
n’effectue aucune recherche Web, ne rouvre aucun corps source pour découvrir des faits, ne prend
pas `ReferenceReport` ni `TechnicalExtraction` comme entrées canoniques et ne consomme pas l’état
`EVENT` Q1 de REFERENCES. Une information absente de l’extraction est omise.

Le brouillon passe par `ModelGateway.draft` sous forme stateless, avec Web désactivé et sortie
structurée. L’identité durable du `ModelRun` porte la soumission et sa réconciliation : une
soumission probablement acceptée n’est pas rejouée automatiquement et requiert une revue. Une
sortie structurée invalide passe également en revue ; elle n’ouvre pas de conversation de
réparation de format. La validation de proposition et le contrôle de son ancrage dans l’évidence
ont lieu aux frontières gateway et application.

Le canonical artifact est `ProductionSynthesisV1`, enregistré dans `canonical_blob_id`. La vue
frontend lit cette valeur canonique et résout chaque `ExtractionEvidenceRefV1.source_document_id`
dans les métadonnées de source de `ProductionExtractionV1` pour présenter les documents associés.
Elle ne résout pas les handles temporaires du prompt et ne prend pas le Markdown rendu pour
source. `rendered_blob_id` peut contenir un aperçu Markdown déterministe optionnel.

### EDITORIAL_ENRICHMENT : contrat canonique AW-015

`EditorialEnrichmentV1` est le contrat sémantique canonique : il décrit les intentions de tables,
diagrammes et figures sources sans syntaxe de renderer ni média dérivé. Les cellules de table, nodes et edges portent des références
d’évidence appartenant à l’extraction courante. Les figures désignent un `source_document_id`
canonique et son URL exacte. Les placements par section sont liés au hash exact de la synthèse.

AW-016 consomme uniquement les artifacts canoniques `EXTRACTION` et `SYNTHESIS` ainsi que les
métadonnées d’accès exactes des sources. Le modèle propose des tableaux indépendants du renderer et
des diagrammes sémantiques via une sortie structurée stricte. Chaque ligne, nœud et arête résout
ses handles vers des `ExtractionEvidenceRefV1` exactes. Une proposition vide reste valide lorsque
les données ne gagnent rien à être représentées autrement. Une panne modèle ou une politique qui
interdit la soumission ne produit jamais un enrichissement vide artificiel.

Aucun langage de renderer ni locator d’image source n’est généré par le modèle. AW-016 ne réalise
pas de recherche web ; le stage s’appuie sur AW-017b pour inventorier les figures des documents et
blobs déjà archivés. L’appel est stateless, sans recherche web ni conversation. L’artifact versionné conserve
la provenance `ModelRun`, le RAW fournisseur disponible et les hashes fonctionnels du pack de
preuves et de la politique d’accès. Une nouvelle version invalide `PUBLICATION`. Le stage peut
consommer au plus un appel modèle ; un reuse exact ou un artifact vérifié déjà présent n’en coûte
aucun. Un retry explicite depuis ce stage force son recalcul cross-run.
Une soumission possiblement acceptée par le fournisseur passe le run en `needs_review` avec le code
partagé `model_submission_reconciliation_required` et l’identité exacte du `ModelRun`, comme
`SYNTHESIS` : seule l’adoption de la réponse existante le débloque, jamais une resoumission.

Une réparation IOC ou règles n’appelle jamais le modèle et ne fabrique pas d’enrichissement vide :
l’enrichissement courant est rebasé tel quel sur la nouvelle lignée extraction/synthèse lorsque
toutes ses preuves citées subsistent. Sinon, la réparation devient `retry_required` depuis
`editorial_enrichment`.

### AW-017a : compilation déterministe des diagrammes

```text
AW-016
EditorialEnrichmentV1
  └── DiagramSpecV1

AW-017a
DiagramSpecV1
  → D2 0.9.0
  → SVG
```

`DiagramSpecV1`, contenu sémantique canonique d’`EditorialEnrichmentV1`, peut être compilé de façon
déterministe en source D2 puis en SVG par le compiler D2 0.9.0. D2 et le SVG sont des médias dérivés,
pas des artifacts canoniques. Cette capacité de compilation n’est ni un `ProductionStage` ni un
`ProductionArtifactStage` et ne modifie donc pas le graphe ni la table canoniques ci-dessus.

Le programme D2 est construit par AutoWork uniquement : identifiants synthétiques `n001`/`g001`
dans l’ordre canonique, labels en chaînes D2 entre guillemets doubles avec échappement de `\`,
`"`, `$` et des sauts de ligne. Le binaire est lancé sans shell (`--layout=dagre`,
`--omit-version`, `--stdout-format=svg`, salt dérivé du hash sémantique du diagramme), source sur
stdin, SVG sur stdout, 10 s et 2 MiB au plus. Le SVG n’est accepté qu’après validation : XML
analysable, racine `svg`, ni `script` ni `foreignObject`, références limitées aux fragments `#id`
et aux polices `data:` embarquées par D2. La policy `diagram-d2-svg-v2` est distincte de la version
du binaire et évolue avec tout changement volontaire des bytes produits.

Le stage `EDITORIAL_ENRICHMENT` compile les diagrammes et persiste les SVG avec les médias gérés par
`MediaAssetStore`. `SourceFigureInventory` inventorie les figures locales ; `SourceFigureIngestor`
valide et archive celles retenues. Assembly projette les tables, les diagrammes déjà compilés
(`compiled_asset_id` → `asset_id`) et les figures `INCLUDED` résolues dans `PublicationDocumentV4`.
Les figures `PROPOSED` ou `EXCLUDED` n’y figurent jamais : Assembly ne décide rien éditorialement,
ne compile aucun diagramme, ne télécharge aucune image et n’appelle aucun modèle. La compilation D2
ne remplace pas le renderer documentaire actuel.

Assembly vérifie le lineage du snapshot, des références, de l’extraction, de la synthèse et de
l’enrichissement avant de construire `PublicationDocumentV4`. Le titre, le lead, les sections, la
chronologie et les incertitudes viennent de Synthesis ; les IOC confirmés viennent d’Extraction.
Les sources sont résolues par `source_document_id`. Le hash d’Assembly dépend des cinq entrées canoniques, de
la version de document (`4`) et de la policy (`2`), sans version de renderer, D2, Pandoc ni Typst :
les mêmes entrées ne réutilisent donc jamais un ancien artifact d’une autre version. Le hash de l’enrichissement
fait partie de l’identité d’Assembly depuis AW-015.
`PublicationDocumentV4.sources` couvre exactement les sources utilisées, enrichissement compris.
QA recalcule la projection V4 depuis les cinq entrées canoniques et compare le document exact.
L'artifact `PUBLICATION` conserve le document canonique, sans rendu, sans source D2, sans SVG
en ligne et sans Typst. Pandoc reste limité à la narration ; il ne rend ni tables, ni diagrammes,
ni figures.

AW-016 produit des propositions structurées. AW-017a, AW-017b et AW-017c fournissent la compilation
des diagrammes, l’inventaire des figures et la persistance des médias. AW-018 projette
l’enrichissement et les médias dans `PublicationDocumentV4`.

## ProductionBoard

Le board d’une édition est lu par :

```text
GET /api/editions/{edition_id}/production
```

Il retourne `200` même lorsqu’aucun sujet n’est éligible et expose :

- les sujets matérialisés et leur capacité à démarrer un run ;
- le batch actif, sa progression et ses étapes ;
- les batchs récents et leurs résultats.

Le board ne lit pas la route Selection. Une édition `ARCHIVED` reste lisible, mais son board est
en lecture seule : aucun nouveau batch ni changement de sujet n’est accepté.

## Création d’un batch

L’opérateur choisit explicitement les sujets et leur ordre canonique. La commande est :

```http
POST /api/editions/{edition_id}/production/batches
Idempotency-Key: aw009-example
Content-Type: application/json

{"subject_ids": ["<subject-a>", "<subject-b>"]}
```

`ProductionBatchService.create` est l’unique primitive de création, pour un seul sujet comme pour
plusieurs. Dans une transaction, elle verrouille l’édition, vérifie qu’elle est ouverte, puis
valide chaque sujet (existence, appartenance à l’édition, `SubjectDiscoveryOrigin`, absence de run
`QUEUED`/`RUNNING`) et capture son snapshot avant toute écriture. Un seul sujet invalide annule tout :
aucun batch, aucun run, aucun snapshot. L’ordre du payload devient l’ordre du lot ; il n’est
jamais retrié. Le job `production.subject.sources` du premier run n’est soumis qu’après le commit :
si le dispatcher échoue, un replay exact avec la même clé reprend la soumission.

La clé d’idempotence est liée à `(edition_id, Idempotency-Key)` et à l’empreinte de
`edition_id` et des `subject_ids` ordonnés. Un replay exact retourne le même batch sans créer de
nouveau run ni de nouveau job logique.

| Cas | Réponse |
| --- | --- |
| `Idempotency-Key` absente, `subject_ids` absent ou vide | `422` |
| édition ou sujet inconnu | `404` (`edition_not_found`, `production_subject_not_found`) |
| édition archivée | `409 production_edition_archived` |
| même clé, autre payload (y compris autre ordre) | `409 production_idempotency_conflict` |
| nouvelle clé pendant un batch actif | `409 production_batch_active` |
| sujet ayant déjà un run actif | `409 production_subject_active` avec les `subject_ids` |
| sujet sans origine Discovery ou d’une autre édition | `409` avec le `subject_id` concerné |

Un batch n’est qu’une enveloppe de séquencement (ordre, pacing, annulation groupée). Un seul run
est `RUNNING` à la fois ; un run terminal (`READY`, `NEEDS_REVIEW`, `FAILED`, `CANCELLED`) passe
la main au suivant. Plusieurs vagues peuvent se succéder dans l’édition : `[A, B]`, puis `[C]`,
puis de nouveau `[A]`. L’édition ne porte aucun statut ni phase de production.

L’historique d’un sujet est lu par `GET /api/subjects/{subject_id}/production/runs` (du plus
récent au plus ancien), un run par `GET /api/production/runs/{run_id}`, et
`GET /api/subjects/{subject_id}/production` reste un raccourci vers le dernier run.

La surface Production utilise exclusivement cette commande avec des `subject_ids` explicites. Elle
ne compose pas un lot à partir de Selection, n’appelle aucune API Selection et ne déduit jamais un
sujet depuis un titre, une position ou une ressemblance visuelle.

## Pipeline et états

Chaque run traverse la pipeline statique suivante :

```text
SOURCES → REFERENCES → EXTRACTION → SYNTHESIS → EDITORIAL_ENRICHMENT → ASSEMBLY → READY
```

Chaque étape produit un artifact versionné et adressé par les entrées fonctionnelles, le run et la
génération de pipeline. Les états, erreurs, retries et progressions sont conservés dans
PostgreSQL ; Redis et les workspaces ne sont pas des sources de vérité.

L’annulation d’un batch actif arrête les runs non terminés, conserve les artifacts déjà produits
et laisse l’édition ouverte. Une nouvelle commande, avec une nouvelle clé, peut démarrer un nouvel
état et un nouveau run lorsque le board le permet.

## Review et publication

La review examine le run, sa génération et l’artifact exact du document. L’acceptation crée un
manifeste de publication immutable contenant l’ordre, les `subject_id`, les runs, les artifacts,
les versions et les hashes retenus. L’assemblage et les rendus lisent uniquement ce manifeste.

Une nouvelle production ne modifie donc pas les snapshots, artifacts ou manifestes historiques.
