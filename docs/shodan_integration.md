# Intégration Shodan

Le port Shodan expose des lectures passives de l'index Shodan. Il ne lance
aucun scan et ne publie ni alerte ni autre ressource. Les capacités sont
facultatives et désactivées par défaut ; une clé absente ou refusée rend cette
capacité indisponible localement sans bloquer les autres outils.

## Capacités et configuration

| Capacité | Lecture utilisée |
| --- | --- |
| `host` | Services indexés d'une adresse IP et leurs bannières |
| `search` | Matches passifs d'une requête, sur une page bornée |
| `domain` | Sous-domaines et enregistrements DNS renvoyés par Shodan |
| `count` | Nombre de matches d'une requête, pour estimer un pivot |

Variables d'environnement disponibles :

- `SHODAN_HOST_ENABLED`, `SHODAN_SEARCH_ENABLED`, `SHODAN_DOMAIN_ENABLED`,
  `SHODAN_COUNT_ENABLED` : opt-in indépendant pour chaque capacité ;
- `SHODAN_API_KEY` : clé transmise uniquement au fournisseur dans le paramètre
  HTTP `key` ; elle n'est pas incluse dans les messages d'erreur ou les
  paramètres d'observation ;
- `SHODAN_BASE_URL` : origine de l'API, par défaut `https://api.shodan.io` ;
- `SHODAN_CONNECT_TIMEOUT_SECONDS`, `SHODAN_READ_TIMEOUT_SECONDS`,
  `SHODAN_MAX_RESPONSE_BYTES`, `SHODAN_MAX_PAGES` et `SHODAN_MAX_RESULTS` ;
- `INVESTIGATION_MAX_SHODAN_CREDITS` : enveloppe de crédits de l'enquête,
  à zéro par défaut.

Une capacité doit être activée et une clé doit être configurée pour effectuer
une requête. Le budget prévisionnel réserve un crédit par opération par
défaut ; les métadonnées d'une réponse conservent ce coût estimé, ainsi que
`query_credits` et `plan` lorsque `/api-info` les fournit.

## Limites d'interprétation

- Un certificat TLS partagé est un pivot d'infrastructure ; il n'établit pas
  l'attribution d'une adresse IP à une famille ou à un acteur.
- Les bannières et les timestamps décrivent l'index Shodan. Ils peuvent être
  anciens et ne prouvent pas l'état actuel du service.
- Les pages et les matches sont bornés par la configuration. Une absence de
  résultat ne prouve pas l'absence mondiale d'un service ou d'un domaine.
- Les bannières sont tronquées à 4096 caractères et les octets JSON complets
  de chaque réponse utile restent disponibles avec leur SHA-256.
- Les filtres de scan actif sont refusés dans `search` : `scan`, `scan_*`,
  `active_scan`, `on_demand_scan`, `scan_id`, `scan_result`, `scan_status` ou
  `scan_time`. Les opérations d'écriture ne sont pas exposées.
