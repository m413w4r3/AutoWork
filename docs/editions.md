# Gestion des éditions mensuelles

## Modèle canonique

Une édition est identifiée par un UUID et par la clé métier unique
`(country_code, period_start, period_end)`. La période couvre obligatoirement un mois civil
complet. PostgreSQL conserve l'état canonique ; l'interface ne déduit ni ne force une
transition.

| Champ | Règle principale |
| --- | --- |
| `country`, `country_code` | libellé et code alpha-2 normalisé en majuscules |
| `period_start`, `period_end` | premier et dernier jour du même mois |
| `tlp` | `CLEAR` à `RED`, sans déclassement |
| `languages` | liste non vide de codes BCP47 simples et uniques |
| `target_major_articles`, `target_briefs` | objectifs bornés, respectivement 0–20 et 0–100 |
| `previous_edition_id` | référence optionnelle à une édition existante |
| `source_profile` | identifiant de configuration, pas un contenu de source |
| `status`, `version` | état de workflow et verrou de concurrence optimiste |

Il n'existe aucun endpoint de suppression. L'archivage est une transition de workflow. Les
éditions publiées ou archivées ne sont plus modifiables.

## Machine d'état

Le parcours nominal est :

`draft → discovery → selection → production → review → assembling → published → archived`

Une revue peut revenir en production, et l'assemblage peut revenir en revue. L'archivage est
possible depuis chaque état non archivé. L'API retourne `allowed_transitions` et le frontend
n'affiche que ces actions, mais le domaine et la transaction SQL restent l'autorité finale.
La progression globale affichée est une projection déterministe du statut, pas un second état.

## API et concurrence

- `POST /api/editions` crée une édition ;
- `GET /api/editions` pagine et filtre par code pays, mois et statut ;
- `GET /api/editions/{id}` retourne l'édition et ses actions autorisées ;
- `PUT /api/editions/{id}` met à jour les métadonnées avec `version` attendue ;
- `POST /api/editions/{id}/transitions` applique une transition avec `version` attendue ;
- `GET /api/editions/{id}/audit` expose le journal d'audit.

Une version périmée produit HTTP 409. L'unicité métier est vérifiée par le service et protégée
par une contrainte PostgreSQL. Les erreurs publiques utilisent un code stable et un message
exploitable sans exposer de détail interne.

## Identité et audit

`IdentityProvider` isole la provenance de l'acteur. En développement,
`LocalIdentityProvider` fournit `dev-analyst`; il sera remplaçable par l'authentification de
production. Création, mise à jour et transition enregistrent toujours cet `actor_id`, le
`correlation_id`, l'avant et l'après dans `edition_audit_events` au sein de la même Unit of
Work que la modification.

Le repository d'audit est append-only et un trigger PostgreSQL rejette `UPDATE` et `DELETE`.
Un trigger distinct empêche également un déclassement TLP par SQL direct.

