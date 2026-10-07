# Audit de la production courante (édition 5defd15d, 9 sujets) — défauts et plan de reprise

Statut : inventaire établi le 2026-10-07 après la phase de revue ; **aucun re-run lancé**.
Références de comparaison : `docs/references/20250331_NP_TLP WHITE_ChapsVision_Bulletin-APT-RU-2503.docx`
et `docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf`.

Légende de coût de reprise : **R0** rebuild de publication seul (aucun appel modèle) · **R1** reprise depuis
`editorial_enrichment` (≈2-3 appels/sujet) · **R2** reprise depuis `synthesis` (≈4 appels/sujet, ≈10-15 min) ·
**S** nécessite d'abord une action sur les sources (archivage manuel / source complémentaire) · **D** décision produit.

## Écart global avec les bulletins de référence

Les bulletins de référence structurent chaque article ainsi : **Chronologie** (puces datées des événements) →
**Vue d'ensemble** (victimologie, arsenal, objectif) → **Synthèse** découpée en sous-sections titrées →
figures/captures et blocs de code commentés → **Note de l'analyste** → fichiers/IOC en annexe. Le bulletin CN fait
120 pages pour 1 article de fond et 7 brèves ; le bulletin RU est un article d'analyse technique approfondie.
Production courante : 255 à 1 341 mots par article, texte continu sans titres, 0 à 2 figures.

| ID | Constat (preuve) | Sujets | Cause | Reprise |
| --- | --- | --- | --- | --- |
| G1 | Pas de bloc « Vue d'ensemble » (victimologie / arsenal / objectif) | 1-9 | absent du contrat de publication | R2 + contrat |
| G2 | Chronologie non publiée : la timeline est calculée (1 entrée sur le sujet 1) mais `publication_builder.py` l'appelle avec `include_timeline=False` (ligne ~796) ; 0 entrée publiée partout | 1-9 | choix de code, jamais rouvert | R0 si l'on la publie ; sinon D |
| G3 | Synthèse en texte continu, sans sous-titres (consigne de prompt « no section titles ») | 1-9 | décision de conception | D (R2) |
| G4 | Peu de figures/captures annotées et aucun bloc de code (0-2 figures) | 1-9 | sélection de figures sources limitée, pas de recherche de ressources (`resource-search-off`) | D |

## Sources et références

| ID | Constat | Sujets | Reprise |
| --- | --- | --- | --- |
| S1 | **Référence en double** : la même page est listée deux fois (`…september-2026` et `…?page=1`), deux lignes datées quasi identiques. C3 ne mutualise que l'extraction, pas les références ni la page publiée | 4, 6, 7, 9 | R0 (fusion à la publication) + canonicalisation de l'URL en Discovery/References |
| S2 | **Sources officielles inaccessibles (HTTP 403)**, arbitrées `continue_without_source` : CISA AA26-097A + STIX JSON/XML + 2 FBI (sujet 5, dont l'avis **qui donne son nom au sujet**), FBI (sujet 3), IranWire ×2 (sujets 7, 9), Reuters (6), krypt3ia (4). Perte d'information réelle masquée par l'arbitrage | 3, 4, 5, 6, 7, 9 | S (archivage manuel via `archive_manual_content`, ou collecteur navigateur) puis R2 |
| S3 | **Source de support abandonnée en silence** : NCSC (contre-analyse) du sujet 5, `extraction_source_output_invalid` / `q2_no_payload` ; son point de vue n'apparaît pas | 5 | enquête extraction ; R1-R2 |
| S4 | Source « core » = blog relais (Cyber Advisors) plutôt que le document officiel : l'article cite le relais, le titre porte `[Cyber Advisors]` | 5 | S |
| S5 | **Source trop mince, jamais complétée** : la section GTG-30004 du rapport tient en 4 courts paragraphes et `NanoDump` n'y figure qu'**une fois** ; l'article (255 mots, 1 page) est fidèle mais ne peut pas expliquer l'outil. Même cause pour « Arman » (sujet 7) | 4, 7 | S (sources complémentaires publiques : dépôt de l'outil, ATT&CK, IranWire) puis R2 |

## Titres

| ID | Constat | Sujets | Reprise |
| --- | --- | --- | --- |
| T1 | 6 titres du modèle sur 9 rejetés (préfixe `[Groupe]` omis) → repli avec **l'éditeur** comme groupe (`[Anthropic]`, `[Kaspersky]`, `[Daylight]`, `[Cyber Advisors]`) alors que la référence met l'**acteur** (`[APT31]`) ; verbe capitalisé collé (`[Anthropic] GTG-30004 Automatise…`) | 2, 4, 5, 6, 8, 9 | R2 ; correctif prompt v10 déjà déployé |
| T2 | Titre du sujet 1 : « …via OP_RETURN » alors que la source cite OP_RETURN seulement comme exemple générique (et pour Glupteba) | 1 | R2 ; règle de prompt déployée |
| T3 | Titre tronqué par « … » (budget 110 car.) | 6 | R2 ; correctif de repli déployé |

## Contenu, tableaux, IOC

| ID | Constat | Sujets | Reprise |
| --- | --- | --- | --- |
| C1 | **Tableau d'adresses IP inutile** (« Adresses IP fournies par la publication », 13 lignes, type = « IP » partout) alors que les mêmes IP sont listées dans la section IOC ; en plus défangées dans le tableau, refangées dans la liste | 5 | R1 + règle : un tableau ne restitue jamais des valeurs d'IOC |
| C2 | Même risque de tableau d'IOC (vu à une génération antérieure du sujet 8 : « Indicateurs techniques documentés »), non déterministe | 8 (et tous) | règle de prompt/validateur |
| C3 | IOC de **services légitimes** publiés comme IOC du sujet (`api.telegram.org`, `gofile.io`, `outlook.office.com`) | 9 ; 3 (`https://api.telegram.org/` en URL) | R0 si politique de liste « contexte » ; D |
| C4 | Listes de hachés très longues dans le corps de l'article (sujet 3 : environ 3,5 pages de SHA-256 sur 7) alors que la référence renvoie les IOC en annexes STIX/Excel | 3, 8 | D (annexe) |
| C5 | Réserve « IOC originaux à lien non démontré » : bien séparée, mais elle republie des valeurs de sources de support complètes | 8 | D |

## Diagrammes et mise en page

| ID | Constat | Sujets | Reprise |
| --- | --- | --- | --- |
| D1 | **Diagramme laid et libellés qui se chevauchent** (ELK compact) : icônes « personne » géantes, libellés d'arêtes superposés (« construit extensions et » / « construit un front end »), libellé traversé par une flèche (sujet 4). Le test sans chevauchement ne couvrait que 4 specs ; la production n'a **aucune vérification de chevauchement** avant publication | 7, 4 | R0-R1 : contrôle SVG en production + tailles d'icônes bornées + rejet/repli |
| D2 | **Diagramme trivial** (3 nœuds, 2 arêtes qui répètent le texte) : valeur analytique nulle | 4 (et 7) | D (seuil de valeur) |
| D3 | Demi-page blanche quand une figure insécable est renvoyée à la page suivante | 1 (p4), 8 | mineur |
| D4 | Césures typographiques (U+00AD) dans la prose | tous | accepté |

## Défauts logiciels du pipeline (hors contenu)

| ID | Constat | État |
| --- | --- | --- |
| P1 | Chaque montée de version fonctionnelle (prompt, validateur, politique) invalide `references` + `synthesis` de **tous** les sujets : un simple changement de texte de prompt coûte ≈2 h de bridge pour 9 sujets. Il manque une « adoption » d'artifacts compatibles | ouvert |
| P2 | Rejouer des décisions de réparation après un changement de version échoue (`synthesis_relevance_projection_changed`, puis `assembly_validation_failed` en forçant l'assemblage seul) ; contournement : reprise depuis `relevance_projection` | ouvert |
| P3 | Le collecteur reçoit des 403 sur les sites `.gov` et presse : pas de repli automatique (navigateur, archive) | ouvert |
| P4 | Un `model_run` orphelin en `running` depuis 24 h (appel perdu lors d'une panne, sujet 3) reste affiché | cosmétique |
| P5 | Trop d'appels modèle par source (corrigé : empreinte stable, dédoublonnage, cadrage par cas ; 28 → 2-4 appels pour les rapports multi-cas) | corrigé, déployé |
| P6 | Progression en direct et activité par sujet absentes de l'UI (corrigé) ; pause de batch (livrée) | corrigé, déployé |
| P7 | Publications historiques illisibles après une montée de politique (corrigé : jeu `legacy`) | corrigé, déployé |

## Ce qui est correct (à ne pas régresser)

Contenu fidèle aux sources et prudent sur l'attribution ; IOC d'autres cas exclus (sujets 4 et 7 : 0 IOC publié) ;
références numérotées et liens cliquables ; littéraux techniques copiables sans caractère invisible ; tableaux entiers
sur une page ; titres de section collés à leur contenu ; QA canonique PASS sur les 9 sujets.

## Décisions à prendre avant re-run

1. Publier la chronologie (G2) et ajouter le bloc « Vue d'ensemble » (G1) ?
2. Autoriser des sous-titres dans la synthèse (G3) pour approcher la référence ?
3. Archiver manuellement les pages officielles 403 (S2) et ajouter des sources complémentaires pour les sujets minces
   (S5), ou accepter ces sujets courts ?
4. Politique d'IOC : services légitimes (C3), listes de hachés en annexe (C4), réserve (C5).
5. Seuil de valeur d'un diagramme (D2).
6. Ajouter l'« adoption » d'artifacts compatibles (P1) avant toute régénération de masse.
