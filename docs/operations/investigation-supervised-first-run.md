# Runbook — premier run CTI supervisé

## État de départ

**NO_GO au 15 octobre 2026.** Le code et les scénarios offline couvrent les
parcours décrits plus bas, mais Bridge, VirusTotal via proxy et Shodan restent
`non vérifiés en live`. Ne démarrez le run qu’après approbation et exécution
des smokes live, identité opérateur décidée, et prévol réellement sûr. La
commande de prévol dans la stack est implémentée et sa ligne Make est vérifiée;
la connexion à la stack réelle reste à valider après son démarrage : voir
« Prévol ».

## 1. Préparer `.env`

Copiez [.env.example](../../.env.example) dans `.env` local, puis renseignez
uniquement les paramètres nécessaires au périmètre approuvé. Ne copiez aucune
clé dans ce document, les commandes ou les journaux.

- **Bridge :** `OPENAI_BRIDGE_BASE_URL` doit être joignable depuis les
  conteneurs; `OPENAI_BRIDGE_API_KEY` est le secret du Bridge. La cible
  `make up` inclut `compose.models.yaml` si le réseau `metaharness-models`
  existe.
- **VirusTotal :** `VIRUSTOTAL_PROXY_URL` et, pour les endpoints qui la
  demandent, `VIRUSTOTAL_API_KEY`. Activez seulement les opérations permises
  par G2 parmi `VIRUSTOTAL_FILE_REPORT_ENABLED`,
  `VIRUSTOTAL_OBJECT_RELATIONSHIPS_ENABLED`,
  `VIRUSTOTAL_INTELLIGENCE_SEARCH_ENABLED`,
  `VIRUSTOTAL_BEHAVIOUR_REPORTS_ENABLED` et
  `VIRUSTOTAL_FILE_DOWNLOAD_ENABLED`. Le client Retrohunt utilise l’API VT v3
  via le proxy; **`VIRUSTOTAL_RETROHUNT_ENABLED=false`** pour ce premier run.
  Aucun serveur vtUI n’est requis.
- **Shodan :** `SHODAN_API_KEY` et les seules lectures approuvées parmi
  `SHODAN_HOST_ENABLED`, `SHODAN_SEARCH_ENABLED`, `SHODAN_DOMAIN_ENABLED` et
  `SHODAN_COUNT_ENABLED`. Les opérations exposées sont passives; aucun scan
  actif.
- **Identité :** `OPERATOR_API_TOKEN` est optionnel. S’il est défini, le code
  exige au moins 32 caractères ASCII sans espaces. Le mode local par défaut
  est `LocalIdentityProvider/dev-analyst`, non vérifié. Un Bearer authentifie
  l’accès API; il ne constitue pas à lui seul une approbation G2/G3, une
  décision éditoriale ou une décision de publication.
- **Limites :** fixez des plafonds explicites selon l’enveloppe G2 :
  `INVESTIGATION_MAX_CYCLES`, `INVESTIGATION_MAX_PIVOT_RUNS`,
  `INVESTIGATION_MAX_HITS_ACQUIRED`, `INVESTIGATION_MAX_NEW_SAMPLES`,
  `INVESTIGATION_MAX_VT_READ_UNITS`, `INVESTIGATION_MAX_SHODAN_CREDITS`,
  `INVESTIGATION_MAX_TOOL_SECONDS`, `INVESTIGATION_MAX_CONTEXT_BYTES` et
  `INVESTIGATION_NO_PROGRESS_LIMIT`. Dans `.env.example`, un budget à zéro
  signifie aucune allocation.

## 2. Reset initial autorisé

Avant toute approbation G2 et tout appel d’outil, vérifiez la cible locale
attendue : projet Compose `cti-bulletin`, Docker local, PostgreSQL Compose,
volumes possédés par ce projet et workspaces sous `var/`. Le script affiche
les comptes et cibles puis refuse une cible distante ou inconnue.

Exécutez **une seule fois, avant le premier run** :

```sh
make up-clean CONFIRM_INITIAL_RESET=yes
```

Cette cible enchaîne `clean` puis `up` et efface les volumes Postgres, Redis,
MinIO ainsi que les chemins locaux prévalidés. C’est une action destructive;
la cible Make ne transmet pas `--dry-run` (l’option existe sur le script seul).
Le script refuse le reset dès qu’un marqueur de run existe — enveloppe G2
persistée ou invocation d’outil — même avec la confirmation, et revérifie ce
marqueur avant de retirer les volumes. Après le début du run : **aucun**
`make clean`, `make up-clean`, `down -v`, `DROP`, `stamp` ou autre reset
destructeur. Pour reconstruire les conteneurs, utilisez `make up`.

La garde persistante porte sur le début du run; elle ne mémorise pas un reset
antérieur tant que le marqueur de run est absent. La règle « une seule fois »
avant le premier run est donc aussi une consigne opératoire.

## 3. Démarrer et faire le prévol

Après le reset, `make up` démarre la stack en arrière-plan, avec build et attente
des healthchecks. Cette commande ne réinitialise pas les volumes.

Le prévol est une inspection locale en lecture seule : il n’appelle pas les
fournisseurs, ne lit pas de corps d’échantillon et ne migre ni n’écrit la base.
Il accepte les arguments `--subject-id`, `--production-run-id`,
`--synthesis-artifact-id`, `--version`, `--sha256`, `--tlp` et `--format`
(`text`, `json` ou `markdown`; défaut `markdown`). Fournissez les six valeurs
canoniques ensemble, sans sélectionner par titre :

```sh
APP_ENV=development SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1 \
make preflight-supervised-first-run PREFLIGHT_ARGS="--subject-id SUBJECT_UUID --production-run-id RUN_UUID --synthesis-artifact-id SYNTHESIS_UUID --version VERSION --sha256 SYNTHESIS_SHA256 --tlp TLP"
```

Remplacez les mots en majuscules par les valeurs exactes. Le processus Make
doit aussi recevoir `POSTGRES_DSN` dans son environnement; le prévol ne lit pas
`.env` lui-même. Il refuse les hôtes non loopback, les bases sans nom
`pilot`/`first-run`/`disposable`, l’absence de
`SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1`, et une base non vide sans identité
Alembic. Gardez le rapport sur la sortie terminale; ne demandez pas de fichiers
de rapport générés.

Le prévol hôte ci-dessus reste destiné à une base loopback distincte dont le nom
est explicitement jetable. Pour la base interne de la stack standard, utilisez
depuis la racine du dépôt :

```sh
make preflight-supervised-first-run-stack SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1
```

Cette cible fixe le projet Compose `cti-bulletin`, injecte l’attestation
explicite `SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1` et exécute le script embarqué
dans l’image backend depuis `worker`. Le garde Postgres accepte alors le seul
hôte interne `postgres` sur le port 5432 pour ce projet, quel que soit le nom
de base, puis exige une connexion en lecture seule et la révision Alembic
courante au head. Les autres cibles locales gardent leur garde loopback et nom
de base jetable. La commande ne publie pas le port Postgres.

Depuis le worker, le prévol vérifie Postgres, Redis par `PING`, le bucket MinIO
par `HEAD`, le statut réel de l’identité via `GET /api/identity/status`, et la
capacité Bridge par `GET /v1/models` sans données d’enquête. Il n’envoie aucune
requête VirusTotal ou Shodan, ne lit pas de corps d’échantillon et n’exécute ni
migration ni écriture. Le registre d’outils est construit avec la factory et les
settings du worker; les adapters configurés sont construits sans requête
fournisseur. Il ne prouve aucun résultat live d’outil.

La ligne Make est vérifiée par `make -n` et
`test_make_stack_preflight_executes_script_inside_worker`; les gardes de cible
et Alembic sont couverts par les tests du prévol. Cette vérification ne démarre
pas la stack : l’exécution de cette commande contre les conteneurs réels, leurs
réseaux et leurs données reste à faire avant le premier run. Un échec garde le
prévol en NO_GO.

## 4. Subject, investigation et décisions G2/G3

Choisissez un `Subject` déjà sélectionné par son UUID et vérifiez les liens
canoniques entre `subject_id`, `production_run_id`, `synthesis_artifact_id`,
version, SHA-256 et TLP. Ne choisissez jamais par titre, rang ou ressemblance.
Pour démarrer l’investigation depuis ce transfert, la route est
`POST /api/subjects/{subject_id}/investigations`; son corps demande
`production_run_id`, `synthesis_artifact_id` et `synthesis_sha256`.

Pour G2, consultez `GET /api/investigations/{investigation_id}/envelope`.
L’approbation initiale est `POST /api/investigations/{investigation_id}/envelope/approve`
avec `Idempotency-Key` unique et un corps qui renseigne `reason`,
`correlation_id`, `allowed_providers`, `budget_limits`,
`acquire_hits_allowed` et `disclosure` (`tlp`, `do_not_submit`,
`external_llm_allowed`). L’approbation initiale ne prend pas
`expected_version`; pour
`POST /api/investigations/{investigation_id}/envelope/extend`, utilisez la
version courante, `reason`, `correlation_id` et une nouvelle
`Idempotency-Key`. `POST /api/investigations/{investigation_id}/envelope/revoke` exige
`expected_version`, `reason`, `correlation_id` et une nouvelle
`Idempotency-Key`.

**La révocation G2 arrête l’investigation.** Une reprise sans nouvelle décision
reste arrêtée avec `MODEL_UNAVAILABLE`. Si `operator-status` expose
`reapproval_supported=true` et l’action `POST
/api/investigations/{investigation_id}/envelope/extend`, l’opérateur peut rendre
une nouvelle décision humaine versionnée : l’identité authentifiée fournit
l’acteur; le corps porte `expected_version`, `reason`, `correlation_id`, les
fournisseurs, budgets et limites de divulgation; l’en-tête porte une
`Idempotency-Key` neuve. La réextension reste dans le plafond G2 sauvegardé lors
de la révocation, ne restaure aucun droit omis et garde chaque quota au moins au
niveau consommé plus réservé. Si la révocation historique n’a pas conservé de
plafond, l’API renvoie `409 envelope_revoked` et `operator-status` propose de
créer une nouvelle investigation depuis une synthèse vérifiée. Relisez la
version courante et `operator-status` après chaque décision; ne reprenez
qu’après une extension G2 explicite réussie.

G3 est la décision humaine sur le brouillon d’avis, après inspection du PDF
(étape 6). Elle se fait par `POST /api/opinions/{opinion_id}/approve`, avec une
`Idempotency-Key` unique et `version`, `expected_sha256` et `reason`. Un jeton
API ne déclenche jamais cette décision de lui-même.

La progression se lit avec `GET /api/investigations/{investigation_id}/operator-status`.
La projection en lecture seule expose les identités et hashes canoniques,
`investigation_version`, `working_state_version`/SHA, jobs actifs, tour,
invocations ambiguës et à réconcilier, checkpoints de preuves/avis, G2,
budgets/réservations, dépendances et `next_idempotent_action`. Codes stables :
`404 investigation_not_found`, `409 investigation_not_started`,
`503 investigation_operator_status_unavailable`.

## 5. STOP → DIAGNOSTIC → CORRECTION → REBUILD → RESUME

1. Demandez l’interruption avec
   `POST /api/investigations/{investigation_id}/interrupt`, une
   `Idempotency-Key` neuve et `{"expected_version": <working_state_version>}`.
   Lisez ensuite `operator-status`; ne partez pas du principe qu’un appel
   externe déjà parti a été annulé.
2. Pour chaque entrée `invocations_requiring_reconciliation`, retrouvez
   l’invocation et son `version`. La route
   `POST /api/investigations/{investigation_id}/invocations/{invocation_id}/reconcile`
   exige une nouvelle `Idempotency-Key`, `expected_version`, `reason` et
   `outcome`. Les seules décisions sont `executed_with_result`,
   `not_executed` et `abandon`; seuls les résultats confirmés acceptent
   `response_refs`. N’attestez une exécution qu’avec ses preuves.
3. Corrigez le code, puis reconstruisez sans reset avec `make up` (`up -d
   --build --wait`). Les volumes et checkpoints canoniques restent en place.
   Si le patch change le schéma de persistance ou la sérialisation canonique
   d’un état, **bloquez la reprise** jusqu’à une procédure de migration et de
   compatibilité validée; n’improvisez ni `stamp` ni réécriture.
4. Relisez `operator-status`, la version courante et G2. Si G2 n’a pas été
   révoquée, reprenez avec `POST /api/investigations/{investigation_id}/resume`,
   une nouvelle `Idempotency-Key` et `expected_version` courant. Après
   révocation, ne reprenez pas sans nouvelle décision : l’état reste
   `MODEL_UNAVAILABLE`. Pour une réextension prise en charge, l’action suivante
   de `operator-status` fournit la route `POST
   /api/investigations/{investigation_id}/envelope/extend`, sa version attendue
   et l’indicateur de plafond G2 appliqué. La nouvelle décision exige l’acteur
   humain authentifié, `reason`, `correlation_id`, `expected_version`, budgets
   et limites conservés à la révocation, et une `Idempotency-Key` unique.
   Aucun droit retiré n’est restauré; chaque quota reste au moins consommé plus
   réservé. Si l’ancienne révocation n’a pas de plafond sauvegardé, l’API répond
   `409 envelope_revoked` et `operator-status` indique de créer une nouvelle
   investigation avec une synthèse vérifiée. Après une extension réussie,
   relisez l’état puis reprenez avec une nouvelle clé et la version courante.

## 6. Brouillon PDF, retouches et publication

Consultez le brouillon via `GET /api/investigations/{investigation_id}/opinion`;
si nécessaire, `POST /api/investigations/{investigation_id}/opinion/compose`
demande une `Idempotency-Key` et un corps de sélection (éventuellement `{}`).
Téléchargez le PDF avec
`GET /api/opinions/{opinion_id}/preview.pdf?version={version}&opinion_sha256={sha256}&base_publication_sha256={sha256}`.
Les trois paramètres sont obligatoires. Vérifiez `X-PDF-SHA256`,
`X-Opinion-Content-SHA256`, `X-Base-Publication-SHA256` et
`X-Opinion-Status: draft-unapproved`. Une version ou base périmée renvoie 409;
relisez le brouillon et redemandez le preview avec ses hashes actuels.

Les retouches passent par `POST /api/opinions/{opinion_id}/revise`, avec une
`Idempotency-Key`, `expected_version` et `blocks` édités. Si `observations` est
fourni, `expected_sha256` est obligatoire. Une observation ancrée contient
`id`, `anchor`, `text`, `state`, `relation` et ses références finding/evidence;
conservez les références vérifiables et l’ancre de section/fragment.
Regénérez et inspectez le PDF après chaque révision.

Après approbation humaine G3, la publication explicite est
`POST /api/opinions/{opinion_id}/publish` avec une `Idempotency-Key`, `version`
et `expected_base_publication_sha256`; elle crée l’artefact de publication V6.
Le retour indique `composition_repin_required`. Relisez
`GET /api/editions/{edition_id}/composition`, puis réépinglez explicitement
par `PUT /api/editions/{edition_id}/composition` avec une
`Idempotency-Key`, `expected_version` et `entries` incluant l’ID, la version et
le hash exacts de l’artefact V6. Préservez les autres entrées voulues.

Relisez `GET /api/editions/{edition_id}/review`, résolvez les décisions
nécessaires, puis acceptez la composition avec
`POST /api/editions/{edition_id}/composition/accept` et
`expected_composition_version`. Suivez `GET /api/editions/{edition_id}/release`;
`POST /api/editions/{edition_id}/release/render` relance le rendu en échec.
Téléchargez le rendu avec `GET /api/editions/{edition_id}/release/pdf`.

Le preview est un brouillon non approuvé : il ne vaut ni G2, ni G3, ni
publication. L’approbation et la publication restent des actes humains
distincts.

## 7. Couverture offline et validations live restantes

Les tests du prévol indiqués ci-dessous ont été exécutés pendant ce lot. Les
autres scénarios indiquent les preuves présentes dans cette worktree; ils ne
remplacent pas les validations live.

| Prouvé par scénario de test offline | À valider pendant le premier run supervisé |
|---|---|
| Prévol : `test_standard_stack_database_requires_compose_identity_and_attestation`, `test_stack_database_without_exact_local_identity_is_refused`, `test_stack_database_requires_the_current_alembic_head`, `test_make_stack_preflight_executes_script_inside_worker`, `test_stack_redis_probe_uses_ping_without_disclosing_url`, `test_stack_minio_probe_checks_bucket_with_read_only_head`, `test_bridge_probe_sends_only_get_models_capability_request`, `test_stack_identity_probe_reports_only_identity_status_fields`, `test_runtime_inventory_uses_worker_factory_without_provider_requests`; reset : `test_real_run_marker_blocks_confirmed_reset_before_any_deletion`, `test_initial_data_reset_requires_confirmation_and_displays_counts`; composition : `test_compose_propagates_critical_settings_to_api_and_worker_from_a_fake_env`. | Exécuter le prévol après `make up` pour vérifier la base et les dépendances de la stack réelle; vérifier séparément les connexions Bridge, VT via proxy et Shodan avec permissions réelles. `make -n` et les tests prouvent la commande générée et ses gardes, pas la connectivité live. |
| G2/CAS : `test_governance_routes_approve_extend_revoke_and_enforce_cas`; révocation/réextension : `test_revoke_then_explicit_reextension_preserves_scope_and_consumption`, `test_revoked_envelope_reextension_cannot_exceed_approved_bounds`, `test_revoked_reextension_cannot_reduce_budget_below_consumed_usage`, `test_legacy_revocation_without_saved_bounds_requires_new_investigation`, `test_w16_revoked_g2_requires_human_extend_then_resumes_without_repeating_vt`, `test_w16_revoked_reextension_rejects_disclosure_relaxation_without_mutation`. | Confirmer les décisions humaines, versions, acteurs, raisons et quotas du vrai dossier; une révocation et une réextension restent des décisions opérateur distinctes. |
| W16 : `test_w16_interrupt_during_bridge_wait_is_durable_read_only_and_resumable` et `test_w16_g2_revocation_during_vt_archives_reply_and_blocks_followup`. | Confirmer dans les journaux live les checkpoints, décisions et preuves sans double effet fournisseur. |
| W17 : `test_w17_real_typst_draft_preview_preserves_structure_and_limits` inspecte le PDF Typst réel et l’ancrage d’une note. | Faire relire le contenu et la mise en page du vrai brouillon avec les preuves du Subject sélectionné; décider G3 manuellement. |
| W18 : `test_w18_supervised_first_run_recovers_without_replaying_confirmed_effects` couvre le parcours intégré avec fournisseurs simulés et la récupération sans rejouer les effets confirmés. | Tester les vraies données et dépendances; les simulations ne prouvent aucune connectivité live ni autorisation de publication. |

FLOSS, MWCP et l’adaptateur d’analyse REA restent absents/non livrés sur cette
machine. Retrohunt reste OFF jusqu’à validation YARA et décision explicite.
