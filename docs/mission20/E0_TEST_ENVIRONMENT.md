# M20-E0 — Environnement de test hôte : causes racines des « 30 échecs + 48 erreurs »

## Constat
Les missions M17→M19 lançaient `/usr/bin/python3.12 -m pytest …` (interpréteur **système**, pytest 9.1.1, pas de
dépendances du projet) au lieu du venv verrouillé `backend/.venv` (Python 3.12.3, `uv.lock`, 142 paquets, groupes
`dev` + `analysis`). Résultat mesuré sur `8f94e002` : `30 failed, 5919 passed, 17 skipped, 48 errors`.
Avec le venv : `6816 passed, 2 skipped, 32 deselected, 0 failed, 0 error` (≈ 3 min 25).

## Cause racine par groupe (ordre de `.claude/CLAUDE.md` : 1 mauvais interpréteur → 2 dépendance manquante → …)
| Symptôme | Nombre | Cause racine | Module manquant dans l'interpréteur système |
|---|---|---|---|
| `ModuleNotFoundError: pefile` à la collecte (test_analysis_worker, workflow_emulator/*, test_workers_tasks, test_static_analysis, test_investigation_*…) | 48 erreurs | mauvais interpréteur → dépendance déclarée (`pefile==2024.8.26`) absente | `pefile` |
| `YaraExecutionStatus.ENGINE_CRASHED` (test_yara_runner ×3), rule_workbench ×9, investigation_rule_tools ×3, signature_bridge ×2, rule_corpus_import ×2 (`UNTESTED` au lieu de `SYNTHETIC_ONLY`), investigation_advisor_tools ×4 | ≈ 21 | `yara-python==4.5.0` (groupe `analysis`) absent : le worker YARA isolé plante, les règles restent non testées | `yara` |
| `python-multipart must be installed` / `invalid_rule_corpus_multipart` (rule_corpus_import, collection_api) | 2 | dépendance déclarée `python-multipart` absente | `multipart` |
| `offline_simulated != worker_runtime_registry` (supervised_first_run_preflight), logging (workers), archive_commands, advisor_checkpoint | ≈ 7 | import transitif de `pefile` échoue → repli/erreur d'import ; passent tous dans le venv | `pefile` |
Les effectifs par ligne sont approximatifs (journal d'origine : `/tmp/m20-e0-oldsuite.log`, non versionné) ; le total 30 + 48 est exact.
Aucun de ces échecs n'était un défaut de code applicatif ni de fixture ; aucun test n'a été modifié, supprimé ni ignoré.

## Recette officielle
```
make test-backend PYTEST_ARGS="-q --tb=short"          # suite hors integration, venv verrouillé
make lint-backend && make typecheck-backend             # ruff check + format --check, mypy src tests
make test-integration                                   # PostgreSQL jetable (Docker)
```
`make test-backend` préfixe automatiquement le PATH avec `~/.local/share/autowork-tools/bin` (Typst 0.15.1 épinglé, `infra/typst.lock`) quand il existe : le Typst snap ne lit pas les répertoires `/tmp` de pytest (60 échecs `input file not found` sinon, mesurés).
Sans cette cible :
`export TYPST_BINARY=$HOME/.local/share/autowork-tools/typst FONT_BUNDLE_ROOT=$PWD/chpTypst TMPDIR=$HOME/typst-tmp`
(voir mémoire « Typst local recipe » : le typst snap est cassé).
Sans réseau : `uv sync --locked --offline` fonctionne à partir du cache uv (`~/.cache/uv`).

## Garde-fou
`backend/tests/conftest.py::pytest_configure` interrompt la session avec le diagnostic ci-dessus si Python ≠ 3.12 ou si
`pefile`, `yara`, `multipart`, `elftools` sont introuvables.

## Autres échecs de qualité corrigés dans E0
`make lint-backend` (7 fichiers à reformater) et `make typecheck-backend` (87 erreurs mypy dans 13 fichiers, régressions de
typage M17→M19) : corrigés par la branche `m20/e0b-lint-types` (aucun changement de comportement).

## Non exécuté ici
`make test-integration` (nécessite Docker/PostgreSQL), `make test-frontend`, tests d'image `ANALYSIS_WORKER_IMAGE_TEST=1`.
