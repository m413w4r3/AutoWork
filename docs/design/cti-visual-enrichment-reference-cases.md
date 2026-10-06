# CTI : cas de référence pour les enrichissements visuels

Ce document fixe des exemples fonctionnels pour guider les futurs modèles et
tests. Il ne définit pas des images de référence pixel-perfect et ne décrit pas
un changement de comportement applicatif. Les informations représentées
doivent rester appuyées par les evidence handles admis; le modèle choisit le
support et le code local le rend. À données identiques, le rendu doit rester
déterministe. Le modèle ne fournit pas de D2, SVG, code graphique, URL ou image
comme preuve; une figure source doit déjà être collectée et archivée.

La question à poser pour chaque cas est :

> Quelle information le lecteur comprend-il mieux grâce à ce visuel ?

Le critère n’est pas « est-ce joli ? ». Une décision sans média reste correcte
si la prose répond déjà à la question.

## Règles d’arbitrage

Pour un besoin analytique donné, suivre cet ordre :

```text
figure source pertinente et disponible
  → graphique pour une information temporelle ou quantitative
  → diagramme pour des relations, architectures ou flux
  → tableau pour des correspondances structurées
  → rien si la prose suffit
```

- La représentation source prime quand elle explique déjà le même sujet et que
  le média est effectivement collecté, archivé et admissible. Ne pas reconstruire
  en D2 une chaîne déjà bien expliquée par une figure source.
- Deux enrichissements ne doivent pas répondre à la même question analytique.
  Un enrichissement supplémentaire n’est pertinent que s’il apporte une
  compréhension différente, elle-même étayée.
- Un graphique ordonne des mesures ou événements; un diagramme rend des liens
  ou un flux explicites; un tableau aligne des valeurs selon des dimensions
  communes. N’ajouter aucun de ces supports par quota.

## Cas A — chaîne BlueMoon : la figure source prime

- **Source :** bulletin CN, p. 5, [PDF de référence](../references/20261005_NP_TLP%20WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf).
- **Ce que montre le visuel :** la figure 1 suit l’opération depuis le leurre
  et l’URL contrôlée par l’acteur, au travers des étapes d’exploitation
  BlueMoon, jusqu’au shellcode et aux charges attribuées aux acteurs affichés.
- **Type retenu et pourquoi :** `FIGURE` si la figure source similaire est
  archivée et admissible. Elle montre déjà la chaîne; un diagramme D2 du même
  flux la répéterait.
- **Règle qui en découle :** même question sur la même chaîne + figure source
  disponible → garder la figure; ne pas ajouter un D2 redondant. Sans figure
  source admissible, un diagramme FLOW peut être proposé si les relations sont
  étayées.
- **Question guide :** « Quelle information le lecteur comprend-il mieux grâce
  à ce visuel ? » — les étapes de l’exploitation et leurs enchaînements.
- **Fixtures existantes :** [`test_fixture_a_source_figure_wins_over_duplicate_analytic_media`](../../backend/tests/test_production_editorial_enrichment_application.py)
  teste la priorité de la figure sur table, diagramme et graphique ayant la
  même question; [`test_bluemoon_archived_chain_is_catalogued_selected_and_published_from_same_blob`](../../backend/tests/test_production_editorial_enrichment_application.py)
  suit une figure BlueMoon archivée de son catalogue jusqu’à sa publication.

## Cas B — journalisation BlueMoon : une capture de code est informative

- **Source :** bulletin CN, p. 6, [PDF de référence](../references/20261005_NP_TLP%20WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf).
- **Ce que montre le visuel :** la figure 2 est une capture de code JavaScript
  qui journalise les états d’échec et de succès, les tentatives de nouvelle
  exécution et le renvoi du journal complet.
- **Type retenu et pourquoi :** `FIGURE` si cette capture source est archivée
  et admissible. Le code documente le comportement de journalisation et les
  consignes de diagnostic; c’est un élément technique porteur d’information,
  pas une image décorative.
- **Règle qui en découle :** juger une capture de code sur l’information
  vérifiable qu’elle apporte. Ne la remplacer par un résumé ou un diagramme que
  si celui-ci répond à une question différente et s’appuie sur les mêmes preuves.
- **Question guide :** « Quelle information le lecteur comprend-il mieux grâce
  à ce visuel ? » — quelles transitions et consignes le code journalise.
- **Fixtures existantes :** [`test_figure_proposal_selects_local_asset_with_the_model_written_caption`](../../backend/tests/test_production_editorial_enrichment_application.py)
  couvre la sélection d’un média source local. [`test_fixture_b_c2_without_source_figure_keeps_the_diagram`](../../backend/tests/test_production_editorial_enrichment_application.py)
  est un cas voisin sur le diagramme quand aucune figure source n’est disponible;
  il ne teste pas spécifiquement l’utilité d’une capture de code.

## Cas C — dates d’enregistrement par acteur : timeline

- **Source :** bulletin CN, pp. 46–48, figures 13–15, [PDF de référence](../references/20261005_NP_TLP%20WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf).
- **Ce que montre le visuel :** les dates de création des domaines racines sont
  placées sur un axe temporel, avec des séries/couleurs par acteur; les figures
  suivantes zooment sur les créations de septembre et montrent les autres
  acteurs.
- **Type retenu et pourquoi :** `CHART` de type timeline quand les dates et
  séries proviennent des preuves et qu’aucune figure source pertinente n’est
  disponible. Si les figures originales sont elles-mêmes collectées et
  admissibles, la priorité de la source s’applique : les conserver au lieu de
  produire un graphique duplicatif. Le support `ChartKind.TIMELINE` est celui
  introduit au LOT 3.
- **Règle qui en découle :** des dates comparables par acteur appellent un axe
  temporel; ne pas inférer d’activité entre les dates. Une figure source déjà
  explicite prime sur une reconstruction des mêmes points.
- **Question guide :** « Quelle information le lecteur comprend-il mieux grâce
  à ce visuel ? » — le regroupement temporel des enregistrements selon l’acteur.
- **Fixtures existantes :** [`test_fixture_c_domain_registration_wire_produces_and_roundtrips_one_chart`](../../backend/tests/test_production_editorial_enrichment_application.py)
  construit et sérialise une timeline à partir de dates de domaines;
  [`test_renders_three_series_with_fixed_editorial_palette`](../../backend/tests/test_analytic_chart_renderer.py)
  vérifie le rendu de trois séries. Le fixture applicatif emploie une série
  « Registered domains » plutôt que les acteurs du bulletin.

## Cas D — correspondance information / étape / identifiant : tableau

- **Source annoncée par le plan :** bulletin RU, pp. 14–15, [DOCX de référence](../references/20250331_NP_TLP%20WHITE_ChapsVision_Bulletin-APT-RU-2503.docx).
- **Ce que montrent effectivement ces pages :** l’extraction paginée du DOCX
  associe la p. 14 à une capture de métadonnées PDF et à du texte sur un autre
  PDF; la p. 15 contient des captures de messages du forum Albion et une URL de
  PDF. Ces pages ne montrent pas de tableau « information / étape /
  identifiant ».
- **Type visé par le cas fonctionnel :** `TABLE`, si les preuves donnent
  réellement des triplets comparables. Des colonnes information, étape et
  identifiant rendent alors chaque correspondance consultable sans réécrire la
  même prose.
- **Arbitrage et écart :** les pp. 14–15 citées ne justifient pas ce tableau.
  Pour ces pages, conserver les captures sources utiles comme figures; ne pas
  leur attribuer les correspondances du test. Le tableau reste un exemple
  fonctionnel synthétique jusqu’à correction de la référence du plan. Le texte
  extrait du DOCX ne permet pas d’identifier une autre page qui présenterait
  cette même correspondance.
- **Question guide :** « Quelle information le lecteur comprend-il mieux grâce
  à ce visuel ? » — quel type d’information correspond à quelle étape et à quel
  identifiant, lorsque ces trois valeurs sont toutes étayées.
- **Fixture existante :** [`test_fixture_d_structured_exfiltration_mapping_keeps_one_table`](../../backend/tests/test_production_editorial_enrichment_application.py)
  vérifie trois colonnes et deux correspondances à partir de faits de test
  synthétiques; il n’établit pas que ces faits figurent dans le bulletin RU.

## Cas E — relations entre page, fichier et URL

- **Source :** bulletin RU, p. 40 selon la pagination enregistrée dans le DOCX,
  [DOCX de référence](../references/20250331_NP_TLP%20WHITE_ChapsVision_Bulletin-APT-RU-2503.docx).
- **Ce que montre le visuel :** la page contient des captures de script HTML
  invoquant `ExecuteShellCommand` dans MMC et de commandes PowerShell; le texte
  adjacent décrit une URL distante et le téléchargement puis l’exécution d’une
  charge utile. Ce sont des captures de code, pas un diagramme relationnel
  déjà publié.
- **Type retenu et pourquoi :** `DIAGRAM`, profil `RELATIONSHIP`, si les
  evidence handles établissent les entités et chaque lien. Il rend explicites
  les relations entre la page HTML hébergée dans MMC, la commande, l’URL et le
  fichier téléchargé. Ne pas ajouter une arête que les captures et le texte ne
  démontrent pas.
- **Règle qui en découle :** choisir un diagramme quand la question porte sur
  les relations; une capture de code peut être informative, mais elle n’est pas
  en soi un diagramme de relations. Garder les deux seulement si elles
  répondent à des questions distinctes.
- **Question guide :** « Quelle information le lecteur comprend-il mieux grâce
  à ce visuel ? » — quelles entités sont liées et quel rôle joue chaque lien.
- **Fixtures existantes :** les tests [`test_inference_relation_is_explicit_and_kept_with_supporting_handles`](../../backend/tests/test_production_editorial_enrichment_application.py)
  et [`test_relation_without_endpoint_support_is_rejected_without_dropping_table`](../../backend/tests/test_production_editorial_enrichment_application.py)
  couvrent le support des extrémités et des relations. Le test
  [`test_real_typst_render_shows_diagram_and_archived_figure_captions`](../../backend/tests/test_typst_chp_parity.py)
  construit aussi des relations entre document, fichiers et infrastructure;
  [`test_real_d2_compiles_cti_profiles_and_printable_relationships`](../../backend/tests/test_d2_diagram_compiler_runtime.py)
  couvre la compilation du profil. Aucun de ces fixtures n’est lié à la source
  de la p. 40. Le fixture E nommé
  [`test_fixture_e_trivial_prose_accepts_nothing`](../../backend/tests/test_production_editorial_enrichment_application.py)
  documente en plus le choix valide de ne rien ajouter.

## Correspondance avec le code actuel

- [`EditorialMediaType`](../../backend/src/cti_app/domain/production_editorial_enrichment.py)
  est le vocabulaire interne de l’arbitre :
  `SOURCE_FIGURE`, `CHART`, `DIAGRAM`, `TABLE`, `NONE`. Ses valeurs sérialisées
  sont `source_figure`, `chart`, `diagram`, `table`, `none`.
- Les blocs wire correspondants sont `FIGURE`, `CHART`, `DIAGRAM` et `TABLE`;
  leur analyse est dans [`production_editorial_enrichment.py`](../../backend/src/cti_app/application/production_editorial_enrichment.py).
  Le schéma de proposition actuel est `EditorialEnrichmentProposalV1` avec les
  collections `figures`, `charts`, `diagrams`, `tables`, `annotations` et
  `resource_needs`. Les propositions analytiques exposent notamment le besoin
  (`purpose`), les données disponibles, le gain de compréhension, le périmètre,
  les evidence handles de la question et les limites de connaissance.
- Le type actuel pour le graphique demandé en C est
  [`ChartKind.TIMELINE`](../../backend/src/cti_app/domain/production_editorial_enrichment.py)
  (`timeline`). Les profils de diagramme du même fichier sont `FLOW`,
  `ARCHITECTURE` et `RELATIONSHIP`; le rendu timeline est dans
  [`analytic_chart_renderer.py`](../../backend/src/cti_app/infrastructure/analytic_chart_renderer.py).
- Les versions lues dans le code au moment de cette référence sont : schéma
  d’enrichissement `8`, politique active `editorial-enrichment-v10-editorial-tables`,
  prompt principal `editorial-enrichment-text-blocks-v17-editorial-tables`,
  contrat de proposition `editorial-enrichment-block-contract-v12-editorial-tables`,
  parseur wire `editorial-enrichment-wire-v12-editorial-tables`, prompt de
  réparation `editorial-enrichment-repair-v5-editorial-tables` et contrat de
  réparation `editorial-enrichment-repair-contract-v5-editorial-tables`; ces
  versions sont définies dans
  [`production_prompts.py`](../../backend/src/cti_app/application/production_prompts.py).
  Relever les versions dans le code avant toute mise à jour de cette référence.

## Vérification des sources et limites

Les pp. 5–6 et 46–48 du PDF CN ont été converties en images puis inspectées;
les numéros du pied de page correspondent. Le DOCX RU a été converti en texte
avec Pandoc. Ses marqueurs de pagination et les images incorporées des pages
14–15 et 40 ont été contrôlés; les captures pertinentes ont aussi été ouvertes.
La conversion complète du DOCX en PDF avec LibreOffice a échoué dans cet
environnement, donc les pages RU n’ont pas été relues comme des pages rendues
complètes.

L’écart matériel confirmé est le cas D : la source citée ne contient pas le
tableau décrit par le plan et le fixture D est synthétique. Pour E, la p. 40
contient du code avec des entités et relations exploitables, mais pas un
diagramme RELATIONSHIP préexistant. Aucun golden test visuel ni capture de page
n’est fourni par ce document.
