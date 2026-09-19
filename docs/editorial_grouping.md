# Regroupement et sélection éditoriale

## Pipeline en deux passes

`EditorialGroup` est une projection de compatibilité destinée à la lecture de Selection. Elle
reflète les relations explicables entre `DiscoveryCandidate` et l'état de sélection ; elle ne
remplace jamais l'identité fonctionnelle `DiscoveryCandidate.id`. `EditorialGroupingService`
calcule cette projection à partir de signaux déterministes : URL canonique, URL de document déjà
archivé, domaine, proximité de date, titre normalisé, entités CTI déjà déclarées et IOC connus.
Il compare le batch courant, les groupes de l'édition — y compris déjà sélectionnés — et les
groupes sélectionnés des éditions antérieures du même pays.

Ce service n'est pas l'autorité de Fusion et n'expose pas les décisions métier `merge` ou `split`.
Ces opérations appartiennent à la capacité Fusion, qui travaille sur des UUID métier et un
`snapshot_version`. Selection consomme la projection pour décider de retenir, rejeter ou composer
un sujet ; elle ne réécrit pas les candidats.

Une correspondance déterministe forte (hard identity evidence : URL anchor + corroborator,
ou identifiant explicite de campagne/incident) enrichit la projection — qu'elle soit PROPOSED ou
SELECTED. Une projection SELECTED conserve son `subject_id` lors de cet enrichissement,
et ses `needs_source_expansion`/`needs_source_verification` sont marqués pour déclencher
la collecte des nouvelles URL.

Une correspondance ambiguë (weak signals, score 0.45–0.85) est présentée en tant que
`AMBIGUOUS_REVIEW` : aucun auto-merge structurel, même si le LLM recommande une fusion
(voir ci-dessous, "Deuxième passe"). Une correspondance historique forte et non
identique est présentée comme `update_previous_subject`, avec un lien vers le groupe
antérieur.

La seconde passe s'exécute dans le job de découverte et appelle uniquement le port
`StructuredExtractionModel`, seulement pour les scores déterministes ambigus (0.45–0.85).
Son schéma fermé peut proposer merge, séparation, mise à jour ou reprise non indépendante, mais
la suggestion modèle reste séparée des signaux déterministes et n'a aucune autorité structurelle.

Même si le LLM recommande "merge", l'outcome reste `AMBIGUOUS_REVIEW` avec la suggestion du
modèle flaggée pour révision humaine. Seule une décision humaine exécutée par Fusion peut causer
une fusion structurelle ; les correspondances déterministes fortes (hard identity evidence)
restent des signaux de compatibilité et ne réidentifient pas les candidats.

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

Les endpoints sous `/api/editions/{edition_id}/editorial-groups` exposent le board de Selection
et les actions `reject` et `select`. Chaque action reçoit l'identité locale `dev-analyst` et le
`correlation_id`. Les décisions sont ajoutées à `human_decisions`, protégée contre `UPDATE` et
`DELETE` par PostgreSQL. Les décisions `merge` et `split` sont exposées par Fusion ; elles peuvent
être corrigées par une nouvelle décision, sans réécrire l'historique précédent.

La sélection exige explicitement `brief` ou `major`, crée un `Subject`, matérialise son
workspace logique avec un manifeste `canonical=false`, puis marque le groupe `selected`.
Aucune collecte n'est lancée par cette action.
