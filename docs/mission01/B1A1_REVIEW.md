# Mission 01 B1A1 — revue adversariale indépendante

- **TASK_ID courant :** M01-B1A1-REVIEW-R3 (les sections R1/R2 ci-dessous sont l’historique)
- **Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`
- **Commit initial :** `3a745de788f2af286274d024a404e6ae7f6728fd` (`fix(investigation): harden admission durability`)
- **Réparation R2 réexaminée :** `d1580a93c8c9a15ba1584b2974c74dd16adb1de0` (`fix(investigation): attest pre-submission failures`), delta `3a745de..d1580a9`
- **Cible R3 :** `2f88a42352575fb486a84b581644706e6be52576`, intégrée par `2cafb379372b603d9d873a1070f1470364aec7e0`
- **Intégration initiale :** `fe68c85`

**Périmètre :** relecture adversariale du delta et des sources réelles, suites offline demandées, Ruff ciblé. Aucune migration, aucun accès live ou fournisseur.

## Verdict final R2

**FAIL.** La preuve forgeable de R1 est fermée pour les nouvelles exécutions et l’extension G2 traverse désormais le service audité. Toutefois, la garde contre une nouvelle clé reste contournable par des paramètres différents à l’octet près mais normalisés pareillement par les adaptateurs (`vt.file_report.file_hash` en casse différente, `advisor.consult.figure_plan.finding_refs` en UUID majuscules/minuscules) et par certains défauts de `review_text`. C’est un constat **HIGH** sur une porte requise. Un refus pré-soumission émis par l’adaptateur advisor reste aussi bloqué en `NEEDS_REVIEW`, faute d’attestation framework.

L’état `BLOCKED` de B1A1 est correctement borné à l’atomicité révocation/dispatch : le contrôle G2 réduit bien les effets visibles avant le dispatch, mais il ne rend pas atomique la fenêtre jusqu’au transport. Il ne masque toutefois pas le contournement sémantique relevé ci-dessus. Le décalage 3 600/7 200 s reste séparément assigné à B1A2; sa valeur runtime exacte est **UNKNOWN**.

Les constats et tests R1 sont conservés ci-dessous comme historique; les constats R2 et le verdict courant figurent dans « Re-revue après d1580a9 ».

## Verdict initial R1 — 3a745de (historique)

**FAIL.** Le coût et la clé de réservation sont désormais transmis de façon cohérente, et le crash après admission reste conservateur. Deux portes de l’objectif ne sont toutefois pas fermées : une nouvelle clé peut contourner la comparaison en modifiant des arguments que l’adaptateur normalise, et un adaptateur peut encore faire accepter une fausse preuve de pré-soumission. La frontière révocation/dispatch est, elle, correctement déclarée `BLOCKED`.

Le statut `PARTIAL / BLOCKED` de B1A1 est donc correctement borné pour l’atomicité révocation/dispatch; il ne doit pas être interprété comme la fermeture de toutes les portes. La mention « portes fermées » de la ligne B1A1 doit être corrigée après traitement des deux constats ci-dessus. Le risque de réservation 3 600 s face au délai Research de 7 200 s reste distinctement attribué à B1A2.

## Constats R1 historiques

| Sujet | Statut | Sévérité | Preuve et conclusion |
|---|---|---:|---|
| Coût et clé entre les deux réservations | **CONFIRMED** | LOW | `InvestigationService.execute_tool_call` conserve le coût journalisé dans `invocation.reserved_budget` (`backend/src/cti_app/application/investigation_service.py:1033-1044`), le persiste avec l’invocation `RUNNING` (`:1103-1147`), puis transmet ce même coût et `operation_key` au dispatcher (`:1253-1261`). Le dispatcher réserve sous cette clé avec `reservation_amounts` (`backend/src/cti_app/application/investigation_tools.py:747-752`); `InvestigationBudget.reserve` refuse explicitement des montants divergents pour une clé existante (`backend/src/cti_app/domain/investigation.py:268-285`). Le test change le coût courant du registre de 30 à 99 et vérifie que le dispatcher reçoit toujours 30 sous la même clé (`backend/tests/test_b1_admission_durability.py:129-180`). Une incohérence déjà présente entre la réservation persistée et l’invocation ferait toujours lever `ValueError`, mais aucune recalculation différente n’existe dans le chemin vérifié. |
| Crash après persistance, avant dispatch | **CONFIRMED** | LOW | Le journal `RUNNING` et la réservation sont commités avant le dispatch (`investigation_service.py:1133-1147`). Au rejeu, `RUNNING` devient `NEEDS_REVIEW` et retourne sans appeler le dispatcher (`:1072-1091`); la réservation reste `RESERVED`, sans consommation. Le test vérifie cet état et zéro appel adaptateur (`backend/tests/test_b1_admission_durability.py:294-339`). Ce n’est pas une libération silencieuse : l’invocation est explicitement en revue et la réservation reste conservée. Elle peut immobiliser du budget jusqu’à réconciliation humaine, qui permet release ou commit (`investigation_service.py:1891-1935`). |
| Nouvelle clé avec contenu sémantique équivalent | **CONFIRMED** | HIGH | La garde compare `canonical_json_bytes(arguments)` aux paramètres enregistrés (`backend/src/cti_app/application/operator_tool_invocation.py:646-654`); ces paramètres sont les arguments bruts, malgré leur nom `normalized_parameters` (`investigation_service.py:1024-1031`). Or `advisor.consult` retire les espaces périphériques de `question` (`backend/src/cti_app/application/investigation_advisor_tools.py:173-176`) et traite `question` absent ou vide comme la même tâche (`:2435-2437`); le champ est optionnel (`:2882`, `:2927-2929`). Une première requête avec `question="plan pivot"` et une seconde avec `question=" plan pivot "` (ou vide absent/explicite) ont donc des octets distincts mais atteignent le même contenu normalisé et peuvent repasser sous une nouvelle clé. L’ordre des clés JSON est neutralisé par la sérialisation canonique; l’alias de schéma legacy `"1"` est canonisé pour les outils connus (`operator_tool_invocation.py:461-474`). Les démarrages concurrents à même version sont aussi arbitrés par le CAS de l’état (`:835-840`), mais aucun test concurrent n’a été ajouté. |
| Preuve `pre_submission` forgeable | **CONFIRMED** | BLOCKER | Le dispatcher retire à la sortie adaptateur seulement `pre_submission_reason=budget_exceeded/policy_denied` (`backend/src/cti_app/application/investigation_tools.py:780-788`). En revanche, `_confirmed_external_non_execution` accepte les codes `envelope_revoked_before_dispatch`, `envelope_admission_conflict`, etc. avec `failure_stage=pre_submission` et `confirmed_non_execution=true` (`backend/src/cti_app/application/investigation_service.py:3354-3368`); ces champs viennent du `ToolResult` de l’adaptateur (`investigation_tools.py:764-765`). Cette branche libère la réservation (`investigation_service.py:2821-2826`) et peut rendre l’invocation terminale, permettant une nouvelle clé. Le même risque est visible avec `advisor_input_invalid` et `failure_stage=pre_transport_validation` (`investigation_service.py:3345-3349`): le nouveau test de retry fournit ces valeurs depuis un faux `vt.file_report` adapter puis autorise le nouvel appel (`backend/tests/test_b1_admission_durability.py:389-433`). Pour `advisor.consult`, la garde supplémentaire accepte un résumé `advisor_status=invalid_input`, sans ID Bridge et avec étape prétransport, sans vérifier que l’observation appartient à cette invocation (`backend/src/cti_app/application/operator_tool_invocation.py:476-503`). `CanonicalEvidenceSummaryPort` vérifie que le blob lu correspond au SHA de l’observation, puis projette les champs du payload (`backend/src/cti_app/application/research_context_builder.py:286-339`); cela garantit l’intégrité des octets enregistrés, pas la vérité de « aucune soumission ». La preuve brute d’une réponse Bridge malformée n’a pas été établie; un `ToolResult` d’adaptateur suffit déjà à contourner la garantie. Le test de preuve forgée ne couvre que `budget_exceeded`, que le dispatcher retire, via `vt.file_report` (`backend/tests/test_b1_admission_durability.py:437-482`). |
| CAS et révocation G2 | **CONFIRMED** | BLOCKER | Le CAS de réservation obsolète ne remplace pas une révocation : le test révoque avant la sauvegarde, puis vérifie les droits révoqués, aucune réservation active et aucun appel adaptateur (`backend/tests/test_b1_admission_durability.py:552-589`). Le callback recharge l’enveloppe, vérifie réduction/révocation et réservation juste avant l’exécution de l’adaptateur (`backend/src/cti_app/application/investigation_service.py:1181-1251`); le test couvre aussi une révocation visible à ce contrôle (`backend/tests/test_b1_admission_durability.py:592-640`). La garantie n’est pas atomique : après le callback, `advisor.consult` prépare preuves et prompt puis appelle le transport sans callback `before_submission` (`backend/src/cti_app/application/investigation_advisor_tools.py:218-293`, `:569-578`). Une révocation dans cette fenêtre reste possible. C’est bien le blocage déclaré par `docs/mission01/TASKS.md:54`, pas un risque fermé par les tests actuels. |
| Refus 266 s / 3 600 s | **CONFIRMED** | LOW | Le test vérifie 266 s restants, `consumed_budget={}`, état `NEEDS_REVIEW`, réservation de cette opération `RELEASED`, réserve antérieure de 1 590 s toujours active et zéro appel adaptateur (`backend/tests/test_b1_admission_durability.py:234-268`). Le compteur `bridge_transport` n’est pas relié à l’adaptateur `vt.file_report` de cette fixture; la preuve décisive d’absence d’effet est `tool.calls == []`, le transport étant inatteignable après le refus dispatcher. |
| Extension de budget après refus | **UNKNOWN** | MEDIUM | Le test parle d’extension approuvée, mais construit directement un nouvel `InvestigationBudget` et remplace l’enveloppe par CAS (`backend/tests/test_b1_admission_durability.py:270-276`). Il ne traverse pas `InvestigationEnvelopeService.extend`, sa décision G2 auditée, ni ses contrôles d’acteur/corrélation. Il démontre qu’un retry fonctionne après une extension de fixture; il ne valide pas le workflow d’approbation G2.
| Réservation 3 600 s face au timeout Research 7 200 s | **CONFIRMED** | HIGH | Le décalage des valeurs par défaut en code reste ouvert : le coût advisor est 3 600 s et le rôle Research peut attendre 7 200 s (`docs/mission01/B1_ADMISSION_REVIEW.md:17`). Ce commit ne modifie ni la limite de réservation advisor ni le timeout du rôle Research; B1A2 porte explicitement cette correction (`docs/mission01/TASKS.md:56`). La valeur runtime historique exacte reste **UNKNOWN**; les 121 tests rejoués ne prouvent pas que le timeout effectif soit borné par la réserve.
| Régressions et périmètre | **CONFIRMED** | LOW | Les quatre fichiers du commit sont les trois modules d’application indiqués et `backend/tests/test_b1_admission_durability.py`; aucun fichier de migration ou B0A n’est touché. Les contrôles provider/TLP/G2 existants restent avant l’adaptateur; aucune suppression ou réduction de ces contrôles n’apparaît dans le diff. Les tests ciblés des suites `investigation_service`, `investigation_tools_contract` et `operator_tool_invocation` passent avec la nouvelle suite. Cela ne neutralise pas le constat de preuve falsifiable ci-dessus.
| Complexité | **PLAUSIBLE** | LOW | Les 939 ajouts incluent 640 lignes de tests. La logique de recherche de résumé d’observation est répétée dans `_is_proven_pre_submit_advisor_failure` et `_is_proven_pre_submission_failure` (`operator_tool_invocation.py:483-503`, `:523-546`), avec des contrôles de provenance différents; un helper commun imposant le lien invocation/opération éviterait duplication et dérive. Le callback générique `pre_dispatch_check` (`investigation_tools.py:416-418`, `:628-630`) est une extension de protocole assez large pour un seul contrôle, mais ne constitue pas à lui seul un défaut bloquant. |

## Axes indépendants R1 historiques

### Standards

- **Violation documentée — MEDIUM.** `backend/AGENTS.md:14-15` exige les états de contrôle typés en `StrEnum` du domaine et interdit de les encoder en chaînes libres ou clés de métadonnées. Le nouveau chemin de retry/release branche sur les champs libres `pre_submission_reason`, `failure_stage` et `confirmed_non_execution` (`investigation_service.py:3354-3368`, `operator_tool_invocation.py:476-503`). Une différence de chaîne change la transition d’état et le droit de retry. Ruff ne détecte pas cette violation sémantique.
- **Heuristique — LOW, duplication possible.** Les deux méthodes de preuve répètent la boucle de chargement observation → résumé (`operator_tool_invocation.py:483-493`, `:523-535`); le hunk de `:483` ne vérifie pas `invocation_ref` ni `object_id`, contrairement à `:523-532`.
- Aucun autre écart documenté ou smell bloquant n’a été établi. Ruff ciblé passe.

### Spécification

- **HIGH — partiel :** la correction de clé/montants est conforme; toutefois la garde « même contenu avant réconciliation » demandée par `docs/mission01/B1_ADMISSION_REVIEW.md:43-45` et `docs/mission01/TASKS.md:54` ne compare pas les paramètres après normalisation effective. Les variantes d’espacement/champs optionnels d’`advisor.consult` peuvent repasser.
- **BLOCKER — incorrect :** la spécification exige une preuve `pre_submission` fiable et des états de réservation distincts (`B1_ADMISSION_REVIEW.md:41,45`; `TASKS.md:54`). Les marqueurs d’adaptateur et le résumé haché ne prouvent pas l’absence de soumission; le test forged-proof vérifie uniquement un marqueur dispatcher neutralisé.
- **BLOCKED correctement borné :** l’absence de primitive atomique entre dernière lecture G2 et transport demeure non résolue. Les tests prouvent une révocation visible au callback, pas une révocation après celui-ci.
- **UNKNOWN :** le test d’extension ne vérifie pas l’approbation G2 auditée; la borne 3 600/7 200 est laissée à B1A2 comme prévu.

## Vérifications R1 exécutées

Commit archivé sous `/tmp/m01-b1a1-review-3a745de`; `.venv` et `.autowork-runs` reliés selon la commande fournie. Tests exécutés exactement sur l’archive :

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  tests/test_b1_admission_durability.py \
  tests/test_investigation_service.py \
  tests/test_investigation_tools_contract.py \
  tests/test_operator_tool_invocation.py -q --tb=short
121 passed in 2.86s
```

Ruff ciblé :

```text
.venv/bin/ruff check \
  src/cti_app/application/investigation_service.py \
  src/cti_app/application/investigation_tools.py \
  src/cti_app/application/operator_tool_invocation.py \
  tests/test_b1_admission_durability.py
All checks passed!
```

`git diff --check 058bb4a...3a745de` passe. Aucun fournisseur/live n’a été appelé. **Coût : UNKNOWN.**

## Re-revue après d1580a9

### Constats reclassés

| Sujet | Statut R2 | Sévérité | Conclusion et preuve |
|---|---|---:|---|
| Fausse preuve `pre_submission` fournie par un adaptateur | **REJECTED — corrigé** | LOW | `FrameworkPreSubmissionProof` est un `StrEnum` du domaine (`backend/src/cti_app/domain/investigation.py:30-42`) et `ToolResult` refuse un autre type (`investigation_tools.py:200-208`). Le dispatcher fabrique cette preuve pour ses propres refus et retire le champ de tout résultat retourné par `adapter.execute` (`investigation_tools.py:779-801`). La libération ne lit plus que ce champ (`investigation_service.py:3317-3319`). Le faux adaptateur de test renvoie simultanément une enum, `pre_submission`, `confirmed_non_execution`, `advisor_status=invalid_input` et un faux `bridge_invocation_id`; après dispatch, la réserve reste `RESERVED`, l’invocation passe en `NEEDS_REVIEW`, et une nouvelle clé reçoit 409 sans second appel (`tests/test_b1_admission_durability.py:598-646`). L’ancien résumé advisor « invalid_input » seul n’est pas une preuve (`tests/test_operator_tool_invocation.py:1233-1316`). |
| Liaison de la preuve persistée | **CONFIRMED** | LOW | Le rejeu exige une réserve `RELEASED` de montants exactement égaux à `invocation.reserved_budget`, puis une observation du même investigation, `invocation.id`, outil, version de schéma et `operation_key`; son `object_type` doit parser en enum (`operator_tool_invocation.py:477-519`). `ProductionArtifactObservationWriter` assigne ces métadonnées depuis l’invocation et le champ enum, sans lire les marqueurs de sortie (`investigation_service.py:170-240`). `CanonicalEvidenceSummaryPort` vérifie le SHA des octets avant de rendre le JSON (`research_context_builder.py:286-327`). Le SHA prouve l’intégrité du blob; l’attestation dépend aussi du writer et du dépôt canoniques. Le code exclut une observation d’une autre invocation et une réserve partielle. Je n’ai pas trouvé de test négatif distinct pour référence croisée, montants partiels ou SHA corrompu : couverture de ces cas **UNKNOWN**. |
| État de preuve persisté comme métadonnée | **CONFIRMED** | MEDIUM | La preuve est écrite dans `EvidenceObservation.object_type` comme texte puis reconstruite par `FrameworkPreSubmissionProof(observation.object_type)` (`investigation_service.py:211-238`; `operator_tool_invocation.py:499-519`). La valeur est allowlistée à la lecture, donc aucun contournement adaptateur n’est établi, mais `backend/AGENTS.md:14-15` demande de ne pas encoder un état de contrôle dans une chaîne libre ou une métadonnée. À traiter par champ/record de domaine typé lors d’une évolution de persistance. |
| Empreinte sémantique / nouvelle clé | **CONFIRMED** | HIGH | Le scan s’applique aux paramètres bruts et seulement au même `tool_name` (`operator_tool_invocation.py:579-619`); son normaliseur traite `question` et `evidence_refs`, pas les autres défauts ou représentations acceptées (`:1018-1037`). Contournement concret 1 : `vt.file_report.file_hash` accepte les deux casses (`investigation_vt_tools.py:81,229-238`) puis `normalize_file_hash` fait `strip().lower()` avant la requête fournisseur (`virustotal.py:451-455`; `investigation_vt_tools.py:686-696`). Une invocation ambiguë en majuscules et une nouvelle clé en minuscules ont des empreintes différentes mais le même effet. Contournement 2 : `figure_plan.finding_refs` accepte UUID majuscules/minuscules, les parse en UUID puis les réécrit canoniquement (`investigation_advisor_tools.py:2798-2801,1821-1847`), mais le normaliseur ne canonise que `evidence_refs`. Contournement 3 : `review_text` omettable résout `opinion_id` par défaut, `opinion_version` en latest et `form` en `body`; ces mêmes valeurs explicites aboutissent aux mêmes entrées advisor mais donnent une empreinte différente (`investigation_advisor_tools.py:1324-1344`). Les trois tests R2 couvrent bien strip/absent-vide de `question` et `evidence_refs` vide, mais aucun de ces autres cas (`test_b1_admission_durability.py:461-554`). Le statut **HIGH** de la porte « nouvelle clé avec même contenu » reste donc ouvert. |
| Outil différent dans la garde sémantique | **REJECTED** | LOW | La comparaison ignore volontairement un ancien outil de nom différent (`operator_tool_invocation.py:581-583`), ce qui distingue les opérations. Je n’ai pas trouvé dans le registre un alias qui exécute le même appel advisor ou `vt.file_report` sous un autre nom; les aliases repérés concernent la version de schéma, pas le nom d’outil (`:462-475`). Toute future aliasation d’outil devra partager une identité canonique. |
| Concurrence avec même version CAS | **CONFIRMED** | LOW | Le test force deux appels distincts à lire la même version, puis vérifie un seul succès, un échec `stale_working_state`, un appel d’adaptateur et zéro soumission Bridge (`tests/test_b1_admission_durability.py:558-594`). Le CAS d’état est effectué avant dispatch (`operator_tool_invocation.py:621-770`). Cela prouve le comportement de la fixture mémoire; l’intégration concurrente multi-processus avec dépôt PostgreSQL n’a pas été exécutée ici (**UNKNOWN**). |
| Coût / clé entre service et dispatcher | **CONFIRMED** | LOW | La nouvelle version conserve le coût journalisé et transmet le même coût et la même clé; le test remplace le coût courant du registre (30 → 99) mais vérifie la réserve et consommation de 30 sous la même clé (`tests/test_b1_admission_durability.py:182-232`). Un second échec de réserve libère la première sans consommation ni appel (`:235-282`). Aucun coût différent recalculé ou réserve orpheline n’est établi. |
| Crash après persistance avant dispatch | **CONFIRMED** | LOW | Le journal `RUNNING` et la réserve précèdent le dispatch (`investigation_service.py:1133-1153`). Le rejeu devient `NEEDS_REVIEW`, ne transporte pas, ne consomme pas et garde la réserve `RESERVED` (`tests/test_b1_admission_durability.py:366-412`). L’immobilisation est explicite et attend une réconciliation; elle n’est pas une libération silencieuse. |
| CAS obsolète et révocation visible au contrôle | **REJECTED** | LOW | Le test CAS préserve la révocation, zéro droit fournisseur et zéro appel (`tests/test_b1_admission_durability.py:716-752`). Le callback recharge enveloppe, décision, droits et montant juste avant l’entrée adaptateur (`investigation_service.py:1190-1236`); le test observe la révocation à ce point et aucune sortie adaptateur/Bridge (`tests/test_b1_admission_durability.py:755-804`). |
| Révocation après contrôle, avant transport | **CONFIRMED — toujours BLOCKED** | BLOCKER | L’adaptateur advisor construit et archive le dossier après le callback, puis appelle `submit_bridge_consultation_once` sans primitive de contrôle atomique (`investigation_advisor_tools.py:548-578`). Une révocation dans cette fenêtre reste possible. `docs/mission01/TASKS.md:61` continue de déclarer l’atomicité révocation/dispatch `BLOCKED`; ce statut est correctement borné et ne prétend pas fermer la fenêtre. |
| Refus 266 s, réserve et extension G2 | **REJECTED — vérification R1 complétée** | LOW | Le refus avec 266 s restants prouve `consumed_budget={}`, réserve de l’appel `RELEASED`, réserve antérieure de 1 590 s toujours active, zéro appel adaptateur et zéro soumission Bridge; l’observation de preuve porte le bon `invocation_ref` et `operation_key` (`tests/test_b1_admission_durability.py:286-329`). Le test utilise maintenant `InvestigationEnvelopeService.extend` avec acteur, motif et corrélation, puis vérifie la décision enregistrée (`:331-363`). L’extension elle-même n’envoie rien; le retry autorisé exécute ensuite un adaptateur de test une fois. La preuve porte sur le workflow hors ligne, pas sur un appel fournisseur live. |
| Refus advisor pré-transport émis par l’adaptateur | **CONFIRMED** | MEDIUM | Le code advisor décrit `_input_failure` comme refus définitif pré-transport mais ne renvoie que des marqueurs libres (`investigation_advisor_tools.py:1050-1091`). Le dispatcher retire la preuve enum de tout retour adaptateur; le service considère alors un échec externe sans preuve comme ambigu, le classe `NEEDS_REVIEW` et conserve la réserve (`investigation_service.py:1238-1266,2791-2804`). Une nouvelle clé reste bloquée jusqu’à la réconciliation opérateur `NOT_EXECUTED`, auditée par acteur/motif (`:1779-1905`). Le refus framework budget est retryable après extension, mais ce refus local adapter/transport n’a pas de retry automatique après preuve d’absence de soumission. Aucun test R2 ne couvre ce cas. |
| Réserve 3 600 s face au timeout Research 7 200 s | **CONFIRMED** | HIGH | Le delta ne modifie pas cette limite, qui reste attribuée à B1A2 (`docs/mission01/B1_ADMISSION_REVIEW.md:17`; `docs/mission01/TASKS.md:56`). La limite runtime exacte est **UNKNOWN**; le test 266 s ne prouve pas le timeout effectif. |
| Régressions et périmètre | **CONFIRMED** | LOW | Le delta touche quatre modules applicatifs et trois fichiers de tests; aucun fichier migration ou B0A. Les suites ciblées passent. Aucun fournisseur live n’a été appelé. |
| Complexité | **PLAUSIBLE** | LOW | La boucle de lecture/résumé des observations est désormais unifiée dans `_framework_pre_submission_proof`. Le dispatcher conserve deux petites tables de mapping preuve → statut et preuve → raison (`investigation_tools.py:819-850`); faible coût de maintenance, sans défaut bloquant établi. |

### Résultat des vérifications R2

L’archive du commit exact `d1580a93c8c9a15ba1584b2974c74dd16adb1de0` a été créée sous `/tmp/m01-b1a1-review-d1580a9`; `.venv` et `.autowork-runs` ont été reliés comme demandé. Commande exécutée :

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  tests/test_b1_admission_durability.py \
  tests/test_investigation_service.py \
  tests/test_investigation_tools_contract.py \
  tests/test_operator_tool_invocation.py -q --tb=short
124 passed in 2.87s
```

Ruff ciblé sur les quatre modules applicatifs du delta et les trois fichiers de tests modifiés :

```text
.venv/bin/ruff check \
  src/cti_app/application/investigation_service.py \
  src/cti_app/application/investigation_tools.py \
  src/cti_app/application/operator_tool_invocation.py \
  src/cti_app/domain/investigation.py \
  tests/test_b1_admission_durability.py \
  tests/test_investigation_service.py \
  tests/test_operator_tool_invocation.py
All checks passed!
```

**Verdict final : FAIL**, malgré les 124 tests verts : la nouvelle clé peut encore contourner la revue d’une invocation ambiguë avec des paramètres canoniquement équivalents. L’atomicité révocation/dispatch demeure correctement `BLOCKED`. **UNKNOWN :** coût/tokens d’exécution; borne runtime 3 600/7 200 s; tests négatifs dédiés pour observation croisée, montants partiels et SHA corrompu; concurrence multi-processus/production. Aucun fournisseur live n’a été contacté.

## Re-revue finale après 2f88a42

**TASK_ID :** `M01-B1A1-REVIEW-R3`

**Cible :** `2f88a42352575fb486a84b581644706e6be52576`, intégré par `2cafb379372b603d9d873a1070f1470364aec7e0`. Cette correction a été implémentée par le parent et a fait l’objet d’une relecture adversariale indépendante.

**Verdict final R3 : FAIL.** Le patch ferme les trois formes précises de R2 testées : casse/espaces du hash `vt.file_report`, casse UUID de `figure_plan.finding_refs`, et valeurs omises/explicites de `review_text` (`opinion_id`, `opinion_version`, `form`). La garde reste toutefois contournable par d’autres représentations admises par les adaptateurs, ce qui laisse ouverte la porte HIGH « nouvelle clé après résultat ambigu » requise par B1A1.

### Constats R3

| Sujet | Statut | Sévérité | Preuve et conclusion |
|---|---|---:|---|
| Sélecteurs `review_text` non canoniques | **CONFIRMED** | HIGH | Le normaliseur fixe l’opinion, retire la version et fixe `form`, mais conserve les autres paramètres bruts (`backend/src/cti_app/application/operator_tool_invocation.py:1076-1102`). Sans `units`, l’adaptateur synthétise U1 à partir de `block_type`, `form` et `anchor` (`backend/src/cti_app/application/investigation_advisor_tools.py:1612-1623`). Pour `form="body"`, `anchor` ne sélectionne rien et la cible renvoyée porte `anchor=None` (`:1656-1681`) : une requête ambiguë sans `anchor` et une nouvelle clé avec `anchor="x"` ont le même effet mais des empreintes différentes. Omettre `block_type` choisit le premier bloc éligible; nommer explicitement ce bloc donne aussi la même cible (`:1657-1678`). La représentation `units` d’un seul U1 équivalent n’est pas repliée par l’empreinte (`:1700-1703`; schéma `:3318-3321`). Le test R3 ne couvre que les défauts opinion/version/form (`backend/tests/test_b1_admission_durability.py:675-703`). |
| UUID imbriqués dans `draft_text` | **CONFIRMED** | HIGH | L’empreinte ne canonicalise que les références de premier niveau et `figure_plan.finding_refs` (`operator_tool_invocation.py:1051-1075`). Le schéma `advisor.consult` accepte des UUID dans `assertions[].refs[].id` (`investigation_advisor_tools.py:3204-3211,3313-3317`); l’adaptateur les parse en `UUID` puis réécrit leur chaîne canonique (`:1510-1528`). Une casse UUID différente dans une assertion valable donne donc la même référence vérifiée et un fingerprint différent; une nouvelle clé peut passer. |
| Références `figure_plan` dupliquées après canonicalisation | **CONFIRMED** | HIGH | Le normaliseur trie les UUID canonicalisés, mais ne retire pas les doublons (`operator_tool_invocation.py:1063-1075`). Le schéma n’interdit que les éléments JSON identiques, donc une même UUID en casse haute et basse est admise (`investigation_advisor_tools.py:3323-3327`). L’adaptateur parse la liste en UUID puis sélectionne les findings par appartenance (`:286-309`); le doublon canonicalisé ne change pas la sélection. Une liste `finding_refs` contenant une référence et une autre contenant ses deux casses peut ainsi produire le même effet avec des empreintes distinctes. |
| Champs normalisés des autres outils VT | **CONFIRMED** | HIGH | La branche hash du fingerprint ne traite que `vt.file_report` (`operator_tool_invocation.py:1098-1102`). `vt.behaviour` et `vt.behaviour_pcap` appellent aussi `normalize_file_hash` (`investigation_vt_tools.py:1186-1187,1348-1350`). `vt.relationships` normalise `object_id` et met `relation` en minuscules après `strip()` (`:862-869,1790-1809`); `vt.object_report` normalise aussi les identifiants IP, domaines et URL (`:746-755,1790-1847`). `vt.intelligence_search` normalise la requête, la justification, les références UUID et `order` (`:998-1022,1158-1178,1732-1763`). Ces variantes passent sous une nouvelle clé puisque l’empreinte ne les traite pas pour ces noms d’outils; le cas d’un hash de fichier via `vt.relationships.object_id` ou `vt.behaviour.file_hash` est un exemple direct. |
| Ordre de listes et alias d’outil | **PAS DE CONTOURNEMENT ACTUEL ÉTABLI** | LOW | `figure_plan` sélectionne les findings par appartenance, donc le tri de `finding_refs` rend l’ordre neutre (`investigation_advisor_tools.py:286-309`). Le tri des `evidence_refs` peut bloquer une requête dont l’ordre aurait influencé le prompt : c’est une sur-restriction conservatrice, pas un nouvel appel permis. Le registre intégré recherche par paire exacte `(name, schema_version)` (`investigation_tools.py:379-409`); l’alias legacy trouvé porte sur la version de schéma d’un même outil (`operator_tool_invocation.py:463-476`), pas sur son nom. Aucun alias de nom d’outil intégré n’a été trouvé; les registres injectés hors de ce parcours ne sont pas couverts. |
| Preuve `pre_submission`, coût/clé, crash, extension G2 | **CONFIRMED** | LOW | La preuve reste un `StrEnum`; `ToolResult` refuse un autre type, le dispatcher efface tout champ retourné par un adaptateur, et seul un résultat attesté par le framework est accepté (`domain/investigation.py:30-42`; `investigation_tools.py:200-208,779-801`; `investigation_service.py:3317-3319`). Le writer canonique rattache l’observation à l’invocation et à la clé (`:204-239`), puis le rejeu vérifie investigation, invocation, outil, version, clé, réserve `RELEASED` de montant égal et valeur enum allowlistée (`operator_tool_invocation.py:478-521`). La réserve et l’appel dispatcher réutilisent le coût journalisé et la même clé (`tests/test_b1_admission_durability.py:182-233`); le rejeu après crash reste `NEEDS_REVIEW`, réserve non consommée, sans dispatch (`:368-413`). L’extension passe par `InvestigationEnvelopeService.extend`, et son acteur, motif, corrélation et décision G2 sont vérifiés (`:334-364`). |
| CAS et révocation | **CONFIRMED — toujours BLOCKED à la frontière** | BLOCKER | Le CAS d’enveloppe obsolète conserve la révocation et évite dispatch/transport (`tests/test_b1_admission_durability.py:865-901`); la révocation visible au contrôle précédant l’entrée adaptateur est aussi refusée (`:905-960`). Ces vérifications ne rendent pas atomique la fenêtre après le contrôle et avant le transport; le statut `BLOCKED` antérieur reste correctement borné (`docs/mission01/TASKS.md:62`; `investigation_advisor_tools.py:548-578`). |
| Dette `EvidenceObservation.object_type` | **CONFIRMED — non bloquante** | MEDIUM | La preuve est persistée dans `object_type` comme chaîne puis reconstruite par l’enum; la lecture rejette les valeurs hors allowlist et exige les liens canoniques ci-dessus (`investigation_service.py:211-239`; `operator_tool_invocation.py:500-520`). La dette de type reste documentée dans la section R2; aucun contournement par cette représentation n’est établi. |

### Vérifications R3

Archive exacte de `2cafb37` sous `/tmp/m01-b1a1-review-r3.pO0mpb`; lien `.venv` vers `/home/nill/perso/work/AutoWork/backend/.venv`. Commande demandée :

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  tests/test_b1_admission_durability.py \
  tests/test_investigation_service.py \
  tests/test_investigation_tools_contract.py \
  tests/test_operator_tool_invocation.py -q --tb=short
125 passed, 2 failed in 2.91s
```

Les deux échecs sont `test_operator_dispatcher_wires_minimal_advisor_request_to_strict_bridge_transport` et `test_advisor_recommendations_record_acceptance_refusal_and_empirical_outcomes`; les deux retournent `required_canonical_evidence_not_in_payload`. Leur cause est **UNKNOWN**; les tests de durabilité, dont les nouveaux cas R3, passent dans cette exécution.

Ruff ciblé sur les deux fichiers modifiés :

```text
.venv/bin/ruff check \
  src/cti_app/application/operator_tool_invocation.py \
  tests/test_b1_admission_durability.py
All checks passed!
```

**UNKNOWN :** cause des deux échecs advisor ci-dessus; comportement concurrent avec dépôt PostgreSQL multi-processus; durée runtime effective face aux valeurs 3 600/7 200 s; coût/tokens. Aucun fournisseur, transport live ou état opérationnel n’a été contacté ou modifié. Le delta R3 ne mérite pas d’être accepté tant que les contournements HIGH de fingerprint ne sont pas fermés.
