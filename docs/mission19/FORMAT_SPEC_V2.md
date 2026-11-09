# Mission 19 — spécification de format V2 (lot M0)

## Portée et statut des constats

Ce document prépare les lots M1 (Typst), M2 (projection et contrat), M3 (synthèse, annotations et D1) et M4 (brèves B1/B2). Le plan normatif est docs/mission19/M19_ARTICLE_BREVE_FORMAT_PLAN.md, exigences A1–A7, B1–B5, C1 et D1. Il prévaut sur docs/mission18/FORMAT_SPEC.md lorsque les deux divergent.

Les références examinées sont les sources Typst et PDFs fournis sous /home/nill/perso/work/AutoWork/docs/references/{RU2609_typ,CN2609_typ}. Aucun passage éditorial chinois ou russe n’est reproduit ici. Les noms de fichiers d’articles et de brèves ne servent qu’à localiser les règles de mise en forme. Les observations de code ci-dessous ont été vérifiées dans le worktree M19-M0. Les recommandations sont identifiées comme telles et les limites sont explicites.

La revue M18 (/home/nill/perso/work/AutoWork/.autowork-runs/m18/REVIEW.md) rapporte un état antérieur — PublicationDocumentV8, ArticleFormatV1, prompt ARTICLE v15 et modèle Typst v33 — et demande une revalidation des références. Ce rapport de run n’est pas une source de preuves CTI.

## 1. Format de référence observé

### 1.1 Gras des outils, noms et termes

Dans chaque source d’article ou de brève, la liste locale bold-words est concaténée à la liste locale bold-regex, puis une expression régulière est construite avec une frontière de mot de part et d’autre. Le motif correspondant est rendu en gras. La sélection est donc éditoriale et explicite par document, complétée par quelques formes génériques régulières; ce n’est pas une détection automatique de toutes les entités. Le motif et le style sont dans les sources d’article/brève, par exemple CN2609_typ/articles/article01/article01.typ:3-27 et CN2609_typ/breves/breve01/breves01.typ:3-18. Le rendu et les structures article/brève sont dans CN2609_typ/UTILS/helpers.typ:167-235.

La règle à reprendre abstraitement est : gras pour les acteurs, campagnes, malwares, outils et logiciels lorsqu’ils sont des entités qui doivent être repérées à la lecture; maintenir une liste explicite et des motifs limités, avec frontières, plutôt qu’un gras lexical global. Ne pas importer les listes éditoriales de ces références dans AutoWork.

### 1.2 Code inline et blocs raw

Les deux main.typ de référence appliquent à raw inline un surlignage gris luma(190), soit environ #BEBEBE, avec rayon 2 pt; texte à 10 pt et police Cascadia Mono, fixée par la règle raw globale. La règle inline active ne spécifie pas d’inset, d’outset ou de padding. Le code en bloc est distinct : fond noir, texte blanc, padding horizontal 2 %, inset x=10 pt/y=1 pt, outset vertical 5 pt, rayon 2 pt. Référence CN2609_typ/main.typ:24-47; même règle dans RU2609_typ/main.typ:22-45. Les commentaires de règles anciennes qui précèdent le bloc inline ne sont pas actifs.

pdffonts confirme dans les deux PDFs Hanken Grotesk régulier, gras, extra-gras, italique, gras italique et semi-gras, ainsi que Cascadia Mono; Libertinus et Liberation Mono sont également incorporées, et CN inclut des polices CJK. Le gabarit de production a déjà l’équivalent du rendu raw inline dans chpTypst/UTILS/document_style.typ:72-89 : surlignage inline luma(190), rayon 2 pt, taille 10 pt et police code; le bloc de production a pour sa part taille 8,8 pt et largeur/padding définis. Les nouveaux fonds gris de A5 portent sur les valeurs techniques annotées, pas sur le code raw lui-même.

### 1.3 Italique des mots anglais et des noms propres

Les références utilisent une mise en emphase sélective dans la source éditoriale; le helper ne contient ni détecteur de langue ni règle générale pour mettre en italique tout mot anglais ou tout nom propre. Un exemple de balisage manuel se trouve dans CN2609_typ/articles/article01/article01.typ:44. Les légendes de figure, elles, sont systématiquement en italique par règle de style (main.typ:60-63).

Règle abstraite recommandée pour A5/B3 : annotations sémantiques distinctes pour les termes anglais insérés dans la prose et pour les noms propres, en conservant les noms exacts. Éviter l’italique sur chaque chaîne technique, nom de produit, citation ou littéral de code. Si un nom est déjà une entité fortement mise en évidence par le gras, arbitrer le chevauchement de styles de manière déterministe.

### 1.4 Notes de bas de page et références

Les helpers exposent #fn(url), qui crée une note contenant le lien cliquable via better-link; le lien est bleu #0563C1 et souligné. Voir CN2609_typ/UTILS/helpers.typ:38-50, 286-290; RU2609_typ/UTILS/helpers.typ:38-50, 311-315. Les marqueurs de note sont des chiffres arabes en exposant; leur texte est regular 12 pt, sur fond gris translucide (gray.transparentize(20%)) avec extent 1 pt. L’entrée de note a un retrait nul. Voir CN2609_typ/main.typ:52-57 et RU2609_typ/main.typ:50-55. Dans les PDFs, l’URL de la note est visible en pied de page dans la zone de notes, en lien bleu. L’examen du helper ne montre pas de section bibliographique Références séparée dans les articles/brèves de référence : #fn(url) affiche l’URL comme note.

Cela diffère de A1 : la sortie AutoWork doit placer d’abord une section Références dont chaque entrée est une phrase source courte, puis rendre l’URL liée en note. Ne pas confondre le modèle de référence footnote-only avec le contrat M19.

### 1.5 Mesure de l’espacement inter-paragraphes

Mesure faite sur les deux PDFs avec pdftotext -bbox-layout. Pour chaque page, seules les paires de blocs texte voisins dans la zone de contenu verticale (y environ 100–730 pt) ont été retenues si chaque bloc avait au moins deux lignes, une largeur d’au moins 250 pt et un recouvrement horizontal d’au moins 75 %. L’écart est calculé comme yMin du bloc suivant moins yMax du précédent. Pour limiter les faux positifs dus aux titres, légendes et changements de colonnes, seuls les écarts de 8–22 pt sont gardés. C’est un écart d’encre bbox, pas la valeur Typst par(spacing).

- RU : 172 paires retenues, médiane 15,33 pt, plage 12,43–20,48 pt.
- CN : 151 paires retenues, médiane 15,33 pt, plage 12,42–21,59 pt.
- Pages 5–25 : RU n=27, médiane 15,33 pt; CN n=37, médiane 15,33 pt.

Le modèle commun article() utilise par(spacing: 2em), soit un espacement nominal de 22 pt à 11 pt, dans CN2609_typ/UTILS/helpers.typ:231-235 et l’équivalent RU. L’écart mesuré sur le PDF est inférieur à cette valeur de réglage car il mesure les boîtes d’encre. Recommandation de vérification M1 : garder la méthode bbox et une médiane de corps proche de 15,3 pt (tolérance initiale ±2 pt), avec contrôle visuel sur un échantillon contenant ARTICLE et BRIEF. Les paires mesurées n’ont pas été séparées de manière fiable par type de publication; ne pas présenter les médianes communes comme des médianes BRIEF isolées.

### 1.6 Figures et légendes

Dans main.typ de référence, le supplément de figure est Figure, le séparateur est « : », la légende est en 9 pt italique. Le PDF affiche les légendes centrées : par exemple page 5 des deux PDFs, le centre de la bbox de glyphes est à moins de 0,1 pt du centre horizontal de la page. Les figures examinées n’ont que leur légende sous l’image; aucun bloc « Documenté, non observé », « Source : » ou « Attribution : » n’y apparaît. Le constat visuel porte sur les exemples inspectés, tandis que la règle de suppression AutoWork doit s’appliquer à tout rendu concerné.

### 1.7 Frise, événements et brèves dans les références

Le helper timeline(events) rend chaque événement sous forme de puce, date violette en gras extra-bold à 11 pt, texte et, si fourni, URL en note. CN2609_typ/UTILS/helpers.typ:52-81; RU2609_typ/UTILS/helpers.typ:52-81. La fonction article() émet le titre Chronologie avant d’itérer les événements, même quand le tuple est vide; aucune règle de seuil à deux événements n’est présente (CN2609_typ/UTILS/helpers.typ:215-218, 177-178). Cette présentation est une liste événementielle verticale, pas le rail frise avec points que le rendu AutoWork ARTICLE utilise déjà.

Les brèves appellent le même helper article() en passant la catégorie Brève. Leur structure observée est donc : titre, étiquette de catégorie et numéro, Chronologie, Vue d’ensemble si elle est fournie, Synthèse et corps. Chaque fichier de brève fournit son propre bold-words/bold-regex; la puce chronologique, le raw gris, et la typographie du helper sont communs. La mise en forme manuelle des italiques reste sélective; aucun traitement automatique des mots anglais n’a été trouvé. Exemples structurels : CN2609_typ/breves/breve01/breves01.typ:1-27 et CN2609_typ/UTILS/helpers.typ:167-235. Le rendu AutoWork doit convertir B4 en frise conforme à C1 et appliquer B5.

### 1.8 IOC dans les références

Les catégories affichées sont adresses IP, noms de domaine, URL et fichiers; les valeurs sont en police monospace. Les helpers affichent la valeur fournie telle quelle et ne la dé-fangent pas. CN2609_typ/UTILS/helpers.typ:292-350, 413-465; RU2609_typ/UTILS/helpers.typ:317-393, 460 et suivantes. Dans les listes IOC étiquetées des PDFs inspectés, les valeurs de la catégorie fichiers étaient toutes des chaînes hexadécimales de longueur hash courante (MD5/SHA-1/SHA-256/SHA-512); les valeurs URL observées étaient des URL http[s] lisibles, et aucun motif hxxp/[.] n’a été trouvé dans ces listes. La portée de ce constat est les listes IOC imprimées, pas chaque mention de protocole dans la prose.

Règle A3 : la catégorie Fichiers du rendu doit contenir les hashes; les noms de fichiers et chemins restent disponibles dans les preuves et exports canoniques. A4 doit dé-fanger les IOC affichés dans les sections et le code inline selon le format demandé, sans réécrire les valeurs canoniques.

## 2. Diagnostic du code par exigence

### A1 — Références en tête, date et note courte

Faits vérifiés :

- production_prompts.py:179-184 impose pour SOURCE NOTES une note de une à deux phrases en français, maximum 260 caractères, sans date; la date de sortie est ajoutée par le renderer. Le prompt ne garantit pas à lui seul que la note sera visible.
- publication_builder.py:1048-1097 associe les notes de synthèse aux sources et fabrique une note déterministe si elle manque. publication_builder.py:880-909 conserve PublicationSourceV1.published_at à partir de la source canonique.
- typst_rendering.py:1841-1869 projette pour une référence text, title, publisher, display_date/date et URL. Le helper non numéroté publication_helpers.typ:318-390 choisit le titre si présent; la note text ne s’affiche que lorsque le titre manque.
- publication_helpers.typ:1241-1288 rend la liste, et publication_helpers.typ:1569-1579 parcourt les sections Références après le corps principal.

Écart : la liste actuelle est en fin d’article, sa ligne standard est éditeur — titre — date — URL, et la note SOURCE NOTES est souvent masquée par le titre. Elle n’est pas la phrase de référence cliquable demandée, avec date publiée en français et URL passée à #fn(url).

Changement recommandé : projection dédiée composée de la date française de published_at et du texte SOURCE NOTES, suivie de #fn(url); ignorer la date si absente plutôt que l’inventer; section Références avant le corps. Conserver les URL canonique et note pour provenance.

### A2/B5 — Espacement

Faits vérifiés : le rendu actuel ajoute un bloc de paragraphe avec paragraph-gap (publication_helpers.typ:1207-1218). Le corps BRIEF passe 9 pt (publication_helpers.typ:1329-1335); le corps ARTICLE passe 7 pt (1341-1348). Les références utilisent list.spacing 4 pt (1241-1288). Les sources de référence règlent 2em et le PDF donne la mesure de section 1.5.

Changement recommandé : régler ARTICLE et BRIEF sur une séparation de corps visuellement équivalente à la référence; garder le réglage distinct des listes, légendes et titres. Mesurer le PDF produit avec la même définition bbox, plutôt que comparer les nombres Typst directement.

### A3 — Fichiers IOC

Faits vérifiés : publication_builder.py:632-641 mappe HASH, FILENAME et FILEPATH vers la catégorie files. build_article_format_v1 conserve value, normalized_value, les sources et les références de preuve (publication_builder.py:650-808; types publication_document.py:2423-2455). Le helper de rendu des IOC affiche les valeurs fournies.

Écart : la projection actuelle classe donc les noms et chemins comme Fichiers, malgré l’exigence de hashes uniquement en sortie. Ces noms/chemins ne doivent pas être supprimés de l’extraction ou des exports.

Changement recommandé : filtrer la seule projection de catégorie affichée à ArtifactType.HASH. Garder les artefacts canoniques FILENAME/FILEPATH, leurs hashes de preuve et les exports; vérifier que l’identité/hachage des sources reste inchangé.

### A4 — Dé-fangage des IOC et du code inline

Fait vérifié : typst_rendering.py:1901-1918 appelle display_publication_indicator_value avec defanged=True pour les IOC rendus, ce qui produit actuellement les IOC dé-fangés. La projection narrative et ses spans sémantiques sont sérialisés dans typst_rendering.py:1505-1553 et 1751-1805; elle ne restaure pas les motifs de dé-fangage dans les chaînes de code inline. Les helpers des références affichent les valeurs d’IOC directement.

Écart : la sortie visuelle est orientée dé-fangage à la fois pour les IOC où ce chemin s’applique et le code inline n’est pas restauré. A4 demande la représentation refangée dans la publication.

Changement recommandé : faire la transformation exclusivement dans la couche d’affichage pour les IOC et les spans inline éligibles, avec un test d’aller-retour garantissant que texte canonique, normalized_value, hashes, annotations et exports restent octet-pour-octet inchangés.

### A5/B3 — Rôles sémantiques et styles absents

Faits vérifiés :

- Le prompt d’annotation et ses versions sont dans production_prompts.py:466-520. La liste de rôles comprend actor, campaign, malware, tool, product, english_term, technical, technical_literal, ioc, path, command, protocol_field, source et proof. Il n’y a pas de rôle général proper_name.
- L’enum et le mapping vers les fonctions Typst sont dans domain/semantic_annotation.py:44-105. L’annotation fournit donc déjà les rôles tool, malware et product pour le gras, et english_term pour l’italique.
- L’enrichissement fabrique les propositions d’annotation en appel séparé et a un tour de réparation borné (production_editorial_enrichment.py:7415-7520). Le contrat/parseur fil est dans production_editorial_enrichment.py:1308-1426.
- La projection sémantique par ancre, texte et spans est dans typst_rendering.py:1505-1553; les paragraphes, événements, références et blocs sont projetés autour de 1751-1869. Le même mécanisme typé nourrit les publications ARTICLE et BRIEF; la construction du document BRIEF ajoute aussi les annotations déterministes de base (brief_publication_builder.py:212-227).
- Les fonctions Typst actuelles sont dans chpTypst/UTILS/helpers.typ:38-76. tool/malware/product sont gras; english_term est italique; technical a une taille/couleur mais pas de fond gris. technical_literal, IOC, path, command et protocol_field sont monospace/couleur, sans surlignage gris de fond. Le style ne représente pas un nom propre distinct.

Écart : la sémantique existante couvre une grande partie du gras et des termes anglais, mais ni le rôle proper_name, ni le fond gris uniforme requis pour les valeurs techniques; un terme anglais et un nom propre ne sont pas équivalents.

Changement recommandé : ajouter proper_name et rendre le rôle technical sur fond gris, préserver les rôles de littéraux/IOC/chemins/commandes pour leurs contraintes mono, définir la priorité sur les plages qui se chevauchent et le rendu des cas composés. Mettre à jour prompt, contrat, parseur, schéma/politique d’annotation et helpers. Réutiliser la même API pour ARTICLE et BRIEF; ne pas introduire un chemin parallèle sans version propre.

### A6 — Notes internes sous les figures

Faits vérifiés : les données de figure sont projetées avec classification, source_note et attribution dans typst_rendering.py:3340-3377. publication_helpers.typ:1138-1158 imprime ensuite la phrase statique « Documenté, non observé — … », la ligne Source et la ligne Attribution.

Écart et changement : retirer ces trois annotations du rendu public sous les figures; garder les champs canoniques et diagnostics pour la provenance, QA et audit. Vérifier qu’aucune autre branche de figure ne les réintroduit.

### A7 — Légendes

Fait vérifié : publication_helpers.typ:886-905 rend Figure X : titre en gris, italique, 9 pt et explicitement aligné au centre. C’est déjà conforme au centre demandé. La référence PDF montre aussi un centrage mesurable.

Changement recommandé : conserver ce contrat et ajouter un test bbox qui échoue si une prochaine variante décentre la légende; ne pas ajouter un second séparateur.

### B1 — Recherche de références et extraction sur une BRIEF

Faits vérifiés sur le chemin partagé :

- ProductionStage déclare REFERENCES, EXTRACTION et RELEVANCE_PROJECTION dans cet ordre avant SYNTHESIS (domain/production.py:34-40); le dispatch appelle les handlers correspondants sans branche BRIEF (production_workflow.py:918-929).
- REFERENCES prépare le contexte du snapshot, vérifie la politique d’accès externe, et fait un appel WEB_RESEARCH avec web_search=True pour une entrée fraîche non réutilisée; les propositions sont persistées comme corpus de références (production_workflow.py:1199-1371). Un artefact déjà compatible peut être réutilisé avant l’appel.
- EXTRACTION consomme le corpus archivé et ne contacte pas le réseau (production_workflow.py:1469-1506; production_extraction.py:768-852). build_extraction_plan exige au moins une source CORE admissible, omet les sources archivées inéligibles avec diagnostics et construit les unités en fonction du niveau, du type, du rôle éditorial et des intentions supplémentaires (production_extraction.py:453-523). Une source proposée par un utilisateur ne devient pas CORE par ce seul fait (domain/production_references.py:261-277).
- production_references.py:197-280 assemble un corpus canonique additif : les sources CORE du snapshot restent prioritaires, les propositions sont ajoutées, puis chaque URL reçoit son observation d’archivage; son parseur lit les blocs SOURCE à :477-540.
- Le routage ARTICLE/BRIEF intervient ensuite à partir des artefacts d’extraction et de pertinence (production_routing.py:52-90, 145-194; domain/production_routing.py:426-517). Il n’existe donc pas de raccourci de route BRIEF avant REFERENCES/EXTRACTION dans ce flux.

Conclusion vérifiée : la pipeline générique exécute déjà la recherche REFERENCES et l’EXTRACTION avant le choix du type de publication. Il faut ajouter un test de contrat BRIEF sur ce fait et réparer un point de sortie trouvé par test; ne pas créer par défaut une deuxième pipeline dédiée.

Coût estimatif Bridge/modèle, non mesuré : pour chaque input BRIEF frais qui ne peut pas réutiliser un résultat compatible, REFERENCES fait une recherche modèle, donc jusqu’à 1 appel WEB_RESEARCH par BRIEF. EXTRACTION fait les appels nécessaires aux unités de plan admissibles, selon sources archivées, formats, éventuels découpages et lots IOC; le nombre n’est pas déductible sans le corpus. Si tout est réutilisé, l’incrément peut être nul. Formule pour N briefs frais : N appels de recherche au maximum, plus le nombre d’unités effectives du plan d’extraction; pas un appel fixe par publication.

S16 « Bogus Bylines » (subject 543a95cd…, publication d6e603d9… v2) : le lien openai.com fourni par le ticket n’est pas une preuve suffisante de son état dans les artefacts canoniques. Un GET en lecture seule via /tmp/aw vers l’API de production locale a échoué faute de permission au socket Docker. L’état des artefacts S16, l’existence de cette URL comme proposition de référence, et la raison précise de son absence de la synthèse ne sont donc pas vérifiés.

Hypothèses seulement : l’URL peut manquer du corpus de références; être archivée mais omise par une règle d’éligibilité d’extraction ou une unité non planifiée; ou être présente mais ne pas être retenue/citée par la synthèse. Le code ne prouve aucune de ces possibilités pour S16.

Périmètre recommandé : tester/générer une correction sur S16 seulement, après GET de ses artefacts quand l’API locale redevient accessible, puis vérifier le résultat et les causes d’omission. Le comportement général references→archive→extraction doit être verrouillé pour toutes les routes par un test hors ligne. N’étendre la relecture des 15 brèves que si l’audit S16 met en évidence une lacune commune ou si chaque artefact montre le même défaut. Cette limite évite de payer 15 recherches neuves et un nombre variable d’extractions sans preuve qu’elles sont nécessaires; les artefacts déjà conformes doivent être réutilisés.

### B2 — Enrichissement éditorial BRIEF

Faits vérifiés :

- Le stage editorial_enrichment est bien appelé dans le dispatch général et reçoit extraction, relevance projection, synthesis et routing decision (production_workflow.py:929, 1976-2039).
- Le service inspecte un inventaire de figures déjà archivées, dérive son input hash de l’inventaire, et réutilise un enrichissement à identité exacte si possible (production_editorial_enrichment.py:6773-6878). La recherche externe de ressources est configurée désactivée par défaut; elle est en plus conditionnée par access_policy.external_llm_allowed (production_editorial_enrichment.py:7090-7143).
- Le maximum BRIEF est trois propositions de figures, utilisé dans les branches publication_kind non-ARTICLE (production_editorial_enrichment.py:242-243, 5051-5059, 5388-5445). Trois propositions est un plafond de sorties figures, pas trois appels de modèle.
- Le service peut faire un appel initial de rédaction, un appel de réparation si des blocs sont rejetés, puis un appel distinct d’annotation sémantique et, si nécessaire, sa réparation (production_editorial_enrichment.py:6914-6923, 7010-7025, 7317-7324, 7415-7520). Les resource proposals sont conditionnelles et leur recherche n’est pas activée par défaut.
- Lacune ferme : l’assemblage charge l’artefact d’enrichissement seulement pour ARTICLE et impose la condition de présence seulement pour ARTICLE (production_workflow.py:2112-2132). PublicationAssemblyService refuse ensuite tout enrichissement en BRIEF (publication_assembly.py:170-190), et le builder indique expressément qu’il ne crée ni ne requiert cet enrichissement (brief_publication_builder.py:60-68). Les sorties BRIEF sont donc inutilisées même si le stage les a produites.

Design minimal recommandé : laisser les figures/captures venir uniquement de l’inventaire archivé validé; autoriser l’assemblage BRIEF à consommer un artefact d’enrichissement valide et optionnel; projeter tables, diagrammes, charts et figures avec leur placement et leurs références de preuve, selon le schéma de publication existant. Maintenir le plafond de trois figures et une production ou une inclusion seulement quand les éléments archivés soutiennent la présentation. Conserver un chemin vide valide quand il n’y a aucun élément fiable. Garder la recherche de ressources désactivée, pas d’acquisition, pas de média sans provenance ni ajout d’analyse non étayé. Inclure l’input hash et l’ID canonique de l’artefact dans l’identité d’assemblage s’il est consommé.

Coût estimatif si une BRIEF est réenrichie et que l’artefact n’est pas réutilisable : normalement jusqu’à une rédaction et une proposition d’annotations si des ancres existent; ajouter au plus un appel de réparation pour les blocs et un pour les annotations rejetés. Recherche web optionnelle : zéro dans la configuration par défaut. Lire execution.model_calls comme compteur effectif; le plafond de figures ne donne pas le nombre d’appels. Pour 15 brèves fraîches, ordre de grandeur initial jusqu’à environ 30 appels (rédaction + annotation quand les deux s’appliquent) avant réparations; ce n’est pas une mesure ni un devis, et l’état de l’inventaire/reuse peut le réduire.

### B3 — Annotations des brèves

Le service d’enrichissement sait proposer des annotations par spans pour les deux types de publication, mais l’assemblage actuel rejette l’enrichissement BRIEF : ces propositions ne figurent donc pas dans le document BRIEF final. brief_publication_builder applique seulement l’annotation déterministe de base fondée sur extraction. Les rôles manquants identifiés sous A5 le sont également pour BRIEF. B2 doit rendre les propositions disponibles, puis B3 doit les appliquer uniquement aux ancres réellement rendues, avec contrôle exact du texte source; ne pas annoter une figure ou une légende depuis un modèle non ancré.

### B4 — Chronologie BRIEF en frise

Fait vérifié : brief_publication_builder.py:175-206 projette les événements datés d’extraction dans la chronologie. Le rendu BRIEF de publication_helpers.typ:1355-1373 émet le titre Chronologie puis le helper de liste publication, avec dates en violet et notes URL; ce n’est pas le rail article-publication de 77-101. La sélection ArticleFormat C1 ne s’applique pas à l’ArticleFormat du BRIEF, qui est null par contrat.

Changement recommandé : adapter le modèle/rendu partagé de la frise pour accepter un ensemble événementiel BRIEF vérifié et appliquer le seuil de deux distincts après ajout éventuel de la date de publication. Garder les dates d’événements canoniques et leurs références; ne pas forcer ArticleFormatV1 dans un BRIEF juste pour partager le dessin Typst.

### C1 — Date de publication dans la frise

Faits vérifiés : ArticleFormatV1 contient les TimelineKeyEvent avec date exacte, texte et références de preuve non vides (publication_document.py:2402-2415). select_article_timeline_key_events_v1 déduplique et ordonne les événements prouvés, rend vide sous deux et plafonne à huit (2519-2574). PublicationDocumentV8 vérifie la sélection canonique (2724-2754). La projection reçoit déjà published_at sur chaque source (publication_builder.py:880-909), et PublicationEvidenceRef accepte SOURCE_METADATA avec une clé SHA-256 (publication.py:152-165).

Changement recommandé : créer un seul événement « publication de la source » pour chaque source dont published_at et preuve SOURCE_METADATA canoniques sont disponibles; ne pas créer d’événement sans date ni ref SHA vérifiable. Faire le test de seuil après cet ajout : événement de publication seul reste caché; deux dates distinctes ou plus font apparaître la frise. Dédupliquer les dates/événements identiques, limiter l’événement synthétique sans remplacer le contenu d’événements CTI. Appliquer le même seuil à la frise BRIEF.

### D1 — Suppression du plancher 1500 mots et des quatre sections

États actuels exhaustivement localisés pour les règles identifiées par D1 :

1. Prompt principal ARTICLE : production_prompts.py:19-21 définit SYNTHESIS_PROMPT_VERSION v15 et le contrat ARTICLE v1; le prompt ARTICLE à :64-85 exige Vue d’ensemble, trois labels puis au moins quatre autres headings, conseille 2500–5000 mots et dit que 1500 est le minimum. Retirer le nombre minimum et le seuil de mots; maintenir Vue d’ensemble, les trois labels ordonnés et les contraintes de grounding. Le texte d’orientation ne doit plus faire pression pour combler une longueur insuffisamment prouvée.
2. Validateur : production_synthesis.py:147-165 définit ARTICLE_SYNTHESIS_MIN_WORDS=1500, MIN_FOLLOW_ON_SECTIONS=4 et l’ensemble de headings requis. validate_article_synthesis_outline et _article_structure_violation_reasons sont à :2811-2911. Supprimer word_floor, too_few_sections et required_heading_coverage comme échecs; maintenir présence et exactitude de Vue d’ensemble, les trois labels/valeurs, l’absence de section vide ou de titre blanc si une section facultative existe, dates/provenance/grounding et validation plain-text.
3. Tour correctif de synthèse : production_synthesis.py:1653-1743 construit plusieurs instructions article_structure demandant de restaurer au moins quatre sections et 1500 mots. Les chemins article et structure utilisent aussi ARTICLE_SYNTHESIS_CORRECTIVE_PROMPT_VERSION à :139-142, 1788-1798. Enlever les injonctions aux seuils; une réparation d’un overview invalide garde les 3 labels, et une correction de claim/plain-text garde les garanties actuelles.
4. Révision ciblée L3b : production_prompts.py:26-32 a ARTICLE_SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION v5 et contract v7. production_synthesis_revision.py:140-142 utilise validator v7; :245-258 sélectionne explicitement ces versions pour ARTICLE. Le prompt de révision :3287-3304 exige encore quatre sections. La validation réapplique validate_article_synthesis_outline à :1875-1887. Modifier la règle, le validateur et le texte de révision pour permettre un ARTICLE court sans affecter les règles BRIEF.
5. Tests qui codent le plancher : backend/tests/test_production_synthesis_application.py:1833-1930 couvre overview, sections, headings requis, word_floor et grounding; 5140 couvre le tour correctif; 7097-7150 vérifie overview et rejette une révision ayant moins de quatre sections. Remplacer les assertions de minimum par acceptation de proposition ARTICLE courte (overview seul ou overview et sections utiles), et conserver les refus pour overview invalide, section vide, mauvais label, absence d’ancrage et date non prouvée.
6. Test de longueur des versions : backend/tests/test_prompt_version_lengths.py:4-17 documente les colonnes varchar(64) et inspecte les versions de prompts/enrichment domain. L’étendre aux constantes ARTICLE ciblées et validateurs détenus par les modules application; toute nouvelle chaîne doit rester ≤64.

Versions D1 recommandées, distinctes de BRIEF :

| Contrat/validateur | Valeur actuelle vérifiée | Nouvelle version recommandée |
|---|---|---|
| production_prompts.SYNTHESIS_PROMPT_VERSION | synthesis-draft-v15-article-date-grounding | synthesis-draft-v16-article-evidence-length |
| production_prompts.ARTICLE_SYNTHESIS_PROPOSAL_CONTRACT_VERSION | synthesis-article-structured-v1-overview-hunting | synthesis-article-structured-v2-overview-evidence |
| production_synthesis.SYNTHESIS_VALIDATOR_VERSION | synthesis-validator-v9-article-structure | synthesis-validator-v10-article-overview |
| production_synthesis.ARTICLE_SYNTHESIS_CORRECTIVE_PROMPT_VERSION | production-synthesis-corrective-v3-multi-violation | production-synthesis-corrective-v4-overview |
| production_prompts.ARTICLE_SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION | synthesis-targeted-revision-v5-article-structure | synthesis-targeted-revision-v6-article-overview |
| production_prompts.ARTICLE_SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION | synthesis-targeted-patch-v7-article-structure | synthesis-targeted-patch-v8-article-overview |
| production_synthesis_revision.ARTICLE_TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION | targeted-synthesis-patch-validator-v7-article-structure | targeted-synthesis-patch-validator-v8-article-overview |

Les valeurs proposées sont sous 64 caractères; le test version-length est l’oracle. Ne pas incrémenter le prompt, contrat ou validateur BRIEF pour la seule suppression D1.

D1 ne modifie pas la forme sérialisée de la synthèse ou du document : pas de version Typst ni de schéma PublicationDocument à incrémenter pour D1 seul. Pour le M19 complet, les versions de sortie à coordonner sont :

- chpTypst/renderer-manifest.json : chp-article-v38-article-format → v39 format M19.
- chpTypst/edition-renderer-manifest.json : chp-edition-v30-article-format → v31 format M19.
- typst_rendering._RENDER_DATA_SCHEMA_VERSION : typst-publication-model-v33-article-format → v34 format M19.
- publication_builder.ARTICLE_FORMAT_POLICY_VERSION : 1-deterministic-overview-timeline-ioc-categories → v2 incluant date source et seuls hashes dans files; ASSEMBLY_POLICY_VERSION : 16-publication-v8-article-format → v17 M19.
- brief_publication_builder.BRIEF_ASSEMBLY_POLICY_VERSION : v1-sourced-timeline-no-ioc-annex → v2 frise/enrichissement BRIEF.

Une date de publication ajoutée comme TimelineKeyEvent réutilise event_date, text et evidence_refs; le filtrage du rendu Fichiers ne change pas le type canonique; l’ordre de Références et le refangage sont des transformations de projection. Ils n’exigent donc pas PUBLICATION_DOCUMENT_V8_SCHEMA_VERSION 8→9 si le schéma reste ainsi. S’il faut ajouter un champ sérialisé à PublicationDocumentV8, produire plutôt une V9 additive, conserver un lecteur V8 et ses hashes historiques, et tester explicitement la conversion; ne pas changer en place le sens des payloads V8.

Autres versions touchées par le format complet :

- Le schéma d’annotation sémantique passe de 1 à 2 si proper_name est ajouté à l’enum sérialisé; technical garde son rôle et reçoit un nouveau style. Politique SEMANTIC_ANNOTATION_POLICY_VERSION v5→v6, prompt v2→v3, contract v2→v3 et wire parser v3→v4. Les constantes actuelles sont domain/semantic_annotation.py:11-13 et production_prompts.py:466-468.
- Si le prompt d’enrichissement devient explicitement BRIEF et ajoute le choix de figures/tables/diagrammes ancré, incrémenter EDITORIAL_ENRICHMENT_GENERATOR_VERSION v10→v11 et EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION v8→v9 (production_editorial_enrichment.py:202-243); le schéma d’enrichissement ne change pas si les sorties restent les mêmes types.
- Ne pas changer la version extraction/références pour un changement seulement de rendu. Si M4 modifie réellement la politique de sélection d’unités, versionner cette politique séparément et seulement après avoir établi la différence.

## 3. Répartition des lots et vérification

### M1 — Typst et PDF

Fichiers propriétaires : chpTypst/UTILS/document_style.typ; chpTypst/RENDERER/publication_helpers.typ; chpTypst/renderer-manifest.json; chpTypst/edition-renderer-manifest.json. M1 ne modifie pas chpTypst/UTILS/helpers.typ, réservé à M3 pour les nouveaux styles sémantiques. La nouvelle vue frise doit être un helper générique appelé pour ARTICLE et BRIEF; aucune logique de seuil métier ne doit être dupliquée dans Typst.

Tests existants à adapter : backend/tests/test_typst_rendering.py, test_typst_article_format_runtime.py, test_edition_typst_rendering.py et test_pdf_inspection.py.

Nouveaux tests recommandés : compilation de PDF fixture ARTICLE et BRIEF avec deux paragraphes, raw inline, note URL, légende et deux événements; pdftotext vérifie titre/références/date, absence des trois notes internes et ordre des sections; pdftotext -bbox-layout vérifie la médiane de séparation des paragraphes et le centre de légende; pdffonts vérifie polices attendues (Hanken Grotesk pour la prose, Cascadia Mono pour le code) et styles bold/italic du texte fixture. Le test bbox doit échouer si l’écart de paragraphe repasse sous la fenêtre de parité; l’oracle doit utiliser les mêmes filtres décrits en 1.5. Ne pas imposer des sous-fontes d’italique/bold qui varient selon la version Typst sans vérifier l’environnement de compilation.

### M2 — Contrat et projection publication

Fichiers propriétaires : backend/src/cti_app/domain/publication_document.py; backend/src/cti_app/application/publication_builder.py; backend/src/cti_app/application/typst_rendering.py. M2 possède dans typst_rendering.py la projection des références, IOC, timelines et figure data. M2 doit produire le contrat que M1 rend, y compris frise, refs d’abord et formes d’IOC restaurées.

Tests existants à adapter : backend/tests/test_publication_assembly.py; test_publication_document_v7.py; test_typst_rendering.py; test_typst_article_format_runtime.py; test_publication_brief_builder.py; test_publication_qa_v6.py.

Nouveaux tests recommandés : événement synthétique de date publiée avec ref SOURCE_METADATA exacte; cache sous deux événements puis affichage à deux dates distinctes; seules les ArtifactType.HASH dans la projection files mais les noms/chemins toujours conservés côté canonique/export; matrices d’IOC refangées dans Typst alors que les mêmes bytes canoniques et hashes ne changent pas; Références avant synthèse, note ≤260 caractères et date localisée sans URL inline; A6 ne supprime pas les métadonnées canoniques.

### M3 — Synthèse, annotations et D1

Fichiers propriétaires : backend/src/cti_app/application/production_prompts.py; production_synthesis.py; production_synthesis_revision.py; backend/src/cti_app/domain/semantic_annotation.py; chpTypst/UTILS/helpers.typ. M3 met à jour le prompt d’annotation et ses rôles; les spans sont déjà projetés par M2 sans changement de son interface. M3 ne modifie pas typst_rendering.py.

Tests existants à adapter : backend/tests/test_production_synthesis_application.py; test_production_synthesis_domain.py; test_semantic_annotation.py; test_semantic_annotation_l13.py; test_prompt_version_lengths.py.

Nouveaux tests recommandés : un tool/malware/product en gras; un nom propre en italique; un mot anglais en italique; une valeur technique grisée; chevauchements de spans déterministes; rôle inconnu rejeté; résumé ARTICLE court valide avec Vue d’ensemble intacte; tour correctif et révision ciblée ne recréent ni sections ni longueur artificielles; toutes nouvelles versions ≤64. Garder les tests de grounding/date/plain-text.

### M4 — Recherche, extraction et enrichissement BRIEF

Fichiers propriétaires requis pour rendre l’enrichissement consommable : backend/src/cti_app/application/production_workflow.py (charger/passer l’artefact à l’assemblage BRIEF); publication_assembly.py (admettre et hasher l’enrichissement BRIEF); brief_publication_builder.py (projection fondée et optionnelle); publication_qa.py (contrat QA identique à l’assemblage). production_editorial_enrichment.py ne doit être modifié que si une règle de génération BRIEF manquante est confirmée; le plafond 3, lecture d’inventaire archivé, refus par policy et recherche ressource off sont déjà en place. production_workflow.py et publication_assembly.py doivent inclure l’artefact enrichi dans l’identité de réutilisation seulement quand il est réellement consommé.

Les étapes REFERENCES et EXTRACTION sont déjà partagées; ce lot doit verrouiller ce comportement par tests. Ne modifier production_references.py ou production_extraction.py que si une preuve déterministe isole une omission BRIEF. Ne pas changer la politique d’archive ou d’accès externe pour forcer l’URL de S16.

Tests existants à adapter : backend/tests/test_publication_brief_builder.py; test_publication_assembly.py; test_publication_qa_v6.py; test_production_routing.py; test_production_extraction_service.py; test_production_editorial_enrichment_application.py; test_production_editorial_enrichment_workflow.py.

Nouveaux tests recommandés : route BRIEF avec references puis extraction avant routage; URL proposée présente/absente dans artifact de référence avec diagnostic d’éligibilité d’extraction; assemblage BRIEF consomme une figure d’inventaire archivé et une table/diagramme validé avec leurs refs; enrichissement absent/vidé reste valide; trois propositions figure au maximum; access policy refusée n’entraîne pas d’appel; l’identité d’assemblage change si un artefact consommé change; QA compare à la projection canonique enrichie.

### Chevauchements et ordre des lots

M1 et M2 ont des fichiers disjoints. M3 ne touche pas typst_rendering.py et M4 ne touche pas les helpers Typst; les modifications sémantiques restent dans helpers.typ/domain/prompt et la sortie commune reste stable.

M4 peut vouloir réutiliser des projecteurs communs de publication_builder.py (propriété M2). Si cela devient nécessaire, terminer M2 avant M4 ou donner au projecteur commun un patch M2 puis l’appeler depuis M4; ne maintenir pas deux implémentations divergentes. L’ordre d’intégration recommandé est M2 contract → M1 rendu du contrat → M3 rôles et D1 → M4 branche BRIEF enrichie → revue indépendante. Les tâches sans dépendance de fichier peuvent être développées en parallèle après accord sur la forme du JSON.

## 4. Invariants, hachages et réutilisation

1. Pour les articles existants, les hashes canoniques REFERENCES, EXTRACTION et RELEVANCE_PROJECTION doivent rester identiques. Aucun changement de format ne justifie leur recalcul ou leur mutation.
2. Ne pas modifier l’identité d’une source pour adapter sa forme affichée. published_at, URL, content SHA, note SOURCE NOTES et evidence refs gardent leur provenance.
3. Le refangage et la sélection des hashes Fichiers sont des projections de lecture. Ils ne changent ni IOC canoniques, normalized_value, noms/chemins, evidence refs, exports ni blobs.
4. La date ajoutée à la frise doit être liée à une source/date de publication archivée et à son EvidenceRef SOURCE_METADATA à clé SHA-256; pas de date déduite d’un titre, URL ou heure d’acquisition.
5. B1 sur S16 peut créer de nouveaux artefacts REFERENCES/EXTRACTION seulement après inspection et contrôle d’identité/réutilisation. Cela ne s’applique pas aux 14 autres brèves sans cause démontrée.
6. Un artefact d’enrichissement BRIEF n’entre dans le hash d’assemblage que s’il est validé et réellement projeté. Ses images, charts, tables et diagrams gardent les mêmes identifiants, références et origine archivée.
7. Un bump de prompt/contrat D1 entraîne une nouvelle identité de SYNTHESIS ARTICLE et des artefacts dépendants en aval; il ne doit pas invalider les références, extractions ou projections de pertinence.
8. Les manifestes Typst et versions de policy doivent refléter tout changement de PDF pour empêcher la réutilisation d’un PDF rendu avec une ancienne template.

## 5. Limites ouvertes

- La présence et l’aptitude de la page openai.com fournie dans les artefacts canoniques de S16 n’ont pas pu être confirmées : endpoint local inaccessible par le wrapper GET. Aucun appel externe, acquisition ou mutation n’a été effectué.
- Le nombre exact d’appels modèle et le coût monétaire des 15 brèves dépendent du corpus archivé, du plan d’extraction, du cache/reuse, des refus et réparations; ils ne sont pas mesurés dans cette spec.
- Les mesures bbox sont des métriques d’encre agrégées selon le filtre écrit en 1.5, et non une extraction du paramètre Typst. La séparation par sous-type ARTICLE/BRIEF est à rendre explicite dans le futur test PDF.
- La génération Typst et une nouvelle comparaison de PDF produit n’ont pas été exécutées pour cette spec.
