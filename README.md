# CTI Bulletin

Application interne de production de bulletins CTI mensuels. Elle fournit l'environnement
local, la persistance canonique, le stockage de blobs, les jobs asynchrones observables et le
premier workflow métier de création et de suivi des éditions. La découverte ponctuelle crée
des candidats sourcés et explicitement non vérifiés via la passerelle LLM typée. Le board
éditorial exige une décision humaine pour créer une brève ou un article principal. Un analyste
peut ensuite lancer explicitement un job de collecte sûre HTML/PDF, examiner les preuves et mener
une brève jusqu’à son approbation et son export Markdown depuis un evidence pack gelé. Aucune
chasse, attribution automatique, exécution d'échantillon ou génération d’article principal n'est
encore activée.

## Prérequis

- Docker avec Docker Compose v2 ;
- pour travailler hors conteneur : Python 3.12+, `uv`, Node.js 22+ et `pnpm` 10+.

## Démarrage local

```bash
cp .env.example .env  # facultatif : Compose possède des valeurs locales par défaut
make up
```

Services exposés :

| Service | URL locale |
| --- | --- |
| Frontend — éditions | <http://localhost:5173/editions> |
| API | <http://localhost:8000> |
| Live | <http://localhost:8000/api/health/live> |
| Ready | <http://localhost:8000/api/health/ready> |
| MinIO | <http://localhost:9001> |
| Bridge ChatGPT (hôte uniquement) | <http://127.0.0.1:8001/health> |

Les ports hôtes peuvent être adaptés dans `.env` avec `BACKEND_PORT`, `FRONTEND_PORT`, `MINIO_API_PORT` et `MINIO_CONSOLE_PORT`. Les ports internes et le proxy entre services ne changent pas.

`GET /api/health/live` ne consulte aucune dépendance. `GET /api/health/ready` retourne HTTP 200 si PostgreSQL, Redis et le bucket MinIO répondent, sinon HTTP 503 avec le détail de chaque dépendance.

Le service ponctuel `migrate` applique les migrations Alembic avant le démarrage du backend. Les corps de documents et échantillons restent dans MinIO ; PostgreSQL ne conserve que leurs métadonnées et références.

Arrêt sans suppression des volumes nommés :

```bash
make stop
```

`make stop` reste un alias de `make down`. Les commandes d’exploitation sont
`make up`, `make down`, `make status`, `make logs`, `make bridge-status`,
`make bridge-logs` et `make restart-bridge`. `make down` conserve toujours les
volumes nommés, dont le registre SQLite `bridge_data`.

`make model-run-diagnostics RUN_ID=<uuid>` affiche uniquement les métadonnées sûres d'une
sortie modèle et donne la commande d'export explicite de l'artefact brut.

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

L'état de production est canonique dans PostgreSQL, les fichiers versionnés et les evidence packs. Les workspaces matérialisés, conversations LLM, files Redis et réponses de services externes ne sont jamais des sources de vérité. Les représentations HTTP encodée et décodée sont archivées séparément par SHA-256 dans MinIO ; les observations d'URL, textes dérivés versionnés, claims, IOC et décisions humaines restent des objets canoniques distincts. La collecte utilise des baux récupérables, épingle une adresse DNS publique contrôlée à chaque connexion et redirection, borne durée, octets, décompression et parsing PDF, puis segmente l'extraction Qwen sans exécuter le contenu distant. Voir [backend/README.md](backend/README.md) pour les tables et limites configurables.

La stratégie et les invariants de l'incrément sont détaillés dans
[docs/source_collection_and_evidence.md](docs/source_collection_and_evidence.md) et
[docs/brief_workflow.md](docs/brief_workflow.md). Les conversations persistantes,
leur politique `fresh`/`continue` et leurs limites sont documentées dans
[docs/model_conversations.md](docs/model_conversations.md).
