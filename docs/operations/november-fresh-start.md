# Reprise locale M24 — démarrage propre de novembre

Cette recette décrit le reset local `make up-clean` et les contrôles à faire
avant de l'autoriser. Elle vise une instance Compose jetable dont les données
peuvent être perdues. Le reset n'est pas une procédure de réparation de schéma.

## Périmètre exact du reset

Dans le projet Compose `cti-bulletin`, `make up-clean` enchaîne `clean` puis
`up` ([Makefile](../../Makefile)). `clean` appelle
`scripts/ops/safe_local_reset.py`; après validation de sa cible, le script
arrête Compose, supprime les volumes locaux présents
`cti-bulletin_postgres_data`, `cti-bulletin_redis_data` et
`cti-bulletin_minio_data`, puis `make up` reconstruit et redémarre la stack.
Les volumes qui n'existent pas ne sont pas créés pour être supprimés.
La cible `up` passe aussi par `stack-clean-stale` : elle retire les conteneurs
créés mais jamais démarrés et leurs points de réseau orphelins. Son prévol
disque peut appliquer le nettoyage de cache BuildKit documenté dans
[`disk-capacity.md`](disk-capacity.md) ; ce chemin ne supprime pas de volumes.

Les chemins locaux supprimés ou vidés sont exactement :

- `var/workspaces/subjects/` et `var/workspaces/editions/` : répertoires
  supprimés ;
- `var/diagnostics/runs/` : répertoire supprimé ;
- `var/diagnostics/events.jsonl` : fichier vidé s'il existe.

Les autres fichiers de `var/`, notamment les autres diagnostics et
`var/pdf-inspections/`, ne sont pas dans la liste de nettoyage. Le workspace
M24 situé sous `~/work/autowork/` n'est pas nettoyé implicitement : le script
ne supprime que les trois chemins ci-dessus et le fichier `events.jsonl`, et
refuse des chemins hors de `var/`. Compose monte les workspaces applicatifs
depuis `./var/workspaces/subjects` et `./var/workspaces/editions` vers
`/work/subjects` et `/work/editions`; il ne les monte pas depuis
`~/work/autowork/`.

## Règle de décision

Ne jamais utiliser un reset pour réparer une migration échouée sur une base
dont les données doivent être conservées. Arrêter la procédure, préserver la
base et ses blobs MinIO, diagnostiquer la migration et préparer une reprise
avec sauvegarde restaurable. Une migration échouée ne rend pas les données
jetables.

Ne contourner aucun refus de `safe_local_reset.py`. Ses refus signifient que
la cible ou l'état à effacer n'est pas démontré sûr :

- Projet Compose différent de `cti-bulletin`, configuration incomplète ou
  invalide, services PostgreSQL/backend inconnus : l'identité ou la forme de
  la stack n'est pas celle attendue.
- DSN absent/invalide, ou qui ne cible pas l'utilisateur et la base du service
  `postgres` sur `postgres:5432` : le reset ne peut pas prouver qu'il vise la
  base locale attendue.
- Volume absent de la déclaration, externe, mal nommé, mal attaché, sans
  labels Compose attendus, ou métadonnées/liste de volumes illisibles : sa
  propriété par cette stack n'est pas prouvée.
- Contexte Docker inconnu, `DOCKER_HOST` et `DOCKER_CONTEXT` définis ensemble,
  ou endpoint différent d'un socket Unix local : le démon ciblé peut être
  distant ou ambigu.
- Bind mount des workspaces différent du chemin attendu sous `var/`, `var/`
  symbolique, chemin de nettoyage traversant la racine, ou cible symbolique :
  la suppression locale ne peut pas être bornée à ces chemins.
- Échec de lecture de Compose, de l'inspection Docker ou de la requête de
  marqueurs PostgreSQL, résultat illisible, ou commande de contrôle en échec :
  l'état n'est pas vérifiable. Un dry-run exige que PostgreSQL tourne déjà ; il
  ne le démarre pas.
- Données détectées (`production_runs`, `analyst_investigations`, `subjects`
  ou `editions`) avant le premier run : sans confirmation explicite du reset
  initial, le reset est refusé. `CONFIRM_INITIAL_RESET=yes` ne vaut que pour
  ce cas de données initiales réellement jetables ; il ne répare pas une
  migration et ne valide pas la perte de données à conserver.
- Marqueur du premier run présent : reset toujours refusé, confirmation ou
  non. Il signifie qu'une enveloppe d'investigation a été persistée (enveloppe
  G2 approuvée) ou qu'une invocation d'outil a été persistée. Le script
  revérifie ce marqueur après l'arrêt de la stack ; s'il est apparu pendant
  l'arrêt, ou si cette seconde lecture échoue, les volumes sont conservés.

Un refus est un arrêt. Corriger ou faire examiner la configuration, puis
recommencer les précontrôles depuis le début ; ne pas modifier les garde-fous,
renommer des volumes pour les faire passer, ou remplacer la cible Docker.

## Avant le reset

1. Vérifier le checkout candidat et conserver ces résultats dans le compte
   rendu opérateur :

   ```sh
   git branch --show-current
   git rev-parse HEAD
   git status --short
   ```

   Confirmer que la branche et le SHA sont ceux approuvés. Vérifier que la CI
   est verte **pour ce SHA précis** dans l'interface CI du dépôt ; une CI verte
   sur un autre commit ne suffit pas.

2. Établir le périmètre de perte avec l'équipe propriétaire des données.
   Examiner les trois volumes Compose nommés ci-dessus et les quatre chemins
   locaux listés plus haut. Confirmer avec le propriétaire si les volumes
   contiennent des données à conserver ; les commandes ci-dessous ne lisent que
   leur identité, leurs labels et leurs métadonnées :

   ```sh
   docker volume ls --filter label=com.docker.compose.project=cti-bulletin --format '{{.Name}}'
   docker volume inspect cti-bulletin_postgres_data cti-bulletin_redis_data cti-bulletin_minio_data
   ```

   Comparer les labels et les noms observés avec le projet et les volumes
   réellement utilisés par la stack. Ne pas monter ou exporter leur contenu
   pour cet inventaire. Une sortie vide, une cible ambiguë ou un volume sans
   propriété démontrée bloque le reset.

3. Si la moindre donnée doit être conservée, créer et restaurer une
   sauvegarde avant toute autorisation de reset. Utiliser un répertoire de
   sauvegarde approuvé, hors de `var/`, sur un stockage protégé. Les variables
   de connexion ci-dessous doivent être injectées par le gestionnaire de
   secrets local ; ne pas les afficher, les inscrire dans l'historique ou les
   inclure dans un ticket.

   Définir `backup_dir` sur un chemin absolu du stockage approuvé (et vérifier
   que ce chemin n'est pas sous `var/`). La comparaison de `var/` utilise un
   répertoire temporaire séparé :

   ```sh
   backup_dir=/chemin/absolu/du-stockage-approuve/autowork
   restore_dir="$(mktemp -d /tmp/autowork-var-restore.XXXXXX)"
   ```

   PostgreSQL — sauvegarde complète puis restauration réelle dans un
   conteneur jetable séparé :

   ```sh
   mkdir -p "$backup_dir"
   docker compose exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$backup_dir/postgres.dump"
   pg_restore --list "$backup_dir/postgres.dump" >/dev/null
   docker run --rm -d --name autowork-pg-restore-check -p 127.0.0.1:55439:5432 -e POSTGRES_USER=restore -e POSTGRES_PASSWORD -e POSTGRES_DB=restore_check -v "$backup_dir:/backup:ro" postgres:16-alpine
   for attempt in $(seq 1 60); do
     docker exec autowork-pg-restore-check pg_isready -U restore -d restore_check && break
     sleep 1
   done
   docker exec autowork-pg-restore-check pg_isready -U restore -d restore_check
   docker exec autowork-pg-restore-check pg_restore --exit-on-error -U restore -d restore_check /backup/postgres.dump
   docker exec autowork-pg-restore-check psql -U restore -d restore_check -c 'SELECT version_num FROM alembic_version ORDER BY version_num;'
   docker stop autowork-pg-restore-check
   ```

   MinIO — `minio-init` utilise déjà l'image `minio/mc` et les variables
   Compose du service. La sauvegarde copie le bucket configuré ; la vérification
   la restaure et compare son contenu dans un MinIO isolé, à port local dédié :

   ```sh
   mkdir -p "$backup_dir/minio"
   docker compose run --rm --no-deps --volume "$backup_dir/minio:/backup" --entrypoint /bin/sh minio-init -ec 'mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc mirror --preserve "local/$S3_BUCKET" /backup'
   docker run --rm -d --name autowork-minio-restore-check -p 127.0.0.1:19000:9000 -e MINIO_ROOT_USER -e MINIO_ROOT_PASSWORD minio/minio:RELEASE.2025-07-23T15-54-02Z server /data --console-address :19001
   for attempt in $(seq 1 60); do
     curl --fail --silent http://127.0.0.1:19000/minio/health/live >/dev/null && break
     sleep 1
   done
   curl --fail --silent http://127.0.0.1:19000/minio/health/live >/dev/null
   docker run --rm --add-host=host.docker.internal:host-gateway --volume "$backup_dir/minio:/backup:ro" -e MINIO_ROOT_USER -e MINIO_ROOT_PASSWORD --entrypoint /bin/sh minio/mc:RELEASE.2025-07-21T05-28-08Z -ec 'mc alias set restore http://host.docker.internal:19000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && mc mb --ignore-existing restore/cti-restore-check && mc mirror /backup restore/cti-restore-check && mc diff /backup restore/cti-restore-check'
   docker stop autowork-minio-restore-check
   ```

   `S3_BUCKET` peut être surchargé dans la configuration locale ; la commande
   de sauvegarde lit la valeur effective du service. La restauration ci-dessus
   place les objets dans le bucket jetable `cti-restore-check`, sans toucher au
   bucket de l'application. Échec de `pg_restore`, de `mc diff`, ou absence de
   vérification de restauration : pas de reset si des données sont à garder.

   `var/` — archiver l'arborescence puis comparer une extraction dans un
   répertoire temporaire. Son contenu peut inclure des données opérationnelles
   sensibles : protéger l'archive comme les données sources.

   ```sh
   tar --acls --xattrs -cpf "$backup_dir/var.tar" var
   mkdir -p "$restore_dir"
   tar --acls --xattrs -xpf "$backup_dir/var.tar" -C "$restore_dir"
   tar --acls --xattrs --compare -f "$backup_dir/var.tar" -C "$restore_dir"
   ```

4. Examiner les marqueurs du premier run. Le script considère le run commencé
   si `investigation_envelopes` contient une enveloppe, ou, à défaut, si
   `tool_invocations` contient une invocation. Il affiche aussi les comptes de
   `production_runs`, `analyst_investigations`, `subjects` et `editions`.
   Le prévol local officiel est en lecture seule :

   ```sh
   make preflight-supervised-first-run
   ```

   Depuis une stack déjà démarrée, le prévol stack lit PostgreSQL et vérifie
   Redis (PING), MinIO (HEAD bucket), l'identité backend et Bridge (GET
   `/v1/models`, sans donnée d'enquête). Il exige une attestation explicite que
   la base est jetable :

   ```sh
   make preflight-supervised-first-run-stack SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1
   ```

   N'utiliser cette seconde commande qu'après avoir établi cette propriété ;
   elle ne remplace ni le consentement à perdre les données, ni les gates
   propres au premier run.

5. Vérifier la disponibilité locale des services et du Bridge par ces seuls
   contrôles de lecture. `make status` décrit la stack ; le prévol stack est le
   contrôle de capacité Bridge documenté ci-dessus. Il ne demande pas de plan,
   de rédaction ou de consultation Bridge.

6. Effectuer le dry-run prévu par le script. Il vérifie le contexte Docker,
   la configuration Compose, les labels des volumes et les marqueurs de run ;
   il ne supprime rien et ne démarre pas PostgreSQL s'il est arrêté :

   ```sh
   cd backend && uv run --no-sync --python 3.12 python ../scripts/ops/safe_local_reset.py \
     --project-name cti-bulletin --compose-command 'docker compose' --dry-run
   ```

   Cette invocation reprend celle de `make clean` (mêmes options, `--dry-run`
   en plus) ; ne pas la lancer depuis le Python de Conda. Si l'environnement
   Compose local est personnalisé (`COMPOSE`, `COMPOSE_FILE`), reprendre la même
   valeur dans `--compose-command`.

   Un échec ou un refus bloque la suite. Le dry-run ne prouve pas l'intégrité
   d'une sauvegarde et n'autorise pas le reset.

## Après un reset autorisé

Le reset destructif reste une opération séparément autorisée. Après sa réussite,
`make up-clean` lance `make up`; si l'opérateur a exécuté le reset seul,
démarrer la stack avec la cible existante :

```sh
make up
make status
```

Vérifier les points suivants avant tout premier run :

1. **Migrations.** Le service `migrate` applique `alembic upgrade head` au
   démarrage. Confirmer une seule ligne Alembic et la tête attendue :

   ```sh
   docker compose exec -T postgres sh -c 'psql -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version ORDER BY version_num;"'
   ```

   Le résultat doit être l'unique valeur `0029_note_anchor_identity`. Une
   autre valeur, plusieurs têtes, ou une erreur de migration arrête la recette.
   Ne pas relancer `make up-clean` pour corriger cet état.

2. **Schéma et protections.** Vérifier les tables de la chaîne M24, notamment
   `investigation_note_proposals`, `production_artifacts`,
   `investigation_envelopes` et `tool_invocations`. La migration 0029 doit
   ajouter à `investigation_note_proposals` les colonnes
   `base_publication_artifact_id`, `base_publication_version`,
   `base_publication_sha256`, `anchor_before_sha256` et `content_blocks`, plus
   sa FK, ses contraintes CHECK et son index. Vérifier les catalogues PostgreSQL
   des contraintes, triggers et fonctions de protection, puis comparer leur
   inventaire aux migrations jusqu'à 0029 :

   ```sh
   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT table_name FROM information_schema.tables WHERE table_schema = '\''public'\'' AND table_type = '\''BASE TABLE'\'' ORDER BY table_name;"'
   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT column_name FROM information_schema.columns WHERE table_schema = '\''public'\'' AND table_name = '\''investigation_note_proposals'\'' ORDER BY ordinal_position;"'
   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT conname, contype FROM pg_constraint WHERE conrelid = '\''public.investigation_note_proposals'\''::regclass ORDER BY conname;"'
   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT c.relname AS table_name, t.tgname AS trigger_name, p.proname AS function_name FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_proc p ON p.oid = t.tgfoid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = '\''public'\'' AND NOT t.tgisinternal ORDER BY c.relname, t.tgname;"'
   ```

   Pour les colonnes et contraintes 0029, contrôler les noms/versionnés dans
   `backend/migrations/versions/0029_note_anchor_identity.py`. Une migration
   marquée présente ne suffit pas si les protections attendues manquent.

3. **Dépendances et interfaces locales.** Contrôler l'état de tous les services
   avec `make status`, puis la readiness backend et l'interface frontend :

   ```sh
   curl --fail --silent http://127.0.0.1:8000/api/health/live >/dev/null
   curl --fail --silent http://127.0.0.1:8000/api/health/ready >/dev/null
   curl --fail --silent http://127.0.0.1:5173/ >/dev/null
   ```

   Les ports 8000 et 5173 sont les valeurs Compose par défaut et peuvent être
   remplacés par `BACKEND_PORT` et `FRONTEND_PORT`. Le prévol stack confirme
   aussi Redis par PING, le bucket MinIO par HEAD et le GET de capacité Bridge,
   sans soumettre de données d'enquête :

   ```sh
   make preflight-supervised-first-run-stack SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1
   ```

4. **Parcours synthétique.** La cible M24 existante utilise PostgreSQL
   d'intégration jetable et des fournisseurs factices ; elle n'appelle pas les
   fournisseurs réels. Diriger ses résultats hors de `var/` :

   ```sh
   mkdir -p /tmp/m24-fresh-start-tmp
   TYPST_BINARY="$HOME/.local/share/autowork-tools/typst" FONT_BUNDLE_ROOT="$PWD/chpTypst" TMPDIR=/tmp/m24-fresh-start-tmp WORKFLOW_RESULTS_DIR=/tmp/m24-fresh-start-results make test-investigation-browser
   ```

   Cette cible réécrit des résultats de navigateur ; `WORKFLOW_RESULTS_DIR`
   les place sous `/tmp`. Le binaire Typst attendu est la version épinglée
   `0.15.1`, installée dans `$HOME/.local/share/autowork-tools/typst` selon la
   recette M24.2 ; vérifier sa version avec
   `./scripts/check-typst.sh "$HOME/.local/share/autowork-tools/typst"`.
   Ce script compare la version à `infra/typst.lock`. La police par défaut du
   Makefile est le dossier `chpTypst` du checkout ; si l'environnement exige une autre racine, utiliser
   la valeur approuvée et versionnée de cette installation. Garder `TMPDIR`
   court pour les sockets Unix du sidecar.

5. **Porte de session fermée.** Vérifier la présence de la clé dans le backend
   sans afficher sa valeur :

   ```sh
   docker compose exec -T backend sh -c 'test -z "${M24_AGENT_SESSION_KEY:-}"'
   ```

   Un code retour non nul signifie que la clé est activée : arrêter et la
   retirer via le gestionnaire de secrets avant de poursuivre. Les routes de
   session doivent rester fermées tant que leurs gates spécifiques ne sont pas
   autorisées.

## Autorisations nécessaires

- Autorisation explicite et distincte pour supprimer les données locales et
  volumes identifiés, après approbation du périmètre, du commit candidat et de
  la perte éventuelle.
- Si des données sont conservées : autorisation de sauvegarder et de lire ces
  données dans un stockage approuvé, puis preuve d'une restauration isolée
  réussie avant le reset.
- Autorisations propres aux gates pour le premier run supervisé, l'activation
  de `M24_AGENT_SESSION_KEY`, tout appel VirusTotal, toute consultation ou
  rédaction Bridge, tout CLI Codex/Claude réel, et tout déploiement ou DDL sur
  une base vivante. Une vérification Bridge GET `/v1/models` ne vaut pas
  autorisation d'une consultation.

## Ce que ce document n'exécute pas

Ce runbook ne lance ni `make clean`, ni `make up-clean`, ni le reset sans
`--dry-run`. Il ne répare ni ne migre une base, ne supprime aucun volume, ne
restaure aucune sauvegarde, ne réalise aucun premier run, n'active pas la clé
de session et n'autorise aucun appel réel VT, Bridge ou CLI. Il ne pousse ni
ne fusionne de commit et ne déploie pas la stack.
