# R78 — Audit des gros fichiers restants (post R68-R76)

Objectif : déterminer, fichier par fichier, si la taille traduit un vrai problème de
navigation/contexte agent, ou si c'est une taille cohérente à ne pas toucher.
Aucun code modifié dans cette tâche.

Rappel du critère central : la taille seule n'est pas une justification. Verdict B/C
seulement si un des 5 critères est vérifié (voir prompt).

`backend/src/cti_app/api/discovery.py` a été retiré de l'audit : 25.3 KB, sous le
seuil de 30 KB (probablement déjà réduit lors d'un passage précédent).

---

## 1. chatgpt-bridge/extension/content.js — 55.8 KB / 1662 lignes

- **Responsabilité principale** : content script unique qui pilote l'UI de ChatGPT
  dans l'onglet (frappe du prompt, lecture de la réponse en streaming, menus de
  réglages, suppression de conversation) pour le pont d'automatisation.
- **Responsabilités secondaires** : sélecteurs DOM (`SELECTORS`), utilitaires de
  picker/menu génériques, normalisation de texte, gestion des pièces jointes.
- **Symboles majeurs** : ~45 fonctions top-level, aucune classe, pas d'import/export
  (script injecté brut, pas un module bundlé).
- **Fanout** : nul en pratique — un seul point d'entrée (`chrome.runtime.onMessage`),
  aucun autre fichier du repo n'importe ce fichier (extension non modulaire).
- **Résultat pertinent** : les tâches réelles sur ce fichier sont typiquement
  localisées à une phase (ex. « la lecture du streaming casse » → zone
  `streamAnswer`/`readAnswer`/`completionState`, une seule zone contiguë ; « le
  toggle web search ne s'applique plus » → zone `setWebSearch`/`applyControls`,
  une seule zone). Aucune tâche observée n'a nécessité de sauter entre >2 zones
  disjointes.
- **Verdict : A — gros mais cohérent.** C'est un seul script pilotant une seule
  surface (l'UI ChatGPT), avec un seul dispatcher de messages. La taille vient du
  nombre de micro-fonctions DOM nécessaires pour une UI tierce fragile, pas d'un
  mélange de responsabilités indépendantes.

---

## 2. backend/src/cti_app/application/production_workflow.py — 41.9 KB / 1060 lignes

- **Responsabilité principale** : `ProductionWorkflowOrchestrator`, orchestrateur
  du pipeline de production d'une brève (sources → références → extraction →
  synthèse → assemblage), une classe unique.
- **Responsabilités secondaires** : réparation de format de sortie modèle
  (`_ask_with_format_repair`), intégration des sources de référence, retry de
  stage (`retry_references`, `retry_synthesis`).
- **Symboles majeurs** : 1 classe, ~17 méthodes, dont 5 `_execute_*_stage`
  correspondant 1:1 aux 5 étapes du pipeline métier.
- **Fanout** : 2 importeurs (`production_jobs.py`, `api/main.py`) — usage centralisé
  via le job runner, pas dispersé.
- **Résultat pertinent** : une tâche « fix stage extraction » touche uniquement
  `_execute_extraction_stage` (une zone contiguë de ~120 lignes) ; une tâche
  « fix retry » touche `retry_references`/`retry_synthesis`, également contiguës.
- **Verdict : A — gros mais cohérent.** Chaque méthode correspond à une étape
  distincte mais appartient au même owner fonctionnel (le pipeline de
  production). Le découpage par méthode que fait déjà le fichier suffit à la
  navigation.

---

## 3. backend/src/cti_app/application/collection.py — 41.3 KB / 1001 lignes

- **Responsabilité principale** : `SubjectCollectionService`, cycle de vie complet
  de la collecte de sources pour un sujet (init, ajout, téléchargement, archivage,
  retry, enregistrement des tentatives).
- **Responsabilités secondaires** : quelques fonctions module-level d'aide
  (clé d'idempotence, libellés d'état, message de résumé).
- **Symboles majeurs** : 1 classe, ~20 méthodes + 7 fonctions utilitaires
  module-level.
- **Fanout** : 6 importeurs (jobs, workers, api/collection.py,
  production_workflow.py) — attendu pour un service de domaine central.
- **Résultat pertinent** : les tâches observées (« fix `_archive` »,
  « fix `download_source` ») restent dans une méthode ou deux méthodes
  adjacentes ; aucune tâche n'a nécessité de sauter entre des zones éloignées et
  indépendantes.
- **Verdict : A — gros mais cohérent.** Toutes les méthodes appartiennent au
  même owner (le cycle de vie `SourceCollection`) ; c'est un service de domaine
  volumineux mais pas un fourre-tout.

---

## 4. frontend/src/features/discovery/DiscoveryPanel.tsx — 40.1 KB / 1067 lignes

- **Responsabilité principale** : composant React unique `DiscoveryPanel`
  (~990 lignes de corps de fonction) affichant l'ensemble du flux de découverte
  d'une édition.
- **Responsabilités secondaires clairement séparables** :
  1. lancement/relance de la découverte (`launch`, `relaunch`, `discovery` query) ;
  2. **workflow de récupération** (« recovery ») en cas d'échec — état dédié
     (`showManualRecovery`, `manualMarkdown`, `visibleRecovery`,
     `manualRecovery`, `confirmRecovery`, `completionRecovery`,
     `abandonRecovery`) et sa section JSX (`recovery-panel`, ~lignes 509-660) ;
  3. **workflow d'import manuel** — état dédié (`showManualImport`,
     `manualImportMarkdown`, `previewImport`, `confirmImport`) et sa section
     JSX (~lignes 613-666, imbriquée dans la section recovery) ;
  4. **liste/filtrage des candidats** — état dédié (`search`, `minimum`,
     `sourceStatus`, `sort`, `markSource`, `attachUrl`, `reprocessReport`) et sa
     section JSX (filtres + stats de consolidation + liste, ~lignes 666-1010).
- **Symboles majeurs** : 2 fonctions top-level seulement (`IncompleteSourceUrlForm`
  et `DiscoveryPanel`), mais `DiscoveryPanel` contient à lui seul 13 `useState`,
  13 `useMutation`/`useQuery`, 2 `useCallback`, 1 `useMemo`.
- **Fanout** : 1 seul importeur (`EditionDetailPage.tsx`) — le composant est déjà
  bien isolé côté consommateurs ; le problème est interne, pas externe.
- **Résultat pertinent** : une tâche « corriger le workflow de récupération »
  oblige à lire l'état déclaré en haut (lignes ~94-107), la mutation
  correspondante (lignes ~216-258) et le JSX (lignes ~509-660) — 3 zones non
  contiguës du même fichier. Idem pour le workflow d'import manuel. C'est le
  seul fichier audité où le critère 1 (>2 zones distinctes pour une tâche
  normale) est vérifié de façon nette.
- **Verdict : B — split potentiellement utile.**

---

## 5. backend/src/cti_app/application/discovery/cumulative/service.py — 37.6 KB / 818 lignes

- **Responsabilité principale** : `CumulativeDiscoveryService`, cycle de vie
  cumulatif de la découverte pour une édition (ingestion de batch → réconciliation
  → résolution des fusions humaines → activation de snapshot).
- **Responsabilités secondaires** : liaison aux groupes éditoriaux après
  activation d'un snapshot.
- **Symboles majeurs** : 1 classe, 12 méthodes, dont deux très longues :
  `reconcile_intake` (~259 lignes) et `_resolve_merge_run` (~236 lignes).
- **Fanout** : 7 importeurs (api/main.py, api/discovery_merge.py,
  api/discovery.py, jobs, workers, manual_source_edits.py,
  cumulative/jobs.py) — usage large mais cohérent avec un service central du
  domaine découverte.
- **Résultat pertinent** : les tâches réelles touchent une seule méthode à la
  fois (ex. « fix reconcile_intake » = une seule zone de 259 lignes). Le
  problème ici n'est pas la structure du fichier mais la longueur de deux
  méthodes individuelles — un sujet de refactor de méthode, pas de split de
  fichier, donc hors périmètre de ce critère.
- **Verdict : A — gros mais cohérent.** Toutes les méthodes appartiennent au
  même owner (cycle de vie de la découverte cumulative). Pas de fanout de
  changement inutilement large observé — les 7 importeurs consomment chacun un
  sous-ensemble stable de l'API publique sans se marcher dessus.

---

## 6. backend/src/cti_app/integrations/models.py — 36.0 KB / 957 lignes

- **Responsabilité principale** : implémentations concrètes des transports et
  adaptateurs modèles (couche `integrations`, sous `application/model_gateway.py`).
- **Responsabilités secondaires clairement séparables — 3 owners** :
  1. **Transports HTTP** : `ResponsesTransport`, `ChatCompletionsTransport`
     (Protocols), `HttpResponsesTransport`, `BridgeTransportError`,
     `ChatGPTBridgeTransport`, `HttpChatCompletionsTransport` — logique HTTP/retry/
     backoff bas niveau (lignes 36-392).
  2. **Adaptateurs par fournisseur** : `OpenAIResearchAdapter`,
     `OpenAIStructuredAdapter`, `QwenAdapter`, `FakeModelAdapter` (lignes
     393-673) — chacun un plugin indépendant, ajouté/modifié sans toucher aux
     autres.
  3. **Stores de sortie** : `BlobModelOutputStore`, `InMemoryModelOutputStore`
     (lignes 674-712).
  4. Fonctions utilitaires de sérialisation partagées en bas de fichier
     (lignes 712-957), utilisées par plusieurs des groupes ci-dessus.
- **Symboles majeurs** : 12 classes + ~20 fonctions module-level.
- **Fanout externe** : très faible — 1 seul importeur (`model_factory.py`), qui
  fait office de point d'assemblage. Le critère pertinent ici n'est pas le
  fanout mais le critère 3 (owners séparables) : ajouter un 5e adaptateur
  fournisseur, ou changer la politique de retry HTTP, ou changer le store, sont
  trois tâches qui ne se recouvrent jamais et sont déjà textuellement
  disjointes dans le fichier.
- **Résultat pertinent** : une tâche « ajouter un adaptateur Mistral » n'a besoin
  de lire que le Protocol (`ModelAdapter`, externe) + un adaptateur existant
  comme modèle (ex. `QwenAdapter`, ~75 lignes) — mais charger le fichier entier
  pour ça tire aussi transports HTTP et stores sans rapport.
- **Verdict : B — split potentiellement utile.**

---

## 7. backend/src/cti_app/application/production_parsers.py — 35.1 KB / 1014 lignes

- **Responsabilité principale** : parsing et validation des sorties texte du
  modèle pour la production (rapports de référence + extractions techniques).
- **Responsabilités secondaires clairement séparables — 2 familles de parsers
  partageant des utilitaires communs** :
  1. **Famille "reference report"** : `ReferenceReport`, `ParsedSource`,
     `ParsedEvent`, `parse_reference_report` (lignes 460-610),
     `reference_report_to_json`/`_from_json` (884-943).
  2. **Famille "technical extraction"** : `SemanticType`, `IndicatorStatus`,
     `IndicatorProvenance`, `DisplayPolicy`, `ExtractionItem`,
     `TechnicalExtraction`, `parse_technical_extraction` (619-754),
     `SynthesisViolation`/`validate_synthesis` (755-883),
     `technical_extraction_to_json`/`_from_json` (944-1014).
  3. Utilitaires de normalisation de texte partagés par les deux familles
     (lignes 271-459 : `normalize_text`, `_fold`, `_split_blocks`, etc.).
- **Symboles majeurs** : ~10 classes/enums, ~20 fonctions.
- **Fanout** : 4 importeurs (`production_stages.py`, `semantic_annotation.py`,
  `production_workflow.py`, `production_rendering.py`, `publication_builder.py`)
  — modéré, cohérent avec un module de parsing central.
- **Résultat pertinent** : une tâche « corriger `parse_technical_extraction` »
  touche les modèles de données (206-270), la fonction de parsing (619-754), la
  validation (755-883) et le JSON (944-1014) — 3-4 zones non contiguës, mais
  toutes dans la même famille "technical extraction" ; le rapport de référence
  n'est jamais concerné dans ce cas. C'est le critère 1 vérifié, mais de façon
  moins nette que DiscoveryPanel (les zones touchées restent toutes dans une
  moitié cohérente du fichier).
- **Verdict : B — split potentiellement utile** (priorité inférieure aux deux
  candidats ci-dessus).

---

## 8. backend/src/cti_app/application/model_gateway.py — 35.1 KB / 907 lignes

- **Responsabilité principale** : `ModelGateway`, point d'entrée unique pour
  exécuter un appel modèle de façon sûre (recherche, extraction, rédaction,
  critique) avec routage, sanitation, retry et reprise après incident.
- **Responsabilités secondaires** : modèles de données/erreurs du domaine
  (`ModelRequest`, `SafeModelRequest`, `ConversationContext`, etc.), les
  Protocols d'adaptateur (`ModelAdapter`, `ResearchModel`, ...), `ModelRouter`.
- **Symboles majeurs** : ~25 classes/dataclasses/Protocols (essentiellement des
  définitions de types, courtes) + 1 classe principale `ModelGateway` avec
  ~17 méthodes, + quelques fonctions de sanitation module-level.
- **Fanout** : 11 importeurs — le plus large de tous les fichiers audités, mais
  attendu : c'est l'abstraction centrale que toute la couche modèle consomme.
  Chaque importeur n'utilise typiquement qu'un sous-ensemble stable (le
  Protocol `ResearchModel` OU `ModelGateway.execute`), pas l'ensemble — pas de
  fanout de *changement*, seulement de *lecture*.
- **Résultat pertinent** : les définitions de types (lignes 31-338) sont lues
  une fois puis stables ; le travail réel se concentre dans `ModelGateway`
  (339-802), notamment `_execute`/`_complete_run`/`resume`, zone contiguë.
- **Verdict : A — gros mais cohérent.** Beaucoup de code est déclaratif
  (dataclasses/Protocols nécessaires à la frontière du module) plutôt que de la
  logique dupliquée ; la classe principale reste un seul owner (l'exécution
  sûre d'un appel modèle).

---

## 9. backend/src/cti_app/application/briefs.py — 31.7 KB / 764 lignes

- **Responsabilité principale** : `BriefService`, cycle de vie complet d'une
  brève (freeze du pack de preuves, génération, révision, approbation,
  promotion, QA, décisions humaines).
- **Responsabilités secondaires** : modèles de sortie structurée du modèle
  (`BriefSentenceOutput`, `BriefBlockOutput`, `BriefDraftOutput`,
  `BriefQaResult`), quelques fonctions utilitaires module-level de
  sérialisation/diff.
- **Symboles majeurs** : 1 classe (~12 méthodes) + 4 modèles Pydantic courts +
  quelques fonctions utilitaires.
- **Fanout** : 4 importeurs (workers, api/briefs.py, jobs) — usage centralisé.
- **Résultat pertinent** : chaque méthode couvre une étape du cycle de vie
  (freeze → generate → revise → approve → promote → qa) ; les tâches observées
  restent dans une méthode.
- **Verdict : A — gros mais cohérent.** Même profil que
  `production_workflow.py`/`collection.py` : une classe de service dont les
  méthodes forment un seul cycle de vie métier, pas un mélange d'owners.

---

## 10. backend/src/cti_app/api/discovery.py — 25.3 KB

Sous le seuil de 30 KB fixé par la tâche — non audité en détail. Signalé pour
mémoire au cas où une future mesure le repasserait au-dessus du seuil.

---

# Synthèse — candidats de refactor, classés par ROI

| # | Fichier | Verdict | Critère déclenché |
|---|---|---|---|
| 1 | `frontend/src/features/discovery/DiscoveryPanel.tsx` | B | 1 (>2 zones non contiguës) + 2 (workflows indépendants : découverte, recovery, import manuel, liste candidats) |
| 2 | `backend/src/cti_app/integrations/models.py` | B | 3 (3 owners séparables : transports HTTP, adaptateurs par fournisseur, stores) |
| 3 | `backend/src/cti_app/application/production_parsers.py` | B | 1 (zones non contiguës par famille de parser) — priorité la plus faible des trois, chevauchement partiel via les utilitaires partagés |

## Candidat 1 — DiscoveryPanel.tsx (ROI le plus élevé)

- **Frontière exacte** : extraire 3 sous-composants du corps actuel de
  `DiscoveryPanel` : `DiscoveryRecoveryPanel` (état + JSX lignes ~509-660,
  incluant l'import manuel imbriqué ou en 4e composant séparé),
  `DiscoveryCandidateList` (filtres + stats + liste, lignes ~666-1010), et
  garder `DiscoveryPanel` comme composant hôte pour le lancement/relance
  (state `discovery`, `launch`, `relaunch`) + composition des sous-composants.
- **Nouveaux owners possibles** : `DiscoveryLaunchPanel` (hôte),
  `DiscoveryRecoveryPanel`, `DiscoveryImportPanel`, `DiscoveryCandidateList`.
- **Nombre probable de fichiers** : 4 (3 nouveaux + le fichier hôte réduit).
- **Bénéfice contextuel attendu** : une tâche sur le workflow de récupération
  ne charge plus ~990 lignes mais un composant ciblé de ~150-250 lignes.
- **Risque** : état partagé entre sections (ex. `jobId`/`jobStatus` utilisés à
  la fois par le lancement et la récupération) impose de bien définir les
  props/callbacks de communication entre composants extraits — risque de
  régression sur la synchronisation d'état si mal fait.

## Candidat 2 — integrations/models.py

- **Frontière exacte** : séparer en 3 fichiers : `integrations/transports.py`
  (Protocols + `HttpResponsesTransport` + `BridgeTransportError` +
  `ChatGPTBridgeTransport` + `HttpChatCompletionsTransport`),
  `integrations/adapters.py` (les 4 adaptateurs fournisseur), et
  `integrations/stores.py` (les 2 stores). Les fonctions utilitaires de
  sérialisation en fin de fichier suivent leur principal appelant (la plupart
  servent aux adaptateurs `Responses`).
- **Nouveaux owners possibles** : `integrations/transports.py`,
  `integrations/adapters.py`, `integrations/stores.py`.
- **Nombre probable de fichiers** : 3 (+ éventuellement garder `models.py`
  comme ré-export pour ne pas casser `model_factory.py`).
- **Bénéfice contextuel attendu** : ajouter/modifier un adaptateur fournisseur
  n'a plus besoin de charger la logique HTTP bas niveau ni les stores.
- **Risque** : faible — un seul importeur externe (`model_factory.py`) à
  mettre à jour ; les classes n'ont pas de dépendances circulaires apparentes
  entre les 3 groupes (adapters dépendent des transports, pas l'inverse).

## Candidat 3 — production_parsers.py (ROI le plus faible des trois)

- **Frontière exacte** : séparer en `production_parsers_reference.py`
  (modèles + `parse_reference_report` + JSON reference report) et
  `production_parsers_extraction.py` (modèles + `parse_technical_extraction`
  + `validate_synthesis` + JSON technical extraction), avec un
  `production_parsers_shared.py` pour les utilitaires de normalisation de
  texte communs (`normalize_text`, `_fold`, `_split_blocks`, etc.).
- **Nouveaux owners possibles** : parsing "reference report", parsing
  "technical extraction", utilitaires partagés.
- **Nombre probable de fichiers** : 3.
- **Bénéfice contextuel attendu** : modéré — les deux familles sont déjà
  groupées par blocs contigus dans le fichier actuel, donc le gain de
  navigation est réel mais plus faible que pour les deux candidats précédents.
- **Risque** : moyen — le fichier partagé de normalisation crée une dépendance
  entre les deux nouveaux fichiers ; 5 importeurs externes à vérifier/mettre à
  jour (imports probablement déjà nommés, donc mécanique).

---

Aucun autre fichier audité ne dépasse le seuil de justification (A pour
`content.js`, `production_workflow.py`, `collection.py`,
`discovery/cumulative/service.py`, `model_gateway.py`, `briefs.py`).
