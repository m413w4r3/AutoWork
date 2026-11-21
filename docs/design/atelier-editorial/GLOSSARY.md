# Atelier éditorial

Vocabulaire des modifications apportées à un document de production à partir de ses occurrences, fils de discussion, propositions et versions canoniques.

## Identité et instantané

**Occurrence éditoriale** (`EditorialBlock`) : occurrence structurelle adressable dans un run de production, même si son contenu est identique à celui d’un autre bloc.
_À éviter_ : empreinte de contenu, cible legacy, paragraphe non qualifié.

**Instantané de l’atelier** (`EditorialWorkspaceSnapshot`) : projection d’un ensemble exact d’épingles de sujet, de run et d’artefacts ; son registre relie les occurrences et leurs lignées.
_À éviter_ : nouvelle version canonique, brouillon, statut de publication.

**Instantané structuré** : projection du document dont les blocs peuvent exposer du texte, un tableau, un graphique ou une figure dans l’ordre du document.
_À éviter_ : liste de texte uniquement, ordre d’affichage reconstruit côté client.

**Identifiant de figure du catalogue** (`catalog_figure_id`) : identifiant opaque limité à la consultation autorisée d’une figure et de sa miniature.
_À éviter_ : clé MinIO, URL ou identifiant canonique de blob.

**Épingle / garde de version** : l’épingle désigne les versions d’artefacts retenues dans un instantané ; la garde vérifie que ces versions et le contenu ciblé n’ont pas changé avant l’écriture.
_À éviter_ : identité de bloc, verrou générique, simple numéro de version.

## Modifications et propositions

**Patch typé** : opération éditoriale fermée dont la forme détermine le changement demandé. Les origines manuelles comprennent `ManualTextPatch`, `ManualTablePatch`, `ManualBarChartPatch` et `ManualSourceFigurePatch`.
_À éviter_ : texte libre de modèle, HTML, SVG ou Typst fourni comme patch.

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
| brouillon pour une modification déjà persistée | brouillon local/serveur ou version canonique |
| PDF, publication et composition comme synonymes | aperçu PDF, artefact publié ou composition épinglée |
