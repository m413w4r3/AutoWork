# Runbook — premier run CTI technique supervisé

## Décision au 14 octobre 2026

**NO_GO pour un premier run réel aujourd'hui.** Le chemin critique n'a pas de
smoke réel, Bridge est inaccessible localement, le proxy vtUI n'est pas
configuré et `LocalIdentityProvider/dev-analyst` est `verified=false`. Ne pas
démarrer un run tant que ces blocages et les décisions opérateur ci-dessous ne
sont pas levés.

Le dépôt est prêt à valider offline. Les tests et une CI verte ne sont pas une
preuve live. Les smoke live demandent une approbation explicite de l'opérateur
et suivent cet ordre : Bridge, VT via vtUI, puis Shodan.

## Décisions manuelles avant tout run

1. Vérifier les identifiants canoniques exacts : `subject_id`,
   `production_run_id`, `synthesis_artifact_id`, version, SHA-256 et TLP. Ne
   jamais choisir par titre, position ou ressemblance visuelle.
2. Fournir une DSN explicitement jetable sur loopback et confirmer le nom de la
   base ainsi que son identité Alembic. Le prévol courant n'a pas de DSN ni de
   référence exacte.
3. Configurer Bridge et le proxy vtUI local; choisir une identité vérifiée pour
   tout gate qui l'exige. `dev-analyst` est accepté uniquement pour un accès
   local isolé et ne passe pas le gate d'identité readiness.
4. Approuver séparément G2 (fournisseurs, budgets, acquisition et divulgation)
   avant les smoke live Bridge, VT/vtUI et Shodan. Relire l'avis puis décider
   G3. La publication reste hors périmètre.

L'aperçu PDF du brouillon « Avis de l'analyste » n'a pas de route API vérifiée.
Ne pas le remplacer par un aperçu d'édition ni prétendre que le PDF du brouillon
est récupérable.

## Prévol et rapports régénérés

Le prévol est en lecture seule : il ne contacte aucun fournisseur, ne lit aucun
corps d'échantillon et ne migre ni n'écrit dans PostgreSQL. Il fait seulement
des sondes TCP locales. Les fichiers JSON et Markdown sont des sorties
générées dépendant de la machine et du SHA; ils sont supprimés et ignorés par
Git. Régénérer les rapports uniquement avec la cible Make :

```sh
POSTGRES_DSN="${PILOT_POSTGRES_DSN:?set the exact loopback pilot DSN}" SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1 APP_ENV=development make preflight-supervised-first-run PREFLIGHT_ARGS="--subject-id ${SUBJECT_ID:?set subject UUID} --production-run-id ${PRODUCTION_RUN_ID:?set production run UUID} --synthesis-artifact-id ${SYNTHESIS_ARTIFACT_ID:?set synthesis artifact UUID} --version ${SYNTHESIS_VERSION:?set synthesis version} --sha256 ${SYNTHESIS_SHA256:?set synthesis SHA-256} --tlp ${SYNTHESIS_TLP:?set synthesis TLP} --report-json ../docs/operations/supervised-first-run-readiness.json --report-markdown ../docs/operations/supervised-first-run-readiness.md"
```

Le prévol refuse une cible distante ou live, une base sans marqueur jetable,
une base pilote mal identifiée et une base non vide sans identité Alembic. Il ne
lance jamais `stamp`, `upgrade` ou `downgrade`. La sortie actuelle est
`NO_GO`.

## Base pilote isolée

Ne pas lancer `make up` sur une DSN non vérifiée : la cible standard contient un
service `migrate`. Pour un pilote explicitement jetable, définir
`PILOT_ENV_FILE` hors du dépôt et injecter les DSN depuis le gestionnaire de
secrets local sans les afficher. Le profil `integration-test` utilise
`postgres-test` sur `tmpfs`; les ports publiés de MinIO, backend et frontend
restent liés à `127.0.0.1`.

```sh
docker compose -p cti-pilot-20261014 --env-file "$PILOT_ENV_FILE" --profile integration-test up -d postgres-test redis minio
docker compose -p cti-pilot-20261014 --env-file "$PILOT_ENV_FILE" run --rm --no-deps minio-init
docker compose -p cti-pilot-20261014 --env-file "$PILOT_ENV_FILE" --profile integration-test exec -T postgres-test createdb -U postgres autowork_pilot
```

N'initialiser que cette base jetable après une sortie de prévol qui confirme
`SAFE_READ_ONLY`, le nom `autowork_pilot`, `schema=empty` et le marqueur
jetable. Pour cette base seule, la commande de migration existante est :

```sh
cd backend && POSTGRES_DSN="${PILOT_POSTGRES_DSN:?set the exact loopback pilot DSN}" APP_ENV=development uv run --python 3.12 alembic upgrade head
```

Ne jamais exécuter cette commande ni `stamp` sur une base live ou inconnue. Ce
runbook ne demande aucune migration sur une base live.

## Démarrer et surveiller

Le profil de pilote configure `BACKEND_PORT=127.0.0.1:18000` et
`PILOT_CONTAINER_POSTGRES_DSN` vers `postgres-test:5432`. Quand le token
opérateur est configuré, `OPERATOR_API_TOKEN` est injecté dans les services
backend et frontend; Vite l'utilise côté serveur uniquement, sans variable
`VITE_`. Le healthcheck `/api/health/ready` et `/api/health/live` sont les deux
routes exactes accessibles sans Bearer.

Pour les commandes ci-dessous, définir `API=http://127.0.0.1:18000` dans le
shell opérateur. L'URL publiée est loopback.

```sh
docker compose -p cti-pilot-20261014 --env-file "$PILOT_ENV_FILE" --profile integration-test run -d --name cti-pilot-api --no-deps --service-ports -e POSTGRES_DSN="$PILOT_CONTAINER_POSTGRES_DSN" backend
docker compose -p cti-pilot-20261014 --env-file "$PILOT_ENV_FILE" --profile integration-test run -d --name cti-pilot-worker --no-deps -e POSTGRES_DSN="$PILOT_CONTAINER_POSTGRES_DSN" worker
curl --fail --silent --show-error http://127.0.0.1:18000/api/health/live
docker logs --follow --tail=100 cti-pilot-api
docker logs --follow --tail=100 cti-pilot-worker
```

Les commandes API métier qui suivent supposent un token conservé dans une
variable locale `OPERATOR_API_TOKEN`; ne pas l'écrire dans le dépôt, les
rapports ou l'historique. Toutes les routes `/api/` sauf les trois chemins
exacts `/api/identity/status`, `/api/health/live` et `/api/health/ready`
exigent le Bearer. Les autres routes `/api/health/*` restent protégées.

## Interrompre, reprendre et récupérer

Relire l'investigation pour obtenir sa version actuelle avant chaque action.
Interrompre et reprendre sont des actions versionnées et exigent chacune une
clé d'idempotence nouvelle. Après révocation de G2, ne pas reprendre.

```sh
curl --fail --silent --show-error -X POST "${API}/api/investigations/${INVESTIGATION_ID}/interrupt" -H "Authorization: Bearer ${OPERATOR_API_TOKEN:?set operator token}" -H 'Content-Type: application/json' -H "Idempotency-Key: ${INTERRUPT_KEY}" -d "{\"expected_version\":${RUNNING_VERSION}}"
curl --fail --silent --show-error "${API}/api/investigations/${INVESTIGATION_ID}" -H "Authorization: Bearer ${OPERATOR_API_TOKEN:?set operator token}"
curl --fail --silent --show-error -X POST "${API}/api/investigations/${INVESTIGATION_ID}/resume" -H "Authorization: Bearer ${OPERATOR_API_TOKEN:?set operator token}" -H 'Content-Type: application/json' -H "Idempotency-Key: ${RESUME_KEY}" -d "{\"expected_version\":${PAUSED_VERSION}}"
```

Pour arrêter le run, interrompre puis révoquer l'enveloppe G2 à sa version
courante :

```sh
curl --fail --silent --show-error -X POST "${API}/api/investigations/${INVESTIGATION_ID}/envelope/revoke" -H "Authorization: Bearer ${OPERATOR_API_TOKEN:?set operator token}" -H 'Content-Type: application/json' -H "Idempotency-Key: ${REVOKE_KEY}" -d "{\"expected_version\":${ENVELOPE_VERSION},\"reason\":\"arrêt du pilote\",\"correlation_id\":\"${REVOKE_KEY}\"}"
docker stop cti-pilot-worker cti-pilot-api
```

Pour une pause des conteneurs sans mutation de l'investigation, les commandes
Compose/Docker existantes sont `docker stop cti-pilot-worker cti-pilot-api`
puis `docker start cti-pilot-api cti-pilot-worker`. L'action API `interrupt`
reste nécessaire pour suspendre l'investigation elle-même.

Récupérer le brouillon JSON par `GET /api/investigations/${INVESTIGATION_ID}`
ou sa route d'opinion, puis sauvegarder PostgreSQL avant l'arrêt du projet :

```sh
curl --fail --silent --show-error "${API}/api/investigations/${INVESTIGATION_ID}" -H "Authorization: Bearer ${OPERATOR_API_TOKEN:?set operator token}"
docker compose -p cti-pilot-20261014 --env-file "$PILOT_ENV_FILE" --profile integration-test exec -T postgres-test pg_dump -U postgres -d autowork_pilot > "$PILOT_BACKUP_DIR/autowork_pilot.sql"
docker compose -p cti-pilot-20261014 --env-file "$PILOT_ENV_FILE" --profile integration-test down
```

Ne pas ajouter `-v` avant la vérification de la sauvegarde. PostgreSQL est sur
`tmpfs`; ses données disparaissent à la suppression du conteneur. Aucun
mécanisme actuel ne produit ou ne récupère l'aperçu PDF du brouillon d'avis.

## État des fournisseurs et limites

- Bridge, VT via vtUI et Shodan n'ont reçu aucun appel réel; les résultats
  offline restent `LIVE_NOT_VALIDATED` / `NOT_VALIDATED`.
- Retrohunt est désactivé par défaut, sans slot consommé. La limite de deux
  jobs par sujet et le refus du troisième sont testés offline.
- FLOSS et MWCP sont absents de la machine. REA/Ghidra est absent; la raison
  pour REA est que le runner confiné n'est pas livré.
- Le gate L5 « un autre développeur suit la doc sur un env vierge » n'a pas été
  validé par un tiers. L'écart éditorial CN2609 reste documenté : les sections
  de l'avis n'ont pas de notes contextualisées par section.
- Le conteneur backend ne tourne pas sous un utilisateur dédié (P1 avant
  exposition); le pilote local tourne sous l'utilisateur non privilégié de la
  machine.
