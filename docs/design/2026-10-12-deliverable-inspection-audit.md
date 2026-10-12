# Audit de faisabilité — inspection visuelle du livrable PDF

**Lot E · Phase 4 · Analyse seule · 12 octobre 2026**

## Décision

Faisable sans créer une seconde vérité documentaire. Le PDF compilé reste le
livrable canonique identifié par son SHA-256 ; `pdfinfo`, les diagnostics, les
PNG, les miniatures et les coordonnées sont des projections calculées depuis
ces octets. Les PNG peuvent vivre dans un cache éphémère borné, jamais dans
MinIO comme artefacts indépendants.

La tranche minimale devrait exposer, pour un SHA-256 de PDF déjà connu ou
fourni par le rendu de preview :

1. les métadonnées de rendu et l’état des diagnostics ;
2. une page PNG ou sa miniature, adressée par le SHA-256 du PDF et le numéro
   de page ;
3. les diagnostics mesurables et les correspondances de blocs que le modèle
   canonique permet effectivement de prouver.

Les PDF publiés et de release ont déjà une identité et une chaîne de
provenance en base/MinIO. Le PDF de preview d’édition est au contraire
compilé à la demande et non persisté. Il faudra lui fournir un SHA-256 de
sortie et un cache temporaire du PDF pour permettre l’inspection par hash.
Cette cache ne devient pas une source de vérité. Ne pas lancer une compilation
à chaque demande d’image.

Le principal manque n’est pas la génération du PDF : c’est l’absence de
coordonnées par bloc et de lien explicite entre ces coordonnées et une cible
de correction canonique. `subject_id` et les identifiants d’artefacts existent
autour du document, mais ne sont pas propagés jusqu’aux blocs Typst.

## Référence examinée et grille Phase 4.2

Références : [CN2609_typ.zip](/home/nill/perso/work/AutoWork/docs/references/CN2609_typ.zip)
et [PDF du bulletin du 5 octobre 2026](/home/nill/perso/work/AutoWork/docs/references/20261005_NP_TLP%20WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf).
L’archive a été extraite dans `/tmp/aw-e/cn2609/` après création d’un nouveau
dossier vide. Aucun fichier ni script extrait n’a été exécuté.

Le bulletin de référence fait 120 pages A4 (595,276 × 841,89 pt), 17 324 692
octets, PDF 1.7, balisé ; SHA-256
`1ea738682bf922449e28c30c885b9712af0b24a0314c3481bd41554ed67ce64e`. Sa
structure observée donne une grille concrète :

- page de résumé/sommaire avec un article, sept brèves numérotées 02–08, le
  suivi des règles, le suivi des indicateurs et les fichiers intégrés ;
- article long suivi de rubriques de synthèse, de références/chronologie, de
  paragraphes, tableaux et figures ;
- 50 légendes `Figure n` dans le texte extrait ;
- IOC présentés par catégories, dont une liste « IOC originaux » visible page
  56 ;
- sources externes, notes de bas de page et liens cliquables ; liste
  « Articles retenus et non traités » en fin de bulletin.

La grille à appliquer au livrable doit séparer trois axes :

| Axe | Contrôles adaptés au bulletin | Automatisation raisonnable |
|---|---|---|
| Structure éditoriale | Titre, sommaire et ordre article/brèves, sections attendues, renvois, annexes IOC et fichiers intégrés, pagination | Nombre de pages, présence et ordre des blocs canoniques, liens internes résolus si leurs destinations sont instrumentées, présence des rubriques attendues |
| Qualité CTI | Source et preuve associées aux affirmations, provenance des IOC, dates, type d’indicateur, cohérence de l’attribution et des relations | Vérifier la fermeture des références, les champs/provenances requis et les comptes ; une machine ne statue pas sur la véracité CTI, l’attribution ou la pertinence éditoriale |
| Rendu visuel | Lisibilité, densité, titres isolés, débordement, tableaux/figures, hiérarchie graphique, polices | Mesures déterministes ou signaux heuristiques ci-dessous, puis observation visuelle de l’agent et décision éditoriale humaine |

Observation visuelle, non-défaut établi : la page PDF 3 présente la
chronologie de l’article dans la moitié haute, une grande zone blanche dessous
et des notes en pied. Le contenu est lisible et tient dans la page. Un seuil de
faible remplissage la signalerait probablement ; seule une personne peut
déterminer si cette composition est souhaitée. La page 1 a un sommaire lisible
avec liens bleus ; les pages 3–4 ne montrent pas de débordement évident à
l’échelle inspectée.

Le PDF est déclaré balisé et `pdffonts` liste des polices incorporées
(Hanken Grotesk, Cascadia Mono, Liberation Mono, Noto CJK et Noto Color Emoji
notamment). `pdfinfo` a néanmoins émis `Syntax Error: Suspects object is wrong
type (boolean)` tout en terminant avec un statut 0 et en lisant 120 pages.
C’est un avertissement de parseur vérifié, pas un défaut visuel démontré.

## Flux et identité actuels

Code inspecté : [API sujet](../../backend/src/cti_app/api/subject_content.py),
[API publication](../../backend/src/cti_app/api/publication.py),
[preview édition](../../backend/src/cti_app/application/edition_preview.py),
[rendu publication](../../backend/src/cti_app/application/publication_rendering.py),
[rendu release](../../backend/src/cti_app/application/edition_rendering.py),
[modèle de rendu publication](../../backend/src/cti_app/domain/publication_render.py),
[modèle de rendu édition](../../backend/src/cti_app/domain/edition_render.py) et
[tables manifest/release](../../backend/src/cti_app/infrastructure/database/models/edition_publication.py).

| PDF demandé | Endpoint actuel | Identité/provenance et disponibilité |
|---|---|---|
| Publication courante d’un sujet | `GET /api/subjects/{subject_id}/publication/pdf` | Rend/récupère le PDF de l’artefact publication vérifié ; la route peut démarrer/terminer un rendu. En-têtes : `X-Publication-Artifact-Id`, `X-Publication-Artifact-Version`, `X-Publication-Render-Identity`. Ce dernier est le **hash des entrées de rendu**, pas le SHA-256 des octets PDF. La ligne `publication_renders` lie le rendu à `publication_artifact_id`; elle stocke `output_sha256`, blob, octets, statut, version du renderer/compilateur/template/polices et politique. Pas de page count exposé. 404 si absent, 409 si non vérifié, sinon PDF ou erreur de stockage/intégrité. |
| État/preview du PDF d’un artefact sujet | `GET /api/subjects/{subject_id}/publication/preview?artifact_id=…` puis `GET /api/subjects/{subject_id}/publication/preview/{artifact_id}/pdf?render_identity=…` | Le premier expose statut `READY/IN_PROGRESS/FAILED/STALE`, id/version/hash d’entrée de l’artefact, identité/hash d’entrée du rendu, disposition et URL conditionnelle. Il appelle le cycle de rendu, qui peut créer/terminer un rendu : ce n’est pas une lecture d’état strictement passive. Le second sert le PDF vérifié, y compris un rendu ancien explicitement marqué stale si disponible. Le SHA-256 de sortie et le nombre de pages ne sont pas exposés. |
| Aperçu d’édition | `GET /api/editions/{edition_id}/preview?preview_input_hash=…`, puis `GET /api/editions/{edition_id}/preview/pdf?preview_input_hash=…` | Le JSON donne version d’édition, hash d’entrée preview, artefacts (position, `subject_id`, `artifact_id`, version, `input_hash`), document et `stale`. Le PDF est recompilé à la demande, retourné en octets et n’est pas inscrit comme rendu canonique. Le hash d’entrée preview n’est pas un hash du PDF. La route n’expose ni hash de sortie ni nombre de pages. |
| PDF de release | `GET /api/editions/{edition_id}/release` pour l’état, `GET /api/editions/{edition_id}/release/pdf` pour les octets | L’état donne manifest id/hash, release id, render id/statut et `pdf_available`. La ligne `edition_renders` relie son blob de sortie au `edition_release_id` et possède `output_sha256`, taille, statut et versions de rendu. Le téléchargement PDF ne renvoie que le type et le nom de fichier. Pas de page count exposé. |

Chaîne vérifiable d’un PDF de release : `edition_render.output_sha256` et
`output_blob_id` → `edition_release_id` → `manifest_id` et hash du manifest →
entrées du manifest (`subject_id`, run, génération, artefact publication id,
version et `document_input_hash`) → document d’édition canonique et son hash.
Pour le PDF de sujet, `publication_renders.publication_artifact_id` renvoie à
l’artefact publication vérifié. Si cet artefact figure dans le manifest lié à
une release, les six champs de l’entrée manifest vérifient qu’il s’agit de la
même version. Le hash de sortie du PDF, le hash d’entrée du rendu, le hash
d’entrée de l’artefact et le hash du manifest sont quatre identités distinctes
et doivent être nommés séparément.

`pdfinfo`, `pdftotext`, `pdftoppm`, `pdfimages` et `pdffonts` (Poppler 24.02.0)
sont disponibles localement. [infra/backend.Dockerfile](../../infra/backend.Dockerfile)
n’installe que Typst, les polices et les utilitaires listés dans son `apt-get` ;
aucun outil Poppler n’est installé dans cette image backend. Le rendu Typst
limite déjà la compilation à 20 secondes et le PDF à 25 MiB
([typst_compilation.py](../../backend/src/cti_app/application/typst_compilation.py)).
Il n’existe pas de limite mémoire applicative équivalente pour un futur
processus Poppler.

Mesure locale : `pdftoppm -f 3 -l 3 -scale-to 1000 -png -singlefile` sur ce
PDF a pris 0,63 s et 15 680 KiB RSS ; PNG de la page 3 : 138 KiB. Cela décrit
une page de ce document sur cette machine, pas un SLA. La complexité d’une
page pleine d’images ou de transparences peut augmenter temps et mémoire.

## Identité de correction et localisation Typst

Le schéma effectivement supporté est [PublicationDocument V4/V5](../../backend/src/cti_app/domain/publication_document.py)
— aucun modèle V3 courant n’a été trouvé. V4 porte `subject_id`, le contenu,
les preuves/sources et les collections de blocs. Les tables, diagrammes,
figures et graphiques enrichis portent une `key` validée et unique dans le
document. Paragraphes, titres de section et entrées de chronologie n’ont pas
tous une clé métier stable. V5 enveloppe V4 avec `rich_text`, références et
IOC originaux ; certains ancres de texte utilisent les index lead/section/
paragraphe. Les ancres riches comme `table:{key}:row:…:cell:…` identifient des
champs de texte, pas actuellement un emplacement rendu ni nécessairement une
cible de mutation.

L’identité de l’artefact (id/version/`input_hash`) est portée par les références
du document d’édition et par les entrées immuables du manifest, et est
renvoyée séparément du document dans la réponse preview. `EditionDocumentV2`
transmet aux publications Typst `subject_id`, position et document, mais pas
l’id/version/hash de l’artefact. Les blocs projetés pour Typst gardent le
`type` et, pour table/figure/diagramme/graphique, leur clé. Les helpers Typst
rendent ces blocs sans label canonique ni sortie de coordonnées. Les labels
présents dans le helper d’édition identifient l’ancre d’article pour les liens
du sommaire, pas chaque bloc.

`revision_targets` est un catalogue calculé dans l’API production à partir du
contenu canonique de synthèse : chaque entrée a un id dérivé du SHA-256 de sa
cible typée et les opérations permises. Il n’est pas inclus dans
PublicationDocument V4/V5 ni dans `render_data` Typst ; pour l’étape publication
ce catalogue n’est pas une carte de blocs rendus. Aujourd’hui un agent ne peut
donc pas relier de façon générale le rectangle vu sur une page à une cible de
correction précise.

La localisation déterministe est techniquement réalisable sans faire de la
page, de l’ordre des éléments ou de leur ressemblance une identité :

1. résoudre, avant rendu, la cible typée canonique et sa référence artefact ;
2. transmettre au rendu un `target_id` opaque et stable dérivé de cette cible,
   avec `subject_id`, artefact id/version/hash et type/clé du bloc ;
3. instrumenter les blocs Typst avec labels/markers de layout et interroger
   leur localisation après layout (`query`/`context`/`locate`, selon le
   prototype Typst retenu). Écrire une sidecar de coordonnées attachée au hash
   exact du PDF ou du hash de rendu. Pour les blocs qui se fragmentent, émettre
   une localisation par fragment/page ; des marqueurs de début/fin seuls ne
   suffisent pas toujours ;
4. utiliser `pdftotext -bbox-layout` comme mesure de boîtes textuelles et
   corrélation de texte **après** cette résolution canonique. `bbox` ne fournit
   pas à lui seul une identité de bloc, et le texte identique répété peut
   ambiguïser une jointure.

La page et le rectangle sont des propriétés de cette compilation précise. Ils
changent au reflow ; ils ne sont jamais envoyés comme identité de mutation.
Commencer par les cibles dont les clés sont déjà explicites (titre d’article,
table, figure, diagramme, graphique) et retourner `mapping: null` avec une
raison quand la jonction canonique manque. Ne pas fabriquer une clé à partir
du numéro de page ou de l’index DOM. Les paragraphes/sections et cellules sans
identité métier sont une extension de schéma ou un mapping partiel à étudier.

## Détections et niveau de preuve

| Défaut recherché | Mesure déterministe disponible/proposée | Faux positifs / statut |
|---|---|---|
| PDF illisible, hash ou nombre d’octets incohérent | Vérifier `%PDF`, SHA-256 et taille contre `output_sha256`/`output_byte_size`, `pdfinfo`; refuser encrypted/parse error selon politique | Déterministe ; ne prouve pas la qualité visuelle. L’avertissement `Suspects` du PDF de référence montre qu’un parseur peut avertir sur une structure tolérée. |
| Nombre de pages / page vide | `pdfinfo` donne pages et dimensions ; `pdftotext -bbox-layout` ou masque de pixels mesure les zones occupées par page | Le compte exact est déterministe. « Faible remplissage » est un signal heuristique : couverture, sommaire, dernière page, figures pleine page et la page 3 de la référence seraient des faux positifs probables. |
| Titre orphelin en bas | Après ajout de markers typés heading/corps, comparer page et bbox du titre au premier bloc suivant | Heuristique si basée uniquement sur proximité géométrique ; signal exact possible une fois les deux cibles canoniques taguées. Titres courts et sections volontairement séparées : faux positifs. |
| Débordement/coupe | Bbox texte hors MediaBox/TrimBox, erreurs/avertissements Typst, ou contenu attendu absent du PDF ; comparer zones des markers aux limites page | Hors-page mesurable, mais le texte clipé peut manquer du bbox. Marges, notes et fonds perdus intentionnels : faux positifs. L’agent doit voir le PNG. |
| Tableau coupé | Une fois lignes/cellules taguées, détecter des fragments de table sur plusieurs pages, ligne scindée ou en-tête non répété ; vérifier les textes attendus | Sans identité de ligne ni instrumentation, `pdftotext` n’établit pas la géométrie des cellules. Coupure sur page acceptable pour certains tableaux : heuristique/observation visuelle. |
| Image/diagramme illisible ou trop petit | `pdfimages -list` fournit dimensions et PPI des images raster ; mesure du rectangle de figure via Typst sidecar ; pixels de miniature pour revue | Les SVG/vectoriels ne sont pas décrits par `pdfimages`. PPI faible ne signifie pas illisible (captures, logos) ; diagnostic final visuel. Exclure masques alpha et ressources d’en-tête répétées. |
| Lien/ancres cassés | Warnings Typst pour références internes ; vérifier destinations/annotations PDF avec inspection des objets et labels attendus ; `pdfinfo -url` énumère les URI externes | Résolution interne déterministe si les destinations sont connues. Une URL externe inaccessible n’est pas testée hors ligne ; URI dupliquées ne signifient pas forcément lien cassé. |
| Police manquante/substituée | Warnings de compilation Typst + `pdffonts` (police et flag embedded) contre versions/allowlist de polices attendues | Police absente/non incorporée mesurable ; une police fallback peut être incorporée et rester visuellement fausse. La présence dans le PDF ne garantit pas la bonne police métier. |
| Qualité CTI / validité des références | Vérifier liens d’evidence/source et provenance IOC dans le document canonique ; vérifier leur présence dans le rendu par markers | La complétude des liens est mesurable. Exactitude, causalité, attribution, pertinence et décision d’inclure restent éditoriales/humaines. |

À présenter séparément dans l’interface : **anomalie déterministe vérifiée**,
**soupçon heuristique**, **observation visuelle de l’agent** et **décision
éditoriale humaine**. Le texte de l’agent ou l’image ne crée ni preuve CTI ni
nouvelle vérité documentaire.

## Contrat d’API minimal proposé

Les routes sont en lecture seule et ne compilent pas un PDF publié à chaque
appel d’image. Hash de route = SHA-256 des **octets PDF de sortie**, distinct
du hash d’entrée Typst. La résolution n’accepte que les blobs de rendu connus,
vérifiés par leur taille et SHA-256 ; pour preview éphémère, la route PDF de
preview renvoie `X-PDF-SHA256` et inscrit temporairement ses octets dans un
cache local borné.

```text
GET /api/pdf-inspections/{pdf_sha256}
GET /api/pdf-inspections/{pdf_sha256}/pages/{page}.png?profile=preview
GET /api/pdf-inspections/{pdf_sha256}/pages/{page}/thumbnail.png
```

Exemple exact de réponse metadata (valeurs illustratives) :

```json
{
  "schema_version": "pdf-inspection-v1",
  "pdf": {
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "byte_size": 17324692,
    "page_count": 120,
    "page_size_points": [595.276, 841.89],
    "encrypted": false
  },
  "availability": "ready",
  "render": {
    "id": "00000000-0000-0000-0000-000000000001",
    "status": "succeeded",
    "input_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "renderer_version": "edition-v3-typst-v3-ioc-index-briefs",
    "template_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "compiler_version": "0.15.1",
    "font_bundle_version": "fonts-v1"
  },
  "source": {
    "kind": "edition_release",
    "edition_id": "00000000-0000-0000-0000-000000000010",
    "release_id": "00000000-0000-0000-0000-000000000011",
    "manifest_id": "00000000-0000-0000-0000-000000000012",
    "manifest_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "edition_document_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "preview_input_hash": null,
    "artifacts": [
      {
        "subject_id": "00000000-0000-0000-0000-000000000020",
        "artifact_id": "00000000-0000-0000-0000-000000000021",
        "artifact_version": 3,
        "artifact_input_hash": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
      }
    ]
  },
  "raster": {
    "status": "available",
    "rasterizer": "poppler",
    "rasterizer_version": "24.02.0",
    "profiles": {
      "preview": {"dpi": 120, "max_edge_px": 1600},
      "thumbnail": {"max_edge_px": 480}
    },
    "page_png_url_template": "/api/pdf-inspections/{pdf_sha256}/pages/{page}.png?profile=preview",
    "thumbnail_url_template": "/api/pdf-inspections/{pdf_sha256}/pages/{page}/thumbnail.png"
  },
  "diagnostics": {
    "status": "ready",
    "engine_version": "pdf-diagnostics-v1",
    "counts": {"deterministic": 0, "heuristic": 1},
    "items": [
      {
        "code": "page_low_occupancy",
        "class": "heuristic",
        "page": 3,
        "bbox_points": [52.0, 80.0, 540.0, 760.0],
        "measurement": {"occupied_ratio": 0.31, "threshold": 0.35},
        "target": null
      }
    ]
  },
  "mapping": {
    "status": "partial",
    "items": [
      {
        "target": {
          "subject_id": "00000000-0000-0000-0000-000000000020",
          "artifact_id": "00000000-0000-0000-0000-000000000021",
          "artifact_version": 3,
          "artifact_input_hash": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
          "block_type": "table",
          "block_key": "cve-summary",
          "revision_target_id": null
        },
        "locations": [
          {"page": 17, "bbox_points": [52.0, 180.0, 540.0, 620.0]}
        ],
        "mapping_quality": "canonical_block_key"
      }
    ]
  }
}
```

Sujet PDF `source.kind` serait `subject_publication` avec son artefact et
éventuellement la release/manifest qui le référence. Preview d’édition serait
`edition_preview`, sans `release_id`, avec `preview_input_hash` et les mêmes
références artefact que son document. Pour preview, absence/expiration du
cache donne `availability: "preview_cache_expired"`; les octets peuvent être
obtenus à nouveau via le endpoint PDF preview, qui renverra leur hash. Pour un
rendu en cours/échoué, métadonnées renvoient l’état connu sans promettre de
PNG ; erreurs stables distinguent absence, hash incorrect, sortie corrompue,
limite dépassée et cache expiré.

Les routes PNG renvoient une seule page par requête, `Content-Type: image/png`,
`ETag` de l’image dérivée, `X-PDF-SHA256`, numéro de page et profil. `page` doit
être entre 1 et `page_count`. Le hash demandé est comparé aux métadonnées avant
toute lecture du blob ; aucun chemin fourni par le client n’est ouvert.

## Bornes, cache, tests, risques

Bornes de départ proposées, à recalibrer par un essai de charge : PDF ≤ 25 MiB
(limite Typst actuelle), ≤ 200 pages, 1 page PNG par requête et au plus 4
pages en lot si un lot est ajouté. Profils fixes, sans DPI libre : preview
120 dpi/plafond 1600 px ; miniature plafond 480 px. Exécuter Poppler dans un
sous-processus sans shell, délai 15 s par page et 30 s par demande, limite
mémoire par processus 256 MiB et concurrence globale initiale 2. Refuser
dimensions de page extrêmes au-delà d’une limite de pixels calculée avant
rasterisation. `pdfinfo`/`pdftotext` diagnostics ont aussi un timeout et une
taille maximale de sortie.

Installer `poppler-utils` dans l’image backend avec version explicitement
verrouillée et inclure version/build dans le manifeste d’image. Normaliser la
locale et tous les paramètres d’appel. Clé de cache exacte :
`pdf_sha256 + rasterizer_build + profile + page + inspection_policy_version`.
Diagnostic texte/structure : `pdf_sha256 + poppler_build + diagnostics_version`.
PNG écrits atomiquement dans un cache local éphémère LRU, plafond 512 MiB et
TTL 24 h pour PDF persistés ; preview temporaire : TTL 10 min, quota 100 MiB,
effacement à expiration. Une entrée de cache manquante se reconstruit depuis
les octets PDF vérifiés. Pas de colonne `page_count` nécessaire : le calcul
est reproductible et cacheable à partir du hash, et aucune donnée de rendu
dérivée n’est promue en canon.

Tests ciblés à écrire pour l’implémentation :

- validité, hash/taille, pages 1/limite/au-delà, PDF tronqué, chiffré, pages
  hors bornes, dimensions anormales, timeout et sortie Poppler invalide ;
- mêmes octets + même build/profil/page donnent les mêmes pixels/hash ; changer
  le PDF ou la version de rasterizer invalide le cache ; LRU/TTL/concurrence
  restent bornés ;
- liaison PDF sujet → artifact id/version/input hash et PDF release → render
  → release → manifest → entrées manifest, avec distinction entre hashes ;
- reflow modifie page/bbox mais ne modifie jamais `target_id`; identités
  ambiguës ou manquantes donnent `mapping: null`, jamais un ciblage par page,
  index ou apparence ;
- pages blanche, titre bas, table fragmentée, image miniature et avertissement
  de police comme fixtures positives/négatives, avec classification
  déterministe vs heuristique explicitement testée.

Risques : `pdfinfo` et `pdftotext` ne donnent pas l’identité d’un bloc ; les
marqueurs Typst doivent couvrir les types supportés et les fragments de blocs
multi-pages. Les chemins à page par page coûtent cher sur images lourdes,
d’où concurrence et quotas. Pinning Poppler et dépendances natives augmente
l’image backend et demande une politique de mise à jour de sécurité. Un SHA-256
PDF égal peut pointer vers plusieurs renders/propriétaires si deux rendus
produisent les mêmes octets ; la résolution garde toutes les provenances et
applique l’autorisation habituelle, sans supposer une relation univoque.

Différer : lecture complète de contenu/OCR, détection IA des défauts, tests de
disponibilité réseau des URL externes, mapping cellule-par-cellule sans clés
canoniques, remédiation automatique, annotation éditoriale persistée, rendu
4K/toutes-pages en un appel, et persistance de PNG/PDF preview dans MinIO.

## Démonstration Phase 4.4 et effort

Une boucle démontrable hors DB réelle et sans exécution de run réel : fixture
isolée contenant une publication synthétique V5, artefact/run/manifest factices
en mémoire et ports de modèles remplacés par des réponses fixes. Compiler
Typst réel sur ce document de fixture, obtenir hash PDF, métadonnées, PNG et
diagnostic volontaire (p. ex. taille d’une figure sous seuil). Résoudre le
finding vers un `target_id` canonique connu, appliquer une correction simulée
qui produit la version d’artefact suivante, recompiler puis prouver : nouvel
artefact id/version/hash et PDF hash, ancien finding absent ou modifié et
localisation recalculée. Garder les PNG des deux hashes pour comparaison.
L’ancienne image reste liée à l’ancien PDF hash. La correction elle-même reste
une mutation canonique du document fixture, jamais une annotation du PNG. Le
smoke existant [scripts/typst_edition_smoke.py](../../scripts/typst_edition_smoke.py)
montre déjà une compilation Typst d’une édition à deux publications sans
base ; les tests [test_edition_preview.py](../../backend/tests/test_edition_preview.py)
et [test_edition_typst_rendering.py](../../backend/tests/test_edition_typst_rendering.py)
offrent des points d’appui pour le montage simulé.

Effort : **4–6 jours** pour API hash, rasterizer borné, cache, diagnostics
simples, instrumentation de quelques types de blocs et tests fixture ; **7–10
jours** si l’on exige dès cette passe un mapping robuste multi-page incluant
tableaux fragmentés, textes sémantiques et intégration complète de toutes les
cibles de correction. Aucun changement de schéma DB n’est a priori requis,
car output SHA et blobs de rendu existent déjà. Fichiers probablement touchés :

- `infra/backend.Dockerfile` et lock/version d’image pour Poppler ;
- nouveaux services/contrats sous `backend/src/cti_app/application/` et
  `backend/src/cti_app/infrastructure/` pour inspection, raster et cache ;
- nouveau routeur d’inspection inclus dans `backend/src/cti_app/api/main.py`,
  plus adaptateurs d’identité dans `api/publication.py` et
  `api/subject_content.py` ;
- `application/edition_document.py`, `edition_typst_rendering.py`,
  `typst_rendering.py` et `chpTypst/RENDERER/{publication_helpers,edition_helpers}.typ`
  pour propager les références et émettre les marqueurs/sidecar ;
- tests backend unitaires de raster, limites, provenance, mapping, diagnostics
  et boucle Phase 4.4 en fixture simulée.

Pas de changement frontend nécessaire pour valider les routes ; une visionneuse
PDF avec surbrillance serait une étape séparée.

## Validation de cet audit

Aucune base PostgreSQL, MinIO, API externe, compilation de production ou suite
de tests n’a été exécutée. Les constats sont une inspection statique et des
mesures de lecture du PDF de référence ; le prototype Typst de coordonnées est
à réaliser pendant l’implémentation. `ctx.py` a échoué avant indexation car
`uv` ne pouvait pas télécharger ses dépendances (réseau DNS indisponible) ; la
recherche a été poursuivie avec `rg` ciblé.
