# Découverte ponctuelle des sujets

La découverte d'une édition est lancée à la demande par `POST
/api/editions/{edition_id}/discovery`. Le pays, la période, les langues, le TLP et le profil de
sources viennent de l'édition canonique. La requête peut ajouter des alias, mots-clés,
exclusions et un `complementary_axis`.

Le job canonique `discover_edition` effectue deux passes : recherche web sourcée, puis
structuration locale stricte en `ResearchBatch`. Les deux `ModelRun`, leurs sorties en blob,
les requêtes et citations sont conservés. Une réponse de modèle ne devient ni une archive de
source, ni une preuve, ni une décision éditoriale.

Avec `chatgpt-bridge`, le batch utilise `source_mode=visible_citations_only`. Il conserve un
snapshot obtenu depuis `/bridge/capabilities` (ou un snapshot de repli marqué indisponible),
le nombre de citations visibles,
`source_coverage_complete=false` et la raison de cette couverture incomplète. Les rôles
`primary`, `independent` et `relay` restent donc provisoires, avec
`relationship_status=provisional` et `verification_status=unverified`. Une absence dans les
citations visibles ne prouve jamais qu'une source n'existe pas.

## Idempotence et déduplication

Le hash de requête couvre tous les paramètres normalisés. Une relance identique réutilise le
job et le batch existants. Un axe complémentaire crée un batch distinct, mais fusionne ses
sources dans un candidat existant lorsque l'empreinte de titre ou une URL canonique concorde.
Dans chaque candidat, la première déduplication utilise l'URL HTTP(S) canonique puis
l'empreinte normalisée du titre.

Le prompt demande de citer chaque source effectivement utilisée et de fournir son URL
HTTP(S) lorsqu'elle est visible. Le schéma interdit de fabriquer une URL manquante. Le
collecteur HTTP, l'archivage et la qualification définitive des relations de sources restent
hors de cet incrément.

Les URLs autres que HTTP(S) sont rejetées par le schéma. Cette validation ne télécharge rien :
l'acquisition, l'archivage et la vérification de disponibilité appartiennent à l'incrément
suivant.

## Vérification humaine

Chaque `SourceCandidate` commence avec `verification_status=unverified`. L'analyste peut le
passer à `verify_later`, `invalid` ou `unavailable` via `PATCH
/api/editions/{edition_id}/discovery/sources/{source_id}`. Aucun `CandidateTopic` n'est
sélectionné automatiquement : `editorial_status` reste toujours `proposed` dans cet
incrément. Chaque changement conserve aussi l'acteur et l'horodatage dans l'état canonique du
batch.

`GET /api/editions/{edition_id}/discovery/candidates` expose les candidats, sources, requêtes
et citations, avec filtres sur le texte, le potentiel technique et l'état des sources, et tri
par potentiel, date, nouveauté ou titre.
