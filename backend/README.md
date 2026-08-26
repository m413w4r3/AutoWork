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

PostgreSQL est la base principale. La migration initiale est appliquée automatiquement par le service Compose `migrate`. La chaîne post-baseline est volontairement réécrivable tant que le projet est en développement local : une base existante doit être recréée après une réécriture. Hors Compose :

```bash
uv run alembic upgrade head
uv run alembic downgrade base
```

Le workflow local recommandé lance automatiquement un PostgreSQL éphémère séparé :

```bash
make test-integration
```

Équivalent manuel :

```bash
docker compose --profile integration-test up -d --wait postgres-test
cd backend
TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
  uv run pytest -m integration
docker compose --profile integration-test rm -sf postgres-test
```

Les tests créent une base temporaire dans ce serveur. `POSTGRES_DSN` est la connexion à la DB applicative ; `TEST_POSTGRES_ADMIN_DSN` est la connexion ADMIN réservée à pytest pour `CREATE DATABASE` / `DROP DATABASE`. Ces deux URLs ne sont pas interchangeables. Un DSN explicite peut remplacer le service local : `TEST_POSTGRES_ADMIN_DSN=<dsn> make test-integration`.

Après réécriture de l'historique des migrations, `make up-clean` est nécessaire pour recréer la DB applicative. Cette commande est destructive : elle supprime les volumes PostgreSQL, Redis, MinIO et workspaces, mais conserve volontairement `bridge_data`. Sauvegarder les données locales importantes avant de l'utiliser.

Les octets des documents et échantillons ne sont jamais stockés dans PostgreSQL.

## Collecte sûre et preuves

La collecte ne concerne que les sources d'un `Subject` sélectionné et démarre exclusivement par
`POST /api/subjects/{id}/collection`. Le job `source.collect` est idempotent, traite chaque
publication séparément et s'arrête à l'état `archived`. **La collecte HTTP n'appelle aucun
modèle** : parsing, Qwen, OpenAI, ChatGPT, claims, IOC et artefacts dérivés appartiennent à une
future étape Analyse explicite.

- `source_collections` est la projection mutable par source sélectionnée : état courant, bail de
  téléchargement, références brute/décodée, rôle proposé et niveau de preuve. Le rôle ne devient
  `verified` qu'avec une preuve `deterministic:*` ou une décision `human:*` journalisée ; PostgreSQL
  applique aussi cet invariant.
- `collection_attempts` est append-only : URL demandée/finale, redirections, UTC, statut, en-têtes
  autorisés, MIME déclaré/détecté, tailles et SHA-256 encodés/décodés, job, snapshot de politique,
  résultat et motif.
- `collection_policy_snapshots` conserve la configuration canonique complète utilisée par chaque
  tentative, et pas seulement son hash.
- `source_documents` conserve chaque observation d'URL, son nom logique et ses métadonnées, ainsi
  que les références explicites vers les octets HTTP encodés immuables `source-raw` et le contenu
  `source-decoded` après `Content-Encoding`.
  Deux gzip distincts peuvent donc partager le blob décodé sans partager le blob brut.
- `derived_artifacts` référence séparément le texte `source-text`, sa version de parseur et les
  métadonnées de publication.
- `claims` et `indicators` sont append-only et référencent le document source ainsi que les offsets
  du passage. Les claims Qwen gardent aussi segment, offsets locaux/globaux et ModelRun. Les IOC
  gardent valeurs originale et normalisée.
- `rejected_model_proposals` journalise séparément chaque proposition Qwen invalide sans supprimer
  les propositions valides du même segment.
- `human_decisions` porte les validations, corrections et rejets sans modifier l'extraction initiale.

Le schéma de base unique (`0001_baseline`) définit une sémantique complète pour les documents
source et leurs métadonnées de collecte, protégée par des contraintes PostgreSQL.

## Brèves et evidence packs

Les tables `brief_evidence_packs` et `brief_drafts` sont append-only et protégées par des
triggers PostgreSQL définis dans le schéma de base. Les packs JSON immuables sont adressés par
contenu dans le bucket logique `brief-evidence-packs`; PostgreSQL conserve leur version et la
référence de blob. Les brouillons versionnés sont invalidés par lecture dès qu’un pack plus
récent existe. Le parcours, la politique Qwen/OpenAI, les contrôles QA et l’export sont
décrits dans `../docs/brief_workflow.md`.

Le collecteur accepte uniquement HTTP(S), refuse credentials, localhost, metadata cloud et plages
privées, loopback, link-local, multicast ou réservées IPv4/IPv6. Il contrôle deux réponses DNS puis
se connecte à l'IP approuvée en conservant la validation TLS du nom d'hôte. Chaque redirection est
revalidée. Le temps total, les octets réseau, les octets décompressés, le ratio de décompression et
le nombre de redirections sont bornés. Le type est détecté depuis les octets (HTML, PDF, texte et
JSON), sans
JavaScript, macro, script ni exécution du fichier.

Les limites se règlent avec `COLLECTION_MAX_REDIRECTS`, `COLLECTION_TIMEOUT_SECONDS`,
`COLLECTION_MAX_DOWNLOAD_BYTES`, `COLLECTION_MAX_EXPANDED_BYTES` et
`COLLECTION_MAX_DECOMPRESSION_RATIO`. `COLLECTION_ALLOWED_DOMAINS` et
`COLLECTION_BLOCKED_DOMAINS` acceptent des domaines séparés par des virgules ; la liste autorisée
vide n'ajoute aucune restriction, tandis que la liste bloquée reste prioritaire. Les valeurs locales
non secrètes sont dans `.env.example`.

Le bail est réglé par `COLLECTION_FETCH_LEASE_SECONDS` et reste toujours supérieur à la durée HTTP
totale. Le PDF est isolé dans un processus interruptible et borné par `PDF_MAX_DOCUMENT_BYTES`,
`PDF_MAX_PAGES`, `PDF_PARSE_TIMEOUT_SECONDS`, `PDF_MAX_TEXT_CHARS` et
`PDF_MAX_METADATA_LENGTH`. Qwen reçoit des segments déterministes configurés avec
`QWEN_CHUNK_MAX_CHARS` et `QWEN_CHUNK_OVERLAP_CHARS`.

## Passerelle de modèles

Les ports de recherche, extraction, rédaction et critique ainsi que la politique OpenAI/Qwen
sont décrits dans `../docs/model_gateway.md`. Aucun appel n'est effectué au démarrage et les
tests utilisent uniquement des transports simulés. Les clés éventuelles restent dans `.env`
ou un secret manager.
