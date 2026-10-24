# Mission 03 / Phase 1 — Cartographie de l'architecture

**SHA étudié :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Branche :** `mission03/architecture-design`
**Nature :** cartographie factuelle et registre de constats. Aucune décision de
refactoring n'est appliquée dans cette phase.

## 1. Méthode et vocabulaire

La cartographie suit les quatre flux imposés : effets externes contrôlés (A),
dossier et avis CTI (B), investigation/contexte/audit (C), publication (D).
Les termes `module`, `interface`, `implementation`, `seam`, `adapter`, `depth`,
`leverage` et `locality` sont ceux du skill `codebase-design`.

Chaque constat porte `VERIFIED` (sourcé au SHA), `INFERENCE` (conclusion
architecturale à partir du code) ou `UNKNOWN` (non établi). Les références sont
des `path:line` au SHA étudié. Les tests ne sont cités comme preuve que s'ils
ont été lus ou exécutés ; le détail des exécutions est dans `TEST_SURFACE.md`.

## 2. Vue d'ensemble

### 2.1 Couches et propriétaires canoniques

| Couche | Contenu | Autorité |
|---|---|---|
| `domain/` | entités, enums de contrôle, invariants purs | aucune I/O |
| `application/` | services, orchestration, ports | décisions métier |
| `infrastructure/` | PostgreSQL, MinIO, Redis, HTTP, transports | adapters |
| `workers/` | jobs Dramatiq | reprise/idempotence durable |
| `api/` | routes FastAPI | identité, autorisation d'entrée |

Les sources canoniques sont PostgreSQL (identités, métadonnées, versions,
événements) et les blobs MinIO adressés par SHA-256. Les workspaces, les
conversations de développement et les fichiers `RUN_*` sont des projections
reconstructibles, jamais des preuves primaires
(`docs/persistence_and_storage.md:3-8,149-163`).

### 2.2 Entrées principales

| Entrée | Point d'entrée | Mode |
|---|---|---|
| API investigation/opérateur | `backend/src/cti_app/api/investigation.py:1461-1517` | `operator_led` |
| Boucle Bridge | `application/investigation_service.py:523-672` → `application/bridge_investigator.py:1172-1445` | `bridge_led` |
| Workers | `backend/src/cti_app/workers/tasks.py` | jobs durables |
| Export d'audit | `backend/scripts/export_investigation_audit.py:40-76` | CLI read-only |
| Rendu publication | `application/publication_rendering.py:244` | API/worker |
| Rendu édition | `application/edition_rendering.py:192,354` | worker |

`BridgeInvestigator.turn` n'appartient pas au registre des outils ordinaires :
il est traité séparément dans le Flux A.

## 3. Flux A — Effets externes contrôlés

### 3.1 Graphe d'appel

```text
POST /investigations/{id}/operator/tools/{tool_name}
  → api/investigation.py:1461-1517
  → OperatorToolInvocationService.invoke
      (alias de schéma, payload sûr, idempotence, mode opérateur,
       divulgation externe, doublon/recovery)
      application/operator_tool_invocation.py:700-930,1031-1123
  → InvestigationService.execute_tool_call
      (ToolInvocation PENDING → réservation → RUNNING → commit)
      application/investigation_service.py:1007-1153
  → RegistryToolDispatcher.dispatch
      (registre, schéma, politique, budget, callback optionnel)
      application/investigation_tools.py:619-798
  → adaptateur du fournisseur → port/transport → réponse
  → observation/archive + clôture invocation/réservation
      application/investigation_service.py:1238-1321
```

Le chemin Bridge est distinct :

```text
InvestigationService.run (bridge_led)
  → contexte persisté + réservation BRIDGE_TURNS
  → BridgeInvestigator.turn
      callback avant/après pacing pour chaque soumission
      application/bridge_investigator.py:1231-1291
  → ModelConversationTurnTransport → ModelGateway → provider
  → tour/ModelRun persisté, résultat ou NEEDS_REVIEW
```

### 3.2 Étapes, effets et propriétaires

| Étape | Preuve | Effet | Propriétaire actuel |
|---|---|---|---|
| Normalisation opérateur et clé | `operator_tool_invocation.py:700-761` | aucun | service opérateur ; la clé inclut la clé cliente, le payload brut, le but et la version attendue |
| Admission opérateur | `operator_tool_invocation.py:835-930` | divulgation externe, doublon sémantique | service opérateur |
| Invocation durable + réservation | `investigation_service.py:1031-1153` | classification d'effet depuis `ToolDefinition` | service investigation |
| Admission dispatcher | `investigation_tools.py:619-798,865-885` | politique provider/effet, schéma, budget | dispatcher (verrou local au processus) |
| Contrôle frais | `investigation_service.py:1190-1226` | enveloppe/réservation/divulgation | callback du service ; optionnel |
| Transport VT | `investigation_vt_tools.py:691-696,754,863-899` ; `infrastructure/virustotal.py:419-440` | `EXTERNAL_READ` (download : `ACQUIRE`) | adapter VT puis client HTTP |
| Transport Shodan | `investigation_shodan_tools.py:261-340,1175-1222` ; `infrastructure/shodan.py:109-225` | `EXTERNAL_READ` | adapter Shodan puis client HTTP |
| Retrohunt | `investigation_retrohunt_tools.py:81-181` ; `application/retrohunt.py:108-329` | `EXTERNAL_SUBMIT` / `EXTERNAL_READ` | adapter + service Retrohunt (slot durable) |
| Acquisition | `investigation_acquisition_tools.py:495-540` ; `application/investigation_acquisition.py:191-450` ; `application/sample_acquisition.py:282-447` | `ACQUIRE` | service acquisition + port VT |
| Advisor | `investigation_advisor_tools.py:164-210,764-778,897-920` | déclaré `EXTERNAL_READ`, effectue une soumission modèle | adapter advisor + helper Bridge |
| Bridge principal | `bridge_investigator.py:1172-1445` | soumission LLM externe | service investigation + Bridge + transport modèle |

### 3.3 Invariants et propriétaires

| Invariant | Propriétaire actuel | Où il est appliqué | Constat |
|---|---|---|---|
| L'effet est fixé par le `ToolDefinition`, pas par le JSON client | composition/registre | `investigation_tools.py:151-188,363-409` ; modules de registration | `VERIFIED` ; le registre peut être modifié à l'exécution (`register`/`unregister`) |
| Un client ne peut pas appeler un outil externe sans mode opérateur et divulgation | service opérateur + dispatcher | `operator_tool_invocation.py:835-861` ; `investigation_tools.py:865-885` | `VERIFIED` sur le chemin nominal |
| L'admission et le coût survivent à un redémarrage avant l'effet | service + repositories PostgreSQL | `investigation_service.py:1031-1153` ; `repositories/investigation.py:159-314` | `VERIFIED` (invocation et réservation commitées avant dispatch) |
| L'enveloppe courante est relue juste avant l'adaptateur | callback du service | `investigation_service.py:1190-1226` | `VERIFIED` ; callback optionnel dans le dispatcher |
| Une soumission ambiguë n'est pas renvoyée automatiquement | invocation/Bridge/Retrohunt/acquisition | `investigation_service.py:1238-1321` ; `bridge_investigator.py:1355-1394` ; `retrohunt.py:152-239` | `VERIFIED` par chemin, pas par un propriétaire unique |
| Une révocation avant le point de décision empêche l'egress | aucun point unique | locks CAS locaux à chaque écriture | `INFERENCE` : la fenêtre entre le dernier contrôle et le transport reste ouverte |
| L'identité d'effet est canonique et persistée | aucun propriétaire unique | `normalized_parameters=arguments` ; fingerprint heuristique | `VERIFIED` : identité brute, pas l'intent normalisé du transport |
| Le budget d'un résultat réutilisé garde sa filiation | plusieurs services | `investigation_acquisition.py:231-288` ; `investigation_advisor_tools.py:897-920` | `VERIFIED` par chemin ; politique inter-outils absente |

### 3.4 Contournements, duplications et couplages

1. **Aucune interface de dispatch obligatoire ne possède la soumission
   externe.** `ToolAdapter.execute`, `RegistryToolDispatcher.dispatch`, les
   ports provider et les clients HTTP sont appelables directement ; le
   callback de contrôle frais est optionnel
   (`investigation_tools.py:412-427`). `VERIFIED`.
2. **Les chemins directs ont des gardes différentes.** Shodan HTTP ne connaît
   pas le G2 d'investigation ; le transport VT ne possède pas le journal
   d'invocation ; `RetrohuntService.submit` et
   `HitAcquisitionService.acquire_hit` ont leurs propres gardes ; le port de
   téléchargement VT est encore plus bas. `VERIFIED` par lecture des seams.
3. **Advisor et Bridge peuvent entrer dans le transport modèle hors du
   registre.** `InvestigationAdvisorTool.execute` fait ses contrôles locaux ;
   `submit_bridge_consultation_once` reçoit un booléen d'autorisation ;
   `BridgeInvestigator.turn` accepte un callback optionnel. `VERIFIED`.
4. **Les verrous sont locaux.** `_ACTIVE_OPERATOR_INVOCATIONS` et le
   `asyncio.Lock` du dispatcher ne sont pas inter-processus
   (`operator_tool_invocation.py:700-761` ; `investigation_tools.py:619-642`).
   `VERIFIED`.
5. **L'identité et la normalisation sont réparties.** La clé opérateur inclut
   le payload brut ; `tool_operation_key` est une autre identité ; le
   fingerprint sémantique ne couvre que certains champs advisor/VT
   (`operator_tool_invocation.py:1330-1398`). `VERIFIED`.
6. **`normalized_parameters` n'est pas normalisé par le transport.**
   `execute_tool_call` y copie `arguments`
   (`investigation_service.py:1031-1038`) ; chaque adapter normalise plus tard.
   `VERIFIED`.
7. **La classification d'effet est poreuse.** `advisor.consult` est
   `EXTERNAL_READ` alors qu'il soumet un modèle ; `EXTERNAL_ACTIVE` est traité
   comme revue par le dispatcher ; la classification n'est pas persistée dans
   l'invocation. `VERIFIED` (`investigation_advisor_tools.py:3328-3411` ;
   `investigation_tools.py:865-885`).
8. **Un tour Bridge logique peut produire plusieurs egress.** Jusqu'à 3
   tentatives pré-soumission et 2 corrections, avec des clés d'essai
   distinctes, sous une seule réservation `BRIDGE_TURNS`
   (`bridge_investigator.py:59,1204-1354,1395-1445`). `VERIFIED`.
9. **Les machines d'état durables sont parallèles.** `ToolInvocation`,
   slot Retrohunt, tentative d'acquisition, `ModelRun`/tour Bridge ont chacun
   leurs états et transitions. `VERIFIED` (`M1A` §e).
10. **Un refus pré-transport émis par un adaptateur devient
    `NEEDS_REVIEW`.** Le dispatcher retire toute preuve fournie par
    l'adaptateur (`investigation_tools.py:799-801`) ; le service conserve la
    réservation en cas de résultat non prouvé
    (`investigation_service.py:1238-1321`). `VERIFIED` ; conséquence déjà
    documentée en Mission 01.

### 3.5 États persistés et courses

| État | Transitions | Course / atomicité |
|---|---|---|
| `ToolInvocation` | `PENDING → RUNNING → terminal` (`NEEDS_REVIEW` inclus) | CAS de version, pas de verrou de ligne ; pas vérifié atomiquement avec l'enveloppe au moment du transport |
| Enveloppe + réservation | version CAS, `SELECT … FOR UPDATE` sur l'enveloppe | locks limités à chaque sauvegarde ; aucun lock tenu pendant le transport |
| Slot Retrohunt | `RESERVED → SUBMITTING → terminé / réconciliation` | machine durable propre ; pas la même transaction que `ToolInvocation` |
| Tentative d'acquisition | reuse / download / ambigu | état propre au service d'acquisition |
| Tour Bridge / `ModelRun` | essai, état de soumission, résultat | idempotence durable du run ; pas la même transaction que l'enveloppe |

**Point de décision sérialisé actuel :** il n'existe pas de transaction
PostgreSQL unique qui réclame `{enveloppe courante, réservation RESERVED,
invocation RUNNING, tentative Bridge équivalente}` et qui précède le
transport. Les écritures individuelles sont protégées par CAS/row lock.
`VERIFIED`.

**Course `RUNNING → NEEDS_REVIEW` :** un replay peut sceller une invocation
`RUNNING` en `NEEDS_REVIEW` (`investigation_service.py:1079-1098` ;
`operator_tool_invocation.py:872-878`) ; l'exécution originale poursuit son
callback, qui contrôle l'enveloppe et la réservation mais pas le statut de
l'invocation (`investigation_service.py:1190-1226`). Si la réservation reste
`RESERVED`, elle peut entrer dans l'adaptateur. `INFERENCE` à partir du code,
non exécutée en concurrence PostgreSQL.

### 3.6 Tests principaux

Les tests lus/qualifiés sont listés dans `TEST_SURFACE.md` §2. Les constats
saillants : `M2-A` recense **0/10** oracles de permis au point de consume ;
**18** cas d'intégration PostgreSQL réels dans quatre fichiers, tous avec
egress factice ; aucun test ne prouve l'absence de dispatch direct d'un
adapter/transport sans permis.

## 4. Flux B — Dossier et avis CTI

> Cette section est consolidée à partir de M1-B, M4-A et des contrats
> advisor. Elle distingue sélection, complétude, divulgation, projection,
> sérialisation, transport, parsing et version d'avis.

### 4.1 Graphe d'appel

```text
état canonique de l'investigation
  → sélection/projection des preuves
  → advisor_dossier.build_advisor_dossier
  → advisor_wire.render_advisor_wire_input
  → contrôle de divulgation + redaction
  → préflight de complétude
  → transport Bridge (submit_bridge_consultation_once)
  → sortie brute archivée
  → parse typé / fallback
  → observation advisor_call non fiable
  → avis versionné (AnalystOpinionV1)
  → reparse éventuel sans nouvelle consultation
```

Références principales : `application/investigation_advisor_tools.py:164-778`
(préparation, divulgation, dossier, préflight, transport),
`application/advisor_dossier.py:304-...` (sélection et rendu),
`application/advisor_wire.py:267-...` (wire et parsing des blocs source),
`application/advisor_contracts.py:46-...` (parsing typé),
`application/advisor_checkpoint.py:17-...` (purpose des traces),
`application/analyst_opinion_service.py:244-...` (version d'avis).

### 4.2 Responsabilités et propriétaires

| Responsabilité | Où elle vit | Constat |
|---|---|---|
| Sélection des preuves pertinentes | `advisor_dossier.py:304-...` | `VERIFIED` : fonction pure sur `prompt_data` ; le manifeste est une provenance locale |
| Complétude et contradictions | `advisor_dossier.py:942-...` (`evaluate_advisor_evidence_preflight`) | `VERIFIED` : statut `READY`/`POLICY_BLOCKED`/`TECHNICAL_BLOCKED` |
| Divulgation explicite | `investigation_advisor_tools.py:362-376,425-435` ; `research_context_builder`/`external_llm_allowed` | `VERIFIED` : `external_llm_allowed`, TLP/DNT, restrictions d'échantillon |
| Projection et rendu du dossier | `advisor_dossier.py:304-...` ; `advisor_wire.py:267-273` | `VERIFIED` : le prompt ne sérialise pas l'objet interne |
| Sérialisation/wire | `advisor_wire.py:26-90,267-...` | `VERIFIED` : blocs source bruts et alias |
| Transport | `investigation_advisor_tools.py:764-778` ; `bridge_investigator.py` | `VERIFIED` : helper de soumission unique |
| Parsing typé | `advisor_contracts.py:46-...` ; `advisor_wire.py:44-90` | `VERIFIED` : parse strict + fallback partiel |
| Version/provenance de l'avis | `analyst_opinion_service.py:244-...` ; `domain/investigation.py` | `VERIFIED` : versions append-only et hash de contenu |
| Reparse | `investigation_advisor_tools.py` (reparse tool) | `VERIFIED` : relit le blob brut et n'appelle pas le transport |

### 4.3 Duplications et contournements vérifiés

- **Recontrôles de divulgation** : la même condition de cas est évaluée par
  `InvestigationService._external_allowed` puis aux frontières de soumission
  et de transport (`bridge_investigator.py:270-277,661-667`). C'est une
  défense en profondeur, pas une seconde politique.
- **Filtrage des secrets opérationnels** : le chemin advisor utilise
  `publication_secret_guard` + l'omission de champs du dossier ; P10 possède
  son propre `_bounded_json`/scrubber, avec des règles différentes et sans
  catalogue de valeurs configurées (`invariant_proposals.py:2205-2364`).
- **Recalcul du dossier** : `advisor_wire_aliases` appelle
  `build_advisor_dossier` alors que le manifeste est déjà fourni
  (`advisor_wire.py:90-264`). Travail redondant, pas seconde barrière.
- **P10 est un chemin frère** : il a son snapshot, son routage de divulgation,
  son redacteur, son parseur et son handoff P09 ; il ne traverse pas le gate
  de complétude `advisor_dossier`. Ce n'est pas un bypass d'`advisor.consult`.
- **Deux parseurs sémantiques** : le parseur typé `advisor_contracts.py` et le
  parseur legacy v2 dans `investigation_advisor_tools.py:4689-4780,5067-5105`
  partagent la lexie wire mais divergent sur les alias, les warnings et le
  texte sûr. Le reparse n'utilise que le parseur typé
  (`investigation_advisor_tools.py:3629-3637`).
- **P10 : refus effaçable** (`VERIFIED`, risque élevé). Le wire P10 transforme
  `STATUS: refused` en simple warning tout en continuant à parser des lignes
  candidates (`invariant_proposals.py:1226-1342`) ; `ProposalResponse` n'a pas
  de champ de refus (`domain/invariant_proposals.py:229-243`) et `propose`
  transmet les candidats à P09 (`invariant_proposals.py:262-275,884-918`).
  Un refus accompagné de candidats peut donc encore enregistrer des
  propositions. Aucun test « refus + candidats » n'a été trouvé.
- **Métadonnées d'échantillon restreint** (`INFERENCE`) :
  `_sample_inventory` expose IDs d'association, hashes courts, MIME, tailles,
  statut et moteurs (`investigation_advisor_tools.py:2376-2446`) et les inclut
  dans les consultations plan/pivot/YARA (`:1638-1648,2143-2155`) sans le
  contrôle `_sample_policy_allows_external` appliqué au contenu. La politique
  doit dire si la restriction couvre aussi les métadonnées.
- **Reparse Suricata** : `suricata_draft` figure dans le contexte de reparse
  mais pas dans les types acceptés par `ReparsePersistedAdvisorOutputTool`
  (`investigation_advisor_tools.py:3442-3487,4188-4225`). Écart à corriger ou
  à documenter.

### 4.4 Versions, preuves et reparse

`VERIFIED` : la sortie brute est archivée avant parsing ; le reparse exige
l'observation `advisor_call`, l'identité d'invocation, le hash vérifié et le
blob `model-outputs`, et ajoute une observation versionnée sans nouvelle
consultation (`investigation_advisor_tools.py:3414-3787`). Le test direct
`test_advisor_output_reparse.py::test_reparse_appends_verified_parse_without_a_bridge_transport`
couvre ce chemin avec des fakes ; le stockage de production et la composition
applicative restent hors de cet oracle (`M2-B`).

La préparation du prompt et le hash final sont connus **après** la réservation
mais **avant** le transport (`M4-A` §timing). C'est le point clé pour S3 : la
réservation est faite sur les maxima (`BRIDGE_TURNS`, `CONTEXT_BYTES`,
`TOOL_SECONDS`), pas sur les octets réellement émis. Une interface de
consultation préparée devrait produire les octets, les refs et le hash avant
le point de décision durable.

### 4.5 Tests principaux

Voir `TEST_SURFACE.md` §3. Les oracles présents couvrent le transport factice
pour les preuves permissives (600 octets DNS `raw`, Base64 espacé/fragmenté,
preuves contradictoires) et les refus explicites testés séparément. Les
oracles manquants sont la matrice croisée autorisation/refus sur le même
chemin, le résidu S2 JSON échappé, la preuve de stockage de production et la
continuité avis → PDF final.

## 5. Flux C — Investigation, contexte et audit

### 5.1 Graphe d'appel

```text
POST operator tool
  → OperatorToolInvocationService.invoke
      (CAS de l'état de travail, action opérateur append)
  → InvestigationService.execute_tool_call
      (invocation + réservation + RUNNING)
  → dispatcher → adapter → observation blob/référence
  → ResearchContextBuilder.build (contexte complet)
      ou read_context_section_page (sections paginées)
  → GET /operator/context, /operator/context/section/{section}
  → CLI export_investigation_audit.py
      → read_investigation_audit_snapshot
      → build_audit_bundle → RUN_EVENTS / RUN_TRACE / RUN_INDEX / RUN_MANIFEST
```

Références : `operator_tool_invocation.py:700-878,1031-1103` ;
`investigation_service.py:1007-1321` ; `research_context_builder.py:1074-1168,1302-1429,2236-2412` ;
`repositories/investigation.py:145-241,377-430,546-616` ;
`context_pagination.py:27-57,82-162,608-748` ;
`infrastructure/database/investigation_audit_reader.py:48-209` ;
`investigation_audit_export.py:2568-2659`.

### 5.2 Artefacts et propriétaires

| Artefact | Producteur | Stockage | Lecteur | Version/CAS |
|---|---|---|---|---|
| Invocation, observation, finding | services/repositories | PostgreSQL ; blobs MinIO | repositories, contexte, export | `operation_key` unique, CAS de version, observation immuable |
| État de travail | `InvestigationWorkingState` | PostgreSQL JSONB + colonnes | contexte, API opérateur | `save_if_version`, égalité idempotente |
| Contexte complet | `ResearchContextBuilder.build` | blob immuable pour un tour en attente | Bridge/opérateur | hash canonique + CAS d'état |
| Pages de section | `context_pagination` + repositories SQL | lecture à la demande | API opérateur | curseur chaîné + SHA de page |
| `RUN_EVENTS` | `project_audit_events` | répertoire d'export | reprise CLI | projection d'un snapshot repeatable-read |
| `RUN_TRACE` | `_trace_bytes` | répertoire d'export | humain | dérivé, non canonique |
| `RUN_INDEX` | `_resource_index` | répertoire d'export | consommateur autorisé | références, pas de payload |
| `RUN_MANIFEST` | `_manifest_bytes` | répertoire d'export | vérification | hashes/counts, digest propre `NOT_COMPUTED` |
| Avis versionné | `AnalystOpinionService` | PostgreSQL + blobs | API/publication | versions append-only, hash de contenu |

### 5.3 Invariants et divergences

| Invariant | Propriétaire | Constat |
|---|---|---|
| Une lecture canonique ne dépend pas d'un workspace | repositories PostgreSQL + blobs | `VERIFIED` ; les workspaces sont des projections |
| Toute écriture d'état est CAS | `save_if_version` | `VERIFIED` ; l'état mémoire utilise un lock local, SQL utilise `FOR UPDATE` |
| Les pages sont ordonnées et chaînées | `context_pagination` + repositories | `VERIFIED` ; tri `(timestamp, UUID)` et `limit+1` |
| Les omissions sont observables | manifestes de page | `VERIFIED` ; `SENSITIVITY` et `TOMBSTONE` existent mais ne sont pas utilisés par ces projections |
| Le contexte complet et les sections paginées décrivent la même vérité | `ResearchContextBuilder` / `OperatorToolInvocationService` | **DIVERGENCE** : findings toutes versions vs `latest_only` ; evidence_index trié par UUID vs `observed_at/id` (`M1C` §verifications) |
| Un export d'audit est une projection déterministe | `build_audit_bundle` | `VERIFIED` ; `RUN_EVENTS` est un snapshot courant, pas l'historique des transitions |
| Le lecteur PostgreSQL d'audit est testé | aucun | **UNKNOWN** : le module le déclare non exercé (`investigation_audit_reader.py:1-4`) |

### 5.4 Tests principaux

Voir `TEST_SURFACE.md` §4. Les primitives de pagination sont couvertes par
des tests purs et des spies SQL ; le lecteur d'audit PostgreSQL n'a pas
d'oracle réel. Le test PostgreSQL
`test_operator_context_summary_postgres.py::test_operator_context_summary_and_pages_preserve_postgres_cas`
couvre une première page et le CAS, pas une chaîne multi-page complète.

## 6. Flux D — Publication

### 6.1 Graphe d'appel

```text
artifact PUBLICATION vérifié (V4/V5/V6)
  → PublicationRenderService._prepare (validation, lineage, hash)
  → TypstRenderer.render (projection déterministe, template/fonts)
  → TypstRenderExecutor → TypstSubprocessCompiler
  → PDF + EditionRender/PublicationRender (blob, SHA, taille)
  → inspection PDF (hash, pages, polices, diagnostics)
```

Chaîne édition sœur :

```text
PublicationManifestV1
  → EditionDocumentV2 → EditionRelease
  → EditionRenderService.render_pdf → EditionTypstRenderer → PDF
```

Références : `application/publication_rendering.py:244,672,714,756,847` ;
`domain/publication_document.py:2525` ; `application/typst_rendering.py:801,971` ;
`application/typst_render_execution.py:112,172,231` ;
`infrastructure/typst_compiler.py:266,389` ;
`application/edition_publication.py:909` ; `application/edition_rendering.py:192,354`.

### 6.2 Invariants et constats

| Invariant | Propriétaire | Constat |
|---|---|---|
| L'artifact canonique reste la source de l'article | artifact PUBLICATION vérifié | `VERIFIED` |
| Un V6 ajoute l'avis approuvé sans remplacer le V5 | `AnalystPublicationService` + validation de domaine | `VERIFIED` ; base exacte, avis/version/hash vérifiés |
| Le PDF final ne peut pas usurper un avis | `PublicationRenderService` + `TypstRenderer` | `VERIFIED` pour V6 ; un V6 sans métadonnées d'augmentation est rejeté |
| La provenance du rendu est complète | render record + `pdf_lineage` | `VERIFIED` pour artifacts/template/fonts/compilateur ; `binary_sha256=null` ; les acteurs reviewer/approver ne sont pas recopiés dans la lineage PDF (`M1D`) |
| Toute entrée canonique atteint le PDF | aucun oracle universel | **UNKNOWN** ; l'incident Mission 02 (5 entrées de chronologie omises) et le test de régression ciblé le montrent |

### 6.3 Tests principaux

Voir `TEST_SURFACE.md` §5. Le test réel Typst de chronologie et le test V6
couvrent des fixtures synthétiques ; le test PostgreSQL V6 couvre le JSON
canonique ; aucun test ne combine document publié complet → PDF final avec
une chronologie exhaustive.

## 7. Registre des constats

Les identifiants sont stables et repris dans `DESIGN_OPTIONS.md`,
`IMPLEMENTATION_PLAN.md` et `REVIEW_AND_HANDOFF.md`.

| ID | Preuve source | Confiance | Conséquence | Décision pressentie | Oracle associé |
|---|---|---|---|---|---|
| A1 | `investigation_tools.py:412-427,619-798` ; `M1A` §d1 | VERIFIED | Un adapter/transport direct contourne l'admission et le journal d'invocation | REPLACE_CANDIDATE (S3) | Permis obligatoire à chaque entrée brute ; zéro appel sans consume |
| A2 | `investigation_service.py:1190-1226` ; `repositories/investigation.py:1414-1467` | VERIFIED | Aucune transaction sérialisée ne couvre enveloppe + réservation + invocation + tentative | REPLACE_CANDIDATE (S3) | Test de concurrence PostgreSQL révocation/consume |
| A3 | `investigation_service.py:1079-1098,1190-1226` | INFERENCE | Un worker en pause peut dispatcher après `NEEDS_REVIEW` | REPLACE_CANDIDATE (S3) | Test `RUNNING → NEEDS_REVIEW` avant consume |
| A4 | `investigation_service.py:1031-1038` ; `operator_tool_invocation.py:1330-1398` | VERIFIED | L'identité persistée est le payload brut, pas l'intent normalisé | REPLACE_CANDIDATE (S3) | Deux requêtes équivalentes → même digest d'effet |
| A5 | `investigation_advisor_tools.py:3328-3411` ; `investigation_tools.py:865-885` | VERIFIED | `advisor.consult` classé `EXTERNAL_READ` alors qu'il soumet un modèle | NARROW_FIX ou intégré au seam advisor | Classification durable/validée au dispatch |
| A6 | `bridge_investigator.py:59,1204-1354,1395-1445` | VERIFIED | Un tour Bridge peut produire plusieurs egress sous une seule réservation | BLOCKED_DESIGN tant que la portée n'est pas définie | Test de portée du permis sur retry/correctif |
| A7 | `M1A` §e ; `retrohunt.py:152-239` ; `sample_acquisition.py:282-447` | VERIFIED | Machines d'état durables parallèles, pas de transaction commune | DEFER/NARROW_FIX selon le seam retenu | Test de cohérence inter-états |
| A8 | `investigation_tools.py:799-801` ; `investigation_service.py:1238-1321` | VERIFIED | Un refus pré-transport d'adaptateur devient `NEEDS_REVIEW` | NARROW_FIX (preuve framework) | Refus pré-transport typé sans nouvelle clé |
| A9 | `operator_tool_invocation.py:700-761` ; `investigation_tools.py:619-642` | VERIFIED | Les verrous sont locaux au processus | REPLACE_CANDIDATE (S3) | Test multi-processus PostgreSQL |
| A10 | `investigation_tools.py:363-409` ; registration modules | VERIFIED | La classification d'effet n'est pas persistée et peut changer au runtime | NARROW_FIX (figer/valider) | Test de définition immuable au moment du dispatch |
| B1 | `investigation_advisor_tools.py:362-435` ; `advisor_dossier.py:304` | VERIFIED | Préparation, divulgation, dossier et préflight sont dans l'adaptateur, après le dernier contrôle générique | REPLACE_CANDIDATE (advisor) | Prompt préparé + refs + hash liés à l'effet |
| B2 | `investigation_advisor_tools.py:425-443` ; `invariant_proposals.py` redaction | VERIFIED | Redaction dupliquée entre P10, advisor et publication | NARROW_FIX ou seam partagé | Matrice de redaction sans faux positif CTI |
| B3 | `M2B` §rows 2–3 | VERIFIED | Matrice croisée autorisation/refus absente ; résidu JSON échappé non couvert | NARROW_FIX (S2) + oracle advisor | Credential échappé absent, preuves CTI intactes |
| B4 | `investigation_advisor_tools.py:764-778` ; `M1B` | VERIFIED | La soumission advisor est un helper, pas un seam de décision durable | REPLACE_CANDIDATE (advisor/S3) | Reparse sans nouvelle consultation ; provenance brute |
| B5 | `invariant_proposals.py:1226-1342,262-275,884-918` ; `domain/invariant_proposals.py:229-243` | VERIFIED | Un `STATUS: refused` P10 peut devenir un warning et laisser passer des candidats vers P09 | NARROW_FIX | Refus + candidats → aucun enregistrement P09, refus modélisé |
| B6 | `investigation_advisor_tools.py:2376-2446,1638-1648,2143-2155` | INFERENCE | Les métadonnées d'un échantillon restreint peuvent être projetées malgré le filtrage du contenu | Décision de politique requise ; NARROW_FIX si les métadonnées sont couvertes | Inventaire d'échantillon restreint absent du prompt |
| B7 | `investigation_advisor_tools.py:3442-3487,4188-4225` | VERIFIED | `suricata_draft` n'est pas accepté par le reparse alors que son contexte est capturé | NARROW_FIX ou documenter | Reparse Suricata sans Bridge |
| B8 | `investigation_advisor_tools.py:3629-3637,4689-4780,5067-5105` | VERIFIED | Deux parseurs sémantiques divergent sur alias/warnings/texte sûr | REPLACE_CANDIDATE (parseur) ; sinon NARROW_FIX | Corpus gelé ancien/nouveau + reparse |
| B9 | `analyst_opinion_preview.py:181-187,278-301` ; `api/analyst_opinion.py:511-537` | VERIFIED | Un `protocol_model_blob_id` temporaire n'apparaît pas dans la lineage du preview | DEFER (décision de lineage) | Preview avec source temporaire identifiée |
| B10 | `investigation_advisor_tools.py:4453-4466,4630-4648` ; `advisor_wire.py:94-106,153-177,229-257` | VERIFIED | L'agrégation de refs peut écraser ou retenir silencieusement deux SHA pour un même `(kind,id)` ; un alias peut diverger du manifeste | REPLACE_CANDIDATE (seam advisor) ; NARROW_FIX si le seam est refusé | Même `(kind,id)` + SHA différent → `prepare` refuse ; alias ⊆ refs transmises |
| B11 | `investigation_advisor_tools.py:741-778` ; `production_artifact_store.py:50-53` ; `bridge_investigator.py:248-252` | VERIFIED | Le prompt est haché, archivé et soumis via des valeurs séparées ; l'égalité octets/hash n'est pas imposée par l'interface | REPLACE_CANDIDATE (seam advisor) | Octets préparés = archivés = transmis ; hash vérifié au seam S3 |
| B12 | `investigation_advisor_tools.py:362-376` ; `bridge_investigator.py:261-282` | VERIFIED | Le transport advisor truste le booléen `external_llm_allowed` du caller ; la politique n'est pas revérifiée au consume | REPLACE_CANDIDATE (S3) + matrice caller-level | Chaque refus (external=false, DNT, RED, restriction échantillon) → zéro appel S3 |
| C1 | `research_context_builder.py:1101-1110,2334-2355` | VERIFIED | Contexte complet et section paginée diffèrent sur les findings | NARROW_FIX / décision de contrat | Test de cohérence full/paged |
| C2 | `research_context_builder.py:3186-3214,2301-2333` | VERIFIED | Ordre de l'evidence_index diffère entre full et paginé | NARROW_FIX / décision de contrat | Test d'ordre et de budget |
| C3 | `investigation_audit_reader.py:1-4` | VERIFIED | Le lecteur PostgreSQL d'audit n'est pas exercé | NARROW_FIX (oracle PG réel) | Export réel depuis PostgreSQL jetable |
| C4 | `investigation_audit_export.py:900-1055,1668-1776` | VERIFIED | `RUN_EVENTS` est un snapshot courant, pas l'historique des transitions | KEEP (documenté) / NARROW_FIX si un consommateur exige l'historique | Test de manifeste et de gaps |
| C5 | `context_pagination.py:82-162` ; `operator_tool_invocation.py:454-509` ; `M4C` §BLOCKED_DESIGN | INFERENCE | Le curseur est lié au SHA de l'état de travail, pas à un snapshot de source ; `context_sha256` a deux sens selon le type de page | BLOCKED_DESIGN (décision de contrat) | Test multi-page avec insertion concurrente ; sémantique de `context_sha256` |
| C6 | `research_context_builder.py:92-119,3148-3183` | VERIFIED | La troncature par résumé n'est signalée que par l'ellipse ; `latest_tool_results` n'a pas de compteur dédié | NARROW_FIX | Manifeste de troncature complet |
| C7 | `investigation_service.py:2037-2070` ; `investigation_audit_export.py:913-915,1058-1084` | UNKNOWN | La réciprocité `response_refs` ↔ `invocation_ref` n'est pas cross-checkée par l'export | NARROW_FIX (oracle) | Paire incohérente → export refuse ou signale |
| D1 | `M1D` §completeness ; `docs/mission02/TYPST_REAL_PDF_AUDIT.md:77-101` | VERIFIED | Aucun oracle universel canonique → PDF ; incident chronologie | NARROW_FIX (oracle de complétude) | PDF publié avec toutes les entrées |
| D2 | `publication_rendering.py:714,756,847` ; `M1D` résumé | VERIFIED | La lineage PDF n'expose pas les acteurs reviewer/approver recopiés | NARROW_FIX | Lineage avec acteurs et hash de base |
| D3 | `publication_rendering.py:582,806` | VERIFIED | `binary_sha256=null` ; la version du compilateur est pinnée, pas son binaire | KEEP/DEFER (limite de provenance) | Hash du binaire ou décision documentée |
| T1 | `M2A` | VERIFIED | 0/10 oracles de permis au consume | couvert par le plan S3 | Tests listés dans `TEST_SURFACE.md` |
| T2 | `M2B` | VERIFIED | 4 oracles majeurs manquants (S2, audit PG, pagination chaînée, PDF final) | lots de test dédiés | Voir `IMPLEMENTATION_PLAN.md` |

## 8. Matrice de propriété des invariants

| Invariant | Propriétaire actuel | Propriétaire cible éventuel | Callers qui n'auront plus à le vérifier |
|---|---|---|---|
| Admission d'effet + décision durable | service + dispatcher + adapters (éclaté) | seam S3 (si approuvé) | opérateur, Bridge, chaque adapter |
| Identité d'effet normalisée | fingerprint heuristique + adapters | seam S3 / préparation typée | service opérateur, adapters |
| Portée de l'autorisation sur retry/correctif | Bridge (`before_submission`) | seam S3 (décision explicite) | `BridgeInvestigator.turn`, advisor |
| Divulgation et redaction CTI | service + advisor + P10 + publication | seam advisor (si approuvé) + P10 narrow | adapters, tests |
| Complétude et provenance de l'avis | `advisor_dossier` + `advisor_contracts` | seam advisor | `InvestigationAdvisorTool`, reparse |
| Pagination/omissions | `context_pagination` + builders | KEEP (primitives) + narrow fixes | API opérateur |
| Export d'audit | CLI + reader + pure builder | KEEP + test PG réel | script CLI |
| Complétude canonique → PDF | renderer + template | KEEP + oracle de projection | `PublicationRenderService` |
| Version/CAS d'état | repositories PostgreSQL | KEEP | services/tests |

## 9. Contradictions et questions ouvertes

1. **Findings full vs paged** : le contexte complet peut inclure des versions
   superseded, la section paginée ne renvoie que `latest_only`. Aucun contrat
   ne tranche explicitement ; à décider avant tout refactoring (`C1`).
2. **Evidence index** : ordre UUID dans le contexte complet, ordre
   `observed_at/id` dans les pages. Les deux peuvent être légitimes, mais
   l'interface ne documente pas la différence (`C2`).
3. **Curseur opérateur** : le SHA de contexte est un CAS d'état de travail,
   pas une promesse de snapshot de source. Un walk multi-page peut voir des
   insertions. À documenter ou corriger (`C5`).
4. **`RUN_EVENTS`** : projection d'un snapshot courant, pas un journal de
   transitions. Le manifeste le dit ; les consommateurs doivent le savoir
   (`C4`).
5. **Advisor classé `EXTERNAL_READ`** : le libellé est faux au regard de
   l'effet réel ; la décision de le corriger touche le registre et la
   politique, pas seulement un commentaire (`A5`).
6. **Portée Bridge** : une réservation `BRIDGE_TURNS` couvre un tour logique
   avec plusieurs soumissions possibles ; le contrat ne dit pas si
   l'autorisation couvre chaque soumission ou le tour borné (`A6`). C'est un
   point `BLOCKED_DESIGN` tant qu'il n'est pas tranché.

## 10. Ce que cette carte ne prouve pas

- Aucune concurrence PostgreSQL multi-processus n'a été exécutée.
- Aucun fournisseur, Bridge, VT, Shodan, Retrohunt ou MinIO réel n'a été
  contacté.
- Les tests cités sont des inventaires ou des exécutions explicitement
  consignées dans `TEST_SURFACE.md` ; un test non exécuté n'est pas un PASS.
- Les fichiers `RUN_*` et les PDF réels existants restent des observations
  historiques, pas des preuves du runtime courant.
