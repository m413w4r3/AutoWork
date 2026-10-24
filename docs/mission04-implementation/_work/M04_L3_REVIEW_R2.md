# Mission 04 / L3 — contre-revue R2

**VERDICT: APPROVED** — aucun finding bloquant sur le correctif L3.

## Findings

- **P3, suivi documentaire** — `docs/mission04-implementation/_work/M04_L3_NOTES.md:5-14,174-185` indique encore que le rerun PostgreSQL reste requis et non exécuté. Le parent rapporte un succès sur le HEAD `600a317f406ad2983148162c80c6adc5e6b7d1d2` (**1 passed in 1.02s**). Réconcilier le journal de validation avant archivage.

Les trois findings de R1 sont fermés. La décision analyste est semée par `uow.analyst_decisions.append`, relue indépendamment depuis `AnalystDecisionRow`, puis contrôlée par type/cible/version nulle dans les événements, la trace, l’index et le manifeste. L’index est comparé à l’ensemble exact dérivé des lignes interrogées, dont les artefacts de synthèse et de publication; le compte manifeste vient de cet ensemble. Les événements attendus comptent par type/ID/version. La trace vérifie ses comptes par type/ID et ses deux versions d’opinion; les perturbations d’omission décision/opinion échouent dans l’oracle principal.

Le test passe par le vrai UOW et le vrai lecteur. Celui-ci exécute `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` avant les lectures. La capture SQL vérifie qu’assemblage, écriture et reprise ne relisent pas PostgreSQL. Les quatre fichiers de reprise sont comparés octet par octet. Je n’ai trouvé aucune assertion antérieure affaiblie, aucun `skip`/`xfail`, INSERT SQL brut, fake UOW, ou changement de production dans le delta R1→R2. IDs et références sont générés puis comparés aux lignes persistées; les temps sont déterministes et les comparaisons ne dépendent pas de l’ordre global des tests.

## Vérifications

- `git diff --check e120a36f1afa08261fdf0276e0200872973ad8f2..600a317f406ad2983148162c80c6adc5e6b7d1d2` — réussi.
- `ruff check backend/tests/integration/test_investigation_audit_export_postgres.py` — réussi.
- `ruff format --check backend/tests/integration/test_investigation_audit_export_postgres.py` — réussi.
- Parent: `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_audit_export_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"` — **1 passed in 1.02s**, après correction rapportée.
- Même test tenté localement avec `TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres uv run --python 3.12 pytest -m integration tests/integration/test_investigation_audit_export_postgres.py -q --tb=short` — bloqué avant création de la base jetable par `PermissionError: [Errno 1] Operation not permitted` à l’ouverture du socket; ce n’est pas un échec d’assertion.

## Limites

Le faux artifact reader vérifie le traitement d’un hash mismatch et l’absence de fuite, pas l’intégration MinIO réelle; cette limite est clairement documentée dans `M04_L3_NOTES.md:52-59`. Aucun comportement de provider ni de concurrence MVCC sous écritures simultanées n’est prouvé par ce test. L’approbation suppose le succès PostgreSQL parental rapporté au SHA courant.
