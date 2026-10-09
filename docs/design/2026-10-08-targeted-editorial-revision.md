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

Les artifacts d’extraction historiques antérieurs au champ facultatif `scope`
restent vérifiables par leur hash canonique exact (sérialiseur courant ou
ancienne forme sans ce champ). Le blob antérieur et son hash enregistré ne sont
jamais normalisés ou réécrits ; les nouveaux artifacts utilisent le hash
courant.

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

## Criticité des échecs d'extraction

Une omission `source_extraction_failed` critique bloque les révisions ciblées
de SYNTHESIS et d'EDITORIAL_ENRICHMENT tant qu'elle n'a pas une décision
effective `continue_without_source`. Le waiver suit la même règle que les
réparations: raison analyste non vide, identité exacte de l'échec, artifact
EXTRACTION observé et génération du pipeline courants. Une décision plus
récente pour la même clé remplace l'ancienne; une décision périmée ou sans
raison ne lève pas le blocage. Cette règle s'applique aussi au mode d'extension
de sources, après promotion des artifacts.

Chaque waiver utilisé est copié dans `metadata.revision.critical_source_waivers`
et dans la réponse/lineage de la révision avec la clé de réparation, l'ID de la
décision, l'acteur, la raison, l'URL, le code d'échec et les fences observées.
Le changement de garde est versionné par le contrat et le validateur de
révision ciblée.

## Ancrage v3 des claims de révision

Un terme distinctif d’un claim est ancré s’il apparaît dans un handle cité ou
dans toute autre preuve d’extraction du même document source qu’un handle cité,
même si cette preuve n’a pas son propre handle dans le pack de prompt. Les
preuves d’un document différent ne sont pas ajoutées au support du claim. Le contexte
du sujet — titre, acteur ou campagne du snapshot, puis titre et paragraphes de
la synthèse de base — peut établir les noms d’entités déjà connus, jamais une
valeur technique. Les labels de source du pack, les URL de toutes les sources extraites et
les éditeurs du snapshot peuvent établir le nom de l’émetteur, sans apporter
de fait technique.

Les nombres, IOC, domaines, URL, chemins, commandes, options, fonctions API,
versions, mutex et clés restent ancrés dans les preuves des documents cités.
Les mots courants, y compris les débuts de phrase et les formes françaises
usuelles à trait d’union (`chaque`, `celles-ci`, `peut-être`), ne sont pas des
termes distinctifs. Les noms propres au milieu d’une phrase, y compris les
noms avec acronymes après un tiret (`Group-IB`), restent contrôlés.
Les handles inconnus et les scopes `tool_context` / `actor_activity` gardent
leurs validations dédiées.

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

## E2b — révision ciblée des enrichissements

Le même endpoint preview/apply/lineage route selon `target.kind` : les cibles
`synthesis.*` restent traitées par le service E1 ; une cible
`editorial_enrichment.<table|diagram|chart|source_figure>[key]` est déléguée au
service d’enrichissement existant. Le contrat ajoute un discriminant
`enrichment_element`, la famille d’élément, une clé facultative (absente à la
création) et une position d’insertion ancrée sur le lead, une section stable de
la synthèse ou la timeline. Les opérations de source figure exposées sont
`replace_source_figure` et `delete_source_figure`; les anciennes révisions
`CHOOSE_ANOTHER_FIGURE` restent lisibles.

Le service conserve son orchestration ModelRun, son parseur wire et ses
validateurs. Un create lui fournit aussi l’enrichissement courant, la synthèse
E1 avec IDs stables, l’EvidencePack admis, le placement demandé et les
instructions, règles analytiques et le contrat de sortie du prompt
`EDITORIAL_ENRICHMENT` courant. Après validation du seul élément demandé, sa clé est
`<family>_<sha256(request_identity)>` (avec suffixe déterministe en cas de
collision). Les relations doivent être démontrées par les preuves et les
handles doivent appartenir au pack. `NO USEFUL ENRICHMENT` devient un refus
typé sans artifact; un pipeline non établi ou un élément trivial est refusé.
Les rewrites qui deviennent triviaux sont refusés avec suppression recommandée.

Les delete sont des opérations locales sans appel modèle. Le payload canonique
résultant est une copie du payload de base avec uniquement l’élément ciblé
retiré; les ancres et le placement des éléments conservés ne changent pas. Les
annotations sémantiques dont l’ancre appartient à l’élément supprimé disparaissent
avec lui, et toutes les autres restent identiques. Pour une figure source, sa
trace de décision est liée à la figure et disparaît avec elle (ou est remplacée
avec sa figure); les traces des autres figures restent identiques. Les fences,
l’idempotence et le CAS restent identiques aux révisions E1.

Les nouveaux artifacts enrichissement écrivent `metadata.revision` au format
commun E1 (identité, base, cible, opération, requête, mode, acteur, ModelRun,
versions de prompt/contrat, références de preuves, additions de sources vides,
date et artifact résultat). `model_run_id` est nul pour un delete pur.
L’historique se parcourt par `base_artifact_id`; les anciennes métadonnées
`element_kind`/`action` gardent leur lecteur legacy et ne sont pas réécrites.
La provenance par élément indique l’artifact initial ou la révision qui l’a
créé/modifié; les éléments inchangés gardent leur provenance.

Une révision ne change que l’artifact `EDITORIAL_ENRICHMENT` et rend
`PUBLICATION` stale avant son rebuild par le chemin de rendu/QA existant, sans
modèle. `SYNTHESIS`, `REFERENCES`, `EXTRACTION`, `RELEVANCE_PROJECTION` et les
autres enrichissements restent inchangés. Le preview expose les fences de
base, la zone visée, les stages conservés/invalidés et 0 appel modèle pour un
delete, 1 pour un create/rewrite (0 si un ModelRun idempotent est déjà
disponible). `allow_source_extension` conserve le refus typé E2a.

## E2c — ancres de rebase et restauration des blocs stale

Les IDs complets de section restent la référence des cibles de révision et
continuent d'inclure le heading. Les placements d'enrichissement utilisent une
ancre distincte calculée sur la section canonique sans son heading : les
paragraphes, leurs preuves et le kind restent dans l'ancre. Un renommage peut
donc conserver un bloc, tandis qu'une réécriture ou une suppression de la
section l'invalide. Le lecteur accepte aussi le hash complet historique.

Un bloc écarté par rebase reste absent de l'artifact courant, avec son
`source_artifact_id`, sa clé et sa raison dans `revision_rebase.stale_blocks`.
Le preview et l'application `restore_enrichment_blocks` parcourent les seuls
ancêtres d'enrichissement vérifiés, rattachent les placements à la synthèse
courante, puis réappliquent la validation des preuves, de la qualité IOC et des
diagrammes avant d'append une nouvelle version. Les suppressions ciblées
`delete_*` de la lignée interdisent la restauration de leur clé. L'application
n'appelle aucun modèle, compare les fences CAS du courant et reconstruit la
publication avec le rendu et QA existants.

Les endpoints sont `POST .../editorial_enrichment/restore/preview` et
`POST .../editorial_enrichment/restore`.

## Enrichissements rebasés après réparations

Une réparation peut conserver l'enrichissement éditorial après avoir validé
que chacune de ses références de preuve existe toujours et reste admissible
selon la projection de pertinence. Elle met alors à jour les hashes
d'extraction, de synthèse et de projection, ainsi que les IDs des artifacts
courants. Elle conserve `evidence_pack_hash` et `input_hash` d'origine : le
chemin de réparation n'a pas chargé la politique d'accès ni reconstruit
l'inventaire d'images requis pour produire une nouvelle identité complète.
Ces anciens hashes décrivent donc l'admission antérieure, et non les entrées
courantes de la révision.

Pour `metadata.lineage_rebased = true` seulement, la fence de révision vérifie
que `metadata.input_artifacts` désigne exactement les artifacts courants
EXTRACTION, RELEVANCE_PROJECTION et SYNTHESIS. Elle compare ensuite les trois
hashes canoniques stockés aux contenus courants, puis exige une égalité exacte
de `access_policy_hash` et du hash fonctionnel de l'inventaire de figures.
Toute divergence reste un conflit typé. Les artifacts sans ce marqueur gardent
la comparaison stricte du EvidencePack et de `input_hash`.

Une fois cette lignée vérifiée, la révision reconstruit le EvidencePack depuis
les artifacts courants et calcule à nouveau `evidence_pack_hash` et
`input_hash`. Le nouvel artifact stocke ces valeurs. Les révisions suivantes
utilisent alors la fence stricte normale, sans exception de rebase.

Cette tolérance ne réutilise pas l'ancien EvidencePack et ne contourne pas la
validation des preuves : le rebase a déjà refusé toute référence conservée
absente ou classée hors périmètre/indéterminée; la révision construit un pack
neuf à partir des artifacts courants et vérifie les handles et références de
toute proposition contre ce pack. Les IDs, hashes, politique d'accès et
inventaire empêchent que des entrées remplacées ou modifiées profitent de
l'exception. L'exception est limitée au marqueur écrit par le chemin de
réparation; aucune nouvelle version de politique n'est nécessaire.

## Filtre de relations des diagrammes

Le filtre résout d'abord chaque handle ordinaire `E…` sur son record narratif
ou technique direct. Si ce record manque, il peut utiliser un record réservé
`R…` qui partage exactement la même référence canonique. Cette résolution de
secours ne remplace jamais un record direct par un autre record qui partage sa
référence; un handle absent du pack reste refusé par le parseur.

Une relation factuelle peut être étayée soit par un record cité qui couvre les
deux extrémités et son libellé, soit par le même handle connu cité sur la
relation et sur les deux nœuds. Dans le second cas, les handles propres à
chaque nœud doivent encore étayer son libellé par correspondance lexicale ou
par la règle existante des preuves en langue étrangère. Cette attestation
partagée couvre les différences de libellé et de langue sans autoriser un
nœud sans preuve. La couverture lexicale accepte les équivalents courants
`défi/challenge`, `piégé/trojanized` et `paquet/package`, ainsi que les alias
explicites séparés par `/` ou placés entre parenthèses. Les extensions
génériques (`.exe`, `.json`, `.js`, etc.) ne suffisent pas seules à établir un
endpoint. Les relations d'infection gardent leur exigence de séquence
documentée; les indications de contre-preuve et les relations de projection
inter-sources restent bloquantes.

La validation de texte brut partage le détecteur de marqueurs de source de la
synthèse, qui interprète aussi `(S3)` comme une citation. Dans un libellé de
nœud diagramme terminé par `Storage (S3)`, cette forme désigne le protocole S3,
pas une référence éditoriale. Le contrôle de texte masque ce seul suffixe
pendant le test de citation; le libellé original reste soumis au filtre de
relations et à la vérification des handles du pack. Les autres marqueurs de
source, les handles inconnus et les endpoints sans preuve restent refusés.
Les refus de validation exposent maintenant `diagnostic_code` et le message
complet dans les détails de la révision, et ajoutent le sous-code aux warnings
`editorial_enrichment_output_invalid`.

Cette règle assouplit seulement le rejet (elle accepte un sur-ensemble de ce que
le validateur acceptait) : aucune sortie précédemment valide ne devient invalide.
Elle n'incrémente donc NI la version de politique du schéma
(`editorial-enrichment-v11-editorial-tables`) NI la version du validateur
(`…-validator-v18-ioc-republication-diagram-value`) : ces deux versions entrent dans
l'empreinte d'entrée des enrichissements stockés, et les changer rendrait tous les
enrichissements existants « non supportés » (politique) ou en conflit de fence
(validateur) pour les révisions ciblées — régressions constatées puis corrigées.
Si un jour le filtre est RESSERRÉ, incrémenter alors la version du validateur et
prévoir la tolérance de fence des artifacts antérieurs.

## Correctif — validation d'un patch sans chapeau

Un patch ciblé de section ou de paragraphe peut omettre `@@LEAD@@`, puisque le
chapeau n'est pas sa cible. Le parseur accepte alors une proposition sans claim
de lead uniquement dans ce mode interne (`require_lead=False`) ; son mode par
défaut conserve l'exigence d'un lead pour les propositions complètes. La
validation continue sur chaque claim de la cible, ses handles exacts, son
scope, ses relations et son ancrage dans l'extraction. Aucun texte de
remplacement ne peut être validé en contournant ces contrôles.

Le chapeau de base n'est plus injecté dans la proposition servant à valider le
patch. L'application garde le payload de base et remplace uniquement la cible,
ce qui préserve les octets canoniques du chapeau et des autres zones hors cible.
Les versions du parseur de révision, du contrat et du validateur sont
incrémentées pour distinguer ce comportement lors des relectures de lineage.

L'audit en lecture seule des neuf sujets réels des fichiers `fin1.json` à
`fin9.json` trouve quatre chapeaux sans paragraphe dont toutes les références
ont un handle admis : `fdde6348-2f3d-4466-b7e2-8be1999c35e7`,
`bb2701a1-777c-405a-b2bd-0f097e648fd3`,
`b17edd9f-743a-4bc9-b919-b65c904a5803` et
`27a56be8-ed2d-4cf4-8542-ea73d744a60e` (GTG-30006). Pour ce dernier, le
chapeau de la synthèse v9 cite un fait direct admis et une incertitude
`context` provenant d'une source `IOC_RULES`. Les incertitudes narratives du
pack de révision sont construites depuis les sources `FULL`, donc cette
seconde référence n'a aucun handle dans le pack, bien que le chapeau reste
inchangé et hors cible.
