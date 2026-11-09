# M19 — Corrections de format ARTICLES + BRÈVES (suite de M18)

Statut : plan prescriptif pour l'orchestrateur M19 (nouvelle session Sonnet 5.5 High pilotant des sous-agents
GPT-6 Luna xhigh). Source : retours de l'utilisateur du 2026-11-09 sur la preview v9
(`~/Documents/bulletin-iran-2610-review/Bulletin-Iran-2610_preview_v9_partiel.pdf`, composition v11).
État de départ : `main` local `b54e136c` (non poussé), stack déployée = HEAD, 18 sujets inclus (3 ARTICLES S3 `05c1d00e`,
S4 `1a53e50e`, S17 `5d312690` ; 15 BRÈVES), S13 `a65fec1d` différé. Journal/état M18 : `.autowork-runs/m18/`
(`REVIEW.md`, `MANIFEST.md`, `JOURNAL.md`, `REGISTRY.md`, `tools/`). Spec de format M18 : `docs/mission18/FORMAT_SPEC.md`
(mesures CN/RU en §11) ; plan M18 : `docs/mission18/M18_RENDER_IMPROVEMENT_PLAN.md`.

## 0. Références (autorité de format)

| Rôle | Chemin (lecture seule, gitignoré : absent des worktrees → lire sous `/home/nill/perso/work/AutoWork/docs/references/`) |
|---|---|
| PDF qualité CN | `docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf` (120 p.) |
| PDF qualité RU | `docs/references/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-RU-2609.pdf` (114 p.) |
| **Sources brutes Typst** | `docs/references/RU2609_typ/`, `docs/references/CN2609_typ/` (`articles/*/*.typ`, `breves/`, `UTILS/helpers.typ`) — à lire pour observer précisément : gras des noms d'outils (liste `bold-words` + regex `bold-regex` en tête de chaque article), éléments techniques en `` `raw` `` surlignés gris, italique des mots anglais/`_spearphishing_`, `#fn(url)` pour les notes de bas de page, `events:` (chronologie), `overview:`, `ioc-list` |
| Gabarits | `docs/references/chpTypst/{TEMPLATES,UTILS,RENDERER}` ; renderer actuel : `chpTypst/RENDERER/{publication,publication_helpers,edition,edition_helpers}.typ`, `chpTypst/UTILS/*` |

## 1. Exigences (normatives — retours utilisateur du 2026-11-09)

### 1.1 ARTICLES (et BRÈVES quand précisé)
A1. **Section « Références » en PREMIER** dans l'article (avant Chronologie/Vue d'ensemble/Synthèse). Format de chaque entrée :
    **`[date] : [phrase de présentation de la référence]`** (date en français, ex. « 14 octobre 2026 : … ») **+ le lien en note
    de bas de page** (`#fn(url)` du gabarit `chpTypst/UTILS/helpers.typ`, avec les bonnes couleurs/formats de `colors.typ`,
    exposants et notes numérotées comme CN/RU). Plus d'URL en clair dans le corps de l'entrée. La phrase de présentation est la
    « note de source » déjà produite par la synthèse (SOURCE NOTES, ≤ 260 car., sans date) ; la date vient de `published_at`.
A2. **Aération** : sauts de ligne/espacement vertical plus marqués entre paragraphes (articles ET brèves) — cf. références
    (paragraphes séparés visiblement, ≈ 1 ligne vide ; mesurer sur CN/RU avec `pdftotext -bbox-layout` et reproduire).
A3. Section **IOC › Fichiers** : **n'afficher que les HASH** (pas les noms/chemins de fichiers). Les noms/chemins restent dans le
    canonique et les exports.
A4. **Refanging à l'affichage** : tous les IOC présentés dans l'article/brève sont affichés **sous forme rétablie (refangée)**
    (`afterworld.store`, `http://…`, `1.2.3.4`), jamais défangés (`[.]`, `hxxp`). Concerne la section IOC ET le texte (codes inline).
    (Rappel : les secrets/IOC CTI sont publiés en clair, voir mémoire `cti-secrets-publish`.)
A5. **Typographie sémantique comme CN/RU** : noms d'outils/malwares/logiciels **en gras** ; éléments techniques (chemins,
    commandes, clés de registre, noms de fichiers, valeurs, ports, user-agents, hashes inline…) **surlignés en gris**
    (style `raw` des références) ; noms propres et mots/termes anglais **en italique** (usage CN/RU : `_spearphishing_`).
    Doit être produit par le pipeline (annotations sémantiques existantes `semantic_annotation_*` / rôles de termes,
    `production_prompts.py` SEMANTIC_ANNOTATION_*, `typst_rendering.py` projection des spans, `publication_helpers.typ`) — pas par un
    patch du livrable.
A6. **Notes internes sous les figures supprimées** : plus de « Documenté, non observé — visuel reproduit depuis une source. »,
    ni « Source : … », ni « Attribution : … » sous les figures/diagrammes (restent dans le canonique/diagnostics).
A7. **Légendes « Figure X : … » centrées**.

### 1.2 BRÈVES
B1. **Référence non exploitée** : dans la brève « Bogus Bylines » (S16 `543a95cd`, PUBLICATION `d6e603d9…` v2), l'article
    `https://openai.com/index/disrupting-ai-enabled-false-front-operations/` n'est pas utilisé. Diagnostiquer pourquoi (la brève n'a-t-elle
    pas d'étape REFERENCES/EXTRACTION complète ? sources de découverte ? tier CORE/SUPPORTING ?) et **ajouter/rendre systématique une
    étape de recherche de références + extraction pour les brèves** (persistant dans la pipeline : workflow `production_workflow.py`,
    `production_references.py`, `production_extraction.py`, routage BRIEF `production_routing.py`), sans casser les brèves existantes.
B2. **Enrichissement des brèves** quand pertinent et possible (images/captures archivées, graphiques, tableaux, diagrammes) —
    pipeline d'enrichissement pour BRIEF (`production_editorial_enrichment.py`, plafond de figures BRIEF actuel = 3, policy BRIEF) ;
    jamais d'ajout sans preuve ; aucune acquisition live non autorisée (demander l'autorisation à l'utilisateur via AskUserQuestion si des images
    pertinentes sont seulement « pending »).
B3. Les brèves suivent le **même format sémantique** que les articles (A5 : gras outils, gris technique, italique noms propres/anglais).
B4. **Frise** (même rendu que les articles) pour la chronologie des brèves (remplace la chronologie legacy en liste).
B5. Mêmes **sauts de ligne** entre paragraphes (A2).

### 1.3 ARTICLES + BRÈVES
C1. **Chronologie à un seul événement** : si la chronologie ne contient qu'UN élément (la date de publication de l'article/brève est
    un élément de chronologie), **supprimer la section Chronologie** au profit de la seule section Références (la date est portée par
    la référence). Seuil d'affichage de la frise : ≥ 2 événements distincts, la date de publication comptant comme événement.
    Attention : cela change la règle L2 « frise ≥ 2 événements datés issus de la timeline canonique » (`select_article_timeline_key_events`,
    `publication_document.py`, `publication_builder.py`) et l'ajout de la date de publication comme événement.

### 1.4 Pipeline synthèse
D1. **Supprimer complètement le plancher de 1 500 mots et l'exigence de 4 sections** (`ARTICLE_SYNTHESIS_MIN_WORDS`,
    `article_structure:word_floor…`, contrôle « ≥ 4 sections h2 », textes du prompt ARTICLE v15 et du tour correctif structure,
    validation des révisions ciblées L3b, tests). Conserver la section « Vue d'ensemble » (3 libellés) et les autres contrôles de grounding.
    Le prompt peut encore viser un traitement détaillé/technique, sans seuil chiffré imposé. Versions de prompt/contrat/validateur à incrémenter ;
    identités ARTICLE seulement.

## 2. Contraintes non négociables
- **Persistant** : tout vit dans pipeline, prompts, contrats versionnés, templates Typst, tests — pas de patch du livrable. Chaque changement
  de comportement porte une version et des tests (y compris un test qui échoue si l'écart réapparaît : ex. test PDF `pdftotext` des motifs).
- Règles `AGENTS.md` : G2/G3, TLP, `external_llm_allowed`, pas de secrets dans les prompts/logs, pas d'acquisition live non autorisée, pas de
  push/merge distant/publication/release/avis analyste, jamais de DELETE en base/MinIO. Bridge : UN seul appel modèle à la fois.
- Ne pas refaire REFERENCES/EXTRACTION/RELEVANCE_PROJECTION des articles ; régénérer seulement SYNTHESIS → ENRICHMENT → ASSEMBLY
  (`reuse/invalidate from synthesis` puis `retry synthesis`). Pour les BRÈVES : nouvelle étape références/extraction (B1) — décider du périmètre
  (au minimum S16 ; idéalement les 15 brèves) en pesant le coût Bridge ; ne pas casser les brèves existantes ; documenter.
- Les avis d'analyste (opinion V6/V7, draft preview) restent inchangés.
- Ne pas copier le contenu éditorial de CN/RU dans les prompts ; en extraire des règles abstraites de style.

## 3. Méthode d'orchestration (leçons M18 — à respecter)
- Orchestrateur = **Sonnet 5.5 High** (cette session) ; implémenteurs/relecteurs = **Luna xhigh** (`codex/gpt-6-luna`, `settings={modeId:"auto-review",
  thinkingOptionId:"xhigh"}`) via Paseo : `create_workspace(isolation=worktree, mode=branch-off, baseBranch=main, branchName=m19/<lot>)` puis
  `create_agent(workspaceId=…)`. **Jamais de repli Sonnet pour l'implémentation** (consigne utilisateur) : si Luna atteint sa limite d'usage
  (`try again at HH:MM`), commiter le WIP, attendre la reprise, relancer un NOUVEL agent Luna (jamais re-prompter un agent terminé).
- Un lot = une tâche, un SHA de base, chemins exacts, oracle de test, rapport (fichiers, tests réels, risques, impact hash/identité/reuse).
  Pas de chevauchement de fichiers entre lots parallèles. Vérifier chaque diff ; intégrer par `git merge --no-ff` local ; **la suite complète
  doit être lancée par l'orchestrateur sur l'hôte** (elle se fige dans les sandbox Luna) :
  `cd backend && export TYPST_BINARY=$HOME/.local/share/autowork-tools/typst PATH=$HOME/.local/share/autowork-tools/bin:$PATH TMPDIR=$HOME/typst-tmp FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst; /usr/bin/python3.12 -m pytest tests -q --tb=line --ignore=tests/integration --continue-on-collection-errors -p no:cacheprovider --basetemp=/tmp/m19-pt`
  Attendu : seuls 30 échecs préexistants (rule_workbench, yara_runner, investigation_*_tools, signature_bridge, rule_corpus_import, advisor_checkpoint,
  archive_commands, logging, collection_api, selection_api, network_capture_import, supervised_first_run_preflight, … vérifiés identiques sur
  `4efb43ad`) + 48 erreurs de collecte `pefile` ; comparer à la liste de base (`git stash`/worktree base) pour détecter toute régression. Supprimer
  ensuite `--basetemp` et `~/typst-tmp/pytest-of-nill`.
- Worktrees Luna : copier `chpTypst/CascadiaCode` et `chpTypst/HankenGrotesk` (gitignorées) dans le worktree avant les tests Typst réels ;
  imposer `--basetemp=/tmp/m19-<lot>/pt` et le nettoyage des temporaires (en M18, 38 Go de fichiers temporaires ont rempli le disque :
  surveiller `df -h /` avant chaque lot, nettoyer `/tmp/m19-*` si < 10 Go). Ne jamais lancer de prune destructif autre que `make docker-gc`.
- Déploiement `make up` UNIQUEMENT quand aucune étape de production ne tourne (il peut échouer une fois sur un endpoint réseau orphelin :
  relancer, `stack-clean-stale` s'en charge). Vérifier ensuite la version dans le conteneur.
- API : toujours via `/tmp/aw METHOD PATH [json]` (outils M17/M18 à recopier : `cp .autowork-runs/m17/tools/* .autowork-runs/m18/tools/* /tmp/ && chmod +x /tmp/aw /tmp/*.sh`).
  Édition `58ee905c-59af-4699-93f1-a5fefe94361a`. Lire les `subject_id` sur le board, ne pas les écrire de mémoire.
  Régénération : `POST /api/subjects/{id}/production/reuse/invalidate {"from_stage":"synthesis"}` puis `…/production/retry {"stage":"synthesis"}`
  (`/tmp/watch.sh SUBJECT SECS` suit l'état ; `/tmp/hashes.sh SUBJECT` donne les hashes REFERENCES/EXTRACTION/RELEVANCE/SYNTHESIS/ENRICHMENT ;
  `/tmp/title2.sh SUBJECT "instruction"` = rewrite_title Bridge + assemblage). `retry editorial_enrichment` seul est désormais possible (route réadoptée, M18).
  Après modification des artifacts : `python3 .autowork-runs/m18/tools/upd_state.py <subject_ids…>` (met à jour `.autowork-runs/m17/state.json`)
  puis `python3 /tmp/compose.py apply` ; preview : `GET /api/editions/{E}/composition/preview` (hash) puis `GET /api/editions/{E}/preview/pdf?preview_input_hash=…`.
- Diagnostic des rejets de synthèse : sortie brute du modèle lisible via `model_runs.raw_output_reference` → blob MinIO (`tools/readblob.py` exécuté dans le conteneur worker),
  jamais le dossier `var/`.
- Erreurs récurrentes rencontrées : `synthesis_unknown_date` (dates ISO non étayées → tour correctif multi-violations déjà en place), `plain_text_violation`
  (backticks/`<!--` → tour correctif localisé en place), `publication_qa_failed` (QA `publication_qa.py` : `reader_ioc_projection` rejette toute clé `sha256`/`_hash`/provenance
  du JSON de rendu lecteur sauf `sha256` des blocs figure), `assembly_error` « ArticleFormat IOC … evidence » (appariement IOC↔preuve, `publication_builder.build_article_format_v1`),
  colonnes `varchar(64)` (versions de prompt ≤ 64 car. : test `tests/test_prompt_version_lengths.py`), Bridge « composer Temporary Chat introuvable » (réessayer / prévenir l'utilisateur).
  Règle : ≤ 3 essais par défaut, puis diagnostic + correctif de fond.
- Ne pas terminer le tour avant le rapport final ; attendre les agents avec `timeout 590 bash -c 'while …; do sleep 20; done'` (les `sleep` longs sont bloqués).
  Handoff vers une NOUVELLE session si le contexte devient lourd (voir `.autowork-runs/m18/HANDOFF.md` pour le modèle).

## 4. Lots proposés (à affiner par l'orchestrateur après un lot 0 de lecture)
| Lot | Contenu | Fichiers principaux | Dépend de |
|---|---|---|---|
| M0 | Spec de format v2 (lecture seule) : observer `RU2609_typ`/`CN2609_typ` + PDF (gras/gris/italique, `#fn`, espacement, légendes centrées, frise) ; diagnostic code : où naissent « Documenté, non observé… », la liste `Références` actuelle, les spans sémantiques (annotations) pour articles ET brèves, le chemin BRIEF références/extraction/enrichissement, pourquoi S16 n'utilise pas la source OpenAI | `docs/mission19/FORMAT_SPEC_V2.md` | — |
| M1 | Rendu Typst : A1 (Références en premier, format `date : phrase` + `#fn`), A2, A3, A6, A7, A5 (styles gras/gris/italique), B4 (frise brèves), C1 (affichage) | `chpTypst/RENDERER/*`, `chpTypst/UTILS/*`, tests Typst | M0 |
| M2 | Projection/contrat : refanging des IOC et du texte (A4), IOC fichiers = hashes seulement (A3), date de publication comme événement + règle C1, phrase de présentation des références (A1), suppression des notes de figure du JSON lecteur (A6) ; versions v34/V9 si nécessaire | `publication_builder.py`, `publication_document.py`, `typst_rendering.py`, `edition_typst_rendering.py` | M0 |
| M3 | Synthèse/annotations : D1 (suppression plancher mots + 4 sections, prompt/validateur/tour correctif/révisions), annotations sémantiques complètes (outils gras, technique gris, italique nom propre/anglais) pour ARTICLE et BRIEF | `production_prompts.py`, `production_synthesis.py`, `production_synthesis_revision.py`, `production_semantic_annotation*.py` | M0 |
| M4 | BRÈVES : B1 (recherche de références + extraction), B2 (enrichissement), B3 | `production_workflow.py`, `production_references.py`, `production_extraction.py`, `production_routing.py`, `production_editorial_enrichment.py` | M0 |
| M5 | Revue indépendante Luna xhigh lecture seule (G2/TLP, identités/hash/reuse, BRIEF/exports/avis) | — | M1–M4 |
| Régénération | S3→S4→S17 (synthesis → enrichment → assembly) ; brèves selon M4 (S16 Bogus Bylines en premier) ; QA PDF page à page vs CN/RU ; corrections Bridge ciblées ; recomposition (pins exacts, ARTICLES avant BRÈVES) ; preview v10 dans `~/Documents/bulletin-iran-2610-review/` (nommer `…preview_v10_…`) + `REVIEW.md` | — | M1–M4 déployés |

## 5. Critères d'acceptation (oracle)
- PDF d'un article : « Références » AVANT toute autre section ; entrées `date : phrase` avec exposant de note ; notes de bas de page avec URL ; aucune URL dans le corps de l'entrée.
- Aucun IOC défangé (`[.]`, `hxxp`, `[:]`) dans le PDF ; « IOC › Fichiers » = uniquement des hashes (pas de nom/chemin) ; catégories Adresses IP / URL / Noms de domaine / Fichiers.
- Plus aucune ligne « Documenté, non observé », « Source : », « Attribution : » sous les figures ; légendes « Figure n : » centrées.
- Espacement inter-paragraphes vérifié par bbox (valeur calibrée sur CN/RU, documentée) pour articles ET brèves.
- Outils en gras, éléments techniques surlignés gris, noms propres/anglais en italique : vérifiés par test sur les fontes/objets PDF (pdffonts/bbox) et par inspection visuelle contre CN/RU, articles ET brèves.
- Brève « Bogus Bylines » : l'article OpenAI figure dans les références et nourrit la synthèse ; chronologie des brèves en frise ; chronologie à 1 seul événement supprimée (articles et brèves).
- Plancher de 1 500 mots et de 4 sections supprimés (code, prompt, tests) ; la Vue d'ensemble reste.
- REFERENCES/EXTRACTION/RELEVANCE_PROJECTION des articles intacts (hashes identiques) ; nouvelles versions SYNTHESIS/ENRICHMENT/PUBLICATION ; suite hôte = uniquement les échecs préexistants ; ruff.
- Rapport final : lots/commits/tests réels, hashes artifacts/PDF, écarts restants vs CN/RU, décisions à valider, autorisations en attente.

## 6. Décisions M18 encore ouvertes (à rappeler à l'utilisateur dans le rapport)
- TLP:RED peut atteindre un modèle externe si `external_llm_allowed=true` (antérieur à M18 ; édition courante = CLEAR) — durcir `production_access_policy.py` ?
- S13 (`a65fec1d`, TTP) attend 4 documents (DOJ usao-sdny + CISA AA22-264A, AA24-241A, AA23-335A).
