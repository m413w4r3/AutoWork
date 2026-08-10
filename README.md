# CTI Bulletin

Application interne de production de bulletins CTI mensuels. Elle fournit l'environnement
local, la persistance canonique, le stockage de blobs, les jobs asynchrones observables et le
premier workflow métier de création et de suivi des éditions. La découverte ponctuelle crée
des candidats sourcés et explicitement non vérifiés via la passerelle LLM typée ; aucun
connecteur de collecte ou de chasse CTI n'est encore activé.

## Prérequis

- Docker avec Docker Compose v2 ;
- pour travailler hors conteneur : Python 3.12+, `uv`, Node.js 22+ et `pnpm` 10+.

## Démarrage local

```bash
cp .env.example .env  # facultatif : Compose possède des valeurs locales par défaut
make dev
```

Services exposés :

| Service | URL locale |
| --- | --- |
| Frontend — éditions | <http://localhost:5173/editions> |
| API | <http://localhost:8000> |
| Live | <http://localhost:8000/api/health/live> |
| Ready | <http://localhost:8000/api/health/ready> |
| MinIO | <http://localhost:9001> |

Les ports hôtes peuvent être adaptés dans `.env` avec `BACKEND_PORT`, `FRONTEND_PORT`, `MINIO_API_PORT` et `MINIO_CONSOLE_PORT`. Les ports internes et le proxy entre services ne changent pas.

`GET /api/health/live` ne consulte aucune dépendance. `GET /api/health/ready` retourne HTTP 200 si PostgreSQL, Redis et le bucket MinIO répondent, sinon HTTP 503 avec le détail de chaque dépendance.

Le service ponctuel `migrate` applique les migrations Alembic avant le démarrage du backend. Les corps de documents et échantillons restent dans MinIO ; PostgreSQL ne conserve que leurs métadonnées et références.

Arrêt sans suppression des volumes nommés :

```bash
make stop
```

## Développement hors conteneur

```bash
cd backend && uv sync
cd ../frontend && pnpm install --frozen-lockfile
```

Les commandes racine sont `make test`, `make test-integration`, `make lint`, `make typecheck` et `make format`. Aucun test ne contacte une API externe. Les tests d'intégration utilisent une base PostgreSQL temporaire indiquée par `TEST_POSTGRES_ADMIN_DSN`.

## Organisation

- `backend/` : API FastAPI, ports d'infrastructure et worker Dramatiq ;
- `frontend/` : React, TypeScript strict, Vite et TanStack Query ;
- `infra/` : images de développement et notes Compose ;
- `docs/adr/` : décisions d'architecture ;
- `scripts/` : contrôles de développement non destructifs.

L'état de production est canonique dans PostgreSQL, les fichiers versionnés et les evidence packs. Les workspaces matérialisés, conversations LLM, files Redis et réponses de services externes ne sont jamais des sources de vérité. La stratégie détaillée est décrite dans [docs/persistence_and_storage.md](docs/persistence_and_storage.md), [docs/async_jobs.md](docs/async_jobs.md), [docs/editions.md](docs/editions.md), [docs/model_gateway.md](docs/model_gateway.md) et [docs/discovery.md](docs/discovery.md).
