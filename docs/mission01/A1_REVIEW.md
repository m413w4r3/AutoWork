# Revue indépendante M01-A1

**Cible :** `cd344743e9ba12d01d509cc8b5f9e42a0f3cb239` (`feat/mission01-a1-strategy-projection`), base `058bb4af2278e3941956e523c5a1d560208de228`. Intégration examinée : `27e99e4769e1f12ca9b717342dbad0e0eefc6fcb`.

**Re-revues :** `da79486dd19cb4f01ecebfb01fa199a282f47472`, puis `a0aaee626206169401b3607eedd7807644ae1dbe`.

**Verdicts historiques : FAIL après cd34474, da79486 et a0aaee6. Verdict final après 27db077 : FAIL.** `27db077` masque les `step_ids` qui ne sont pas UUID/SHA et ajoute un libellé d’assertion. La revue trouve encore des références SHA validées par leur seule forme et des métadonnées d’outil acceptées par regex, sans liaison canonique obligatoire; elles peuvent réfléchir du contenu choisi dans `experiment.list` et GET. Le frontend ne consomme pas le nouveau libellé.

Les statuts ci-dessous qualifient les constats de revue; `CONFIRMED` indique un comportement directement établi par le code ou les tests, pas une validation en production.

## Constats au commit cd34474

### A1-R1 — La réponse peut retourner `REFUTED` sans preuve complète et persistée

**CONFIRMED · BLOCKER**

`build_investigation_experiment_timeline()` calcule `claims` par `consolidate_claim_experiments()` ([investigation_experiments.py:160-174](../../backend/src/cti_app/application/investigation_experiments.py#L160)). Le consolidateur choisit `REFUTED` depuis le seul verdict du dernier enregistrement actif ([investigation_experiment.py:463-496](../../backend/src/cti_app/domain/investigation_experiment.py#L463)); il ne consulte ni les observations persistées, ni leur SHA, ni la disponibilité, ni la couverture, ni le résultat observé, ni le critère.

La stratégie parallèle applique ces gardes ([investigation_experiment.py:526-572](../../backend/src/cti_app/domain/investigation_experiment.py#L526)). L’API renvoie les deux calculs dans la même réponse ([api/investigation.py:1062-1082](../../backend/src/cti_app/api/investigation.py#L1062)). Une expérience `REFUTES` à couverture limitée avec résultat `OBSERVED` et une référence d’observation satisfait les validations de construction ([investigation_experiment.py:284-303](../../backend/src/cti_app/domain/investigation_experiment.py#L284)), mais la matrice la garde `UNKNOWN` tandis que `claims.status` devient `refuted`. De même, un objet d’observation référencé absent ou avec un SHA incorrect laisse `claims` à `refuted` même si la matrice retourne `UNKNOWN`.

Le test d’observation étrangère ne vérifie que `strategy.claims.status` et `matrix_status`, pas `payload["claims"]` ([test_investigation_experiment_api.py:444-470](../../backend/tests/test_investigation_experiment_api.py#L444)). Cela laisse passer le cas fautif.

**Correction requise :** calculer le statut de `claims` avec la même projection conservatrice et les mêmes objets d’observation vérifiés, ou ne pas émettre de statut concluant dans cette liste. Ajouter les cas couverture partielle, observation absente/mismatch et outcome incompatible sur la réponse API complète.

### A1-R2 — Le critère de falsification n’est pas contrôlé pour sa pertinence

**CONFIRMED · HIGH**

La projection accepte tout critère non vide autre que quatre chaînes génériques (`none`, `unknown`, `n/a`, `not applicable`) avant de retourner `REFUTED` ([investigation_experiment.py:559-572](../../backend/src/cti_app/domain/investigation_experiment.py#L559)). La construction borne sa longueur mais ne relie pas le critère à la prédiction ou au claim ([investigation_experiment.py:212-218](../../backend/src/cti_app/domain/investigation_experiment.py#L212)). Un texte sans rapport peut donc satisfaire la garde.

La carte de stratégie demandait un critère pertinent et signalait déjà que le texte du critère/verdict reste une assertion opérateur sans oracle sémantique ([CTI_STRATEGY_MAP.md:7,17,101](../../docs/mission01/CTI_STRATEGY_MAP.md#L7)). Les tests vérifient les valeurs structurelles, pas la pertinence de la falsification. Pour respecter le contrat, la pertinence doit être une décision explicitement qualifiée ou un champ structuré vérifiable; le texte libre seul ne suffit pas.

### A1-R3 — Le timeline API peut renvoyer du contenu libre non expurgé

**CONFIRMED · HIGH**

Le schéma autorise toute propriété dans `tool_arguments` ([investigation_experiments.py:743-746](../../backend/src/cti_app/application/investigation_experiments.py#L743)). Le validateur interdit plusieurs noms de clés sensibles et les préfixes `Bearer`/`Basic`, mais accepte toute autre chaîne JSON jusqu’à 4 096 caractères ([investigation_experiment.py:91-108](../../backend/src/cti_app/domain/investigation_experiment.py#L91)). Le sérialiseur rend les arguments sans transformation ([investigation_experiment.py:315-340](../../backend/src/cti_app/domain/investigation_experiment.py#L315)); le timeline inclut aussi les champs libres `prediction`, `expected`, `actual`, `scope`, `limitations` et `implication` ([investigation_experiments.py:140-157](../../backend/src/cti_app/application/investigation_experiments.py#L140)). La route retourne le timeline entier ([api/investigation.py:1080-1082](../../backend/src/cti_app/api/investigation.py#L1080)).

La route ne lit pas les blobs d’observation et ne renvoie leurs références que sous forme d’IDs/SHA; toutefois, un sample encodé sous une clé neutre comme `payload`, ou du texte de preuve copié dans `actual`, traverse ces validations et est renvoyé. Les fixtures API utilisent `tool_arguments={}` et ne testent pas cette frontière ([test_investigation_experiment_api.py:68-110](../../backend/tests/test_investigation_experiment_api.py#L68)). La réponse devrait exposer une projection avec champs autorisés et des références, pas les arguments et textes libres intégraux.

### A1-R4 — Les tests API n’exercent pas la route FastAPI réelle

**CONFIRMED · MEDIUM**

Le helper de test construit un `Request`, appelle directement `get_investigation_experiments()`, puis fait `model_dump()` lui-même ([test_investigation_experiment_api.py:207-239](../../backend/tests/test_investigation_experiment_api.py#L207)). Il ne passe pas par `create_app()`, l’inclusion du router, le middleware d’identité ou la sérialisation FastAPI `response_model`. Le test de l’allowlist opérateur couvre uniquement la fonction de décision, pas une requête authentifiée réelle ([test_operator_client_routes.py:11-45](../../backend/tests/test_operator_client_routes.py#L11)).

Le middleware réel protège `/api/` et restreint le scope opérateur client ([api/main.py:631-657](../../backend/src/cti_app/api/main.py#L631)); la route `experiments` est dans son allowlist GET ([api/main.py:783-824](../../backend/src/cti_app/api/main.py#L783)). Je n’ai pas trouvé de contournement dans ce code. Il reste cependant un trou de test matériel pour l’authentification effective, le câblage UOW et la sérialisation HTTP de cette route.

### A1-R5 — Agrégation de couverture du checkpoint dupliquée

**PLAUSIBLE · LOW**

La stratégie calcule sa couverture sur les expériences actives après supersession ([investigation_experiments.py:255-264](../../backend/src/cti_app/application/investigation_experiments.py#L255)); le checkpoint la recalcule sur toutes les expériences tentées, y compris celles supersédées ([investigation_operator_view.py:71-84](../../backend/src/cti_app/application/investigation_operator_view.py#L71)). Une ancienne tentative limitée peut donc afficher `limited` dans le checkpoint alors que la stratégie active est `complete`. Clarifier la sémantique cumulative ou mutualiser la réduction éviterait deux interprétations du même champ.

### Complexité — amélioration, hors blocage supplémentaire

Le commit ajoute **1 195 lignes** dans 9 fichiers : 450 lignes de production et 745 lignes de tests (avec 15 suppressions au total). Le défaut de statut distinct entre `claims` et `strategy` (A1-R1) est le risque principal et bloque l’acceptation. La consolidation du verdict et la projection de stratégie recalculent la même conclusion avec des gardes différentes; une projection unique éviterait cette divergence. À moindre sévérité, les deux réductions de couverture (A1-R5) peuvent être mutualisées ou leur portée documentée. Le reste de la taille vient surtout des scénarios offline et des tests API; aucune nouvelle couche de persistance n’est ajoutée.

## Contrats vérifiés à la première revue

- **source_ref/snapshot — CONFIRMED, avec une limite de test.** La projection exige le même artifact, sujet, run, version positive égale à celle du snapshot, SHA de forme SHA-256 et références sûres non vides; sinon elle renvoie `UNKNOWN` et vide les refs ([investigation_experiments.py:194-207,231-269](../../backend/src/cti_app/application/investigation_experiments.py#L194)). Le resolver charge uniquement un artifact `SYNTHESIS` vérifié lié à l’investigation, calcule le SHA sur le contenu canonique et joint la version de cet artifact ([investigation_article_snapshot.py:62-76,146-150,233-255](../../backend/src/cti_app/application/investigation_article_snapshot.py#L62)). Les modes `wrong_snapshot`, `version_mismatch` et `bad_sha` restent `UNKNOWN`; une observation d’une autre investigation aussi ([test_investigation_experiment_api.py:386-470](../../backend/tests/test_investigation_experiment_api.py#L386)). Un snapshot sans version est également refusé par le garde d’égalité, mais aucun test distinct ne couvre un ancien objet sans `synthesis_version`.
- **Raisons opérateur — CONFIRMED pour les raisons émises.** `budget` vient de `StopReason.BUDGET_EXHAUSTED`, `refus_g2` de l’état d’autorisation révoqué passé par la route, l’indisponibilité d’un champ persisté `availability`, et `deja_testee` d’une identité d’essai calculée depuis les enregistrements. Aucun `ABANDONED` n’est inféré; l’état d’hypothèse est seulement projeté depuis l’état de travail ([investigation_operator_view.py:58-141](../../backend/src/cti_app/application/investigation_operator_view.py#L58), [api/investigation.py:1061-1079](../../backend/src/cti_app/api/investigation.py#L1061)). `non_discriminante` n’est pas émis; les cas non prouvés retombent sur `inconnue`. Le motif G2 est limité à la révocation, pas à toute décision de refus.
- **Lecture seule et primitives existantes — CONFIRMED.** La route est `GET` et ne fait que charger investigation, timeline, état, observations, snapshot, enveloppe et décisions avant projection ([api/investigation.py:1001-1082](../../backend/src/cti_app/api/investigation.py#L1001)). Le diff A1 ne touche ni migration, modèle SQL, UOW ni dépôt; les expériences réutilisent le protocole de persistance et le dépôt SQL déjà présents, ainsi que la table C2 existante ([investigation_persistence.py:70-82](../../backend/src/cti_app/application/investigation_persistence.py#L70), [repositories/investigation.py:536-570](../../backend/src/cti_app/infrastructure/database/repositories/investigation.py#L536), [models/investigation.py:230-256](../../backend/src/cti_app/infrastructure/database/models/investigation.py#L230)). Aucun endpoint de spawn opérateur ni stockage canonique secondaire n’est ajouté. Le garde d’écriture d’expérience exige toujours l’enveloppe G2 courante ([investigation_experiments.py:76-87,420-440](../../backend/src/cti_app/application/investigation_experiments.py#L76)).
- **Scénarios/YARA — CONFIRMED au niveau des fixtures offline.** Les six scénarios utilisent des ports fake; la fixture interdit sockets et `urlopen` ([test_investigation_strategy_scenarios.py:48-68](../../backend/tests/test_investigation_strategy_scenarios.py#L48)). Le scénario YARA utilise un faux moteur/corpus, conserve `SYNTHETIC_ONLY`, vérifie les dénominateurs et les dénominateurs réels à zéro, sans promotion en `REAL_VALIDATED` ([test_investigation_strategy_scenarios.py:274-363](../../backend/tests/test_investigation_strategy_scenarios.py#L274)). Cela ne valide pas un transport live ni une généralisation YARA réelle.
- **B0A/merge — REJECTED comme risque d’écrasement dans ce fichier.** Le blob `research_context_builder.py` au premier parent `f52e067` est identique à celui de la base `058bb4a`; il n’y a pas de delta B0A séparé sur ce fichier dans l’intégration inspectée. Le diff `f52e067..27e99e4` est l’ajout A1 de `synthesis_version` optionnelle et sa validation entière positive hors booléen ([research_context_builder.py:150-158](../../backend/src/cti_app/application/research_context_builder.py#L150)); le blob fusionné correspond à celui de `cd34474`. Aucun contenu de premier parent n’est écrasé. Les deux échecs de tests sur le merge sont déjà présents sur le parent B0A avant A1 (détails ci-dessous).

## Tests et checks de la première revue

Depuis une archive de `cd34474`, avec le `.venv` existant et les tests configurés sans accès réseau ou base :

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_investigation_strategy_scenarios.py tests/test_investigation_experiments.py tests/test_investigation_experiment_api.py tests/test_operator_tool_invocation.py -q --tb=short` — **53 passed** : 6 scénarios, 25 tests domaine/API et 22 tests d’invocation opérateur.
- Ruff ciblé sur les 9 fichiers A1 modifiés — **All checks passed**.
- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_operator_client_routes.py -q --tb=short` — **42 passed** (allowlist fonctionnelle seulement).

Depuis une archive de l’intégration `27e99e4`, la même commande pytest donne **51 passed, 2 failed** :

- `test_operator_dispatcher_wires_minimal_advisor_request_to_strict_bridge_transport`
- `test_advisor_recommendations_record_acceptance_refusal_and_empirical_outcomes`

Les deux échouent dans le contrôle B0A `required_canonical_evidence_not_in_payload` / `tool_unavailable`. Les mêmes deux tests échouent sur le parent pré-A1 `f52e067`; ils ne sont donc pas une régression introduite par A1. Ruff ciblé sur le merge — **All checks passed**. Ces tests et fixtures ne contactent aucun fournisseur, PostgreSQL ou Docker. Le test d’API A1 appelle toutefois directement le handler, comme décrit en A1-R4. Le test PostgreSQL d’intégration n’a pas été exécuté.

## Inconnues et suites du premier rapport

- **UNKNOWN :** le comportement du dépôt PostgreSQL et de la route montée dans l’application n’est pas couvert par les tests offline exécutés; les tests d’API fournis utilisent un UOW fake.
- **UNKNOWN :** la pertinence sémantique de tout critère de falsification et le contenu réel qui pourrait être enregistré dans les champs libres ne sont pas évalués par les fixtures.
- Coût d’exécution : **UNKNOWN**.

À la date du premier rapport, le contrat d’intégration devait être retesté après correction de A1-R1 et A1-R3, avec un test ASGI authentifié sur la route complète. Les deux tests B0A échouants restent une réparation distincte du merge A1.

## Re-revue après da79486

Cette section évalue `da79486dd19cb4f01ecebfb01fa199a282f47472` par rapport à `cd344743e9ba12d01d509cc8b5f9e42a0f3cb239`; elle remplace le verdict initial pour les constats repris ci-dessous.

### A1-R1 — Statut de `payload.claims`

**REJECTED · corrigé pour la route HTTP (sévérité initiale BLOCKER).**

La route construit `strategy`, puis passe le payload de timeline par `_safe_experiment_timeline_payload()` avant de joindre les deux dans la réponse ([api/investigation.py:1069-1089](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L1069)). La fonction remplace `claims[].status` par le statut stratégie; `UNKNOWN` devient `inconclusive`, tandis que `verdicts` reste visible comme assertion enregistrée ([api/investigation.py:1093-1116](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L1093)). La projection accepte le soutien uniquement pour `SUPPORTS` + `OBSERVED`; tout verdict de réfutation retourne `UNKNOWN`, après contrôle de disponibilité, couverture complète, présence d’observations et correspondance ID/SHA ([investigation_experiment.py:526-567](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/domain/investigation_experiment.py#L526)).

Les tests de la route montée couvrent couverture partielle, observation absente, SHA discordant et outcome incompatible. Ils vérifient la réponse entière, l’absence de `refuted` et la visibilité de `verdicts: ["refutes"]` ([test_investigation_experiment_api.py:363-412](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L363)). La divergence signalée dans le premier rapport est fermée pour cette route.

### A1-R2 — Critère libre et autres sorties de verdict

**CONFIRMED · HIGH — reste ouvert hors de la route HTTP.**

Le projecteur stratégie documente qu’un critère en prose n’a pas d’oracle sémantique et ne peut jamais prouver `REFUTED`; un test avec critère hors sujet retourne `UNKNOWN` ([investigation_experiment.py:531-567](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/domain/investigation_experiment.py#L531), [test_investigation_experiments.py:231-248](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiments.py#L231)). Cette limite est donc annoncée honnêtement pour cette projection.

La consolidation historique n’a pas été modifiée : `consolidate_claim_experiments()` continue à choisir `REFUTED` d’après le dernier verdict actif ([investigation_experiment.py:473-488](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/domain/investigation_experiment.py#L473)). L’outil `experiment.record` renvoie cette consolidation dans `claim_verdicts` ([investigation_experiments.py:553-568](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L553)); `experiment.list` retourne le timeline brut, y compris ses claims consolidés ([investigation_experiments.py:572-585](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L572)). Un critère libre hors sujet peut donc encore sortir comme `status: refuted` par ces outils, même si le GET HTTP dit `inconclusive`. Aucun nouveau test ne couvre ces deux sorties.

### A1-R3 — Expurgation et préservation des références

**CONFIRMED · HIGH — amélioration partielle, risque résiduel.**

La réponse GET remplace les champs libres de preuve par un marqueur, supprime préconditions et limitations, et le test vérifie qu’un texte contenant un secret factice sous la clé neutre `payload` est absent ([api/investigation.py:1118-1163](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L1118), [test_investigation_experiment_api.py:414-445](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L414)). Ce cas testé est fermé.

La fonction conserve toutefois jusqu’à 32 valeurs numériques ou booléennes sous des clés génériques autorisées ([api/investigation.py:1180-1200](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L1180)); le schéma d’écriture accepte toujours toute propriété dans `tool_arguments` ([investigation_experiments.py:743-746](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L743)). Un corps peut donc être encodé en octets numériques sous des clés neutres et survivre à ce filtre. En outre, `input_id` n’a qu’une validation de chaîne/longueur dans le domaine ([investigation_experiment.py:111-124](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/domain/investigation_experiment.py#L111)), le schéma ne lui impose aucune sémantique canonique ([investigation_experiments.py:723-736](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L723)), et la réponse le préserve dès qu’il respecte la regex d’identifiant ([api/investigation.py:1203-1234](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L1203)). Un identifiant arbitraire de type hexadump ou secret alphanumérique peut ainsi être reflété comme référence. La sortie de `experiment.list` contourne entièrement l’expurgation HTTP en renvoyant le timeline brut (lien ci-dessus).

Ce nouveau filtre est une liste blanche locale; il n’appelle pas `redact_operational_secrets()` ([publication_secret_guard.py:118-153](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/publication_secret_guard.py#L118)). Cet utilitaire seul ne couvre pas les corps de sample, mais l’absence d’une garde commune et le passage de valeurs scalaires/références non validées laissent le contrat « aucun corps de preuve/sample/secret » non établi. Les tests n’attaquent que les chaînes de `tool_arguments` et les textes libres.

### A1-R4 — Route FastAPI montée et lifespan

**CONFIRMED · MEDIUM — route testée, initialisation de production non couverte.**

Le nouveau test utilise `create_app()`, `ASGITransport`, traverse le middleware d’identité et la sérialisation `response_model`; il obtient `401` sans jeton et `200` avec jeton ([test_investigation_experiment_api.py:351-360](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L351)). Cela ferme le trou précédent pour le routage et l’authentification HTTP.

L’ASGI test ne démarre cependant pas le lifespan : le helper injecte lui-même un faux `uow_factory` ([test_investigation_experiment_api.py:251-269](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L251)). En production, la route dépend de ce factory, que le lifespan installe après création de l’engine PostgreSQL ([api/main.py:171-180,494-496](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/main.py#L171)). Le test masque donc une éventuelle erreur de câblage au démarrage. Ce chemin reste UNKNOWN en offline; aucun PostgreSQL n’a été lancé.

### A1-R5 — Couverture checkpoint/stratégie

**REJECTED · corrigé (sévérité initiale LOW).**

Le checkpoint agrège maintenant les valeurs de couverture déjà projetées par claim, comme la stratégie; les IDs d’expériences tentées restent historiques ([investigation_operator_view.py:65-88](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_operator_view.py#L65)). Le test supersède une tentative limitée par une tentative complète et vérifie à la fois `complete` dans la stratégie et le checkpoint, tout en conservant les deux IDs ([test_investigation_strategy_scenarios.py:275-299](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_strategy_scenarios.py#L275)).

### Autres contrats et absence de régression observée

**CONFIRMED**, dans le delta `cd34474..da79486` : seuls `api/investigation.py`, `application/investigation_operator_view.py`, `domain/investigation_experiment.py` et trois fichiers de tests sont modifiés. Pas de migration, schéma, seconde base, endpoint de spawn, modification de G2 ou écriture canonique ajoutée; `research_context_builder.py` de B0A n’est pas touché. La lecture de snapshot/source et les raisons checkpoint antérieures ne sont pas affaiblies par ce delta. Les tests exécutés restent offline, avec UOW et observations factices; ils n’appellent ni réseau, fournisseur, PostgreSQL ni Docker.

## Tests et checks de la re-revue

Depuis une archive de `da79486dd19cb4f01ecebfb01fa199a282f47472` avec le `.venv` existant :

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_investigation_strategy_scenarios.py tests/test_investigation_experiments.py tests/test_investigation_experiment_api.py tests/test_operator_tool_invocation.py -q --tb=short` — **61 passed in 5.03s**.
- `.venv/bin/ruff check src/cti_app/api/investigation.py src/cti_app/application/investigation_operator_view.py src/cti_app/domain/investigation_experiment.py tests/test_investigation_experiment_api.py tests/test_investigation_experiments.py tests/test_investigation_strategy_scenarios.py` — **All checks passed**.

Le test API vérifie bien 401/200 et `response_model`, mais il n’exécute pas le lifespan; le câblage PostgreSQL reste UNKNOWN. Aucun test exécuté ne vérifie que `experiment.record/list` suppriment les conclusions ou le contenu libre. Aucun test séparé ne couvre l’encodage de sample en valeurs numériques ou la validation canonique de `input_id`.

## Verdict final après da79486

**FAIL — 3 constats résiduels :** A1-R2 **HIGH**, A1-R3 **HIGH**, A1-R4 **MEDIUM** (route HTTP couverte; lifespan non vérifié). A1-R1 et A1-R5 sont fermés pour les chemins projetés par la route GET. Le coût est **UNKNOWN**.

## Re-revue finale après a0aaee6

Cette re-revue examine le delta `da79486dd19cb4f01ecebfb01fa199a282f47472..a0aaee626206169401b3607eedd7807644ae1dbe`. Le delta touche quatre fichiers : `backend/src/cti_app/api/investigation.py`, `backend/src/cti_app/application/investigation_experiments.py` et leurs deux fichiers de tests. Aucun changement de production hors de ces deux modules.

### A1-R1 — `payload.claims` et stratégie conservatrice

**REJECTED · fermé, sans régression.** La route GET passe maintenant le timeline complet par le même projecteur de sécurité que les outils, en lui fournissant la stratégie calculée ([api/investigation.py:1081-1085](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L1081)). Le statut d’un claim vient de la stratégie; en absence de statut vérifié il devient `inconclusive` ([investigation_experiments.py:177-191](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L177)). La projection domaine exige disponibilité, couverture complète, observations persistées présentes et correspondance ID/SHA; elle ne retourne `SUPPORTED` que pour `SUPPORTS` + `OBSERVED`, et laisse les autres cas à `UNKNOWN` ([investigation_experiment.py:531-567](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/domain/investigation_experiment.py#L531)). Les tests vérifient la réponse API complète pour couverture partielle, observation absente, SHA discordant et outcome incompatible : aucun ne donne `refuted` ([test_investigation_experiment_api.py:371-420](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L371)).

### A1-R2 — `experiment.record/list` et critère libre

**REJECTED · risque HIGH fermé. PLAUSIBLE · LOW sur la présentation.** `record` et `list` appellent tous deux `project_safe_investigation_experiment_timeline()` sans stratégie; le statut de claim retombe donc sur `inconclusive` ([investigation_experiments.py:696-704,723-729](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L696)). Le test utilise un critère hors sujet et vérifie les deux sorties : les statuts restent inconclusifs tandis que `refutes` demeure présent dans les champs de verdict enregistré ([test_investigation_experiments.py:486-547](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiments.py#L486)). La limitation est documentée : la prose d’un critère n’a pas d’oracle sémantique et ne peut établir une réfutation ([investigation_experiment.py:531-538](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/domain/investigation_experiment.py#L531)).

Le verdict persisté reste séparé du statut calculé, mais le champ est nommé `verdict`/`verdicts` sans marqueur explicite « assertion ». Le frontend affiche une expérience comme « Réfute » ([InvestigationLabViews.tsx:48-60,445-451](../../../feat-mission01-a1-strategy-projection/frontend/src/features/investigation/InvestigationLabViews.tsx#L48)); le statut agrégé affiché pour le claim peut toutefois rester « Non concluant ». C’est une ambiguïté de présentation plausible, pas une fausse conclusion de projection.

### A1-R3 — assainissement des sorties et références

**CONFIRMED · HIGH — reste ouvert.** `tool_arguments` est vidé dans le projecteur commun, les champs libres sont remplacés avant l’appel à `redact_operational_secrets()`, et les préconditions/limitations sont retirées ([investigation_experiments.py:196-253](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L196)). `record`, `list` et GET utilisent ce projecteur. Les tests attaquent les valeurs de paramètres sous clé neutre, un hexdump/URL dans les arguments, des scalaires numériques et le secret factice dans du texte; ces valeurs sont absentes. Un `input_id` hexadécimal de 128 caractères est expurgé, tandis qu’une paire SHA-256 de 64 caractères cohérente est conservée ([test_investigation_experiment_api.py:422-466](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L422), [test_investigation_experiments.py:486-547](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiments.py#L486)).

En revanche, `step_ids` accepte jusqu’à 256 caractères quelconques dans le schéma d’écriture ([investigation_experiments.py:890-894](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L890)) et le projecteur les conserve s’ils ne contiennent que lettres, chiffres, `.`, `_`, `:` ou `-` ([investigation_experiments.py:246-250](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L246)). Le domaine stocke ces IDs tels quels ([investigation_experiments.py:653-661](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L653)). Vérification directe du projecteur avec `step_ids=["deadbeef" * 16]` : cette valeur de 128 caractères reste dans la sortie; le même projecteur sert à `experiment.list` et au GET. Les tests ne couvrent pas ce chemin. `tool_name`, `tool_version` et `tool_schema_version` suivent aussi une validation de caractères permissive sans liaison à une invocation canonique ([investigation_experiments.py:212-224,886-888](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L212)). Ce chemin permet un reflet de contenu alphanumérique sous des clés qui ne sont pas des champs de preuve libres; le contrat « aucun contenu de sample/secret dans la réponse » n’est donc pas garanti.

### A1-R4 — route montée et lifespan

**CONFIRMED pour la route HTTP; UNKNOWN pour le câblage de production.** Le test passe par `create_app()`, ASGI, le middleware d’identité et le `response_model`, avec `401` sans jeton et `200` avec jeton. Il documente explicitement que le lifespan PostgreSQL n’est pas démarré et que le UOW est injecté dans le test ([test_investigation_experiment_api.py:352-368](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L352)). Cette limite est acceptée comme UNKNOWN, conformément à la portée offline; aucun moteur factice n’est présenté comme preuve du lifespan réel.

### Non-régression, frontend et tests

**CONFIRMED dans le delta inspecté.** La validation source/snapshot et les liens avec versions/SHA de stratégie ne sont pas changés; les raisons opérateur, règles G2, écriture canonique et endpoint GET ne sont pas modifiés par ce delta. Il ne touche ni `research_context_builder.py` (B0A), ni les fichiers B1A, migration, schéma, UOW ou modèle SQL; il n’ajoute ni seconde base, ni framework, ni API de spawn. Les sorties gardent les types attendus par `frontend/src/api/investigationExperiments.ts` (`tool_arguments` reste un objet, champs texte des chaînes, `input_refs` garde `input_id` et `sha256|null`); aucune incompatibilité de type n’est apparue. L’UI reçoit des champs masqués sous forme de chaînes compatibles.

Depuis une archive de `a0aaee626206169401b3607eedd7807644ae1dbe`, avec le `.venv` du dépôt lié :

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_investigation_strategy_scenarios.py tests/test_investigation_experiments.py tests/test_investigation_experiment_api.py tests/test_operator_tool_invocation.py -q --tb=short` — **61 passed in 5.20s**.
- `.venv/bin/ruff check src/cti_app/api/investigation.py src/cti_app/application/investigation_experiments.py tests/test_investigation_experiment_api.py tests/test_investigation_experiments.py` — **All checks passed**.
- Vérification directe du projecteur avec un ID alphanumérique long dans `step_ids` — la valeur passe, confirmant le constat A1-R3.

Les tests ont été exécutés hors ligne avec les doubles de test existants; aucun fournisseur, réseau, PostgreSQL ou Docker n’a été démarré. Le lifespan réel reste UNKNOWN. Coût : **UNKNOWN**.

## Verdict final après a0aaee6

**FAIL — 1 constat confirmé HIGH reste ouvert (A1-R3 : contenu alphanumérique arbitraire réfléchi via `step_ids` et métadonnées similaires).** A1-R1 et le risque de fausse réfutation dans `experiment.record/list` sont fermés; A1-R4 est limité à l’UNKNOWN documenté du lifespan. A1-R2 garde une ambiguïté de présentation PLAUSIBLE/LOW. Coût : **UNKNOWN**.

## Re-revue finale après 27db077

Cette re-revue examine `a0aaee626206169401b3607eedd7807644ae1dbe..27db077daf79455c6e3ca668501fb6722638653f`. Le delta ne modifie que `backend/src/cti_app/api/investigation.py`, `backend/src/cti_app/application/investigation_experiments.py` et leurs deux tests.

### Identifiants et sorties `record`, `list`, GET

**REJECTED · fermeture partielle du constat précédent `step_ids`.** Les identifiants qui ne correspondent pas au format attendu deviennent `[redacted]`; les tableaux dépassant les limites reçoivent un élément marqueur au lieu d’être tronqués silencieusement ([investigation_experiments.py:305-342,351-408](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L305)). Les références d’invocation et d’observation n’exposent que leurs champs connus; leurs IDs sont contrôlés comme UUID, les versions sont bornées, les SHA contrôlés en format ([investigation_experiments.py:385-432](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L385)). Les liens `supersedes` sont aussi vérifiés à l’écriture comme des expériences antérieures de la même investigation ([investigation_experiments.py:1228-1251](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L1228)).

Le test hostile des `step_ids` vérifie le résultat de `record` et `list`; le test route vérifie le GET monté avec l’ID hostile et plusieurs champs identifiants hostiles ([test_investigation_experiments.py:486-558](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiments.py#L486), [test_investigation_experiment_api.py:477-533](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L477)). Ces tests utilisent toutefois un ID long qui échoue aux formats, pas un contenu encodé dans un identifiant de forme valide.

**CONFIRMED · HIGH — un contenu peut encore être réfléchi comme référence SHA.** La validation de `input_refs.input_id` contrôle UUID/SHA et exige que `sha256 == input_id` pour une forme SHA, mais ne résout pas l’ID dans un dépôt canonique ([investigation_experiments.py:441-484](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L441)). L’adaptateur `record` construit ensuite la référence depuis les arguments sans charger l’objet source ([investigation_experiments.py:840-846](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L840)). `next_pivots` est également validé par motif SHA uniquement ([investigation_experiments.py:315-317](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L315)); `step_ids` accepte UUID ou SHA sans lien à un objet canonique. Vérification directe du projecteur : une valeur `deadbeef` répétée huit fois (64 caractères hex) passe dans `step_ids`, `next_pivots` et `input_refs.input_id` lorsque le SHA associé est identique. La même projection est utilisée par `experiment.list` et GET ([investigation_experiments.py:904,924-938](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L904)). Un SHA de forme valide est donc préservé même sans preuve qu’il désigne un artefact réel; les valeurs peuvent transporter 32 octets de contenu par référence. `record` n’inclut pas ces références dans son résultat direct, mais persiste les valeurs que `list` et GET reflètent.

**CONFIRMED · HIGH — métadonnées d’outil reflétables sans invocation canonique.** La projection conserve `tool_name`, `tool_version` et `tool_schema_version` s’ils correspondent à des regex, sans allowlist d’outils ([investigation_experiments.py:264-284](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L264)). Le schéma d’écriture n’impose que des chaînes bornées ([investigation_experiments.py:1095-1098](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L1095)); le contrôle contre une invocation persistée ne s’exécute que si `tool_invocation_refs` est non vide, et ne compare pas `tool_version` ([investigation_experiments.py:812-838](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L812)). Vérification directe : `tool_name="secret.payload"`, `tool_version="SensitiveData-1"` et `tool_schema_version="sensitive.payload.v1"` passent le projecteur avec une liste d’invocations vide. La stratégie GET expose aussi `tool_name` avec le même prédicat de forme ([investigation_experiments.py:614-624](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L614)). Le test hostile actuel met des valeurs trop longues/majuscules pour passer ces regex; il ne couvre pas ce cas de forme valide.

Les autres champs libres observés sont traités : `tool_arguments` est vide; `prediction`, critère, `method`, `expected`, `actual`, `scope` et `implication` sont masqués; préconditions et limitations sont vidées. `author` et `origin` sont fixés aux valeurs canoniques ([investigation_experiments.py:242-263,285-294,310-342](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L242)). Aucun champ `details` n’est présent dans le payload produit par le domaine, et le modèle HTTP interdit les propriétés supplémentaires ([api/investigation.py:387-398](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L387)). Les IDs de claim proviennent du snapshot et sont vérifiés à l’écriture; les IDs d’observation/invocation sont résolus et rattachés à l’investigation à l’écriture. Le risque A1-R3 reste néanmoins ouvert à cause des SHA non résolus et des noms/versions d’outil choisis librement.

### Libellé du verdict enregistré

**CONFIRMED · LOW — réponse backend corrigée, interface non mise à jour.** Les trois sorties gardent le champ machine `verdict: "refutes"` et ajoutent un libellé indiquant explicitement « assertion, non conclusion » ([investigation_experiments.py:435-438,906-912](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L435)). Le test vérifie le libellé sur `record`, `list`, les claims et GET ([test_investigation_experiments.py:529-552](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiments.py#L529), [test_investigation_experiment_api.py:522-533](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L522)). Le champ machine reste inchangé. Le frontend ne déclare pas le nouveau libellé et continue de présenter `experiment.verdict` comme « Réfute » ([investigationExperiments.ts:27-68](../../../feat-mission01-a1-strategy-projection/frontend/src/api/investigationExperiments.ts#L27), [InvestigationLabViews.tsx:48-60,445-451](../../../feat-mission01-a1-strategy-projection/frontend/src/features/investigation/InvestigationLabViews.tsx#L48)). Le JSON reste compatible grâce aux champs additifs, mais l’utilisateur de l’interface ne voit pas l’avertissement.

### Non-régression et tests

**CONFIRMED dans le delta inspecté.** Sans stratégie vérifiée, les claims restent `inconclusive`; la projection commune conserve `tool_arguments: {}` et appelle `redact_operational_secrets()`. Les champs libres et la provenance source/snapshot restent protégés par les mêmes chemins précédemment examinés. Le GET reste en lecture seule; le contrôle G2 de `record` demeure en place. Le test ASGI documente que le lifespan PostgreSQL n’est pas démarré et que ce câblage reste **UNKNOWN** ([test_investigation_experiment_api.py:360-375](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L360)). Le delta ne touche ni `research_context_builder.py` (B0A), ni B1A, migration, schéma SQL, UOW ou modèle de persistance. Il n’ajoute pas de base, framework ou endpoint de spawn.

Depuis une archive de `27db077daf79455c6e3ca668501fb6722638653f` avec le `.venv` du dépôt lié :

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_investigation_strategy_scenarios.py tests/test_investigation_experiments.py tests/test_investigation_experiment_api.py tests/test_operator_tool_invocation.py -q --tb=short` — **62 passed in 5.14s**.
- `.venv/bin/ruff check src/cti_app/api/investigation.py src/cti_app/application/investigation_experiments.py tests/test_investigation_experiment_api.py tests/test_investigation_experiments.py` — **All checks passed**.
- Vérification directe du projecteur sur des références SHA de forme valide et des métadonnées d’outil de forme valide — les valeurs choisies passent.

Les vérifications sont offline; aucun fournisseur, réseau, PostgreSQL ou Docker n’a été démarré. Le lifespan de production reste UNKNOWN. Coût : **UNKNOWN**.

## Verdict final après 27db077

**FAIL — A1-R3 conserve un constat confirmé HIGH : des références SHA de forme valide sans résolution canonique et les métadonnées d’outil reflètent du contenu choisi dans `experiment.list` et GET.** Le cas des longs `step_ids` non conformes est fermé. A1-R2 a un libellé correct dans les sorties backend, mais l’interface continue d’afficher le verdict sans ce libellé (**CONFIRMED · LOW**). Le lifespan reste UNKNOWN et documenté. Coût : **UNKNOWN**.

## Re-revue finale après 4a71f45

Cette re-revue examine `4a71f45c5a03761357f74ec363cb80b96cfb85b5`, enfant direct de `27db077daf79455c6e3ca668501fb6722638653f`. Le worktree de revue est resté sur `47a0ef9`; le code cible a été testé depuis une archive temporaire exacte de `4a71f45`.

### Résolution des références et contournements résiduels

**CONFIRMED · fermeture partielle de A1-R3, deux contournements restent ouverts.** Le projecteur constitue ses références UUID/SHA à partir des expériences du timeline et des observations/invocations canoniques de la même investigation ([`investigation_experiments.py`](../../backend/src/cti_app/application/investigation_experiments.py#L186)). Les références étrangères réellement fournies comme observations ou invocations sont filtrées par `investigation_id`. Les références SHA qui ne figurent dans aucune observation canonique deviennent `[redacted]`; `deadbeef` répété huit fois est testé dans `record`, `list` et GET. Les références tronquées par les limites des listes reçoivent un marqueur `[redacted]` ([même fichier](../../backend/src/cti_app/application/investigation_experiments.py#L503)). Les appels `record`, `experiment.list` et GET passent par le projecteur avec les observations/invocations chargées pour l’investigation ([`record`/`list`](../../backend/src/cti_app/application/investigation_experiments.py#L1067), [GET](../../backend/src/cti_app/api/investigation.py#L1091)).

**CONFIRMED · HIGH — un UUID canonique mais non lié peut sortir en clair.** Le projecteur place tous les UUID d’expérience, d’observation, de blob d’observation et d’invocation dans un seul ensemble, sans conserver leur type ni le lien avec l’expérience projetée ([`investigation_experiments.py`](../../backend/src/cti_app/application/investigation_experiments.py#L198)). `step_ids` accepte ensuite tout UUID présent dans cet ensemble ([même fichier](../../backend/src/cti_app/application/investigation_experiments.py#L439)). Vérification directe sur `4a71f45` : un UUID d’observation canonique de la même investigation, absent de `observation_refs`, est restitué sans masquage dans `step_ids`. Le test demandé pour un UUID valide d’invocation absent du dépôt n’est pas présent; les tests vérifient plutôt une liste d’invocations vide et un identifiant hostile de forme invalide.

**CONFIRMED · HIGH — le SHA d’une observation d’un autre type peut être présenté comme entrée.** `_safe_experiment_input_references()` cherche une observation uniquement par SHA, puis accepte `sample_sha256` ou `capture_sha256` sans valider `kind`, `object_type` ni leur correspondance avec le type déclaré ([`investigation_experiments.py`](../../backend/src/cti_app/application/investigation_experiments.py#L584)). Vérification directe : le SHA canonique d’une observation `vt_file_report` ressort avec `input_type: capture_sha256`, `input_id` et `sha256` en clair. Le type d’objet n’est pas comparé à la déclaration d’entrée.

L’API filtre bien une observation appartenant à une autre investigation, mais le test existant ne vérifie que `strategy.claims.status == UNKNOWN`, pas que la référence sérialisée `observation_refs` soit `[redacted]` ([`test_investigation_experiment_api.py`](../../backend/tests/test_investigation_experiment_api.py#L704)). Je n’ai pas trouvé de fuite de `tool_name` provenant d’une autre investigation : les invocations étrangères sont exclues de la table de correspondance. Les références d’invocation partielles sont résolues individuellement et les entrées hors limite sont masquées.

### Métadonnées d’outil et libellé

**CONFIRMED.** `tool_name` et `tool_schema_version` ne sortent que si une référence pointe vers une invocation canonique de la même investigation, à la même version d’invocation, avec le même nom et le même schéma ([`investigation_experiments.py`](../../backend/src/cti_app/application/investigation_experiments.py#L241)). Sans invocation, `secret.payload`, `SensitiveData-1` et `sensitive.payload.v1` sont absents des réponses `record`, `list` et GET; les champs sont masqués. `tool_version` reste toujours `[redacted]`, car `ToolInvocation` ne porte pas de version d’outil canonique ([même fichier](../../backend/src/cti_app/application/investigation_experiments.py#L467)).

Le libellé « assertion, non conclusion » est généré dans la projection backend ([`investigation_experiments.py`](../../backend/src/cti_app/application/investigation_experiments.py#L578)), déclaré dans le type frontend ([`investigationExperiments.ts`](../../frontend/src/api/investigationExperiments.ts#L60)), rendu par le composant ([`InvestigationLabViews.tsx`](../../frontend/src/features/investigation/InvestigationLabViews.tsx#L449)) et recherché par son test ([`InvestigationLabViews.test.tsx`](../../frontend/src/features/investigation/InvestigationLabViews.test.tsx#L519)). Le défaut d’affichage frontend du rapport précédent est corrigé.

### Contrats conservés et limites

**CONFIRMED dans le delta inspecté.** Sans stratégie vérifiée, les claims restent `inconclusive`. `tool_arguments` demeure `{}` et `redact_operational_secrets()` reste appliqué ([`investigation_experiments.py`](../../backend/src/cti_app/application/investigation_experiments.py#L427)). La validation `source_ref`/snapshot et les raisons opérateur antérieurement vérifiées ne sont pas modifiées. Le GET reste en lecture seule; l’écriture exige toujours l’enveloppe G2 courante ([même fichier](../../backend/src/cti_app/application/investigation_experiments.py#L934)). Le delta ne touche ni B0A, ni B1A, ni migration. Le lifespan PostgreSQL de production reste **UNKNOWN**, comme documenté dans le rapport précédent.

Depuis une archive de `4a71f45c5a03761357f74ec363cb80b96cfb85b5`, avec le `.venv` existant lié :

- La commande pytest demandée — **63 passed in 5.31s**.
- `.venv/bin/ruff check .` — **All checks passed**; `.venv/bin/ruff format --check .` — **829 files already formatted**.
- `pnpm test --run src/features/investigation/InvestigationLabViews.test.tsx` — **UNKNOWN** : Vite n’a pas démarré, écriture temporaire refusée (`EROFS`) sous le lien `node_modules/.vite-temp`.
- `pnpm typecheck` — **UNKNOWN** : TypeScript n’a pas pu écrire ses fichiers `.tsbuildinfo` sous le lien `node_modules/.tmp` (`EROFS`).
- Vérification directe du projecteur : l’UUID canonique mais non lié et le SHA canonique présenté sous un autre type ressortent en clair.

Les tests backend et checks Ruff sont offline; aucun fournisseur, réseau, PostgreSQL ou Docker n’a été démarré. Les checks frontend n’ont pas abouti et ne sont pas comptés comme passés. Coût : **UNKNOWN**.

## Verdict final après 4a71f45

**FAIL — deux contournements confirmés restent ouverts : un UUID canonique de même investigation mais non lié à l’expérience peut être réfléchi, et un SHA d’observation est accepté sous un type d’entrée incompatible.** Les métadonnées d’outil sont liées aux invocations canoniques, le libellé assertion/non-conclusion est affiché frontend, et les protections `tool_arguments`, secrets, source/snapshot, G2 et lecture seule sont conservées. Les checks frontend et le lifespan réel restent UNKNOWN. Coût : **UNKNOWN**.

## Re-revue après 1f0e813

Cette re-revue cible `1f0e81344e73e4d3db128bf8a7dfd74b11740849`, delta `4a71f45c5a03761357f74ec363cb80b96cfb85b5..1f0e81344e73e4d3db128bf8a7dfd74b11740849`.

### Résolution des références

**CONFIRMED · R5 fermé pour les références non liées.** Le projecteur construit les ensembles autorisés par expérience depuis ses seuls `observation_refs` et `tool_invocation_refs`, puis résout les observations par ID et SHA exacts, et les invocations par ID et version exacte, dans les objets rattachés à la même investigation ([`investigation_experiments.py:185-256`](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L185)). `step_ids` n’accepte que les invocations liées; les `input_refs` et `next_pivots` n’acceptent que les SHA des observations liées ([même fichier:454-474](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L454)). `record`, `experiment.list` et GET utilisent le projecteur ([même fichier:1111-1116,1155-1182](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L1111), [`investigation.py:1091-1099`](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/api/investigation.py#L1091)).

Les UUID d’invocation canoniques de la même investigation mais absents des refs de l’expérience, et les UUID d’invocation absents du dépôt, ressortent `[redacted]`. Les références liées et résolues restent visibles. Un UUID d’observation dans `step_ids` est masqué. Les SHA d’observations non liées, y compris `vt_file_report`, sont masquées dans `input_refs`, `next_pivots` et les refs; une observation liée avec SHA exact reste visible.

**CONFIRMED · MEDIUM — type d’observation non vérifié pour les entrées et pivots liés.** `_safe_experiment_input_references()` vérifie que `input_type` vaut `sample_sha256` ou `capture_sha256`, puis cherche le SHA dans les observations liées; il ne compare pas `kind`/`object_type` au type déclaré ([`investigation_experiments.py:608-655`](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L608)). `next_pivots` accepte également le SHA de toute observation liée ([même fichier:457-467](../../../feat-mission01-a1-strategy-projection/backend/src/cti_app/application/investigation_experiments.py#L457)). Une vérification directe via GET avec un `vt_file_report` lié a renvoyé son SHA sous `capture_sha256` et dans `next_pivots`, tandis que son ID dans `step_ids` était bien masqué. La SHA était aussi publiée comme observation liée; le défaut restant est une attribution de type/provenance erronée, pas une divulgation d’une observation non liée. Le test ajouté ne couvre que le cas `vt_file_report` non lié ([`test_investigation_experiment_api.py:761-846`](../../../feat-mission01-a1-strategy-projection/backend/tests/test_investigation_experiment_api.py#L761)).

Pas de contournement trouvé par casse ou référence imbriquée : les formes UUID/SHA sont minuscules et canoniques, et la projection reconstruit les champs fixes des refs d’entrée. Les SHA d’invocation ne sont pas utilisés pour autoriser les `input_refs`/pivots. Le contrôle de version d’invocation est exact; aucun test dédié ne vérifie une version erronée.

### Couverture et protections conservées

Le test GET couvre les cas lié, canonique non lié et absent pour `step_ids`/invocations, ainsi que les observations liées et non liées. Les tests `record` et `list` vérifient le masquage des invocations non liées/absentes et gardent visibles les observations liées; ils ne vérifient pas directement une invocation liée visible dans ces deux sorties. `tool_arguments` reste vide; secrets et texte libre restent masqués; les claims non prouvés restent `inconclusive`. Les protections source/snapshot, G2 et GET en lecture seule ne sont pas modifiées par ce delta. Aucun fichier B0A/B1A ni migration n’est touché. Le lifespan de production reste **UNKNOWN**.

Depuis une archive exacte du commit cible, avec le `.venv` relié conformément à la consigne :

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_investigation_strategy_scenarios.py tests/test_investigation_experiments.py tests/test_investigation_experiment_api.py tests/test_operator_tool_invocation.py -q --tb=short` — **64 passed in 5.25s**.
- `.venv/bin/ruff check .` — **All checks passed**.
- Vérification directe GET du cas `vt_file_report` lié — UUID d’observation dans `step_ids` masqué; SHA visible avec le mauvais `input_type` et dans `next_pivots`.

Les checks sont offline; aucun fournisseur, PostgreSQL, Docker ou transport live n’a été démarré. Coût : **UNKNOWN**.

## Verdict après 1f0e813

**PASS_WITH_CHANGES — les fuites R5 de références non liées sont fermées.** Reste un défaut **MEDIUM** de cohérence de type/provenance pour une observation liée utilisée sous `sample_sha256`/`capture_sha256` ou comme pivot. Corriger/valider la correspondance du type d’observation avant de qualifier la projection entièrement sûre. Le lifespan de production reste **UNKNOWN**. Coût : **UNKNOWN**.

## Résiduel A1-RES — réparation et revue

**Réparation :** `fbe5d36904ee14d1c5467f475f9178565522d097` (branche
`feat/mission01-a1-residual`, base `a1d263a0`), 3 fichiers.
Le projecteur n’expose une entrée que si son SHA correspond à une
observation liée et unique dont `object_type` correspond à
`sample_sha256`/`capture_sha256`; les rapports, types incompatibles et
correspondances ambiguës sont masqués; `next_pivots` est restreint aux
observations sample/capture; la version d’invocation exacte reste visible.
Tests: sample/capture liés visibles, `vt_file_report` lié incompatible
masqué, SHA dupliqué/non lié masqué, invocation exacte/erronée/absente.

**Revue indépendante :** agent `fe7f765f-1d6d-4e66-a24c-b5770e44b332`,
Luna xhigh, oracle **57 passed**. Verdict **PASS_WITH_CHANGES**.

### MEDIUM résiduel — `kind` non recoupé

`investigation_experiments.py:607` déduit le type d’`object_type` seul.
Une observation `vt_file_report` avec `object_type="sample"` serait classée
`sample_sha256` et son SHA pourrait ressortir dans `input_refs`/
`next_pivots`. Le domaine n’impose pas la correspondance
`kind`↔`object_type` (`domain/investigation.py:528`) et l’enregistreur les
accepte séparément (`evidence_recorder.py:112`). Le producteur VT fixe
`object_type="file"` (`investigation_vt_tools.py:714`), donc le cas n’est
pas démontré pour ce chemin; une observation incohérente/historique reste
possible. Correction A1-RES-R1 : recouper `kind` et `object_type`, masquer
les combinaisons incohérentes.

Lifespan de production et PostgreSQL : **UNKNOWN**. Coût : **UNKNOWN**.

## Revue finale A1-RES-R1 — PASS

**Objet :** `749a3f2fcbfbfc627831f1e0ff37afcb730b29e9` (base `a1d263a0`,
deltas `fbe5d36` + `bd35278` + `749a3f2`).
**Revue :** agent `cdb8f69e-be9c-4cce-b9e9-03d09989c391`, Luna xhigh.
**Oracle rejoué :** 79 passed ; 3 tests PostgreSQL collectés, exécution
**UNKNOWN** (`TEST_POSTGRES_ADMIN_DSN` absent).

Aucun constat BLOCKER/HIGH/MEDIUM/LOW. Le mapping strict des quatre couples
producteurs est partagé par `input_refs` et `next_pivots`; les SHA dupliqués
et couples inconnus sont masqués. Les tests de route couvrent les couples
positifs, `vt_file_report/sample`, `binary_analysis/network_capture`, l'alias
`other/capture`, les SHA dupliqués et les versions d'invocation. Les fixtures
corrigées vers `network_capture` ne relâchent pas leurs assertions. Claims/
matrice, redaction et route GET restent inchangés.

**Intégration :** branche `feat/mission01-a1-residual` fusionnée dans
`chore/mission01-integration` au commit `69d3b904`. Oracle combiné
A1+L2+B1B sur l'intégration : **194 passed**. Résiduel : PostgreSQL réel et
lifespan de production `UNKNOWN`.


