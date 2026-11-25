# M24.4 — Analyse des causes racines

Base : `main@1406a040`. Observations : voir `M24-4-INCIDENT-BASELINE.md`.
Verdicts : **confirmée / infirmée / indéterminée**. Les références `fichier:ligne`
proviennent d'audits en lecture seule du code à ce SHA ; les lignes peuvent bouger.

## Synthèse des 7 arrêts observés

| Arrêt | Run(s) | Cause racine | Classe | Défaut de conception ? |
|---|---|---|---|---|
| Routage — CORE inaccessible (FT, nia, WSJ) | 692a800a, 93e8a331 | sources CORE réellement `blocked/access_blocked` ; 0 IOC confirmé → blocage légitime | A | **Oui** : motif générique `critical_source_missing`, aucune cause lisible ; la dérogation `CONTINUE_WITHOUT_SOURCE` n'est pas lue par le routage → impasse |
| Routage — EUR-Lex « illisible » | c7d54013, 9767436e | HTTP **202** + page `text/html` de 2035 o archivée comme `succeeded`/`archived` ; l'extraction la déclare à juste titre illisible | D | **Oui** : l'acquisition promeut une réponse non documentaire |
| Synthèse corrective | a38a4cc5 | la requête corrective ne contient pas la proposition initiale | — | **Oui** (H2) |
| Brief sans chronologie | 8c10e585 | routage `brief` sans prédicat de faisabilité (snapshot non chargé) | — | **Oui** (H3) |
| Bridge | 0f7c7807 | onglet fermé après soumission ; réconciliation obligatoire | — | Non pour le blocage (conforme) ; défaut annexe : adoption concurrente non idempotente |

## H1 — Routage : **confirmée, mais le scénario « provenance alternative » n'explique aucun cas réel**

- Les deux causes (A inaccessible, D acquisition invalide) partagent bien le même
  motif `critical_source_missing` (`domain/production_routing.py`,
  `decide_publication_routing()`).
- **Défaut latent (classe B)** : une source CORE présente avec
  `INDEPENDENT_CORROBORATION / BRIDGE_TRANSCRIPTION / VISUAL_OCR / THIRD_PARTY_EXTRACTION`
  force `critical_source_missing` **même avec un IOC confirmé**
  (`production_routing.py:~329`, priorité sur `:~506`) ; aucun chemin de décision
  humaine n'accepte cette provenance. Non observé dans ce run : à corriger sans
  l'assouplir (motif distinct + vérification adaptée), pas à titre de remède aux 4 arrêts.
- **Impasse de dérogation** : `CONTINUE_WITHOUT_SOURCE` exige motif et diagnostics
  (`production_repairs.py:~708`) mais le routage ne reçoit pas les décisions de réparation
  (`application/production_routing.py:~76`) ; pour `SOURCE_EXTRACTION_FAILED` l'impact
  est `NO_DELIVERABLE_CHANGE` et aucun re-routage n'a lieu (`production_repairs.py:~4371`).
  Une dérogation valide ne débloque donc jamais le sujet.
- **Source de `source_loss_diagnostics=[]`** : le workflow n'émet d'entrée de perte
  que pour les omissions échouées avec `error_code` (`production_workflow.py:~451`) ;
  les omissions `reference_not_eligible` n'en ont pas, le routeur ajoute seul le
  diagnostic CORE. (Corrélation avec le journal : **inférée**.)
- `publication_routing_needs_review` est un code d'étape générique pour tout routage
  `NEEDS_REVIEW` (`production_workflow.py:~834`) : il ne dit rien de la cause.
- Couverture : unitaires riches (`test_production_routing.py`), PostgreSQL limité ;
  aucun test de dérogation, de source corrigée puis re-routée, ni de diagnostics vides.

## H2 — Synthèse corrective : **confirmée**

- `_build_synthesis_corrective_request()` (`production_synthesis.py:~1617/1758`)
  ajoute les violations à `request.text` ; la réponse brute initiale (`raw_text`,
  déjà archivée, hachée et vérifiée, `:~5139`) n'est pas transmise (`conversation=None`).
  Observé en production : le modèle réclame lui-même la proposition.
- Aucune borne d'entrée applicative ; une seule correction (`synthesis_corrective_turn=1`).
- Défauts voisins : réparation d'annotations sémantiques tronquée à `raw_text[:8000]`
  (`production_editorial_enrichment.py:~8040`) ; `BridgeInvestigator.turn` n'ajoute
  pas la réponse rejetée (`bridge_investigator.py:~1746`). Le premier est une
  troncature silencieuse contraire à la décision du mandat (à traiter ou borner
  explicitement) ; le second est hors périmètre pipeline (consigné).

## H3 — Brief : **confirmée**

`decide_publication_routing()` n'accepte ni snapshot ni verdict de faisabilité ;
la synthèse appelle `brief_timeline_from_extraction(extraction, relevance, snapshot)`
(`production_brief_synthesis.py:~142/276`) puis s'arrête avant l'appel modèle
(`production_synthesis.py:~4390`). Le routage dispose des artefacts mais pas du
snapshot (repli sur les métadonnées de publication). Correction : charger le snapshot
au routage et réutiliser **le même prédicat** ; ne pas fabriquer de date ni
rebasculer en article.

## H4 — Collectes complémentaires : **confirmée (4 défauts distincts)**

1. **État `pending` des événements `q1`** : instantané périmé. Le workflow capture
   `collection.state` avant `collect_subject` (`production_workflow.py:~1033/1065`) et
   l'inscrit en tête de l'événement ; les `details` sont relus après. L'autorité
   canonique est PostgreSQL (`source_collections.state`, `collection_attempts`) : **le
   défaut est dans la projection diagnostique, pas dans l'état durable**.
2. **HTTP 202 traité comme succès** : classe D ci-dessus (observé).
3. **Retries d'échecs déterministes** : `BLOCKED`/`UNAVAILABLE` rentrent à nouveau
   dans la chaîne de fallback à chaque passe (jusqu'à 40 tentatives / 120 s par
   passe, `acquisition_fallbacks.py:~1160`, `collection.py:~503`) → 41–88 tentatives
   cumulées par source (observé : `embedded_structured_data` 80, `representations`
   64, `publisher_alternative_url` 48, `wayback`, `commoncrawl`…). Le `dns_error`
   n'a pas de plafond global entre passes.
4. **Fallback sur corps 401/403** : le corps déjà reçu peut être transmis aux
   stratégies de rendu (`http_collection.py:~324`, `acquisition_fallbacks.py:~948`) ;
   des tests montrent un corps 403 rendu puis archivé quand `PlaywrightStrategy` est
   configurée. **Non observé dans ce run** (stratégies Playwright `skipped`) ;
   latent, contraire à « ne jamais considérer une page 401/403 comme archive ».
- `www.www.semperis.com` : **indéterminé**. Le code suit `Location` via `urljoin`,
  ne préfixe `www.` que dans `archive_url_variants` (idempotent, une seule fois,
  `acquisition_fallbacks.py:~3475`) et la canonicalisation préserve les labels `www`.
  Aucune transformation locale ne peut produire `www.www`. L'en-tête `Location` n'est
  pas persisté (`allowed_headers` = `content-type`, `content-length`) : l'hypothèse
  « réponse du serveur distant » est la seule compatible avec le code, mais
  **non prouvée**. Correction d'observabilité : persister `Location` des redirections.
- Un échec de collecte complémentaire n'échoue pas le run (avertissement,
  `production_workflow.py:~1056`) : **infirmé comme risque** ; un test d'intégration
  existe (supplémentaire 404 → run `ready`).

## H5 — Bridge : **partiellement confirmée**

- Garantie conservée : jamais de seconde POST sur soumission ambiguë
  (`model_gateway.py:~1341/1550`, `production_recovery.py:~82` force `MANUAL_ONLY`).
  États couverts : non soumis ; soumis/inconnu (rejeu scellé) ; réponse retrouvée
  (adoption hachée) ; indisponible (exige un signal de terminaison vérifié, ou
  `declare-lost` humain) ; déjà adoptée (rejeu séquentiel idempotent).
- **Défaut vérifié** : l'adoption concurrente de la même réponse n'est pas idempotente
  à la frontière `ModelRun` (`model_gateway.py:~763/801`) : le second appelant peut
  lever une erreur. Pas de second appel fournisseur.
- Le contournement humain `declare-lost` peut créer une nouvelle génération : voulu,
  à documenter dans le playbook.

## Lots correctifs retenus

| Lot | Objet | Priorité |
|---|---|---|
| L1 | requête corrective avec proposition initiale + budget + échec explicite | P0 |
| L2a | motifs de routage versionnés et diagnostics enrichis (A/B/D) | P0 |
| L2b | consommation d'une dérogation humaine par le routage (à concevoir) | P0 |
| L3 | faisabilité *brief* au routage (snapshot + même prédicat) | P1 |
| L4a | HTTP 202 non archivable ; persistance de `Location` | P1 |
| L4b | état `q1` rafraîchi ; plus de rejeu de fallback sur échec déterministe épuisé ; 401/403 jamais archivé | P1 |
| L5 | adoption Bridge concurrente idempotente | P1 |
| L6 | cockpit : cause, action, conséquences | P1 |
| L7 | contrat de diagnostic structuré + rapport/playbook | P2 |
