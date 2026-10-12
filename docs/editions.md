# Gestion des éditions mensuelles

## Modèle canonique

Une édition est un **conteneur mensuel durable**, pas une machine d'état éditoriale. Elle est
identifiée par un UUID et par la clé métier unique `(country_code, period_start, period_end)`.
La période couvre obligatoirement un mois civil complet. PostgreSQL conserve l'état canonique.

| Champ | Règle principale |
| --- | --- |
| `country`, `country_code` | libellé et code alpha-2 normalisé en majuscules |
| `period_start`, `period_end` | premier et dernier jour du même mois |
| `tlp` | `CLEAR` à `RED`, sans déclassement |
| `languages` | liste non vide de codes BCP47 simples et uniques |
| `state` | `open` ou `archived` |
| `version` | verrou de concurrence optimiste |

Il n'existe aucun endpoint de suppression.

## Cycle de vie

Le cycle de vie est administratif et minimal :

`open → archived`

Une édition `open` accepte de nouvelles opérations. `archived` est terminal : les métadonnées ne
sont plus modifiables et aucune réouverture n'est prévue. L'archivage est un cas d'usage
explicite, jamais une conséquence automatique d'une publication ou d'un export.

L'édition ne porte **aucune** progression de pipeline. La découverte, la sélection, la
production, la revue et la publication peuvent survenir plusieurs fois pendant la vie d'une même
édition ; leur état appartient aux entités spécialisées (`DiscoveryRun`, `Subject`,
`ProductionRun`, snapshots de publication) et non au conteneur. Quand une opération doit savoir
si elle est permise au niveau de l'édition, la seule règle est `open` / `archived`.

## API et concurrence

- `POST /api/editions` crée une édition, toujours `open` ;
- `GET /api/editions` pagine et filtre par code pays, mois et `state` ;
- `GET /api/editions/{id}` retourne l'édition ;
- `PUT /api/editions/{id}` met à jour les métadonnées avec `version` attendue ;
- `POST /api/editions/{id}/archive` archive l'édition avec `version` attendue ;
- `GET /api/editions/{id}/audit` expose le journal d'audit.

Une version périmée produit HTTP 409 (`stale_edition_version`). Une modification d'une édition
archivée produit HTTP 409 (`invalid_edition_action`). L'unicité métier est vérifiée par le
service et protégée par une contrainte PostgreSQL (`uq_editions_country_period`), qui produit
HTTP 409 (`duplicate_edition`). Les erreurs publiques utilisent un code stable et un message
exploitable sans exposer de détail interne.

## Identité et audit

`IdentityProvider` isole la provenance de l'acteur. En développement,
`LocalIdentityProvider` fournit `dev-analyst`; il sera remplaçable par l'authentification de
production. Création, mise à jour et archivage enregistrent toujours cet `actor_id`, le
`correlation_id`, l'avant et l'après dans `edition_audit_events` au sein de la même Unit of
Work que la modification.

Le repository d'audit est append-only et un trigger PostgreSQL rejette `UPDATE` et `DELETE`.
Un trigger distinct empêche également un déclassement TLP par SQL direct.

## Composition éditoriale multibatch

La composition est l'autorité versionnée du bulletin. Elle comprend une ligne
`edition_compositions`, ses `edition_composition_entries` ordonnées et le journal append-only
`edition_composition_events`. Une entrée `included` épingle exactement le `ProductionRun`, sa
génération, l'artifact `PUBLICATION`, sa version, son `input_hash` et sa provenance batch
éventuelle. Une entrée `deferred` n'a pas de pin. Une entrée `excluded` porte l'ID d'une décision
de Review. Un sujet ne peut apparaître qu'une fois dans une version.

Review lit les entrées `included` et leurs pins; elle ne choisit pas le dernier batch ni le dernier
run d'un sujet. `GET /api/editions/{id}/composition/validation` retourne des raisons métier et
trois axes indépendants :

- `production_validity` décrit les runs et artifacts épinglés;
- `discovery_update_available` indique qu'un snapshot Discovery plus récent change les données
  de l'identité canonique d'un pin;
- `composition_eligibility` décrit si la composition peut être modifiée/acceptée selon ses
  références et diagnostics.

`discovery_update_available` est calculé depuis les UUID d'identité, les membres candidats et le
résumé du snapshot; il n'utilise pas les titres comme clé. `true` ne signifie ni `STALE` ni
invalidation : l'édition garde son run et son pin. Pour actualiser, l'opérateur déclenche un
nouveau batch explicitement.

La composition se lit avec `GET /api/editions/{id}/composition` et se remplace avec
`PUT /api/editions/{id}/composition`, `Idempotency-Key` et `expected_version`. Les entrées
incluses requièrent le pin complet. Le preview peut être partiel; l'acceptation finale utilise
`POST /api/editions/{id}/composition/accept` avec la version attendue. Le manifeste V2 est
append-only, référence `composition_id`/`composition_version` et n'a pas de `batch_id` racine.
La provenance batch est nullable par entrée; les manifestes V1 historiques gardent leur schéma
et leurs releases restent lisibles.
