# Revue indépendante M01-A1

**Cible :** `cd344743e9ba12d01d509cc8b5f9e42a0f3cb239` (`feat/mission01-a1-strategy-projection`), base `058bb4af2278e3941956e523c5a1d560208de228`. Intégration examinée : `27e99e4769e1f12ca9b717342dbad0e0eefc6fcb`.

**Re-revue :** `da79486dd19cb4f01ecebfb01fa199a282f47472`.

**Verdict initial sur cd34474 : FAIL. Verdict final après da79486 : FAIL.** Le correctif ferme la divergence `claims`/`strategy` dans la route HTTP, mais les outils opérateur `experiment.record/list` exposent encore `REFUTED` depuis le verdict persisté seul. La nouvelle expurgation de la réponse HTTP filtre les textes et chaînes de paramètres testés; elle conserve cependant des valeurs scalaires arbitraires et des identifiants d’entrée sans validation canonique.

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
