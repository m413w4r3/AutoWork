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

ChatGPT Bridge est exploité comme service indépendant. AutoWork peut l'utiliser
via `OPENAI_BRIDGE_BASE_URL`, au même titre que d'autres transports de modèles
configurés par le `ModelGateway`.

Les ports hôtes peuvent être adaptés dans `.env` avec `BACKEND_PORT`, `FRONTEND_PORT`, `MINIO_API_PORT` et `MINIO_CONSOLE_PORT`. Les ports internes et le proxy entre services ne changent pas.

`GET /api/health/live` ne consulte aucune dépendance. `GET /api/health/ready` retourne HTTP 200 si PostgreSQL, Redis et le bucket MinIO répondent, sinon HTTP 503 avec le détail de chaque dépendance.

Le service ponctuel `migrate` applique les migrations Alembic avant le démarrage du backend. Les corps de documents et échantillons restent dans MinIO ; PostgreSQL ne conserve que leurs métadonnées et références.

Arrêt sans suppression des volumes nommés :

```bash
make stop
```

`make stop` reste un alias de `make down`. Les commandes d'exploitation sont
`make up`, `make down`, `make status` et `make logs`. `make down` conserve
toujours les volumes nommés.

`make model-run-diagnostics RUN_ID=<uuid>` affiche uniquement les métadonnées sûres d'une
sortie modèle et donne la commande d'export explicite de l'artefact brut.

## Développement local

```bash
make setup
```

`make setup` vérifie les prérequis, synchronise le backend avec `backend/uv.lock`
en installant explicitement les groupes `dev` et `analysis`, puis installe les
dépendances frontend. Les commandes utiles pour une action ciblée sont :

```bash
make doctor
make backend-sync
make frontend-sync
make reset-backend-env
```

Pour mettre volontairement à jour le lockfile après une modification de
dépendances, utiliser `make backend-lock`. `backend-sync` utilise `--locked` et
échoue si `backend/pyproject.toml` et `backend/uv.lock` sont désynchronisés.

Les commandes racine sont `make help`, `make test`, `make test-integration`,
`make test-all`, `make lint`, `make typecheck` et `make format`. Aucun test ne
contacte une API externe.

`make test` lance les tests backend ordinaires et frontend, sans PostgreSQL
d'intégration. `make test-backend` exclut `tests/integration`, tandis que
`make test-frontend` lance les tests frontend. `make test-integration` ne
collecte que `backend/tests/integration` avec un PostgreSQL dédié ;
`make test-all` enchaîne les deux suites.

Exemples de tests ciblés :

```bash
make test-backend \
  PYTEST_ARGS="tests/test_static_analysis.py -q"

make test-integration \
  INTEGRATION_TEST_PATH=tests/integration/production \
  INTEGRATION_PYTEST_ARGS="-x -vv"
```

Les tests d'intégration n'utilisent pas `POSTGRES_DSN`, réservé à la base
applicative. Ils utilisent un PostgreSQL dédié et respectent
`TEST_POSTGRES_ADMIN_DSN` s'il est fourni ; sinon `make test-integration` démarre
et supprime le service temporaire dont il est propriétaire, sans collecter les
tests unitaires.

`make help` affiche les commandes Make documentées et leurs descriptions.

Le service PostgreSQL éphémère `postgres-test` est indépendant de la DB
applicative et est supprimé même en cas d'échec lorsque la commande l'a démarré.
`POSTGRES_DSN` désigne la DB applicative ; `TEST_POSTGRES_ADMIN_DSN` est réservé
à la création et suppression des bases temporaires de pytest. Après une
modification volontaire de l'état local des migrations, `make up-clean` recrée
les volumes applicatifs ; cette commande est destructive.

## Développement automatisé avec MetaHarness

AutoWork peut être utilisé comme dépôt cible de MetaHarness.

MetaHarness reste externe au produit et n'est jamais une source de vérité
applicative. Il résout le contexte, produit et fait approuver les contrats
d'implémentation, exécute les workers dans un worktree isolé, lance les
validations déterministes, réalise la revue sémantique puis contrôle le commit
et la publication.

Dans un run MetaHarness :

- `AGENTS.md` et les `AGENTS.md` de composants restent les règles
  d'architecture du dépôt ;
- `scripts/ctx/ctx.py` est utilisé par la phase de contextualisation/planning,
  pas par les workers d'implémentation ;
- les workers reçoivent un scope borné et ne doivent pas refaire la discovery ;
- MetaHarness possède les checks finaux, Git, le commit et la publication.

Pour l'orchestration, les profils modèles et l'exploitation des runs, voir le
dépôt MetaHarness ; ce README reste la documentation d'AutoWork lui-même.

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
