# Mission 01 — revue adversariale B1A2

**TASK_ID :** M01-B1A2-REVIEW

**Base :** 02999f8ee50009bc9a264361fa08afbd5d05a3f3

**Commit examiné :** 3c2ff0e91248e7272e83670c5698a4a7ad796490

**Intégration indiquée :** 723b761cab8ce36580bcb9e8c14f16d08a151017
**Verdict initial (historique) : FAIL — supersédé par la re-revue finale ci-dessous.**

La borne choisit bien le minimum entre la réservation G2 active et le timeout
Research, puis transmet cette durée au transport. Elle ne constitue toutefois
pas une limite dure de l’exécution advisor : l’attente du pacer Bridge et le
préflight du gateway ne sont pas couverts par le même délai. L’usage mesuré
avant le pacer est ensuite plafonné, ce qui peut masquer un dépassement.

## Constats

### F1 — CONFIRMED / BLOCKER — le délai dur ne couvre pas l’attente avant transport

La fonction _advisor_execution_timeout vérifie une réservation encore
RESERVED, exige un tour Bridge, puis calcule
floor(min(reserved_seconds, role_timeout))
(backend/src/cti_app/application/investigation_advisor_tools.py:3921-3958).
Le résultat est calculé après admission et passé à
submit_bridge_consultation_once (:204-215, :649-663). Le gateway combine la
durée de requête et le timeout Research, puis applique asyncio.timeout autour
de adapter.invoke (backend/src/cti_app/application/model_gateway.py:1004-1022,
:1168-1189).

La borne relative commence trop tard pour couvrir l’exécution comptée par
l’advisor. submit_bridge_consultation_once attend
pacer.wait_for_next_submission() avant de prendre execution_started_at
(backend/src/cti_app/application/bridge_investigator.py:212-224). Le pacer
peut attendre sans deadline (:91-109) et conserve un délai Retry-After sans
plafond (:85-90, :249-257). Le préflight ModelGateway précède également le
bloc asyncio.timeout (model_gateway.py:1163-1178). Pourtant l’advisor démarre
son horloge avant le pacer (investigation_advisor_tools.py:649-662), puis
plafonne la consommation à execution_timeout_seconds (:755-770, :2815-2822).

Contre-exemple : avec une réserve de 60 s, un pacer bloqué 120 s, puis une
réponse en 59 s, le Bridge accepte la réponse comme succès, bien que l’advisor
ait attendu 179 s. La consommation persistée est artificiellement limitée à
60 s. Une réponse arrivée après la borne comptée peut donc être traitée comme
un succès. Le test de réponse tardive couvre 3 601 s de transport sans délai
préalable du pacer (backend/tests/test_b1_g2_bounded_timeout.py:87-122).

La tâche demande une borne dure tool_seconds sous capacité G2 et propagée au
transport (docs/mission01/TASKS.md:61); la gouvernance réserve cette dimension
avant l’appel (docs/investigation_governance.md:46-49). Il faut que le délai
couvre l’attente avant soumission et le transport, et que l’expiration avant
transport empêche l’envoi. Le plafonnement actuel ne prouve pas la borne.

### F2 — CONFIRMED / HIGH — une nouvelle clé peut contourner le blocage d’un appel ambigu

Un rejeu avec la même clé reste attaché à l’invocation existante. Le chemin
Bridge remet un tour NEEDS_REVIEW sans le resoumettre
(backend/src/cti_app/application/bridge_investigator.py:475-513). Le contrôle
des nouvelles clés compare cependant les arguments canoniques bruts
(backend/src/cti_app/application/operator_tool_invocation.py:646-654),
tandis qu’advisor.consult normalise question avec strip() et assimile son
absence à la chaîne vide
(backend/src/cti_app/application/investigation_advisor_tools.py:180-184).
Une nouvelle clé accompagnée d’une variante d’espacement peut donc éviter le
contrôle de l’invocation ambiguë et provoquer un second transport. Cette porte
est déjà relevée dans docs/mission01/B1A1_REVIEW.md:22 et la réparation
sémantique reste RUNNING dans docs/mission01/TASKS.md:60. B1A2 ne la ferme
pas. Le transport unique dans un appel B1A2 ne prouve pas l’absence de second
appel avant réconciliation avec une nouvelle clé.

### F3 — PLAUSIBLE / MEDIUM — le seuil de 60 s peut refuser un appel légitime

MIN_ADVISOR_EXECUTION_SECONDS vaut 60
(backend/src/cti_app/application/investigation_advisor_tools.py:87-88), et
une réservation ou un timeout effectif inférieur est refusé (:3943-3957).
Le test couvre 59 s de capacité restante face à la réservation par défaut de
3 600 s et vérifie un refus avant transport
(backend/tests/test_b1_g2_bounded_timeout.py:202-256); il n’isole pas une
réservation active de moins de 60 s. Aucune justification de cette durée
minimale n’apparaît dans la tâche B1A2 (docs/mission01/TASKS.md:61) ni dans
la gouvernance G2. Une réservation
approuvée de 30 s et un appel pouvant finir en 10 s seront refusés. Ce seuil
sur-refuse; il ne sous-réserve pas les appels acceptés.

### F4 — CONFIRMED / LOW — le test combiné ne compte pas les transports

Le test ajouté pour le rate-limit ambigu appelle _submission_result
directement et vérifie NEEDS_REVIEW, l’absence de blocs parsés et un tour
consommé (backend/tests/test_investigation_advisor_tools.py:4067-4118). Il
n’exécute pas de transport et ne vérifie donc pas son nombre d’entrées. Le test
paramétré séparé vérifie un seul appel pour un http_429
(:4011-4064), sans combiner ce cas avec un résultat ambigu. Le chemin de code
ambigu lui-même n’effectue pas de retry automatique; le test combiné ne le
démontre pas au niveau transport.

## Vérifications comportementales

- **Borne et propagation :** les réservations 3 600/timeout Research 7 200,
  réserve 9 000/timeout 7 200 et rôle sous réserve sont couverts
  (test_b1_g2_bounded_timeout.py:87-199). Le timeout transmis est le minimum
  attendu. Le cas sous le minimum est refusé avant transport. La couverture
  n’inclut pas le délai pacer dans la deadline.
- **Consommation et résultat tardif :** le test à 3 601 s vérifie
  NEEDS_REVIEW, aucune sortie parsée, un transport et une consommation
  committée sous 3 600 s (test_b1_g2_bounded_timeout.py:87-122). Cela passe
  sans délai préalable du pacer; le plafonnement ne valide pas le temps total
  du contre-exemple F1.
- **Classification :** NEEDS_REVIEW et les états de soumission ambiguë sont
  évalués avant http_429, puis le rate-limit avant le cas générique
  (investigation_advisor_tools.py:2831-2895). Un état ambigu consomme un tour
  (:3853-3866) et l’exécution ne transmet au parseur que le texte d’un statut
  SUCCEEDED (:669-673). Le résultat rate-limit porte retry=false. PARTIAL est
  conservé pour les résultats normalisés partiels et les lots éditoriaux
  (:1185-1225, :2981-2982).
- **G2/TLP/provider et périmètre :** le correctif
  02999f8..3c2ff0e ne touche que investigation_advisor_tools.py et son test.
  Il ne modifie pas les contrôles G2/TLP/provider, n’ajoute pas d’extension
  automatique ni de migration, et n’introduit aucune abstraction de
  classification. Les changements du commit initial B1A2 dans model_gateway.py
  et bridge_investigator.py portent la propagation du timeout. La revue ne
  relève pas de duplication de logique. Ce correctif de 24 insertions et 26
  suppressions reste ciblé.
- **Complexité — REJECTED / LOW :** le diff R1 ne crée pas de nouvelle
  abstraction ni de second classificateur. Il réordonne les branches existantes
  pour donner priorité à l’ambiguïté et retire l’exemption erronée des tours
  rate-limit ambigus.
- **Estimation historique : DEFER.** Le code indique explicitement que
  l’estimation depuis ModelRun.duration_ms est différée
  (investigation_advisor_tools.py:3925-3929). Aucun modèle de durée
  historique n’est employé comme borne.

## Tests et contrôles exécutés

Archive exacte de 3c2ff0e sous /tmp/m01-b1a2-review-3c2ff0e, avec les liens
.venv et .autowork-runs demandés. Aucun fournisseur ou service live appelé.

    PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
      tests/test_b1_g2_bounded_timeout.py \
      tests/test_b1_admission_durability.py \
      tests/test_advisor_completeness.py \
      tests/test_bridge_investigator.py \
      tests/test_investigation_advisor_tools.py -q --tb=short
    133 passed in 2.93s

Ruff ciblé sur les neuf fichiers de l’implémentation et des tests B1A2 :

    .venv/bin/ruff check \
      src/cti_app/application/bridge_investigator.py \
      src/cti_app/application/investigation_advisor_tools.py \
      src/cti_app/application/model_conversations.py \
      src/cti_app/application/model_gateway.py \
      tests/test_b1_g2_bounded_timeout.py \
      tests/test_bridge_investigator.py \
      tests/test_investigation_advisor_tools.py \
      tests/test_model_adapters.py \
      tests/test_model_gateway.py
    All checks passed!

git diff --check 02999f8..3c2ff0e passe. Le commit 3c2ff0e est ancêtre de
723b761. Aucun test PostgreSQL ni runtime/provider n’a été exécuté : ces
comportements restent **UNKNOWN**. Coût : **UNKNOWN**.

## Conclusion

Le minimum entre réservation et timeout de rôle est correctement calculé et
transmis à l’appel, mais F1 laisse dépasser la borne totale comptée par
tool_seconds; F2 laisse subsister un second appel avec une nouvelle clé et
des arguments équivalents après normalisation. Le seuil fixe de 60 s manque
de justification et le test combiné de rate-limit ne compte pas le transport.
Le changement de classification est correct, mais le gate B1A2 reste **FAIL**
jusqu’à fermeture de F1.

## Re-revue finale après 01346e2/5484375

**TASK_ID :** M01-B1A2-REVIEW-R2

**Commits vérifiés :** `01346e28e001c170b919c970a90f3a1c21c291d8` (intégré par
`91db0f3d90b0f0186bfef441e412532ae128c3e4`) et
`54843750029a04b063b5a2e3f93c7feeff3960c5` (intégré par
`591eeca061607649f84ac641e3b04a67ac62d1b1`). Les deux intégrations sont dans
`3e15193a9d5dafdadf41a472699768cede164d35`, qui ajoute ensuite trois tests
B1A1-R2 de normalisation sémantique. `git diff --check` de la base de revue à
cette intégration passe.

**Verdict final : PASS_WITH_CHANGES**

### Résultat

- Sur le chemin de production `advisor.consult`, l’horloge monotone démarre
  avant la préparation, puis la même deadline absolue est transmise au helper
  Bridge (`investigation_advisor_tools.py:217-240,688-700`). Le pacer compare
  son attente prévue au temps restant et refuse avant transport si elle
  atteindrait la deadline (`bridge_investigator.py:91-116,235-255`). Après le
  pacing, le helper calcule le timeout transport restant, le passe au transport
  et l’encadre aussi par `asyncio.timeout`; une réponse retournée à la deadline
  ou après devient `NEEDS_REVIEW`, sans texte réussi à parser
  (`bridge_investigator.py:256-305`). Le gateway borne également l’appel
  d’adaptateur par le plus petit des timeouts de requête et de rôle.
- Les retours ambigus passent avant la classification `http_429`; seul le 429
  confirmé devient `advisor_rate_limited`. Un résultat ambigu consomme un tour
  réservé et n’est pas retenté avec un second transport. Les tests couvrent
  aussi le succès avant borne, la réponse à 3601 s pour un budget de 3600 s,
  l’unique appel transport et la consommation plafonnée à la réserve.
- Le plancher est calculé depuis pacing, jitter, connect, capabilities,
  archive et marge (`investigation_advisor_tools.py:4013-4045`). Les refus
  d’admission exigent une réservation G2 active avec au moins un tour et assez
  de `tool_seconds`; l’exécution est plafonnée par le minimum entre réserve et
  timeout Research (`:4048-4088`). L’estimateur historique de coût basé sur
  `ModelRun.duration_ms` reste explicitement **DEFER**. Aucun changement de
  migration ou contournement G2 n’a été trouvé dans ces commits.
- Le test
  `test_remaining_capacity_below_minimum_is_refused_before_reservation_or_transport`
  vérifie désormais `FrameworkPreSubmissionProof.BUDGET_REJECTED`, conserve la
  dépense existante, et vérifie l’absence de nouvelle réservation et d’appel
  transport (ainsi que le compteur Bridge). Il n’emploie plus le champ libre
  `pre_submission_reason`; l’assertion de refus avant réservation/transport
  n’est pas affaiblie. Les cas de capacité insuffisante ne remplacent toutefois
  pas les tests séparés du calcul du plancher.

### Changements encore recommandés

- Le calcul du plancher (`:4017-4045`) additionne le pacing maximal, connect et
  capabilities, mais utilise `max(archive, 35 s)` pour archive et marge. La
  justification de la constante de 35 s et le fait que ces deux phases soient
  alternatives plutôt que séquentielles ne sont pas établis dans les contrats
  consultés. De plus, après préparation et pacing, le helper compare le temps
  restant au plancher complet, qui inclut déjà pacing et marge
  (`bridge_investigator.py:259-265`). Cela peut refuser conservativement un
  transport encore possible; aucun dépassement n’en résulte. Clarifier les
  phases et tester un cas de pacing consommé près du plancher.
- Le chemin distinct `BridgeInvestigator.turn` conserve un pacer sans deadline
  puis un appel transport sans timeout (`bridge_investigator.py:1082,1125` dans
  l’intégration vérifiée). L’unique appel de production à
  `submit_bridge_consultation_once` est `advisor.consult` et lui passe la
  deadline; ce chemin distinct n’est donc pas un contournement de cet appel
  B1A2. La borne ne doit pas être présentée comme couvrant tous les tours
  Bridge de l’investigation.
- La valeur `submission_state="external_state_unknown"` est encore affectée
  comme chaîne dans le résultat ambigu (`bridge_investigator.py:303`), alors
  que le domaine expose `ModelSubmissionState.EXTERNAL_STATE_UNKNOWN` et que
  `backend/AGENTS.md` demande des StrEnums pour les états de contrôle. C’est une
  incohérence de typage de faible gravité, sans échec observé dans la suite.

### Vérifications exécutées

Les archives exactes des commits d’intégration ont été testées sous
`/tmp/m01-b1a2-review-591eeca-r2` et
`/tmp/m01-b1a2-review-3e15193-r2`, avec les liens `.venv` et `.autowork-runs`
indiqués dans la mission. Sur `591eeca`, les neuf fichiers demandés donnent
**217 passed**; sur l’intégration finale `3e15193`, la même suite donne
**220 passed** (les trois tests supplémentaires sont ceux de normalisation
B1A1-R2). Ruff sur tout le backend passe aux deux révisions (`All checks
passed!`). Aucun fournisseur ou service live n’a été appelé.

**UNKNOWN :** tests PostgreSQL et runtime/provider non exécutés; aucun résultat
de transport live n’est inféré de ces tests hors ligne. **Coût : UNKNOWN.**
