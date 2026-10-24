# Mission 04 — Revue indépendante du lot L6

**Base :** `df9e091076cbfd3147690f184c5562d626d8ae66`

**Lot / HEAD vérifié :** `40f1ac271c2180fbc5bad9d034d2dcad75a48ab4`
**Verdict : `APPROVED`**, avec un risque résiduel P3 décrit ci-dessous.

## Standards

Aucune violation dure des règles de `AGENTS.md` ou `backend/AGENTS.md` trouvée. Heuristique non bloquante : les deux allowlists de consultations admises restent dupliquées dans `backend/src/cti_app/application/investigation_advisor_tools.py:3450,3483`; elles pourraient diverger lors d’une prochaine extension.

## Spec et constats

Le diff correspond au scope L6 de `docs/mission03-architecture/IMPLEMENTATION_PLAN.md:193-212` et aux conditions révisées de `docs/mission04-implementation/_work/L6_SCOPE_REVIEW.md:26-46`.

- **Périmètre vérifié :** les seuls changements de production sont les deux allowlists et la garde Suricata dans `investigation_advisor_tools.py:3449-3450,3482-3483,3595-3652`. Le writer initial, le transport Bridge, les lecteurs de preuves, `advisor_contracts.py`, `investigation_composition.py`, le mode worker, les migrations et les adapters DB/blob/provider ne sont pas modifiés. Les deux fichiers de tests ne comportent que des ajouts (`137/0` et `10/0` insertions/suppressions). `M04_L6_NOTES.md` est ajouté.
- **Ordre fail-closed vérifié :** l’outil valide l’observation advisor originale, son blob et sa provenance, le blob de sortie stocké et son SHA, puis normalise les refs. Pour Suricata, contexte absent, faits non-listes/vides, faits mal formés, SHA invalide ou `fact_id` dupliqué retournent `advisor_reparse_context_unavailable`; refs transmises discordantes ou faits non liés à une ref observation retournent `advisor_reparse_context_refs_invalid`. Chaque retour précède le parseur et `record_json` (`investigation_advisor_tools.py:3595-3652`). Le flux valide atteint un seul appel au recorder.
- **Chemin valide vérifié au seam réel :** le test `test_reparse_accepts_verified_suricata_context_without_bridge` traverse `ReparsePersistedAdvisorOutputTool.execute` avec dépôts, store et recorder espions. Il vérifie la sortie typée utile, les `fact_refs` résolus dans les faits stockés, les refs source complètes inchangées, SHA de sortie, version et clé d’idempotence, zéro tour Bridge, un seul append et aucune relecture autre que l’observation advisor originale et son blob de sortie (`test_advisor_output_reparse.py:223-272`). Les refus testés ont zéro appel recorder et ne produisent pas d’observation partielle (`:275-334`).
- **Composition vérifiée :** le test de composition par défaut constate l’absence de registration; le test opérateur vérifie registration, capability et disponibilité de `advisor.reparse` (`test_investigation_composition.py:197,280-308`). Le runner standard construit la composition sans activer `operator_mode` (`investigation_jobs.py:73-76`), et la valeur par défaut est `False`; cette limite est documentée dans `M04_L6_NOTES.md:36`. Aucun changement worker n’a été apporté.
- **Contrôles de diff :** `git diff --check df9e091076cbfd3147690f184c5562d626d8ae66..40f1ac271c2180fbc5bad9d034d2dcad75a48ab4` passe sans sortie. Aucun test existant n’a été supprimé ou affaibli dans les deux fichiers modifiés.

### Risque résiduel P3

Avec un contexte complet mais une sortie qui ne peut pas être parsée, le fallback générique de `investigation_advisor_tools.py:3698-3721` ajoute `figure_plan=None` pour Suricata, au lieu d’une forme partielle propre à `suricata_draft`. Le résultat reste marqué non utilisable; ce cas n’invalide pas les refus du contexte absent/incomplet ni les critères L6 vérifiés. Le test du chemin valide ne couvre pas cette forme d’échec. À traiter dans un scope séparé si les consommateurs exigent une forme partielle Suricata dédiée.

Les branches SHA invalide, fait non lié et `fact_id` dupliqué sont présentes dans la garde; les tests couvrent les cas absent, vide, structure incomplète et refs discordantes, sans cas séparé pour chacune de ces trois branches. Les fakes ne prouvent pas le comportement PostgreSQL/MinIO en production. Le runner worker et le câblage réel de déploiement restent hors scope et non vérifiés au runtime.

## Commandes et résultats

Exécutées depuis `backend/`. Les caches et fichiers temporaires ont été dirigés vers `/tmp` afin de préserver l’arbre du dépôt.

```sh
UV_CACHE_DIR=/tmp/autowork-m04-l6-review-uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_advisor_output_reparse.py::test_reparse_accepts_verified_suricata_context_without_bridge tests/test_advisor_output_reparse.py::test_reparse_refuses_suricata_without_persisted_context tests/test_advisor_output_reparse.py::test_reparse_refuses_suricata_with_incomplete_network_facts tests/test_advisor_output_reparse.py::test_reparse_refuses_suricata_context_with_conflicting_references tests/test_investigation_composition.py::test_composition_announces_local_tools_and_only_configured_capabilities tests/test_investigation_composition.py::test_operator_inventory_capabilities_match_the_dispatcher -q --tb=short
# 7 passed in 0.77s

UV_CACHE_DIR=/tmp/autowork-m04-l6-review-uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_advisor_output_reparse.py tests/test_investigation_composition.py -q --tb=short
# 18 passed in 0.93s

UV_CACHE_DIR=/tmp/autowork-m04-l6-review-uv-cache RUFF_CACHE_DIR=/tmp/autowork-m04-l6-review-ruff-cache uv run --no-project --python 3.12 ruff check src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_output_reparse.py tests/test_investigation_composition.py
# All checks passed!

UV_CACHE_DIR=/tmp/autowork-m04-l6-review-uv-cache RUFF_CACHE_DIR=/tmp/autowork-m04-l6-review-ruff-cache uv run --no-project --python 3.12 ruff format --check src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_output_reparse.py tests/test_investigation_composition.py
# 3 files already formatted

UV_CACHE_DIR=/tmp/autowork-m04-l6-review-uv-cache uv run --no-project --python 3.12 mypy --cache-dir=/tmp/autowork-m04-l6-review-mypy-cache src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_output_reparse.py tests/test_investigation_composition.py
# Success: no issues found in 3 source files
```

Les tests ci-dessus sont hors ligne et utilisent des fakes; aucun PostgreSQL, MinIO, provider, Bridge ou service CTI n’a été appelé. La dette mypy globale n’a pas été mesurée.

## Rollback

Aucun changement applicatif ou de données n’est effectué par cette revue. Le lot est entièrement réversible par revert de `40f1ac271c2180fbc5bad9d034d2dcad75a48ab4`; aucun schéma ni donnée persistée ne dépend du changement.

**Résumé par axe :** Standards : zéro violation dure, une heuristique de duplication non bloquante. Spec : zéro critère L6 manquant; le risque le plus élevé est la forme partielle générique sur échec de parsing (P3).
