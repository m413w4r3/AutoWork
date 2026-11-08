# M18-L0 — Spécification de format des ARTICLES

Statut : spécification de travail destinée à L1–L4. Les règles R1–R9 du plan
M18 sont normatives. Les valeurs indiquées comme « source locale » viennent des
fichiers Typst suivis dans ce dépôt ; celles des PDF CN/RU ne sont pas vérifiées
dans cette base, car docs/references/ est absent.

## 1. Portée et niveau de preuve

Cette spécification distingue trois niveaux :

- **Vérifié dans le code** : observé dans les fichiers indiqués avec leurs
  lignes.
- **Rapporté par le plan** : constat fourni par
  docs/mission18/M18_RENDER_IMPROVEMENT_PLAN.md, sans accès à la pièce source
  correspondante dans ce worktree.
- **À confirmer** : recommandation de conception ou fait non mesurable ici.

Convention des références de code : `helpers.typ`, `document_style.typ`,
`colors.typ` et `header_footer.typ` sont sous `chpTypst/UTILS/` ;
`publication.typ`, `publication_helpers.typ`, `edition.typ` et
`edition_helpers.typ` sont sous `chpTypst/RENDERER/` ; les modules `.py` cités
sans préfixe `domain/` sont sous `backend/src/cti_app/application/`, et
`publication_document.py` sous `backend/src/cti_app/domain/`. Les chemins
complets cités font exception à cette convention.

Le plan désigne les fichiers
`docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf`,
`docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-RU-2609.pdf`,
et les répertoires `docs/references/CN2609_typ` et
`docs/references/RU2609_typ`. Aucun de ces chemins n’existe dans cette base ;
aucun PDF ni aucune page d’article CN/RU n’a donc été extrait ou rendu. Les
exécutables pdftotext, pdftoppm et pdfimages sont présents, mais ne peuvent pas
remplacer les fichiers manquants. Le répertoire .codegraph/ est vide et aucun
index CodeGraph n’est disponible. Les gabarits consultés sont ceux suivis dans
chpTypst/, qui ne sont pas les chemins docs/references/chpTypst indiqués par le
plan.

Conséquence : les chiffres de densité ci-dessous sont explicitement incomplets.
Ne pas les présenter comme une mesure page-à-page ni comme une moyenne CN/RU.
Aucun contenu éditorial CN/RU n’est reproduit dans ce document.

## 2. Forme d’un ARTICLE de référence

### 2.1 Enveloppe de page

| Élément | Spécification à retenir | Source / état |
|---|---|---|
| Papier et marges | A4 ; haut/bas 2,3 cm ; gauche/droite 2,2 cm. | Vérifié : chpTypst/UTILS/document_style.typ:30-51. |
| Texte courant | Hanken Grotesk, 11 pt, langue fr ; paragraphes justifiés. | Vérifié : document_style.typ:30-35, 72. |
| En-tête de référence | Texte fixe rapporté par le plan : « Bulletin n°3 / Septembre 2026 ». Reprendre le même rôle éditorial (titre du bulletin et période), avec les valeurs de l’édition en cours. | Rapporté par le plan ; référence PDF non disponible. |
| Pied de page de référence | Pied de bulletin et pagination courante/total ; conserver une ligne fine de séparation. Le texte exact du pied doit être relevé sur le PDF CN/RU quand il sera disponible. | À confirmer visuellement. |
| État du renderer actuel | Publication autonome : marque Chaps Vision, classification et document_header en en-tête ; pied issu du style commun. Édition : en-tête réduit dans les pages après la couverture. | Vérifié : chpTypst/RENDERER/publication.typ:7-37 ; edition.typ:6-20 ; UTILS/header_footer.typ:25-70. |

Le style commun actuel réserve une hauteur d’en-tête de 28 pt dans la publication
et 14 pt dans l’édition. Le gabarit hérité header_footer.typ place chap.png à
gauche et le libellé à droite dans un tableau 1:5, avec trait de 0,6 pt. Son
en-tête construit « Actualité des codes et infrastructures {pays} — {période} »
(sans suffixe période si celle-ci est vide). Le pied construit
« Bulletin-{pays} | Actualité des codes et infrastructures {pays} | {période} »
et affiche le numéro de page courant/total ; le suffixe période est omis si
vide. Il utilise un trait de 0,6 pt, un libellé de 9 pt et un compteur gras de
8 pt. Ces mesures et textes sont ceux du helper local, pas une mesure du PDF
absent.

### 2.2 Ordre d’ouverture et titre

1. Garder le titre éditorial sous la forme **[Acteur] Titre**, sans ajouter de
   texte technique interne.
2. Afficher le titre avant la ligne de classement dans le gabarit article
   hérité. Le titre est violet/vert de palette, 16 pt regular dans
   TEMPLATES/article.typ:18-22 et UTILS/helpers.typ:308-314.
3. Afficher ensuite le label **ARTICLE** en capitales et le numéro d’article
   aligné à droite. Le label hérité est 8 pt gras, tracking 0,5 pt, fond
   purple-light, inset horizontal 8 pt / vertical 4 pt, coins de 3 pt ;
   le numéro est 11 pt gras purple. Sources : helpers.typ:112-126,
   TEMPLATES/article.typ:18-22.
4. Commencer chaque article sur une nouvelle page. Le helper article() appelle
   pagebreak() : helpers.typ:287-307.

Écart actuel : le rendu en édition place le titre puis le couple ARTICLE/numéro,
avec 10 pt après le couple. Le rendu standalone ne projette pas ce couple.
L1 doit uniformiser les deux chemins ARTICLE sans casser le couple BRÈVE,
actuellement distinct (edition_helpers.typ:13-49).

### 2.3 « Vue d’ensemble »

Afficher le bloc immédiatement après le titre et le label ARTICLE, avant la
narration. Il contient exactement trois champs, dans cet ordre :

- Victimologie
- Arsenal
- Objectif principal

Les valeurs doivent provenir de la synthèse et être soutenues par ses
références d’évidence ; elles ne sont ni un statut de traitement, ni un
comptage de sources.

Paramètres du gabarit hérité : titre de bloc « Vue d’ensemble » à 14 pt
extrabold, purple ; fond purple-light (#E8F0F0) ; inset 12 pt ; rayon 4 pt ;
étiquette de champ semibold purple avec puce. Sources : helpers.typ:128-135,
209-224, TEMPLATES/article.typ:26-30, 342-350.

Écart actuel : render-data « overview » ne contient que status_label et
source_count (typst_rendering.py:1057-1060). publication_helpers.typ:1412-1437
rend STATUT/SOURCES dans un encadré à fond #F1F6F5, trait #CAD8D6, inset
9 × 7 pt. Il ne s’agit pas de la Vue d’ensemble éditoriale demandée.

### 2.4 Sections et rythme typographique

- Un h1 de synthèse : 20 pt extrabold purple, 28 pt au-dessus et 16 pt
  au-dessous.
- Un h2 : 14 pt bold purple, 20 pt au-dessus et 12 pt au-dessous.
- h3 : 12 pt bold purple, 15 pt au-dessus et 8 pt au-dessous.
- h4 : 10 pt bold purple, 15 pt au-dessus et 8 pt au-dessous.

Ces valeurs sont celles de document_style.typ:92-114. L’ancien helper de
section applique également 14 pt extrabold purple, un espace interne de 4 pt
et 6 pt au-dessous (helpers.typ:128-135). Le renderer actuel donne aux
section_heading 7 pt au-dessus et 5 pt au-dessous, et aux paragraphes un écart
de 3 pt par défaut (publication_helpers.typ:939-950). L1 doit établir une
échelle cohérente et mesurable plutôt que juxtaposer ces règles.

L’ancien gabarit article fixe par(spacing: 2em) avant le corps quand il existe
une chronologie (helpers.typ:353-358). Ce réglage local n’est pas une preuve
que le PDF de référence utilise 2em entre tous les paragraphes ; le comparer
aux PDF avant d’en faire une valeur globale. Ne pas supprimer les paragraphes
courts par fusion artificielle.

### 2.5 Références

Titre : « Références ». Une puce par source ; une ligne contient éditeur,
titre, date et URL, dans cet ordre, séparés par un tiret cadratin. Ne pas
numéroter la liste et ne pas utiliser de tableau. Les URL restent des liens
cliquables. Les numéros de notes de bas de page dans le corps sont des appels
de note, distincts de l’ordre des références.

Le helper historique source-list applique un bloc de 4 pt au-dessus et au-dessous,
titre gras, éditeur/date gris de 9 pt, puis URL (helpers.typ:590-612).
Le renderer courant produit une entrée numérotée et datée (8 pt gris), puis le
texte et une ou plusieurs URL (publication_helpers.typ:186-222) ; la section
est en capitales (publication_helpers.typ:968-1006). R1 demande de remplacer
ce rendu par des puces non numérotées.

### 2.6 Figures, tableaux, encadrés, code et notes

| Élément | Paramètres vérifiés dans les gabarits locaux |
|---|---|
| Figure | Image centrée ; légende « Figure n : … », 9 pt italique gris, centrée, 4 pt sous la légende. Le style Typst global utilise supplement « Figure », séparateur « : », caption 9 pt italique. Sources : publication_helpers.typ:550-568 ; document_style.typ:87-90. |
| Figure source dans le renderer actuel | Bloc non sécable, 10 pt dessous ; limite de hauteur 36 % de la zone ; annotations de source/attribution gris 7 pt. Diagramme : limite usuelle 35 % ; graphique : 55 %. Sources : publication_helpers.typ:711-752, 754-831, 833-845. |
| Tableau styled-table | Corps 9 pt ; en-tête purple (#5D7374), texte blanc gras ; cellules avec inset 5 pt horizontal / 4 pt vertical ; trait 0,6 pt gris ; en-tête répétée ; tableau centré. Jusqu’à 12 lignes de corps, le bloc est non sécable ; au-delà, il peut se répartir. Titre éventuel : 6 pt avant, puis 4 pt entre titre et tableau. Source : helpers.typ:366-429. |
| Callout Vue d’ensemble | Fond #E8F0F0 ; inset 12 pt ; rayon 4 pt ; libellés purple semibold. Source : helpers.typ:209-224. |
| Callout analyste | Le composant disponible est noteAnalyste, pas une fonction générique callout : fond notion-gray-bg (#F1F1EF), largeur 100 %, rayon 4 pt, inset 8 pt, bloc non sécable ; « Note de l’analyste » 14 pt extrabold purple-dark. Source : helpers.typ:226-247. Aucun callout générique nommé callout n’a été trouvé dans helpers.typ local. |
| Code brut | Familles de repli, dans cet ordre : Cascadia Mono, Cascadia Code, Cascadia Mono PL, Cascadia Code PL ; raw en bloc noir, texte blanc 8,8 pt, inset horizontal 10 pt / vertical 1 pt, rayon 2 pt, sortie verticale 5 pt, interligne 0,95 em. Source : helpers.typ:5-10 ; document_style.typ:53-70. |
| Note de bas de page | La marque est mise en exposant et surlignée gris transparent ; indentation d’entrée 0 em. Les références fn(url) utilisent un lien cliquable. Sources : document_style.typ:76-85 ; helpers.typ:431-435. |
| Séparateur | Ligne 0,6 pt, light-grey (#E4E7EC). Source : helpers.typ:203-206. |

Palette locale : accent #243B53, accent-light #E8EEF3, dark #17202A, grey
#667085, light-grey #E4E7EC, notion-gray-bg #F1F1EF, purple #5D7374,
purple-light #E8F0F0, purple-dark #3D5051 (UTILS/colors.typ:1-21). Le nom
« purple » désigne un vert-gris dans cette palette ; garder les noms et valeurs
tels quels pour la parité.

### 2.7 IOC de fin d’article

Le dernier bloc de l’article s’intitule exactement « IOC ». Il présente des
listes simples, sans tableau ni statut, en n’affichant que les catégories
non vides, dans cet ordre :

1. Adresses IP
2. URL
3. Noms de domaine
4. Fichiers

« Fichiers » regroupe hashes, noms de fichiers et chemins. Les valeurs sont
défanguées pour l’affichage et conservées sans césure linguistique ; aucun
marqueur « Indicateur », « Relation », statut, provenance ou numéro de source
ne doit être attaché à chaque valeur. Ne pas fabriquer une catégorie
« e-mail » absente de R4. Les données canoniques et exports restent complets.

Le helper historique ioc-list propose IP, domaines, URL, e-mail et hashes, avec
valeurs en police monospace 8,75 pt (helpers.typ:443-588). Le renderer courant
rend d’abord « IOC de l’article », puis « IOC originaux » et éventuellement
« Pistes non confirmées » (publication_helpers.typ:1230-1269). La taxonomie
finale est donc à changer.

## 3. Densité CN/RU et cibles mesurables

### 3.1 Mesures disponibles dans cette base

| Corpus / article | Figures | styled-table | Callouts | Code | Mots de synthèse | Sections titrées | Titres abstraits observés | État |
|---|---:|---:|---:|---:|---:|---:|---|---|
| CN / article01 | 17 | 8 | N/D | N/D | N/D | N/D | N/D | Rapporté uniquement dans M18_RENDER_IMPROVEMENT_PLAN.md ; source absente. |
| CN / autres articles | N/D | N/D | N/D | N/D | N/D | N/D | N/D | docs/references/CN2609_typ et PDF absents. |
| RU / tous les articles | N/D | N/D | N/D | N/D | N/D | N/D | N/D | docs/references/RU2609_typ et PDF absents. |

N/D signifie non mesuré ; ce n’est pas zéro. Le plan décrit les deux PDF comme
comptant respectivement 120 et 114 pages, mais ces totaux ne sont pas confirmés
ici. Les nombres 17 et 8 sont les seules densités par article explicitement
rapportées par le plan.

Pour mesurer chaque article quand les références seront de nouveau montées,
compter dans les sources Typst :
figures = chaque figure visible numérotée une fois (image, diagramme ou
graphique, sans recompter l’asset) ; tableaux = #styled-table(...) ; callouts =
les boîtes éditoriales, dont noteAnalyste(...), avec le bloc Vue d’ensemble
compté séparément ; extraits = blocs de code bruts ;
mots = corps de synthèse uniquement, hors références, IOC, en-têtes et légendes ;
sections = headings h1/h2. Vérifier les comptes par article avec
pdftotext -layout et les pages rendues ; noter séparément les images sources,
diagrammes reconstruits et graphiques.

### 3.2 Cibles provisoires R7/R9

Les objectifs d’enrichissement reprennent le plan M18 et restent à calibrer
contre toutes les pages CN/RU. Le plancher de mots et le nombre de sections
sont des propositions L0, pas des mesures ni des seuils explicitement fixés
par le plan :

- **Tableaux** : au moins 2 par ARTICLE si les preuves donnent au moins deux
  objets/comportements comparables ou une correspondance utile. Un tableau
  doit apporter une consultation plus rapide que la prose.
- **Diagramme** : au moins 1 diagramme de chaîne/infrastructure dès qu’au
  moins 3 étapes ou relations sont explicitement documentées et que le schéma
  ne répète pas une assertion déjà lisible dans la synthèse.
- **Figures sources** : inclure toutes celles qui sont archivées, pertinentes
  pour cet article, lisibles et hors logos/chrome ; aucun nombre fixe sans
  inventaire par article.
- **Graphique** : seulement si plusieurs observations datées exactes révèlent
  une distribution temporelle utile ; ne pas interpoler.
- **Longueur** : ≥ 1 500 mots de synthèse comme plancher d’acceptation provisoire,
  avec au moins quatre sections h2 distinctes si la preuve permet de les
  soutenir. Ne pas compter les tableaux, légendes, références ni IOC comme mots
  de synthèse. Une borne supérieure ne peut pas être déduite sans les sources.
- **Paragraphes** : courts à moyens, un mécanisme/étape par paragraphe. Garder
  le seuil de longueur subordonné à l’évidence : aucune répétition pour atteindre
  un nombre de mots ou de médias.

Le plan rapporte un article CN avec 17 figures et 8 tableaux ; cette densité
constitue seulement un point de référence isolé, pas une moyenne ni un minimum
pour chaque article. L’orchestrateur doit remplacer ces cibles provisoires par
des médianes/étendues CN/RU une fois les sources accessibles.

## 4. Écarts du renderer et changement par exigence

| Exigence | État actuel observé | Changement L1/L2 proposé |
|---|---|---|
| R1 Références | Entrées numérotées avec date séparée, texte et URL(s) : publication_helpers.typ:186-222, 994-1005. | Une puce par source : éditeur — titre — date — URL ; zéro tableau et zéro numérotation de bibliographie. Garder les liens et les notes de bas de page ordinaires. |
| R2 Chronologie | Section CHRONOLOGIE et liste ordonnée pour article ; si une derived_timeline_figure existe, rendu de sa table Date/Événement, puis liste des non-datés : publication_helpers.typ:1023-1058, 570-596. L’ancien helper fait une liste d’événements et non une frise : helpers.typ:152-201. | Supprimer la section/table actuelles pour ARTICLE et dessiner une frise native Typst à partir de timeline_key_events. Aucune image compilée. |
| R3 Espacement | Paragraphes 3 pt, heading bloc 7/5 pt, figure 10 pt dessous, tableau dépendant du titre et du nombre de lignes : publication_helpers.typ:939-950, 711-715 ; helpers.typ:409-428. | Définir une seule échelle verticale partagée : paragraphe 5 pt après ; h1 28/16 pt avant/après ; h2 20/12 pt avant/après ; h3/h4 15/8 pt ; figure/table/callout 10 pt avant et 8 pt après ; listes 2–3 pt entre items ; références 4 pt entre entrées. Ce sont des valeurs de proposition L0 à ajuster à la mesure PDF, non des mesures CN/RU. |
| R4 IOC | Sorties « IOC de l’article », « IOC originaux », « Pistes non confirmées » ; les groupes existants ne sont pas la taxonomie R4 : publication_helpers.typ:1230-1269. | Une section « IOC » avec Adresses IP, URL, Noms de domaine, Fichiers ; valeurs brutes défanguées, fichiers couvrant hashes/noms/chemins, listes simples. |
| R5 Marqueurs internes | Les libellés Indicateur/Relation sont dans le template : publication_helpers.typ:861-882. Les qualifications candidates et labels de statut sont projetés côté Python : typst_rendering.py:118-130, 909-961, 2012-2027, 2097-2106. Les identifiants de provenance sont conservés dans render-data sans impression par certaines figures : typst_rendering.py:2698-2710. | Ne plus projeter les statuts et renvois de statut dans les listes lecteur. Omettre les « Pistes non confirmées » du PDF ; préserver les valeurs et motifs dans l’artifact canonique et les exports. Supprimer du rendu toutes les sections « PROVENANCE DE PUBLICATION » et les hashes de document. Les appels de note bibliographique restent autorisés ; supprimer seulement les numéros de source/IOC parasites. |
| R6 Annexe IOC | Tableau paysage et titre « ANNEXE TECHNIQUE — inventaire exhaustif des IOC » : publication_helpers.typ:1271-1301 ; autres blocs d’annexe présents jusqu’à 1395. | Ne pas rendre la table exhaustive d’IOC. Garder les exports CSV/ZIP et toute autre matière technique seulement si elle relève explicitement du document article ; ne pas supprimer en amont les données canoniques. |
| R7 Enrichissement | Le prompt accepte zéro table, zéro diagramme et zéro figure ; aucune obligation conditionnelle article : production_editorial_enrichment.py:5153-5171, 5227-5236. Les limites et politiques sont détaillées en §7. | Mettre une exigence conditionnelle fondée sur les preuves ; vérifier les rejets et les figures archivées avant de conclure qu’aucun enrichissement n’est utile. |
| R8 Parité | Le template utilise Titre + tag ARTICLE ; le renderer autonome affiche un titre typé h1 et une overview de statut/source count, et l’édition ajoute un tag : publication_helpers.typ:1402-1445 ; edition_helpers.typ:13-30. | Aligner en-tête/pied, titre, article/numéro, overview, headings, figures, tableaux et notes sur le gabarit ci-dessus. Valider visuellement page à page contre les PDF CN/RU. |
| R9 Synthèse | Prompt v13 déjà exige prose technique française, contexte/victimologie/chaîne, TTP, outils, C2 et attribution nuancée, mais headings optionnels et aucun plancher de mots : production_prompts.py:19-65. | Prompt ARTICLE versionné v14+ ; imposer overview, sections courtes titrées et couverture hunting selon les preuves ; minimum provisoire 1 500 mots et quatre sections titrées. Garder BRIEF inchangé. |

### 4.1 Origine des libellés IOC et de provenance

| Texte ou donnée observée | Renderer Typst | Backend / projection |
|---|---|---|
| IOC de l’article | Titre littéral rendu dans publication_helpers.typ:1230-1237. | Le backend produit une section typée ioc et ses trois groupes retained/originals/candidates, sans le titre français : typst_rendering.py:2156-2161, 2770-2771. Le builder assemble les groupes et détails d’IOC : publication_builder.py:294-318, 524-536. |
| Indicateur : / Relation : | Libellés littéraux sous une valeur IOC : publication_helpers.typ:861-882. | Les labels sont projetés dans indicator_status_label et relationship_status_label : typst_rendering.py:2177-2183, 2143-2153, 2219-2227. Le renderer les affiche en texte lecteur. |
| Candidat non confirmé | Valeur dynamique dans les colonnes de statut IOC ; le template affiche les champs à publication_helpers.typ:864-880 et les lignes IOC annexes à 475-525. | Produit par les projections de draft IOC et d’IOC incertains : typst_rendering.py:909-927, 957-961, 2012-2027, 2101-2106 ; un label de liste est « Piste non confirmée » : 2177-2183. |
| contextual indicator | Le template ne contient pas cette expression anglaise ; la valeur française est portée par le label dynamique ci-dessus. | Motif interne snake_case contextual_indicator dans publication_builder.py:300-315 ; traduction « indicateur contextuel » dans typst_rendering.py:118-130. Cette chaîne ne doit pas être imprimée. |
| ANNEXE TECHNIQUE — inventaire exhaustif des IOC | Titre littéral et appel du tableau paysage : publication_helpers.typ:1271-1301. | Le backend assemble les lignes IOC dans une section de type technical_annex, sans ce titre : typst_rendering.py:2163-2167, 2772-2791. |
| PROVENANCE DE PUBLICATION / identifiants et hashes | Encadré et titre statique : publication_helpers.typ:453-473, 1279-1295, 1375-1392, 1440-1444. L’édition appelle aussi le helper : edition_helpers.typ:83-86. | Le renderer autonome/édition fournit les document labels au modèle de rendu : typst_rendering.py:1045-1055 ; edition_typst_rendering.py:167-180. Les métadonnées des figures sont aussi gardées dans render-data : typst_rendering.py:2698-2710. La présence d’un champ de provenance dans le JSON n’impose pas son impression. |

### 4.2 Frise chronologique Typst native — proposition R2

Contrat de sélection déterministe :

1. Partir exclusivement des entrées chronologiques canoniques qui ont une date
   ISO exacte et au moins une référence d’évidence.
2. Dédupliquer par couple date ISO + texte normalisé ; ne jamais convertir une
   date relative ou estimée en date exacte.
3. Si 2 à 8 événements restent, garder tous les événements. Si plus de 8,
   garder le premier et le dernier, puis les six entrées restantes avec le plus
   grand nombre de sources distinctes ; départager par date ISO puis texte
   normalisé. Restituer le résultat par date croissante.
4. Si moins de 2 événements datés restent, omettre le bloc et son titre
   « Chronologie » ; ne pas produire une frise vide.
5. La sélection et les règles de départage doivent être versionnées et testées.
   L2 garde la relation entre les événements retenus et leurs entrées canoniques.

Proposition de forme : frise verticale dans la largeur du texte, sans D2, SVG
ni bitmap. Une ligne accent de 1 pt relie des pastilles de 5 pt ; chaque entrée
est une rangée avec date française en 8 pt bold accent et libellé en Hanken
Grotesk 9–10 pt, marge verticale de 5 pt. Les références restent attachées aux
données canoniques et n’ajoutent pas de numéros visibles à côté du libellé.
La forme verticale reste lisible en A4 et ne réduit pas les libellés à des
cartes trop étroites. L1 peut implémenter les pastilles avec des formes Typst
natives ; le même JSON et le même ordre produisent le même résultat.

Points à confirmer par l’orchestrateur : plafond de huit événements et règle
de priorité fondée sur le nombre de sources. Si la synthèse canonique fournit
déjà une classe de jalon validée, celle-ci peut remplacer le score de sources
à condition que le départage reste stable et vérifiable.

## 5. Contrat canonique et modèle JSON Typst

### 5.1 Version recommandée

Le code actuel ne s’arrête pas à V6. L’assemblage ARTICLE construit V5 puis
l’enveloppe par les contrats de routage actuels ; PublicationDocumentV7 enveloppe
V6, lui-même enveloppant le V5 exact
(publication_assembly.py:148-155, 175-185 ;
backend/src/cti_app/domain/publication_document.py:2019-2045, 2225-2289).
L’ADR-0008 décrit V7 comme enveloppe de routage
(docs/adr/ADR-0008-publication-kind-routing.md:37-40). Le document
docs/publication_document_v6.md:7-9 affirme que l’assemblage normal écrit V5 ;
cette affirmation est dépassée par l’assembleur et l’ADR-0008 actuels.

Recommandation L2 : ajouter **PublicationDocumentV8** comme enveloppe additive
de V7, sans modifier les octets de V5/V6/V7 intégrés. V8 porte une section
ArticleFormatV1 avec overview, timeline_key_events et ioc_categories. Pour un
BRIEF, article_format est null ou absent de façon codée strictement ; le
renderer conserve le chemin BRIEF actuel. Garder les codecs de lecture V4–V7.

Nouveau modèle canonique recommandé :

- OverviewFieldV1 : text non vide, evidence_refs non vide.
- ArticleOverviewV1 : victimology, arsenal, main_objective, chacun de type
  OverviewFieldV1 ou null. Les champs non nuls exigent une évidence ; si les
  trois sont nuls, article_format.overview est null et le bloc n’est pas rendu.
- TimelineKeyEventV1 : event_date au format ISO date, text non vide,
  evidence_refs non vides ; l’événement doit correspondre à une entrée datée
  de timeline existante. La sélection R2 est reproductible à partir des entrées
  canoniques.
- PublicationIOCValueV1 : value, normalized_value, source_document_ids et
  evidence_refs. Aucun statut d’affichage. Garder la provenance, les décisions
  et les détails d’IOC existants dans les artifacts et exports.
- IOCArticleCategoriesV1 : ips, urls, domains, files, chacun une liste stable
  de PublicationIOCValueV1. Fichiers regroupe HASH, FILENAME et FILEPATH.
  E-mail n’a pas de catégorie R4 : garder les données/export actuels, mais ne
  pas les afficher comme une cinquième catégorie sans décision explicite.

Le changement est en amont du renderer : PublicationDocumentV5 contient déjà
le corps canonique, la timeline, les références et les groupes IOC ; les
wrappers V6/V7 ne doivent pas être réécrits en place
(backend/src/cti_app/domain/publication_document.py:1415-1448, 2019-2045,
2225-2289). Un wrapper V8 évite
de modifier le hash du V5 qui lie les wrappers existants et permet une lecture
versionnée stricte.

### 5.2 Effet sur hash, identité et réutilisation

- Changer le prompt de synthèse et refaire SYNTHESIS change le hash de
  synthèse ; l’enrichissement aval dépend de la synthèse et est à recalculer.
  Les étapes REFERENCES, EXTRACTION et RELEVANCE_PROJECTION restent les mêmes
  entrées réutilisées selon le plan.
- Changer seulement le prompt/politique d’enrichissement change
  editorial_enrichment_hash ; l’input_hash de publication dépend alors de ce
  nouvel hash.
- L’input hash de contenu publication incorpore snapshot, références,
  extraction, synthèse, enrichissement, projection de pertinence, version de
  schéma et policies (publication_builder.py:614-638). L’assemblage ajoute le
  type, la décision/règle de routage et la version V7
  (publication_assembly.py:394-446). Si le contrat canonique change, mettre à
  jour explicitement la version de schéma/policy dans cet input ; le hash du
  document canonique change aussi avec ses octets JSON.
- Ajouter seulement des données au render-data change render_data_sha256 ;
  changer les templates change leur template bundle hash et le source hash si
  l’entrypoint change. Les hashes de rendu et d’édition incluent les identités
  de template/version propres au rendu ; cela ne change pas les hashes
  SYNTHESIS ou ENRICHMENT (typst_rendering.py:1045-1083, 2856-2887 ;
docs/adr/ADR-0006-unified-publication-and-enrichment-architecture.md:359-364).
- Le template bundle hash porte sur les fichiers de manifeste. Ne pas supposer
  qu’un changement dans un helper inclus change source_sha256 de l’entrypoint ;
  il change le bundle hash (typst_rendering.py:2861-2877).
- Éviter la réutilisation silencieuse après changement de champs : la recherche
  réutilisable s’appuie sur input_hash et la publication compare également les
  octets canoniques avant de reprendre/réutiliser un artifact
  (publication_assembly.py:224-239, 304-364).

**Migration DB : non nécessaire pour ce changement de document**, tant que les
nouveaux champs restent dans le blob canonique versionné et qu’aucune colonne,
contrainte ou index relationnel n’est ajouté. La table production_artifacts
stocke stage/version/input_hash, des identifiants de blobs et metadata JSONB,
pas des colonnes PublicationDocument
(backend/src/cti_app/infrastructure/database/models/production.py:203-226 ;
backend/src/cti_app/infrastructure/database/repositories/production.py:625-646).
Les blobs conservent leur
schema_version propre. Toute proposition de colonne relationnelle exige une
analyse distincte et suit ADR-0007 ; ne pas modifier la baseline.

### 5.3 Contrat du JSON de rendu à partager L1/L2

Le modèle de rendu est une projection sérialisée ; les preuves canoniques ne
sont pas rendues comme texte. Conserver les champs racine et les variantes
`content_sections` du modèle actuel, puis versionner le JSON en
`typst-publication-model-v33-article-format`. L’extrait suivant définit tous
les champs ajoutés/modifiés par L0 ; aucun champ fourre-tout `object` n’est
introduit. Les champs racine existants sont `kind`, `document_label`,
`document_label_display`, `document_header`, `language`, `title`, `overview`,
`brand_logo_available`, `content_sections`, et leurs champs optionnels
`draft_watermark`, `article_title_target_id`,
`mapping_instrumentation_version`.

    PublicationRenderDataV33 = PublicationRenderDataV32 + {
      schema_version: "typst-publication-model-v33-article-format",
      article_format: null | ArticleFormatRenderV1
    }

    ArticleFormatRenderV1 = {
      overview: null | {
        victimology: OverviewRenderField | null,
        arsenal: OverviewRenderField | null,
        main_objective: OverviewRenderField | null
      },
      timeline_key_events: TimelineRenderEvent[],
      ioc_categories: {
        ips: IOCDisplayValue[],
        urls: IOCDisplayValue[],
        domains: IOCDisplayValue[],
        files: IOCDisplayValue[]
      }
    }

    OverviewRenderField = { text: string }
    TimelineRenderEvent = {
      date_iso: string,                    // date ISO YYYY-MM-DD
      display_date_fr: string,             // libellé français calculé par L2
      text: string                         // libellé court et étayé
    }
    IOCDisplayValue = { value: BreakableText }
    BreakableText = {
      text: string,
      break_chunks: string[],
      break_after?: boolean[]
    }

Pour un BRIEF, `article_format` vaut null. Pour un ARTICLE, `article_format`
est présent ; `overview` peut être null ou contenir les trois clés avec une
valeur texte ou null chacune. `timeline_key_events` ne contient que les
événements sélectionnés et validés par L2. Les quatre catégories IOC sont
toujours des clés présentes, même si leurs listes sont vides. Les entrées
`content_sections` gardent leur projection existante et ne servent pas de
source alternative pour ces trois champs.

En édition, le renderer autonome et chaque entrée `publications[]` de
`edition_typst_rendering.py` doivent employer le même `ArticleFormatRenderV1` ;
le wrapper d’édition et ses champs existants restent versionnés séparément.

Sémantique obligatoire : article_format est null pour un BRIEF ; la frise ne
s’affiche que si timeline_key_events contient au moins deux événements ; les
quatre catégories IOC sont toujours présentes comme clés, même si leurs listes
sont vides ; le rendu n’accède jamais à des statuts de provenance. L1 ne doit
pas recomposer la sélection chronologique ou la classification des IOC.

Le modèle courant v32 met schema_version, kind, document_label,
document_label_display, document_header, language, title, overview de
status_label/source_count, brand_logo_available et content_sections au niveau
racine (typst_rendering.py:1045-1065). `article_format.overview` reste distinct
de l’aperçu actuel qui alimente l’encadré Statut/Sources. La version v33 et
les noms proposés ci-dessus ne sont pas encore implémentés.

## 6. Guide abstrait du prompt de synthèse ARTICLE (R9)

Le prompt v13 demande déjà une prose dense, française, technique et étayée,
avec progression générale du contexte et de l’attribution vers la campagne,
la victimologie, la chaîne, les mécanismes/TTP, les composants, l’infrastructure
C2 puis les limites (production_prompts.py:25-73). Les headings restent
optionnels ; le v14+ doit imposer la structure ARTICLE sans changer BRIEF.

Plan recommandé, en n’utilisant que les faits réellement soutenus :

1. Lead : acteur et sujet, période si prouvée, impact ou intérêt CTI. Ne pas
   répéter le titre.
2. Vue d’ensemble : victimologie, arsenal, objectif principal ; une valeur
   courte et sourcée pour chaque champ.
3. Contexte et victimologie : cibles, secteurs, géographies et attribution
   avec niveau de confiance bref.
4. Chaîne d’attaque : étapes ordonnées uniquement quand l’ordre est prouvé ;
   regrouper les branches ou alternatives sans inventer de lien causal.
5. Capacités et techniques : exécution, persistance, élévation, évasion,
   anti-analyse et impacts qui sont documentés.
6. Infrastructure et C2 : protocoles, champs, flux, rôles et infrastructure
   observés ; séparer les inférences.
7. Détection et hunting : chemins, fichiers, clés de registre, mutex,
   commandes, user-agents, ports, motifs ou pivots effectivement présents dans
   les preuves. Ne pas transformer un indicateur en preuve d’attribution.
8. Limites : attribution et lacunes en un court paragraphe ; éviter de répéter
   les réserves dans chaque section si elles ne changent pas l’analyse.

Cible provisoire : ≥ 1 500 mots, au moins quatre h2 substantiels, paragraphes
courts à moyens et terminologie française cohérente. Les valeurs littérales
(commandes, chemins, hashes, champs, ports, user-agents) restent exactes et
monospace ; noms d’acteur/outils et termes techniques gardent leur style
sémantique. Pas de headings vides, de contenu pour atteindre artificiellement
une longueur, de tableaux de valeurs IOC répétés ni de détails non attestés.
Conserver les contrôles actuels de grounding, de références et de littéraux ;
les noms de sections sont des libellés éditoriaux, non des claims autonomes.

La longueur et le détail sont une cible de travail, pas une densité CN/RU
mesurée dans ce worktree. Adapter après lecture du corpus source.

## 7. Diagnostic d’enrichissement et changements L4 (R7)

### 7.1 Limites/politiques confirmées

| Zone | Limite/politique confirmée | Effet et proposition L4 |
|---|---|---|
| Politique et versions | La politique de domaine est actuellement editorial-enrichment-v11-editorial-tables (domain/production_editorial_enrichment.py:35-40). Le prompt construit le payload de conseil et sa version dans production_editorial_enrichment.py:5095-5318. | Versionner le prompt, la politique et le contrat/parser à chaque changement matériel. Garder les gates de preuve et les révisions. |
| Zéro média accepté | Le prompt dit explicitement « aucun quota minimal », « zéro table et zéro diagramme sont valides » et « zéro figure est valide » (production_editorial_enrichment.py:5153-5171, 5230-5236). | Remplacer la permission générale par des minima conditionnels R7 : 2 tableaux si ≥2 objets/champs comparables sont documentés ; 1 diagramme si ≥3 étapes documentées ; rester à zéro si les preuves ne justifient pas ces formes. |
| Figures modèle | MAX_ENRICHMENT_FIGURE_PROPOSALS = 3 ; le parseur refuse les propositions supplémentaires (production_editorial_enrichment.py:230-233, 2876-2893). | Distinguer les figures sources choisies des figures reconstruites. Revoir le plafond 3 si plusieurs images archivées pertinentes doivent être conservées ; le rendre configurable, borné et diagnostiqué. |
| Inventaire source | MAX_SOURCE_FIGURES=128 ; document source 25 MiB ; par figure 5 MiB ; total 50 MiB (source_figure_inventory.py:47-54). Une image sous 160×100 est rejetée ; dépassement de limites dimensions/octets est rejeté (source_figure_inventory.py:273-320, 874-875). | Garder les plafonds de sûreté, mais exposer les comptes acceptés/rejetés/tronqués et leur motif. Le seuil de 128 peut tronquer les corpus denses ; vérifier les rejets avant le prompt. |
| Figures en attente | Le prompt n’autorise que des handles acceptés et interdit les médias rejetés ou en attente d’archivage (production_editorial_enrichment.py:5227-5236). | Ne sélectionner qu’un blob déjà archivé et résolu ; ne jamais demander au modèle de télécharger l’image. Vérifier l’archive à partir du contenu et du contexte enregistrés. |
| Recherche externe | Le prompt demande d’ouvrir source_page_url, et ModelRequest active web_search lorsqu’une figure acceptée existe (production_editorial_enrichment.py:5230-5233, 5320-5323). | Risque de recherche live incompatible avec la limite R7 de sources déjà archivées. L4 doit fournir contexte/media archivé suffisant et désactiver la recherche live pour cette étape, sauf autorisation séparée. Aucun appel live n’a été fait pour L0. |
| Tableaux | Les tableaux sont éligibles seulement avec champs comparables, mappings ou plusieurs valeurs de consultation ; l’article peut actuellement en produire zéro (production_editorial_enrichment.py:5145-5169). Le parseur rejette une ligne unique (production_editorial_enrichment.py:2819-2822) ; le prompt de réparation demande deux lignes informatives (5385-5392). | Exiger au moins deux tableaux utiles quand les données permettent deux questions distinctes ; couvrir TTP/capacités/infrastructure/protocole et éviter la répétition IOC. |
| Répétition IOC | Les tableaux qui republient des IOC projetés peuvent être rejetés ; les représentations répondant à la même question analytique sont arbitrées (production_editorial_enrichment.py:2810-2847, 2833-2845). | Conserver cette protection. Une table IOC n’est admissible que si ses colonnes portent des attributs analytiques distincts et documentés. |
| Diagrammes | Prompt max 8 nœuds, 6 mots/≈40 caractères par nœud, 5 mots par arête ; types/profils et relations doivent être appuyés (production_editorial_enrichment.py:5184-5221, 5279-5284). Le modèle exige au moins deux nœuds et une arête hors profils de protocole (domain/production_editorial_enrichment.py:530-550). Pas de plafond de nombre de diagrammes trouvé dans les fichiers consultés. | Garder un seul diagramme par question/chaîne utile ; préserver l’exigence ≥3 étapes avant de déclencher infection_chain. Distinguer relation observée, inférence et comparaison. |
| Graphiques | Le prompt ne permet que CHART kind timeline ; date ISO exacte, libellé/série et evidence handle par point ; pas d’interpolation (production_editorial_enrichment.py:5011-5016, 5148-5152). | Garder cette restriction ; aucun nombre minimal/maximum de graphiques par article n’a été trouvé dans le compilateur. |
| Projection des plans | Les libellés de nœud/arête/titre/légende sont plafonnés à 56/64/72/480 caractères (figure_plan_projection.py:69, 98, 129-130, 274-285). | Ne pas raccourcir en supprimant le sens ou le fondement ; placer les détails en légende/caption. |
| Compiler | D2 porte la policy diagram-d2-svg-v8-chapsvision (diagram_compilation.py:12). analytic_chart_compilation.py définit un port de compilation, sans seuil de nombre visible dans son contrat. | Conserver le déterminisme local du rendu D2/chart et exposer les rejets de compilation dans les diagnostics d’enrichissement. L’exécution réelle du conteneur D2 n’a pas été vérifiée ici. |

Dans les fichiers consultés, aucun plafond de nombre explicite pour les tables,
diagrammes ou charts n’a été trouvé, hormis le plafond de trois propositions de
FIGURE et les limites de structure ci-dessus. La cause la plus directe d’un
ARTICLE sans tables/diagrammes est la règle de prompt « zéro valide », avec
les validations qui rejettent la redondance ou les preuves insuffisantes. Ne
pas interpréter cette lecture statique comme une mesure des trois sujets M17.

### 7.2 Politique opérationnelle de sélection proposée

- Lire l’inventaire source avec décisions acceptée/rejetée/en attente et
  diagnostic de troncature ; la sélection est limitée aux médias archivés.
- Retenir les captures, schémas et images réellement liées au contenu
  environnant ; exclure logo, navigation, chrome, doublons et images
  illisibles, mais enregistrer le motif dans le diagnostic.
- Pour chaque table/diagramme/chart, conserver PURPOSE, DATA, GAIN, SCOPE,
  PURPOSE_EVIDENCE, LIMITS et PLACEMENT ; chaque ligne/nœud/arête/point garde
  les handles d’évidence validés.
- Ajouter deux tableaux seulement si deux questions analytiques distinctes
  peuvent être traitées à partir de données vérifiées. Si une donnée manque,
  produire une rejection explicite plutôt que du contenu décoratif.
- Quand la synthèse montre au moins trois étapes canoniques documentées,
  proposer un diagramme d’infection ; sinon préférer un tableau de capacités,
  d’infrastructure ou de protocole si sa comparaison apporte une information.
- Distinguer figures sources, diagrammes reconstruits et graphiques dans le
  budget, les logs canoniques et le compte de parité.

## 8. Checklist de parité page-à-page et oracle R1–R9

Commandes de lecture seules à lancer sur un PDF fixture autorisé après les lots ;
elles ne sont pas exécutées pour L0 faute de PDF de référence et de PDF rendu :

    pdftotext -layout article.pdf /tmp/article-layout.txt
    pdftotext -bbox-layout article.pdf /tmp/article-bbox.html
    pdfinfo article.pdf
    pdffonts article.pdf
    pdftoppm -png -r 120 -f 1 -l 1 article.pdf /tmp/article-page

Pour chaque page, comparer visuellement l’équivalent CN/RU : en-tête, pied,
marges, titre, label/numéro, densité, césures, taille des légendes, coupures de
tableaux, espaces blancs, notes et lisibilité des images. Évaluer sur des
articles de densités différentes. Garder la page de référence et la page
nouvelle au même zoom ; ne pas faire passer une comparaison de texte pour une
validation visuelle.

| R | Oracle reproductible |
|---|---|
| R1 | Extraire le texte puis vérifier une puce devant chaque source ; aucune entrée du bloc Références ne commence par un numéro. Confirmer éditeur, titre, date et URL par source. |
| R2 | Comparer les dates exactes de l’artifact canonique avec les événements projetés. PDF : titre Chronologie et au moins deux dates seulement si deux événements valides ; aucune table Date/Événement héritée ; rendu visuel de la ligne/pastilles et ordre croissant. Fixture à 0/1 événement : aucun bloc vide. |
| R3 | Analyser pdftotext -bbox-layout : aucun chevauchement, ordre y croissant, gaps paragraphes/titres/figures/tables/références non nuls et dans l’échelle de style convenue. Comparer les mesures aux valeurs du §2. |
| R4 | Le texte extrait contient seulement les groupes non vides Adresses IP, URL, Noms de domaine, Fichiers ; chaque valeur reste défanguée et sans statut ; hash, nom et chemin de fichier sont regroupés sous Fichiers. Vérifier le mapping à partir de l’artifact, pas du texte seul. |
| R5 | Rechercher sans casse les motifs bannis ci-dessous dans le texte complet ; inspecter visuellement notes et captions pour les numéros parasites. Répéter pour article standalone et édition. |
| R6 | Aucun titre/table « ANNEXE TECHNIQUE — inventaire exhaustif des IOC » ; les tableaux exportés CSV/ZIP ne font pas partie du PDF. Vérifier que l’export canonique garde ses détails et provenances. |
| R7 | Sur JSON de rendu + artifact : compter tables, figures sources, diagrammes et charts ; ≥2 tables si les preuves supportent deux questions ; diagramme si ≥3 étapes documentées ; toutes les figures pertinentes acceptées/archivées sont présentes ; aucune figure en attente, rejetée ou non pertinente. Chaque item conserve ses preuves. |
| R8 | Comparer page par page avec pdftoppm ; contrôler Hanken Grotesk et styles monospace via pdffonts ; contrôler texte, formats, en-têtes/pieds, Figures n : et notes. Répéter sur ARTICLE autonome, édition avec ARTICLE et BRIEF. |
| R9 | Extraire uniquement le corps SYNTHÈSE ; compter les mots hors titres fixes, références, légendes et IOC. Comparer au plancher provisoire de 1 500 mots et aux ≥4 headings proposés au §6, seulement si les preuves les soutiennent ; vérifier un bloc de hunting et les domaines techniques réellement étayés ; refaire les contrôles de grounding et comparer les BRIEF sans changement de prompt. |

Recherche des motifs interdits dans le texte extrait :

    rg -ni 'PROVENANCE DE PUBLICATION|Indicateur[[:space:]]*:|Relation[[:space:]]*:|contextual indicator|indicateur[[:space:]]+contextuel|Candidat non confirmé|Pistes non confirmées|IOC de l.article|ANNEXE TECHNIQUE' /tmp/article-layout.txt

Résultat attendu : aucune ligne. Les notes de bas de page éditoriales restent
permises ; les index de bibliographie/IOC ne le sont pas. Compléter les oracles
PDF avec des tests unitaires hors ligne sur le JSON Typst et les invariants
canoniques ; aucun test, rendu PDF ni appel fournisseur n’a été exécuté au lot
L0.

## 9. Partage des fichiers et ordre d’intégration

Pas de chevauchement recommandé entre lots :

| Lot | Propriété principale | Interfaces à respecter |
|---|---|---|
| L2 — contrat/projection | backend/src/cti_app/domain/publication_document.py ; publication_builder.py ; publication_assembly.py ; typst_rendering.py ; edition_typst_rendering.py ; codecs et tests dédiés. | Publie V8/ArticleFormatV1 et le JSON Typst v33 ; garde V4–V7 et exports lisibles ; documente les identités/hash. |
| L1 — Typst | chpTypst/RENDERER/publication.typ, publication_helpers.typ, edition.typ, edition_helpers.typ, UTILS/document_style.typ, helpers.typ et colors.typ ; fixtures/tests Typst. | Consomme exactement le JSON v33 adopté par L2 ; garde les styles BRIEF compatibles. |
| L3 — synthèse ARTICLE | backend/src/cti_app/application/production_prompts.py ; production_synthesis.py ; tests synthesis. | Utilise seulement les champs overview et structures convenus avec L2 ; nouveau prompt/contrat/parser versionné ; BRIEF inchangé. |
| L4 — enrichissement | backend/src/cti_app/application/production_editorial_enrichment.py, source_figure_inventory.py, diagram_compilation.py, analytic_chart_compilation.py, figure_plan_projection.py ; domain/production_editorial_enrichment.py ; tests d’enrichissement. | Consomme les éléments canoniques de L2 ; ajuste prompts, politique, caps/décisions et diagnostics ; pas d’acquisition live. |

Ordre : L0 (ce document) → L2 (contrat JSON et versions) → L1, L3 et L4 en
parallèle sur des branches sans fichier partagé → L5 revue indépendante. Avant
le parallélisme, figer avec l’orchestrateur les noms/types de v33, la politique
timeline, et la gestion des e-mails. Fusionner d’abord L2 ; chaque lot parallèle
se rebase sur cette interface ; exécuter ensuite les tests hors ligne puis la
parité PDF.

## 10. Décisions et vérifications à reprendre

1. Réattacher/monter les deux PDF et sources CN/RU à leur emplacement attendu,
   puis calculer les statistiques par article ; les statistiques complètes,
   titres types et mesures page-à-page ne sont pas vérifiées dans L0.
2. Valider ou ajuster le plafond timeline de huit événements et son score de
   sélection.
3. Confirmer qu’un ARTICLE V8 enveloppe V7 avec article_format, et que le champ
   est absent/nul pour BRIEF.
4. Décider le traitement PDF des e-mails : R4 n’a pas de catégorie, alors que
   les artifacts historiques et exports peuvent les contenir. La recommandation
   de L0 est de les garder dans le canonique/export et de ne pas les imprimer
   comme une cinquième catégorie.
5. Ajuster la cible provisoire ≥1 500 mots aux mesures réelles CN/RU.
6. Vérifier en environnement de compilation autorisé que le moteur Typst utilise
   la police Hanken Grotesk et que D2 est disponible ; ces dépendances n’ont pas
   été lancées au lot L0.

État de vérification L0 : sources applicatives/Typst locales lues et citables ;
aucun code applicatif modifié ; aucun test, PDF de référence, rendu Typst ou
service live utilisé. Les faits non accessibles restent explicitement N/D ou à
confirmer.

## 11. Addendum orchestrateur — mesures sur les références (docs/references est gitignoré)

Mesures faites sur `docs/references/{CN2609_typ,RU2609_typ}/articles/*/*.typ` et pdftotext (CN 120 p., RU 114 p.).
Les comptes incluent code et légendes (mots ≈ `\w+` sur la source Typst, donc surestimés ; ordre de grandeur seulement).

| Article | Figures (`#figure`/`image(`) | styled-table | noteAnalyste | blocs raw | `#fn(` | h1 | h2 | h3 | ioc-list | mots src ≈ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CN article01 | 34 | 11 | 10 | 48 | 24 | 15 | 49 | 7 | 3 | 18 400 |
| RU article01 | 28 | 8 | 2 | 12 | 13 | 7 | 6 | 7 | 4 | 7 900 |
| RU article02 | 14 | 2 | 2 | 26 | 3 | 7 | 8 | 1 | 5 | 7 300 |

Calibrage : médiane ≈ 28 figures (hors doublons d'images), 8 tableaux, 7 h1 + 6–49 h2 ; les références sont des
analyses de reverse/hunting très longues (7–18 k mots). Le plancher « ≥ 1 500 mots » du plan reste un **minimum** ; la cible
visée par le prompt ARTICLE doit être nettement supérieure quand les preuves le permettent (2 500–5 000 mots, bornés par
l'évidence et le budget de sortie du modèle).

Titres h1 typiques (abstraits) : « Aperçu », « Reconnaissance », « Accès initial », « Persistance et accès aux identifiants »,
« Collection », « Exfiltration », « Infrastructure utilisée », « Attribution », « Chaîne d'exploits / de l'implant »,
« Campagnes observées », « Documents leurres et ciblage », « Diffusion et exécution », « Analyse de la porte dérobée »,
« Perfectionnement progressif », « Perspectives ». h2/h3 : sous-composants, pivots/requêtes de recherche, analyse des
fichiers déposés, variantes, commandes disponibles, réseau, synthèse.

Format observé dans le PDF RU (p. 4) : en-tête = logo Chaps Vision à gauche, « Bulletin n°4 / Septembre 2026 » à droite,
trait fin ; pied = tableau 3 colonnes « Bulletin-APT-RU2609 | Actualité des codes et infrastructures russes — n/N |
Bulletin n°4 / Septembre 2026 » ; texte justifié, mots-clés en gras (noms d'outils/acteurs), listes à puces compactes,
`#raw` inline grisé ; titres h1 vert-gris ~20 pt avec grand espace avant.
Bloc IOC final = titre « IOC » puis sous-titres **« Adresses IP : »**, **« Noms de domaine : »**, **« Fichiers : »** (et URL
si présentes) suivis de valeurs brutes une par ligne, sans table ni statut (vérifié p. 1205-1230 de `pdftotext`).
(Les « IOC originaux » en milieu de PDF viennent de nos propres publications, pas de CN/RU.)
