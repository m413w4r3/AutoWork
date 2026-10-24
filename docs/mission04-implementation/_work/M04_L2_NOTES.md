# Mission 04 — lot L2 (P10 refusal)

Date : 2026-10-24
Base : `df9e091076cbfd3147690f184c5562d626d8ae66`
Worktree : `mission04/l2-p10-refusal`

## Décision

Le statut wire `STATUS: refused` devient un outcome typé `ProposalOutcome.REFUSED`, accompagné de `refusal_reason` et, si le wire en fournit un, `refusal_code` dans `ProposalConversationResult`. Une raison par défaut explicite est conservée si le wire ne fournit aucune raison. Le statut est lu directement dans les champs wire, jamais déduit d'un warning. En cas de statuts répétés et contradictoires, toute occurrence explicite de `refused` est terminale.

Les réponses `partial` exposent `ProposalOutcome.PARTIAL` et gardent leur passage antérieur vers P09. Les sorties JSON gardent le comportement existant et prennent l'outcome par défaut `SUCCEEDED`. Le schéma wire, la version de prompt/parser, les identifiants de candidat, l'idempotence et la persistance restent inchangés; aucune migration n'est requise.

Politique des statuts : un `STATUS` absent reste permissif pour compatibilité historique : l'outcome est `SUCCEEDED`, le warning `proposal status missing` est archivé, et un candidat peut atteindre P09. Un statut présent hors de `succeeded`, `partial`, `refused` est un wire invalide et lève `ProposalOutputValidationError` avant P09; il n'est pas converti en refus. Ce choix garde l'absence distincte d'une valeur inconnue.

Les parcours frais et replay appellent le même gate `_process_response_for_p09`. Un refus retourne `invariant_results == ()` avant la validation YARA/P09 et n'enregistre aucun candidat dans P09. Les candidats parsés restent disponibles dans `response` pour conserver le contenu normalisé et ses identifiants; le refus empêche leur handoff.

Pour `REFUSAL_REASON` et `REFUSAL_CODE` répétés, la première valeur non vide dans l'ordre du wire est conservée. Si plusieurs valeurs non vides sont contradictoires, elles restent non divulguées dans un warning explicite et la première est utilisée; si aucune valeur n'est fournie, la raison générique existante s'applique et le code reste `None`.

## RED

Commande, avant le correctif :

```text
cd backend
UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 pytest tests/test_invariant_proposals.py -k 'refused_wire_with_candidate or refused_wire_without_candidate or partial_wire_without_refusal or invalid_json_with_refused_status or replay_of_refused_wire' -q --tb=short
```

Résultat : `4 failed, 1 passed, 58 deselected`. Le wire refusé avec candidat appelait P09 une fois; le replay l'appelait deux fois. Les cas refusé sans candidat et `partial` échouaient parce que `ProposalConversationResult` n'exposait pas encore d'outcome. L'erreur de validation sur sortie invalide restait présente.

La revue indépendante a ensuite trouvé le cas des champs `STATUS` répétés. L'oracle ajouté a échoué avant ce correctif : `test_later_refusal_status_is_terminal_for_p09` observait un appel P09 avec `STATUS: succeeded` suivi de `STATUS: refused`.

Pour les trois findings de reprise, RED supplémentaire :

```text
cd backend
UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -k 'missing_status or unknown_status_wire or repeated_refusal_fields' -q --tb=short
```

Résultat avant correctif : `2 failed, 1 passed, 64 deselected`. Le test d'absence de statut confirmait déjà la compatibilité historique; `unknown` n'était pas rejeté et les premiers champs de refus vides masquaient les valeurs ultérieures.

## GREEN

- Oracle ciblé après correctif, incluant statuts répétés : `6 passed, 58 deselected`.
- `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -k 'refused_wire or later_refusal_status or missing_status or unknown_status_wire or repeated_refusal_fields or partial_wire_without_refusal' -q --tb=short` : `9 passed, 58 deselected`.
- Fichier P10 complet après reprise : `67 passed` avec `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short`.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` : **All checks passed**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` : **3 files already formatted**.
- `MYPY_CACHE_DIR=/tmp/mypy-cache UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 mypy src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py` : **Success: no issues found in 2 source files**.
- `rg -n 'ProposalConversationResult|candidate_results|_pass_candidates_to_p09' backend/src backend/tests` : aucun consommateur additionnel hors du service P10; le fichier de tests P10 couvre les deux appels frais/replay.
- Revue Luna xhigh en lecture seule : le gate partagé protège le parcours frais et replay; le cas de statuts répétés est corrigé; aucun autre problème de risque élevé relevé.

`UV_CACHE_DIR=/tmp/uv-cache make typecheck-backend` a été exécuté mais échoue sur des erreurs mypy dans des modules/tests hors lot (notamment `context_pagination.py`, `investigation_persistence.py`, `investigation_audit_export.py`, `research_context_builder.py` et leurs tests). L'antériorité de ces erreurs n'a pas été vérifiée par un typecheck de la base. Aucune erreur n'est signalée dans les trois fichiers modifiés.

## Fichiers et rollback

- `backend/src/cti_app/application/invariant_proposals.py` : métadonnées de parse explicites et gate P09 commun aux deux parcours.
- `backend/src/cti_app/domain/invariant_proposals.py` : enum `ProposalOutcome`.
- `backend/tests/test_invariant_proposals.py` : refus frais/replay, raison/code, statut partiel, wire invalide et statuts répétés.
- `docs/mission04-implementation/_work/M04_L2_NOTES.md` : présente note.

Rollback : retirer ces changements de code, tests et note; aucun état persistant ni schéma DB n'a été modifié.

Risques résiduels : le typecheck global reste rouge hors de ce lot. Par compatibilité acceptée, un wire sans `STATUS` conserve `SUCCEEDED` et peut transmettre un candidat à P09; le warning rend l'absence observable. Un statut inconnu est rejeté. Les réponses refusées conservent les candidats analysés dans `response`; les consommateurs doivent respecter l'outcome, tandis que le service garantit déjà qu'aucun candidat n'atteint P09.

## Reprise R2 — conflits entre noms canoniques et alias

Pour les métadonnées d'un refus, `REFUSAL_REASON` est prioritaire sur `REASON`, et `REFUSAL_CODE` sur `CODE`. Les deux champs de chaque paire sont maintenant évalués : la valeur canonique non vide est retenue; si l'alias est aussi non vide et différent, `transformations` archive un warning de conflit sans inclure les valeurs. Si le champ canonique est absent ou vide, l'alias non vide est utilisé sans warning de conflit. Les répétitions de chaque nom gardent séparément la règle précédente : première valeur non vide dans l'ordre du wire, warning si les valeurs non vides se contredisent.

La politique de compatibilité antérieure reste inchangée : `STATUS` absent conserve `SUCCEEDED` avec warning et peut transmettre un candidat à P09; un `STATUS` inconnu lève `ProposalOutputValidationError` avant P09.

### RED

```text
cd backend
UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -k 'canonical_fields_conflicting_with_aliases or alias_fields_without_canonical' -q --tb=short
```

Résultat avant le correctif : `1 failed, 1 passed, 67 deselected`. Le cas alias seul passait, mais le conflit canonique/alias n'était pas archivé dans `transformations`.

### GREEN

- Groupe ciblé de refus/statuts P10 : `12 passed, 57 deselected` avec `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -k 'refused_wire or refusal or missing_status or unknown_status or partial_wire_without_refusal or unrecognized_wire_output' -q --tb=short`.
- Fichier P10 complet : `69 passed` avec `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short`.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` : **All checks passed**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` : **3 files already formatted**.
- `MYPY_CACHE_DIR=/tmp/mypy-cache UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 mypy src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py` : **Success: no issues found in 2 source files**.

Le replay et le parcours frais restent derrière le même gate P09; le refus explicite garde priorité sur les candidats et les statuts répétés. Aucun schéma, version wire, identifiant, provider ou état DB n'a été modifié.
