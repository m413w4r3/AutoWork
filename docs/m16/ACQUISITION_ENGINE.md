# M16 — moteur d’acquisition résiliente

> Mise à jour intégrée : 2026-11-06. Cette page décrit le comportement du code et de ses
> réglages par défaut ; elle ne constate pas l’état d’un déploiement.

## Déclenchement opérateur

`POST /api/subjects/{subject_id}/collection` lance la collecte HTTP directe (`source.collect`).
Un échec ne déclenche pas automatiquement le moteur de repli. Après un échec admissible, un
opérateur peut demander le repli avec :

```http
POST /api/subjects/{subject_id}/sources/{collection_id}/fallback-acquire
```

La réponse est `202` avec `{job_id, duplicate}`. Le job asynchrone
`source.fallback_acquire` est idempotent sur le sujet, la source, le snapshot de policy et le
prochain numéro de tentative. Il est refusé (`404` source inconnue, `409` repli désactivé ou état
inadmissible) si la source est déjà archivée, en cours de collecte, ou hors des états
`UNAVAILABLE`, `BLOCKED`, `FAILED_RETRYABLE`, `FAILED_TERMINAL`. Un replay identique retourne le
même job avec `duplicate=true`.

Le workbench `GET /api/subjects/{subject_id}/workbench` présente pour chaque source son
`latest_attempt` et son `archive_receipt`. La tentative porte stratégie, version, résultat,
raison, durée, statut HTTP et hashes. Le reçu porte méthode, classes d’équivalence et de preuve,
observation, qualité, hashes raw/decoded, ainsi que la provenance du modèle lorsqu’elle existe
(`model_run_id`, consultation et hashes du prompt/réponse). Toutes les tentatives restent
persistées dans `collection_attempts`; l’événement `source.archived_fallback` conserve la
provenance d’archivage.

Une archive directe ou humaine déjà attachée gagne toujours contre un fallback. L’endpoint
`POST /api/subjects/{subject_id}/sources/{collection_id}/content` permet à un humain de remplacer
un fallback archivé en passant par le CAS sous verrou du service. L’ancien document et ses blobs
restent immuables. Une archive directe ou humaine déjà attachée ne peut pas être remplacée et
renvoie `409 source_already_archived`.

## Ordre du registre

Une archive déjà attachée court-circuite le registre. Pour les autres sources, l’orchestrateur
essaie les stratégies dans cet ordre ; il garde les tentatives et s’arrête sur la première
réponse admissible :

1. Profils HTTP épinglés : `http_plain`, `http_standard_headers`, `http_same_site_referer`,
   `http_retry_after`.
2. Transport HTTP/2 `http2`, puis `curl` : emplacements présents dans le registre, mais aucun
   adaptateur ne garantit actuellement le même épinglage DNS ; ils ne téléchargent rien.
3. Représentations éditeur `representations` (canonique, AMP, print, mobile).
4. `rss_atom_jsonfeed`, `publisher_public_api`, `embedded_structured_data`.
5. `playwright_chromium_dom`, `playwright_network_response`, `playwright_embedded_state`, puis
   `playwright_firefox` et `playwright_webkit` (ces deux moteurs ne sont pas implémentés).
6. `wayback_snapshot`, `commoncrawl_warc`, `memento_archive`, `archive_ph`.
7. `exact_title_search`, `publisher_alternative_url`, `authorized_syndication`.
8. `publisher_pdf`, `visual_ocr`, `official_newsletter_archive`, `third_party_extraction`.
9. `bridge_source_discovery`, puis `bridge_transcription`.

`multi_extractor` n’est pas une stratégie indépendante du registre : l’extraction déterministe
interne de `application/article_extraction.py` choisit parmi HTML, JSON-LD/état embarqué,
flux, PDF et texte dérivé. Le stage métier `EXTRACTION` reste strictement archive-only : il ne
réouvre aucune URL.

## Réglages et valeurs par défaut

Les noms ci-dessous sont des variables d’environnement Settings (majuscules du champ Pydantic).
Les chaînes vides sont les valeurs par défaut lorsqu’elles sont indiquées.

| Réglage | Défaut | Effet opérationnel |
|---|---:|---|
| `COLLECTION_ACQUISITION_DEADLINE_SECONDS` | `120` s | Plafond global du repli. |
| `COLLECTION_ACQUISITION_MAX_ATTEMPTS` | `40` | Budget maximal de probes. |
| `COLLECTION_ACQUISITION_PER_DOMAIN_CONCURRENCY` | `2` | Sémaphore par hôte. |
| `COLLECTION_ACQUISITION_STRATEGY_ENABLED` | mapping ci-dessous | Active/désactive les stratégies par nom. |
| `COLLECTION_ACQUISITION_STRATEGY_TIMEOUT_SECONDS` | mapping ci-dessous | Timeout propre à chaque stratégie. |
| `COLLECTION_FALLBACK_ENDPOINT_ENABLED` | `true` | Autorise l’endpoint opérateur. |
| `COLLECTION_FALLBACK_VARIANTS_ENABLED` / `COLLECTION_FALLBACK_ARCHIVE_ENABLED` / `COLLECTION_FALLBACK_BRIDGE_ENABLED` | `true` | Interrupteurs généraux historiques des groupes de repli. |
| `COLLECTION_FALLBACK_MAX_VARIANTS` / `COLLECTION_FALLBACK_MIN_TEXT_CHARS` | `4` / `200` | Limite de variantes et seuil minimal de texte admissible. |
| `COLLECTION_PLAYWRIGHT_SIDECAR_URL` | vide | URL de contrôle facultative du renderer. |
| `COLLECTION_BROWSER_CONTROL_SOCKET` | vide | Chemin du socket de contrôle Unix partagé. |
| `COLLECTION_BROWSER_NAVIGATION_ENABLED` | `false` | Le navigateur ne navigue pas par défaut. |
| `PLAYWRIGHT_ACQUIRER_SOCKET` | `/run/browser/acquirer.sock` dans Compose | Socket Unix de contrôle du sidecar. |
| `BROWSER_EGRESS_PROXY_URL` | défini par Compose dans le sidecar | Proxy sortant imposé pour la navigation ; ce n’est pas un champ Settings backend. |
| `BROWSER_EGRESS_PROXY_TOKEN` | secret facultatif | Authentification proxy côté sidecar ; fournir par secret manager, jamais journaliser sa valeur. |
| `COLLECTION_SYNDICATION_DOMAINS` | vide | Domaines de syndication explicitement autorisés. |
| `COLLECTION_EXACT_TITLE_SEARCH_ENDPOINT` | vide | Endpoint public de recherche par titre exact. |
| `COLLECTION_OFFICIAL_NEWSLETTER_ARCHIVE_ENDPOINT` | vide | Archive newsletter explicite. |
| `COLLECTION_THIRD_PARTY_PROVIDER_URL` / `COLLECTION_THIRD_PARTY_PROVIDER_TOKEN` | vides | Endpoint et jeton du provider ; aucun jeton réel n’est consigné ici. |
| `COLLECTION_THIRD_PARTY_PROVIDER_ENABLED` | `false` | Opt-in supplémentaire du provider tiers. |
| `COLLECTION_OCR_COMMAND` | vide | Commande OCR locale, passée comme argv sans shell. |
| `COLLECTION_ALLOWED_DOMAINS` / `COLLECTION_BLOCKED_DOMAINS` | vides | Allowlist facultative et denylist. |
| `COLLECTION_TIMEOUT_SECONDS` / `COLLECTION_MAX_REDIRECTS` | `30` s / `5` | Limites du transport HTTP. |
| `COLLECTION_MAX_DOWNLOAD_BYTES` / `COLLECTION_MAX_EXPANDED_BYTES` / `COLLECTION_MAX_DECOMPRESSION_RATIO` | `10 MiB` / `25 MiB` / `20` | Limites de réponse et de décompression. |

Dans `COLLECTION_ACQUISITION_STRATEGY_ENABLED`, les stratégies sont activées par défaut sauf
`playwright_firefox`, `playwright_webkit`, `visual_ocr`, `official_newsletter_archive`,
`third_party_extraction` et `bridge_source_discovery`. Une stratégie activée peut rester
indisponible si son endpoint ou sa dépendance manque.

Valeurs par défaut de `COLLECTION_ACQUISITION_STRATEGY_TIMEOUT_SECONDS` : HTTP plain/standard/
same-site `15` s, retry-after `20` s, HTTP/2 et curl `1` s, représentations `20` s, flux/API/
structured data `15` s, Playwright `45` s par stratégie, Wayback `20` s, Common Crawl `25` s,
Memento `15` s, archive.ph `15` s, titre exact/URL alternative/syndication/newsletter `15` s,
PDF/OCR/provider tiers `20` s, Bridge discovery `1` s, transcription Bridge `90` s.

Une allowlist `COLLECTION_ALLOWED_DOMAINS` vide autorise toute destination **publique** ;
`COLLECTION_BLOCKED_DOMAINS` gagne toujours. Les deux sont appliquées par le transport HTTP et
par le proxy navigateur. Le choix d’une destination publique n’autorise jamais une adresse
privée, locale, link-local ou de métadonnées.

## Sécurité et dépendances

- Tous les fetchs HTTP revalident chaque redirection, résolvent puis épinglent une IP publique,
  refusent les credentials dans l’URL et appliquent les domaines, tailles et délais. Une erreur
  SSRF ou de domaine est terminale ; aucune stratégie suivante ne contourne ce refus.
- Le profil Compose `playwright-acquisition` fournit le renderer Chromium sur socket Unix, un
  réseau interne dédié `acquisition-browser` et le proxy egress filtrant. Le renderer n’a pas
  d’autre sortie réseau. Compose alimente `BROWSER_EGRESS_ALLOWED_DOMAINS` et
  `BROWSER_EGRESS_BLOCKED_DOMAINS` depuis la policy `COLLECTION_*`. `BROWSER_EGRESS_PROXY_URL`
  est obligatoire pour naviguer ; sans proxy,
  la stratégie est `BLOCKED_BY_SECURITY`. Sans le profil, le montage UDS et le proxy fonctionnels,
  garder `COLLECTION_BROWSER_NAVIGATION_ENABLED=false`. Le rendu de HTML déjà collecté reste
  disponible en mode hors ligne et bloque les requêtes secondaires. Pas de furtivité, résolution
  de CAPTCHA, rotation de proxy, contournement de connexion ni usage de credentials.
- Les archives publiques reçoivent uniquement une URL d’origine sans credentials et passent par
  les contrôles de destination. Provider tiers et Bridge exigent la permission de divulgation
  (`external_llm_allowed`) et un TLP admissible ; TLP RED est refusé. L’absence de permission
  produit une tentative de policy refusée, pas un envoi.
- Bridge source discovery et transcription utilisent des parseurs de réponse tolérants et gardent
  les identifiants du run/de consultation et les hashes. Le résultat de découverte modèle lui-même
  est `model_generated=true`, `primary_source_verified=false`, `ioc_primary_verified=false`,
  `evidence_class=model_contextual_unverified`. Un article retrouvé est ensuite téléchargé et
  évalué séparément, avec la provenance du run de découverte. Une transcription est toujours
  `model_generated=true`, `primary_source_verified=false`, `ioc_primary_verified=false`,
  `evidence_class=model_contextual_unverified`. Une réponse modèle reste du contexte, jamais une
  preuve primaire. Les prompts, corps, URLs à credentials et secrets ne vont pas dans les logs.
- OCR n’est pas activé par défaut. La commande reçoit l’artefact en stdin, avec seulement
  `PATH`, `LANG` et `HOME` dans son environnement, sans stderr conservé ; l’entrée est bornée à
  25 MiB, la sortie à 5 MiB et l’exécution au timeout de stratégie. Le résultat reste
  `OCR_DERIVED`/`OCR_TEXT`, contextualisé jusqu’à vérification indépendante.
- Le collector tiers est désactivé par défaut. Le transport `SafeHttpCollector` retire
  actuellement `Authorization` : si provider, URL et jeton sont configurés et la stratégie
  activée, elle est `BLOCKED_BY_SECURITY` (`provider_auth_transport_unavailable`). Aucun jeton
  n’est transmis. La stratégie n’envoie que l’URL publique canonique et le titre après policy.

## Capacité, dépendance et mode d’échec

| Méthode/groupe | État par défaut et dépendance | Coût/failure observables |
|---|---|---|
| Profils HTTP, représentations, flux, API éditeur, données structurées | Implémentés ; SafeHttpCollector requis. | Requêtes HTTP bornées ; `failed`, `rejected`, `blocked` ou timeout avec raison. |
| `http2`, `curl` | Transport non implémenté : `transport_not_supported_by_pinned_adapter` / `curl_adapter_not_available_with_same_dns_pinning`. | Registre et probes indiquent `NOT_IMPLEMENTED`, avec résultat `skipped` et cette raison ; aucun téléchargement. |
| Chromium DOM/réseau/état | Navigation désactivée par défaut ; sidecar, UDS et proxy filtrant requis. | CPU + sorties réseau contrôlées ; `BLOCKED_BY_SECURITY`, indisponible ou timeout. Le mode offline HTML reste sûr. |
| Firefox/WebKit | Non implémentés (`browser_engine_not_implemented`). | Probe `NOT_IMPLEMENTED`, pas de moteur de remplacement. |
| Wayback, Common Crawl WARC, Memento, archive.ph | Implémentés ; endpoints publics et collector sûrs requis. | Requêtes de recherche puis snapshot ; absence de capture, refus de policy ou timeout. |
| Recherche titre exact | `DISABLED_BY_CONFIGURATION` sans `COLLECTION_EXACT_TITLE_SEARCH_ENDPOINT` (`search_endpoint_not_configured`). | Pas de requête sans endpoint ; aucun candidat devient une preuve sans capture. |
| URL alternative éditeur | Implémentée ; sitemap/URLs publiques et policy requises. | Requêtes éditeur bornées ; candidat absent ou rejet qualité. |
| Syndication autorisée | Implémentée seulement pour domaine explicitement autorisé et éditeur concordant. | Requête/copie publique ; domaine non listé ou licence/provenance inconnue = refus/équivalence inconnue. |
| PDF éditeur | Implémenté avec dépendance PDF disponible ; sinon `DEPENDENCY_UNAVAILABLE` (`pdf_extractor_not_configured`). | CPU et parsing ; type non PDF, lien absent ou parse impossible. |
| OCR visuel | `DISABLED_BY_CONFIGURATION` par défaut ; commande absente = `DEPENDENCY_UNAVAILABLE` (`ocr_not_configured`). | CPU local ; timeout, erreur ou sortie > 5 MiB. |
| Archive newsletter officielle | `DISABLED_BY_CONFIGURATION` sans endpoint (`official_archive_not_configured`). | Requête publique bornée ; endpoint non officiel (`archive_endpoint_not_official`) ou contenu non pertinent. |
| Extraction tierce | Désactivée par défaut (`provider_not_configured`) ; transport d’authentification non supporté. | Divulgation/coût fournisseur évités ; avec URL/jeton et feature activée, `BLOCKED_BY_SECURITY` (`provider_auth_transport_unavailable`). |
| Bridge discovery / transcription | Discovery désactivée par défaut ; transcription exige Bridge joignable et gate TLP/divulgation. | Appel modèle et coût de consultation ; refus policy, indisponibilité ou parse invalide. Résultat non primaire. |

Les probes distinguent notamment `IMPLEMENTED`, `DISABLED_BY_CONFIGURATION`,
`DEPENDENCY_UNAVAILABLE`, `BLOCKED_BY_SECURITY` et `NOT_IMPLEMENTED`. Un refus de policy est
également conservé comme tel ; il ne signifie pas que la méthode manque.

## Décision pratique

| Situation initiale | Méthodes permises au repli explicite | Résultat attendu / raison | Vue opérateur |
|---|---|---|---|
| `403` | Profils HTTP, représentations éditeur, browser si activé, archives, copies autorisées, puis providers/Bridge sous gates. | Une page de blocage ou CAPTCHA est rejetée ; aucun contournement. Le moteur poursuit dans son budget. | `latest_attempt` et raisons par stratégie ; `BLOCKED`/`REJECTED` si aucune copie valide. |
| `404` | Représentations, URL éditeur alternative, recherche titre configurée, archives. | Copie/capture admissible ou `unavailable`/absence de candidat. | Méthode, outcome, raison et URL finale dans la tentative. |
| Article JavaScript seulement | Données structurées/état déjà embarqué ; navigation Chromium avec proxy si activée. | Sans navigation sûre, skips explicites ; DOM/réponse JSON ne vaut que selon sa classe de preuve. | Capacité et raison `browser_navigation_disabled`/`browser_unavailable`, ou reçu d’archive. |
| `200` vide, court ou hors sujet | Extracteurs déterministes, représentation, PDF/OCR si artefact fourni, archives. | `rejected` selon qualité/titre ; le statut HTTP 200 ne suffit pas. | `quality_score`, `quality_reasons`, outcome et hashes. |
| `429` | Profil `http_retry_after`, puis sources publiques ultérieures si budget restant. | Respect borné de `Retry-After`; fin de budget si délais épuisés. | Durée, status HTTP et raison dans les tentatives. |
| `5xx` | Profils/réessais bornés puis archives et autres stratégies autorisées. | Erreur temporaire ou aucune archive ; pas de succès supposé. | `failed`/timeout et raison exacte. |
| SSRF, credential URL ou domaine bloqué | Aucun contournement ni repli vers une destination refusée. | Blocage terminal (`BLOCKED_BY_POLICY`/raison de destination). | Tentative refusée et raison ; pas de body ni secret dans les logs. |
| Archive directe/humaine déjà attachée | Aucun fallback de remplacement. | Le job est refusé (`409`) ; l’archive canonique reste attachée. | `state`, `source_document_id` et reçu du document existant. |
| CORE disponible seulement via Bridge | Aucun statut primaire ne peut être déduit du texte Bridge. | `NEEDS_REVIEW` avec `CRITICAL_SOURCE_MISSING`, même si des IOC confirmés existent ailleurs. | `model_generated`, classe contextualisée et identité de consultation. |

## Classes de preuve et routage

La méthode décrit l’acquisition ; l’équivalence décrit le lien avec la publication ; la classe
de preuve décrit l’autorité des octets. `archived` seul ne veut pas dire primaire.

| Valeur/code | Lecture opérateur |
|---|---|
| `exact_original` / `primary_observed` | Observation directe de l’original ; vérifier l’URL et l’éditeur. |
| `historical_original` / `historical_primary` | Copie d’archive datée de l’original ; l’horodatage historique est conservé. |
| `verified_publisher_equivalent` / `verified_equivalent` | Variante éditeur vérifiée (AMP/print/mobile/API, etc.). |
| `authorized_republication` | Républication dont l’autorisation/lien éditeur est établi. |
| `independent_corroboration` | Source indépendante utile pour corroborer, jamais promue en CORE par elle-même. |
| `ocr_derived` | Texte dérivé d’image/PDF ; contexte tant que non vérifié sur une source primaire. |
| `model_contextual_unverified` | Découverte/transcription modèle ; non primaire et non vérifiée. |

Un CORE dont l’unique source exploitable est Bridge, OCR ou extraction tierce impose
`NEEDS_REVIEW`/`CRITICAL_SOURCE_MISSING`. Les IOC contextuels ne lèvent pas ce blocage, même si
d’autres IOC ont été confirmés. Une preuve indépendante ou une archive primaire doit être
acquise puis vérifiée avant toute promotion.

## Persistance, migrations et couverture hors ligne

Chaque tentative porte `strategy_name`, `strategy_version`, `acquisition_method`, outcome et
raison, équivalence, classe de preuve, observation, qualité et hashes. Les reçus/provenances
conservent les références raw et decoded, `encoded_sha256`, `decoded_sha256`, temps de snapshot,
acteur/job, et pour les modèles `model_run_id`, `consultation_id`, `prompt_sha256`,
`response_sha256`, `model_generated`, `primary_source_verified`, `ioc_primary_verified`.
PostgreSQL, les événements de provenance et les blobs content-addressed sont canoniques ; les
artefacts/hashes anciens ne sont ni réécrits ni supprimés par l’ajout de ces champs.

La migration additive `0008b_version_length` (fichier `0008b_alembic_version_length.py`) prépare
la longueur de version, `0009_acquisition_strategy_attempts` ajoute les métadonnées de stratégie
aux tentatives et `0010_acquisition_provenance` ajoute les classes,
la qualité, le temps de snapshot et les provenances modèle aux tentatives qui portaient déjà les
hashes de contenu. Les gardes de migration refusent les
schémas partiels et le downgrade refuse de supprimer les données de provenance.

La matrice hors ligne de 24 cas est dans `backend/tests/test_acquisition_matrix.py`. Les cas 14
et 18 sont partiels : la policy d’acquisition ne modélise pas d’autorité G2/G3 explicite ; le cas
18 vérifie le bail de service en mémoire, pas un verrou de ligne PostgreSQL. La course
fallback/fallback a un test PostgreSQL ; la course manuelle/fallback n’a qu’un test en mémoire.
Ces tests n’établissent pas une disponibilité réseau en production.
