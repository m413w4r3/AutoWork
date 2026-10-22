# ADR-0007 — Gel de la baseline Alembic et migration C2 additive

Statut : accepté — 2026-10-20

## Contexte

La base live contient `investigation_experiment_heads`,
`investigation_experiments`, leur index, la fonction append-only et son trigger.
Ces objets ont été appliqués manuellement alors que la base conservait le
marqueur Alembic `0001_baseline`. D'autres installations peuvent porter le même
marqueur sans aucun objet C2. Le dépôt ne versionnait auparavant pas cette
différence, car la baseline était traitée comme mutable.

Une base fraîche doit obtenir le même schéma que les bases existantes après
mise à niveau. Une base marquée `0001_baseline` ne doit perdre ni données ni
historique pendant cette réconciliation. Une forme C2 partielle ou différente
ne peut pas être corrigée par une supposition ou par un `IF NOT EXISTS`.

## Décision

1. Geler `0001_baseline` à la forme pré-C2 correspondant aux installations
   déjà estampillées avant l'ajout manuel. La baseline n'ajoute ni les deux
   tables C2, ni l'index, ni la fonction, ni le trigger. Elle reste le socle
   des nouvelles bases ; les révisions futures portent les changements additifs.
2. Restaurer `0002_c2_experiments` comme propriétaire unique du DDL C2. Il
   crée les tables, clés primaires et contraintes, l'index de timeline, la FK
   restrictive vers `analyst_investigations`, la fonction et le trigger
   append-only.
3. Au début de `0002`, reconnaître seulement deux états déterministes :
   - aucun objet C2 attendu n'existe : créer la forme canonique ;
   - au moins un objet C2 attendu existe : vérifier l'ensemble de la forme
     canonique, puis ne rien modifier si elle est conforme.

   Tout état partiel ou non conforme échoue explicitement avant DDL. La
   validation compare les relations, colonnes/types/nullabilité/défauts,
   contraintes et expressions, index et colonnes ordonnées, FK et actions,
   fonction et corps, ainsi que la sémantique et l'état du trigger. L'échec
   PostgreSQL annule la transaction et laisse le marqueur `0001_baseline`.
4. Le downgrade de `0002` refuse toute suppression si une ligne existe dans
   l'une ou l'autre table C2. Il est uniquement exercé sur des bases jetables
   et est interdit sur la base live, même si ces tables sont vides.
5. Les tests d'intégration PostgreSQL couvrent la base vide, une base déjà
   estampillée `0001` sans C2 avec données, une forme C2 manuelle conforme
   sans DDL supplémentaire, une forme C2 altérée sans mutation, et le refus
   de downgrade avec historique.

## Conséquences

- `alembic upgrade head` sur base vide produit le schéma ORM complet et C2.
- Une installation `0001` sans C2 reçoit seulement les objets C2 ; ses lignes
  antérieures restent intactes.
- Une installation live dont la DDL manuelle correspond exactement au contrat
  passe à `0002` sans recréer ni modifier les objets. L'opérateur doit
  sauvegarder et inspecter la base avant cette commande.
- Un écart de forme demande un diagnostic et un plan séparé. La migration ne
  répare pas une dérive et ne supprime aucun objet existant.
- Une restauration après incident se fait dans une base isolée à partir de la
  sauvegarde vérifiée. On ne rétrograde pas `0002` sur live et on ne restaure
  pas par-dessus la base live dans le cadre de cette procédure.

## Alternatives écartées

- Continuer à muter `0001` ne mettrait pas à niveau les bases déjà estampillées.
- Recréer les objets avec `IF NOT EXISTS` pourrait masquer une table, un index,
  une fonction ou un trigger incompatible.
- Supprimer C2 pour revenir à la baseline perdrait un état C2 canonique et
  n'est pas nécessaire pour enregistrer la révision Alembic.
