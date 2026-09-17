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
