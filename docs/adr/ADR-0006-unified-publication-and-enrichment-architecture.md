# ADR-0006 — Unité éditoriale unique, publication et enrichissements techniques

Statut : accepté — 2026-08-28

## Contexte

AutoWork possède déjà des frontières robustes pour la persistance, les blobs, les jobs et la
traçabilité, mais le parcours éditorial reste marqué par une distinction historique entre
`brief` et `major`. Cette distinction apparaît notamment dans les profils de production et dans
l'ancien parcours evidence-first des brèves. Elle ne correspond plus à la direction produit : à
terme, AutoWork ne publiera qu'un seul type de contenu éditorial. Certains sujets contiendront des
IOC et déclencheront alors des traitements techniques supplémentaires, notamment des
consultations et pivots VirusTotal, sans devenir pour autant un autre type d'article.

L'interface doit également permettre d'accéder facilement, depuis un sujet, au contenu publié,
aux IOC associés, aux sources et, lorsqu'ils existent, aux fichiers acquis. Ces données n'ont ni
la même volumétrie ni la même sémantique : le texte destiné au bulletin ne doit pas devenir le
stockage opérationnel des IOC, et les fichiers volumineux ne doivent pas être chargés avec les
écrans de liste.

Les invariants d'architecture existants restent applicables : PostgreSQL est canonique pour les
identités, métadonnées, relations et événements ; les contenus volumineux vivent dans le blob
store ; le filesystem est une projection reconstructible ; les jobs asynchrones conservent leur
état canonique dans PostgreSQL et Dramatiq/Redis ne transporte que leur identifiant.

Cette décision fixe la cible avant la refonte de l'interface, la review d'édition, le gel de
publication et l'ajout ultérieur de l'enrichissement IOC/VirusTotal. Elle vise à éviter que ces
travaux introduisent de nouvelles dépendances au vocabulaire `brief/major` ou un second workflow
parallèle.

## Décision

### 1. `Subject` est le pivot stable d'un contenu éditorial

Dans le périmètre production/publication, `Subject` reste l'identité stable à laquelle sont
rattachés les sources, les productions, les IOC, les observations techniques et les fichiers.
Cette identité est un pivot relationnel ; elle ne signifie pas qu'un unique objet Python ou une
unique requête doit charger tout le dossier en mémoire.

Les identités et snapshots propres à la découverte conservent leur sémantique et leur lignée. Le
présent ADR ne redéfinit pas le modèle de découverte : il fixe la frontière à partir du sujet
sélectionné pour la production.

Aucune nouvelle entité métier `Brief` ou `MajorArticle` ne doit être introduite pour représenter
le contenu final.

### 2. La distinction `brief / major` devient explicitement legacy

Les profils, tables, endpoints et composants existants qui emploient encore `brief` ou `major`
peuvent rester temporairement en place tant qu'ils sont nécessaires au code courant. Ils sont des
mécanismes de compatibilité, pas une extension point.

Tout nouveau code doit être nommé selon sa fonction : `publication`, `content`, `item`, `review`,
`indicator`, `asset`, `enrichment` ou `production`. Il ne doit pas créer de nouvelle branche
métier fondée sur le fait qu'un sujet serait une brève ou un article majeur.

### 3. Le document éditorial et les données techniques sont séparés

Le modèle conceptuel cible d'un sujet est :

```text
Subject
├── Publication document
├── Indicator catalog
├── VirusTotal enrichments
├── Sources and samples
└── Production history
```

Le document de publication contient ce qui est nécessaire au lecteur du bulletin : titre,
chronologie, synthèse, références, incertitudes et, lorsque cela est éditorialement utile, un
snapshot d'IOC destiné à l'affichage.

Les IOC opérationnels ne sont pas stockés uniquement dans ce document. Ils possèdent leur propre
catalogue structuré, normalisé et indexable, avec leur provenance et leurs liens vers les
observations VirusTotal et les samples éventuellement acquis. Le document de publication peut
référencer ces informations ou en figer une représentation, mais il ne constitue pas leur source
opérationnelle.

Les `SourceDocument`, les `Sample` et les observations VirusTotal restent des objets distincts.
Partager un hash ou les mêmes octets ne leur donne pas la même sémantique.

### 4. Le contrat éditorial futur est `PublicationDocumentV2`

`BriefDocumentV1` reste un format sérialisé valide pour les productions existantes tant que leur
lecture est nécessaire. La cible est un contrat renderer-independent nommé
`PublicationDocumentV2`, qui représente l'unique type de contenu éditorial.

Le passage à V2 est un changement de schéma explicite. Une nouvelle version ne doit pas être
écrite sous le numéro de schéma V1 et les anciens artifacts ne doivent pas être réécrits en place.
Si une lecture V1 reste nécessaire pendant la migration, elle passe par un lecteur ou adaptateur
explicite vers la représentation interne courante.

Le document canonique reste indépendant de Markdown, DOCX ou de tout autre renderer.

### 5. La production cible utilise une pipeline unique et statique

La pipeline cible est :

```text
SOURCES
  → REFERENCES
  → EXTRACTION
  → SYNTHESIS
  → ASSEMBLY / QA
```

Cette séquence est déclarée explicitement dans le domaine. AutoWork ne construit pas de graphe de
workflow dynamique pour décider du parcours d'un sujet.

L'enrichissement IOC n'est pas encore un stage de cette production. Il fera l'objet du Prompt 7B
et restera une capacité distincte, raccordée après stabilisation de cette pipeline.

Les différences de traitement sont donc déterminées par les données et les capacités disponibles,
pas par un type éditorial `brief` ou `major`.

### 6. Les services spécialisés restent séparés de l'orchestrateur de production

L'orchestrateur de production coordonne les stages, leur idempotence et leurs transitions. Il ne
doit pas contenir directement toute la logique VirusTotal, d'acquisition de samples, de review ou
de publication d'édition.

Les frontières cibles sont les suivantes :

- production d'un sujet : orchestration et artifacts de stages ;
- enrichissement IOC : sélection des IOC éligibles, politiques, budgets et appels aux services
  techniques ;
- review d'édition : décisions humaines et calcul des blockers ;
- publication d'édition : gel immutable des artifacts retenus ;
- rendu d'édition : assemblage déterministe et export ;
- workspace : projection locale best-effort de l'état canonique.

Ces frontières peuvent être réalisées par des modules applicatifs distincts. Elles ne justifient
pas l'introduction d'un moteur générique de workflow.

## Modèle conceptuel et relations

Le modèle logique cible est orienté par identifiants et relations explicites :

```text
Subject
  │
  ├── SourceDocument ── Blob
  ├── Sample ────────── Blob
  ├── SubjectProductionRun
  │     └── ProductionArtifact
  │            └── PublicationDocument
  │
  ├── Indicator
  │     ├── source/extraction lineage
  │     ├── VirusTotalObservation
  │     └── Sample
  │
  └── production diagnostics/history
```

Le catalogue IOC futur doit pouvoir répondre efficacement aux besoins suivants sans décoder tous
les artifacts JSON du sujet :

- lister les IOC d'un sujet ;
- filtrer par type ;
- retrouver leur valeur normalisée ;
- connaître leur provenance ;
- savoir s'ils ont été enrichis ;
- retrouver les observations VirusTotal associées ;
- retrouver les samples acquis à partir d'un IOC.

Une projection SQL dédiée aux IOC est donc autorisée et recommandée lorsqu'elle sera implémentée.
Elle est dérivée d'un artifact d'extraction précis et reste traçable vers cet artifact ; elle ne
remplace pas l'historique immutable des artifacts. Les liens vers observations et samples utilisent
des tables relationnelles explicites plutôt qu'une référence polymorphique `type/id`.

## Pipeline, idempotence et reprise

Chaque stage produit un résultat identifiable par ses entrées fonctionnelles, ses versions de
contrat et son `pipeline_generation`. Les retries techniques d'un worker restent distincts d'un
retry métier qui crée une nouvelle génération de pipeline.

Une reprise ne doit jamais dépendre d'un workspace local. Elle repart de PostgreSQL et du blob
store, ou d'un artifact canonique déjà validé. Les jobs continuent à être adressés par identifiant
et à exposer leur progression, leurs retries et leurs erreurs depuis l'état canonique en base.

À l'intérieur d'une édition, la production reste séquentielle pour garder un comportement
prévisible vis-à-vis des modèles, des conversations, des quotas externes et du pacing. La
concurrence entre éditions indépendantes pourra être introduite plus tard sans changer le modèle
de sujet ou de publication.

## Compatibilité temporaire

La migration est désormais au cutover de la pipeline article. Les lecteurs historiques restent
progressifs afin de ne pas réécrire les documents et releases déjà produits.

Pendant cette période :

- `ProductionProfile.BRIEF_AUTO` et `ProductionProfile.MAJOR_ASSISTED` ne sont lus que par les
  compatibilités historiques nécessaires ;
- aucune nouvelle feature ne doit ajouter une condition fonctionnelle fondée sur ces profils ;
- `BriefDocumentV1` reste lisible mais n'est plus produit par la pipeline courante ;
- l'ancien parcours `BriefEvidencePack` / `BriefDraft` reste isolé ; il ne doit pas devenir une
  dépendance de la nouvelle review ou de la nouvelle publication d'édition ;
- les nouveaux endpoints et composants utilisent une terminologie générique même s'ils s'appuient
  temporairement sur des services internes portant encore un nom legacy.

Un adaptateur temporaire est préférable à un renommage transversal qui mélangerait changement de
vocabulaire et changement de comportement.

## Historique de la migration `brief / major`

Le cutover futur suit quatre étapes conceptuelles.

1. Les surfaces UI/API et les nouveaux use cases n'emploient plus la distinction.
2. `PublicationDocumentV2` devient le seul format écrit pour les nouvelles productions ; V1 reste
   uniquement un format historique lisible si nécessaire.
3. La configuration éditoriale d'une édition passe d'objectifs distincts `major/brief` à un objectif
   unique d'items/articles, et la production passe à une seule pipeline déclarée.
4. Les profils legacy et les composants exclusivement attachés à ce workflow restent limités à la
   lecture ou au parcours legacy identifié ; ils ne sont pas une dépendance de la production.

La base étant conçue pour être reconstruite depuis une migration cible sur une base vide, le
cutover de schéma doit préférer un modèle final propre à des colonnes legacy ou à des backfills de
compatibilité complexes. La compatibilité nécessaire aux artifacts historiques est traitée au
niveau de leurs lecteurs versionnés, pas en conservant indéfiniment deux modèles métier.

## Stockage et workspaces

PostgreSQL et le blob store restent les seules sources canoniques. Le filesystem ne devient jamais
une condition de succès métier.

Deux projections locales ont des responsabilités différentes :

- le workspace sujet est un laboratoire technique reconstructible pour les sources, samples,
  analyses, pivots et autres artifacts d'investigation ;
- le workspace édition est une vue éditoriale pratique pour les checkpoints, la review et la
  release.

La structure cible du workspace édition est :

```text
/work/editions/<YYYY-MM>_<COUNTRY_CODE>/
├── manifest.json
├── items/
│   └── <position>-<slug>/
│       ├── article/
│       │   ├── publication.json
│       │   └── publication.md
│       ├── indicators/
│       │   ├── indicators.json
│       │   └── enrichment.json
│       ├── sources/
│       │   └── manifest.json
│       ├── assets/
│       │   └── manifest.json
│       └── pipeline/
│           └── production-state.json
├── review/
│   └── review-snapshot.json
└── release/
    ├── publication-manifest.json
    ├── edition.json
    ├── edition.md
    └── bulletin.docx
```

Chaque manifeste de workspace indique explicitement qu'il n'est pas canonique. Les écritures sont
atomiques et best-effort. Une erreur filesystem est journalisée mais ne transforme jamais une
production réussie en échec.

Les gros samples ne sont pas recopiés automatiquement à chaque checkpoint. Leurs métadonnées et
références de blob sont présentes dans le manifeste ; les octets sont matérialisés seulement à la
demande ou lors d'une étape qui en a explicitement besoin.

## Review et publication

La review d'édition est distincte de la production d'un sujet.

Une décision de publication est append-only et attachée à l'objet exact examiné :

```text
PublicationReviewDecision
  edition_id
  subject_id
  production_run_id
  pipeline_generation
  document_artifact_id
  document_artifact_version
  document_input_hash
  decision = INCLUDE | EXCLUDE
  actor_id
  reason
  occurred_at
```

Une décision historique reste consultable, mais elle ne s'applique pas automatiquement à une
nouvelle génération ou à un nouvel artifact créé par retry.

Les règles par défaut sont :

- un item `READY` sans décision explicite est inclus ;
- une décision `EXCLUDE` l'exclut ;
- un item `FAILED`, `NEEDS_REVIEW`, `QUEUED` ou `RUNNING` bloque l'acceptation tant qu'il n'est pas
  devenu publiable ou explicitement exclu lorsque cette exclusion est autorisée ;
- les règles `included`, `blocking`, `can_retry` et `can_accept` sont calculées côté backend et ne
  sont pas réimplémentées comme logique métier dans React.

L'action d'acceptation de la production crée un `PublicationManifestV1` immutable. Ce manifeste
fige l'ordre éditorial et les références exactes de chaque document retenu : run, génération,
artifact, version et hash. Il enregistre également les exclusions effectives.

Seul le use case qui crée avec succès ce manifeste peut faire passer une édition de `REVIEW` à
`ASSEMBLING`. La transition ne doit pas être exposée comme une action générique indépendante de ce
gel.

L'assemblage d'édition lit uniquement les artifact IDs et hashes présents dans le manifeste. Il ne
résout jamais un "artifact courant" pendant le rendu. Il construit un document d'édition
renderer-independent puis produit les représentations Markdown/DOCX de manière déterministe et
sans appel à un LLM.

Une modification après gel nécessite un retour explicite en review et la création ultérieure d'un
nouveau manifeste ; un manifeste déjà créé reste immutable et historique.

## API et ergonomie

Les nouvelles APIs destinées à la consultation d'un sujet sont organisées par intention, par
exemple :

```text
GET /api/subjects/{id}/content
GET /api/subjects/{id}/indicators
GET /api/subjects/{id}/assets
GET /api/subjects/{id}/production
```

Les réponses de liste retournent des métadonnées et des références. Elles ne contiennent jamais
les octets d'un blob, d'une archive ou d'un sample. Les téléchargements utilisent une action
séparée et autorisée explicitement.

L'interface Edition est pilotée par l'état de l'édition et présente une seule surface principale à
la fois : découverte/sélection, production, review, assemblage ou release. Les diagnostics et
artifacts bruts restent accessibles, mais ils ne constituent pas le parcours principal.

L'interface Subject est organisée autour du besoin utilisateur : contenu, IOC, sources/fichiers,
puis pipeline/diagnostics. Les données coûteuses sont chargées paresseusement par onglet.

## Performance et scalabilité

Le modèle d'écriture reste normalisé et transactionnel. Les écrans de supervision peuvent utiliser
des read models applicatifs optimisés sans créer un second état canonique.

En particulier :

- le statut d'un batch destiné au polling doit être construit avec un nombre constant et faible de
  requêtes, pas avec une succession de lectures par item ;
- les listes d'IOC, sources et assets sont paginables et indexées sur leurs clés de rattachement ;
- les blobs volumineux ne sont lus qu'à la demande ;
- un `Subject` sert de pivot d'identité mais n'est jamais chargé comme un graphe ORM complet ;
- les artifacts et enrichissements sont append-only ou versionnés afin de rendre les retries
  idempotents et auditables ;
- les clés d'idempotence et hashes d'entrée protègent les jobs et les assemblages contre les
  doubles soumissions ;
- la concurrence future inter-éditions ne doit pas imposer de verrou global ni changer la
  sémantique de la production séquentielle intra-édition.

La scalabilité est obtenue par des frontières de lecture/écriture, des relations explicites, du
chargement paresseux et des jobs idempotents, pas par l'introduction précoce d'un orchestrateur
distribué supplémentaire.

## Conséquences

- La refonte UI, la review et la publication peuvent être développées sans dépendre du futur
  cutover `brief/major`.
- Le futur stage VirusTotal se branche sur une couture dédiée au lieu d'ajouter un nouveau profil
  éditorial.
- Les IOC deviennent consultables et indexables indépendamment du document final.
- Les sources et samples restent accessibles sans gonfler les contrats d'API usuels.
- Les releases sont reproductibles à partir d'un manifeste immutable et d'artifacts adressés
  explicitement.
- Une dette temporaire de vocabulaire subsiste dans le code legacy ; elle est acceptée pour éviter
  une migration transversale risquée avant la stabilisation du workflow end-to-end.
- Le nombre de petits services/read models peut augmenter, mais chacun porte une responsabilité
  étroite et testable ; cette modularité est préférée à des fichiers d'orchestration toujours plus
  volumineux.

## Non-objectifs

Cet ADR ne :

- modifie aucun comportement de production existant ;
- implémente pas `PublicationDocumentV2` ;
- implémente pas encore `IOC_ENRICHMENT` ni les pivots/downloads VirusTotal ;
- supprime pas encore `ProductionProfile`, `BriefService`, `BriefEvidencePack` ou `BriefDraft` ;
- ne modifie pas la lignée de découverte ;
- ne crée pas un nouveau cache canonique ;
- ne rend pas le filesystem nécessaire à la reprise d'un job ;
- n'introduit pas Temporal, Kubernetes ou un moteur générique de workflow ;
- ne requiert pas WebSocket ou SSE pour l'interface de production ;
- ne place jamais les octets de gros blobs ou samples dans les endpoints de liste.
