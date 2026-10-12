# Note de déploiement — composition éditoriale multibatch

Date : 2026-10-12 · Branche : `chantier/multibatch-composition`

## État du schéma livré

La cible de cette branche utilise une seule révision Alembic : `0001_baseline`, qui reste
mutable avant le gate AW-024. Elle inclut les tables de composition
`edition_compositions`, `edition_composition_entries` et `edition_composition_events`, ainsi que
les colonnes et contraintes de manifestes nécessaires au discriminateur V1/V2. Elle contient
aussi le schéma de pause des batches (dont `paused_at`, `paused_by`, `ck_batch_status` et
`ck_batch_pause_identity`). Aucune nouvelle chaîne de migrations n'est livrée.

La note d'état de base indique que la base réelle est estampillée
`0002_production_batch_pause`. La cible du dépôt, elle, ne connaît que `0001_baseline`. La
comparaison S1/S2 de la note de réduction Alembic prouve l'équivalence pour le schéma de pause,
mais ne prouve pas que la base réelle possède les nouvelles tables de composition de cette
branche. Cette session n'a ni consulté ni modifié la base réelle.

## Actions d'exploitation proposées — non exécutées

1. **Avant toute opération**, préparer une sauvegarde vérifiée et une fenêtre de maintenance;
   désigner un responsable DBA et un chemin de retour. Ne pas lancer un reset ni un upgrade
   automatique.
2. Lire `public.alembic_version` et inventorier `pg_catalog` / `information_schema` en lecture
   seule. Comparer le schéma réel à une base PostgreSQL jetable construite depuis cette branche,
   incluant tables, colonnes, contraintes, index, fonctions, triggers et valeurs de la révision.
3. Vérifier explicitement les trois tables de composition, les contraintes d'unicité et
   d'immutabilité, les colonnes V2 de `publication_manifests`, et les gardes TLP. Archiver les
   résultats de comparaison et les hashes des dumps normalisés.
4. Si un objet attendu manque ou diffère, **arrêter**. La baseline mutable n'est pas un plan
   d'upgrade pour une base déjà estampillée : aucun `stamp` ne crée les tables manquantes. Faire
   approuver et exécuter une procédure DDL additive séparée, ou un autre plan de migration
   explicitement autorisé, puis refaire la comparaison. Ne pas fabriquer de nouvelle révision
   Alembic dans ce chantier.
5. Seulement si le schéma complet est déjà identique à la cible et si l'exploitant autorise
   explicitement l'opération, préparer le changement d'estampille
   `0002_production_batch_pause` → `0001_baseline` selon la procédure vérifiée de
   `2026-10-12-baseline-collapse-live-db-note.md`. Confirmer une ligne avant et après; interrompre
   et annuler toute transaction dont le nombre de lignes affectées n'est pas exactement un.
6. Après l'opération approuvée, contrôler l'estampille, les invariants SQL, les lectures V1/V2,
   puis les endpoints de composition et publication avec un plan de vérification distinct. Ne
   réaliser aucune écriture de données éditoriales pendant le contrôle.

Ces actions sont **proposées et non exécutées**. Elles ne constituent pas une autorisation de
consulter ou modifier la base réelle. Le déploiement reste bloqué tant que l'état exact des
tables de composition n'a pas été prouvé.

## Risques et limites

- La baseline mutable ne peut pas mettre à niveau les installations existantes à elle seule; le
  risque principal est de confondre estampille Alembic et présence physique des objets.
- Une différence de schéma, une estampille inattendue ou une version Alembic multiple invalide la
  procédure de restamp proposée dans la note de réduction.
- Les manifestes V1 doivent rester lisibles et leurs releases historiques intacts. La vérification
  de lecture V1 est un critère préalable au déploiement.
- Les objets de composition sont canoniques dans PostgreSQL; les blobs documentaires restent dans
  MinIO. Un déploiement incomplet pourrait rendre les commandes de composition indisponibles,
  mais ne doit pas altérer les releases historiques.
- Aucun test de ce lot ne valide des données CTI ni une base réelle. Les scénarios PostgreSQL
  utilisent un conteneur jetable.
