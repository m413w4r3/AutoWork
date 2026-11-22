# Atelier éditorial

Vocabulaire des modifications apportées à un document de production à partir de ses occurrences, fils de discussion, propositions et versions canoniques.

## Identité et instantané

**Occurrence éditoriale** (`EditorialBlock`) : occurrence structurelle adressable dans un run de production, même si son contenu est identique à celui d’un autre bloc. Son identifiant est scopé à une version d’artefact.
_À éviter_ : empreinte de contenu, cible legacy, paragraphe non qualifié.

**Clé d’élément d’enrichissement** (`element_key`) : identité textuelle stable d’un tableau, graphique, diagramme ou figure à travers les versions d’artefact ; elle survit aux changements de placement et de contenu.
_À éviter_ : `occurrence_id` (scopé à une version), identifiant d’asset compilé, index de position dans une famille.

**Asset d’élément** : identité média immuable d’un élément — asset compilé d’un graphique ou diagramme (`compiled_asset_id`) ou blob de figure résolu ; une nouvelle version peut référencer un nouvel asset sans réécrire l’ancien.
_À éviter_ : `element_key`, URL distante, clé de blob choisie par le client.

**Instantané de l’atelier** (`EditorialWorkspaceSnapshot`) : projection d’un ensemble exact d’épingles de sujet, de run et d’artefacts ; son registre relie les occurrences et leurs lignées.
_À éviter_ : nouvelle version canonique, brouillon, statut de publication.

**Instantané structuré** : projection du document dont les blocs peuvent exposer du texte, un tableau, un graphique, un diagramme ou une figure dans l’ordre canonique.
_À éviter_ : liste de texte uniquement, ordre d’affichage reconstruit côté client.

**Identifiant de figure du catalogue** (`catalog_figure_id`) : identifiant opaque limité à la consultation autorisée d’une figure et de sa miniature.
_À éviter_ : clé MinIO, URL ou identifiant canonique de blob.

**Épingle / garde de version** : l’épingle désigne les versions d’artefacts retenues dans un instantané ; la garde vérifie que ces versions et le contenu ciblé n’ont pas changé avant l’écriture.
_À éviter_ : identité de bloc, verrou générique, simple numéro de version.

## Modifications et propositions

**Patch typé** : opération éditoriale fermée dont la forme détermine le changement demandé. Les insertions manuelles comprennent `ManualTextPatch`, `ManualTablePatch`, `ManualBarChartPatch` et `ManualSourceFigurePatch` ; les révisions d’éléments existants forment l’union fermée `VisualRevisionPatch` (`revise_chart`, `revise_diagram`, `update_visual_caption`, `move_enrichment_element`, `reorder_enrichment_elements`, `replace_source_figure`, `remove_enrichment_element`).
_À éviter_ : texte libre de modèle, HTML, SVG ou Typst fourni comme patch.

**Révision visuelle** (`VisualRevisionPatch`) : modification typée d’un élément d’enrichissement existant qui conserve sa clé ; elle est préparée par un planificateur unique et adoptée par l’écrivain d’adoption commun, quel que soit son origine manuelle ou Bridge.
_À éviter_ : écriture canonique directe, patch libre, seconde validation propre à une variante.

**Ordre canonique des éléments** (`enrichment_element_order`, `enrichment_order_rank`) : ordre unique des éléments d’un même placement, calculé par le domaine et consommé à l’identique par le snapshot de l’atelier, les placements résolus de publication et le rendu Typst.
_À éviter_ : ordre d’affichage reconstruit côté client, ordre déduit du seul champ `order` des figures, ordre propre au PDF.

**Groupe d’ordre** (`group_order`, schéma V11) : entier explicite qui fixe la position d’un élément dans son groupe de placement ; il n’est écrit qu’après une réorganisation ou une insertion/déplacement dans un groupe déjà ordonné, et seuls les groupes `after_paragraph` sont réorganisables.
_À éviter_ : index de liste implicite, ordre de famille utilisé comme ordre éditorial, `group_order` posé sans décision explicite.

**Déplacement d’élément** (`move_enrichment_element`) : changement de la seule ancre de placement d’un élément vers un paragraphe ou un chapeau enregistré ; le contenu, la clé et les preuves restent identiques, et l’ordre explicite de l’élément déplacé est effacé.
_À éviter_ : réorganisation d’un groupe, réécriture de contenu, déplacement avec repli en fin de section.

**Remplacement de figure** (`replace_source_figure`) : substitution en place d’une figure par une autre figure du catalogue autorisé, avec nouvelle résolution des faits d’archive à la préparation et à l’adoption ; même clé, placement et ordre, une seule version, l’ancienne version conservant l’image précédente.
_À éviter_ : retrait puis création comme deux écritures, réutilisation d’un fait d’archive fourni par le client, nouvelle clé d’élément.

**Retrait d’élément** (`remove_enrichment_element` → `delete_*`) : suppression d’un élément de sa famille uniquement ; les autres positions et ordres sont conservés, les archives, l’inventaire et les preuves sources restent intacts, et la trace de décision de la figure retirée n’est plus portée par le nouvel artefact.
_À éviter_ : suppression d’archive ou de blob, renumérotation des autres éléments, écriture directe hors adoption.

**Graphique à barres** (`ChartKind.BAR`, `ChartSpecV1`) : graphique quantitatif portant des valeurs numériques finies ; pour chaque point, une même preuve admissible justifie la valeur et son unité.
_À éviter_ : valeur estimée, valeur sans unité ou point sans preuve.

**Figure archivée** : figure de source conservée dans l’inventaire autorisé avec sa provenance et son blob immuable.
_À éviter_ : image distante, image choisie par URL, fichier envoyé par le navigateur.

**Sélection de figures** : choix ordonné d’une ou plusieurs figures archivées, avec légendes et ancre de paragraphe, soumis comme une opération manuelle typée.
_À éviter_ : appel Bridge, envoi d’octets ou de clé de blob par le client.

**Brouillon éditorial** : texte manuel non canonique, conservé localement par occurrence ou sur le serveur avec contrôle d’accès et rétention ; le brouillon serveur expire par défaut 30 jours après sa dernière modification.
_À éviter_ : proposition persistée, version canonique, publication.

**Proposition éditoriale** (`EditorialProposal`) : changement typé validé et persisté contre une cible et une version de base précises, avec ses preuves et son historique de transitions.
_À éviter_ : message de discussion, réponse Bridge non validée, proposition d’invariant P10.

**Adoption** : écriture humaine qui promeut une proposition acceptée vers une nouvelle version canonique après revalidation des preuves, de la politique et des gardes de version.
_À éviter_ : clic d’acceptation seul, publication externe, repin automatique.

**Fils éditorial** (`EditorialThread`) : discussion rattachée à une occurrence, dont l’origine est conservée même lorsque la cible courante avance par une lignée prouvée.
_À éviter_ : `ModelConversation`, preuve, nouvelle discussion à chaque version.

**Historique éditorial** (`EditorialHistory`) : liste paginée des propositions et décisions, avec leur origine affichée comme manuelle ou Bridge.
_À éviter_ : transcription des conversations, preuve canonique, historique de versions reconstruit depuis l’interface.

## Publication et lecture

**Statut du PDF** : état de la prévisualisation courante, parmi `READY`, `STALE`, `GENERATING` et `FAILED`.
_À éviter_ : état de publication externe ou équivalence entre « prêt » et « épinglé dans une composition ».

**Composition épinglée** : composition d’édition qui référence explicitement les versions d’artefacts choisies pour chaque sujet.
_À éviter_ : composition qui suit automatiquement le dernier PDF.

**Composition périmée** : composition dont une ou plusieurs épingles ne correspondent plus aux versions courantes ; une mise à jour explicite est nécessaire pour les remplacer.
_À éviter_ : repin automatique, publication de remplacement implicite.

**Lecture historique seule** : consultation d’une ancienne publication sans capacité d’écriture, migration implicite ni réécriture de son blob canonique.
_À éviter_ : édition désactivée seulement côté présentation, conversion silencieuse d’un ancien schéma.

**Version canonique** : version append-only d’un artefact de production, conservée avec ses octets, son SHA-256 et sa lignée.
_À éviter_ : brouillon, aperçu, assemblage ou publication externe.

**Type de publication** : classification explicite `ARTICLE` ou `BRIEF`, résolue côté serveur et projetée dans l’instantané.
_À éviter_ : statut de publication, format déduit sans preuve.

## Termes à distinguer

| À éviter | Employer |
|---|---|
| « proposition » pour du texte de discussion | « message », « demande de proposition » ou `EditorialProposal` selon l’état réel |
| bloc, occurrence et cible comme synonymes | occurrence éditoriale, cible legacy ou empreinte de contenu selon le cas |
| occurrence, clé d’élément et asset comme synonymes | occurrence éditoriale, clé d’élément d’enrichissement ou asset d’élément selon l’identité visée |
| brouillon pour une modification déjà persistée | brouillon local/serveur ou version canonique |
| PDF, publication et composition comme synonymes | aperçu PDF, artefact publié ou composition épinglée |
| retrait et remplacement comme synonymes | retrait (`delete_*`) ou remplacement en place (`replace_source_figure`) |
| déplacement et réorganisation comme synonymes | déplacement (change l’ancre) ou réorganisation (change l’ordre d’un groupe `after_paragraph`) |
| ordre d’affichage et ordre canonique comme synonymes | ordre canonique des éléments, piloté par le domaine et partagé avec le PDF |
