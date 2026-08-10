# Backend

API FastAPI et processus Dramatiq du socle CTI. Le package suit une séparation en couches. Les jobs restent canoniques dans PostgreSQL et Dramatiq ne transporte que leurs identifiants.

L'API des éditions mensuelles est décrite dans `../docs/editions.md`. En développement,
l'abstraction d'identité attribue les décisions à `dev-analyst`; elle ne constitue pas une
authentification de production.

```bash
uv sync
uv run uvicorn cti_app.api.main:app --reload
uv run dramatiq cti_app.workers.tasks
uv run python -m cti_app.workers.scheduler
```

## Migrations

PostgreSQL est la base principale. La migration initiale est appliquée automatiquement par le service Compose `migrate`. Hors Compose :

```bash
uv run alembic upgrade head
uv run alembic downgrade base
```

Les tests d'intégration créent une base PostgreSQL temporaire et exigent une URL d'administration dédiée :

```bash
TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres \
  uv run pytest -m integration
```

Les octets des documents et échantillons ne sont jamais stockés dans PostgreSQL. Voir `docs/persistence_and_storage.md` à la racine.

## Passerelle de modèles

Les ports de recherche, extraction, rédaction et critique ainsi que la politique OpenAI/Qwen
sont décrits dans `../docs/model_gateway.md`. Aucun appel n'est effectué au démarrage et les
tests utilisent uniquement des transports simulés. Les clés éventuelles restent dans `.env`
ou un secret manager.
