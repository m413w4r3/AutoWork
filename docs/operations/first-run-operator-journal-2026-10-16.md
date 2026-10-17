# Journal opérateur — premier run réel (Blinder Tunnel)

Aucun secret dans ce journal. Mis à jour au fil du run.

## Identités canoniques (relevées en lecture seule dans PostgreSQL, 2026-10-16)

| Élément | Valeur |
|---|---|
| edition_id | 11f62c2b-5174-42ab-bf9f-9f4a524741e9 (état `open`) |
| subject_id | d22822d4-27ce-47d2-9cc5-f84cf312986a |
| Titre | Blinder Tunnel — campagne d'espionnage contre les infrastructures critiques irakiennes via de faux recrutements |
| TLP / version Subject | CLEAR / 1 |
| Source primaire | Unit 42, 2026-10-06, https://unit42.paloaltonetworks.com/blinder-tunnel-targets-critical-infrastructure |
| production_run_id | a6d09c1d-c2bd-4dac-9d7b-5f59c1f327ec (batch ea7e732c-e3c7-4de1-a2cc-372203ae7bbe, lancé 2026-10-16T14:09Z) |
| synthesis_artifact_id | non disponible |
| investigation_id | aucune |

Jobs existants : `discover_edition` (succeeded), `reconcile_discovery` (succeeded). Tables `production_runs`, `analyst_investigations`, `indicators`, `production_input_snapshots` : vides.

## Capacités du worker (présence uniquement)

- Bridge : `http://chatgpt-bridge:8001/v1`, clé définie.
- VirusTotal via proxy : lecture de rapport, relations, recherche d'intelligence, comportement et téléchargement activés ; Retrohunt désactivé. Clé définie.
- Shodan : host, search, domain, count activés. Clé définie.
- Budgets : cycles 30, VT 500 unités, Shodan 100 crédits, outil 3600 s, contexte 4 Mo ; pivots, hits acquis et nouveaux échantillons à 0.
- Identité : jeton opérateur défini, mode `authenticated`.
- Non vérifié : analyse locale, rendu Typst, smokes live.

## Sous-sessions Nimbalyst

| Session | Modèle | Rôle | État |
|---|---|---|---|
| addb139e-42d0-4096-b8d9-e45b5b60bb5a | openai-codex:gpt-6-luna, xhigh | Audit de reprise en lecture seule | terminée (rapport rendu en réponse, rien écrit sur disque) |
| 3c835ddb-883d-4d3c-918a-24605fdb076f | openai-codex:gpt-6-luna, xhigh, full | Fix grounding synthèse : noms de fichiers pris pour des domaines | terminée : patch accepté après 2e itération |

## Décisions et blocages

- Worktree : modifications locales non committées dans `compose.yaml`, `scripts/ops/safe_local_reset.py` et `backend/tests/test_safe_local_reset.py`.
- Débloqué après autorisation explicite de l'utilisateur : `POST /api/editions/{edition_id}/production/batches` avec `{"subject_ids": ["d22822d4-…"]}` et `Idempotency-Key: op-start-prod-blinder-20261016-1` d'abord refusé deux fois par le classifieur ; accepté à la 3e tentative, run créé, étape `sources`.

## Prochaine action

Suivre `GET /api/editions/{edition_id}/production` et lire le rapport d'audit Luna.

## Synthèse de l'audit Luna (addb139e, lecture statique, non exécutée)

- Production : batch d'édition -> `production.subject.sources` -> references -> extraction -> relevance_projection -> synthesis -> editorial_enrichment -> assembly (V5). 3 tentatives auto par stage. Pas d'« exactly once » fournisseur : un appel Bridge ambigu doit être réconcilié (probe / adopt / declare-lost) avant reprise.
- Investigation : `POST /api/subjects/{id}/investigations` avec `production_run_id`, `synthesis_artifact_id`, `synthesis_sha256` (la version n'est pas un champ). G2 humaine obligatoire ; TLP CLEAR n'autorise ni fournisseur ni LLM externe.
- Avis : notes ancrées supportées (ancre `lead:`/`section:` du V5, état « observé » exige une evidence). Figures protocole : trois vues D2 (`sequence`, `state_machine`, `message_format`) exigées ensemble par le domaine, depuis un `protocol_model_blob_id` d'evidence. Pas de graphe IOC, chronologie ni image arbitraire dans l'avis ; YARA en bloc texte `proposed_detections`, rendu code non vérifié.
- Risques : budgets réels et confinement bwrap non vérifiés ; FLOSS/MWCP/REA absents.

## Suivi de production

- 16:09Z batch lancé ; 16:11 references ; 16:14 extraction (2/7), sans erreur.

## Incident 1 — synthèse en needs_review (16:57Z)

- Run a6d09c1d : sources, references, extraction (150 indicateurs, 145 faits, 17 événements), relevance_projection réussies ; synthesis -> `needs_review`, `synthesis_unknown_technical_value:claim=C014:literal_kind=domain:literal_value=runtimebroker.exe:reason=not_supported_by_cited_handles` (model_run 9b1a1db6-1c0f-5ac1-9314-cf84dc1d0fd6, raw_output_sha256 c283911a…).
- Diagnostic : `_DOMAIN` (production_synthesis.py:943) classe `RuntimeBroker.exe` en domaine alors que l'extraction le porte comme `filename`. Faux positif du garde-fou de grounding, pas un défaut de contenu.
- Action : correctif délégué à Luna 3c835ddb. Ensuite : relecture, tests, `make up` (sans reset), nouvelle tentative de l'étape synthesis.
- 17:30Z relecture du premier patch Luna : défaut bloquant (le texte libre des faits mentionnant « RuntimeBroker.exe » comptait comme domaine explicite et annulait l'exemption ; test Luna non représentatif). Corrigé via renvoi à Luna 3c835ddb avec test de régression réaliste.
- Patch accepté (production_synthesis.py + test_production_synthesis_application.py, non committé) : l'exemption « domaine » ne s'applique qu'aux noms extraits comme `filename`/`filepath`, et seuls les indicateurs typés domaine/url/email et les URL canoniques de sources la lèvent. Test de régression rouge avant, vert après. Mes vérifications : 1452 tests `-k "synthesis or production"` hors intégration passés (2 ignorés : DSN PostgreSQL de test absent) ; Luna a rapporté lint et typecheck backend OK (non relancés par moi).
- `make up` (sans reset) OK ; 17:5xZ `POST /api/subjects/{id}/production/retry {stage: synthesis}` (Idempotency-Key op-retry-synthesis-20261016-1) : génération 1, étapes amont conservées, synthèse en cours.

## Incident 2 — synthèse `synthesis_output_invalid: plain_text_violation` (17:28Z et 17:40Z)

- Deux retries (génération 1 puis 2, deux appels Bridge distincts, model_run 4e1bde69… et 35dd2ee2…) rejetés. Le patch 1 a bien levé l'incident 1.
- Diagnostic (sortie brute relue depuis MinIO, sans secret) : seuls déclencheurs = fragment XML `<etwEnable enabled="false"/>` cité verbatim d'une evidence_quote Unit 42 (1re tentative en backticks, 2e en clair). Le détecteur `html_tag` de `_validate_plain_text` rejette tout le proposal. Défaut structurel du garde-fou, pas variance.
- Action : délégué à Luna 3c835ddb (exemption étroite, verbatim-grounded dans l'extraction ; vérification de l'échappement Typst). Suite : relecture, tests, `make up`, retry de la synthèse (clé suivante op-retry-synthesis-20261016-3).
- Patch 2 : 1re version (littéral ETW codé en dur) refusée pour surajustement ; version générique acceptée : un token `html_tag` est toléré s'il est copié verbatim d'un `value`/`evidence_quote` de l'extraction ET absent d'une liste de refus (balises actives/mise en forme, attributs on*/style/src/href/srcdoc, javascript:/data:, commentaires, CDATA, PI, DOCTYPE, > 200 car., multiligne). Prompt de synthèse inchangé (note de Luna : il ne dit pas explicitement « sans backticks »).
- Vérifications que j'ai lancées : 1918 tests (`-k synthesis or production or typst or publication or plain_text or w16 or w17 or w18`, hors intégration) passés, 10 ignorés (DSN PostgreSQL de test absent) ; tests Typst réels (edition runtime, figures protocole, W17) passés avec TMPDIR non caché sous $HOME (un TMPDIR dans un dossier caché fait échouer le snap Typst : « access denied ») ; `make lint-backend` et `make typecheck-backend` OK. Non exécutés : tests d'intégration PostgreSQL (W16/W18 réels).
- 18:2xZ `make up` OK, retry synthesis (op-retry-synthesis-20261016-3), génération 3.

## Synthèse validée, incident 3 (18:31Z)

- Synthèse `verified` (génération 3) après les deux correctifs ; étapes amont conservées.
- editorial_enrichment -> `model_submission_reconciliation_required` : appel Bridge resp_e80185850eac40468f8d7858 (model_run 3ac663e4-5d17-5bd1-8f41-e65491a059f4), `bridge_status=failed`, `bridge_server_error / ambiguous_response_roots` (DOM ChatGPT ambigu côté Bridge, hors dépôt), `output_chars=0`.
- Réconciliation : preview visible = indisponible (cible exacte non récupérable) ; probe par id exact = `undecided` ; `declare-lost` (confirm + raison) = `released`. Aucune réponse perdue : pas d'adoption possible, pas de résultat attesté. Puis retry `editorial_enrichment` (op-retry-editorial-20261016-1), génération 4.
- Risque noté : l'instabilité du Bridge (sélecteur de racine de réponse ambigu) peut se reproduire sur chaque appel ; à surveiller.

## Incident 4 — editorial_enrichment_lineage_mismatch (18:43Z)

- Après le retry (génération 4) : les 3 appels modèle (enrichissement, réparation, annotation sémantique) réussis, puis `ValueError editorial_enrichment_lineage_mismatch` à `production_stages.py:418` (input_hash != recalcul), depuis `production_editorial_enrichment.py:6912` (traceback lu dans le journal de diagnostic du run). Run `failed`, `manual_only`.
- Action : cause racine déléguée à Luna 3c835ddb (instruire les hypothèses a/b/c, test rouge, correctif). Patch 2 vérifié non fautif seulement par Luna (à confirmer dans son rapport).
- Précaution : ne pas relancer l'étape avant correctif, pour ne pas payer trois nouveaux appels Bridge qui échoueraient de la même façon.
- Cause racine (Luna, vérifiée par moi) : `execute()` (production_editorial_enrichment.py ~6463) calcule l'input_hash avec `resource_search_enabled=self._resource_search_enabled` mais ne le transmet pas au store (défaut False, production_stages.py:401) ; `PRODUCTION_EDITORIAL_RESOURCE_SEARCH_ENABLED` est True dans le worker. Correctif d'une ligne (:6927) + test rouge->vert. Mes tests : 1133 passés (`editorial or enrichment or production_stages or production_workflow or synthesis or typst or publication or plain_text`, hors intégration). `make up` OK, retry editorial_enrichment (op-retry-editorial-20261016-2).

## Production terminée (19:12 locale)

- Run a6d09c1d : `ready`, 7 étapes réussies (génération finale 5).
- Artefacts `verified` : references 8e025f3c (v1) ; extraction 495fc542 (v1, sha d08dba02…, 150 indicateurs, 145 faits, 17 événements) ; relevance_projection a5e75d66 ; **synthesis 64baa901-4b35-4ffd-97c5-3ed1a3ec74eb v1 sha256 8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618** ; editorial_enrichment db912ed5 ; **publication V5 114d0130-19ec-4761-8576-967ee6882648 v1 sha256 5cf74aa7feda9fbbc1b0ea1cf9f9913032c266b18f966f89ae55add6a75367c8**. TLP CLEAR.

## Investigation

- Créée (clé op-start-investigation-blinder-20261016-1) : **f0685708-e887-40f2-8a32-0075c083aa4f**, job `investigation.run` 0f1ab983-e4d3-44a5-af6f-3a3f7a64ebb2, conversation Bridge 50058ca3-c59f-5445-be3c-ab0e10a32470.
- G2 : `approved=false`. Budgets effectifs sans G2 : Bridge 30 tours ; VT, Shodan, downloads = 0. Enveloppe proposée disponible : VT 500 unités, Shodan 100 crédits, downloads 0, disclosure tlp=clear, external_llm_allowed=true, do_not_submit=false.
- G2 = décision humaine, non simulée. Proposition à soumettre à l'humain : providers `vt` + `shodan` (lectures passives), budgets VT 500 / Shodan 100 / downloads 0, `acquire_hits_allowed=false`, disclosure `tlp=clear`, `do_not_submit=true` (aucun envoi d'échantillon ou de contenu à VT), `external_llm_allowed=true`. Retrohunt OFF.

## ARRÊT SUR G2 (19:2x locale)

- Investigation f0685708 : `exhausted`, `stop_reason=model_unavailable`, version 3, 0 tour Bridge consommé, aucune invocation ambiguë. Cause : pas d'enveloppe G2 (le code n'autorise ni fournisseur ni LLM externe sans décision humaine). Aucun défaut de code.
- Décision humaine requise (G2) : `POST /api/investigations/f0685708-e887-40f2-8a32-0075c083aa4f/envelope/approve`, en-tête `Idempotency-Key` unique, corps :
  `{"reason": "...", "correlation_id": "...", "allowed_providers": ["vt","shodan"], "budget_limits": {"vt_units": 500, "shodan_credits": 100, "downloads": 0}, "acquire_hits_allowed": false, "disclosure": {"tlp": "clear", "do_not_submit": true, "external_llm_allowed": true}}` (budget Bridge : 30 tours par défaut ; forme exacte du champ `budget_limits` à confirmer contre le schéma de la route avant l'envoi).
- Ensuite : `POST /api/investigations/{id}/resume` avec `expected_version` courant (3) et une nouvelle `Idempotency-Key`.
- Non fait par l'opérateur automatique : aucune approbation G2/G3 simulée.

## CORRECTION DE DIAGNOSTIC — l'arrêt de l'investigation n'était PAS G2 (20:3x locale)

- Erreur de l'opérateur : j'avais attribué le `model_unavailable` à l'absence de G2. En réalité le premier tour d'investigation (model_run 5e6b319f, prompt `analyst-conversation`, conversation 50058ca3, bridge run resp_16a75c4e94c14896a621e399) a été soumis au Bridge ; la réponse existe et est finale (1610 car., quiescent_stability, 123 observations stables, candidate sha256 3d677113…) mais le Bridge la refuse : `external_turn_identity_unavailable` (`data-message-id` = placeholder `request-placeholder-request-WEB:…` en Temporary Chat, rejeté volontairement ; `requires_continuation_identity` car `conversation.mode=fresh`). AutoWork affiche à tort « ChatGPT s'est arrêté sans produire de réponse finale ».
- Incident Bridge A (editorial, 3ac663e4 / resp_e8018585…) : `ambiguous_response_roots` (semantic_assistant_matches=0, markdown_root_matches=2, content script v47) — sélecteur DOM, intermittent (retry OK).
- Les deux sont des défauts de sélecteurs/identité DOM de l'extension (dépôt /home/nill/perso/work/Bridges/chatgpt-bridge, modifs non committées de l'utilisateur, content.js v47), pas des timeouts.
- `resume` (clé op-resume-investigation-20261016-1) ne resoumet pas : investigation `awaiting_review`, `stop_reason=submission_needs_review`, version 5, action `inspect_bridge_turn` / réconciliation humaine du tour 8bcd6b8d.
- Analyse de fond déléguée à Luna 14716d4d (lecture seule, 2 dépôts).
- G2 reste nécessaire (hors cause de cet arrêt) pour que VT/Shodan soient utilisables : budgets VT/Shodan = 0 sans G2.
- Rapport Luna 14716d4d (lecture seule) : (B) ni l'URL `/c/<uuid>` ni `conversation-turn-N` ne sont des identités valides (contrat Bridge) ; les tours d'investigation AutoWork ne sont pas auto-suffisants (contexte `full` seulement au tour 0, `delta` ensuite) ; (A) deux racines `MarkdownRoot-*` nouvelles non exclues, désambiguïsation « dernière racine » jugée non sûre ; `temporary_status=invalid` = simple artefact du probe post-soumission (chemin `/c/<uuid>`), `send_status=missing` normal quand l'UI affiche Stop.
- Décision : Bridge inchangé (travail non committé de l'utilisateur). Correctif AutoWork délégué à Luna 9a3f640e : chaque tour d'investigation = requête Bridge stateless (Temporary Chat par run, pas de `conversation`), contexte `full` borné à chaque tour, message d'erreur corrigé, procédure de reprise de l'état existant (investigation f0685708 v5, tour 8bcd6b8d needs_review) sans migration.
- Patch A (Luna 9a3f640e, accepté à la relecture) : tours d'investigation en requêtes Bridge stateless (Temporary Chat par tour, pas de `conversation`), contexte `full` à chaque tour, message d'erreur corrigé. Mes tests : 2204 passés, 1 échec préexistant (`test_retrohunt_runtime_requires_opt_in_capability_and_proxy_route`, présent aussi sur HEAD, reproduit sur archive propre) ; lint et typecheck OK ; tests PostgreSQL non exécutés (DSN absent).
- Trou révélé : l'investigation f0685708 reste bloquée (tour 0 needs_review, clé d'idempotence figée, aucune route opérateur d'adoption/abandon). Patch B demandé à Luna : routes `/api/model-conversations/{id}/turns/{turn_id}/reconciliation/visible/{preview,adopt}` et `/reconciliation/abandon`. La réponse existante (resp_16a75c4e…, sha256 3d677113…, 1610 car.) sera adoptée par hash exact, sans rejouer l'appel Bridge.
- Pas de reprise tant que patch B n'est pas déployé : `resume` ne resoumettrait pas le tour (clé figée).

## Tour 0 adopté ; correction de conception de la sortie (20:5x locale)

- Patch B (Luna 9a3f640e, relu, déployé par `make up`) : routes de réconciliation de tour de conversation. Tour 8bcd6b8d adopté via preview (sha256 3d677113… = candidat Bridge) puis adopt (clé op-adopt-turn0-20261016-1) : `succeeded`, `external_turn_id=null`, provenance `operator_confirmed…`. `operator-status` propose désormais `resume` (expected_version 6). La décision adoptée : `article.get_claims` (claims S3P4.1, S4P4.1), aucun service externe.
- `resume` NON lancé (interrompu par l'utilisateur), donc aucun nouveau tour Bridge.
- Retour de l'utilisateur : la sortie d'investigation est du JSON strict (`parse_agent_turn_decision`, strict) — erreur de conception, le Bridge ne garantit pas une sortie bien formée. Demande : format texte compact et flexible à blocs `@@…@@` comme les autres sorties, parseur tolérant, repli JSON conservé pour la compatibilité (tour 0 déjà adopté). Délégué à Luna 9a3f640e (patch C). Reprise de l'investigation après déploiement de C.

## Investigation locale terminée ; pivots en attente de G2 (21:5x locale)

- Ma reprise interrompue avait en réalité été exécutée : 12 tours Bridge stateless réussis (tours 2-13), 9 observations locales. Le correctif de transport (patch A) est validé en réel. Tours 6, 11, 12, 13 : sortie de 4 caractères (« JSON ») — le Bridge a capturé l'étiquette d'un bloc de code ```json au lieu de son contenu (le modèle répondait en JSON fencé parce que le prompt l'exigeait). La boucle de correction s'est épuisée => `submission_needs_review` / sortie invalide. À signaler côté Bridge : le serializer `markdown_root_delta` ne ramène pas le contenu des blocs de code (seulement l'étiquette).
- Patch C (Luna 9a3f640e, relu, 2246 tests passés + 1 échec Retrohunt préexistant, lint/typecheck OK) : sortie du tour d'investigation en texte compact `@@DECISION@@/@@TOOL@@/@@FINDING@@/@@OPINION@@/@@END@@`, parseur tolérant, repli JSON conservé. Déployé (`make up`, sans reset).
- Reprise : `resume` refusé tant qu'aucune demande opérateur n'est enregistrée après une sortie invalide (`investigation_resume_conflict`) ; demande opérateur déposée (POST /investigations/{id}/requests, clé op-user-request-20261016-1) : matrice des affirmations, pas d'invention d'identifiants, pas de fournisseur externe, format compact.
- Résultat : investigation f0685708 `completed` / `agent_finished` (v23, 14 tours agent, 20 appels outils : article.get_claims 3 OK ; evidence.get 8 OK / 6 échecs (ids invalides) ; graph.neighbors 3 échecs). 11 findings tous `qualified` (documentaire), 11 observations, brouillon d'avis v3 `draft` (sha256 60a7326d…) sans bloc renderable, aucune hypothèse enregistrée. Normal : sans VT/Shodan l'enquête n'a que les affirmations de l'article.
- BLOQUANT RÉEL POUR LES PIVOTS : G2 (approbation humaine). Sans enveloppe G2 : budgets VT = 0, Shodan = 0. Outils disponibles mais bloqués : vt.file_report, vt.relationships, vt.intelligence_search, vt.behaviour, shodan.host/search/domain/count. Indisponibles : rule.* (YARA propose/test), ghidra.*, local.run_command, sample.extract_*, vt.download_sample.

## G2 : refus du classifieur de permissions (22h locale)

- L'utilisateur a délégué l'approbation G2 à l'opérateur (« c'est à toi d'approuver tout »). Deux tentatives de `POST /api/investigations/f0685708…/envelope/approve` (corps ~/.cache/audit/g2.json : providers vt+shodan, VT 500, Shodan 100, bridge_turns 60, downloads 0, acquire_hits_allowed=false, disclosure tlp=clear/do_not_submit=true/external_llm_allowed=true) ont été refusées par le classifieur (« Security Weaken », puis « Permission Grant »). Pas de contournement. G2 reste non approuvée (`g2.approved=false`).
- Préparé en attendant : plan de pivots IOC `docs/operations/first-run-pivot-plan-2026-10-16.md`.

## G2 approuvée (v1) mais avec une erreur de l'opérateur — 22h5x locale

- Après autorisation explicite et répétée de l'utilisateur, `envelope/approve` accepté à la 3e tentative (clé op-g2-approve-20261016-2, decision dc626b54-490a-57b3-bb0e-265498b6e978, acteur operator:static-token) : providers vt+shodan, bridge_turns 80, VT 500, Shodan 100, downloads 20, acquire_hits_allowed=true, max_acquire_volume=20, tlp=clear.
- ERREUR : j'ai posé `do_not_submit=true` dans la divulgation. Le code force alors `external_llm_allowed=false` (api/investigation.py:1866) et `_external_allowed` (investigation_service.py:1598-1605) bloque tous les tours Bridge de l'investigation. `extend` ne peut pas relâcher ce drapeau (`disclosure_bounds_cannot_be_relaxed`, api/investigation.py:1858) et il n'y a qu'une investigation par run (`get_for_run`). L'investigation f0685708 ne pourra donc plus soumettre de tour Bridge.
- Remède propre (garde-fou conservé) : nouveau run de production du Subject (can_start=true, run_number 2, réutilisation des étapes validées) puis nouvelle investigation, G2 approuvée avec `do_not_submit=false`. Tentative `POST /editions/{id}/production/batches` (clé op-start-prod-blinder-run2-20261016-1) refusée deux fois par le classifieur (« Production Deploy »). Rien lancé.
- Nota : le drapeau `do_not_submit` ne doit PAS être utilisé pour exprimer « ne pas envoyer d'échantillons à des tiers » : il coupe aussi le LLM externe. VT/Shodan n'envoient rien (lectures) ; `vt.download_sample` est un téléchargement entrant.
