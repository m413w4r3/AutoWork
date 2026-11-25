# M24.4 — Baseline d'incident (premier run réel, novembre 2026)

Statut : **Gate A — diagnostic** (lecture seule, aucune donnée modifiée).
Base de code : `main@1406a0401f0edb1df9cd381f3c7f7e23a13d6cb3`.
Instantané : 2026-11-25 ≈ 13:40 UTC+1 (la production tourne encore ; les états
`queued`/`running` peuvent avoir changé depuis).

## 1. Sources et limites d'observabilité

| Source | Accès | Usage |
|---|---|---|
| PostgreSQL de l'instance (`production_runs`, `production_routing_decisions`, `source_collections`, `collection_attempts`, `model_runs`) | lecture seule (`BEGIN READ ONLY`), depuis l'hôte | état canonique, décisions, tentatives |
| `var/diagnostics/events.jsonl` (67 événements) | lecture, accès explicitement autorisé par l'utilisateur | chronologie, codes d'erreur d'étape |
| `var/diagnostics/runs/<run>/…traceback.txt` | lecture | cause Bridge du run `0f7c7807` |
| Captures d'écran de l'interface (12 sujets) | **non disponibles** | — |
| Blobs MinIO (corps des archives, réponses brutes des modèles) | **non lus** | hors périmètre de cette phase |

Le journal fourni dans le mandat comptait 63 événements ; le fichier local en a 67
(4 événements postérieurs, dont le run `4d972fc8` en cours). Les 4 sujets non couverts
par le journal d'origine sont réellement `queued` (3) ou `running` (1) en base :
**aucun résultat n'est extrapolé pour eux**.

Marquage : **[O]** observé (table/journal), **[I]** inféré (code + observations),
**non observable** = pas de preuve disponible.

## 2. Matrice des 12 sujets

| # | Sujet | Run | Gén. | État / étape | Code d'arrêt | Cause sous-jacente | Preuve |
|---|---|---|---|---|---|---|---|
| 1 | f2f4e1b6 | 6ac5e5fa | 0 | `queued` / sources | — | pas encore démarré | [O] `production_runs` |
| 2 | 153e789c | fc6501f4 | 0 | `queued` / sources | — | pas encore démarré | [O] |
| 3 | 3c651625 | 98f95481 | 0 | `queued` / sources | — | pas encore démarré | [O] |
| 4 | abd21ee0 | 4d972fc8 | 0 | `running` / extraction | — | en cours (sources, références réussies) | [O] journal 12:34–12:36 |
| 5 | d9efaf00 | 29f2b81d | 0 | `ready` / assembly | — | production terminée ; article, 1 IOC confirmé. CORE Semperis non collectée (`dns_error`, voir §4) mais décision article maintenue grâce à l'IOC confirmé | [O] routage `subject_ioc_confirmed` |
| 6 | 4f09a00b | 93e8a331 | 0 | `needs_review` / relevance_projection | `publication_routing_needs_review` → `critical_source_missing` | CORE `wsj.com` inaccessible (`access_blocked`, 84 tentatives cumulées) ; 0 IOC confirmé, 0 contextuel | [O] routage + `source_collections` |
| 7 | 89fba5eb | 0f7c7807 | 0 | `needs_review` / extraction | `model_submission_reconciliation_required` | onglet ChatGPT du Bridge fermé après soumission (`bridge_extension_disconnected`, `BridgeTransportError`), état externe inconnu | [O] `error_details`, traceback |
| 8 | 3de84f3a | 9767436e | 0 | `needs_review` / relevance_projection | idem → `critical_source_missing` | source EUR-Lex (rôle `counter_analysis`, critique) « archivée » mais illisible (`extraction_source_text_unreadable`) ; 59 IOC contextuels, 0 confirmé | [O] |
| 9 | ecf68196 | 692a800a | 0 | `needs_review` / relevance_projection | idem | CORE `ft.com` et `nia.go.th` inaccessibles (`access_blocked`, 88 tentatives) ; 3 contextuels, 0 confirmé | [O] |
| 10 | f8c1561d | c7d54013 | 0 | `needs_review` / relevance_projection | idem | source EUR-Lex PDF (rôle `primary`, critique) « archivée » mais illisible ; 2 contextuels, 0 confirmé | [O] |
| 11 | c46b35bc | a38a4cc5 | 0 | `needs_review` / synthesis | `synthesis_output_invalid` (correction `synthesis_lead_missing`) | 1ʳᵉ synthèse rejetée (`synthesis_unknown_technical_value`) ; le modèle correctif répond ne pas avoir « la proposition précédente » | [O] `error_details.diagnostics.missing_coverage` |
| 12 | f1ed6fdf | 8c10e585 | 0 | `needs_review` / synthesis | `brief_chronology_unavailable` | routage `brief / no_subject_ioc` (0 IOC) ; synthèse : `dated_timeline_entry_count: 0` | [O] `error_details` |

Total : 1 terminé, 7 `needs_review` (4 routage, 1 synthèse article, 1 brief, 1 Bridge),
3 `queued`, 1 `running`.

## 3. Arrêts de routage (4) — causes observées

Politique : `subject-publication-routing-v2-bridge-evidence-v1`. Les quatre décisions
portent `reason_code=critical_source_missing`, mais deux situations distinctes :

| Run | Source concernée | `failure_code` du diagnostic | État de collecte réel |
|---|---|---|---|
| 692a800a | CORE `ft.com`, CORE `nia.go.th` | `reference_not_eligible` | `blocked / access_blocked` (88 tentatives) — **accès refusé** |
| 93e8a331 | CORE `wsj.com` | `reference_not_eligible` | `blocked / access_blocked` (84) — **accès refusé** |
| c7d54013 | `eur-lex …/TXT/PDF` (supporting, critique car `primary_role`) | `extraction_source_text_unreadable` | `archived`, mais **HTTP 202, `text/html`, 2035 octets** |
| 9767436e | `eur-lex …/TXT` (supporting, critique car `counter_analysis`) | `extraction_source_text_unreadable` | `archived`, mais **HTTP 202, `text/html`, 2035 octets** |

Constats :

- **[O]** Aucun des quatre cas n'est une « source présente avec une provenance
  alternative » : le chemin `INDEPENDENT_CORROBORATION / BRIDGE_TRANSCRIPTION /
  VISUAL_OCR / THIRD_PARTY_EXTRACTION` n'est pas en cause dans ce run.
- **[O]** Les deux EUR-Lex ont reçu une réponse **HTTP 202** (même page de 2035 octets
  pour les deux URL) enregistrée comme `archived`. Le contenu n'est pas un document :
  c'est un défaut d'acquisition (classe D), pas un défaut de routage.
- **[I]** Les deux routages sans entrée de perte (`source_loss_diagnostics=[]` dans le
  mandat) sont FT/nia et WSJ : l'omission `reference_not_eligible` n'émet pas
  d'entrée de perte dans le workflow ; seul le routeur ajoute le diagnostic CORE
  (cf. RCA).
- **[O]** Les décisions `29f2b81d` (CORE Semperis non collectée) et `a38a4cc5`
  (CORE `thehackernews.com` bloquée) portent le même diagnostic CORE mais ont été
  décidées en `article` car un IOC sujet est confirmé : la politique distingue donc
  déjà « CORE manquante + IOC confirmé » de « CORE manquante sans IOC confirmé ».

## 4. Collectes complémentaires (17 événements `q1.supplemental_collection_failed`)

| Catégorie | Nb | Journal | Base (`source_collections`) |
|---|---|---|---|
| `access_blocked` (401/403) | 12 | `collection_state: blocked` (4) ou `pending` (8) | `blocked`, tentatives cumulées 41–88 |
| `obsolete_url_404` | 2 | `unavailable` (1) ou `pending` (1) | `unavailable`, tentatives 86 et 44 |
| `dns_error` (Semperis) | 1 | `failed_retryable`, `retry_attempted:true` | `failed_retryable`, 2 tentatives |
| `timeout` (PDF, HTTP 200) | 1 | `pending` | `failed_terminal`, 1 tentative |
| `non_pdf_content` (HTTP 200) | 1 | `pending` | `failed_terminal`, 1 tentative |

Le même sujet peut donc avoir, pour la même catégorie, un état d'événement `pending`
dans un cas et `blocked`/`unavailable` dans un autre : l'état de tête de l'événement
n'est pas dérivé de l'état terminal durable.

- **[O]** 11 événements ont `collection_state: "pending"` en tête alors que leurs
  `details` portent des compteurs terminaux (`blocked`, `unavailable`,
  `failed_terminal`) et que la base connaît l'état terminal. `error_code` est
  toujours le générique `source_collection_no_success`, `error_type: JobHandlerError`.
- **[O]** `www.www.semperis.com` : `requested_url=https://www.semperis.com/` (racine
  du site, pas l'article), chaîne `http://www.semperis.com/ → https://www.www.semperis.com/`,
  réponse 301 de 134 octets, `candidate_resolution_provenance=same_site_http_redirect`.
  L'origine (réponse réelle du serveur ou résolution locale) n'est **pas** tranchée
  par les données observées → voir RCA.
- **[O]** Les tentatives cumulées de 41–88 sur des sources bloquées contredisent
  « ne pas multiplier les retries d'échecs déterministes » si le compteur reflète
  des tentatives réelles ; sémantique du compteur à établir (RCA).

## 5. Synthèse corrective (run a38a4cc5)

- **[O]** `error_details` : `rejections=[LEAD / synthesis_lead_missing]`,
  `missing_coverage` = « La proposition précédente n'est pas présente dans le texte
  transmis… Transmets la proposition complète précédente… » : le modèle correctif
  déclare lui-même ne pas avoir l'objet à corriger.
- **[O]** Deux `model_runs` associés (initial + correctif) ; `model_run_id` correctif
  `4d5e077b-3607-547e-8914-f1f58287cd64`.

## 6. Brief (run 8c10e585)

- **[O]** Routage : `brief / no_subject_ioc`, 0 IOC confirmé/contextuel/ambigu.
- **[O]** Synthèse : diagnostic `brief_timeline_requires_1_to_4_source_dated_entries`,
  `dated_timeline_entry_count: 0`, arrêt avant tout appel modèle.

## 7. Réconciliation Bridge (run 0f7c7807)

- **[O]** `reconciliation_submission_state=external_state_unknown`, phase
  `reconciliation`, étape `extraction`, `submission_state=post_submission`,
  1 tentative, `retryable:false`, `bridge_status=failed`,
  `diagnostic_code=bridge_extension_disconnected`, `tab_state.exists=false`.
- **[O]** Traceback : `BridgeTransportError: l'onglet ChatGPT lié (ou sa fenêtre
  dédiée) a été fermé pendant le run`.
- Le refus de réémission automatique est conforme à l'exigence de conservation.

## 8. Reproductibilité

Requêtes (lecture seule, depuis l'hôte, sans secrets) : tables `production_runs`,
`production_routing_decisions` (`critical_source_diagnostics`, `reason_code`),
`source_collections` (`state`, `failure_reason_code`, `attempt_count`),
`collection_attempts` (`http_status`, `final_url`, `redirect_chain`,
`candidate_resolution_provenance`), `model_runs`. Aucune donnée d'évidence ni aucun
credential n'est reproduit dans ce document.
