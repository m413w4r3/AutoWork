# QA PDF — aperçu d’avis v4

**Verdict : FAIL** — le texte et les références sont globalement cohérents, mais la pagination de fin, les en-têtes irréguliers et la lisibilité/localisation de l’annexe ne permettent pas une validation de publication.

## Identité et méthode

- PDF examiné : `.autowork-runs/m07b/editorial/opinion-v4-draft.pdf`, 19 pages, SHA-256 `e322f2303fd627ba3ee029e63fd05a092171d72584ddfa97fca5b9b363a6808a`.
- Rendu des 19 pages à 110 dpi dans `/tmp/m07b-pdf-qa-004`; examen visuel, extraction `pdftotext -layout`, `pdfinfo` et `pdffonts`.
- Référence : `docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf`, SHA-256 `1ea738682bf922449e28c30c885b9712af0b24a0314c3481bd41554ed67ce64e`; comparaison visuelle des pages 1 et 119, du gabarit `chpTypst/UTILS/header_footer.typ` et du tableau 9 pt de `CN2609_typ/reports/table_reporting.typ`.
- Les polices Hanken Grotesk et Cascadia Mono sont intégrées. Aucun test logiciel ni accès relais/PG/Docker n’a été utilisé.

## Constats priorisés

1. **P1 — pagination finale, p.12 et p.19.** p.19 ne contient que quatre identifiants E11–E14 en haut de page, sans titre répété, puis reste presque entièrement vide. p.12 ne contient que six noms de fichiers, avec une grande zone blanche. Cause probable : la liste de traçabilité se coupe après E10 et le changement d’orientation / de bloc annexe provoque une page de continuation trop courte.
2. **P1 — en-tête/logo absent, p.2, 10, 11, 14 et 15.** Ces pages ont leur pied et leur pagination, mais pas le logo ni l’en-tête visibles sur les pages voisines. Cause probable : les flux de continuation de chronologie, d’IOC et de tableaux ne réappliquent pas systématiquement l’en-tête de page.
3. **P2 — portée du titre, p.1–2, à harmoniser avec p.8.** Le titre et l’ouverture parlent des « infrastructures critiques irakiennes »; la conclusion précise que les éléments décrivent une cible individuelle probablement ingénieur, et aucune série de compromissions confirmées. Le cadrage peut être compris comme une compromission des infrastructures elles-mêmes. Cause probable : reprise du cadrage sectoriel de la source sans la réserve de portée dans le titre.
4. **P2 — taxonomie brute en anglais, p.10–18.** Les annexes françaises affichent des marqueurs tels que `subject link not demonstrated`, `contextual indicator`, `source_verified` et `malicious_role_not_demonstrated`. Cause probable : rendu direct de valeurs internes de provenance/statut, sans libellés éditorialisés; cela alourdit les cellules et rend la distinction des statuts moins immédiate.
5. **P3 — tables et hashes, p.1, 4–5, 10, 13–18.** Les valeurs des huit hashes d’IOC, les identifiants de référence et la provenance sont complets; le SHA de provenance p.1 et le hash de comparaison p.10 se replient toutefois au milieu de la chaîne. La colonne « Contexte et provenance » des tables paysage est petite, dense et peu contrastée face au tableau de référence p.119. La comparaison de déchiffrement amorcée p.4 se termine par une seule carte en tête p.5. Cause probable : colonnes étroites et blocs/tabulations non gardés ensemble.

## Revue page par page

| Page | Observation |
|---|---|
| 1 | Logo, titre, carte statut/sources, provenance et références présents; début de chronologie. SHA de provenance complet mais coupé par le retour de ligne. |
| 2 | Fin de chronologie puis début de synthèse : ordre contractuel respecté. En-tête/logo absent. |
| 3 | Récit technique lisible; pas de débordement constaté. |
| 4 | Suite narrative et comparaison technique; la dernière carte est repoussée p.5. |
| 5 | Fin de la comparaison puis section GitHub; contenu complet. |
| 6 | Analyse réseau, figure et légende visibles; pas de chevauchement. |
| 7 | Attribution et distinction entre Blackwood et Blinder Tunnel prudentes; corrélations non présentées comme preuve d’identité. |
| 8 | Conclusion et limites cadrent la portée sur une cible individuelle; réserves explicites. |
| 9 | Sources et avis analyste présents; H1, H2 et H3 restent qualifiés, avec limites expliquées. Renvois F1–F3/E1–E14 visibles. |
| 10 | Hypothèses, points non résolus et début des IOC; en-tête/logo absent. Hash de comparaison replié sur deux lignes. |
| 11 | Suite des IOC et pistes; en-tête/logo absent; aucun contenu sous le pied. |
| 12 | Suite courte de la liste des pistes; logo présent, page très clairsemée. |
| 13 | Annexe technique paysage; titre, sous-titre et premières lignes du tableau réseau présents; colonne de provenance dense. |
| 14 | Suite du tableau et hashes; en-tête/logo absent; valeurs complètes et visibles. |
| 15 | Dernier hash puis tableau des fichiers; en-tête/logo absent; cellules de provenance petites. |
| 16 | Tableau des fichiers; en-tête/logo présent, aucune ligne ne passe sous le pied. |
| 17 | Tableau continué, en-tête répété; texte de provenance petit mais rendu. |
| 18 | Fin du tableau puis traçabilité F1–F3/E1–E10; aucun chevauchement avec le pied. |
| 19 | Seulement E11–E14, sans intitulé répété; page quasi vide. |

## Régressions ciblées et intégrité

- **Corrigés / non observés :** aucun backtick Markdown littéral; aucun « Confiance : NN % »; aucun titre seul en bas; aucune page entièrement blanche; aucune ligne de tableau sous le pied; justification sans étirement manifeste.
- Les 19 UUID extraits sont complets (identité de publication, provenance, findings et evidences). Les 8 hashes d’IOC sont complets; les deux autres hashes affichés dans le corps sont également présents au complet malgré leur retour à la ligne.
- Pas de contradiction de date entre p.2 et p.6 : l’échantillon soumis le 25 mai 2025 concerne une autre infrastructure que l’événement associé au 24 mai 2026.
- L’ouverture suit bien l’ordre références → chronologie → synthèse. La carte indique six sources et six références sont listées.
- Aucun bandeau TLP n’est présent, conformément à l’absence de TLP canonique; aucun TLP n’a été inféré. Les mentions « TLP non précisé » des lignes d’annexe ne forment pas un bandeau.
- `pdfinfo` émet « Suspects object is wrong type (boolean) » sur ce PDF et sur la référence; ce n’est pas un défaut spécifique à v4. Les deux déclarent ensuite `Suspects: no`.
- La revue confirme la cohérence interne des statuts et réserves visibles dans le PDF; elle ne remplace pas une vérification indépendante des observations canoniques ni un sign-off éditorial.
