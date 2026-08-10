# Regroupement et sélection éditoriale

## Pipeline en deux passes

`EditorialGroupingService` transforme les `CandidateTopic` persistés en groupes éditoriaux.
La première passe est déterministe et explicable : URL canonique, URL de document déjà
archivé, domaine, proximité de date, titre normalisé, entités CTI déjà déclarées et IOC
connus. Elle compare le batch courant, les groupes de l'édition — y compris déjà sélectionnés
— et les groupes sélectionnés des éditions antérieures du même pays.

Une correspondance avec un groupe proposé du batch peut enrichir ce groupe. Une
correspondance avec un groupe déjà sélectionné n'est jamais absorbée automatiquement : elle
reste visible comme doublon ou ambiguïté. Une correspondance historique forte et non
identique est présentée comme `update_previous_subject`, avec un lien vers le groupe
antérieur.

La seconde passe s'exécute dans le job de découverte et appelle uniquement le port
`StructuredExtractionModel`, seulement pour
les scores déterministes ambigus. Son schéma fermé peut proposer merge, séparation, mise à
jour ou reprise non indépendante. Sa justification reste une confiance de regroupement : ce
n'est ni un fait probant, ni un niveau d'attribution. Un résultat ambigu ou indisponible reste
présenté à l'analyste. La lecture du board reste déterministe et ne déclenche donc aucun appel
modèle lent dans la requête HTTP.

## Couverture des sources du bridge

Les groupes issus de `visible_citations_only` commencent avec :

- `source_relationship_status=provisional` ;
- `needs_source_verification=true` ;
- `needs_source_expansion=true` ;
- une confiance et une justification limitées à l'identité éditoriale.

Le board affiche explicitement que seules les citations visibles de ChatGPT sont disponibles.
Il ne présente aucun groupe comme exhaustif et n'interprète jamais une source absente comme
inexistante. Le collecteur du prochain incrément vérifiera URLs, archives et relations
primaire/relais/indépendante.

## Score et décisions humaines

Le score contient six dimensions de 0 à 4 : impact, nouveauté, profondeur technique,
potentiel de chasse, actionnabilité et qualité des sources. Chaque dimension possède une
justification. Ce score ordonne l'information ; aucun seuil ne sélectionne un groupe.

Les endpoints sous `/api/editions/{edition_id}/editorial-groups` exposent le board et les
actions `merge`, `split`, `reject` et `select`. Chaque action reçoit l'identité locale
`dev-analyst` et le `correlation_id`. Les décisions sont ajoutées à `human_decisions`, protégée
contre `UPDATE` et `DELETE` par PostgreSQL. Une fusion peut être corrigée par une nouvelle
décision de séparation ; l'historique précédent n'est pas réécrit.

La sélection exige explicitement `brief` ou `major`, crée un `Subject`, matérialise son
workspace logique avec un manifeste `canonical=false`, puis marque le groupe `selected`.
Aucune collecte n'est lancée par cette action.
