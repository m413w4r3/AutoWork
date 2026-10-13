# Revue humaine des règles YARA et archive d’édition

## Revue d’une version

Une version YARA reste immuable après son test. La revue lit son UUID, sa
version, sa source et son SHA-256 ; elle exige une observation `YARA_MATCH`
liée à cette version exacte, dont le rapport conserve le hash du corpus, les
hashes par rôle, les résultats goodware/holdout disponibles, la couverture et
les biais de sélection.

`POST /api/detections/{version_id}/approve` demande un acteur fourni par
`IdentityProvider`, une raison, `expected_source_sha256` et
`known_limitations`. Si le rapport montre des matches goodware, les limites
doivent mentionner explicitement le risque de faux positif ou les hashes
concernés. `POST /api/detections/{version_id}/reject` exige l’acteur, la raison,
le hash source attendu et le rapport exact. Les deux routes exigent
`Idempotency-Key`; sa valeur devient le `correlation_id` durable de la décision.
Un rejeu identique retourne la décision existante, tandis qu’une réutilisation
avec un autre contenu est refusée.

Les décisions `DETECTION_APPROVE` et `DETECTION_REJECT` sont append-only. Elles
portent les références du rapport, le hash source et l’identité/hash de la
publication de base. Elles ne réécrivent pas `DetectionCandidateVersion`. Le
GET d’enquête expose la source YARA à la revue humaine, son statut effectif, les
rapports et comparaisons, ainsi que les faux positifs goodware connus.

## Archive liée au manifeste

`EditionRuleArchiveService` part des pins PUBLICATION exacts de l’unique
manifeste visé. Pour chaque entrée incluse, il retient les versions dont la
dernière décision au moment du manifeste est une approbation humaine. Le hash
source est revérifié, le rapport cité est relu et son SHA-256 contrôlé. Le lien
`previous_publication_artifact_id` de H2 est suivi pour accepter une enquête
basée sur une V6 issue de la publication épinglée. Une version d’article
exclue, une règle non décidée, rejetée ou approuvée après la création du
manifeste n’entre pas dans cette archive.

Chaque règle analyste est écrite sous `analyst/yara/` avec son UUID/version,
son hash source, l’article et le pin exacts, l’enquête, l’approbateur, la date,
les limites connues et la référence/hash du rapport. Les chemins, métadonnées
JSON, horodatages ZIP et ordre des règles sont déterministes. Le SHA-256 de
l’archive est disponible sur `EditionRuleArchive.sha256`.

Quand aucune règle analyste n’est applicable au manifeste, aucune entrée, clé ou
ligne supplémentaire n’est ajoutée au ZIP : l’archive conserve exactement sa
sérialisation historique. Les approbations postérieures n’altèrent ni le
manifeste, ni la release, ni le PDF existants ; une nouvelle composition
versionnée doit retenir l’article concerné avant qu’une nouvelle publication
puisse faire entrer sa règle dans l’archive.
