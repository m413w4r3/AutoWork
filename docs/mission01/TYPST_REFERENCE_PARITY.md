# M01-T — Parité des références Typst

**Statut du livrable : DONE** pour l’inventaire, la comparaison contrôlée et les captures synthétiques. Aucune correction du moteur ni décision éditoriale n’est incluse. La parité de bout en bout avec le renderer Python reste partiellement inconnue : son test de compilation réel s’arrête sur le bundle de polices local incomplet.

## Références approuvées et provenance

Les trois archives et leurs extractions étaient présentes et lisibles sous `/home/nill/perso/work/AutoWork/docs/references/`. Les empreintes des extractions sont calculées sur les fichiers triés par chemin relatif, avec une ligne `SHA256(fichier)␠␠chemin-relatif\n` par fichier, puis un SHA256 du bloc concaténé.

| Référence | SHA256 ZIP | SHA256 extraction | Fichiers |
|---|---|---|---:|
| `CN2609_typ.zip` | `e7e3a6bd826039a4cb3299a3d3b2bb22c2bedb069701881e27391f76fd4b7287` | `844a87bf1970e220c870ce92a7072bc3086319f0cf0184625eef984a10e5fdbc` | 105 |
| `typstPipeline.zip` | `2dabf68892f71d25fb2f93ab21ae50c578514aca8125564161fccd1edf224c3e` | `e8d0585c5e81e6f0e10a56954a9e591e0afe25c7bb1dc78eef5ef9348d226af5` | 50 |
| `chpTypst.zip` | `bea74c614642766ade1b195f341efbddcb6f6edaafcbbfbb555cf0e9bfd2c5c8` | `449f7c24e5ef0529316a24814c6d6ce5c762e10337b44b480e12a474f5dea96c` | 32 |

Les noms d’archives, les README et l’arborescence identifient les artefacts, mais ne donnent pas de dépôt amont ni de commit d’origine pour `typstPipeline` ou `chpTypst`. Cette provenance amont reste **UNKNOWN**. Le `chpTypst/TEMPLATES/article.typ` conservé dans l’extraction actuelle est identique à son exemplaire approuvé historique (`8328349505ed12ba174308384c285f80e5dcde02661ba6c4d9e61ea32a7d88fc`); il ne constitue pas l’entrée de production actuelle.

Les PDFs approuvés sont des références historiques distinctes des compilations de cette mission :

| PDF | SHA256 | Pages déclarées | Observation |
|---|---|---:|---|
| CN2609 | `1ea738682bf922449e28c30c885b9712af0b24a0314c3481bd41554ed67ce64e` | 120 | Page A4; texte des pages 1–3 extrait pour confirmer couverture, page d’annexes et début d’article. |
| RU2609 | `996debd1882cb52129cb40a5f6e0c4d941625c0f7d84a9778db7efbd72ff92e5` | 114 | Page A4; métadonnées de pagination vérifiées. |

Poppler signale `Syntax Error: Suspects object is wrong type (boolean)` sur ces PDFs et sur les sorties Typst synthétiques; il fournit néanmoins les dimensions et nombres de pages. Ces nombres ne sont pas une preuve de qualité de rendu. Le PDF CN de 120 pages n’a pas été recompilé ni entièrement relu ici, et ne représente pas un succès du renderer actuel.

## Typst, polices et chemins de production

- Le manifeste courant [`chpTypst/renderer-manifest.json`](../../chpTypst/renderer-manifest.json) annonce `chp-article-v33-literal-analyst-notes`; son entrée est `RENDERER/publication.typ`, qui charge `render-data.json` puis `render-publication`.
- `/snap/typst/current/bin/typst --version` retourne `typst 0.15.1 (9dfd3a08)`. Son SHA local est `e2c38340d3b54f72188c8ae65049b16526a9622d475683a24e9536b09cf5cdcc`. `infra/typst.lock` épingle également la version 0.15.1 mais son SHA AMD64 déclaré est `a6d077d0a95eed5a2eba715b2dae06be954f624ccbf85758a03f389ded33118c`; le binaire Snap utilisé a donc la même version, mais n’est pas le binaire verrouillé par SHA. Le lanceur `typst` habituel échoue ici sous `snap-confine`/AppArmor; la version a été lue directement depuis le binaire embarqué.
- Fontconfig recense 391 familles installées. `Liberation Mono` est disponible; `Hanken Grotesk` et les familles `Cascadia` demandées par le renderer actuel ne le sont pas dans l’inventaire système. Les compilations de comparaison ont chargé explicitement Hanken depuis l’extraction approuvée `chpTypst/HankenGrotesk` : Regular `315cda587038b4d21cb4899df306fef5c15b34b6b9470352eba9a3dfce72f380`, Bold `d4483ef2e26692ea2e491be712b21e0df9cd02c72aea8fa620dd167a337087e8`.
- Les compilations actuelles avertissent que `Cascadia Mono`, `Cascadia Code` et leurs variantes sont inconnues. Les caractères IOC de cette comparaison utilisent donc un repli de police; la parité typographique des valeurs techniques n’est pas validée. Le test automatisé réel exige tout le bundle de `infra/typst-fonts.lock`, qui n’est pas présent dans l’extraction approuvée `chpTypst`.

Le pipeline historique documenté dans `typstPipeline/README.md` part d’un article par dossier, d’un `article.csv`, d’une liste manuelle dans `main.typ` et d’un sommaire entretenu à la main. Le CSV commence par une ligne de métadonnées puis `isNew,type,value`; `ioc-listv2` sépare `isNew == "0"` et `isNew == "1"` en IOC et IOC originaux. `ioc_reporter_typst.py` contient une couche d’enrichissement via des fournisseurs réseau (dont Team Cymru, Shodan, VirusTotal, MalwareBazaar, Triage et Hybrid Analysis), puis produit des CSV et sorties Typst. Aucun de ces appels ni aucun enrichissement n’a été exécuté.

Le chemin actuel d’assemblage appelle `build_publication_document_v5` (`backend/src/cti_app/application/publication_assembly.py:113-120`), puis `TypstRenderer.render` construit le JSON envoyé à l’entrée Typst (`backend/src/cti_app/application/typst_rendering.py:839-853`). V5 enveloppe V4 avec texte sémantique, références datées, groupes d’IOC originaux et détails d’IOC (`backend/src/cti_app/domain/publication_document.py:1410-1418`). Les ajouts V6 — avis approuvé, graphe et détection — sont des extensions distinctes.

## Méthode de comparaison

La source partagée [`fixture.json`](typst-captures/fixture/fixture.json) ne contient que des valeurs synthétiques : plages RFC 5737, URL `example.invalid`, textes explicitement marqués comme essais, événement et schéma SVG sans infrastructure observée. Le titre, l’événement, le texte principal, le tableau (avec les quatre libellés `sourcé`, `validé`, `candidat`, `écarté`), sa légende, la figure, la note et l’URL sont présents dans chaque rendu.

Le générateur [`render_fixture_inputs.py`](typst-captures/fixture/render_fixture_inputs.py) prépare trois variantes :

1. le renderer Typst courant à partir de JSON conforme à sa surface `render-data`;
2. le helper historique `article()` de l’extraction approuvée `chpTypst`;
3. le helper `article()` du pipeline historique avec son `article.csv` et `ioc-listv2`.

La projection JSON courante est construite manuellement. Elle exerce les composants Typst courants, mais **ne passe pas par l’objet Python `TypstRenderer` ni par l’assemblage de publication**. L’exécution de son test réel est rapportée plus bas; elle n’a pas atteint la compilation faute de bundle de polices. Le CSV historique reçoit seulement les deux valeurs synthétiques `sourcé` et `validé`, chacune avec `isNew=0`. `isNew` sépare IOC et IOC originaux; il ne signifie pas « validé ». Le champ historique binaire ne peut exprimer candidat/écarté; ceux-ci restent dans le tableau commun et ne sont pas promus dans sa liste d’IOC.

Le script [`render_fixture_captures.sh`](typst-captures/fixture/render_fixture_captures.sh) régénère les PDFs synthétiques et les captures PNG à 140 dpi. [`inspect_fixture_outputs.py`](typst-captures/fixture/inspect_fixture_outputs.py) extrait le texte avec Poppler, vérifie les contenus communs et les différences attendues, puis contrôle qu’une capture existe par page. Les captures ont aussi été inspectées visuellement pour la hiérarchie, les polices, tableaux, renvois, notes et débordements.

## Comparaison par page et section

| Variante, page | Contenu observé | Comparaison visuelle et textuelle |
|---|---|---|
| Actuelle, p. 1 | Titre 20 pt gras, en-tête documentaire, `RÉFÉRENCES`, `SYNTHÈSE`, tableau, figure, note, IOC documentés et candidat. Les quatre libellés synthétiques restent visibles dans le tableau. | La page est dense mais lisible; pas de chevauchement ni de coupure constatée. Le tableau courant affiche titre et légende, avec corps à 8 pt et traits fins. Figure plus grande et légende « Figure 1 : ». L’IOC écarté n’est pas promu dans la liste courante; il reste dans le tableau synthétique. |
| Actuelle, p. 2 | `PROVENANCE DE PUBLICATION` et identité complète uniquement. | Page largement vide, créée par le `pagebreak()` avant l’annexe d’identité (`publication_helpers.typ:1187-1205`). Le supplément conserve une identité documentaire explicite, mais son coût visuel mérite validation. |
| Historique `chpTypst`, p. 1 | Page blanche dans le rendu autonome. | Le helper `article()` commence par `pagebreak()` (`UTILS/helpers.typ:167-186`). C’est un effet du fixture autonome; dans un bulletin complet, ce saut sépare les contenus précédents de l’article. Il ne faut pas le traiter seul comme une page manquante du moteur actuel. |
| Historique `chpTypst`, p. 2 | Titre, badge `ARTICLE`, numéro `07`, `Chronologie`, `Vue d’ensemble`, `Synthèse`, tableau, figure, note; la note de source est un renvoi en bas de page. | Le corps de table est plus grand que les 8 pt actuels; le helper historique n’a pas de paramètres de titre/légende de tableau, donc le fixture ajoute ces textes autour de `styled-table()`. Les IOC démarrent sur la page suivante; les blocs sont lisibles, avec beaucoup d’espace restant sur p. 3. |
| Historique `chpTypst`, p. 3 | Quatre listes d’IOC manuelles étiquetées `sourcé`, `validé`, `candidat`, `écarté`. | Les titres et valeurs sont séparés visuellement des sections précédentes. Le helper historique autorise ces titres, mais le format CSV du pipeline ne les encode pas. |
| Pipeline historique, p. 1 | Page blanche due au même `pagebreak()` autonome. | Même effet d’insertion que ci-dessus. |
| Pipeline historique, p. 2 | Métadonnées d’article, chronologie, vue d’ensemble, synthèse, tableau, figure, note et renvoi source. | La mise en page est proche du helper historique `chpTypst`; le titre et la légende du tableau restent du texte placé par le fixture autour du helper. |
| Pipeline historique, p. 3 | En-tête `IOC` et deux adresses correspondant aux seules lignes `isNew=0`. | La liste est séparée sur une page presque vide. Les catégories candidat et écarté n’existent pas dans le schéma historique binaire et ne sont pas ajoutées à cette liste. |

Les gabarits et le renderer partagent le papier A4, le texte Hanken 11 pt et les marges 2,3 cm haut/bas, 2,2 cm gauche/droite. Le titre `article()` historique est défini en 16 pt régulier (`chpTypst/UTILS/helpers.typ:188-194`); le renderer courant formate le titre de niveau 1 en 20 pt extrabold (`chpTypst/UTILS/document_style.typ:92-96`). Le tableau courant emploie 8 pt et une bordure 0,4 pt (`publication_helpers.typ:197-232`), contre 11 pt par défaut et une bordure 1 pt dans le helper historique (`UTILS/helpers.typ:243-283`). Le contenu visible est comparable, mais le poids typographique, l’espace vertical, la taille du graphique et le libellé des figures diffèrent.

## Écarts classés

Ces classifications sont des recommandations de parité, pas une approbation éditoriale ni une demande de retour arrière automatique.

| Écart | Classe | Preuve et validation éditoriale requise |
|---|---|---|
| `category`, numéro d’article et `overview` ne figurent pas dans V4 ni dans son JSON (`publication_document.py:738-754, 861-876`), alors que `article()` les accepte et affiche badge/numéro/vue d’ensemble (`chpTypst/UTILS/helpers.typ:167-228`). | **RESTORE_REFERENCE** | Rétablir ces concepts dans une extension versionnée seulement après validation éditoriale de leur définition et de leur source d’autorité. Ne pas déduire la vue d’ensemble du lead ou de la synthèse. |
| Références datées, références liées, détails de provenance et annexe IOC structurée. V5 conserve ces champs; le renderer courant affiche références et identité complète. V5 distingue `RETAINED`, `UNCERTAIN`, `EXCLUDED` et libelle `Écarté` (`publication_document.py:1243-1261`; `typst_rendering.py:112-116, 1945-2055`). | **KEEP_JUSTIFIED_EXTENSION** | Conserver cette structure de références et de provenance. Elle protège les sources, relations et motifs de décision. La liste de synthèse et l’annexe technique ne doivent pas être confondues. |
| Correspondance entre les quatre libellés synthétiques et le modèle courant, d’une part, et le `isNew` historique binaire, d’autre part. | **DEFER** | Les termes de fixture ne sont pas une équivalence des enums `Retenu/Incertain/Écarté`; `isNew` signifie original/non-original. Validation CTI et éditoriale requise avant toute règle de conversion. Maintenir les quatre niveaux distincts et leurs références; ne pas promouvoir un candidat ou un écarté en IOC retenu. |
| Événement rendu comme `Chronologie` avec note de bas de page dans l’ancien helper, contre entrée `RÉFÉRENCES` et URL directement visible dans le fixture courant; V4 conserve néanmoins `timeline`. | **DEFER** | Décider si une chronologie doit rester séparée des références d’articles et si les notes deviennent des liens numérotés. Conserver la date et la source dans les deux cas. |
| Hiérarchie de titres, taille du tableau, supplément de figure et pagination changent. Le pipeline ancien expose aussi `execSum`, absent de V4. | **DEFER** | Validation de maquette par l’édition avant toute restauration de style ou ajout d’un champ résumé exécutif. Le fixture montre des différences réelles mais ne suffit pas à définir la maquette d’un bulletin complet. |
| Sections courantes typées (références, limites, avis, détection, graphiques, annexe) et leur rendu orienté provenance. | **KEEP_JUSTIFIED_EXTENSION** | Conserver les blocs typés étayés par sources, hashes et relations. Les extensions ne suppriment pas le besoin éditorial sur `category`, numéro ou vue d’ensemble. |

## Vérifications exécutées

| Vérification | Résultat |
|---|---|
| Python du worktree : `cd backend && PYTHONPATH=$PWD/src .venv/bin/python -c 'import sys; print(sys.executable, sys.version)'` | `/home/nill/.paseo/worktrees/0y1qsxa1/docs-mission01-t-typst-parity/backend/.venv/bin/python`, version 3.12.3. Après la consigne d’environnement, les tests ont utilisé ce binaire directement, sans `uv run`, Conda actif ni Python nu. |
| `bash docs/mission01/typst-captures/fixture/render_fixture_captures.sh` | Succès : compilation Typst des 3 variantes, PDFs synthétiques, PNG par page et contrôle de texte. Compteurs constatés : actuel 2 pages, `chpTypst` historique 3, pipeline historique 3. |
| `inspect_fixture_outputs.py` via le script ci-dessus | Succès : contenus communs, quatre valeurs et niveaux de la table, URL, note, légende de figure, sections héritées/courantes attendues, pages PNG présentes. La vérification combine texte et captures; le nombre de pages seul n’est pas utilisé comme oracle. |
| `test_typst_chp_parity.py::test_real_typst_pdf_preserves_chp_publication_structure` | **Échec avant compilation** : `FontBundleInvalidError: Manifest-listed file is missing: 'CascadiaCode/ttf/CascadiaCode.ttf'`. Source du bundle : extraction approuvée `chpTypst`, qui ne contient que les polices Hanken référencées ici. Aucun téléchargement n’a été tenté. |
| `test_typst_input_inspection.py::test_persisted_inspection_lists_exact_inputs_links_pdf_and_never_executes` | **1 passed**. Le test vérifie les entrées et liens PDF d’inspection, sans compilation ni appel modèle. |
| `pdfinfo`, `pdftotext -layout`, `fc-list`, `fc-match`, SHA256 | Exécutés sur les références et fixtures. Avertissement Poppler `Suspects object is wrong type (boolean)` rapporté plus haut. |

Une tentative initiale `uv run --python 3.12 ...` — avant la consigne de ne plus utiliser `uv` dans ce worktree — a sélectionné Miniconda 3.12.4 et s’est arrêtée sur la dépendance absente `flare-floss==3.1.1` pendant une tentative de téléchargement bloquée. Aucun test n’a été lancé dans cet environnement; le `.venv` créé par cette tentative a été supprimé puis remplacé par le lien vers l’environnement backend validé. Aucun fournisseur CTI n’a été appelé.

Commandes pytest réellement lancées depuis `backend/` :

```sh
TMPDIR="$HOME/tmp" TYPST_BINARY=/snap/typst/current/bin/typst \
FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/docs/references/chpTypst \
PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  tests/test_typst_chp_parity.py::test_real_typst_pdf_preserves_chp_publication_structure \
  -q --tb=short

TMPDIR="$HOME/tmp" PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  tests/test_typst_input_inspection.py::test_persisted_inspection_lists_exact_inputs_links_pdf_and_never_executes \
  -q --tb=short
```

## Limites et suites requises

- Les PNG sous `typst-captures/{current,historical,historical-pipeline}/` sont des **captures de fixtures synthétiques**, pas un bulletin, une publication CTI finale, ni une inspection v61. Les PDFs de travail sont créés sous `/tmp` puis supprimés par le script.
- Le test Python du chemin de compilation actuel n’a pas dépassé la validation du bundle de polices. L’assemblage `PublicationDocumentV5 → TypstRenderer → PDF` complet reste donc **UNKNOWN** dans cet environnement. Le test Typst direct porte sur les composants actuels et le JSON synthétique explicitement décrit ci-dessus.
- Les quatre niveaux sont des étiquettes synthétiques. Les adresses sont des exemples RFC 5737 et l’URL est sous `.invalid`; aucune donnée de preuve CTI canonique, référence canonique, hash d’évidence ou statut de release/G3 n’a été modifié.
- La comparaison de contenu réel est limitée aux premières pages du PDF CN historique; elle ne qualifie pas l’intégralité de ses 120 pages. Les appels aux fournisseurs historiques n’ont jamais été exécutés.
- Le binaire Typst utilisé partage la version 0.15.1 verrouillée, mais pas le SHA de l’artefact AMD64 épinglé. La différence de polices Cascadia empêche une comparaison typographique définitive.
- Toute réintroduction des métadonnées perdues et toute conversion de statuts demandent une validation éditoriale explicite. Aucun changement de schéma ou de moteur n’est recommandé avant cette décision et la disponibilité du bundle de polices verrouillé.
- Coût observé : **UNKNOWN**.
