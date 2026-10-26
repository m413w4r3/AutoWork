# M06-P/R — Carte des émissions physiques par famille S3

Produit par l'enfant `S3-FAMILY-MAP` (Paseo `20a08f26`, codex/gpt-6-luna
high, lecture seule) sur le code de `main@a832902b` (sources identiques à
`af819b2e` hors tests), API coordinateur lue sur `m06r/s3-core-fix@8a8bc7da`.
Chemins relatifs à `backend/src/cti_app/`. **V** = vérifié dans le code ;
**I** = inférence.

## A. VT lecture

| Chemin | Primitive / comportement | Ligne persistée avant envoi et contrôles |
|---|---|---|
| `vt.file_report` | **V** `VirusTotalHttpAdapter.file_report` → `_execute_route` → `_file_report_request` → `_get` → `AsyncClient.get` (`infrastructure/virustotal.py:419,943,1224`). Route proxy v3, repli proxy optionnel, puis v2 direct si explicitement activé ; `_execute_route` n'avance que si la route l'autorise ; redirections refusées (`:966,1577,1253`). | **V** invocation `RUNNING` + réservation commitées ; `pre_dispatch_check` revérifie enveloppe/réservation ; pas de `pre_transport_check` VT (`application/investigation_service.py:1188,1360`, `application/investigation_tools.py:795`). |
| `vt.relationships`, `vt.intelligence_search`, `vt.behaviour` | **V** `_paginate` → `_get` → `AsyncClient.get` ; chaque curseur = une requête distincte ; l'outil peut boucler les pages (`infrastructure/virustotal.py:1012,1090,1123,1164` ; `application/investigation_vt_tools.py:890,1035,1204`). Proxy v3 seulement ; redirections refusées. | **V** identiques. |
| Autres lectures | **V** `object_report` utilise `_get` (`infrastructure/virustotal.py:831`). Le client Retrohunt (proxy) émet GET statut/résultats paginés et POST de soumission via `AsyncClient.stream` (`infrastructure/virustotal_retrohunt_client.py:143,169,176,183`). **I** Retrohunt = famille distincte (réservation propre). | — |

**V** Clients VT : `follow_redirects=False` (`infrastructure/virustotal.py:122,1253`) ; pas de boucle de retry applicative hors repli de route et pagination ; pas de couche SDK à retries.

## B. VT téléchargement / acquisition

| Chemin | Primitive / comportement | Avant envoi |
|---|---|---|
| `vt.download_sample` | **V** `application/investigation_acquisition_tools.py:511` → `HitAcquisitionService.acquire_hit` (`application/investigation_acquisition.py:378`) → `VirusTotalSampleAcquisitionService.acquire` → `_download_and_store` → `VirusTotalPort.file_download` (`application/sample_acquisition.py:407`). Première requête `AsyncClient.stream("GET", …/files/{hash}/download)` ; redirections suivies **manuellement** avec clients sans credential, bornées (allowlist hôtes/ports) ; sur 404 de route : `/download_url` puis URL CDN signée, ou v2 direct si activé. **Chaque saut est une requête physique** (`infrastructure/virustotal.py:2069,526,596`). | **V** budget VT/download commité ; invocation `RUNNING` + réservation ; **aucune tentative de dispatch pré-envoi** : `sample_acquisition_attempts` n'est écrit qu'après transport (`application/sample_acquisition.py:349,659`). |
| Réutilisation locale | **V** acquisition antérieure réutilisée après vérification de l'échantillon et du blob de quarantaine ; `HitAcquisitionService` revérifie les octets (`application/sample_acquisition.py:315`, `application/investigation_acquisition.py:201`). Doit rester **sans émission**. Les nouveaux octets passent par spool + contrôle SHA-256 puis `samples-quarantine` (`application/sample_acquisition.py:425,514,605`). | — |

**Écart 0004 (V)** : `external_effect_claim.acquisition_attempt_id` référence
`sample_acquisition_attempts` (registre post-réponse,
`infrastructure/database/models/external_effects.py:106`) alors que la spec
exige une table pré-envoi `sample_acquisition_dispatch_attempts`
([`S3_IMPLEMENTATION_SPEC.md`](S3_IMPLEMENTATION_SPEC.md) l. 101, 107, 111).
**I** : migration additive `0005` (table de dispatch + FK du claim + lien
nullable depuis le registre terminal) ; ne jamais autoriser la première
requête depuis une ligne post-réponse.

## C. Bridge et Advisor

| Chemin | Comportement | Lignée |
|---|---|---|
| Tour `BridgeInvestigator.turn` | **V** retries des échecs pré-soumission confirmés, pacing/backoff, un retry rate-limit borné, tours correctifs séparés ; appelle `submit`/`submit_with_attempt` (`application/bridge_investigator.py:1229,1248,1351`). `ModelConversationTurnTransport.submit` → `model_conversations.add_turn` persiste `ModelConversationTurn` + `ModelRun`, commit, puis appelle la gateway (`application/bridge_investigator.py:794`, `application/model_conversations.py:1282,1299,1304`). Clé d'idempotence incluant l'identité de tentative (`bridge_investigator.py:678`). | **I** chaque renvoi physique = un envoi coordinateur distinct lié au tour/run exact. |
| `advisor.consult` | **V** `pre_transport_check` après préparation du prompt, avant `submit_bridge_consultation_once` (`application/investigation_advisor_tools.py:1049,1087`, `application/bridge_investigator.py:263`). Envoi unique : `HttpResponsesTransport._request` → `AsyncClient.request("POST", …/responses)`, une tentative HTTP, pas de retry automatique ; `ChatGPTBridgeClient` hérite (`integrations/models.py:121,133,541`). | **V** réservation au niveau opération dans l'enveloppe ; les lots advisor créent des clés/réservations enfants (`application/investigation_service.py:3006`, `application/investigation_advisor_tools.py:1620`). |

## Câblage partagé et séquencement

- **V** Seam partagé : `ToolExecutionContext.pre_transport_check`,
  `RegistryToolDispatcher` (cache mémoire `_completed`),
  `InvestigationService.execute_tool_call` (`application/investigation_tools.py:225,622`,
  `application/investigation_service.py:1188`). La composition enregistre les
  outils VT et construit dispatcher/transport Bridge dans un seul fichier
  (`application/investigation_composition.py:1261,1625`).
- **Conflits** : A et B partagent `infrastructure/virustotal.py` et la
  composition ; C touche transport Bridge et composition.
- **Ordre recommandé (I)** : (1) API coordinateur + schéma additif, dont la
  table pré-envoi de B ; (2) A puis B via le transport VT commun
  (`virustotal.py` co-possédé, donc sérialisé) ; (3) C, un claim par tour/run
  et par envoi ; (4) intégration unique de la composition. La réutilisation
  locale vérifiée reste hors `send_one`.
