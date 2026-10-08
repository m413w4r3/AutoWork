# Targeted editorial revision — conception E1

## Contrat

Une révision est une commande typée et immuable : `target`, `operation`,
`instruction` (texte normalisé, 1–2000 caractères), `mode`, identité de
l’acteur et fences de l’artifact de base. Le contrat commun déclare les
opérations de synthèse et d’enrichissement, ainsi que
`existing_evidence_only` et `allow_source_extension`. E1 n’autorise que les
opérations sur `synthesis.title`, `synthesis.lead`, une section ou un paragraphe :
`rewrite_title`, `rewrite_lead`, `rewrite_paragraph`, `insert_paragraph`,
`delete_paragraph`, `rewrite_section`, `insert_section`, `delete_section`,
`merge_sections`, `split_section` et `rename_section`.
L’extension de sources et les opérations table/diagramme/figure sont refusées
avec `source_extension_not_available_yet` et restent à E2.

Les IDs de paragraphes et sections sont calculés sur leur JSON canonique avant
la demande. Ils survivent aux déplacements, pas à une réécriture ; une cible
ambiguë (contenu dupliqué) est refusée. L’index de lead sert seulement à
présenter l’emplacement. `before_hash` reste la preuve d’identité de la cible.

Le modèle reçoit la synthèse courante rendue avec ces IDs, la cible, la
consigne, le mode, le seul EvidencePack admis à la création de la synthèse,
`SYNTHESIS_EDITORIAL_CONTRACT_V5` et les règles de conservation. Sa réponse est
un patch wire limité à `TARGET`, `ACTION`, `BASE_ARTIFACT_HASH`, `BEFORE_HASH`,
`AFTER` (claims et handles) et `RATIONALE`, ou un marqueur de refus typé.
Le parseur dédié vérifie ces enveloppes puis réutilise le parseur des claims,
`validate_synthesis_proposal`, les validateurs de titre, de headings et de
lineage. Aucun payload de synthèse complète n’est accepté comme patch.

## Écriture, lineage et reprise

L’identité de requête est le SHA-256 du JSON canonique des fences, de la cible,
opération, consigne, mode et versions ; son UUIDv5 rend les retries
idempotents. Une révision crée un nouvel artifact/version SYNTHESIS. Elle ne
modifie aucun blob antérieur. `metadata.revision` conserve l’identité, hashes,
cible, opération, instruction, mode, acteur, ModelRun, versions de prompt,
contrat, parseur et validateur, handles/références admis, additions de sources
(vide en E1), date et artifact résultat. La provenance de chaque paragraphe
indique sa révision d’origine ; les paragraphes conservés gardent leur origine.

Avant l’appel, le service retrouve une éventuelle réponse avec le resolver
d’exécution existant et vérifie le ModelRun et ses octets archivés. Une
soumission potentielle est récupérée puis validée/adoptée, jamais soumise une
seconde fois. Le stockage compare atomiquement artifact courant, version,
input hash et SHA-256 canonique ; toute divergence produit un conflit explicite.
Le template est une identité texte libre dans le schéma actuel : aucune
migration n’est requise.

## Application, impact et invalidation

Le service applique le patch au JSON canonique en remplaçant seulement la
cible. Il compare la sérialisation canonique et la structure de toutes les
zones hors cible avant/après ; toute différence invalide le patch. Les IOC,
timeline, source notes, incertitudes, attribution et métadonnées de preuve sont
préservés hors cible. Les claims nouveaux doivent citer des handles du pack ;
aucune source ni relation nouvelle n’est créée.

Le preview est une lecture sans modèle ni écriture : artifact/hash de base,
zone, stages conservés/invalidés, extension de sources et nombre d’appels
attendus. E1 n’invalide jamais REFERENCES, EXTRACTION ou RELEVANCE_PROJECTION.
La synthèse remplacée rend PUBLICATION stale puis la reconstruit par le chemin
de rendu existant, sans modèle. Les enrichissements sont rebasés sans modèle
seulement si leurs hashes d’evidence et d’ancres restent valides ; sinon le
bloc dépendant devient stale. Les suppressions/réécritures de paragraphes
invalident les ancres correspondantes. Le titre seul ne modifie pas les
ancres textuelles.

`revision_not_applicable` porte `recommended_restart_stage` et `reason` pour
les changements de scope, la réparation d’extraction, le changement d’acteur
sans preuve, la source critique absente, le contournement QA, les faits sans
preuve ou la refonte complète. Les refus du modèle passent par le même type.

## Découpage

- **E1** : contrat de domaine générique, patch de synthèse, lineage, fences,
  preview/apply/lecture, invalidation et tests ; aucun écran.
- **E2** : extension/archivage de sources et adaptateur d’enrichissement vers
  ce lineage et ce contrat commun ; pas de duplication de son validateur.
- **F** : UI et choix de cible/preview/application.

Le service d’enrichissement reste l’implémentation de référence pour le
ModelRun et ses garanties de reprise. E1 extrait seulement les conventions
partagées nécessaires au contrat et au stockage synthèse ; il ne déplace pas
son orchestration ni ne change son wire format.
