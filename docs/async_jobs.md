# Infrastructure des jobs asynchrones

## Frontières et état canonique

La table PostgreSQL `jobs` porte l’état canonique. Redis/Dramatiq ne transporte que l’UUID du
job à exécuter ; perdre ou rejouer un message ne remplace donc jamais l’état en base. FastAPI
n’utilise pas `BackgroundTasks` pour les traitements longs.

`JobDispatcher` est le port applicatif. `DramatiqJobDispatcher` publie dans Redis en
développement et en production. `SynchronousJobDispatcher` exécute exactement le même
`JobExecutor` en ligne dans les tests, sans Redis ni service externe.

## Table `jobs`

| Groupe | Colonnes | Rôle |
| --- | --- | --- |
| Identité | `id`, `kind`, `aggregate_type`, `aggregate_id` | rattachement typé à un agrégat |
| État | `status`, `progress_current`, `progress_total`, `user_message` | suivi utilisateur |
| Idempotence | `idempotency_key` unique | refus atomique des soumissions dupliquées |
| Exécution | `attempt`, `max_attempts`, `next_retry_at` | retries bornés et rejouables |
| Temps | `started_at`, `finished_at`, `heartbeat_at` | observabilité et reprise |
| Erreur | `error_code`, `error_message` | code stable et message public nettoyé |
| Contexte | `correlation_id`, `input_parameters`, `output_reference` | paramètres validés et résultat référencé |
| Annulation | `cancellation_requested_at` | signal coopératif lu entre deux étapes |
| Audit technique | `created_at`, `updated_at` | ordre et flux SSE |

La table `job_events` journalise chaque transition avec les statuts avant/après, l'acteur, le
`correlation_id` et une charge utile technique bornée. Le repository n'expose que `append` et
un trigger PostgreSQL rejette toute mise à jour ou suppression.

Les statuts autorisés sont `queued`, `running`, `waiting_human`, `succeeded`, `failed` et
`cancelled`. La migration ajoute des contraintes de progression, un plafond de tentatives et
des index de reprise. Son downgrade retire uniquement la table et ses index.

## Exécution, erreurs et reprise

Un handler est enregistré avec un modèle Pydantic strict. La validation a lieu à la
soumission puis avant l’exécution. Le handler ne reçoit ni shell ni commande arbitraire : il
reçoit ses paramètres validés et un contexte borné pour publier progression et heartbeat.

Seule une `JobHandlerError` explicitement marquée transitoire déclenche un retry exponentiel.
Dramatiq a ses retries automatiques désactivés : le calcul et `next_retry_at` restent visibles
en base. Une exception inattendue devient `internal_error` avec un message générique ; son
texte brut n’est jamais enregistré ni exposé par l’API.

L’annulation d’un job en attente est immédiate. Pour un job en cours, l’API pose un signal que
le contexte vérifie à chaque heartbeat/progression. Le processus `job-recovery` demande
périodiquement au worker de reprendre les jobs `running` dont le heartbeat a expiré. La reprise
est idempotente et respecte `max_attempts`.

## API et démonstration

- `POST /api/jobs` soumet un job et retourne HTTP 409 pour une clé déjà utilisée ;
- `GET /api/jobs/{id}` lit l’état canonique ;
- `POST /api/jobs/{id}/retry` relance explicitement un échec relançable ;
- `POST /api/jobs/{id}/cancel` demande une annulation coopérative ;
- `GET /api/jobs/{id}/history` lit le journal append-only des transitions ;
- `GET /api/jobs/metrics/operational` expose volumes par statut, retries en attente, durée
  moyenne et taux d'échec ;
- `GET /api/jobs/{id}/events` diffuse des snapshots SSE et se ferme sur un état terminal.

Les opérations initiées par HTTP reçoivent l'acteur local `dev-analyst`. Les opérations du
worker et de reprise sont attribuées respectivement à `system:worker` et `system:recovery`.
Les métriques sont calculées depuis PostgreSQL et n'introduisent pas de second état canonique.

Le kind `demo.deterministic` accepte `steps` et `label`, publie une progression déterministe
et retourne une référence `demo://`. Il ne contacte aucun service et n’exécute aucun fichier.
Le composant React `JobStatusCard` consomme le SSE et conserve un polling HTTP comme fallback.
