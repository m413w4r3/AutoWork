# M18 — Plan d'amélioration du rendu des ARTICLES (synthèse → enrichissement → assemblage → Typst)

Statut : plan prescriptif pour l'orchestrateur M18. Source : retours de l'utilisateur du 2026-11-08 sur la preview v8
(`~/Documents/bulletin-iran-2610-review/Bulletin-Iran-2610_preview_v8_partiel.pdf`) et analyse des références.
État de départ : `main` local `fbd4003a` (non poussé), stack déployée = HEAD, composition v8 (3 ARTICLES S3 `05c1d00e`,
S4 `1a53e50e`, S17 `5d312690` + 15 BRÈVES ; S13 `a65fec1d` différé). Journal/état M17 : `.autowork-runs/m17/`
(`MANIFEST.md`, `state.json`, `REVIEW.md`, `tools/`).

## 0. Références (autorité de format)

| Rôle | Chemin |
|---|---|
| PDF qualité CN | `docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf` (120 p.) |
| PDF qualité RU | `docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-RU-2609.pdf` (114 p.) |
| Sources brutes Typst | `docs/references/CN2609_typ`, `docs/references/RU2609_typ` (articles, `article.csv`, images) |
| Gabarits/format d'origine | `docs/references/chpTypst` : `TEMPLATES/article.typ`, `TEMPLATES/breve.typ`, `UTILS/helpers.typ` (`article`, `callout`, `styled-table`, `fn`, `ioc-list`, `noteAnalyste`…), `UTILS/colors.typ`, `UTILS/header_footer.typ` |
| Renderer actuel | `chpTypst/RENDERER/{publication,publication_helpers,edition,edition_helpers}.typ`, `chpTypst/UTILS/helpers.typ` |

Constats d'analyse des références (article CN `article01`, 17 figures, 8 `styled-table`, callouts, footnotes `#fn(url)`) :
- En-tête de page « Bulletin n°3 / Septembre 2026 », étiquette `ARTICLE` + numéro, titre `[Acteur] Titre` ;
- bloc **« Vue d'ensemble »** (callout) : *Victimologie / Arsenal / Objectif principal* (champ `overview` de `#article(...)`) ;
- puis **Synthèse** structurée en sections titrées (`= Aperçu`, `= … chaîne d'exploits …`, `== sous-sections`), nombreuses
  **figures légendées** (schémas de chaîne d'attaque, captures), **tableaux** (`styled-table`), **encadrés** (`callout`),
  extraits de code, références en **notes de bas de page** ;
- bloc **« IOC »** en fin d'article : sous-listes de valeurs brutes « Adresses IP : », « Noms de domaine : » (+ URL, Fichiers),
  **aucun** marqueur interne, **aucune** annexe tabulaire ;
- aucun bloc « provenance/SHA-256 » dans la mise en page livrable.

Écarts constatés dans notre rendu v8 (cf. `.autowork-runs/m17/composition/preview_v8.pdf`, p. 4-7 : S3) :
RÉFÉRENCES numérotées avec dates/contexte en tête de document ; CHRONOLOGIE en tableau + liste numérotée de non datés ;
blocs sans espacement vertical (paragraphe/titre/références collés) ; section « IOC de l'article » + « Pistes non confirmées » avec
marqueurs internes (« Indicateur : Documenté dans l'article · Relation : Candidat non confirmé : contextual indicator.; … · 1 , 2 ») ;
« ANNEXE TECHNIQUE — inventaire exhaustif des IOC » (tableaux, 20 à 60 pages) ; bloc « PROVENANCE DE PUBLICATION … SHA-256 » ;
peu ou pas de figures/tableaux/diagrammes (S3: 0 figure/diagramme/tableau ; S17: 2 tableaux seulement) ; synthèses trop prudentes,
peu techniques, en blocs de texte longs.

## 1. Exigences (reprises des remarques utilisateur — normatives)

R1. **Références** : liste à puces (`•`), une entrée par source (éditeur — titre — date — URL), pas de numérotation, pas de tableau.
R2. **Chronologie** : supprimer la section « Chronologie » actuelle (tableau + liste numérotée) ; la remplacer par une **frise
chronologique simple et lisible** (rendu Typst natif, déterministe, issu de la timeline canonique ; sélection des événements clés,
libellés courts, dates en français ; pas de frise vide si < 2 événements datés).
R3. **Espacement** : sauts de ligne/espaces verticaux cohérents entre paragraphes, entre paragraphe et titre, autour des figures,
tableaux, listes et de la section Références (cf. références).
R4. **IOC** : section intitulée simplement **« IOC »** (pas « IOC de l'article ») ; catégories : **Adresses IP, URL, Noms de domaine,
Fichiers** (hash + noms/chemins de fichiers sous « Fichiers ») ; valeurs brutes (défanguées pour affichage) en liste simple.
R5. **Aucun marqueur interne dans le livrable** : supprimer « Indicateur : … · Relation : … · n », statuts, « Candidat non confirmé »,
« contextual indicator », numéros de source parasites, bloc « PROVENANCE DE PUBLICATION » / hash. La provenance reste dans les
artifacts canoniques, les exports (CSV/ZIP IOC) et les diagnostics — jamais dans le PDF. Les « pistes non confirmées »
n'apparaissent pas comme section marquée : soit omises du PDF, soit regroupées sans marqueur (décision à documenter).
R6. **Annexe technique** : supprimer entièrement les tableaux « ANNEXE TECHNIQUE — inventaire exhaustif des IOC » du PDF article/bulletin.
R7. **Enrichissement** : produire nettement plus d'éléments enrichis par article quand les preuves le permettent : captures d'écran /
images sources archivées, tableaux (`styled-table` : TTP, capacités, infrastructure, chronologies techniques), diagrammes
(chaîne d'infection, infrastructure C2, flux protocolaire), graphiques, encadrés/extraits de code pertinents — légendés en français
avec attribution de la source. Objectif mesurable par article (à calibrer sur CN/RU) : ≥ 1 diagramme de chaîne/infrastructure dès
que ≥ 3 étapes documentées ; ≥ 2 tableaux ; toutes les figures sources pertinentes archivées (hors logos/chrome) ; pas d'ajout
gratuit sans preuve.
R8. **Fidélité de format** : se rapprocher de l'original `chpTypst` (en-tête/pied, étiquette ARTICLE+numéro, titre `[Acteur] Titre`,
« Vue d'ensemble », titres h1/h2 avec bon espacement, légendes « Figure n : … », notes de bas de page, polices HankenGrotesk,
couleurs `colors.typ`) ; parité visuelle à vérifier page à page contre CN/RU.
R9. **Qualité des synthèses** : techniques, centrées sur les éléments pertinents CTI/hunting, détaillées, riches, bien rédigées, en
français : contexte et victimologie, arsenal, chaîne d'attaque par étapes, infrastructure et protocole C2, persistance/évasion,
artefacts hôte/réseau, **pivots de détection/hunting** (chemins, clés de registre, mutex, user-agents, ports, motifs), limites
d'attribution **brièvement** formulées (sans noyer le texte), niveau de détail de CN/RU. Sections titrées, paragraphes courts à
moyens.

## 2. Contraintes non négociables

- **Persistant** : les changements doivent vivre dans la pipeline, les prompts, les contrats/versions et les templates Typst —
  pas dans un patch du livrable courant. Toute modification de comportement porte une version (policy/prompt/parser/renderer) et des tests.
- **Ne rien supprimer en amont** : `REFERENCES`, `EXTRACTION`, `RELEVANCE_PROJECTION` existants sont conservés et réutilisés.
  Seules les étapes **SYNTHESIS → EDITORIAL_ENRICHMENT → ASSEMBLY** (+ rendu) sont refaites. Utiliser
  `POST /api/subjects/{id}/production/reuse/invalidate {from_stage: "synthesis"}` puis `retry` stage `synthesis` ;
  jamais de nouveau batch/run qui recrée l'extraction ; jamais de DELETE en base/MinIO.
- Règles AGENTS.md : G2/G3, TLP, `external_llm_allowed`, pas de secrets dans prompts/logs, pas d'acquisition live non autorisée
  (les images/captures sources s'obtiennent via les blobs déjà archivés ou les mécanismes existants ; tout besoin de téléchargement
  supplémentaire → demander une autorisation à l'utilisateur), pas de push/merge distant/publication/release. Bridge : un seul appel
  modèle à la fois (relais mono-thread), pas de JSON forcé ; relancer via réconciliation (`probe`) avant toute resoumission.
- Ne pas casser les BRÈVES ni les exports : le renderer partagé doit rester compatible ; les BRÈVES profitent des corrections de
  mise en page (espacement, marqueurs internes, annexe) sans nouvelle synthèse sauf demande explicite.
- Ne pas copier le contenu des références dans les prompts ; en extraire un **guide de style/structure** abstrait.

## 3. Architecture de la solution (décisions à valider au lot 0)

1. **Contrat canonique** : ajouter/versionner dans le document de publication ARTICLE (PublicationDocument V5/V6, voir
   `docs/publication_document_v6.md`, `publication_builder.py`, `typst_rendering.py::project_publication_to_typst_model`) :
   `overview` (victimologie, arsenal, objectif principal — issus de la synthèse, grounded), `timeline_key_events` (sélection
   déterministe pour la frise), IOC regroupés par catégorie **sans** statuts affichables, figures/diagrammes/tableaux déjà supportés.
   Évolution JSON versionnée (pas de migration DB sauf nécessité prouvée → ADR-0007, jamais `0001_baseline`).
2. **Synthèse** (`production_prompts.py` synthesis-draft-v13, `production_synthesis.py`) : nouveau prompt ARTICLE (v14+) avec guide de
   style CN/RU, structure imposée (Vue d'ensemble puis sections titrées), exigences de détail technique/hunting ; contrôles de
   grounding existants conservés (dates, valeurs techniques, handles) ; budgets de longueur adaptés ; BRIEF inchangé.
3. **Enrichissement** (`production_editorial_enrichment.py`, `diagram_compilation.py`, `analytic_chart_compilation.py`,
   `source_figure_inventory.py`, `figure_plan_projection.py`) : revoir politique/prompt/seuils pour produire plus de tableaux,
   diagrammes, graphiques et figures sources ; vérifier pourquoi S3/S4/S17 ont 0–2 éléments (rejets de l'inventaire : logos,
   images non archivées « pending », limites) ; D2 disponible dans le conteneur ; style des diagrammes (`docs/diagram-rendering-style.md`).
4. **Assemblage/rendu** : `RENDERER/publication.typ` + `publication_helpers.typ` (+ `edition*.typ`) réalignés sur `chpTypst`
   (header/footer, étiquette, « Vue d'ensemble » en callout, titres/espacements, frise native, références à puces, bloc IOC simple,
   suppression annexe/provenance). Le hash du template change l'identité des rendus (pas de version supplémentaire requise, à vérifier).
5. **Exports** : CSV/ZIP IOC (`edition_ioc_export.py`) gardent le détail (statuts/provenance) hors PDF.

## 4. Lots, propriété des fichiers et dépendances

Chaque lot : 1 sous-agent **Codex GPT-6 Luna xhigh** (Paseo, worktree isolé, branche `m18/<lot>`), contrat de sortie : fichiers modifiés,
tests exécutés (résultats réels), risques. Pas de chevauchement de fichiers entre lots parallèles ; l'orchestrateur intègre
(`git merge` local), déploie (`make up`) et vérifie. Fresh agent par correction (ne jamais re-prompter un enfant terminé).

| Lot | Contenu | Fichiers principaux | Dépend de |
|---|---|---|---|
| L0 | **Spec de format** : analyse CN/RU + `chpTypst` → `docs/mission18/FORMAT_SPEC.md` (éléments, espacements, tailles, textes fixes) + checklist de parité + inventaire des écarts + décision contrat canonique (§3.1) ; lecture seule | docs/ | — |
| L1 | Renderer Typst : R1–R6, R8 (espacement, références à puces, frise, IOC simple, marqueurs/annexe/provenance supprimés, vue d'ensemble) + tests Typst réels (recette `typst-snap-tmpdir`) | `chpTypst/RENDERER/*`, `chpTypst/UTILS/*`, tests Typst | L0 |
| L2 | Contrat/projection : champs `overview`, `timeline_key_events`, IOC par catégorie, projection Typst, version(s) ; builder | `publication_builder.py`, `publication_document*.py`, `typst_rendering.py`, `edition_typst_rendering.py`, tests | L0 (avant L1 pour le schéma ; L1/L2 s'accordent sur le JSON de rendu) |
| L3 | Prompt/contrat de synthèse ARTICLE (R9) + `overview` + tests de prompt/contrat | `production_prompts.py`, `production_synthesis.py`, tests | L0, L2 |
| L4 | Enrichissement (R7) : politique, prompts, seuils, inventaire de figures, diagrammes/tableaux/graphiques | `production_editorial_enrichment.py`, `source_figure_inventory.py`, `diagram_compilation.py`, `figure_plan_projection.py`, tests | L0, L2 |
| L5 | Revue indépendante (Luna xhigh, lecture seule) de L1–L4 : sécurité (G2/TLP), identités/hash/reuse, régressions BRIEF/exports | — | L1–L4 |

Parallélisme : L0 puis (L2) puis L1 ‖ L3 ‖ L4 puis L5. Un seul lot touchant Bridge/prod à la fois.

## 5. Régénération et validation (après intégration + déploiement)

1. Ordre : S3 (`05c1d00e`), S4 (`1a53e50e`), S17 (`5d312690`) — un sujet à la fois. Pour chacun : `reuse/invalidate from synthesis`,
   `retry synthesis` ; suivre synthesis → enrichment → assembly ; réconciliation `probe` si ambigu ; reprendre aux erreurs (≤ 3 essais
   utiles/défaut, sinon diagnostic + escalade code).
2. QA PDF page à page contre CN/RU (checklist L0) : frise lisible, références à puces, espacements, IOC simple, absence de
   marqueurs/annexe/provenance, nombre de figures/tableaux/diagrammes, lisibilité des figures, qualité rédactionnelle (technique,
   hunting, français). Corrections ciblées via les endpoints de révision Bridge (`synthesis/revisions[/preview]` : rewrite_paragraph,
   insert_paragraph, rewrite_section, rewrite_table, create/rewrite_diagram, replace_source_figure, rewrite_title…) ; bug de rendu →
   sous-agent Typst, pas Bridge.
3. Recomposition : `python3 .autowork-runs/m17/tools/compose.py apply` (pins exacts, ARTICLES avant BRÈVES, numérotation continue) →
   preview bulletin → copie dans `~/Documents/bulletin-iran-2610-review/` (nommer `…preview_v9…`) + mise à jour `REVIEW.md`.
4. Vérifier que les BRÈVES et l'aperçu édition ne régressent pas (diff visuel avant/après) et que S13 (en attente de documents
   utilisateur) bénéficiera du nouveau pipeline à sa production.
5. Rapport final : lots, commits, tests réellement exécutés, artefacts/PDF (sha256), écarts restants vs CN/RU, décisions à valider.

## 6. Critères d'acceptation (oracle)

- Aucun des motifs suivants dans le PDF d'un article : « PROVENANCE DE PUBLICATION », « Indicateur :», « Relation :», « contextual
  indicator », « ANNEXE TECHNIQUE », « Candidat non confirmé », « IOC de l'article ».
- Section Références en puces ; frise présente (≥ 2 événements datés) ; section « IOC » avec catégories Adresses IP / URL /
  Noms de domaine / Fichiers ; espacements conformes (mesures `pdftotext -bbox`).
- Chaque article : « Vue d'ensemble » + ≥ 4 sections titrées, ≥ 1 diagramme (si ≥ 3 étapes), ≥ 2 tableaux, figures sources pertinentes
  présentes, légendes FR ; synthèse ≥ 1 500 mots (à calibrer) avec section pivots de détection/hunting.
- Extractions/références/projections existantes intactes (hash inchangés) ; artifacts SYNTHESIS/ENRICHMENT/PUBLICATION nouvelles versions.
- Tests : modules touchés verts hors-ligne ; tests Typst réels sur les nouveaux gabarits ; `ruff` ; aucune régression BRIEF/exports.
