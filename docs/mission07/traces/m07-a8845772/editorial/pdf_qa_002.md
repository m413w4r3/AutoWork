# QA de composition Typst — avis v9

**Verdict : FAIL pour validation éditoriale.** La narration est lisible et le rendu est conforme à la famille de marque, mais des défauts de sens et de pagination restent visibles; une ligne d'IOC est masquée par le pied de page.

## Identité et méthode

- PDF : `.autowork-runs/m07/editorial/opinion-v9-draft.pdf`, 22 pages, Typst 0.15.1, SHA-256 `ba8157515326e6f76e864b782ee8413f6450c4bb534f1a6ade4abb6654888d33`.
- Base locale examinée : `main@aab700106a00777ff7a241467dfdb6c34a8eaa0f`.
- Référence : `docs/references/CN2609_typ/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf`, 120 pages, SHA-256 `1ea738682bf922449e28c30c885b9712af0b24a0314c3481bd41554ed67ce64e`.
- Les 22 pages ont été examinées visuellement à 120 dpi (rendus dans `/tmp`); les pages de référence 1, 4, 9 et 119 ont été comparées au PDF et aux templates `docs/references/CN2609_typ` et `docs/references/chpTypst`.
- Reproducteur visuel : `pdftoppm -png -r 120 .autowork-runs/m07/editorial/opinion-v9-draft.pdf /tmp/m07-v9-hi`; pages en cause : 1–2, 9–18 et 22. SHA du PDF ci-dessus.
- Hanken Grotesk et Cascadia Mono sont intégrées dans le PDF. Logo, en-tête et pagination rouge « BROUILLON NON APPROUVÉ » sont visibles sur les pages, y compris les pages paysage. Le corps à 11 pt et les marges suivent la famille de templates. Le tableau de référence p.119 est paysage et composé à 9 pt; les cellules de l'annexe v9 descendent à 6,5–7,5 pt.
- Les 7 SHA-256 de l'inventaire p.10 sont présents au complet à l'extraction (plusieurs passent à la ligne); les 8 SHA-256 de p.19 sont complets. Pas de hash visiblement tronqué. Les tables de l'annexe utilisent des retours qui rendent toutefois les continuations de hash moins immédiates à lire.
- Le bandeau TLP est absent. C'est conforme : aucune valeur TLP canonique n'est disponible; ne pas en inférer une. Le rendu n'affiche que la classification que reçoit le template (`chpTypst/RENDERER/publication.typ:8,19–27`).
- `pdfinfo` émet `Syntax Error: Suspects object is wrong type (boolean)` pour le brouillon **et** le PDF de référence. Les deux déclarent ensuite `Suspects: no`; ce n'est pas un défaut propre au v9.

## Revue page par page

| Page | Conformité et constat | Défaut / sévérité |
|---|---|---|
| 1 | Portrait; logo, titre, carte statut/sources et pied présents. Références et chronologie occupent la page. | **Majeur** — l'ouverture ne présente pas la synthèse; le titre de campagne est suivi des références et de la chronologie. |
| 2 | Portrait; fin de la chronologie, puis titre « SYNTHÈSE » et texte d'ouverture. Typographie et pagination cohérentes. | **Majeur** — la chronologie continue avant la synthèse; l'ordre éditorial reste inversé par rapport à l'ouverture de référence. |
| 3 | Portrait; section narrative et texte technique lisibles. Pas de débordement; coupure de paragraphe en bas de page normale. | Aucun défaut majeur. |
| 4 | Portrait; suite du récit et encadrés de comparaison technique. Logo, corps et pied corrects. | Aucun défaut majeur; quelques césures de fin de ligne. |
| 5 | Portrait; encadré de configuration puis section GitHub. Pas de tableau cassé ni de débordement. | Aucun défaut majeur. |
| 6 | Portrait; prose, schéma et légende présents. Schéma lisible et contenu dans la page. | Aucun défaut majeur. |
| 7 | Portrait; suite du récit et pivots de détection. Texte dense mais lisible. | Aucun défaut majeur; césures automatiques visibles. |
| 8 | Portrait; fin de l'article, limites et remarques sourcées. Pas de contenu coupé. | Aucun défaut majeur. |
| 9 | Portrait; références de limites, sources et début de l'avis analyste. Le bas de page reste largement vide. | **Majeur** — page clairsemée; « Confiance : 90%/80% » et backticks Markdown imprimés dans les paragraphes. |
| 10 | Paysage; tableau d'inventaire à 4 colonnes, en-tête et 7 lignes présents; tous les hashes sont complets. | **Mineur** — hashes répartis sur deux lignes et « octets » coupé; beaucoup d'espace sous le tableau. |
| 11 | Portrait; seul le disclaimer sur `FlightManager.csproj` occupe deux lignes au début. | **Majeur** — page presque vide, note orpheline après le tableau paysage; backticks littéraux. |
| 12 | Portrait; blocs 1.3–1.5, références et texte lisibles. | **Majeur** — valeurs 85%, 80%, 80% affichées comme probabilités; backticks visibles autour des noms techniques. |
| 13 | Portrait; fin de l'avis, blocs 1.6–1.7 et points non résolus. Pas de débordement. | **Majeur** — valeurs 70% et 85% non calibrées; backticks visibles. |
| 14 | Portrait; fin des points non résolus puis IOC de l'article et pistes non confirmées. | **Mineur** — métadonnées et sources serrées, grisées et petites; pas de coupe manifeste. |
| 15 | Portrait; suite des pistes IOC, séparées par type. Les entrées restent visibles; espace libre en bas. | **Mineur** — page de continuation peu remplie; texte de provenance petit. |
| 16 | Portrait; page vide hors logo/en-tête et pied `16 / 22`. | **Majeur** — page blanche parasite au passage vers l'annexe paysage. |
| 17 | Paysage; seul le titre « ANNEXE TECHNIQUE » et le titre du groupe Réseau sont présents. | **Majeur** — titre d'annexe orphelin, tableau repoussé à p.18. |
| 18 | Paysage; tableau Réseau avec en-têtes et lignes répétées. | **Bloquant** — la dernière ligne déborde sous le pied rouge; le texte de preuve/pertinence est masqué ou coupé. `pdftotext` place le pied au milieu de cette ligne. Texte de contexte en 6,5 pt. |
| 19 | Paysage; hashes et début du tableau des noms de fichiers; les 8 hashes affichés sont complets. | **Mineur** — contexte très petit (6,5–7,5 pt) et gris, mais sans coupe de ligne visible. |
| 20 | Paysage; suite du tableau des noms de fichiers, en-têtes présents. | **Mineur** — lignes de contexte très denses et petites; pas de débordement visible. |
| 21 | Paysage; fin des noms de fichiers et annexe de traçabilité; identifiants complets. | **Mineur** — deux tableaux et provenance partagent la page; corps de référence petit mais contenu visible. |
| 22 | Paysage; boîte de provenance avec identité et SHA-256, puis pied. | **Majeur** — page presque vide; la phrase unique est étirée par la justification, puis le hash passe seul à la ligne. |

## Classement de parité

- **RESTORE_REFERENCE** — rétablir une ouverture par la synthèse (p.1–2), un rendu de code inline lisible (p.9, 11–13), des pages sans orphelins ni débordements (p.11, 16–18) et une annexe paysage aussi lisible que possible au regard de la table 9 pt de la référence p.119 (p.14–15, 18–21). Revoir aussi la provenance isolée p.22.
- **KEEP_JUSTIFIED_EXTENSION** — conserver l'absence de bandeau TLP faute de TLP canonique; conserver les SHA-256 complets même lorsqu'ils se replient à la ligne p.10.
- **DEFER** — masquer la métrique `certainty` dans l'avis tant qu'une calibration n'est pas définie (p.9, 12–13). Le diagnostic `pdfinfo` est commun au brouillon et à la référence et ne nécessite pas de changement visible du document.

## Correctifs TECH-FIX prioritaires

1. **TECH-FIX-01 — Bloquant, p.18 : empêcher le tableau IOC de passer sous le pied.** Dans `chpTypst/RENDERER/publication_helpers.typ:186–242,419–489`, le tableau et les cellules sont non sécables par défaut; les lignes longues peuvent occuper la zone du pied. Rendre les groupes réellement sécables avec en-têtes répétés et garder chaque ligne dans la zone imprimable; raccourcir ou répartir les cellules de contexte. **Test à ajouter** dans `backend/tests/test_typst_edition_runtime.py` : compiler un groupe réseau assez long pour passer sur deux pages; vérifier en-têtes répétés, dernière ligne complète et boîtes de texte entièrement au-dessus du pied sur chaque page.
2. **TECH-FIX-02 — Majeur, p.9 et 11–13 : traiter les spans de code inline dans les paragraphes d'analyste.** `backend/src/cti_app/application/typst_rendering.py:605–620` ne marque que hashes/UUID; `chpTypst/RENDERER/publication_helpers.typ:273–300` envoie les autres spans à `#text(span.text)`, conservant les délimiteurs Markdown. Parser `code` inline en spans monospace et supprimer les backticks de délimitation dans la sortie. **Test à ajouter** dans `backend/tests/test_typst_edition_runtime.py` : rendre un paragraphe synthétique contenant ``Run `token` ``; le texte PDF conserve `token`, sans backticks, et la police du span est monospace.
3. **TECH-FIX-03 — Majeur, p.9 et 12–13 : ne pas présenter `certainty` comme un taux calibré.** `backend/src/cti_app/domain/analyst_opinion.py:245–256` valide seulement une valeur 0–1; `backend/src/cti_app/application/typst_rendering.py:2500–2510` la multiplie par 100; `chpTypst/RENDERER/publication_helpers.typ:1004–1016` l'étiquette « Confiance ». Masquer ce champ dans le PDF ou le remplacer par une qualification éditoriale validée, sans pourcentage. **Test à ajouter** dans `backend/tests/test_typst_edition_runtime.py` : une valeur synthétique `0.9` ne produit pas « Confiance : 90% »; statut et références restent présents.
4. **TECH-FIX-04 — Majeur, p.1–2 : faire passer la synthèse avant les références et la chronologie.** Le constructeur ajoute les références d'abord (`backend/src/cti_app/application/typst_rendering.py:2202–2210`), la chronologie ensuite (`:2283–2292`); `chpTypst/RENDERER/publication_helpers.typ:900–963` respecte cet ordre. Réordonner les sections pour ouvrir sur la synthèse/le lead, puis références et chronologie. **Test à ajouter** dans `backend/tests/test_typst_rendering.py` : vérifier l'ordre des sections projetées; test de rendu réel dans `test_typst_edition_runtime.py` : page 1 commence par la synthèse et ne contient pas la chronologie avant elle.
5. **TECH-FIX-05 — Majeur, p.10–11 et 16–18 : supprimer pages vides/orphelines et garder les notes avec leur tableau.** La table large imprime son suffixe après le retour au portrait (`chpTypst/RENDERER/publication_helpers.typ:273–289`), ce qui isole le disclaimer sur p.11. Le changement d'orientation de l'annexe (`:1167–1183`, retour `:1283`) suit un `pagebreak` explicite et laisse p.16 vide; titre de groupe et tableau se séparent avec le bloc/tableau non sécable (`:223–242,477–489`). Fusionner la note au tableau, éviter le double saut au changement d'orientation et garder le titre avec la première ligne. **Test à ajouter** dans `backend/tests/test_typst_edition_runtime.py` : fixture avec table paysage et annexe; aucune page de corps vide, aucun disclaimer isolé, titre d'annexe suivi de son tableau sur la même page ou immédiatement après, sans recouvrement du pied.
6. **TECH-FIX-06 — Majeur, p.18–21 : améliorer la lisibilité des tableaux d'annexe.** `chpTypst/RENDERER/publication_helpers.typ:433–488` rend contexte et provenance à 6,5–7,5 pt. La référence paysage p.119 utilise une table à 9 pt (`docs/references/CN2609_typ/reports/table_reporting.typ:1–2,666–700`). Répartir les champs très longs, retirer les répétitions non nécessaires ou reporter le détail; garder hashes et provenance complets. **Test à ajouter** dans `backend/tests/test_typst_edition_runtime.py` : fixture d'annexe imprimable avec hashes complets, données non coupées et taille minimale lisible vérifiée dans le PDF.
7. **TECH-FIX-07 — Majeur, p.22 : intégrer proprement la provenance et désactiver la justification de sa phrase courte.** Le bloc final est ajouté après tous les tableaux (`chpTypst/RENDERER/publication_helpers.typ:1280–1283`); `render-publication-provenance` est à `:404–417`; la justification globale vient de `chpTypst/UTILS/document_style.typ:72`. La placer avec les métadonnées d'ouverture ou réduire la boîte, avec `justify:false` et coupure maîtrisée du hash. **Test à ajouter** dans `backend/tests/test_typst_edition_runtime.py` : le libellé et hash restent complets sans espacement étiré; la provenance ne crée pas une page finale presque vide.

## Décision sur les points ciblés

- (a) Confirmé : backticks imprimés p.9, 11, 12 et 13.
- (b) Confirmé : pourcentages issus directement de `certainty`; aucune calibration n'est définie par le type de domaine.
- (c) Confirmé : p.9 est clairsemée et l'ouverture place références/chronologie avant synthèse.
- (d) Bandeau TLP absent volontairement; ne pas en inventer.
- (e) Cette revue QA v9 a été faite page par page; résultat FAIL ci-dessus.
- (f) Avertissement `pdfinfo` reproduit aussi sur la référence; pas de différence v9.

**Tests exécutés :** aucun test logiciel. Contrôles de rendu effectués : SHA-256, `pdfinfo`, `pdffonts`, extraction de texte, comparaison visuelle page par page et vérification de présence des hashes.

**Signoff requis :** nouvelle revue visuelle humaine après les correctifs avant approbation éditoriale; ce rapport n'autorise ni approbation ni publication.
