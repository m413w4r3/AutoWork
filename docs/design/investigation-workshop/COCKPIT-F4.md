# F4-D — Cockpit d’enquête M24 : manques (a)–(h), conception et découpage

Statut : **conception ; lots B1–B7 et F1–F6 livrés ; F7a (étapes 1–11) livré, F7b
(étapes 12–24) en cours**. Base de conception : `7e4a0e5e` (branche
`m24-1/f4d-cockpit-design`). Tête Alembic livrée à `1c88ac5b` : `0029_note_anchor_identity`
— les pins d'ancre **et** `content_blocks` sont dans `0029` ; le lot B5a n'a pas créé de
`0030`. Légende : **[V]** vérifié dans le code à la base de conception ; **[I]** inférence de
conception. Les chemins sont cités `fichier:ligne`.
Sauf mention [V], les routes, schémas et noms de lots proposés ci-dessous ont été livrés ; les
écarts constatés sont signalés dans l'encadré ci-dessous.

**État livré (2026-11-23).** B1 (`0028_task_context_consultations`), B2/B3
(`api/investigation_task_consultations.py`, `context_task_id` sur `POST /plans/request`),
B4+B5a (`0029_note_anchor_identity`), B5b (rendu Typst) et B5c (blocs de Note dans les
éditions du bulletin), B6
(`GET …/agent-sessions/{sid}/summary`, `api/agent_sessions.py:474`) et B7
(`application/investigation_timeline.py:88-91`, sources handoff/contribution/session/instruction)
sont livrés ; côté frontend, F1 (`tasks/TaskBridgePanel.tsx`), F2 (`opinion/AnchorPicker.tsx`),
F3 (`opinion/NoteContentBlocks.tsx`), F4 (`tasks/OperatorsPanel.tsx`), F5
(`overview/EnvelopeLifecycleSection.tsx`) et F6 (`tasks/TimelinePanel.tsx`) sont livrés.
F7 est scindé : **F7a** livre les étapes 1–11 (`make test-investigation-browser`,
`Makefile:648`) ; **F7b** couvre les étapes 12–24 et reste ouvert.

## 1. Inventaire vérifié des manques

| # | Backend existant | Frontend existant | Ce qui manque exactement |
|---|---|---|---|
| **a** | `advisor.consult` unique ([V] `application/investigation_advisor_tools.py:740-1000`) ; plan via `POST /plans/request` ([V] `api/investigation_plans.py:395-458`) ; propositions de Notes avec `task_id` déjà accepté ([V] `api/opinion_note_proposals.py:59-83,311-355`) ; registre `GET /consultations` ([V] `api/investigation_plans.py:371-392`, DTO `application/investigation_consultations.py:95-124`). | Demande Bridge seulement dans la vue Avis ([V] `opinion/OpinionNotesView.tsx:144-164`, `opinion/BridgeRequestPanel.tsx:143-345`) ; la carte tâche n’offre aucune action Bridge ([V] `tasks/TaskCard.tsx:93-330`). | Aucune route consultative *task-scoped* ; l’attribution humaine `tool_invocations.task_id` est interdite sans session ([V] check `(task_id IS NULL) = (agent_session_id IS NULL)` `models/investigation.py:91-93`, `application/operator_tool_invocation.py:1157-1159`). |
| **b** | Ancres canoniques calculées par `publication_renderable_body_anchors` ([V] `domain/publication_document.py:1272-1312`) ; exposées dans `ArticleSnapshot.article.publication_anchors` ([V] `application/investigation_article_snapshot.py:280-311`) ; validées à la demande ([V] `application/opinion_note_proposals.py:1116-1133`) et à l’acceptation/approbation ([V] `application/analyst_opinion_service.py:1311-1316,1784-1792`). | Saisie libre + datalist des ancres déjà présentes dans l’avis ([V] `opinion/BridgeRequestPanel.tsx:152-173`) ; l’UI dit explicitement que le contrat n’expose pas les `publication_anchors` ([V] `:170-172`). | Aucune route de lecture des ancres ; le panneau valide contre le **dernier** snapshot alors que l’acceptation valide contre la **base épinglée** de l’avis ([V] `opinion_note_proposals.py:430,1116` vs `analyst_opinion_service.py:1265-1316`) ; la proposition ne pin pas la publication de base ni le hash du paragraphe. |
| **c** | Rendu Typst des blocs `table`/`diagram`/`figure`/`chart` ([V] `chpTypst/RENDERER/publication_helpers.typ:1039-1078` ; builders `application/typst_rendering.py:4508,4566`) ; code en clôture et littéraux techniques dans les Notes ([V] `typst_rendering.py:1757-1853`, test `tests/test_typst_rendering.py:1165-1180`) ; parseur de tableaux pipe pour les **blocs d’avis** ([V] `typst_rendering.py:1880-1935,4185`) ; M23 : insertion/édition visuelle, légende, remplacement, retrait ([V] `frontend/src/features/editorial-workspace/{insertionOperations.ts,VisualActionBar.tsx,CaptionEditor.tsx,ReplaceFigureDialog.tsx,RemoveElementDialog.tsx,ReorderElements.tsx}`). | La revue de Note annonce que tableaux/figures sont rendus seulement par l’aperçu PDF ([V] `opinion/NoteProposalReview.tsx:340-344`) ; aucune sélection d’élément vérifié dans la proposition. | Les Notes ne portent ni référence typée vers un élément M23, ni extraction de code/IOC liée à une observation ; `notes_by_anchor` n’ajoute pas `pipe_table` ([V] `typst_rendering.py:2871-2902`). |
| **d** | Instructions persistées et lisibles par session ([V] `application/agentic_sessions.py:914-921`, `infrastructure/database/repositories/agent_sessions.py:446-470`) ; drain backend ([V] `api/main.py:228-253`) ; invocations attribuées à `agent_session_id` ([V] `models/investigation.py:111-130`). | Panneau opérateurs affiche « Opérations : Non exposées » et « Budget : Non exposé » ([V] `tasks/OperatorsPanel.tsx:184-191`) ; file d’instruction seulement affichée à l’enqueue ([V] `:114-120`). | Aucune lecture humaine des instructions ni résumé ops/budget/état broker/dernier handoff par session ; `handoff_id` existe déjà ([V] `domain/agentic_operator.py:881`, `api/agentSessions.ts:81`). |
| **e** | `extend`/`revoke` existent ([V] `api/investigation.py:2537-2642`, service `application/investigation_governance.py:308,469`) ; hooks front existent ([V] `frontend/src/api/investigationGovernance.ts:192-210,313-322`). | Panneau complet mais **legacy** ([V] `investigation/InvestigationGovernancePanel.tsx:329-368`), monté seulement dans `InvestigationLabViews` ([V] `InvestigationLabViews.tsx:989`) ; le cockpit M24 remplace ces vues en `bridge_only` ([V] `InvestigationTab.tsx:359-370`). | Le cockpit n’offre que l’approbation initiale bootstrap ([V] `overview/EnvelopeSection.tsx:45-50,152-199`). |
| **f** | Projection timeline sur événements/décisions/observations/invocations/plans ([V] `application/investigation_timeline.py:65-74,330-395`) ; protocole d’extension prévu ([V] `:110-112`). | Types/labels figés aux six item types ([V] `frontend/src/features/investigation/api/tasks.ts:205-216`, `tasks/labels.ts:97-104`). | Aucune source `handoffs`, `contributions`, `agent_sessions`, `instructions` ; aucun `extra_sources` branché ([V] `api/investigation_tasks.py:827`). |
| **g** | Codes `invalid_observation_anchor` émis ([V] `analyst_opinion_service.py:1314,1790`) ; mapping HTTP ([V] `api/analyst_opinion.py:355`). | `invalid_note_anchor` traduit ([V] `opinion/errors.ts:43`) ; `invalid_observation_anchor` absent du mapping et de l’union ([V] `api/opinionProposals.ts:41-58`). | Message lisible + rafraîchissement du sélecteur d’ancre. |
| **h** | Scénario 24 étapes API réel ([V] `tests/integration/test_acceptance_scenario_postgres.py:1-1538`) ; harnais factice réutilisable ([V] `tests/acceptance/scenario_support/harness.py:770-925`). | e2e M24 intercepte **tous** les appels API ([V] `frontend/e2e/investigation-workshop.spec.ts:11-15`, mock `e2e/support/workshopApi.ts:1778`). | Aucun parcours navigateur M24 contre FastAPI + PostgreSQL + sidecar + Typst réels. Les harnais réels existants visent M23 ([V] `Makefile:558-646`, `editorial_browser_server.py`) ou J2 legacy ([V] `Makefile:492-557`, `investigation_browser_server.py`). |

## 2. (a) Demander à Bridge depuis une tâche

**Réutilisation, pas de second transport.** Trois chemins canoniques seulement :
`advisor.consult` pour un avis/pivot/critique ; `POST /plans/request` pour une
révision de plan ; `POST /note-proposals` (déjà `task_id`) pour rédiger ou corriger
une Note. Aucun nouvel appel Bridge, aucun nouveau parseur de réponse.

Correspondance des demandes : *avis* → `interpretation` ou `critique` ; *critique
technique* → `critique` ; *pistes de pivot* → `pivot_strategy` ; *révision de plan* →
`POST /plans/request` (`plan_investigation`, champ `context_task_id`) ; *rédaction* →
`draft_text` ; *correction de rédaction* → `review_text` (ou `draft_text`) sur une Note
existante. Les trois derniers gardent l’ancre et le formulaire de la vue Avis.

**Nouvelle route** `POST /api/investigations/{investigation_id}/tasks/{task_id}/consultations`
(humain vérifié, `Idempotency-Key` obligatoire, CAS `expected_working_state_version`/`sha256`) :
corps `{consultation_type: "interpretation"|"critique"|"pivot_strategy", question: str, include_previous_consultation?: true, anchor?: str}`.
La route résout la tâche (même enquête, non terminale), construit le dossier, puis
appelle **une fois** `operator_tool_invocations.invoke(tool_name="advisor.consult")`
avec `requester_actor_kind=human`, `actor_id`, `context_task_id`. Réponse :
`{invocation_id, invocation_status, context_task_id, task_context_sha256, consultation: InvestigationConsultationV1}`.
Extension de `POST /plans/request` : champ optionnel `context_task_id` (même dossier,
même materialisation de plan) ; `GET /consultations` gagne `context_task_id` et un
filtre `?task_id=`.

**Contenu automatique du dossier** — la route assemble un `task_context` structuré
(ids/hash seulement, jamais de prose libre hors titre/objectif/question bornés) et le
passe comme champ **additif** accepté par `advisor.consult` : `investigation_id`,
`task_id`, `task_key`, `title`, `question`, `objective`, `status`, `priority`,
`claim_refs`, `evidence_refs`, `sample_refs`, `expected_outputs`, `result_refs`,
`assigned_actor`, `human_contributions[{id,category,file_relpath,file_sha256,origin,import_status}]`,
`previous_consultation{invocation_id,type,output_sha256,parse_status}` et
`editorial_anchor{anchor,excerpt,text_sha256}` si fourni. Le dossier existant ajoute
déjà article (résumé/passages), findings et observations ; la route ajoute les refs
explicites de la tâche et ses contributions (jamais promues en preuve). Les
credentials opérationnels restent exclus par `_require_safe_operator_payload`
([V] `application/operator_tool_invocation.py:1169`).

**Attribution.** Nouvelle colonne additive `tool_invocations.context_task_id`
(§ migration 0028) pour lier un appel **humain** à une tâche sans toucher au check
`(task_id, agent_session_id)` réservé aux sessions agentiques. `task_id`/`agent_session_id`
restent l’autorité de l’exécution opérateur ; `context_task_id` est le contexte de la demande.

**États affichés** (registre + `derive_advisor_consult_status`,
[V] `application/advisor_checkpoint.py:138-180`) : envoyée/en cours (`pending|running|sent_to_bridge`), reçue (`succeeded` + `parsed`), partielle (`partially_parsed`),
refusée (`failed|denied`), ambiguë à réconcilier (`needs_review` ou
`reconciliation_state=required`), nouvelle proposition/plan (`note_proposal_id` ou
`plan_version`), résultat lié à la tâche (même ligne de registre). **Jamais de
resoumission implicite** : la clé d’idempotence de la mutation est conservée tant que
le corps est identique ; après timeout réseau l’UI affiche « état inconnu », propose
« vérifier le registre » et ne relance qu’avec la même clé. Un avis n’écrit ni
résultat de tâche, ni preuve, ni décision.

## 3. (b) Ancres canoniques et sélection visuelle

**Source canonique unique.** `publication_renderable_body_anchors` est l’autorité
([V]). Il faut une **seule** fonction partagée `publication_note_body_anchors(document)`
qui applique le même drapeau que le rendu : `render_duplicate_paragraphs` vrai pour
`PublicationDocumentV9/V10`, faux sinon ([V] rendu `typst_rendering.py:2868`, snapshot
`investigation_article_snapshot.py:280-311`). Écarts vérifiés à corriger :
1. **V9/V10 sous-déclarés** : le rendu émet aussi les paragraphes dupliqués et la
   section overview ; le snapshot et l’acceptation les retirent, donc une ancre
   rendue peut être refusée `invalid_note_anchor`/`invalid_observation_anchor`. Une
   occurrence multiple identique n’est aujourd’hui pas sélectionnable.
2. **Base non épinglée à la demande** : `_validate_anchor` interroge le dernier
   snapshot ([V] `application/opinion_note_proposals.py:1116-1133`) alors que
   l’acceptation valide la base épinglée de l’avis ([V]
   `application/analyst_opinion_service.py:1265-1316`). Une révision de publication
   entre demande et acceptation peut donc produire une proposition inacceptable sans
   refus à la demande.
3. **Identité du paragraphe absente** : aucune Note ne pin le texte/ancre (`before_hash`
   M23 existe pour les blocs, pas pour les Notes). Rebase signale les ancres changées
   par la chaîne de correction ([V] `analyst_opinion_service.py:850-869`) mais ne peut
   pas détecter un changement de texte hors chaîne.

**Nouvelle lecture** `GET /api/investigations/{id}/article-anchors` (humain) :
`{base_publication_artifact_id, base_publication_version, base_publication_sha256,
source: "pinned_opinion_base"|"current_base", anchors:[{anchor,text,text_sha256,
section_index?,paragraph_index?}], elements:[{kind,key,title,caption,element_sha256,
anchor}], truncated}`. Base = celle de l’avis s’il existe, sinon la base courante
(résolue comme l’acceptation). `elements` liste les éléments M23 rendus dans cette
base (V9/V10 seulement pour `after_paragraph`). Limite 128 ancres comme le contexte
existant ([V] `application/investigation_contracts.py:39`, `MAX_CONTEXT_PUBLICATION_ANCHORS`),
avec `truncated` explicite.

**Contrat de proposition renforcé (additif)** : pin `base_publication_artifact_id/
version/sha256` et `anchor_before_sha256` (SHA-256 du texte exact du paragraphe) dans
`investigation_note_proposals`. À la demande, l’ancre est validée contre **cette**
base ; à l’acceptation, la base doit être identique (sinon `stale_base`) et le hash du
paragraphe doit correspondre (sinon `note_anchor_text_changed`, refus lisible). Un avis
existant garde sa base épinglée ; une révision d’article exige rebase/re-proposition,
jamais un déplacement silencieux. Les anciennes Notes sans hash restent lisibles.

**UI** : sélecteur « Ancre de publication » groupé lead/section, texte complet (borné),
badge « occurrence n » quand deux paragraphes ont le même texte, badge « remplace la
note existante » à la même ancre, et identité de base affichée (id/version/SHA). Le
remplacement à la même ancre reste réécrit par l’acceptation existante ([V]
`analyst_opinion_service.py:1339-1342`). Si l’article est révisé, bandeau « base
périmée » + rechargement ; si l’ancre a disparu, refus explicite et rechargement de la
liste. Saisie manuelle conservée derrière un `<details>` **« Diagnostic — saisie
manuelle d’ancre »** clairement étiqueté, sans datalist, avec avertissement que le
serveur reste l’autorité.

**Affichage de l’article.** Réutiliser M23 : `getEditorialWorkspaceSnapshot`
([V] `frontend/src/api/editorialWorkspace.ts`) et `EditorialDocument` en `readOnly`
([V] `EditorialDocument.tsx:476-512`) pour le contexte visuel et les aperçus
d’éléments ; le sélecteur d’ancre reste alimenté par la route canonique ci-dessus.

## 4. (c) Notes avec tableaux/figures M23

**Chaîne existante à réutiliser.** Le document de publication porte tables, charts,
diagrammes, figures sourcées avec légende/provenance ; le rendu Typst dispatche ces
types ([V] `publication_helpers.typ:1039-1078`) via `_table_block`, `_chart_block`,
`_diagram_block`, `_figure_block` ([V] `typst_rendering.py:2664-2760,4508,4566`). Les
Notes rendent déjà le texte, les littéraux techniques et les blocs de code en clôture
([V] `:1757-1853`, test `tests/test_typst_rendering.py:1165-1180`) ; le parseur de
tables pipe existe mais n’est branché que sur les blocs d’avis, pas sur
`notes_by_anchor` ([V] `:1880-1935,2871-2902,4185`). Aucun moteur Markdown→Typst
parallèle n’est donc nécessaire.

**Contrat proposé — `NoteContentBlockV1` (union fermée)** :
- `article_element` : `{kind:"table"|"chart"|"figure"|"diagram", element_key,
  element_sha256, anchor}`. Référence un élément **déjà rendu** de la publication
  épinglée ; il doit être résolu en V9/V10, placé sur la même ancre que la Note
  (mapping `resolved_placements.paragraph_kind/index`, [V] `publication_document.py:3349-3389`).
  La Note rend une **cross-référence** (titre/légende/numéro) ; l’élément reste rendu à
  son placement d’article, jamais dupliqué.
- `canonical_code` : `{observation_id, blob_sha256, offset, length, excerpt_sha256,
  language}`. Le serveur extrait les octets exacts du blob de l’observation (même
  enquête) à l’acceptation et au rendu ; rendu par le chemin code existant. Aucun
  texte de code fourni par Bridge n’est accepté comme preuve.
- `ioc_table` : `{observation_ids[], title}`. Lignes construites déterministiquement
  depuis les observations canoniques ; rendu par le builder table existant.
- `protocol_figure` : `{kind, asset_sha256}` pour les trois figures D2 déjà portées par
  l’avis ([V] `domain/analyst_opinion.py:629-674`), rendues en annexe.

**Règles de provenance.** Un `chart` doit résoudre un chart M23 dont chaque point
porte des `evidence_refs` ([V] `domain/production_editorial_enrichment.py:281-292`) ;
sinon `note_chart_without_evidence`. Une `figure` doit porter un
`source_document_id` connu du document ; sinon `note_figure_source_missing`. Un
`canonical_code` doit correspondre octet pour octet ; sinon `note_code_ref_stale`.
Un IOC doit référencer une observation de la même enquête et non consultative ;
sinon `note_ioc_ref_unresolved`. Un `article_element` dont le hash/ancre a changé est
`note_block_ref_stale`. Les légendes d’éléments d’article restent éditées **dans M23**
(`CaptionEditor`) ; les blocs inline reçoivent une légende **déterministe** (pas de
prose humaine, pour préserver `bridge_only`). Bridge reste le seul auteur de la prose ;
l’humain choisit/positionne des références, jamais du texte publiable.

**Édition visuelle via ponts M23.** Créer/déplacer/réordonner/remplacer/supprimer/
légender se fait dans l’atelier M23 existant (`VisualInsertionComposer`,
`VisualActionBar`, `CaptionEditor`, `ReplaceFigureDialog`, `RemoveElementDialog`,
`ReorderElements`) ; la Note expose un aperçu en lecture seule, un lien vers
`/subjects/{id}/editor` et un bouton « recharger les éléments ». Modifier l’article crée
une nouvelle version de publication : la proposition devient stale à l’acceptation et
exige rebase ; c’est le comportement voulu.

**Persistance/contrat additif exact** :
- migration `0029_note_anchor_identity` (lots B4 **et** B5a) : `investigation_note_proposals`
  gagne `base_publication_artifact_id uuid null` (FK `production_artifacts.id` RESTRICT),
  `base_publication_version bigint null`, `base_publication_sha256 char(64) null`,
  `anchor_before_sha256 char(64) null`, checks de paire et de SHA, **et** `content_blocks jsonb
  not null default '[]'` (+ `CHECK jsonb_typeof='array'`). Il n'existe **pas** de
  `0030_note_content_blocks` : la correction de numérotation est actée dans
  `backend/migrations/versions/0029_note_anchor_identity.py` (docstring et colonnes). Downgrade
  refusé tant qu'une ligne porte un pin ou des blocs.
- payload d’avis `analyst-observations.v2` (JSONB, **aucun DDL**) : chaque note gagne
  `content_blocks`; le parseur accepte v1 et v2 ; les anciennes versions restent
  lisibles et inchangées. Les nouveaux chemins n’ajoutent qu’une version d’avis,
  jamais une mise à jour en place d’une version antérieure ; le CAS porte sur
  `status_version` de proposition, la paire de base d’avis et le pin de publication.
- `NoteProposalV1`/`NoteProposalRequestV1`/`NoteProposalViewV1`/`NoteProposalDiffViewV1`
  + `same_note_proposal_request` ([V] `investigation_persistence.py:1073-1088`) incluent
  les nouveaux champs ; le rendu `typst_rendering.py` résout `content_blocks` dans
  `notes_by_anchor` et le template `render-analyst-observation` accepte `blocks`.
- **Aucun** changement du schéma d’enrichissement M23 (V11) dans ce lot. Un placement
  `in_note` (élément rendu *dans* le panneau de Note) exigerait V12 + registry + rendu
  M23 : hors périmètre, question ouverte (§8).

## 5. (d)(e)(f)(g) — détail minimal

**(d) Panneau opérateurs.** Nouvelle lecture humaine
`GET /api/investigations/{id}/agent-sessions/{sid}/summary` : `operations` (invocations
`agent_session_id=sid`, tool/status/budget consommé-réservé, dates), `budget` (somme
session + budget d’enveloppe restant via `GET /envelope`), `broker` dérivé du journal
(dernier appel signé, statut, nombre ambigu/reconciliation), `handoff` (`handoff_id` →
`GET /handoffs/{id}` : version, SHA-256 ; `/freshness` pour l’état), `instructions` (20 dernières :
état, SHA-256, position, dates, texte humain). Aucun état broker inventé : le panneau
dit « dérivé du journal ». L’UI affiche ces champs et cesse « Non exposé ».

**(e) G2 extend/revoke.** Backend déjà prêt ; seul le montage cockpit manque. Extraire
ou réutiliser le formulaire extend/revoke de `InvestigationGovernancePanel`
(`useInvestigationEnvelope/useExtend…/useRevoke…`) dans une section de la Vue d’ensemble
affichée **seulement** si l’enveloppe est approuvée (l’approbation initiale reste
`EnvelopeSection`, pas de doublon). Afficher `currently_authorized`, le statut de
politique héritée et l’état révoqué ; après révocation, aucun bouton de ré-approbation.

**(f) Chronologie.** Implémenter quatre `TimelineSource` et brancher `extra_sources`
dans `api/investigation_tasks.py:827` : handoffs, contributions, sessions, instructions.
Ajouter les item types `handoff`, `contribution`, `agent_session`, `instruction`
(append-only, `evidence:false`, sauf observation canonique déjà `evidence:true`) ;
labels/types front alignés. Aucune nouvelle table.

**(g) Erreurs.** Ajouter `invalid_observation_anchor` (et `note_block_ref_stale`,
`stale_base`, `note_anchor_text_changed`, `note_code_ref_stale`) au mapping
`opinion/errors.ts` et aux unions de codes ; message français + bouton « recharger les
ancres/la proposition ». Test Vitest sur chaque code.

## 6. Vrai parcours navigateur (24 étapes)

**Réalisé par étapes (2026-11-23).** **F7a** livre les étapes **1–11** : cible
`make test-investigation-browser` (`Makefile:648`), config
`frontend/playwright.investigation.config.ts`, launcher
`backend/tests/workflow_emulator/m24_browser_server.py` et spec
`frontend/e2e-real/investigation-workshop/workshop_real.spec.ts` (« steps 1–11 » explicite en
tête, arrêt volontaire après l'étape 11). Les étapes **12–24** (note workspace,
livraison/ack d'instruction, résultat signé hôte, consultations de tâche, propositions de
Note, ancres, corrections, PDF Typst réel) restent **F7b, en cours** ; leurs prérequis
B5a/B5b/F2/F3 sont désormais fusionnés.

**Cible** `make test-investigation-browser`, calquée sur `test-editorial-browser`
([V] `Makefile:558-646`) : PostgreSQL d’intégration jetable (ou DSN externe), attestation
`M24_REAL_LITE_ATTESTED_FAKE_PROVIDER=1`, serveur M24 réel sur loopback, arrêt propre.
Nouvelle config `frontend/playwright.investigation.config.ts` calquée sur
`playwright.editorial.config.ts` (port dérivé du worktree, **pas** le port fixe 4174 de
`playwright.real.config.ts`) ; testDir `frontend/e2e-real/investigation-workshop/`.

**Serveur** `backend/tests/workflow_emulator/m24_browser_server.py` (nouveau) :
base jetable créée/migrée à la tête, `create_app()` réel, `lifespan="off"` + état injecté
comme le harnais d’acceptation ([V] `tests/acceptance/scenario_support/harness.py:770-925`),
en réutilisant les patterns W10 (`tests/workflow_emulator/browser_server.py`) et J2
(`investigation_browser_server.py:680-790`) pour l’état, les jobs différés et l’arrêt ;
sidecar workspace réel sur UDS tmp, profils CLI factices H1, VT factice, Bridge factice
**auto-répondeur** (plan/note/review déterministes), Typst réel, identité
`StaticTokenIdentityProvider` avec jeton de test ≥32 caractères injecté par le proxy Vite
via `OPERATOR_API_TOKEN` (jeton synthétique, jamais un credential). Routes de contrôle
`/api/m24-browser/state`, `/__m24_browser/drain-instructions`,
`/__m24_browser/shutdown` ; refus si DSN/host non loopback ou attestation absente
(modèle [V] `editorial_browser_server.py:461-483`). Drain manuel car `lifespan="off"`
(la boucle ID-2 vit dans le lifespan, [V] `api/main.py:696-726`).

**Parcours** (le navigateur pilote les actions humaines ; les effets hôte — signed
tool-calls, ack d’instruction, CLI — passent par les routes signées D7 du harnais,
jamais par un mock d’API) :
1. Ouvrir l’ARTICLE du sujet seedé (synthèse/publication vérifiées, article rendu).
2. Démarrer l’atelier (`creation_profile=m24_workshop`, `operator_led`, `bridge_only`).
3. Vue d’ensemble : agent hôte disponible, créer/lier le workspace réel.
4. Inventaire IOC : seeds ≠ découverts, échantillons.
5. Approuver l’enveloppe G2 initiale (motif + confirmation + empreinte).
6. Amorçage : `POST /bootstrap/run`, snapshot + téléchargements VT factices.
7. Demander le plan Bridge depuis le cockpit ; réponse factice.
8. Accepter le plan (décision humaine + matérialisation des tâches).
9. Assigner une tâche à un opérateur (passation v1).
10. Ouvrir une session Codex factice réelle (sidecar, lease/fence).
11. Avancement : heartbeat/état de tâche/session et résultat technique visible.
12. Écrire une note humaine dans le workspace (carte tâche).
13. Synchroniser : la note apparaît via l’API (non-preuve).
14. Envoyer une instruction depuis le panneau opérateurs.
15. Livraison effective : drain backend, instruction `delivered`/`acknowledged` visible.
16. Soumettre le résultat (host signé) puis l’accepter côté humain.
17. Transférer la tâche vers Claude Code (passation v2, session factice).
18. **Avis Bridge depuis la tâche** : `POST …/tasks/{id}/consultations` (`critique`),
    états affichés (envoyée → reçue), aucun changement de statut métier.
19. **Rédaction Bridge** : depuis la Note, composer avec `task_id` pré-rempli.
20. **Sélection d’ancre** : sélecteur visuel canonique, occurrence multiple, base
    affichée, saisie manuelle repliée en mode diagnostic.
21. Proposition : texte stocké + cross-références/blocs vérifiés, diff, SHA.
22. Correction : seconde demande Bridge ciblée, nouvelle proposition.
23. Accepter par version/hash (CAS) ; ancienne proposition stale refusée.
24. Aperçu PDF réel Typst : deux Notes à deux ancres, placement asserté, ligne de
    provenance et blocs liés visibles.

**Déjà couvert** : les 24 étapes API par
`tests/integration/test_acceptance_scenario_postgres.py` ; les vues M24 par 13 tests
Playwright interceptés ; le navigateur réel M23 par 8 tests
(`frontend/e2e-real/editorial/workspace_real.spec.ts`) ; le J2 legacy par
`test-workflow-browser-investigation`. **Livré par F7a** : les étapes navigateur 1–11 contre
le backend M24 réel. **Encore non couvert** : les étapes 12–24 contre le backend M24 réel
(F7b).

**Contraintes d’exécution** : `CHOKIDAR_USEPOLLING=1` si inotify saturé, env Typst
réel, `tests/workspace_agent` sans `TMPDIR` long (limite AF_UNIX 108 octets), un seul
pytest à la fois.

## 7. Découpage en lots indépendants

Règle : un seul écrivain par fichier ; `api/main.py` reste à l’orchestrateur (inclusion
+ allowlist exacte) ; câblage UoW dans l’ordre 04B1 < 05A < 05B1 < 07 < 08B. Backend
d’abord quand une route manque, puis frontend. Chaque lot ≤ ~1 jour d’agent.

### Lots backend

| Lot | Contenu | Fichiers propriétaires (exclusifs) | Dépend de | Oracle |
|---|---|---|---|---|
| **B1** | Migration `0028_task_context_consultations` + `ToolInvocation.context_task_id` (domaine, row, repo, invocation service, registre) | `migrations/versions/0028_*`, `domain/investigation.py`, `models/investigation.py`, `repositories/investigation.py`, `application/investigation_persistence.py`, `application/operator_tool_invocation.py`, `application/investigation_service.py` (création d’invocation), `infrastructure/database/uow.py`, `application/persistence.py`, tests migration/attribution | — | `test_alembic_chain.py::test_expected_chain_ends_with_0028`, `test_investigation_consultation_ledger.py::test_context_task_id_is_projected_and_filterable`, `test_tool_invocation_attribution_pair_check` étendu (pair agentique inchangé) |
| **B2** | Contrat `task_context` du dossier Advisor : champ admis, `prompt_data`, prompt, dossier, omissions | `application/investigation_advisor_tools.py` (champs/dossier uniquement), `application/advisor_prompt.py`, `application/advisor_dossier.py`, tests | B1 | `test_investigation_advisor_tools.py::test_task_context_is_bounded_in_dossier`, `::test_task_context_never_carries_credentials` |
| **B3** | Route task-scoped + extension plans request + filtre registre + propagation `context_task_id` aux propositions de Notes | `api/investigation_task_consultations.py` (nouveau), `api/investigation_plans.py`, `application/opinion_note_proposals.py` (propagation), `api/opinion_note_proposals.py` (adaptateur seulement), tests ; inclusion/allowlist par l’orchestrateur dans `api/main.py` | B1, B2 | `test_investigation_task_consultations.py::test_task_consultation_reuses_advisor_consult_and_attribution`, `::test_task_consultation_never_mutates_task_status`, `test_investigation_plan_api.py::test_plan_request_with_context_task_links_invocation`, `test_opinion_note_proposals.py::test_task_drafting_links_context_task` |
| **B4** | Ancres : helper unique renderable (V9/V10 aligné), pin de base + hash de paragraphe dans la proposition, lecture `GET /article-anchors`, résolution des éléments M23 de la base | `migrations/versions/0029_*`, `application/investigation_article_anchors.py` (nouveau), `application/investigation_article_snapshot.py`, `application/opinion_note_proposals.py` (validation base), `api/investigation_article.py` (nouveau), `domain/opinion_note_proposal.py` (pin), `models/opinion_note_proposals.py`, `repositories/opinion_note_proposals.py`, `application/investigation_persistence.py` (row/proxy pin), `application/analyst_opinion_service.py` (validation base/ancres), tests | B1, B3 | `test_alembic_chain.py::test_expected_chain_ends_with_0029`, `test_opinion_note_proposals.py::test_anchor_uses_pinned_base_publication`, `::test_duplicate_paragraph_anchor_is_renderable_and_accepted_on_v9`, `::test_removed_anchor_refused_with_typed_code`, `test_investigation_article_anchors.py::test_anchor_route_returns_exact_base_identity` |
| **B5a** | Blocs de Note : union domaine, persistance proposition + avis v2, validation/acceptation, idempotence | `migrations/versions/0029_*` (`content_blocks` dans la même révision que B4), `domain/opinion_note_proposal.py`, `domain/analyst_opinion.py`, `application/opinion_note_proposals.py`, `application/analyst_opinion_service.py` (accept), `api/opinion_note_proposals.py`, `models/opinion_note_proposals.py`, `repositories/opinion_note_proposals.py`, `investigation_persistence.py`, tests | B4 | `test_alembic_chain.py::test_expected_chain_ends_with_0029`, `test_opinion_note_proposals.py::test_article_element_ref_pinned_and_stale_refused`, `::test_code_ref_matches_observation_bytes`, `::test_ioc_ref_rejects_advisory_observation`, `::test_note_payload_v2_preserves_v1_readability` |
| **B5b** | Rendu Typst des blocs de Note (résolution refs, cross-références, code/IOC inline), template | `application/typst_rendering.py`, `chpTypst/RENDERER/publication_helpers.typ`, tests rendu | B5a | `test_typst_rendering.py::test_note_content_blocks_render_with_provenance`, `::test_note_chart_without_evidence_refused`, `::test_note_code_ref_exact_bytes` |
| **B6** | Résumé opérateur : ops/budget/broker/handoff/instructions | `api/agent_sessions.py`, `application/agentic_sessions.py` (lecture), tests | ID-3/ID-4 mergés (éviter conflit de fichier) | `test_agent_sessions.py::test_session_summary_exposes_operations_budget_handoff_instructions`, `::test_session_summary_never_leaks_secret` |
| **B7** | Sources timeline handoffs/contributions/sessions/instructions + item types | `application/investigation_timeline.py`, `api/investigation_tasks.py` (branchement `extra_sources`), tests | B6 | `test_investigation_timeline.py::test_timeline_projects_handoff_contribution_session_instruction`, `::test_new_items_are_non_evidence` |

### Lots frontend

| Lot | Contenu | Fichiers propriétaires (exclusifs) | Dépend de | Oracle |
|---|---|---|---|---|
| **F1** | Panneau « Demander à Bridge » dans la carte tâche : avis/pivot/critique, révision de plan, liens rédaction/correction ; états et non-resoumission | `features/investigation/tasks/TaskBridgePanel.tsx` (nouveau), `api/taskConsultations.ts` (nouveau), `tasks/TaskCard.tsx` (point d’entrée), `plan/PlanPanel.tsx` (option task), tests | B3 | `pnpm test --run TaskBridgePanel TaskCard`, `api/taskConsultations.test.ts` |
| **F2** | Sélecteur d’ancre canonique + mode diagnostic + traductions d’erreurs | `opinion/AnchorPicker.tsx` (nouveau), `api/articleAnchors.ts` (nouveau), `opinion/BridgeRequestPanel.tsx`, `opinion/errors.ts`, `api/opinionProposals.ts` (codes), tests | B4 | `pnpm test --run AnchorPicker OpinionNotesView`, `opinion/errors.test.ts` |
| **F3** | Blocs riches de Note : sélection M23, aperçu lecture seule, cross-références, code/IOC, revue | `opinion/NoteContentBlocks.tsx` (nouveau), `opinion/NoteProposalReview.tsx`, `opinion/OpinionNotesView.tsx`, `api/opinionProposals.ts` (types), tests | B5a | `pnpm test --run NoteContentBlocks NoteProposalReview OpinionNotesView` |
| **F4** | Panneau opérateurs : ops/budget/broker/handoff/instructions | `tasks/OperatorsPanel.tsx`, `api/agentSessions.ts`, `tasks/labels.ts`, tests | B6 | `pnpm test --run OperatorsPanel agentSessions` |
| **F5** | G2 extend/revoke dans la Vue d’ensemble (sans doublon d’approbation) | `overview/EnvelopeLifecycleSection.tsx` (nouveau), `overview/OverviewView.tsx`, `overview/EnvelopeSection.tsx` (lien seulement), tests | — | `pnpm test --run EnvelopeLifecycleSection OverviewView` |
| **F6** | Timeline : nouveaux types/labels/filtres | `tasks/TimelinePanel.tsx`, `tasks/labels.ts`, `api/tasks.ts`, tests | B7 | `pnpm test --run TimelinePanel` |
| **F7** | Parcours navigateur réel + cible Make + config Playwright. **F7a livré : étapes 1–11. F7b ouvert : étapes 12–24.** | `backend/tests/workflow_emulator/m24_browser_server.py` (livré), `backend/tests/workflow_emulator/test_m24_browser_server.py` (livré), `frontend/playwright.investigation.config.ts` (livré), `frontend/e2e-real/investigation-workshop/workshop_real.spec.ts` (livré, étapes 1–11), `Makefile` (cible livrée) | B1–B7, F1–F6 | `make test-investigation-browser` (**étapes 1–11**), `test_m24_browser_server.py` (attestation/loopback/shutdown) ; oracle 12–24 à livrer par F7b |

**Ordre** : B1 → B2 → B3 ; B4 → B5a → B5b ; B6 (après merges ID-3/ID-4) → B7 ;
F5 peut démarrer immédiatement ; F2 après B4 ; F1 après B3 ; F3 après B5a (F3 peut
précéder B5b, le rendu réel restant couvert par B5b + F7) ; F4 après B6 ; F6 après B7 ;
F7 en dernier. Aucun lot frontend ne modifie un fichier d’un autre lot ; `TaskCard.tsx`
appartient à F1 seul (F4 reste dans `OperatorsPanel`).

**Fichiers partagés entre lots séquentiels** (jamais en parallèle, ordre imposé) :
`application/opinion_note_proposals.py` (B3 < B4 < B5a), `domain/opinion_note_proposal.py`
et `application/analyst_opinion_service.py` (B4 < B5a), `application/investigation_persistence.py`
(B1 < B4), `models/opinion_note_proposals.py`/`repositories/opinion_note_proposals.py`
(B4 < B5a), `api/opinionProposals.ts` (F2 < F3). L’orchestrateur fusionne chaque lot
avant d’ouvrir le suivant sur ces fichiers.

**Discipline migration** : ADR-0010 D8 / ADR-0007 — un ticket DDL ne touche que la
migration, `migrations/env.py`, `tests/integration/test_migrations.py`,
`tests/test_alembic_chain.py`, `tests/test_architecture_production_lineage.py`.
Prévalidation de l’état attendu, échec avant DDL, downgrade refusé tant que des lignes
utilisent les colonnes (`context_task_id`, `content_blocks`, pins).

## 8. Risques et questions ouvertes pour l’humain

1. **Placement des blocs riches** : la conception retient « élément M23 associé à
   l’ancre + cross-référence » pour éviter toute duplication et tout nouveau schéma.
   Faut-il au contraire rendre l’élément **dans** le panneau de Note ? Cela exige un
   placement d’enrichissement `in_note` (schéma V12, registry, rendu M23) — lot
   supplémentaire non prévu ici.
2. **Occurrences multiples V9/V10** : aligner la validation sur le rendu autorise des
   Notes sur des paragraphes dupliqués/overview auparavant refusés (test d’acceptation
   `DEDUPLICATED_ANCHOR` à réviser). Confirmer que c’est la sémantique voulue.
3. **Prose et blocs inline** : les légendes des blocs inline sont déterministes pour
   préserver `bridge_only`. Si l’humain doit pouvoir légender librement, il faut une
   provenance Bridge de la légende (nouveau contrat D4).
4. **Base périmée** : toute modification M23 de l’article rend les propositions de Note
   stale (comportement voulu). Confirmer le parcours rebase/re-proposition attendu.
5. **BRIEF** : `_publication_anchors` renvoie `()` pour un V7/V8 BRIEF ([V]
   `investigation_article_snapshot.py:290-303`) ; les Notes y restent impossibles. Le
   cockpit doit-il créer des enquêtes sur BRIEF ?
6. **Migrations 0028–0030** — **résolu** : `0028_task_context_consultations`, puis
   `0029_note_anchor_identity` qui porte les pins **et** `content_blocks` ; aucun `0030`.
7. **Coût du parcours navigateur** : un serveur M24 réel + sidecar + PG + Typst + 24
   étapes est lourd (probablement 3–6 min). Le réserver au gate final, pas à chaque lot.
8. **Transport multi-paragraphes Bridge** : le parcours doit inclure une réponse à deux
   paragraphes pour prouver la conservation des sauts (`note_draft_text_from_sentences`),
   point encore non prouvé en transport réel.
9. **Jeton du harnais navigateur** : `OPERATOR_API_TOKEN` synthétique injecté par Vite ;
   vérifier qu’il ne fuit ni dans les traces Playwright ni dans les logs du serveur.
10. **Extend/revoke** : la révocation coupe les droits externes ; le test navigateur doit
    la couvrir sur une seconde enquête ou en fin de parcours pour ne pas casser les
    étapes suivantes.

## 9. Points de contrôle avant implémentation

- Aucune route nouvelle n’est accessible au jeton `OPERATOR_CLIENT` seul ; toutes les
  routes de ce document sont humaines vérifiées ([V] gate
  `api/investigation.py:4670-4690`) et n’exigent donc pas d’entrée d’allowlist, sauf
  décision contraire de l’orchestrateur.
- Toute nouvelle route est incluse par l’orchestrateur dans `api/main.py` avec son
  entrée d’allowlist exacte si elle doit être opérateur-lisible.
- Les tests n’appellent aucun fournisseur réel ; Bridge/VT/CLI restent factices et
  attestés ; PostgreSQL/Typst restent réels dans le parcours navigateur.
- Aucune migration ne touche `0001_baseline` ; la tête reste linéaire et additive.
