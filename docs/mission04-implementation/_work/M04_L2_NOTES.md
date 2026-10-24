# Mission 04 — lot L2 (P10 refusal)

Date : 2026-10-24
Base : `df9e091076cbfd3147690f184c5562d626d8ae66`
Worktree : `mission04/l2-p10-refusal`

## Décision

Le statut wire `STATUS: refused` devient un outcome typé `ProposalOutcome.REFUSED`, accompagné de `refusal_reason` et, si le wire en fournit un, `refusal_code` dans `ProposalConversationResult`. Une raison par défaut explicite est conservée si le wire ne fournit aucune raison. Le statut est lu directement dans les champs wire, jamais déduit d'un warning. En cas de statuts répétés et contradictoires, toute occurrence explicite de `refused` est terminale.

Les réponses `partial` exposent `ProposalOutcome.PARTIAL` et gardent leur passage antérieur vers P09. Les sorties JSON gardent le comportement existant et prennent l'outcome par défaut `SUCCEEDED`. Le schéma wire, la version de prompt/parser, les identifiants de candidat, l'idempotence et la persistance restent inchangés; aucune migration n'est requise.

Les parcours frais et replay appellent le même gate `_process_response_for_p09`. Un refus retourne `invariant_results == ()` avant la validation YARA/P09 et n'enregistre aucun candidat dans P09. Les candidats parsés restent disponibles dans `response` pour conserver le contenu normalisé et ses identifiants; le refus empêche leur handoff.

## RED

Commande, avant le correctif :

```text
cd backend
UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 pytest tests/test_invariant_proposals.py -k 'refused_wire_with_candidate or refused_wire_without_candidate or partial_wire_without_refusal or invalid_json_with_refused_status or replay_of_refused_wire' -q --tb=short
```

Résultat : `4 failed, 1 passed, 58 deselected`. Le wire refusé avec candidat appelait P09 une fois; le replay l'appelait deux fois. Les cas refusé sans candidat et `partial` échouaient parce que `ProposalConversationResult` n'exposait pas encore d'outcome. L'erreur de validation sur sortie invalide restait présente.

La revue indépendante a ensuite trouvé le cas des champs `STATUS` répétés. L'oracle ajouté a échoué avant ce correctif : `test_later_refusal_status_is_terminal_for_p09` observait un appel P09 avec `STATUS: succeeded` suivi de `STATUS: refused`.

## GREEN

- Oracle ciblé après correctif, incluant statuts répétés : `6 passed, 58 deselected`.
- Fichier P10 complet : `64 passed` avec `UV_CACHE_DIR=/tmp/uv-cache uv run --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short`.
- `UV_CACHE_DIR=/tmp/uv-cache make lint-backend` : Ruff check réussi; format réussi (`845 files already formatted`).
- `rg -n 'ProposalConversationResult|candidate_results|_pass_candidates_to_p09' backend/src backend/tests` : aucun consommateur additionnel hors du service P10; le fichier de tests P10 couvre les deux appels frais/replay.
- Revue Luna xhigh en lecture seule : le gate partagé protège le parcours frais et replay; le cas de statuts répétés est corrigé; aucun autre problème de risque élevé relevé.

`UV_CACHE_DIR=/tmp/uv-cache make typecheck-backend` a été exécuté mais échoue sur des erreurs mypy dans des modules/tests hors lot (notamment `context_pagination.py`, `investigation_persistence.py`, `investigation_audit_export.py`, `research_context_builder.py` et leurs tests). L'antériorité de ces erreurs n'a pas été vérifiée par un typecheck de la base. Aucune erreur n'est signalée dans les trois fichiers modifiés.

## Fichiers et rollback

- `backend/src/cti_app/application/invariant_proposals.py` : métadonnées de parse explicites et gate P09 commun aux deux parcours.
- `backend/src/cti_app/domain/invariant_proposals.py` : enum `ProposalOutcome`.
- `backend/tests/test_invariant_proposals.py` : refus frais/replay, raison/code, statut partiel, wire invalide et statuts répétés.
- `docs/mission04-implementation/_work/M04_L2_NOTES.md` : présente note.

Rollback : retirer ces changements de code, tests et note; aucun état persistant ni schéma DB n'a été modifié.

Risques résiduels : le typecheck global reste rouge hors de ce lot. Les réponses refusées conservent les candidats analysés dans `response`; les consommateurs doivent respecter l'outcome, tandis que le service garantit déjà qu'aucun candidat n'atteint P09.
