# Réduction de la chaîne Alembic et note pour la base réelle

Date : 2026-10-12

## Décision sur le bootstrap

Conserver Alembic avec une seule baseline mutable, 0001_baseline.

Un bootstrap sans Alembic devrait quand même importer toute la metadata ORM, exécuter séparément
les fonctions et triggers PostgreSQL définis aujourd'hui dans la baseline, puis vérifier que
tables, colonnes, contraintes, index et gardes TLP/immutabilité correspondent à la cible. Il
faudrait aussi détecter et refuser explicitement un schéma partiel ou incompatible, vérifier la
cohérence ORM/base, gérer le second lancement sans dégâts, et maintenir l'exécution identique
dans Compose et la CI. Base.metadata.create_all() seul ne satisfait pas ces exigences.

Retirer une révision redondante d'Alembic supprime une étape conditionnelle sans créer un second
chemin d'initialisation à maintenir. Alembic garde le suivi de version, l'échec transactionnel
sur une collision, le cycle de downgrade de la baseline et le point d'entrée Compose existant.
Un bootstrap maison augmenterait le code et les vérifications dupliqués sans simplifier
l'initialisation complète. La chaîne conservée est donc Alembic avec 0001_baseline comme unique
head.

## Preuve préalable S1 / S2 / ORM

La preuve a été exécutée avant modification du dépôt, sur PostgreSQL 16.14 jetable en tmpfs,
publié sur 127.0.0.1:55444. La base applicative réelle n'a pas été consultée.

- S1 : Alembic a appliqué 0001_baseline puis 0002_production_batch_pause. La révision enregistrée
  était 0002_production_batch_pause.
- S2 : Alembic a appliqué une copie temporaire ne contenant que 0001_baseline. La baseline a
  créé les mêmes objets à partir de Base.metadata.
- Les snapshots pg_catalog et information_schema ont été identiques octet pour octet. Chaque
  snapshot décrit 73 tables, 969 colonnes, 592 contraintes, 243 index, 10 fonctions, 37 triggers
  et aucune séquence.
  La signature compare les types, nullabilités et défauts des colonnes, les définitions et
  propriétés des contraintes, pg_get_indexdef, pg_get_functiondef, pg_get_triggerdef et les
  paramètres de séquence.
- pg_dump --schema-only --no-owner --no-privileges a produit 5 662 lignes pour chaque base.
  Les seules différences brutes étaient les valeurs aléatoires des commandes restrict et
  unrestrict de PostgreSQL 16. Après leur retrait, les deux dumps ont le même SHA-256 :
  97924484be7e5aec38bdcf89327e40414dc232fa8f23aba256f30a6298f618f9.
- Dans les deux bases, paused_at est nullable timestamp with time zone, paused_by est nullable
  varchar(255), ck_batch_status contient paused, et ck_batch_pause_identity impose
  (paused_at IS NULL) = (paused_by IS NULL).
- L'ORM contient déjà ces deux colonnes et ces deux CHECK dans
  backend/src/cti_app/infrastructure/database/models/production.py. 0001 importe ce modèle et
  crée toute la metadata. Il n'y a donc pas de dérive entre la cible ORM, 0001 et l'effet de
  0002 ; les inspections conditionnelles de 0002 ne changeaient rien sur S1.
- Les colonnes, CHECK, table, index et trigger décrits par
  docs/design/2026-10-08-live-db-additive-ddl.sql sont déjà présents dans 0001 via la metadata
  ORM et la déclaration des triggers de la baseline. Le journal du 8 octobre est annoté ci-dessus
  pour préciser que son estampille décrit l'état historique/réel de la base, pas la chaîne cible.

## Base réelle : procédure préparée, non appliquée

**NON APPLIQUÉE — requiert l'accord explicite de l'utilisateur.** Cette branche ne consulte et ne
modifie pas la base réelle. La base réelle est déclarée comme étant estampillée
0002_production_batch_pause ; après suppression du fichier de révision, Alembic ne pourra plus
résoudre cette estampille avant restamp.

Toutes les commandes ci-dessous sont des pré-vérifications en lecture seule. Les exécuter avec
les paramètres de connexion de la base réelle uniquement après accord :

1. Vérifier qu'Alembic contient exactement une ligne, égale à
   0002_production_batch_pause :

   ```sql
   SELECT count(*) AS rows, min(version_num) AS revision
     FROM public.alembic_version;
   ```

   Le résultat attendu est 1 et 0002_production_batch_pause.

2. Comparer en lecture seule le schéma de la base réelle au schéma obtenu par
   alembic upgrade head sur une base PostgreSQL jetable créée depuis la même révision du code.
   Dans un fichier alembic.ini temporaire, utiliser le chemin backend/migrations de ce checkout,
   backend/src dans prepend_sys_path et l'URL de la base jetable dans sqlalchemy.url ; lancer
   alembic upgrade head sur cette base jetable. Puis exécuter, en lecture seule sur les deux
   bases :

       pg_dump "$LIVE_DSN" --schema-only --no-owner --no-privileges > /tmp/live-schema.sql
       pg_dump "$BASELINE_DSN" --schema-only --no-owner --no-privileges > /tmp/baseline-schema.sql
       sed -E '/^\\(un)?restrict /d' /tmp/live-schema.sql > /tmp/live-schema-normalized.sql
       sed -E '/^\\(un)?restrict /d' /tmp/baseline-schema.sql > /tmp/baseline-schema-normalized.sql
       diff -u /tmp/live-schema-normalized.sql /tmp/baseline-schema-normalized.sql

   Le code de sortie de diff doit être 0. Cette comparaison exhaustive du dump comprend tables,
   colonnes/types/nullabilité/défauts, PK/FK/UNIQUE/CHECK et leurs définitions, index, fonctions,
   triggers et séquences. Vérifier aussi depuis pg_catalog et information_schema, en lecture
   seule, les mêmes objets et définitions, notamment paused_at, paused_by, ck_batch_status et
   ck_batch_pause_identity.
3. En cas de différence, arrêter ici : ne pas restamp et ne pas lancer d'upgrade automatique.
   Faire examiner la dérive et obtenir une procédure distincte.

Seulement après accord explicite et après égalité de schéma vérifiée, démarrer la transaction
minimale suivante :

    BEGIN;
    LOCK TABLE public.alembic_version IN EXCLUSIVE MODE;
    UPDATE public.alembic_version
       SET version_num = '0001_baseline'
     WHERE version_num = '0002_production_batch_pause';

Avant de continuer, confirmer que psql affiche UPDATE 1. Pour tout autre résultat, exécuter
ROLLBACK et arrêter. Si le résultat est UPDATE 1, terminer et vérifier :

    COMMIT;
    SELECT version_num FROM public.alembic_version;

La ligne finale doit retourner 0001_baseline. Le restamp ne modifie aucun objet métier. Il ne
doit jamais être appliqué avant les pré-vérifications ni sans l'accord explicite de l'utilisateur.
