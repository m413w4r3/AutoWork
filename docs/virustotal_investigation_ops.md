# Opérations VirusTotal d’investigation

Ce document décrit les opérations passives exposées par `VirusTotalPort` et
l’adaptateur HTTP partagé. L’adaptateur conserve les routes d’endpoint ; un
appelant choisit un type d’opération, jamais un chemin HTTP.

## Opérations et capacités

| Opération | Capability | Résultat |
|---|---|---|
| Rapport fichier | `file_report` | Rapport normalisé et corps JSON brut |
| Relation d’objet | `file_relationships` | Pages pour un fichier, une IP, un domaine ou une URL |
| Intelligence Search | `intelligence_search` | Pages de résultats, dork effective, `descriptors_only` et `order` |
| Rapports Behaviour | `behaviour_reports` | Pages brutes et résumés de sandbox, date, tags, indicateurs PCAP/HTML et sections présentes |
| Téléchargement | `file_download` | Octets streamés vers le sink, tailles et empreintes calculées |
| PCAP Behaviour | `behaviour_pcap` | Capability réservée au téléchargement PCAP ; aucune route n’est ajoutée dans ce lot |

Les capabilities restent désactivées par défaut. Les paramètres `Settings`
ajoutés pour activer les opérations sont :

- `VIRUSTOTAL_OBJECT_RELATIONSHIPS_ENABLED`
- `VIRUSTOTAL_INTELLIGENCE_SEARCH_ENABLED`
- `VIRUSTOTAL_BEHAVIOUR_REPORTS_ENABLED`

Les flags existants `VIRUSTOTAL_FILE_REPORT_ENABLED` et
`VIRUSTOTAL_FILE_DOWNLOAD_ENABLED` restent séparés. Le téléchargement exige
également une route configurée et une allowlist d’hôtes signés HTTPS.

## Relations autorisées

Les types sont `file`, `ip_address`, `domain` et `url`. Les relations suivent
les allowlists vtUI :

- fichier : `contacted_domains`, `contacted_ips`, `contacted_urls`,
  `dropped_files`, `execution_parents`, `itw_urls`, `embedded_urls`,
  `similar_files`, `bundled_files`, `submissions` ;
- IP : `comments`, `communicating_files`, `downloaded_files`, `graphs`,
  `historical_ssl_certificates`, `historical_whois`, `related_comments`,
  `related_references`, `related_threat_actors`, `referrer_files`,
  `resolutions`, `urls` ;
- domaine : `caa_records`, `cname_records`, `comments`,
  `communicating_files`, `downloaded_files`, `graphs`,
  `historical_ssl_certificates`, `historical_whois`, `immediate_parent`,
  `mx_records`, `ns_records`, `parent`, `referrer_files`, `related_comments`,
  `related_references`, `related_threat_actors`, `resolutions`, `soa_records`,
  `siblings`, `subdomains`, `urls`, `user_votes` ;
- URL : `communicating_files`, `downloaded_files`, `referrer_files`,
  `redirecting_urls`.

Un identifiant URL est encodé en Base64 URL-safe sans `=`. Les autres IDs sont
quotés pour le segment de chemin. Une relation absente de l’allowlist lève
`VirusTotalRelationNotAllowedError` avant la requête HTTP.

## Routage et pagination

Les opérations d’investigation utilisent le proxy v3. Les routes fichier,
relations, recherche et Behaviour ne basculent pas vers un accès direct après
un refus, quota, timeout ou erreur serveur. Le fallback v3/v2 existant du
rapport fichier reste explicitement configuré et déclenché uniquement par un
404.

Le téléchargement essaie `GET /files/{hash}/download` sur sa route configurée.
Le ticket `/files/{hash}/download_url` n’est demandé qu’après un 404. Le ticket
est suivi par un client signé sans en-tête d’authentification, sans redirection,
avec allowlist HTTPS, limite de taille et vérification MD5/SHA-1/SHA-256.

Les relations et la recherche utilisent la pagination `meta.cursor`. Behaviour
utilise la limite par défaut de 10 ; les autres pages utilisent la limite de
configuration. Les pages réussies sont archivées comme JSON brut, avec
opération, capability, curseurs d’entrée/sortie, ordre, nombre observé et
indicateur `exhaustive`. Aucune observation d’erreur HTTP n’est créée.

Les dorks restent natives à VT, sans AST global. Leur longueur est limitée à
2 000 caractères ; les caractères de contrôle sont refusés ; les termes
`content:` doivent représenter au moins quatre octets.

## Erreurs vers les statuts d’outil

| Signal VT ou local | Erreur AutoWork | Statut d’outil |
|---|---|---|
| Capability ou route locale absente | capability désactivée / route indisponible | `unavailable` |
| HTTP 403 ou entitlement/plan non autorisé | `VirusTotalNotAuthorizedError` | `unavailable` |
| HTTP 429 ou code/message de quota explicite | `VirusTotalQuotaExceededError` | `quota` |
| 2xx terminal avec `data: []` | page avec `empty=True` et `exhaustive=True` | `empty` |
| Résultats présents mais curseur restant ou plafond atteint | page non exhaustive | `partial` |
| 404, transport, timeout, 5xx ou JSON incompatible | erreur typée avec statut/contexte disponibles | `failed` |

Un 404 n’est jamais converti en résultat vide. `VirusTotalHttpError` conserve
`status_code`, le code et message VT bornés, `retry_after`, la route et
l’opération pour le futur journal d’invocation. Le journal lui-même est hors du
périmètre de ce lot.
