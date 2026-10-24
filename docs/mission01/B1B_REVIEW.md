# Mission 01 B1B — revue critique indépendante

**Objet :** commit `e4850d2d6bc070ce1a105083feade9b3ec81b56b` (branche
`feat/mission01-b1b-temporal-recovery`), base `6b195122`.
**Revue :** agent Paseo `b1a2f600-21f9-4e97-8f66-e12afc5f66b0`, GPT-6 Luna
xhigh, lecture seule, worktree `m01-b1b-review`.
**Oracle rejoué par la revue :** 170 passed. Aucun service ni fournisseur
live contacté.

## Verdict : FAIL

### BLOCKER — contention du pacer vs deadline G2

`BridgeSubmissionPacer.wait_for_next_submission` conserve `self._lock` pendant
tout le sommeil (`bridge_investigator.py:95-113`). Un
`BridgeInvestigator.turn` concurrent sans deadline peut donc bloquer
`defer_submissions` et les autres waiters pendant un long `Retry-After`. Le
chemin advisor après 429 appelle `defer_submissions` sans borne
(`bridge_investigator.py:380`) et ne contrôle le budget restant qu'après
(`385-396`). La deadline G2 peut être dépassée sans second envoi.

### HIGH — clé de retry et preuve pre_submission

Le premier appel utilise `submit` (`attempt=0`), le retry
`submit_with_attempt(attempt=1)` (`bridge_investigator.py:287-292`); le
transport incorpore `attempt` dans la clé (`546-550`, `1330-1344`). Le test
`test_one_proven_pre_submission_429_retry_uses_retry_after_and_existing_attempt_key`
(`test_b1b_temporal_recovery.py:168-193`) ne vérifie pas la clé et son nom
est trompeur. Risque conditionnel : si un 429 n'attestait pas réellement
l'absence d'envoi, la nouvelle clé autoriserait une seconde génération. Le
parseur HTTP 429 est déjà couvert par
`test_model_adapters.py:782` (pre_submission + Retry-After), mais ce test
n'était pas dans l'oracle B1B.

### MEDIUM — Retry-After non plafonné

`_retry_after_or_none` accepte toute valeur finie positive
(`bridge_investigator.py:417-425`) et l'installe dans le pacer partagé avant
le contrôle de budget (`380`). Une valeur très grande peut bloquer les
appels suivants. Les tests couvrent NaN/infini/négatif/invalide mais pas une
grande valeur finie ni une date HTTP.

### LOW — sleeps réels et état non typé

Le test de réponse tardive utilise `asyncio.sleep(1)` puis `0.02`
(`test_b1b_temporal_recovery.py:358-367`). `TurnSubmission.submission_state`
reste `str | None` (`bridge_investigator.py:175-187`) au lieu d'un `StrEnum`
de domaine.

## Défaut complémentaire confirmé par le parent

`_PermissionAwareInvestigationTransport.submit_with_attempt`
(`investigation_composition.py:780`) n'accepte pas `timeout_seconds`. Le
retry B1B l'appelle avec ce mot-clé : reproduction offline =
`TypeError: ... unexpected keyword argument 'timeout_seconds'`, converti en
`bridge_unavailable`. En composition de production, le retry 429 est donc
cassé et mal classifié. Les tests B1B utilisent un faux transport dédié.

## Résiduels

- Atomicité PostgreSQL et concurrence multi-processus : **UNKNOWN** ; les
  tests de crash utilisent des dépôts en mémoire.
- Le résiduel HIGH B1A1 sur les fingerprints de nouvelles clés demeure hors
  du delta B1B.
- Coût : **UNKNOWN**.

## Recommandation

Ne pas accepter B1B avant : (1) verrou du pacer compatible avec la deadline,
(2) retry aligné sur le contrat attendu et couvert sur états ambigus,
(3) Retry-After plafonné et dates HTTP testées, (4) wrapper
`_PermissionAwareInvestigationTransport` corrigé, (5) sleeps de test
réduits. Réparation `M01-B1B-R1` lancée sur la même branche.

## Deuxième revue après R1 — PASS_WITH_CHANGES

**Objet :** `fd397de12f27b90f0e1a2d3ba45a2538b2ccd08b`.
**Revue :** agent `66c9488e-4eec-4a3b-96cf-4a1da6984e72`, Luna xhigh.
**Oracle rejoué :** 236 passed (ciblé), 289 passed / 6 skipped (élargi).

Les cinq constats R1 sont fermés dans le périmètre examiné : verrou du pacer
relâché pendant l'attente, cible relue sous verrou, jitter conservé, borne
deadline ; états ambigus non retentés ; clés/attempts distincts ; wrapper
transmettant `timeout_seconds` et testé ; Retry-After HTTP-date ; sleeps
≤ 50 ms. Aucun affaiblissement G2/TLP/B1A1/B1A2 observé.

### MEDIUM résiduel — cooldown partagé `Retry-After`

`bridge_investigator.py:441-447` utilise le plafond de retry local (8 s)
comme plafond du cooldown partagé : un `Retry-After` de 10 000 s ne bloque
le pacer que 8 s. Inversement, si le retry local est inéligible (pas de
`submit_with_attempt`) ou si le budget est insuffisant, les retours
`468-469` n'appliquent aucun cooldown partagé. Un `Retry-After=5 s` ne met
donc pas le pacer en pause. Correction R2 : séparer plafond de retry local
et plafond de cooldown partagé, appliquer le cooldown avant tout retour.

### Résiduels

PostgreSQL concurrent, transport Bridge réel, coût : **UNKNOWN**.

