# M20 E3a — Anatomie des avis de référence et grille de qualité

## Portée et méthode

Cette note couvre uniquement la comparaison documentaire E3a. Références lues en lecture seule sous `/home/nill/perso/work/AutoWork/docs/references/`; aucun contenu ni binaire n’a été copié dans le dépôt. Les phrases citées ci-dessous sont les seules citations textuelles de la référence et restent sous 25 mots chacune.

**Base :** `main@6714b3592b02f23b78b095ce783d199b93ebd7c7`; branche `m20/e3a-reference-rubric`.

| Référence effectivement lue | Pages | SHA-256 |
|---|---:|---|
| `CN2609_typ/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf` | 120 | `1ea738682bf922449e28c30c885b9712af0b24a0314c3481bd41554ed67ce64e` |
| `RU2609_typ/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-RU-2609.pdf` | 114 | `996debd1882cb52129cb40a5f6e0c4d941625c0f7d84a9778db7efbd72ff92e5` |
| `livrable-IR2609/IR2609.pdf` | 32 | `bb82f3039d5b4bc23c49b2c03f4d6d18db4ea63fe23ac1a15c5efff2a9ba2e19` |
| `livrable-IR2609/typst-source/bulletin.pdf` | 47 | `b3bf3cec4cff9448055c29de022996cfefe2d6dfa392c6f20755b42ee6079ba3` |

**Divergence de référence vérifiée :** `livrable-IR2609/LISEZMOI.md` annonce 45 pages pour `IR2609.pdf`; le PDF présent en compte 32. Le PDF livré s’arrête après l’article 09; le PDF sous `typst-source/` ajoute une annexe IOC sur les pages 33–47. Le PDF source fait donc 47 pages, et non les 45 annoncées. Pour le récit analytique, les mesures ci-dessous portent sur `IR2609.pdf`; les inventaires IOC viennent du `manifest.json` et des CSV. La parité textuelle complète entre les deux PDF n’est pas affirmée.

Les blocs CN/RU ont été repérés par `#noteAnalyste[` dans les seuls dossiers `articles/` et `breves/`. Le comptage lexical est une mesure de source : extraction des délimiteurs Typst équilibrés, suppression des commandes `#...`, puis comptage des mots Unicode; les paragraphes sont les groupes séparés par une ligne vide, les titres les lignes Typst `==...`, les blocs de code les paires de clôtures triples. Ces nombres sont donc approximatifs et reproductibles, mais ne remplacent pas le comptage du texte PDF. Les mesures IR viennent des blocs typés de `typst-source/RENDERER/edition-render-data.json`; les contrôles PDF ont utilisé `pdfinfo`, `pdftotext -layout` et `pdftoppm` hors ligne. Les pages 12–14 du PDF IR et une page de note CN et RU ont été inspectées visuellement.

## A. Anatomie des références

### A.1 `noteAnalyste` des bulletins CN/RU

**Vérifié.** Les sources CN comportent 9 appels et les PDF 9 encadrés; les sources RU comportent 4 appels et les PDF 4 encadrés. Aucun appel CN n’est situé dans `breves/`; RU en a deux dans les articles et deux dans les brèves. Les pages imprimées CN sont 8, 17, 21, 22, 25, 27, 31, 41 et 45; RU : 18, 34, 68 et 83.

La séquence ci-dessous résume les titres et sujets dans leur ordre d’apparition sans recopier les intitulés ni les éléments CTI de référence. `H` désigne les sous-titres Typst, `P` les paragraphes, `fn` les appels à la note de bas de page, `fig` les figures/images et `code` les blocs clôturés.

| Famille / bloc source | Séquence et structure observées | Mots approx. | P | H | fn | fig / table / code |
|---|---|---:|---:|---:|---:|---:|
| CN article, l.132–160 | Validation OSINT → récupération → comparaison de versions → différentiel de scripts | 604 | 11 | 4 | 1 | 0 / 0 / 0 |
| CN article, l.359–398 | Infrastructure → indicateurs internes → métadonnées de soumission | 591 | 13 | 3 | 0 | 0 / 0 / 2 |
| CN article, l.467–474 | Note courte sans sous-titre | 198 | 3 | 0 | 0 | 0 / 0 / 0 |
| CN article, l.496–513 | Recherche d’adresses → fichiers déposés → persistance planifiée | 383 | 8 | 3 | 0 | 0 / 0 / 0 |
| CN article, l.539–561 | Pivot sur stockage cloud → recherche dans le code → persistance | 347 | 9 | 3 | 0 | 0 / 0 / 1 |
| CN article, l.577–596 | Observations d’échantillons → pivots | 334 | 9 | 2 | 0 | 0 / 0 / 0 |
| CN article, l.671–678 | Note courte sans sous-titre | 174 | 3 | 0 | 0 | 0 / 0 / 0 |
| CN article, l.906–992 | Bloc analytique continu; 3 références et extraits de code | 1 070 | 15 | 0 | 3 | 0 / 0 / 5 |
| CN article, l.1002–1184 | Chronologie → méthode et limites → pivots d’infrastructure et de redirection → synthèse | 2 697 | 57 | 20 | 0 | 4 / 0 / 0 |
| RU article 01, l.403–490 | Marqueur et vulnérabilité → emails/IP → signatures externes → moteurs d’infrastructure → expéditeurs/contenu | 1 794 | 34 | 9 | 4 | 1 / 0 / 1 |
| RU article 02, l.195–436 | Fichiers déposés → variantes et threat hunting → récupération/pivots → LNK → réseau → synthèse | 2 544 | 67 | 9 | 2 | 1 / 0 / 6 |
| RU brève 01, l.305–311 | Note de deux paragraphes sans sous-titre | 50 | 2 | 0 | 0 | 0 / 0 / 0 |
| RU brève 02, l.235–301 | Note technique sans sous-titre Typst; extraits et figures au fil du texte | 1 123 | 19 | 0 | 4 | 2 / 0 / 4 |

| Agrégat | Blocs | Mots approx. | Paragraphes | Sous-titres | `fn` | Figures | Blocs de code |
|---|---:|---:|---:|---:|---:|---:|---:|
| CN articles | 9 | 6 398 | 128 | 35 | 4 | 4 | 8 |
| RU articles | 2 | 4 338 | 101 | 18 | 6 | 2 | 7 |
| RU brèves | 2 | 1 173 | 21 | 0 | 4 | 2 | 4 |
| Total `noteAnalyste` | 13 | 11 909 | 250 | 53 | 14 | 8 | 19 |

Les références d’article sont donc hétérogènes : la note la plus courte fait 50 mots/2 paragraphes; les deux notes les plus développées font 2 544 mots/67 paragraphes et 2 697 mots/57 paragraphes. Le nombre de sous-titres va de 0 à 20. Une « note équivalente » ne se juge pas par un seul seuil de longueur.

**Profondeur technique vérifiée.** Dans l’article CN01 et ses encadrés, le récit couvre la rétro-ingénierie d’exploit/chargeur, les variantes de code, le fonctionnement post-compromission, la configuration et le C2 d’extension, puis les pivots d’infrastructure et la chronologie. Les blocs RU privilégient les pivots documentés, les variantes, les fichiers déposés et les observations réseau; le bloc RU de brève prouve qu’un format bref peut accueillir extraits, figures et notes, mais ce n’est pas la taille typique des autres blocs. Dans l’article CN, le niveau d’attribution est expressément limité : « candidat probable » (PDF CN, p. 8).

**Figures, tableaux et code vérifiés.** Les encadrés CN/RU sont composés comme blocs cassables à fond gris, avec titre `purple-dark` extrabold de 14 pt (`CN2609_typ/UTILS/helpers.typ:106–123`; RU `:125–142`). Les exemples visuels inspectés montrent les chaînes de code en monospace, des données techniques en surbrillance grise et des valeurs de requête en bloc sombre. Les notes emploient des sous-titres, listes, extraits de code et figures; les notes courtes n’ont pas toutes ces éléments. Les fonctions `fn(url)` rendent les références en notes de bas de page (`CN2609_typ/UTILS/helpers.typ:286–290`; RU `:311–315`).

**Conventions typographiques vérifiées et limites.** Les gabarits CN/RU appliquent `luma(190)` en fond gris et Cascadia Mono à 10 pt aux éléments Typst `raw` en ligne (`main.typ:32–47`); les requêtes VirusTotal ont un fond noir et du monospace blanc (`UTILS/helpers.typ:296` CN; RU `:338`). Les termes anglais sont fréquemment balisés en italique par `_..._` dans le contenu source. Le titre de chaque encadré est en gras. Le balisage CN/RU des notes n’expose toutefois pas un rôle `semantic-tool` distinct : la mise en gras de noms d’outils n’est pas une règle typée uniforme de ces deux sources.

### A.2 Équivalent analytique dans IR2609

**Vérifié.** Le document IR n’emploie pas le type de bloc `noteAnalyste` : son modèle de rendu contient des blocs `paragraph`, `section_heading`, `figure`, `diagram` et `table`. Les articles constituent l’équivalent le plus proche. Le tableau donne leur ordre dans le bulletin, le nombre de paragraphes analytiques et les objets typés; les sujets sont abrégés.

| Ordre / sujet abrégé | Mots de paragraphes | P | H | Figures | Diagrammes | Tables | Références de source |
|---|---:|---:|---:|---:|---:|---:|---:|
| 01 — routage C2/Bitcoin | 724 | 8 | 7 | 1 | 1 | 0 | 2 |
| 02 — faux recrutements et charges utiles | 1 309 | 10 | 8 | 0 | 1 | 2 | 5 |
| 03 — surveillance via Telegram / HEAVYGRAM | 2 288 | 22 | 9 | 1 | 1 | 1 | 4 |
| 04 — profilage OSINT et obfuscation | 550 | 5 | 0 | 0 | 1 | 0 | 3 |
| 05 — continuité d’une campagne contre des PLC | 832 | 8 | 0 | 0 | 0 | 1 | 3 |
| 06 — reconnaissance navale | 363 | 6 | 1 | 0 | 0 | 1 | 2 |
| 07 — outils de surveillance | 470 | 6 | 0 | 0 | 0 | 1 | 2 |
| 08 — usurpation du support Teams | 1 339 | 9 | 6 | 0 | 1 | 0 | 10 |
| 09 — malwares et hameçonnage | 1 241 | 11 | 6 | 0 | 1 | 1 | 4 |
| **Total** | **9 116** | **85** | **37** | **2** | **6** | **7** | **35** |

Ordre des sections de l’IR, résumé sans reprendre les titres textuels : 01 données de courtage → stockage/résolution C2 → résilience → limites d’attribution; 02 faux recrutement → démarrage/persistance → commandes/C2 → détection → limites d’attribution; 03 leurres → livraison/persistance → collecte → C2/exfiltration → détection → limites d’attribution; 04 profilage puis obfuscation; 05 analyse sans sous-titre; 06 un sous-titre; 07 analyse sans sous-titre; 08 ciblage → exécution/persistance → C2 → détection → attribution; 09 leurres → implant → M365 → réseau → détection → corroboration/attribution.

L’article 03 fournit l’exemple de protocole le plus utile à E3 : une figure distingue deux chaînes de livraison et précise qu’une branche n’implique pas nécessairement l’installation persistante; une autre figure schématise l’exécution, l’API bot Telegram et le transfert de fichiers. Le texte donne l’ordre des échanges, le heartbeat et cinq commandes dans un tableau. Les pages 12–14 ont été rendues et vérifiées visuellement (`pdftoppm`); les légendes identifient la source du schéma.

La configuration est documentée dans certains articles. L’article 03 identifie les fichiers de configuration et la décryption à l’exécution, sans tableau visible de clé extraite ou d’offset de bytes dans le rendu narratif. **Inférence bornée :** l’équivalence des valeurs de configuration/clé au niveau byte ne peut pas être conclue à partir de ce PDF; l’archive contient d’autres sources, mais aucune analyse exhaustive de tous les échantillons n’a été faite dans E3a.

**Détections et attribution.** Le PDF IR ne contient aucune occurrence YARA dans le texte extrait et indique explicitement : « Aucune règle Suricata n’est fournie pour cette édition. » (p. 2). Aucun rapport de compilation ou de test corpus d’une règle locale n’a été repéré dans le PDF ou les blocs typés; il ne faut donc pas évaluer les références comme si elles établissaient une règle testée. Les sections de limites et de corroboration apparaissent dans plusieurs articles; aucun score numérique de confiance n’a été repéré dans le texte extrait ou les champs structurés. L’attribution reste formulée dans le récit et bornée par des limites documentées.

Le manifeste IR compte 9 publications, 35 références de source et 256 IOC : 50 domaines, 167 empreintes de fichiers, 14 IP et 25 URL. Le CSV normalisé contient 256 lignes avec les mêmes comptes par type; `is_new` distingue 23 indicateurs originaux de 233 indicateurs de source. Une adresse de courriel est exclue avec le motif `unsupported_type_by_ioc_reporter`. L’appendice du PDF Typst réunit les inventaires; les CSV/manifestes constituent l’oracle de cardinalité, pas le seul texte du bulletin.

**Typst IR vérifié.** `typst-source/UTILS/helpers.typ:33–45` définit des rôles sémantiques : `semantic-tool` applique le gras (22 occurrences dans les données), `semantic-english-term` l’italique (56), et les rôles techniques appliquent une fonte/couleur dédiée (180 spans `semantic-technical`, 96 `semantic-technical-literal`, plus les rôles de table). Le gris d’arrière-plan des raw en ligne est une convention CN/RU; il n’est pas uniforme dans IR, où les rôles techniques sont principalement monochromes colorés/monospace. Les références sont regroupées par article plutôt qu’en `#fn` dans chaque paragraphe d’avis.

### A.3 Ce qui est vérifié sur AutoWork par lecture du code

Le service compose les blocs à partir des findings et rattache leurs observations (`backend/src/cti_app/application/analyst_opinion_service.py:682–760, 861–886`). `OpinionBlock` stocke un `text` et des références, sans spans typés d’outils, d’anglais ou de sous-sections (`backend/src/cti_app/domain/analyst_opinion.py:297–307`). V6 conserve l’avis et sa section rendue, et vérifie l’égalité du texte, des qualifications et des références; V7 enveloppe le document V6 avec le routage (`backend/src/cti_app/domain/publication_document.py:2032–2041, 2146–2167, 2239–2261`). Ces gardes sont compatibles avec un correctif de rendu dérivé qui ne modifie pas le texte source.

Dans le rendu d’avis, `_opinion_text_spans` ne reconnaît que le code inline délimité par backticks, les SHA-256 et UUID (`backend/src/cti_app/application/typst_rendering.py:1475–1524`). Le gabarit rend les spans techniques en monospace de 8 pt, sans fond gris; les spans ordinaires sont du texte simple (`chpTypst/RENDERER/publication_helpers.typ:538–568`). Le gabarit montre un titre de section, un titre par bloc, un label de qualification, puis les références en petit texte « Références » (`:1469–1502`). Il ne crée pas le panneau gris `noteAnalyste` des références.

**Non vérifié dans ce lot :** aucun aperçu PDF d’avis AutoWork n’a été produit; aucun rendu Typst AutoWork, test ou comparaison visuelle à contenu identique n’a été exécuté. La qualité CTI réelle, l’exhaustivité d’un avis donné et l’équivalence substantielle ne sont donc pas établies ici. Le présent audit conclut sur des faits de source/rendu et des écarts de format seulement.

## B. Grille de comparaison mesurable

Comparer un aperçu brouillon au document de référence portant sur le même sujet, avec la même portée et les mêmes artefacts. Les seuils ci-dessous sont des critères d’équivalence auditables, pas l’affirmation que les références les satisfont toutes : si une référence n’a pas de règle, de clé ou de figure, l’avis AutoWork doit indiquer l’absence au lieu d’inventer un artefact.

| Critère | Définition et métrique observable | Seuil « équivalent à la référence » | Méthode de mesure sur un aperçu brouillon |
|---|---|---|---|
| Profondeur technique | Matrice des dimensions pertinentes : rétro-ingénierie; configuration/clé; ordre et champs C2; limitations; pivots; détection. Compter dimensions traitées et faits techniques par dimension. | Toutes les dimensions traitées par la référence comparable sont couvertes ou explicitement marquées inconnues; chaque fait technique est rattaché à une preuve. Une section plus longue ne compense pas une dimension absente. | Checklist article/sections sur `render-data.json` + PDF extrait par `pdftotext -layout`; lier chaque claim à ses `finding_refs`/`evidence_refs`. |
| Exhaustivité artefacts/IOC | Comptes par type, ensembles `(type, valeur)` comparés, doublons, exclusions motivées, hash et provenance. Pour IR2609, référence globale : 50 domaines, 167 fichiers, 14 IP, 25 URL. | 100 % de l’inventaire attendu pour le sujet figure dans l’avis ou dans son annexe; écart inexpliqué = 0; les exclusions conservent un motif et une source. Ne pas comparer un sujet à tout l’inventaire du bulletin. | Parser `manifest.json` et `iocs_normalized.csv`, puis comparer aux IOC de l’aperçu; produire uniquement les deltas de compte/type et les valeurs manquantes dans le rapport de QA. |
| Figures et protocole | Compter figures/tables et vérifier acteurs, nœuds, arêtes, ordre, champs de message, légendes, source et hash d’asset. | Tous les éléments sémantiques nécessaires de la figure de référence sont conservés; chaque arête/champ vient d’une observation ou est marqué hypothétique; aucun décor ne doit inventer une interaction. Lisibilité contrôlée sur le PDF. | Vérifier métadonnées de figure et provenance, extraire le texte, rendre les pages concernées avec `pdftoppm`; revue visuelle des légendes, flèches et taille des champs. |
| Détections YARA/Suricata testées | Nombre par type et état : candidate, compilée, testée, validée; compiler/version, hashes du corpus, positifs/négatifs, TP/FP/FN et rapport. | Aucun statut « testé/validé » sans compilation et test hors ligne reproductible; au moins un positif et un négatif identifiés par hash, métriques et limites affichées. Si la référence ne donne pas de règle (cas Suricata IR2609), afficher « aucune règle fournie » plutôt que fabriquer une équivalence. | YARA : `yarac` puis `yara` sur fixtures positives/négatives; Suricata : `suricata -T` puis replay PCAP hors ligne; vérifier que le rapport de test canonique et ses hashes sont liés à l’avis. |
| Traçabilité et preuve | Ratio des claims factuels avec références résolubles, source/hash/page/offset, observation et limitations. | 100 % des faits techniques et d’attribution ont une référence résoluble; 0 observation brute convertie silencieusement en conclusion; conflits et lacunes visibles. | Dénombrer claims puis joindre les `finding_refs` et `evidence_refs` de l’aperçu aux observations de l’enquête; vérifier échantillon par échantillon les pages/hash source. |
| Attribution nuancée/confiance | Pour chaque claim d’acteur/campagne : portée et fenêtre temporelle, faits vs inférences, soutien/contradiction, alternatives, label de confiance et raisons. | Chaque attribution est explicitement qualifiée et bornée; les alternatives/contradictions disponibles sont visibles; aucun acteur n’est présenté comme certain au-delà des preuves. Aucun score numérique ne doit être inventé par conversion arbitraire. | Tableau de claims, avec colonnes observation, inférence, élément contraire, alternative, confiance et citation; revue humaine contre les passages de source. |
| Lisibilité/langue | Comptes mots, paragraphes, titres, longueur des phrases, fautes bloquantes, renvois et termes techniques mal formatés. | Même classe de document et portée; longueur dans la plage observée de cette famille ou écart expliqué. 0 faute bloquante et 0 référence cassée après correction éditoriale. | `pdftotext -layout` puis script Python de compte mots/paragraphes/phrases; relecture française et contrôle des renvois sur le PDF rendu. |
| Conventions Typst | Ratio de spans connus correctement stylés : outils en gras; raw technique en monospace/surlignage du gabarit choisi; anglais/noms propres en italique quand le gabarit l’exige; références en note ou renvoi résoluble. | 100 % des spans explicitement typés suivent le gabarit choisi. Vérifier la sortie PDF; le gris CN/RU et les couleurs sémantiques IR sont deux profils distincts, à ne pas fusionner implicitement. | Compter les rôles/span dans le modèle de rendu; inspecter les macros Typst; rendre les pages avec `pdftoppm` et cocher visuellement chaque catégorie. |
| Longueur/structure | Mots, paragraphes, ordre et nombre de titres; distinguer avis bref, note d’article et article complet. | Dans la plage de la famille comparable : note CN observée 174–2 697 mots; note RU d’article 1 794–2 544; note de brève RU 50–1 123. L’ordre des sections requises et une synthèse/limites ne doivent pas être sacrifiés au nombre de mots. | Parser les délimiteurs/titres Typst ou compter les blocs de l’aperçu; comparer aux plages de l’inventaire A.1 et aux sections pertinentes d’A.2. |

**Application dans E3a :** la grille n’a pas été exécutée sur une preview AutoWork. Le code établit que l’avis relie déjà certains blocs à des findings/observations et distingue Fait/Inférence/Hypothèse; il n’établit pas que le contenu d’un brouillon particulier égale les exemples de référence.

## Écarts AutoWork de format/rendu vérifiés et candidats E3c

Gravité : **Moyenne** = risque de masquer une distinction technique, de confiance ou de preuve; **Faible** = différence de présentation sans perte de contenu source vérifiée. Les correctifs proposés sont limités au rendu/projection : ils ne réécrivent ni n’approuvent les avis canoniques. Un nouveau PDF pourrait toutefois avoir des octets différents après correctif, sans changement des avis persistés.

| ID / gravité | Écart vérifié et preuve code | Correctif borné et sûr proposé | Estimation |
|---|---|---|---:|
| F1 — Faible | La note de référence a un panneau gris et un libellé accentué; le rendu AutoWork utilise le titre générique de section et aucun panneau autour du bloc (`publication_helpers.typ:1469–1493`). | Ajouter un conteneur `breakable` de style avis avec fond léger et titre, sans modifier le texte, la qualification ou les références. | 1 fichier, 1–2 h |
| F2 — Moyenne | Les spans techniques AutoWork ont du monospace 8 pt mais pas le surlignage gris de CN/RU (`typst_rendering.py:1488–1524`; `publication_helpers.typ:562–565`). Les références CN/RU montrent raw gris 10 pt (`CN2609_typ/main.typ:32–47`). | Appliquer au seul span technique d’avis le fond gris et la taille du profil choisi; conserver la fonte monospace et le contenu exact. | 1 fichier, 1–2 h |
| F3 — Moyenne | `OpinionBlock` ne stocke que le texte et les références; la projection ne classe que raw inline, SHA-256 et UUID. Outils, noms propres et anglais restent dans les spans simples (`analyst_opinion.py:297–307`; `typst_rendering.py:1488–1524`; `publication_helpers.typ:560–565`). | Ajouter des annotations sémantiques de rendu explicitement validées (outil, terme anglais, technique) et les styliser. Ne pas déduire automatiquement un nom propre ou un outil à partir d’une majuscule; garder le texte source inchangé. | 2–3 fichiers, 4–8 h |
| F4 — Moyenne | Le parseur ne traite que le code inline; les blocs multi-lignes restent dans le texte ordinaire. Les notes de référence contiennent 19 blocs de code mesurés dans les notes examinées (`typst_rendering.py:1483–1524`; `publication_helpers.typ:538–568`). | Reconnaître uniquement des fences triples équilibrées dans le rendu dérivé, les afficher comme code échappé en monospace/fond discret, et conserver les octets comme texte inerte. Tests ciblés sur fence malformée et caractères Typst. | 2 fichiers + 1 test, 3–5 h |
| F5 — Faible | AutoWork met les références en ligne à la fin du bloc; les CN/RU emploient `#fn(url)` en note (`publication_helpers.typ:1495–1500`; helpers CN `:286–290`). | Option de rendu en marqueurs superscript/notes numérotées, tout en conservant les IDs de findings/preuves cliquables et la résolution actuelle. | 2 fichiers, 2–4 h |
| F6 — Moyenne | `certainty` arrive dans la projection mais est ignoré (`typst_rendering.py:3568–3593`); seul le libellé Fait/Inférence/Hypothèse est affiché (`publication_helpers.typ:1486–1493`). | Après définition d’une calibration éditoriale, afficher une catégorie de confiance dérivée de `certainty` avec son libellé; ne pas afficher le décimal brut ni inventer de seuil sans décision produit. | 2 fichiers, 2–4 h, calibration requise |

### Proposition de lot E3c

1. **Lot visuel borné : F1 + F2**, avec rendu Typst ciblé et test de preview : 1–2 fichiers de rendu, 2–4 h.
2. **Lisibilité technique : F4 + F5**, seulement après test que les références et caractères arbitraires restent du texte inerte : 2 fichiers de rendu + tests ciblés, 5–9 h.
3. **À cadrer avant implémentation : F3 + F6.** Il faut d’abord définir qui annote les spans et quelle calibration éditoriale donne un sens à `certainty`; ne pas appliquer de heuristique aux avis existants.

## Vérifié / inféré / non vérifié

**Vérifié par lecture ou comptage local :** le nombre et la structure des blocs `noteAnalyste`, les styles décrits par les macros, les comptes structurés IR, les 9 articles et les nombres d’IOC du manifeste; le schéma/code V6/V7 et le chemin Typst AutoWork cités ci-dessus. Les pages PDF indiquées ont été extraites ou rendues hors ligne.

**Inféré :** les six propositions E3c et leurs charges en heures/fichiers; le classement de gravité; l’usage des corps d’articles IR comme meilleur équivalent à une note d’analyste explicite. Ces estimations supposent que le lot garde les avis persistés et leurs hashes intacts.

**Non vérifié / prochaine étape permise :** aucun avis brouillon AutoWork sur une enquête existante n’a été généré ni comparé; aucun test, aucun rebuild complet du bulletin, aucune validation des références externes du texte, aucun test de règle YARA/Suricata n’a été exécuté. Le prochain lot peut choisir un aperçu autorisé et appliquer la grille sans publier, repinner ni modifier un avis existant.
