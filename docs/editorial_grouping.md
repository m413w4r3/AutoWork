# Regroupement et sélection éditoriale

## Pipeline en deux passes

`EditorialGroupingService` transforme les `CandidateTopic` persistés en groupes éditoriaux.
La première passe est déterministe et explicable : URL canonique, URL de document déjà
archivé, domaine, proximité de date, titre normalisé, entités CTI déjà déclarées et IOC
connus. Elle compare le batch courant, les groupes de l'édition — y compris déjà sélectionnés
— et les groupes sélectionnés des éditions antérieures du même pays.

Une correspondance déterministe forte (hard identity evidence : URL anchor + corroborator,
ou identifiant explicite de campagne/incident) enrichit un groupe — qu'il soit PROPOSED ou
SELECTED. Un groupe SELECTED conserve son `subject_id` lors de cet enrichissement,
et ses `needs_source_expansion`/`needs_source_verification` sont marqués pour déclencher
la collecte des nouvelles URL.

Une correspondance ambiguë (weak signals, score 0.45–0.85) est présentée en tant que
`AMBIGUOUS_REVIEW` : aucun auto-merge structurel, même si le LLM recommande une fusion
(voir ci-dessous, "Deuxième passe"). Une correspondance historique forte et non
identique est présentée comme `update_previous_subject`, avec un lien vers le groupe
antérieur.

La seconde passe s'exécute dans le job de découverte et appelle uniquement le port
`StructuredExtractionModel`, seulement pour les scores déterministes ambigus (0.45–0.85).
Son schéma fermé peut proposer merge, séparation, mise à jour ou reprise non indépendante.

**Patch 1 (Consolidation d'identité)**: Le LLM n'a plus d'autorité de fusion structurelle.
Même si le LLM recommande "merge", l'outcome reste `AMBIGUOUS_REVIEW` avec la suggestion du
modèle flaggée pour révision humaine. Seule une action `HumanDecisionType.MERGE` appliquée
par l'analyste peut causer une fusion structurelle; les correspondances déterministes fortes
(hard identity evidence) s'enrichissent automatiquement sans intervention humaine.

La justification du LLM reste une confiance de regroupement : ce n'est ni un fait probant,
ni un niveau d'attribution. Un résultat ambigu ou indisponible reste présenté à l'analyste.
La lecture du board reste déterministe et ne déclenche donc aucun appel modèle lent dans la
requête HTTP.

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
