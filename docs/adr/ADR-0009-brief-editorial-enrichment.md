# ADR-0009 — Enrichissement éditorial optionnel des brèves

Statut : accepté — 2026-11-09 (amende ADR-0008, point 4)

## Contexte

M19 B2/B3 permet aux brèves d'utiliser les blocs éditoriaux et les annotations
sémantiques déjà produits par le stage `EDITORIAL_ENRICHMENT`. Le routage M15
avait retiré ce stage des brèves, et l'assemblage refusait tout artefact associé.
La correction M0 de la spécification M19 ne s'applique donc plus à l'état de
ce lot.

## Décision

1. Les routes ARTICLE et BRIEF traversent le stage
   `SYNTHESIS → EDITORIAL_ENRICHMENT → ASSEMBLY`. Le stage existe déjà dans les
   enums et contraintes de stockage; aucune migration n'est requise.
2. Pour BRIEF, l'artefact d'enrichissement est facultatif à l'assemblage. Son
   absence — notamment sur une publication historique — équivaut à un contenu
   enrichi vide et conserve la projection M4a. Un artefact présent est validé
   contre les entrées canoniques; seuls les blocs et annotations effectivement
   projetés sont consommés.
3. La brève ne lance aucune recherche Web ni recherche de ressources externes.
   Les figures doivent provenir de l'inventaire archivé, avec provenance et
   preuve; au plus trois figures peuvent être proposées. Tables, diagrammes et
   graphiques ne sont projetés que si leurs preuves passent la validation.
   Zéro enrichissement reste valide. Les images non archivées restent des
   diagnostics du stage.
4. L'identité d'assemblage BRIEF inclut le hash canonique, l'input hash du stage
   et l'ID d'artefact d'enrichissement seulement quand celui-ci contribue à la
   publication. L'absence ou un artefact vide n'introduit pas ces champs.
   `BRIEF_ASSEMBLY_POLICY_VERSION` passe à
   `brief-assembly-v3-m19-enrichment`; les ARTICLES gardent leur identité.
5. Le schéma PublicationDocumentV8 et les rendus Typst existants couvrent déjà
   tables, diagrammes, graphiques, figures, placement, preuves, ancres
   sémantiques et références de médias. Aucun changement de manifeste Typst
   n'est nécessaire tant que leurs templates ne changent pas.

## Conséquences

- Reprise, retry, invalidation et progression dérivent de la route commune.
- Les publications brèves historiques V7/V8 restent lisibles; une
  réassemblée sans enrichissement conserve son contenu M4a, avec une nouvelle
  identité de policy d'assemblage.
- Les annotations suivent les ancres exactes validées et ne s'appliquent qu'au
  texte présent dans la projection rendue.
- Les versions de génération et de policy de l'enrichissement sont bumpées
  pour invalider les checkpoints générés sous l'ancien prompt.
