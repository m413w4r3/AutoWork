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

Les octets des documents et échantillons ne sont jamais stockés dans PostgreSQL.

## Collecte sûre et preuves

La collecte ne concerne que les sources d'un `Subject` sélectionné et démarre exclusivement par
`POST /api/subjects/{id}/collection`. Le job `source.collect` est idempotent et reprend les sources
interrompues sans recréer les objets déjà terminés.

- `source_collections` est la projection mutable par source sélectionnée : état courant, rôle proposé
  et niveau de preuve. Le rôle ne devient `verified` qu'avec une preuve `deterministic:*` ou une
  décision `human:*` journalisée.
- `collection_attempts` est append-only : URL demandée/finale, redirections, UTC, statut, en-têtes
  autorisés, MIME déclaré/détecté, taille, SHA-256, job, snapshot de configuration, résultat et motif.
- `source_documents` conserve chaque observation d'URL et référence un blob immuable `source-raw`.
  Deux URL aux octets identiques gardent deux observations mais réutilisent le même blob.
- `derived_artifacts` référence séparément le texte `source-text`, sa version de parseur et les
  métadonnées de publication.
- `claims` et `indicators` sont append-only et référencent le document source ainsi que les offsets
  du passage. Les IOC gardent valeurs originale et normalisée.
- `human_decisions` porte les validations, corrections et rejets sans modifier l'extraction initiale.

La migration additive et réversible `0007_source_collection_and_evidence` crée ces tables sans
modifier les migrations antérieures.

Le collecteur accepte uniquement HTTP(S), refuse credentials, localhost, metadata cloud et plages
privées, loopback, link-local, multicast ou réservées IPv4/IPv6. Il contrôle deux réponses DNS puis
se connecte à l'IP approuvée en conservant la validation TLS du nom d'hôte. Chaque redirection est
revalidée. Le temps total, les octets réseau, les octets décompressés, le ratio de décompression et
le nombre de redirections sont bornés. Le type est détecté depuis les octets (HTML/PDF MVP), sans
JavaScript, macro, script ni exécution du fichier.

Les limites se règlent avec `COLLECTION_MAX_REDIRECTS`, `COLLECTION_TIMEOUT_SECONDS`,
`COLLECTION_MAX_DOWNLOAD_BYTES`, `COLLECTION_MAX_EXPANDED_BYTES` et
`COLLECTION_MAX_DECOMPRESSION_RATIO`. `COLLECTION_ALLOWED_DOMAINS` et
`COLLECTION_BLOCKED_DOMAINS` acceptent des domaines séparés par des virgules ; la liste autorisée
vide n'ajoute aucune restriction, tandis que la liste bloquée reste prioritaire. Les valeurs locales
non secrètes sont dans `.env.example`.

## Passerelle de modèles

Les ports de recherche, extraction, rédaction et critique ainsi que la politique OpenAI/Qwen
sont décrits dans `../docs/model_gateway.md`. Aucun appel n'est effectué au démarrage et les
tests utilisent uniquement des transports simulés. Les clés éventuelles restent dans `.env`
ou un secret manager.
