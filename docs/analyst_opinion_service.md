# Avis de l'analyste : brouillon, revue et publication

`AnalystOpinionService` conserve un brouillon consultable par enquête. Les
versions sont append-only et rattachées à une synthèse vérifiée et à une
PUBLICATION V5 précise. La composition construit uniquement les blocs pour
lesquels des findings apportent une information ; un seul bloc renseigné
suffit.

## États et revue

- `draft` : version éditable et encore non approuvée.
- `rejected` : version examinée pour laquelle l'éditeur a demandé des
  changements. Le contenu reste dans l'historique et peut être révisé dans une
  nouvelle version.
- `approved` : nouvelle version append-only qui reprend le texte exact de la
  version examinée, avec l'identité du reviewer. Comme le hash canonique inclut
  statut et reviewer, l'éditeur soumet le `content_sha256` du brouillon ; la
  réponse distingue `reviewed_version` et `approved_version`.

La révision compare `expected_version` à la dernière version (CAS). Une
approbation vérifie le hash soumis, les citations, l'identité de la synthèse,
la PUBLICATION V5 courante et la politique de divulgation. Une affirmation
présentée comme fait doit citer un finding `supported` dont les observations
existent encore dans la même enquête. Les réserves des findings restent dans
le texte des blocs. Les éléments qualifiés, réfutés ou sans observation
suffisante restent des inférences ou des hypothèses ; ils ne sont pas promus en
faits.

Le bloc « Vérification des affirmations » utilise un
`ClaimKnowledgeProvider` structural, compatible avec
`FindingService.knowledge_of_claim` sans dépendre de ses détails internes. Si
aucun provider n'est configuré, le service conserve cette limite dans les
points non résolus et omet le bloc.

## Publication augmentée

`AnalystPublicationService` charge une version approuvée précise, revalide ses
citations et sa politique TLP, puis délègue la persistance à
`PublicationAugmentationArtifactService`. Le résultat est un nouvel artefact
`PUBLICATION` V6 qui enveloppe le V5 de base et l'avis approuvé. Le blob V5
antérieur reste intact et le nouvel artefact porte
`previous_publication_artifact_id`.

La publication ne crée pas de `ProductionStage` et n'écrit dans aucune
composition, édition, review, release ou manifeste. Le réépinglage de la
nouvelle PUBLICATION dans une composition est une action distincte de
l'éditeur. Les autres sujets et les publications précédentes gardent leur
propre état de publiabilité.

## Actions et routes

- `GET /api/investigations/{id}/opinion` expose la version courante,
  l'historique, les violations de citation, les blocs rendables et leur
  provenance.
- `POST /api/investigations/{id}/opinion/compose` projette les findings
  courants et les observations choisies en blocs éditoriaux.
- `POST /api/opinions/{id}/revise` ajoute une version de brouillon sous CAS.
- `POST /api/opinions/{id}/approve` approuve le texte exact soumis et écrit une
  décision `NOTE_APPROVE` ciblant `NOTE` avec acteur, raison et corrélation.
- `POST /api/opinions/{id}/request-changes` écrit une version `rejected` et une
  décision `NOTE_CHANGES_REQUESTED`.
- `POST /api/opinions/{id}/publish` propose une nouvelle PUBLICATION V6 et
  indique que le réépinglage reste à faire par l'éditeur.

Les mutations exigent un `Idempotency-Key`. Les actions de revue portent
l'identité authentifiée, une raison et un hash de version ; les répétitions
identiques réutilisent la version ou l'artefact déjà produit. Codes d'erreur
principaux : `stale_version`, `version_hash_mismatch`, `citations_invalid`,
`tlp_forbidden`, `do_not_submit`, `stale_base` et
`opinion_not_approved`.
