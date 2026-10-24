# Mission 03 / Phase 1 — Options de conception

**SHA étudié :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Branche :** `mission03/architecture-design`
**Nature :** sélection M5 et cadrage M6. Aucune implémentation de production.

## 1. Règle de décision

Chaque candidat est comparé à **KEEP_EXISTING + correctifs étroits**. Un
`REPLACE` n'est retenu que si la preuve couvre les huit points de charge :
problème observable, appelants affectés, invariants dispersés, nouveau
propriétaire unique, obligations retirées aux appelants, code supprimable,
nouvel oracle, coût de migration et risque de régression.

La taille d'un fichier n'est pas un argument. Une interface courte qui
reporte les invariants sur les appelants est jugée superficielle. Un wrapper
supplémentaire autour d'une logique inchangée est rejeté.

Deux seams au maximum sont soumis à DESIGN-IT-TWICE : **S3** et **advisor**.

## 2. Inventaire et classification des candidats

| Candidat | Classe | Preuve principale | Comparaison à KEEP_EXISTING | Décision M5 |
|---|---|---|---|---|
| Décision de dispatch et identité d'effet externe | **REPLACE_CANDIDATE** ; statut courant `BLOCKED_DESIGN` | `M1A` A1–A10 ; `M3` 12 questions ; `M2A` 0/10 oracles de consume | KEEP + callback obligatoire et recheck d'invocation fermerait la course, mais ne fournit ni identité normalisée, ni consume, ni portée Bridge/retry, ni fail-closed des entrées directes | **Seam 1** — conception puis revue |
| Consultation advisor préparée + parseur sémantique | **REPLACE_CANDIDATE** | `M1B` §refs/parseurs ; `M4A` §parser cluster et timing ; `M2B` oracles 1/3/6 | KEEP + supprimer le dossier recalculé et ajouter un corpus de parse corrige des défauts locaux, mais ne donne pas d'objet préparé liant octets/refs/hash avant le point de décision S3, ni un propriétaire unique du parse/reparse | **Seam 2** — conception puis revue |
| `advisor_dossier` (extraction/sélection/manifeste/complétude) | **KEEP** | `M4A` §KEEP ; fonctions pures, tests nombreux | Aucun appelant ne réimplémente la sélection ; le remplacer ajouterait une couche | KEEP |
| `submit_bridge_consultation_once` / transport Bridge | **KEEP** | `M4A` §KEEP ; adapter production + scripté | Le seam cache pacing, idempotence, retry et ambiguïté ; le remplacer dupliquerait un comportement déjà localisé | KEEP (le seam S3 doit s'y interfacer, pas le réimplémenter) |
| `AnalystOpinionService` / `AnalystOpinionV1` | **KEEP** | `M4A` §KEEP ; versions/hash/citations | Validation indépendante avant écriture d'avis ; la supprimer ferait confiance au parseur seul | KEEP |
| P10 — credential JSON échappé | **NARROW_FIX** | `M4A` §S2 ; `invariant_proposals.py:2301-2364` | Un matcher local et un test de rendu suffisent ; un remplacement du normaliseur risquerait d'élargir la perte de preuves CTI | Lot étroit |
| P10 — refus effaçable | **NARROW_FIX** | `M1B` §refusal gap ; `invariant_proposals.py:1226-1342` ; `domain/invariant_proposals.py:229-243` | Modéliser le refus comme outcome et bloquer les candidats ; pas besoin d'un nouveau module | Lot étroit |
| P10 — faux positifs de redaction CTI | **NARROW_FIX** | `M4A` §duplication ; `M2B` oracle manquant | Ajouter un oracle positif de preuve CTI et restreindre les libellés ; pas de fusion spéculative avec l'advisor | Lot étroit |
| Métadonnées d'échantillon restreint (advisor) | **BLOCKED_DESIGN** (décision de politique) | `M1B` §restricted sample metadata | Impossible de trancher sans décision humaine sur la portée « métadonnées vs contenu » | Décision requise avant tout code |
| Lecteur PostgreSQL d'audit | **NARROW_FIX** (oracle) | `M4C` §NARROW_FIX ; `investigation_audit_reader.py:1-4` | Le reader et le pure exporter ont déjà chacun un propriétaire ; un facade serait superficiel | Lot de test |
| Projections contexte full vs paged | **NARROW_FIX** | `M1C` C1/C2 ; `M4C` §NARROW_FIX | `ResearchContextBuilder` possède déjà la profondeur ; aligner les deux chemins sans nouveau wrapper | Lot étroit + oracle |
| Identité de page/cursor opérateur | **BLOCKED_DESIGN** | `M4C` §BLOCKED_DESIGN | Le contrat « lignes courantes sous CAS » vs « source figée » n'est pas tranché ; changer le curseur v1 peut invalider les clients | Décision de contrat |
| `context_pagination` + repositories d'entités | **KEEP** | `M4C` §KEEP | Les preuves de page et les seek SQL sont déjà partagés ; un merge ne supprimerait pas de code | KEEP |
| `PublicationRenderService.render_pdf` | **KEEP** | `M1D` §KEEP | Seam d'appel profond : un artifact ID cache validation, projection, médias, compilation, persistance, provenance | KEEP |
| Projection Typst / complétude canonique → PDF | **NARROW_FIX** | `M1D` D1 ; `M2B` oracle #7 | Le renderer est partagé ; ajouter un oracle de complétude et corriger la lineage, sans remplacer le seam | Lots étroits |
| Lineage reviewer/approver dans le PDF | **NARROW_FIX** | `M1D` D2 | Ajouter les acteurs/hash de base à la lineage | Lot étroit |
| Preview `protocol_model_blob_id` | **DEFER** | `M4A` §opinion provenance | Question de lineage de preview, sans consommateur démontré | DEFER |
| Reparse `suricata_draft` | **NARROW_FIX** | `M1B` §reparse | Ajouter le type accepté ou documenter l'exclusion | Lot étroit |
| Consolidation cross-path disclosure/redaction | **DEFER** | `M4A` §DEFER | Les politiques ont des entrées différentes (snapshot/enveloppe vs échantillons retenus) ; une fusion exigerait une matrice de faux positifs/négatifs non établie | DEFER |
| Service/route d'export d'audit dans `InvestigationService` | **DEFER** | `M4C` §DEFER | Un seul caller CLI ; déplacer le wiring serait un wrapper | DEFER |
| Parseur sémantique advisor seul | **REPLACE_CANDIDATE** (subsumé par le seam 2) | `M4A` §parser cluster | La conception du seam 2 doit inclure un contrat de parse unique ; un remplacement du seul parseur laisserait la préparation dispersée | Intégré au seam 2 |
| Frontend | non requis | `M1D`/`M4C` | Aucun appel frontend ne complique les seams retenus | Hors périmètre |

## 3. Seam 1 — décision de dispatch et identité d'effet externe

### 3.1 Problème observable

Le contrôle d'admission est un **read-then-send** : le callback relit
l'enveloppe et la réservation, puis retourne, et le dispatcher entre dans
l'adaptateur (`investigation_service.py:1190-1237` ;
`investigation_tools.py:779-784`). Il n'existe aucune transaction PostgreSQL
qui réclame une décision d'envoi unique et coordonne l'enveloppe, la
réservation, l'invocation `RUNNING` et la tentative Bridge. Un replay peut
sceller l'invocation en `NEEDS_REVIEW` alors que l'exécution originale
poursuit et dispatche encore (`operator_tool_invocation.py:863-878` ;
`investigation_service.py:1190-1226`). `normalized_parameters` contient les
arguments bruts, pas l'intent normalisé (`investigation_service.py:1031-1040`).
Aucun permis obligatoire ne couvre les entrées directes d'adapters/transports.

### 3.2 Chemin d'appel et appelants

- `OperatorToolInvocationService.invoke` → `InvestigationService.execute_tool_call`
  → `RegistryToolDispatcher.dispatch` → adapters VT/Shodan/Retrohunt/acquisition/advisor.
- `InvestigationService.run` → `BridgeInvestigator.turn` → transport modèle.
- `RetrohuntService.submit` et `HitAcquisitionService.acquire_hit` ont leurs
  propres machines d'état durables.
- Appelants de test : `test_b1_admission_durability.py`,
  `test_investigation_service.py`, `test_investigation_tools_contract.py`,
  `test_bridge_investigator.py`, `test_retrohunt_tools.py`,
  `test_investigation_acquisition*.py`.

### 3.3 Invariants non négociables

- Révocation commitée avant le consume → zéro egress.
- Consume commité d'abord → l'exécution autorisée peut partir ; une
  révocation postérieure ne l'annule pas ; les egress ultérieurs doivent être
  explicitement couverts ou refusés.
- Le consume vérifie et lie atomiquement : enveloppe+version, réservation
  exacte `RESERVED`, décision d'effet unique, invocation `RUNNING`, paramètres
  normalisés+version, tentative Bridge équivalente.
- Aucun verrou SQL tenu pendant le transport.
- Crash/timeout après consume sans issue prouvée → `NEEDS_REVIEW` ; jamais de
  resoumission automatique.
- Le réemploi préserve invocation/observation/budget ; budget chargé une fois.
- Aucune hypothèse d'idempotence/canonicalisation fournisseur.

### 3.4 Dépendances classées

| Dépendance | Catégorie | Conséquence de test |
|---|---|---|
| PostgreSQL (enveloppe, invocation, réservation, décision) | système possédé, local-substitutable | base jetable obligatoire pour les courses ; un fake mémoire ne prouve pas `FOR UPDATE`/unicité |
| Transports VT/Shodan/Retrohunt/acquisition | externes réels | doubles de transport ; zéro appel réel |
| Bridge/modèle | externe réel, avec `ModelConversationTurnTransport` + scripted | fake scripté pour les tests ; persistance de tour réelle en PG pour la coordination |
| Normalisation d'effet | in-process | tests unitaires de digest/version |
| Registre d'outils | in-process | test de classification/immutabilité |

### 3.5 Implementation actuelle et seam actuel

Le seam actuel est le couple `ToolDispatcher.dispatch` + callback optionnel
`pre_dispatch_check`. L'adaptateur reçoit le contexte et les arguments bruts ;
il n'existe ni permis, ni session de transport, ni décision persistée. Le
`ToolExecutionContext` (`investigation_tools.py:224-245`) ne porte pas
d'identité d'invocation.

### 3.6 Obligations inutilement exposées aux appelants

- Chaque appelant doit savoir que le callback est optionnel et que l'absence
  de callback n'empêche pas l'egress.
- Chaque adaptateur doit renormaliser ses paramètres et refaire ses propres
  contrôles de divulgation.
- Le service doit re-vérifier l'enveloppe, la réservation et le statut
  d'invocation à plusieurs endroits.
- Les tests doivent simuler des courses que le seam ne sérialise pas.

### 3.7 Contrats à préserver

`ToolDefinition`, `ToolResult`, `ToolInvocationStatus`, `BudgetDimension`,
`BudgetReservationStatus`, les codes d'erreur/refus, `NEEDS_REVIEW`,
`external_llm_allowed`, TLP, `do_not_submit`, `allowed_providers`,
`approval_decision_id`, les clés d'idempotence existantes, les API et les
statuts actuellement exposés.

### 3.8 Exemple de code illustrant le problème

```python
# Current shape, simplified
invocation = persist_running(operation_key, arguments)      # raw arguments
budget.reserve(operation_key, maximum_cost)
commit()                                                     # admission checkpoint
proof = await check_current_admission(ctx)                   # reads envelope/reservation only
if proof is None:
    result = await adapter.execute(arguments, ctx)           # no permit, no invocation recheck
```

Le problème n'est pas la signature seule : c'est que le droit de partir n'est
pas matérialisé, que l'identité d'effet n'est pas liée aux octets émis, et que
le point de décision ne coordonne pas les états qui peuvent changer avant
l'egress.

## 4. Seam 2 — consultation advisor préparée et parseur sémantique

### 4.1 Problème observable

L'adaptateur advisor concentre validation, limites, chargement de preuves,
restrictions d'échantillon, divulgation, projection, redaction, dossier,
complétude, dimensionnement, rendu du prompt, archive, transport, parse
typé/legacy, observation et reparse (`investigation_advisor_tools.py:164-778,
3414-3787, 4689-5105`). Les octets exacts, les refs et le hash sont connus
**après** la réservation générique, donc le seam S3 ne peut pas les lier à sa
décision. L'identité des refs est reconstruite par au moins quatre helpers
(`_typed_transmitted_refs`, `_transmitted_refs_for_dossier`,
`advisor_wire_aliases`, résolution d'alias du parseur). Le parseur typé v3 et
le parseur legacy v2 divergent ; le reparse n'utilise que le typé.

### 4.2 Chemin d'appel et appelants

- `InvestigationAdvisorTool.execute` (consultation normale).
- `ReparsePersistedAdvisorOutputTool.execute` (reparse sans consultation).
- `AnalystOpinionService` (vérification de provenance des blocs Bridge).
- `advisor_dossier` (sélection/manifeste) et
  `submit_bridge_consultation_once` (transport) sont des seams KEEP avec
  lesquels la nouvelle interface doit composer.

### 4.3 Invariants non négociables

- Preuves CTI permissives intactes : raw, hex, Base64, bytes, réseau, IOC,
  extraits longs, contradictions.
- Refus explicites préservés ; un refus n'est jamais un warning.
- Aucun plafond arbitraire, aucune troncature silencieuse, IDs/SHA/offsets/
  longueurs/alias/provenance conservés.
- Reparse sans nouvelle consultation ; sortie brute non canonique.
- Avis versionné et validé par `AnalystOpinionService`.
- Le seam ne possède ni l'autorisation, ni le consume, ni le transport : il
  prépare et parse.

### 4.4 Dépendances classées

| Dépendance | Catégorie | Conséquence de test |
|---|---|---|
| Dossier/sélection/complétude | in-process | tests purs + golden manifest |
| Redaction/secrets | in-process + settings injectés | tests de faux positifs/négatifs |
| Parseur | in-process, mais compatible avec des réponses persistées | corpus gelé v2/v3 + reparse |
| Transport | délégué à `submit_bridge_consultation_once` / S3 | fake transport ; aucun provider réel |
| Opinion | PostgreSQL canonique | tests PG existants à préserver |

### 4.5 Implementation actuelle et seam actuel

Le seam actuel est `InvestigationAdvisorTool.execute`, une méthode de plus de
1 000 lignes avec ses helpers privés. `advisor_dossier` est un seam pur
distinct, déjà profond. `submit_bridge_consultation_once` est un seam de
transport distinct. Le parseur n'a pas de contrat public unique.

### 4.6 Obligations inutilement exposées aux appelants

- Le reparse doit connaître le contexte de reparse et le parseur typé.
- L'opinion doit revérifier les refs et la provenance d'une observation
  advisor.
- Les tests doivent traverser l'adaptateur pour vérifier le dossier, les refs
  ou le parse.
- Les helpers de refs peuvent diverger sans qu'un oracle unique le détecte.

### 4.7 Contrats à préserver

`advisor-consult.v1`, `advisor-wire.v1`, `advisor-response.v2/v3`,
`advisor-source-parser.v3`, `operator-advisor-call.v1`, `analyst-opinion.v1`,
les statuts `refused`/`partial`/`invalid`/`needs_review`, les alias et refs
persistés, les hashes de prompt/output, et le comportement de reparse.

### 4.8 Exemple de code illustrant le problème

```python
# Current shape, simplified
reserve(maximum_cost)                      # before the exact bytes exist
prompt_data = build_consultation_data(...)
dossier = build_advisor_dossier(prompt_data)
prompt = render_advisor_wire(prompt_data, dossier)
prompt_sha = sha256(prompt)
submit(prompt, prompt_sha)                  # S3 could not bind prompt_sha before its decision
raw = transport_output
parsed = parse_typed_or_legacy(raw)         # two semantic paths
```

Le problème n'est pas qu'il existe un adaptateur : c'est que la préparation
et le parse sont des responsabilités sans contrat public, que les identités de
refs sont recalculées et que les octets exacts ne peuvent pas être liés à la
décision d'envoi.

## 5. Options indépendantes (M6)

Les options sont produites par des designers Luna xhigh indépendants, à partir
du même brief et du même SHA. Les sections suivantes sont consolidées après
réception ; aucun designer ne voit les propositions des autres.

### 5.1 S3

| Option | Contrainte | Artefact | Statut |
|---|---|---|---|
| A — Minimal | 1–3 opérations, profondeur maximale | `_work/D6_S3_A_MINIMAL.md` | reçu |
| B — Flexibilité | variantes réelles, retries/pages/correctifs | `_work/D6_S3_B_FLEX.md` | reçu |
| C — Caller/migration | parcours opérateur trivial, migration progressive | `_work/D6_S3_C_MIGRATION.md` | reçu |
| D — Fiabilité | CAS, crash, révocation, provenance | `_work/D6_S3_D_RELIABILITY.md` | reçu |

#### 5.1.1 Résumé des options

- **A — Minimal.** `InvestigationExternalEffectGateway.execute_one(intent)` +
  `reconcile(attempt_id, decision)`. Une route préparée par famille, une ligne
  `external_effect_attempts` par requête physique, `PREPARED → CONSUMED →
  SUCCEEDED | NEEDS_REVIEW`, preuve framework pour `NOT_SENT`, migration
  additive. Un seul consume autorise une seule requête physique ; chaque
  retry/page/correctif recommence. Le gateway possède la normalisation de
  route.
- **B — Flexibilité.** `OutboundEffectGate.prepare(intent) -> EffectRef` +
  `dispatch(effect_ref)`. `RequestDescriptor` complet (route, méthode, cible,
  body ref, wire digest, ordinal), matrice variante/consommateur couvrant VT
  pages, Shodan, Retrohunt submit/status/results, acquisition ticket/redirect/
  fallback, advisor, Bridge et local ; une requête physique par consume, avec
  `occurrence_id`, `parent_effect_id` et `step`. Comptabilité Bridge explicite :
  `BRIDGE_TURNS` logique une fois, reçu de mesure par dispatch.
- **C — Caller/migration.** `ExternalEffectCoordinator.send_one(authority,
  prepared, transport)` + `PermitAwareTransport`. L'adaptateur prépare les
  octets exacts ; le coordinateur possède l'autorité durable, le consume, le
  permis et l'outcome. Aucun état `PREPARED` commité : le claim est créé au
  consume. Séquence expand/contract la plus détaillée (schéma additif, module
  dormant, cutover par famille, drain des anciens workers, rollback qui
  conserve le gate).
- **D — Fiabilité.** `ExternalEffectCoordinator.dispatch(owner, prepared,
  transport)` avec machine d'état explicite `PREPARED → CONSUMED → SUCCEEDED |
  CONFIRMED_NOT_SENT | NEEDS_REVIEW → RECONCILED_* | ABANDONED`, événements
  append-only, garde de consume complète, ordre de verrouillage incluant
  stop/cancel, règlement budgétaire et lineage de réemploi.

#### 5.1.2 Comparaison

| Critère | A | B | C | D |
|---|---|---|---|---|
| Opérations publiques | `execute_one` + `reconcile` | `prepare` + `dispatch` | `send_one` (+ reconcile au service) | `dispatch` (+ réconciliation) |
| Propriétaire de la normalisation | gateway (route) | adaptateur de route | adaptateur (préparé) | adaptateur (préparé) |
| État durable avant consume | oui `PREPARED` | oui `PREPARED` | non ; claim créé au consume | oui `PREPARED` |
| Une requête physique par consume | oui | oui | oui | oui |
| Matrice des variantes | partielle | **complète** | complète par caller | complète par famille |
| Ordre des locks | enveloppe → invocation → attempt → Bridge | enveloppe → invocation/Bridge → réservation → famille → dispatch | enveloppe → invocation → réservation → claim → famille → Bridge | enveloppe → stop → invocation → réservation → dispatch → tentative |
| Comptabilité Bridge | non détaillée | **explicite** | explicite | explicite |
| Migration/rollout | correcte | deny-by-default | **expand/contract le plus détaillé** | additif + events append-only |
| Risque principal | routes/normalisation dans le gateway | complexité de la matrice | claim non préparé avant consume | ordre de locks à réconcilier avec les tables réelles |

#### 5.1.3 Sélection provisoire — hybride C+D, enrichi par B

Le parent retient provisoirement :

1. **Interface publique minimale de C/D** :
   `send_one(authority, prepared, transport) -> EffectOutcome` et une opération
   de réconciliation CAS. `authority` est construite côté serveur depuis
   l'invocation/réservation/tentative Bridge durables ; elle n'est jamais
   fournie par le JSON client. `prepared` porte les octets exacts ou une
   référence d'artifact immuable, le digest sémantique, le digest de requête
   applicative, la famille/opération/version de normalizer, `occurrence_id`,
   `parent_effect_id`, `step`, `reuse_policy` et les refs de preuve.
2. **Normalisation owned by the adapter**, pas par le gateway (C/D). Le
   gateway vérifie, persiste, consume et émet le permis ; il ne réimplémente
   pas VT/Shodan/Retrohunt/acquisition/advisor/Bridge.
3. **Machine d'état de D** : une ligne `external_effect_dispatch` par requête
   physique, `PREPARED` explicitement **non-permission**, `CONSUMED` comme seul
   droit d'envoi, `CONFIRMED_NOT_SENT` uniquement sur preuve framework,
   `NEEDS_REVIEW` pour toute ambiguïté, événements append-only, réconciliation
   CAS. La ligne est créée à la préparation pour l'idempotence et la reprise ;
   un `PREPARED` orphelin est `DENIED`/`ABANDONED`, jamais dispatché.
4. **Matrice et comptabilité de B** : une requête physique par consume ;
   chaque page/retry/correctif/hop a un `occurrence_id`, un parent et un step ;
   `BRIDGE_TURNS` reste une réservation logique chargée une fois, chaque
   dispatch produit un reçu de mesure ; aucune double charge.
5. **Ordre de locks de C, corrigé par les faits** : enveloppe (même ligne que
   la révocation) → invocation `RUNNING` si présente → réservation (dans le
   snapshot d'enveloppe aujourd'hui ; ligne dédiée si le schéma est scindé) →
   ligne de dispatch → slot Retrohunt/tentative d'acquisition → tour Bridge →
   `ModelRun`. Inclure la génération stop/cancel si elle est persistée
   séparément. Cet ordre doit être gelé et testé ; aucun lock ne traverse le
   transport.
6. **Aucun backfill** depuis `normalized_parameters` bruts ; les anciens
   `RUNNING` externes non prouvés deviennent `NEEDS_REVIEW` et ne sont jamais
   redispatchés. Migration additive après le head courant ; cutover par
   famille ; drain des anciens workers ; rollback qui conserve le gate.
7. **Classification advisor** : `advisor.consult` est traité comme
   `EXTERNAL_SUBMIT` par le gateway, tout en conservant le nom, le schéma et
   la provenance actuels.

Cette sélection reste provisoire jusqu'à la revue indépendante M7. Le point
ouvert principal est la faisabilité exacte de l'ordre de locks avec les tables
réelles et l'inventaire complet des transports physiques.

#### 5.1.4 Révision R2 après revue indépendante

La revue R7-S3 rend **REVISE** avec deux `BLOCKER` (transport fourni par
l'appelant ; set de locks non réconcilié avec stop/budget/Bridge) et deux
`HIGH` (identité/occurrence/réemploi ; reprise post-consume et settlement
budgétaire/preuve). La révision suivante est retenue comme base de la
deuxième revue :

1. **Route figée, pas de transport fourni par l'appelant.** L'interface
   publique devient `send_one(prepared: PreparedExternalRequest) ->
   EffectOutcome` plus une opération de réconciliation CAS. `prepared` porte
   un `route_id` versionné ; le coordinateur résout la route dans une table
   immutable composée au démarrage (normalizer, serializer, transport
   permit-aware, famille, provider). Aucun `transport` n'est un paramètre
   public. Les substitutions de test se font à la composition, pas à l'appel.
   Une route absente, stale ou dont le normalizer/serializer ne correspond
   pas échoue avant tout I/O.
2. **Dernier mètre fail-closed.** Chaque méthode de send physique
   (VT/Shodan/Retrohunt/acquisition/advisor/Bridge/ModelGateway) exige un
   permis one-use émis par le coordinateur et vérifie le digest des octets
   exacts au point d'envoi. Les méthodes raw ne sont pas exposées comme
   dépendances de caller. Le retry interne de `ModelGateway.execute`
   (`model_gateway.py:1161-1179,1257-1264`) est désactivé pour le chemin gaté
   ou chaque tentative devient un `send_one` distinct ; aucun wrapper
   au-dessus du gateway ne suffit.
3. **Set de locks exact et ordre global.** L'ordre est :
   (1) `investigation_envelopes` `FOR UPDATE` (version, approbation,
   `budget_snapshot`) ;
   (2) `investigation_working_states` `FOR UPDATE` (version et
   `stop_reason`/état d'arrêt) lorsque l'arrêt bloque l'egress ;
   (3) `tool_invocations` `FOR UPDATE` (`RUNNING`, version attendue) pour
   opérateur/advisor ;
   (4) `external_effect_dispatches` `FOR UPDATE` (`PREPARED`, identité,
   digest, occurrence, route/version) ;
   (5) ligne d'attempt familiale : `retrohunt_reservations`/slot ou
   `sample_acquisition_attempts` ;
   (6) `model_conversation_turns` puis `model_runs` `FOR UPDATE`
   (Bridge/advisor).
   La réservation opérateur est lue dans le snapshot d'enveloppe verrouillé ;
   si un chemin utilise les tables `investigation_budget_*`, la ligne de
   réservation et ses montants ordonnés sont verrouillés après l'enveloppe,
   avant le dispatch. Tous les writers/reconcilers suivent le même ordre ;
   la révocation ne verrouille que l'enveloppe (rang 1) et commit vite.
4. **Claim et occurrence durables.** `external_effect_dispatches` a une
   unicité `(investigation_id, effect_key)` où `effect_key` inclut le digest
   sémantique versionné et un jeton d'occurrence. L'occurrence par défaut est
   un sentinelle stable dérivé de l'identité, jamais un UUID aléatoire ;
   page/retry/correctif utilisent un ordinal stable possédé par le workflow,
   avec `parent_effect_id` ; poll/refresh utilisent un ID applicatif stable.
   Un résultat ambigu ne peut pas mint une nouvelle occurrence. Un changement
   de `normalizer_version` est un nouvel effet par défaut ; une équivalence
   inter-versions exige une règle explicite revue, sinon une occurrence
   explicite est requise pour éviter un renvoi silencieux.
5. **Réemploi avec claimant/use row.** `external_effect_uses` lie chaque
   invocation consommatrice au dispatch source, à l'invocation source, aux
   observations/artefacts sources et au delta budgétaire (zéro pour un
   réemploi). Seuls les résultats terminaux, complets, de même identité et
   autorisés par une `reuse_policy` par opération sont réutilisables. Un
   résultat ambigu ou `NEEDS_REVIEW` n'est jamais réutilisé. Aucun backfill
   depuis `normalized_parameters` bruts.
6. **Table de transitions et reprise complètes.** États :
   `PREPARED → CONSUMED → SUCCEEDED | NEEDS_REVIEW` ;
   `PREPARED → DENIED | ABANDONED` ;
   `CONSUMED → CONFIRMED_NOT_SENT` uniquement sur preuve framework ;
   `NEEDS_REVIEW → RECONCILED_EXECUTED | RECONCILED_NOT_EXECUTED | ABANDONED` ;
   terminaux immuables. Événements append-only. Un commit consommé dont
   l'acquittement est perdu ne reconstruit jamais un permis : relecture de la
   ligne, état `NEEDS_REVIEW` si incertain, zéro envoi. Une réponse tardive
   s'attache par CAS au dispatch exact et au digest de requête, sans nouvel
   envoi. Les réponses/octets partiels sont conservés comme artefacts de
   revue, jamais promus en succès. Un refus d'adaptateur ne vaut jamais
   preuve de non-envoi.
7. **Settlement budgétaire unique.** `BRIDGE_TURNS` reste une réservation
   logique chargée une fois ; chaque dispatch physique produit un reçu de
   mesure unique `(dispatch_id, dimension)`. Un règlement unique par
   `(investigation_id, reservation_operation_key)` empêche le double
   paiement. Pour l'acquisition, documenter la relation entre la réservation
   d'opération, le ledger VT-read/hit et le réemploi local vérifié (zéro
   download/VT unit) ; aucune libération sur simple exception d'adaptateur.
8. **Migration et premier slice.** Révision additive après le head
   `0002_c2_experiments`, sans backfill ; cutover par famille avec deny-all ;
   drain/fence des anciens workers ; rollback uniquement vers un binaire
   permit-aware. Premier slice sûr : route `vt.file_report` **test-only**,
   identité préparée, ligne de dispatch, transport final permit-required,
   production désactivée, toutes les autres familles deny-all. Premier oracle
   RED : deux sessions PostgreSQL, A commit `RUNNING` + réservation +
   prepared et pause avant consume, B commit `NEEDS_REVIEW`, A reprend →
   zéro consume/permis et zéro appel du fake VT. Ajouter l'assertion
   direct-entry sans permis avant toute activation.

La deuxième revue indépendante R7-S3-R2 doit statuer sur cette révision.

### 5.2 Advisor

| Option | Contrainte | Artefact | Statut |
|---|---|---|---|
| A — Minimal | 1–3 opérations, profondeur maximale | `_work/D6_ADVISOR_A_MINIMAL.md` | reçu |
| B — Flexibilité | types, unités, v2/v3, reparse | `_work/D6_ADVISOR_B_FLEX.md` | reçu |
| C — Caller/migration | adapter trivial, migration progressive | `_work/D6_ADVISOR_C_MIGRATION.md` | reçu |

#### 5.2.1 Résumé des options

- **A — Minimal.** Module pur `advisor_consultation.py` avec deux opérations :
  `prepare(projection) -> PreparedConsultation | PreparationRefusal` et
  `parse(basis, raw) -> ParseResult`. `PreparedConsultation` porte les octets
  UTF-8 exacts, `context_sha256`, le manifeste, les `transmitted_refs`, un
  catalogue d'alias immuable et le contexte de reparse. `parse` accepte une
  base préparée ou stockée ; le reparse n'est pas une opération séparée.
  Identité de ref = `(kind, id, sha256)` ; collision de SHA → fail-closed.
  Migration minimale, pas de schéma. Risque principal : corpus historique
  v2/v3 incomplet.
- **B — Flexibilité.** Même module, trois opérations `prepare`, `parse`,
  `reparse`, avec union explicite des sept types de consultation, matrice
  variante/consommateur, `response_schema` distinct de `parser_version`,
  `unit_plan` pour le batching éditorial et `provenance_eligible`. Le reparse
  sélectionne le décodeur depuis le schéma/contexte persisté et retourne
  `legacy_context_unavailable` si le contexte est insuffisant ; Suricata n'est
  admis qu'avec un contexte réseau vérifié. Note un plafond de 160 KiB dans
  `AnalystOpinionService` à préserver.
- **C — Caller/migration.** Deux opérations `prepare`/`parse`, contrat proche
  de A, avec une séquence expand/contract détaillée (module à côté de l'ancien,
  double exécution en test sans second envoi, cutover préparation puis parse
  puis reparse, suppression), un tableau caller-by-caller et un champ de
  parse-context additif dans l'observation existante. Risque principal :
  complétude des contextes historiques.

#### 5.2.2 Comparaison

| Critère | A | B | C |
|---|---|---|---|
| Placement du seam | après admission/projection, avant archive/S3 | idem | idem |
| Opérations | 2 (`prepare`, `parse`) | 3 (`prepare`, `parse`, `reparse`) | 2 (`prepare`, `parse`) |
| Identité de refs | catalogue immuable `(kind,id,sha256)` | `SourceRef` + alias legacy + offsets | ledger + snapshot de contexte |
| Variantes | sept types implicites | matrice explicite, `response_schema`/`parser_version` | sept types via le contrat |
| Batching | reste dans le caller, un prepared par dispatch | `unit_plan` + provenance par unité | reste dans le caller |
| Reparse | `parse(StoredParseBasis)` | opération dédiée + fallback legacy | `parse` avec contexte stocké |
| Migration | minimale | rollout avec corpus v2/v3 | expand/contract le plus détaillé |
| Deletion test | fort (5 responsabilités réapparaissent) | fort (variants/parse/reparse) | fort (caller-by-caller) |
| Risque principal | corpus historique | contexte legacy/Suricata | contexte historique |

#### 5.2.3 Sélection provisoire — hybride A+C, enrichi par B

Le parent retient provisoirement :

1. **Interface à deux opérations** (`prepare`, `parse`) comme A/C, pour
   maximiser la profondeur ; le reparse est une **base de parse stockée**, pas
   une troisième opération.
2. **Types et variantes de B** : union explicite des sept types,
   `response_schema` distinct de `parser_version`, `unit_plan` pour le
   batching, `provenance_eligible`, et règle Suricata « seulement avec contexte
   vérifié ».
3. **Identité de refs de A** : `(kind, id, sha256)`, catalogue immuable,
   collision fail-closed, alias legacy en lookup seulement.
4. **Migration de C** : module additif, double exécution en test sans second
   envoi, cutover préparation → parse → reparse, champ de parse-context additif,
   rollback par wiring, conservation des blobs/observations immuables.
5. **Contraintes de A/B** : le module ne possède ni autorisation, ni budget, ni
   consume, ni transport ; S3 reste le seul seam d'envoi.

Cette sélection reste provisoire jusqu'à la revue indépendante M7.

#### 5.2.4 Révision R2 après revue indépendante

La revue R7-ADV rend **REVISE** avec trois constats `HIGH` (identité de
refs, liaison exacte des octets, frontière d'autorisation), un `HIGH` de
migration et deux `MEDIUM` (batching, observabilité des omissions). La
révision suivante est retenue comme base de la deuxième revue :

1. **Catalogue de refs fail-closed.** Clé exacte `SourceRef(kind, id,
   sha256)`. La préparation refuse (`ref_reference_conflict`) si un même
   `(kind, id)` porte deux SHA différents, dans la projection, le manifeste
   ou les refs transmises. Pour les opinions, `id` est le `source_ref`
   versionné complet (`opinion:<uuid>:v<n>:<sha>:<block>`), jamais l'UUID nu,
   afin que deux versions légitimes ne collisionnent pas. Tout alias de
   réponse doit cibler une ref effectivement transmise ; un manifest-only ref
   ne devient jamais un alias, sauf cible de review versionnée explicitement
   typée. Aucune résolution par ID seul. Les alias legacy sont des lookups,
   pas des identités.
2. **Octets liés mécaniquement.** `prepare` retourne `prompt_utf8: bytes` et
   `prompt_sha256` ; il n'expose pas de `prompt_text` réutilisable. Le caller
   archive ces octets tels quels, décode une seule fois au seam S3, et
   vérifie `sha256(archived) == prompt_sha256 == sha256(sent_bytes)`. L'oracle
   compare les octets préparés, archivés et transmis, y compris Unicode/CRLF.
3. **Frontière d'autorisation.** `prepare` accepte uniquement une projection
   post-filtrage produite par le loader/adaptateur existant ; il ne reçoit ni
   booléen `external_llm_allowed`, ni TLP/DNT, ni capability d'autorisation.
   Les gates restent dans le caller et dans le consume S3, qui revalide
   l'enveloppe courante. Une matrice caller-level (`external=false`, DNT, RED,
   restriction échantillon) prouve zéro appel S3. Un type nominal n'est pas
   un jeton de sécurité.
4. **Contexte de parse versionné et fail-closed.** Ajouter
   `parse_context_version` distinct de `response_schema` et `parser_version`.
   `StoredParseBasis` exige un contexte persisté vérifié ; s'il manque ou est
   incomplet, retour `legacy_context_unavailable` non-utilisable, sans
   rechargement de preuves et sans Bridge. `parse(StoredParseBasis, raw)`
   vérifie que `basis.prompt_sha256` et les refs correspondent au call
   `operator-advisor-call.v1` stocké et au blob de sortie, avant toute
   interprétation. Conserver l'ancien `reparse_context` pendant le rollback ;
   le nouveau champ est additif.
   `suricata_draft` reste gaté sur des faits réseau/refs/hash stockés
   complets ; ne pas élargir le reparse plan/pivot par effet de bord.
   `refused`, `partial`, `invalid` et l'ambiguïté sont des outcomes distincts ;
   une ambiguïté de transport n'entre jamais dans `parse`.
5. **Batching hors du module.** Retirer `unit_plan` du contrat public ou le
   réduire à une description en lecture seule du seul dispatch sélectionné.
   Le caller conserve le découpage, les clés enfants, le curseur, le budget et
   les outcomes par unité. Une valeur préparée = un dispatch enfant réel.
6. **Observabilité des omissions.** Réutiliser `build_advisor_dossier`, le
   secret guard et le préflight de complétude ; transporter leurs résultats
   d'omission/complétude dans le prepared/refusal. Aucun nouveau plafond, aucune
   redaction large, aucune perte de raw/hex/Base64/réseau/contradictions.
7. **Gate de migration.** Avant cutover, exécuter l'ancien et le nouveau
   chemin sur des fixtures locales uniquement, avec zéro envoi, zéro
   observation appendée et zéro shadow parse. Ajouter des tests caller-level
   qui prouvent que le nouveau `prepare`/`parse` est réellement utilisé,
   l'égalité octets/refs, le fail-closed des contextes stockés et la
   provenance d'avis inchangée. Le deletion test doit rendre inaccessibles
   l'ancienne agrégation de refs et l'ancien dispatch sémantique, pas seulement
   un wrapper.

La deuxième revue indépendante R7-ADV-R2 doit statuer sur cette révision.

#### 5.2.5 Verdict R2 et première tranche sûre

R7-ADV-R2 rend **REVISE pour le cutover**, avec :

- **CLOSED** : catalogue de refs fail-closed (avec exception de cible de
  review versionnée), liaison exacte des octets préparés/archivés, batching
  hors module, observabilité des omissions.
- **PARTIAL** : frontière d'autorisation (le seam S3 actuel ne peut pas
  revérifier l'enveloppe au consume ; il ne reçoit qu'un booléen), contexte
  de parse historique (aucun `parse_context_version` sur les enregistrements
  existants ; convertisseur legacy à nommer), gate de migration (corpus
  v2/v3 gelé à produire).
- **Nouveau MEDIUM** : l'oracle d'octets doit observer l'entrée après le
  string adapter (`ModelConversationService.add_turn` strippe la chaîne),
  pas seulement le helper `submit_bridge_consultation_once`.

**Première tranche approuvée : pure `prepare` uniquement.**
Implémenter le module et le catalogue de refs, sans le brancher dans
`InvestigationAdvisorTool.execute`, sans archive/envoi, sans nouveau champ
de contexte persisté, sans changer le reparse/parseur ni l'avis. Cette
tranche est réversible et ne crée aucun egress ni comportement historique.

**Premier oracle RED :** deux sources de même `(kind,id)` avec des SHA
valides différents, introduites séparément via projection, manifeste et refs
transmises, doivent produire `ref_reference_conflict` ; les alias absents,
restreints, stale ou de mauvais kind ne résolvent pas ; une cible de review
versionnée résout vers le `source_ref` exact (version/SHA/block) ; les octets
et le hash sont déterministes avec Unicode et CRLF.

**Conditions avant cutover :** matrice caller-level de refus à zéro appel S3 ;
consume S3 qui revalide la politique ou correction explicite de cette
frontière ; égalité octets préparés/archivés/aval ; contexte stocké unique
versionné avec convertisseur legacy/fail-closed ; corpus v2/v3 gelé ; pas de
dual-send ni shadow parse ; suppression effective de l'ancien dispatch
sémantique.

## 6. Comparaison et arbitrage

### 6.1 S3

Les quatre options convergent sur le cœur : **une requête physique par
consume**, une identité d'effet versionnée distincte de `operation_key`, un
permis one-use vérifié au dernier mètre, une transaction PostgreSQL courte
avant l'I/O, et `NEEDS_REVIEW` pour toute ambiguïté. Elles diffèrent sur le
placement de la normalisation, l'état `PREPARED`, la matrice des variantes,
l'ordre des locks et la migration.

- **A** est la plus petite, mais place la normalisation et la table de routes
  dans le gateway ; la matrice des variantes et la comptabilité Bridge sont
  moins détaillées.
- **B** fournit la matrice la plus complète (pages VT/Retrohunt, redirects/
  fallback acquisition, Bridge retry/correctif) et la comptabilité
  `BRIDGE_TURNS` logique vs reçu par dispatch ; sa complexité doit rester
  portée par des routes explicites, pas par un moteur de politique.
- **C** fournit la meilleure migration expand/contract, le propriétaire
  adaptateur de la normalisation et l'ordre de locks le plus proche des
  tables réelles ; son absence d'état `PREPARED` commité simplifie la reprise
  mais exige un artefact de requête durable avant consume.
- **D** fournit la machine d'état et les événements append-only, la preuve
  framework de non-envoi et la couverture crash/replay la plus rigoureuse ;
  son ordre de locks doit être corrigé pour refléter le snapshot d'enveloppe
  et les tables réellement utilisées.

**Arbitrage retenu :** hybride **C+D+B** — interface `send_one(prepared)`
sans transport fourni, route figée serveur, machine d'état/événements de D,
matrice et comptabilité de B, migration de C. La revue R7-S3 a confirmé que
la direction est saine mais a bloqué le transport fourni et l'ordre des
locks ; la révision §5.1.4 a fermé F1, mais R7-S3-R2 maintient F2–F4
PARTIAL et le gate `S3_READY_FOR_IMPLEMENTATION` **NOT MET**. S3 reste donc
`S3_BLOCKED_DESIGN`.

**Pourquoi pas KEEP + correctifs étroits ?** Un callback obligatoire, un
recheck d'invocation et une normalisation locale fermeraient la course
`RUNNING → NEEDS_REVIEW` et amélioreraient l'existant, mais ne
fourniraient ni identité d'effet persistée, ni permis unique, ni couverture
des entrées directes, ni portée Bridge/retry, ni filiation de réemploi. Le
deletion test montre que ces règles devraient être réimplémentées dans au
moins six routes (opérateur, VT, Shodan, Retrohunt, acquisition, advisor,
Bridge). KEEP reste la référence, mais il ne satisfait pas les garanties
demandées pour S3.

### 6.2 Advisor

Les trois options convergent sur un module pur
`advisor_consultation` avec `prepare`/`parse`, sans autorisation ni
transport. A maximise la profondeur avec deux opérations ; B ajoute la
matrice des variantes, la distinction `response_schema`/`parser_version`, le
batching et le gating Suricata ; C fournit la migration expand/contract et le
tableau caller-by-caller.

**Arbitrage retenu :** hybride **A+C+B**, avec `prepare`/`parse` comme
interface, le catalogue triple de A, les variantes/versions de B et la
migration de C. La revue R7-ADV a exigé un catalogue fail-closed, une
liaison mécanique des octets, une frontière d'autorisation explicite et un
contexte de parse versionné ; R7-ADV-R2 a clos refs/bytes/batching/omissions
et approuvé une première tranche **pure `prepare`**. Le cutover complet reste
conditionnel aux points PARTIAL (autorisation S3, contexte historique,
corpus v2/v3 gelé).

**Pourquoi pas KEEP + correctifs étroits ?** Supprimer le dossier recalculé,
ajouter un corpus de parse et corriger le reparse Suricata améliorerait
l'existant, mais ne créerait pas d'objet préparé liant octets/refs/hash avant
le point de décision S3, ne résoudrait pas la collision `(kind,id)`/SHA et
laisserait la préparation/parse réparties entre le caller live et le reparse.
KEEP reste la référence, mais le deletion test montre que ces obligations
réapparaissent dans les deux callers.

## 7. Décision retenue et compromis

**Décision advisor :** la première tranche `prepare` est approuvée comme
expand-only, sans branchement, archive, envoi, persistance de contexte ni
changement de parse/reparse/avis. Le cutover complet est `REVISE` conditionnel
et ne doit pas être implémenté avant clôture des points PARTIAL.

**Décision S3 :** S3 reste **`S3_BLOCKED_DESIGN`**. R7-S3-R2 a fermé F1
mais maintient F2–F4 PARTIAL et le gate `S3_READY_FOR_IMPLEMENTATION`
**NOT MET** (inversion de l'ordre des locks avec réconciliation/settlement,
binding owner/route, handoff `ModelGateway`, normalizer-version). Aucun
egress de production, migration, cutover ou réemploi S3 n'est autorisé. Seule
une composition de test isolée `vt.file_report`, sans client live et avec
production deny-all, pourra être préparée après correction du binding
owner/route et du contrat de transaction.

**Décision globale : `GO_NARROW_ONLY`.** Les lots autorisés sont L1 (S2
échappé), L2 (refus P10), L3 (oracle PostgreSQL d'audit) et L20 (advisor
`prepare` expand-only, non branché). Aucun seam de production complet n'est
autorisé.

**Compromis et coûts :**

- S3 ajoute une table de dispatch, des événements, une table d'usage/claim et
  une migration additive ; le coût de migration et de cutover par famille est
  élevé, mais il est réversible et sans backfill.
- Le gate S3 impose de désactiver ou de surfacer les retries SDK/gateway ;
  c'est une contrainte de comportement à valider famille par famille.
- Advisor ajoute un module pur et un catalogue de refs, sans schéma ; le coût
  principal est le corpus v2/v3 et la compatibilité des contextes historiques.
- Les correctifs étroits L1–L3 restent prioritaires et indépendants.

**Ce que la décision n'autorise pas :** aucune migration live, aucun envoi
fournisseur, aucun cutover advisor/S3, aucun rollback vers un binaire non
permit-aware, aucune publication/G3.
