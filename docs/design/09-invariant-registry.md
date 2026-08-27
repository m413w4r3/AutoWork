# M3 / P09 — registre d'invariants multi-provenance

Statut : contrat verrouillé avant implémentation. Le lot 09 l'implémente sans le redéfinir.

## Périmètre

P09 construit et persiste un registre d'invariants candidats depuis les sorties déjà persistées des lots 06, 07, 07B, 08 et 08B. Aucun appel modèle, VirusTotal, compilation YARA ou approbation. Ne relance pas les extracteurs M2 et n'ouvre pas les binaires pour recalculer une feature déjà persistée.

## Taxonomies fermées

Types d'invariant, exactement :

- `literal_string`
- `hex_pattern`
- `code_ngram`
- `opcode_sequence`
- `import_name`
- `export_name`
- `section_name`
- `capability`
- `similarity_hash`
- `structural_metadata`
- `relation`

Catégories sémantiques, exactement :

- `c2_indicator`
- `mutex_or_event`
- `pdb_or_build_path`
- `config_marker`
- `crypto_constant`
- `custom_protocol`
- `ransom_or_ui_text`
- `code_sequence`
- `capability_pattern`
- `similarity_key`
- `library_noise`
- `packer_artifact`
- `compiler_artifact`
- `generic_winapi`
- `unknown`

Statuts, exactement : `proposed`, `approved_for_pivot`, `validated`, `rejected`, `unselective`, `shared_component`. Le statut initial est toujours `proposed`. Toute transition de statut porte acteur, date et raison ; P09 n'approuve rien automatiquement.

## Provenances typées

Chaque provenance est validée selon son type et ne peut pas emprunter les champs d'un autre type :

- `sample_feature` : `sample_sha256`, `feature_id`, `offsets` ;
- `code_feature` : `sample_sha256`, adresse de fonction, décalage, version du désassembleur ;
- `tool_output` : `sample_sha256`, outil, version, identifiant interne à l'outil ;
- `capability` : `sample_sha256`, `capability_id`, adresses ;
- `report_claim` : `claim_id`, document source, sans octets ni offsets ;
- `analyst_manual` : acteur, date, motif.

La persistance doit conserver assez d'identifiants pour remonter à la ligne/source M2 exacte. Une provenance inexistante ou incomplète est rejetée avant création de l'invariant.

## Mapping M2 -> taxonomie M3

Ne crée pas une seconde taxonomie publique. Les sorties M2 servent ainsi :

- chaînes persistées -> `literal_string` ;
- imports -> `import_name` ; exports -> `export_name` ; sections -> `section_name` ;
- capa -> `capability` avec `capability_id` et adresses ;
- n-grammes SMDA -> `code_ngram`, avec motif masqué et compteurs déjà calculés ;
- `imphash`, `ssdeep`, `tlsh`, `rich_header_hash`, `vhash`, `main_icon_dhash` -> `similarity_hash`, la sous-espèce est portée dans la valeur/métadonnée canonique sans créer un nouveau type ;
- `opcode_fragment16` peut alimenter `hex_pattern`/`opcode_sequence` seulement si la provenance M2 permet de représenter honnêtement le motif demandé ; ne synthétise pas d'information absente ;
- `structural_metadata` et `relation` ne sont créés que depuis une sortie persistée qui porte réellement cette sémantique ; jamais par inférence opportuniste.

Le registre travaille sur le jeu de samples explicitement fourni pour l'investigation ; il ne découvre pas l'appartenance à l'investigation par scan global.

## Mesures calculées avant modèle

Chaque invariant persistant porte :

- score/verdict de banalité du lot 07 et identifiant de la baseline utilisée ;
- verdict de spécificité du lot 07B et liste déterministe des familles concernées ;
- prévalence dans le corpus bénin ;
- support positif dans le snapshot courant ;
- provenance(s) typée(s).

`UNKNOWN`/non mesurable reste inconnu et n'est jamais transformé en zéro. Les seuils de `BanalityScorer` sont injectés explicitement depuis la configuration M3 ; P09 ne les invente pas.

Pour `code_ngram`, persiste aussi le motif masqué, `byte_count`, `fixed_byte_count`, `masked_byte_count`, `longest_fixed_run` et le drapeau `likely_packed` de l'échantillon d'origine. `likely_packed` est une décision déterministe P09 construite à partir des signaux de packing M2 et de seuils/configuration explicitement fournis ; si ces seuils ne sont pas configurés, arrête au lieu de les inventer.

## Rejets déterministes avant persistance

Chaque rejet est consigné dans une table suivant le motif du `rejected_model_proposals` existant, avec cause requêtable. Appliquer avant persistance d'un invariant :

1. provenance inexistante ou incomplète pour son type ;
2. catégorie hors enum ;
3. catégorie `library_noise`, `packer_artifact`, `compiler_artifact` ou `generic_winapi` ;
4. banalité `banal` ;
5. spécificité `multi_family` ;
6. motif vide ou surdimensionné ;
7. pour `code_ngram`, ratio `masked_byte_count / byte_count` strictement supérieur à `CODE_NGRAM_MAX_MASK_RATIO` ;
8. pour `code_ngram`, `longest_fixed_run < CODE_NGRAM_MIN_CONTIGUOUS`.

Les limites de taille de motif et les deux constantes code-ngram sont des paramètres M3 explicites. Elles ne sont pas déduites du code ni choisies par le modèle.

Le journal expose au minimum des statistiques par cause, notamment banalité goodware, non-spécificité de famille et ratio de masquage, afin d'alimenter le point d'arrêt manuel 2.

## Cas report_claim et saisie manuelle

Une provenance `report_claim` peut entrer dans le registre et être utilisée comme pivot, mais reste interdite dans une règle YARA publiée tant qu'elle n'est pas confirmée par au moins un sample du corpus positif. Persiste un champ de confirmation explicite et teste cette règle.

Expose un chemin applicatif de saisie manuelle par un analyste identifié. Une saisie `analyst_manual` passe par exactement le même scoring, les mêmes rejets et la même persistance que toute autre proposition ; elle ne contourne jamais les filtres.

## Identité, idempotence et persistance

L'identité de replay doit être déterministe à partir de l'investigation, du type, de la représentation canonique du motif et de la provenance canonique pertinente. La contrainte d'idempotence est protégée en PostgreSQL, pas seulement par un pre-check Python.

Créer exactement `backend/migrations/versions/0011_invariant_registry.py` avec `down_revision = "0010_code_features"`. Ne modifie jamais 0001–0010.

La décomposition SQLAlchemy peut suivre l'architecture existante, mais elle doit rendre requêtables : invariants, provenances, transitions de statut et journal de rejets. Si une nouvelle table possède un vrai `blob_id` FK, l'ajouter au comptage de références de `BlobRepository`.

## Tests P09 verrouillés

Couvrir au minimum : toutes les taxonomies fermées ; validation des champs obligatoires de chaque provenance ; statut initial `proposed` ; transition acteur/date/raison ; banalité + baseline ; spécificité + familles ; prévalence bénigne ; support snapshot ; code-ngram et seuils ; chaque cause de rejet ; statistiques par cause ; report_claim non confirmé ; confirmation positive ; saisie analyste scorée ; replay concurrent/idempotent DB ; aucun appel modèle/VT/YARA.
