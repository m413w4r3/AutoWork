# Mission 01 B1A1 — revue adversariale indépendante

- **TASK_ID :** M01-B1A1-REVIEW
- **Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`
- **Commit examiné :** `3a745de788f2af286274d024a404e6ae7f6728fd` (`fix(investigation): harden admission durability`)
- **Intégration indiquée :** `fe68c85`

**Périmètre :** lecture du commit et des sources réelles, tests offline demandés, Ruff ciblé. Aucune migration, aucun accès live ou fournisseur.

## Verdict

**FAIL.** Le coût et la clé de réservation sont désormais transmis de façon cohérente, et le crash après admission reste conservateur. Deux portes de l’objectif ne sont toutefois pas fermées : une nouvelle clé peut contourner la comparaison en modifiant des arguments que l’adaptateur normalise, et un adaptateur peut encore faire accepter une fausse preuve de pré-soumission. La frontière révocation/dispatch est, elle, correctement déclarée `BLOCKED`.

Le statut `PARTIAL / BLOCKED` de B1A1 est donc correctement borné pour l’atomicité révocation/dispatch; il ne doit pas être interprété comme la fermeture de toutes les portes. La mention « portes fermées » de la ligne B1A1 doit être corrigée après traitement des deux constats ci-dessus. Le risque de réservation 3 600 s face au délai Research de 7 200 s reste distinctement attribué à B1A2.

## Constats

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

## Axes indépendants

### Standards

- **Violation documentée — MEDIUM.** `backend/AGENTS.md:14-15` exige les états de contrôle typés en `StrEnum` du domaine et interdit de les encoder en chaînes libres ou clés de métadonnées. Le nouveau chemin de retry/release branche sur les champs libres `pre_submission_reason`, `failure_stage` et `confirmed_non_execution` (`investigation_service.py:3354-3368`, `operator_tool_invocation.py:476-503`). Une différence de chaîne change la transition d’état et le droit de retry. Ruff ne détecte pas cette violation sémantique.
- **Heuristique — LOW, duplication possible.** Les deux méthodes de preuve répètent la boucle de chargement observation → résumé (`operator_tool_invocation.py:483-493`, `:523-535`); le hunk de `:483` ne vérifie pas `invocation_ref` ni `object_id`, contrairement à `:523-532`.
- Aucun autre écart documenté ou smell bloquant n’a été établi. Ruff ciblé passe.

### Spécification

- **HIGH — partiel :** la correction de clé/montants est conforme; toutefois la garde « même contenu avant réconciliation » demandée par `docs/mission01/B1_ADMISSION_REVIEW.md:43-45` et `docs/mission01/TASKS.md:54` ne compare pas les paramètres après normalisation effective. Les variantes d’espacement/champs optionnels d’`advisor.consult` peuvent repasser.
- **BLOCKER — incorrect :** la spécification exige une preuve `pre_submission` fiable et des états de réservation distincts (`B1_ADMISSION_REVIEW.md:41,45`; `TASKS.md:54`). Les marqueurs d’adaptateur et le résumé haché ne prouvent pas l’absence de soumission; le test forged-proof vérifie uniquement un marqueur dispatcher neutralisé.
- **BLOCKED correctement borné :** l’absence de primitive atomique entre dernière lecture G2 et transport demeure non résolue. Les tests prouvent une révocation visible au callback, pas une révocation après celui-ci.
- **UNKNOWN :** le test d’extension ne vérifie pas l’approbation G2 auditée; la borne 3 600/7 200 est laissée à B1A2 comme prévu.

## Vérifications exécutées

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
