# Dérive de `ck_vt_observation_capability`

## État et constat

`vt.behaviour` produit une observation avec `capability = 'behaviour_reports'`. Le modèle courant
construit `ck_vt_observation_capability` depuis `VirusTotalCapability` et accepte huit valeurs,
dont `behaviour_reports`. La migration unique `0001_baseline` appelle
`Base.metadata.create_all`, donc une base neuve reçoit cette même définition.

L'historique Git du seul modèle montre une ancienne contrainte codée en dur sans
`behaviour_reports` (`ef5ad55`), puis son remplacement par la génération depuis l'enum
(`293f1f7`). Une base déjà créée avec la première version conserve sa CHECK : modifier le modèle
ou la baseline ne modifie pas une contrainte PostgreSQL déjà installée. La base live n'a pas été
consultée dans ce lot; sa définition exacte reste à relever par l'opérateur. Tant qu'elle n'est
pas contrôlée et corrigée, `vt.behaviour` reste `DB_SCHEMA_BLOCKED`.

La reproduction sur `postgres-test` a créé cette ancienne CHECK dans une base d'intégration
éphémère. `VirusTotalObservationService.store_behaviours()` a alors levé une violation de
`ck_vt_observation_capability` après archivage de la réponse brute. Après remplacement de la
CHECK par celle du modèle, le même chemin a persisté `behaviour_reports`; les huit valeurs de
l'enum ont également été insérées. Le test existant contre une base créée depuis la baseline a
passé sans modification de migration.

## Sonde lecture seule

Le script exige un DSN explicite, ne lit aucun fichier d'environnement, ouvre une transaction
PostgreSQL en lecture seule et interroge `pg_get_constraintdef(pg_constraint.oid)`. Il compare les
littéraux de la définition avec le `CheckConstraint` du modèle et `VirusTotalCapability`.

```sh
cd backend
uv run --python 3.12 scripts/db_drift_vt_observation_capability.py \
  'postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DATABASE'
```

Codes retour : `0` conforme, `1` contrainte absente ou différente, `2` erreur de connexion,
DSN invalide ou incohérence interne modèle/enums. La sonde n'émet jamais le DSN ni les détails
d'une erreur driver qui pourrait contenir des identifiants.

## Inventaire avant correction

Ne pas lancer la procédure ci-dessous sans approbation de l'opérateur et vérification du DSN
cible. Le lot n'a aucun accès live et n'a exécuté aucun DDL sur cette base.

1. Archiver la sortie de la sonde et la définition actuelle de la contrainte via un SELECT
   `pg_get_constraintdef`.
2. Sauvegarder la base selon le processus DBA avant DDL; conserver aussi un export du schéma de
   `virustotal_observations` et l'inventaire suivant :

   ```sql
   SELECT capability, count(*)
   FROM public.virustotal_observations
   GROUP BY capability
   ORDER BY capability;
   ```

   ```sql
   SELECT count(*) AS rows_rejected_by_current_enum
   FROM public.virustotal_observations
   WHERE capability NOT IN (
     'file_report', 'file_relationships', 'intelligence_search', 'file_download',
     'submissions', 'behaviour_reports', 'behaviour_pcap', 'retrohunt'
   );
   ```

3. Vérifier que la table est `public.virustotal_observations` et que la définition ciblée est
   bien `ck_vt_observation_capability`. Le SQL suppose ces identités et s'arrête en cas d'écart.
4. Prévoir une fenêtre de faible activité : remplacer la contrainte exige un verrou DDL sur la
   table. La validation séparée permet de relâcher ce verrou avant le scan des lignes existantes.

## SQL proposé — à ne pas exécuter sans approbation

La liste est celle de l'enum courant, dans son ordre applicatif. L'ajout `NOT VALID` commence à
contrôler les nouvelles écritures immédiatement; `VALIDATE` vérifie ensuite les lignes déjà
présentes.

```sql
BEGIN;
ALTER TABLE public.virustotal_observations
  DROP CONSTRAINT ck_vt_observation_capability;
ALTER TABLE public.virustotal_observations
  ADD CONSTRAINT ck_vt_observation_capability
  CHECK (capability IN (
    'file_report',
    'file_relationships',
    'intelligence_search',
    'file_download',
    'submissions',
    'behaviour_reports',
    'behaviour_pcap',
    'retrohunt'
  )) NOT VALID;
COMMIT;

BEGIN;
ALTER TABLE public.virustotal_observations
  VALIDATE CONSTRAINT ck_vt_observation_capability;
COMMIT;
```

Après validation, relancer la sonde et l'inventaire `GROUP BY` ci-dessus. Le remplacement ne
modifie ni les observations, ni leurs blobs, ni leur provenance. La CHECK actuelle ne peut avoir
laissé passer que des valeurs autorisées par son ancienne définition; la validation détectera
toute ligne étrangère à l'enum courant.

## Sauvegarde et retour arrière

Avant le premier `COMMIT`, un échec de `DROP`/`ADD` se traite par `ROLLBACK`. Si le premier
`COMMIT` a réussi mais `VALIDATE` échoue, conserver la nouvelle contrainte `NOT VALID` : elle
bloque déjà les nouvelles valeurs invalides. Diagnostiquer les lignes signalées et choisir une
correction en avant; ne pas supprimer la contrainte pour rétablir les écritures non contrôlées.

Un retour après validation n'est envisageable qu'avec la définition enregistrée avant le
changement et après vérification qu'aucune ligne ne contient une valeur non admise par cette
ancienne définition. Comme les observations sont append-only, si des lignes `behaviour_reports`
ont été écrites, ne pas restaurer une CHECK qui les rejette ni les supprimer : garder la
contrainte courante et faire approuver une correction en avant. Le dump et la définition
pré-état sont conservés pour audit et restauration DBA, pas pour réécrire les observations.

Si et seulement si la définition archivée avant intervention est exactement l'ancienne liste
historique ci-dessous et que le compte `behaviour_reports` est nul, le retour après le premier
commit peut être fait ainsi. Pour toute autre définition pré-état, ce SQL ne convient pas : faire
approuver la restauration de la définition réellement archivée.

```sql
BEGIN;
ALTER TABLE public.virustotal_observations
  DROP CONSTRAINT ck_vt_observation_capability;
ALTER TABLE public.virustotal_observations
  ADD CONSTRAINT ck_vt_observation_capability
  CHECK (capability IN (
    'file_report',
    'file_relationships',
    'intelligence_search',
    'file_download',
    'submissions',
    'behaviour_pcap',
    'retrohunt'
  )) NOT VALID;
COMMIT;

BEGIN;
ALTER TABLE public.virustotal_observations
  VALIDATE CONSTRAINT ck_vt_observation_capability;
COMMIT;
```

## Validation réalisée

- `test_virustotal_observations_postgres.py`: toutes les combinaisons d'opération et de capacité
  acceptées par le schéma issu de la baseline.
- `test_vt_observation_capability_drift.py`: reproduction de la violation sur CHECK historique,
  détection par la sonde (`1`), succès de `store_behaviours` après application du modèle, toutes
  les valeurs d'enum persistées, sonde conforme (`0`).
- Aucun DDL, test de connexion ou lecture n'a ciblé la base live.
