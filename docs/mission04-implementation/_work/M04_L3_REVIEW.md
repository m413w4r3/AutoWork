# Mission 04 / Lot L3 — revue indépendante

**Verdict : REVISE**

Base : `e120a36f1afa08261fdf0276e0200872973ad8f2`

Lot : `84b8688cf0ea5e75b4c0cc658b29b35d2e2d2246`

La différence contient seulement le nouveau test PostgreSQL et ses notes. Aucun code de production n'a changé.

## Constatations

1. **Moyen — décision analyste absente de l'oracle.** La spécification demande une ligne de décision persistée ([M4_AUDIT_DIAGNOSIS.md:67](/home/nill/perso/work/AutoWork_m04_l3/docs/mission03-architecture/_work/M4_AUDIT_DIAGNOSIS.md:67)). La fixture n'en crée pas et les comptes attendus omettent `operator.decision_recorded` ([test:418](/home/nill/perso/work/AutoWork_m04_l3/backend/tests/integration/test_investigation_audit_export_postgres.py:418)). `opinion.block_decision` est un autre type d'événement. Un reader qui omet toutes les décisions analyste resterait vert.

2. **Moyen — membership de RUN_INDEX vérifiée par sous-ensemble.** Les clés attendues sont comparées avec `<=`, sans égalité de l'ensemble des ressources ([test:467](/home/nill/perso/work/AutoWork_m04_l3/backend/tests/integration/test_investigation_audit_export_postgres.py:467)). Le compte du manifeste est ensuite comparé à la longueur du même index ([test:550](/home/nill/perso/work/AutoWork_m04_l3/backend/tests/integration/test_investigation_audit_export_postgres.py:550)). La disparition d'une référence de réponse résoluble peut donc passer si les événements et la ressource d'observation restent présents.

3. **Moyen — RUN_TRACE ne compte pas les événements/version.** L'oracle vérifie seulement que chaque ID de record distinct apparaît ([test:540](/home/nill/perso/work/AutoWork_m04_l3/backend/tests/integration/test_investigation_audit_export_postgres.py:540)). Les deux versions d'opinion gardent le même ID ([test:745](/home/nill/perso/work/AutoWork_m04_l3/backend/tests/integration/test_investigation_audit_export_postgres.py:745)); omettre une ligne de trace pour l'une d'elles laisse l'ID visible. Le hash du manifeste valide les octets produits, sans oracle indépendant de complétude de la trace.

## Vérifications

- Le seed utilise `SqlAlchemyUnitOfWork` et ses repositories; aucune insertion SQL brute, ligne `SimpleNamespace` ou fausse UOW. Le test appelle `read_investigation_audit_snapshot`; le reader exécute `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` avant le chargement ([reader:175](/home/nill/perso/work/AutoWork_m04_l3/backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:175)). Le résultat PostgreSQL fourni par le parent est **1 passed in 1.25s**; je ne l'ai pas reproduit.
- Les pièges de portée étrangère, référence absente, hash altéré, perturbations de sensibilité et reprise sont présents. Le lecteur d'artifacts fake est acceptable pour ce lot : il ne simule que les octets au hash incorrect et les notes précisent que ce n'est pas une preuve MinIO ([M04_L3_NOTES.md:42](/home/nill/perso/work/AutoWork_m04_l3/docs/mission04-implementation/_work/M04_L3_NOTES.md:42)). Aucun appel provider/MinIO réel, aucun `skip`/`xfail` ni changement production observé.
- `UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_audit_export_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"` : arrêt avant pytest, accès à `/var/run/docker.sock` refusé.
- La première tentative directe `TEST_POSTGRES_ADMIN_DSN='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres' UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache uv run --python 3.12 pytest -m integration tests/integration/test_investigation_audit_export_postgres.py -q --tb=short` avait `CONDA_PREFIX` actif et n'est pas comptée. Relance propre (variables d'activation et chemins Conda retirés; Python 3.12.3) : `env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_EXE -u CONDA_PYTHON_EXE -u CONDA_PROMPT_MODIFIER -u VIRTUAL_ENV PATH='/home/nill/perso/work/AutoWork_m04_l3/backend/.venv/bin:/home/nill/.local/bin:/usr/bin:/bin' PYTHONDONTWRITEBYTECODE=1 TEST_POSTGRES_ADMIN_DSN='postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres' /home/nill/perso/work/AutoWork_m04_l3/backend/.venv/bin/python -m pytest -m integration tests/integration/test_investigation_audit_export_postgres.py -q --tb=short -p no:cacheprovider` : erreur au setup sur `PermissionError` lors de la création du socket, avant connexion ou création de base; le corps du test ne s'exécute pas.
- Ruff, exécuté avec les variables et chemins Conda retirés : `ruff check tests/integration/test_investigation_audit_export_postgres.py` et `ruff format --check tests/integration/test_investigation_audit_export_postgres.py` passent. `git diff --check e120a36f1afa08261fdf0276e0200872973ad8f2..84b8688cf0ea5e75b4c0cc658b29b35d2e2d2246` passe. Les notes de lot restent marquées `BLOCKED_ENV` ([M04_L3_NOTES.md:5](/home/nill/perso/work/AutoWork_m04_l3/docs/mission04-implementation/_work/M04_L3_NOTES.md:5)); cela décrit la tentative de leur auteur, antérieure au résultat fourni par le parent.

## Condition d'approbation

Ajouter un seed et des assertions indépendantes pour `AnalystDecision`, comparer l'ensemble attendu des ressources de `RUN_INDEX`, et vérifier la complétude des lignes de `RUN_TRACE` par type/version. Relancer ensuite le test PostgreSQL dans un environnement Python 3.12 sans Conda actif.
