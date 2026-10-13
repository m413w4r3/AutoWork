# API d'enquête

L'API expose la boucle d'enquête B depuis une synthèse `VERIFIED` liée à un
`subject_id`, un `production_run_id` et un `synthesis_artifact_id` exacts. Le
SHA-256 reçu doit correspondre au contenu canonique relu depuis le stockage
d'artefacts. Si le transfert post-synthèse n'existe pas encore, cette route le
crée depuis l'instantané d'entrée canonique du run puis le persiste avant de
construire le contexte d'article.

## Routes

| Méthode et route | Contrat |
| --- | --- |
| `POST /api/subjects/{subject_id}/investigations` | Corps : `production_run_id`, `synthesis_artifact_id`, `synthesis_sha256`. Retourne l'enquête et le job de génération 1. Un nouvel appel pour la même synthèse vérifiée retourne l'enquête et le job existants. |
| `GET /api/subjects/{subject_id}/investigations` | Liste les enquêtes liées aux synthèses vérifiées du sujet. |
| `GET /api/investigations/{investigation_id}` | Vue de lecture bornée décrite ci-dessous. |
| `POST /api/investigations/{investigation_id}/resume` | Corps : `expected_version`; en-tête requis `Idempotency-Key`. Reprend l'état par CAS et soumet la génération suivante. |
| `POST /api/investigations/{investigation_id}/interrupt` | Corps : `expected_version`; en-tête requis `Idempotency-Key`. Conclut avec `user_interrupt` et demande l'annulation du job actif. |
| `GET /api/investigations/{investigation_id}/envelope` | Lit l'enveloppe G2 approuvée ou une proposition locale non persistée (`approved: false`, version `0`). |
| `POST /api/investigations/{investigation_id}/envelope/approve` | Approuve la première enveloppe G2. Corps : fournisseurs configurés, budgets, acquisition/volume, divulgation, raison et `correlation_id`; en-tête `Idempotency-Key`. |
| `POST /api/investigations/{investigation_id}/envelope/extend` | Étend par CAS l'enveloppe approuvée. Corps : `expected_version` et champs à étendre; raison et `correlation_id`; en-tête `Idempotency-Key`. |
| `POST /api/investigations/{investigation_id}/envelope/revoke` | Révoque les droits externes par une nouvelle version auditée, sans supprimer les droits locaux. Corps : `expected_version`, raison et `correlation_id`; en-tête `Idempotency-Key`. |
| `GET /api/investigations/{investigation_id}/samples` | Lit les associations et leur historique de qualification, sans retourner les octets en quarantaine. |
| `POST /api/investigations/{investigation_id}/samples/{association_id}/qualify` | Décision G3 par CAS : état cible, raison, preuves et `correlation_id`; en-tête `Idempotency-Key`. `confirmed` et `rejected` sont des actions humaines. |
| `POST /api/investigations/{investigation_id}/requests` | Ajoute une demande d'analyse bornée (`text` ≤ 2 000 caractères, cible optionnelle `hash`, `ioc` ou `claim_id`). En-tête `Idempotency-Key`; une enquête arrêtée est remise en file. |

Les commandes `resume` et `interrupt` portent l'acteur provenant de l'`IdentityProvider`.
Une version qui ne correspond plus à `expected_version` retourne `409` avec un
code d'erreur stable. Les relectures d'une même clé d'idempotence renvoient la
commande déjà enregistrée.

La réponse de commande contient `investigation`, `job_id` et
`idempotent_replay`. La vue d'enquête expose l'état, le motif d'arrêt, l'agenda,
les hypothèses, les dernières versions des findings, les observations sans
blob, les associations d'échantillons, le brouillon d'avis courant, le budget
par dimension, les décisions bloquées, et les outils/capacités disponibles ou
désactivés. Elle ne renvoie aucun octet, secret, clé ou contenu brut de
fournisseur.

## Limites d'approbation

La création ne fabrique pas de décision d'approbation. Sans enveloppe persistée,
la proposition par défaut autorise la lecture de l'article et les outils locaux
sur les échantillons déjà en quarantaine. Les fournisseurs externes et
l'acquisition sont refusés au point de dispatch. Le refus est renvoyé comme
`policy_denied`/`needs_review`, avec les alternatives locales et
`request_permission: true`; la boucle peut poursuivre les autres outils.

G2 est une décision humaine durable et auditée. L'approbation accepte les clés
de fournisseurs réellement disponibles (`vt`, `shodan`), un budget par
dimension (`bridge_turns`, `vt_units`, `shodan_credits`, `downloads`, etc.) et
des bornes de divulgation qui ne peuvent pas assouplir la politique de la
synthèse source. L'extension exige la version courante. La révocation ajoute une
version sans fournisseurs externes, sans acquisition et sans envoi externe au
modèle. Les trois commandes prennent l'acteur du fournisseur d'identité, une
raison, un `correlation_id` et `Idempotency-Key`.

G3 conserve l'échantillon à l'état `candidate` après téléchargement ou analyse.
Seul l'analyste peut confirmer ou rejeter l'appartenance; la décision est
append-only et cite les preuves fournies. Une qualification rejouée pour le
même état retourne l'association sans ajouter une seconde décision.

Les demandes d'analyse sont sauvegardées dans l'état de travail, ajoutées au
delta de contexte rétrocompatible `user_requests` et consommées par le prochain
tour Bridge. Une reprise après pause crée un nouveau job; une clé répétée ne
crée ni seconde demande ni second job.

## Réconcilier une invocation ambiguë

`POST /api/investigations/{investigation_id}/invocations/{invocation_id}/reconcile`
termine une invocation `needs_review` après vérification humaine. La requête
contient `expected_version`, `outcome` (`executed_with_result`, `not_executed`
ou `abandon`), `reason` et éventuellement `response_refs` pour
`executed_with_result`. L’acteur vient du fournisseur d’identité et
`Idempotency-Key` protège les reprises. Le CAS, la réservation de budget et la
décision d’audit sont commités ensemble. Une enquête ne peut reprendre qu’après
la réconciliation de ses invocations ambiguës.

## Job durable

Le type `investigation_run` utilise l'agrégat `investigation`, les paramètres
`investigation_id`, `run_generation` et `request_key_sha256`, et une clé
d'idempotence métier `investigation:{id}:run:{generation}`. L'exécuteur conserve
le heartbeat et les retries du système de jobs. Le type reprend le même
attempt après perte du worker : la boucle recharge son état, et les clés
déterministes Bridge et outils rejouent les résultats persistés ou passent une
soumission ambiguë en revue sans renvoyer le tour. Une enquête qui attend une
permission ou une revue termine son job normalement avec son état durable.

## Extension des outils

`build_investigation_composition(...)` crée le registre mémoire et la fabrique
expose `ToolRegistrar = Callable[[ToolRegistry,
InvestigationToolDependencies], None]`. Le registrar reçoit les repositories
et ports pour une enquête. La composition enregistre les outils locaux de B :
`evidence.get`, `article.get_claims`, `sample.get_feature_pack` et
`graph.neighbors`, ainsi que les outils d'analyse/qualification locale des
associations disponibles.

`InvestigationToolDependencies` fournit les ports optionnels `virustotal`,
`shodan`, `rule_workbench`, `local_commands`, `ghidra` et `acquisition`, plus
leurs archives et lecteurs de preuves. Les ports désactivés ou absents
n'annoncent aucun outil. Les capacités Bridge sont la réunion des capacités
par outil des registrars et des capacités locales B, filtrées par les outils
effectivement enregistrés. Voir [investigation_operations.md](investigation_operations.md)
pour les réglages et le runbook.
