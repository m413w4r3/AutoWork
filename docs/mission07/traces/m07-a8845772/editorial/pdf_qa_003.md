# QA PDF — avis v9, re-rendu 2

**Verdict : PASS_WITH_NOTES** — les défauts bloquants de pagination du rapport précédent sont corrigés et aucun hash complet, identifiant de référence ni titre de section n’a disparu. Quelques défauts de composition restent à traiter avant une validation éditoriale définitive.

## Identité et méthode

- PDF examiné : `.autowork-runs/m07/editorial/opinion-v9-render2.pdf`, 21 pages, SHA-256 `4efeee8b5a355985d326546c5e56a5fa94da6c64df51975ba6077f547acad9e9`.
- Comparaison : brouillon précédent `opinion-v9-draft.pdf`, 22 pages, SHA-256 `ba8157515326e6f76e864b782ee8413f6450c4bb534f1a6ade4abb6654888d33`.
- Référence PDF : `docs/references/CN2609_typ/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf`, SHA-256 `1ea738682bf922449e28c30c885b9712af0b24a0314c3481bd41554ed67ce64e`; comparaison visuelle avec sa page paysage 119 et les templates de `CN2609_typ` et `chpTypst`.
- Les 21 pages ont été rendues et examinées à 110 dpi dans `/tmp`. Contrôles complémentaires : `pdfinfo`, extraction `pdftotext -layout` et comparaison des identifiants/hashes.
- L’avertissement `pdfinfo` « Suspects object is wrong type (boolean) » apparaît aussi sur le PDF de référence; il n’est pas spécifique à ce rendu.

## Défauts du rapport `pdf_qa_002.md`

1. **Ligne du tableau masquée par le pied p.18 — CORRIGÉ (p.15–16).** Les lignes du tableau réseau tiennent au-dessus du pied. Aucun chevauchement ni texte placé sous le bandeau rouge n’est visible.
2. **Page blanche p.16 — CORRIGÉ (p.16).** La page contient désormais la suite du tableau réseau et l’inventaire des empreintes.
3. **Titre d’annexe seul p.17 — CORRIGÉ (p.15).** Le titre d’annexe est suivi du sous-titre et des premières lignes du tableau réseau sur la même page.
4. **Note isolée p.11 — CORRIGÉ (p.10).** La note sur `FlightManager.csproj` est réunie avec le tableau d’inventaire.
5. **Backticks Markdown littéraux — PARTIEL (p.10).** Ils ont disparu des paragraphes d’analyse et les termes techniques y sont rendus en monospace. Deux backticks restent imprimés autour de `FlightManager.csproj` dans la note d’inventaire.
6. **« Confiance : 90 % » et autres pourcentages — CORRIGÉ (p.9, 11–13).** Aucun libellé de confiance numérique n’apparaît dans le nouveau texte extrait. La formulation qualitative « confiance élevée » reste sans pourcentage.
7. **Lisibilité des tableaux d’annexe — PARTIEL (p.15–20).** La nouvelle composition est plus lisible et les lignes sont visibles en entier; les cellules grises de contexte/provenance restent denses, autour de 7,5–8,5 pt pour certains passages, contre 9 pt dans le tableau de référence p.119.
8. **Provenance de publication finale — CORRIGÉE (p.1).** L’identité complète et le SHA-256 de la publication de base sont réunis dans la carte de provenance d’ouverture; la page finale dédiée a disparu.
9. **Hash de provenance coupé sur deux lignes — PARTIEL (p.1).** Le SHA-256 est complet et lisible dans la carte, mais sa chaîne est encore coupée au retour de ligne.

## Nouveaux constats et intégrité du contenu

- **Priorité moyenne — nouvelle page finale clairsemée (p.21).** Elle ne contient que les identifiants `E7` à `E12` en haut de page; le reste est vide. Les identifiants `F1`–`F8` et `E1`–`E6` sont sur p.20. Aucun identifiant n’a été perdu, mais la liste de traçabilité est séparée entre deux pages et crée une fin de document peu équilibrée.
- Les neuf SHA-256 complets présents dans l’ancien PDF sont retrouvés dans le nouveau, y compris celui de la provenance de publication. Les 22 UUID de référence et d’évidence sont également présents. Les sections `RÉFÉRENCES`, `CHRONOLOGIE`, `SYNTHÈSE`, `ANNEXE TECHNIQUE`, `ANNEXE DE TRAÇABILITÉ` et `PROVENANCE DE PUBLICATION` sont toutes retrouvées.
- Les différences d’extraction observées concernent les pourcentages de confiance désormais supprimés, les délimiteurs Markdown des spans de code et la réorganisation des colonnes de tableaux; aucun hash complet, UUID, marqueur de référence ou titre de section n’a été perdu. Les lignes de tableaux repliées différemment restent présentes sous la nouvelle structure.
- Logos, en-têtes et pieds de page restent visibles; aucune autre page entièrement blanche ni ligne de tableau sous un pied n’a été observée.

## Observations et limites

- L’ouverture présente toujours les références et la chronologie avant la synthèse (p.1–2). C’est conforme au contrat de cette QA de publication et ne constitue pas un défaut.
- L’absence de bandeau TLP est volontaire. Aucun TLP n’a été inféré.
- Aucun test logiciel n’a été exécuté; cette revue porte sur le rendu visuel et l’extraction du PDF.
- Ce rapport de QA ne vaut ni approbation éditoriale ni autorisation de publication.
