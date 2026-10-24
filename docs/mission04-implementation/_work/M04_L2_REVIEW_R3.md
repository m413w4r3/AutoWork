# Mission 04 / L2 — contre-revue R3

**Verdict : APPROVED**
**Base de cette contre-revue :** `0ee3504cd085c2379323f258276b5d5d24ac2f1f`
**HEAD examiné :** `5ab00368fa1496e5dffadb7b8e8c3369bdf3bd53`
**Base initiale du lot :** `df9e091076cbfd3147690f184c5562d626d8ae66`

Le delta R3 ajoute la comparaison des métadonnées canoniques de refus avec leurs alias, trois tests de service et leur documentation. Aucun chemin DB, provider, Bridge ou migration n'est modifié. Le fichier R2 déjà présent dans le lot est un artefact de revue antérieur.

## Findings classés

### LOW — deux cas vérifiés en probe, sans test permanent dédié

Le fichier de tests couvre le conflit canonique/alias, l'utilisation d'un alias seul et les valeurs répétées contradictoires. Il ne contient pas de test permanent pour les valeurs canoniques et alias identiques, ni pour le fallback générique lorsque raison et code sont absents. Des probes temporaires traversant `ProposalConversationService.propose` ont vérifié ces deux cas : la valeur identique ne crée aucun warning de conflit; sans valeur utile, la raison générique est retournée, le code reste `None`, et P09 n'est pas appelé. Ce manque de couverture durable n'empêche pas l'intégration.

### Aucun finding bloquant ou majeur

- `canonical_or_alias` conserve la première valeur canonique non vide; un alias différent déclenche un warning dans `transformations`. Un alias seul est retenu sans warning de conflit. Les répétitions d'un champ gardent la première valeur non vide et signalent les contradictions sans inclure les valeurs dans le warning ([source](../../../backend/src/cti_app/application/invariant_proposals.py:1297), [tests](../../../backend/tests/test_invariant_proposals.py:1444), [notes](M04_L2_NOTES.md:65)).
- Le parcours frais et le replay utilisent le même gate P09. Un refus retourne `()` avant validation YARA et sans appel au registre. Le test de replay refusé confirme zéro appel P09, un seul appel externe, et le même outcome, la même raison et le même code au replay ([gate](../../../backend/src/cti_app/application/invariant_proposals.py:935), [test](../../../backend/tests/test_invariant_proposals.py:1566)). Un probe complémentaire `partial` puis `refused` retourne `REFUSED`, `invariant_results == ()` et zéro appel P09.
- Un `STATUS` présent mais vide est rejeté par `ProposalOutputValidationError` avant P09; le probe de service l'a confirmé. Un `STATUS` inconnu est également rejeté par le test permanent. Le statut absent garde le comportement historique `SUCCEEDED`, avec warning archivé et candidat transmis à P09; cette limite est explicitement documentée et testée ([source](../../../backend/src/cti_app/application/invariant_proposals.py:1313), [test](../../../backend/tests/test_invariant_proposals.py:1405), [notes](M04_L2_NOTES.md:13)).
- Les schémas wire, versions, `ProposalResponse` et identifiants ne sont pas changés par le delta R3. L'outcome et les détails du refus restent additifs à `ProposalConversationResult`. Les réponses JSON typées n'ont pas de chemin de conversion en refus ajouté ici; les erreurs de validation restent des erreurs.
- Nuance de replay : un replay réussi peut rappeler le service P09 comme avant ce lot. L'enregistrement reste dédupliqué par la clé de proposition, le verrou et la contrainte unique avec `ON CONFLICT DO NOTHING` dans le registre persistant. Cette revue n'a fait aucun test DB; le replay refusé, concerné par ce lot, ne rappelle pas P09.

## Standards

La relecture indépendante ne relève aucune violation des règles backend et aucun smell matériel dans le delta R3. Le statut de contrôle demeure typé par `ProposalOutcome`; les valeurs wire inconnues sont rejetées à la frontière du parseur. Les tests restent hors provider.

## Spec

La relecture indépendante confirme la politique canonique/alias demandée et ne trouve pas de déviation comportementale. Le seul écart de couverture est le finding LOW ci-dessus : les cas égalité et fallback ont été vérifiés par probes, sans oracle permanent.

## Commandes et résultats

Depuis `backend/` :

```text
UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short
69 passed in 0.58s

UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -k 'refused_wire or refusal or missing_status or unknown_status or partial_wire_without_refusal or unrecognized_wire_output' -q --tb=short
12 passed, 57 deselected in 0.36s

UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py
All checks passed!

UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py
3 files already formatted

UV_CACHE_DIR=/tmp/uv-cache MYPY_CACHE_DIR=/tmp/mypy-r3-cache uv run --no-project --python 3.12 mypy src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py
Success: no issues found in 2 source files
```

Typecheck global, depuis la racine :

```text
make typecheck-backend UV_SYNC=true UV_RUN='UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12'
Échec : 60 erreurs mypy dans 13 fichiers; aucune dans les modules du lot.
```

Les 13 fichiers en erreur sont `bridge_investigator.py`, `context_pagination.py`, `investigation_audit_export.py`, `investigation_persistence.py`, `operator_tool_invocation.py`, `research_context_builder.py`, `investigation_audit_reader.py`, `test_investigation_audit_export_postgres.py`, `test_advisor_consultation.py`, `test_investigation_vt_tools.py`, `test_mission01_l2_context_pagination.py`, `test_model_gateway.py` et `test_research_context_builder.py`.

Depuis la racine :

```text
git diff --check df9e091076cbfd3147690f184c5562d626d8ae66..5ab00368fa1496e5dffadb7b8e8c3369bdf3bd53
OK, aucune sortie.
```

## Risque résiduel, intégration et rollback

Le risque résiduel accepté est qu'un wire sans `STATUS` est traité comme `SUCCEEDED`; un candidat valide peut atteindre P09, et le warning `proposal status missing` rend cette absence observable. Cette compatibilité est explicitement documentée dans `M04_L2_NOTES.md` et couverte par `test_missing_status_keeps_legacy_success_and_warning`. Un statut inconnu ou vide est rejeté.

**Le lot est intégrable localement.** Aucun commit, merge, déploiement, appel provider ou accès DB n'a été effectué pendant cette contre-revue. Rollback : revenir du changement R3 à `0ee3504cd085c2379323f258276b5d5d24ac2f1f`; le delta R3 est limité au parseur, ses tests et la note de lot, sans état persistant ni migration.
