# Mission 04 / Phase 2 — S3-D normalizer, occurrence et settlement

**STATUS :** analyse lecture seule terminée ; contrat proposé, aucune implémentation.
**Base :** `e120a36f1afa08261fdf0276e0200872973ad8f2`.
**Fichier de sortie :** `docs/mission04-implementation/_work/S3D_C_VERSION_SETTLEMENT.md`.
**Gate :** `S3_READY_FOR_IMPLEMENTATION` reste **NOT MET** au point de revue R7-S3-R2 ; aucune route de production n’est autorisée par cette analyse (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:48-56`).

Les étiquettes **VERIFIED**, **INFERENCE** et **UNKNOWN** distinguent le code ou les documents effectivement observés, les conséquences déduites et les points non établis. Les oracles ci-dessous sont des propositions RED ; aucun test, PostgreSQL, serveur, fournisseur, Bridge ou service n’a été lancé.

## Décisions de contrat proposées

1. **Fail closed entre versions du normalizer.** Un changement de version ne crée ni alias, ni autorisation, ni nouvelle occurrence. La même intention logique retrouve le même claim de continuité ; si la version préparée diffère, le résultat est `NEEDS_REVIEW` et aucun nouvel envoi ne part. L’envoi après cette revue exige une occurrence distincte, créée par un workflow nommé, liée au claim parent et à une étape vérifiée. Un dispatch ambigu ne peut pas créer cette occurrence.
2. **Un seul claim logique, plusieurs dispatchs physiques contrôlés.** Le claim porte l’identité et l’occurrence ; chaque requête physique a son propre dispatch, consume et mesure. Un dispatch consommé ambigu n’est jamais renvoyé. Une nouvelle tentative n’est admissible que si le dispatch précédent est prouvé non envoyé, ou si une réponse terminale complète autorise explicitement un retry par politique de workflow.
3. **Réemploi uniquement d’une réponse complète et terminale, sous la même version et politique.** Il crée une ligne `use` reliant la source au demandeur ; aucun egress ni delta budgétaire supplémentaire. `PARTIAL`, `NEEDS_REVIEW`, `CONSUMED` sans réponse certaine, `UNAVAILABLE` et les refus ne sont pas des résultats réutilisables.
4. **Budget opérateur par enveloppe ; quotas acquisition distincts.** `investigation_envelopes.budget_snapshot` reste l’autorité de réservation/settlement des appels opérateur. Les compteurs d’acquisition VT (`VT_READ_UNITS`, `HITS_ACQUIRED`, et à définir `NEW_SAMPLES`) sont des quotas métier distincts, pas des reçus de coût fournisseur. Le ledger `sample_acquisition_attempts` établit un résultat d’acquisition, pas une unité facturée.
5. **Settlement unique et conservative.** Un settlement par clé d’opération de réservation et une mesure par dispatch/dimension. Le consume DB précède l’appel HTTP et libère les locks avant transport. Après commit, perte d’acquittement, timeout, exception de transport ou réponse partielle restent ambigus ; aucune libération ou retransmission automatique.

Ces choix ferment le défaut N4 décrit par R7 : l’identité versionnée seule peut autrement ouvrir un nouveau claim par défaut (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:32-34`). Ils complètent les points encore ouverts F3/F4 : la continuité inter-versions et la propriété budgétaire de l’acquisition n’étaient pas tranchées (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:15-16`).

## 1. Inventaire d’identité, normalisation, occurrences et réemploi

### Identité courante

- **VERIFIED — aucune identité S3 commune en code.** L’identité générique actuelle est `tool_operation_key`: SHA-256 d’un JSON canonique avec investigation, nom du tool, `schema_version` et paramètres ; `purpose` est exclu, et l’identité de cache n’est ajoutée qu’aux calculs locaux déterministes (`backend/src/cti_app/domain/investigation.py:400-427`). L’invocation journalise les arguments comme `normalized_parameters` avant dispatch (`backend/src/cti_app/application/investigation_service.py:1031-1040`). Il n’existe dans les sources/migrations examinées ni `normalizer_version`, ni `effect_key`, ni table `external_effect_dispatches` ou `external_effect_uses` ; le code courant classe seulement un tool comme `external_effect` par son enum (`backend/src/cti_app/application/investigation_service.py:1022-1039`).
- **VERIFIED — la version du schéma change déjà la clé.** `schema_version` figure dans le JSON de `tool_operation_key` (`backend/src/cti_app/domain/investigation.py:419-427`). Pour les appels `EXTERNAL_READ`, le service réutilise la clé de base ; les autres effets ajoutent un scope de tour/contexte (`backend/src/cti_app/application/investigation_service.py:1441-1452`). Certaines tentatives d’échec ajoutent aussi `:retry:<n>` (`backend/src/cti_app/application/investigation_service.py:1486-1496`). Ce sont des clés d’invocation, pas des identités versionnées du corps fournisseur.
- **VERIFIED — occurrence actuelle implicite.** Le code n’a pas de `occurrence_id` S3. Les valeurs approchantes sont le scope tour/contexte pour certains tools (`backend/src/cti_app/application/investigation_service.py:1445-1452`), les suffixes retry ci-dessus, l’opération Retrohunt, et une clé Bridge fondée sur investigation/tour/hash de contexte/attempt (`backend/src/cti_app/application/investigation_service.py:3309-3314`). Elles n’établissent pas une autorisation durable d’occurrence nouvelle.
- **VERIFIED — pas de serializer commun lié à la clé.** Les adapters forment ou valident leurs arguments localement. La sérialisation JSON canonique de `tool_operation_key` n’est pas le digest des octets effectivement envoyés (`backend/src/cti_app/domain/investigation.py:419-427`). Le design R2 propose précisément un `route_id` immuable qui lie normalizer, serializer et transport, et des digests de sémantique et de requête préparée (`docs/mission03-architecture/DESIGN_OPTIONS.md:342-350`; `docs/mission03-architecture/IMPLEMENTATION_PLAN.md:233-261`).

### Chemins actuels par famille

| Chemin | Normalisation / identité et route observées | Réemploi actuel et conséquence |
|---|---|---|
| **VT report** | `_FileReportAdapter` normalise le hash avant `file_report`, puis archive rapport brut et résumé (`backend/src/cti_app/application/investigation_vt_tools.py:686-724`). L’infrastructure résout une route VT après normalisation (`backend/src/cti_app/infrastructure/virustotal.py:419-438`). Les tools VT enregistrés sont `EXTERNAL_READ` avec un coût en unités VT/secondes (`backend/src/cti_app/application/investigation_vt_tools.py:1684-1705`). | La réutilisation vient de l’invocation générique, pas d’un claim fournisseur. Même tool/schema/arguments dans l’investigation peut reprendre l’invocation ; le replay accepte `SUCCEEDED`, `EMPTY`, `UNAVAILABLE` et transmet aussi un `PARTIAL` par la branche générique (`backend/src/cti_app/application/investigation_service.py:1951-2035`). `UNAVAILABLE`/`PARTIAL` ne satisfont pas le contrat proposé de résultat complet. |
| **Shodan** | L’adapter normalise IP ou query/page, puis appelle le port correspondant (`backend/src/cti_app/application/investigation_shodan_tools.py:665-681, 767-800`). Le client encode IP/query/page dans le chemin ou les paramètres HTTP (`backend/src/cti_app/infrastructure/shodan.py:133-178`). La réservation est l’estimation `SHODAN_CREDITS`; les résultats exposent `meta.credits_consumed` (`backend/src/cti_app/application/investigation_shodan_tools.py:1202-1222, 712-752`). | Même replay d’invocation. Les arguments génériques incluent aussi justification et références de preuve, même si la requête provider `search` n’utilise que query/page : l’identité courante peut donc différer alors que les octets de requête sont équivalents. Le coût d’infrastructure est une estimation/configuration, pas une preuve de facturation fournisseur. |
| **Retrohunt submit/status/results** | Les paramètres sont filtrés puis JSON canonique ; `_operation_key` inclut le marqueur fixe `vt-retrohunt-operation.v1`, source, corpus et paramètres (`backend/src/cti_app/application/retrohunt.py:136-150, 491-531`). La table impose l’unicité `(subject_id, operation_key)` et borne les slots (`backend/src/cti_app/infrastructure/database/models/retrohunt.py:25-62`). Le submit commit le slot puis passe `RESERVED → SUBMITTING` avant l’appel ; un replay `SUBMITTING` devient `RECONCILIATION_REQUIRED`, sans renvoi automatique (`backend/src/cti_app/application/retrohunt.py:152-197`). Status/results font aussi des lectures provider (`backend/src/cti_app/application/retrohunt.py:241-329`). | C’est une protection idempotente propre au submit, sans `normalizer_version` ni ligne `use`. Un bump du marqueur de clé peut produire une autre clé et un autre slot/submit ; l’unicité ne l’arrête pas. Status/results n’ont pas de coût VT dans leur `ToolDefinition` (`backend/src/cti_app/application/investigation_retrohunt_tools.py:81-105`). |
| **Acquisition VT** | `normalize_file_hash` donne l’objet canonique du download (`backend/src/cti_app/application/sample_acquisition.py:295-305`). `vt.download_sample` porte `ACQUIRE` et `{VT_UNITS:1, DOWNLOADS:1}` (`backend/src/cti_app/application/investigation_acquisition_tools.py:51-54, 656-681`). Le port est appelé avec le hash normalisé (`backend/src/cti_app/application/sample_acquisition.py:407-429`). | Le chemin `acquire_hit` réutilise d’abord un succès vérifié ou un sample quarantiné du même sujet et renvoie un coût opérateur nul (`backend/src/cti_app/application/investigation_acquisition.py:173-243, 245-288`). `sample_acquisition_attempts` réutilise aussi un succès vérifié par investigation/hash (`backend/src/cti_app/application/sample_acquisition.py:315-347`). Le ledger ne contient ni version de normalizer, ni dispatch, ni owner claimant/source. |
| **Bridge / advisor** | `ModelConversationService` persiste turn et run avant `ModelGateway.execute` (`backend/src/cti_app/application/model_conversations.py:1282-1305`). Le hash `authorized_input_hash` couvre la requête sanitizée et sa version de prompt (`backend/src/cti_app/application/model_gateway.py:1463-1503`), mais `serializer_version` est attachée après réponse depuis les métadonnées adapter (`backend/src/cti_app/application/model_gateway.py:1375-1380`). Le gateway persiste l’état de soumission avant transport et peut retenter des erreurs terminales avec la même identité de requête (`backend/src/cti_app/application/model_gateway.py:1051-1083, 1140-1160, 1257-1263`). | Pas de claim/dispatch commun ni lineage `use`. L’advisor réserve une unité logique `BRIDGE_TURNS`; `_bridge_turns_consumed` compte 0 avant soumission et 1 pour succès ou ambiguïté (`backend/src/cti_app/application/investigation_advisor_tools.py:3395-3407, 4008-4021`). Chaque send physique n’a pas aujourd’hui son consume S3. R7 note que le protocole de handoff au ModelGateway doit être défini et que la retry interne doit soit être gated par dispatch distinct, soit être désactivée avec oracle (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:28-38`). |

### Règle cross-version et occurrence proposée

**INFERENCE / PROPOSITION.** Séparer trois valeurs afin que la version du digest ne serve jamais de permission :

1. `continuity_key` : empreinte stable par investigation, famille/operation et projection d’intention fournisseur canonique dont le contrat ne change pas au bump de normalizer. Elle ne contient pas `normalizer_version`.
2. `versioned_semantic_digest` : empreinte de la sémantique préparée, avec `normalizer_version` et version du contrat de normalisation.
3. `request_digest` : SHA-256 des octets exacts préparés par le serializer de la route ; le transport final vérifie ce digest contre le permis one-use.

Le claim par défaut est unique sur `(investigation_id, continuity_key, occurrence_id='default')`. Le claim conserve aussi les digests versionnés et versions de route. Un bump sur la même intention retrouve ce claim : si la version diffère, le coordinateur répond `VERSION_REVIEW_REQUIRED`/`NEEDS_REVIEW`, sans réemploi cross-version et sans egress. Il n’invente pas un nouvel `effect_key`. Cela choisit le **refus fail-closed**, pas un alias inter-versions automatique.

Un workflow peut créer une occurrence explicite seulement pour une étape définie (`page`, poll/refresh stable, retry terminal certain, correction ou demande répétée autorisée), en persistant `occurrence_id`, `parent_effect_id`, étape, raison, ordinal stable et version d’état workflow. Une valeur passée directement par un caller n’est jamais une occurrence autorisée. Un parent `CONSUMED`/`NEEDS_REVIEW`/partiel interdit de créer une occurrence de retry. Pour une correction dont le contenu change, le nouveau digest et le lien parent sont obligatoires.

**UNKNOWN :** les routes actuelles ne définissent pas de projection d’intention version-indépendante. Une migration de normalizer qui rend équivalentes deux entrées auparavant distinctes nécessite une règle de continuité revue ; à défaut, aucune comparaison n’est prouvée et le chemin doit rester deny-all/review. Ne pas reconstruire ces liens depuis `tool_invocations.normalized_parameters` historiques : R7 l’interdit sans preuve (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:60-64`).

## 2. Budgets, ledgers et settlement courants

| Autorité / données | Qui réserve ou débite aujourd’hui | Interprétation vérifiée et limite |
|---|---|---|
| **Enveloppe opérateur** | `InvestigationEnvelopeRow` stocke `budget_snapshot` avec la version d’approbation et le reste de l’enveloppe (`backend/src/cti_app/infrastructure/database/models/investigation.py:547-583`). Le repository la verrouille `FOR UPDATE` et CAS sa version (`backend/src/cti_app/infrastructure/database/repositories/investigation.py:1426-1467`). `execute_tool_call` reprend le coût durable de l’invocation, réserve avant le dispatch et commit RUNNING + réservation avant l’effet (`backend/src/cti_app/application/investigation_service.py:1021-1048, 1110-1153`). | Réservation par `operation_key` et dimension ; les clés de coût persistées dans l’invocation survivent à un changement de coût du registry (`backend/src/cti_app/application/investigation_service.py:1043-1048`). Ce budget contrôle l’opération G2, il ne prouve pas qu’un fournisseur a facturé une unité. |
| **Tables `investigation_budget_*`** | ORM : `investigation_budget_records`, `limits`, `reservations`, `reservation_amounts` (`backend/src/cti_app/infrastructure/database/models/investigation.py:585-667`). Le repository a son propre snapshot/version et remplace les lignes enfants au save (`backend/src/cti_app/infrastructure/database/repositories/investigation.py:1505-1577, 1625-1682`); il est exposé sur le UOW (`backend/src/cti_app/infrastructure/database/uow.py:270-271, 366-368`). | **VERIFIED :** ce jeu est distinct du JSON `investigation_envelopes.budget_snapshot`. Aucun appel applicatif à `uow.investigation_budgets` n’a été trouvé dans le chemin `execute_tool_call`/`operator_tool_invocation` examiné. **UNKNOWN :** usage déployé et relation opérationnelle hors de ces appelants ; ne pas le traiter comme un miroir ni comme l’autorité S3 sans appelant démontré. |
| **Settlement opérateur courant** | `_complete_tool_reservation` lit l’enveloppe, garde une réservation incertaine, libère seulement la preuve framework, et commit `SUCCEEDED`, `EMPTY`, `PARTIAL` avec `result.budget_consumed` ou le montant réservé (`backend/src/cti_app/application/investigation_service.py:2761-2832`). | Ce n’est pas une ligne settlement append-only par dispatch. Résultat `PARTIAL` est aujourd’hui facturable et peut être rejoué ; une exception après commit consume ne lie pas un reçu fournisseur. Les invocations d’une même réservation sont ajustées après la réponse ; la réponse terminale humaine `NOT_EXECUTED` libère et `ABANDON`/`EXECUTED_WITH_RESULT` commettent le montant réservé (`backend/src/cti_app/application/investigation_service.py:1870-1910`). La décision humaine reste auditée mais n’est pas une preuve technique `CONFIRMED_NOT_SENT`. |
| **Budget loop acquisition** | `LoopBudget` a `VT_READ_UNITS`, `HITS_ACQUIRED`, `NEW_SAMPLES` (`backend/src/cti_app/domain/production.py:773-822`). Juste avant download, l’acquisition commit `VT_READ_UNITS +1`, et `HITS_ACQUIRED +1` pour `HIT_REVIEW` (`backend/src/cti_app/application/sample_acquisition.py:349-367`). | Ces compteurs sont consommés avant HTTP et ne sont pas remboursés sur échec. `NEW_SAMPLES` existe comme limite/compteur mais aucun appel `consume(NEW_SAMPLES)` n’a été trouvé ; il n’est donc pas débité par ce chemin. Ce sont des quotas investigation, distincts de `VT_UNITS`/`DOWNLOADS` de l’enveloppe. |
| **Ledger acquisition** | `sample_acquisition_attempts` est append-only avec forme `SUCCESS(sample_id)` ou `ERROR(error_code)` et unicité partielle du succès `(investigation_id, requested_hash)` (`backend/src/cti_app/infrastructure/database/models/production.py:469-526`). Le repository cherche succès/dernière erreur et append (`backend/src/cti_app/infrastructure/database/repositories/production.py:183-228`). | Le ledger prouve le succès d’idempotence ou un échec enregistré ; il n’a ni montants, ni `dispatch_id`, ni settlement, ni preuve de bytes envoyés. L’erreur est ajoutée si le débit budget local a été commis (`backend/src/cti_app/application/sample_acquisition.py:622-670`). |
| **VT/Shodan** | Les tools VT réservent 1 unité pour report simple ou jusqu’à `max_pages` pour certains endpoints, puis rapportent unités/page réellement comptées par l’adapter (`backend/src/cti_app/application/investigation_vt_tools.py:653-672, 1571-1653, 1684-1705`). Shodan réserve l’estimation `SHODAN_CREDITS` et renvoie le compteur de `meta` (`backend/src/cti_app/application/investigation_shodan_tools.py:1202-1222`); l’infrastructure remplit `meta.credits_consumed` via son estimation configurée (`backend/src/cti_app/infrastructure/shodan.py:133-178`). | La policy locale de route/estimation, capturée avant envoi, doit posséder le montant. `meta.credits_consumed` n’est pas un reçu de facturation externe. Les tentatives physiques par page ne sont pas stockées en S3 aujourd’hui. |
| **Retrohunt** | Un `retrohunt_reservations` garde le slot d’envoi par `(subject_id, operation_key)` et un plafond de slots ; `submit` le commit avant la requête (`backend/src/cti_app/infrastructure/database/models/retrohunt.py:25-62`; `backend/src/cti_app/application/retrohunt.py:152-212`). | C’est l’autorité actuelle du nombre de soumissions Retrohunt. Les `ToolDefinition` submit/status/results ont `cost_model={}` (`backend/src/cti_app/application/investigation_retrohunt_tools.py:81-105`), donc aucune réservation VT/outil externe via l’enveloppe dans ce registry. Il faut décider le coût route par route avant d’activer la gateway ; ne pas inventer l’unité fournisseur. |
| **Bridge** | `advisor.consult` réserve une unité logique `BRIDGE_TURNS`, contexte et temps (`backend/src/cti_app/application/investigation_advisor_tools.py:3395-3407`). L’adapter renvoie 0 en pré-soumission/blocked, 1 au succès ou au résultat ambigu (`backend/src/cti_app/application/investigation_advisor_tools.py:4008-4021`). | L’enveloppe est l’autorité d’un tour logique. Les retries physiques ModelGateway ne doivent pas créer une seconde charge de tour logique, mais chaque envoi physique a besoin de son propre consume et d’une mesure interne unique. Aucune preuve fournisseur de paiement/usage n’est supposée. |

### Acquisition : relation exacte observée

- Pour une acquisition nouvelle via `vt.download_sample`, la réservation opérateur `{VT_UNITS: 1, DOWNLOADS: 1}` est persistée avant dispatch. Le chemin interne débite séparément `VT_READ_UNITS` et, pour `HIT_REVIEW`, `HITS_ACQUIRED` avant d’appeler le port (`backend/src/cti_app/application/investigation_acquisition_tools.py:51-54, 667-670`; `backend/src/cti_app/application/sample_acquisition.py:349-377`). Le `Sample`, la provenance et la ligne de succès ne sont ajoutés qu’après ingestion des bytes (`backend/src/cti_app/application/sample_acquisition.py:533-620`).
- Une erreur explicite de téléchargement rapporte `VT_UNITS=int(provider_called)` et `DOWNLOADS=0`; si des octets partiels sont présents ou que le hash ne correspond pas, l’état est ambigu et l’appel reste `NEEDS_REVIEW` (`backend/src/cti_app/application/investigation_acquisition.py:411-450`; `backend/src/cti_app/application/sample_acquisition.py:428-448`). Le compteur loop, déjà commis, n’est pas libéré.
- Le réemploi local vérifié renvoie `{VT_UNITS:0, DOWNLOADS:0}` avant le téléchargement (`backend/src/cti_app/application/investigation_acquisition.py:201-243, 245-288`). **INFERENCE vérifiée sur les chemins :** il ne doit pas non plus prendre un `VT_READ_UNITS`, `HITS_ACQUIRED` ou `NEW_SAMPLES` supplémentaire, puisqu’il n’appelle pas `SampleAcquisitionService.acquire` dans ces branches.
- **Risque vérifié par lecture indépendante :** course possible si `acquire_hit` ne voit pas encore la ligne de succès, puis le `SampleAcquisitionService` la trouve à son second contrôle et renvoie un sample local ; le wrapper retourne néanmoins `_ACQUISITION_COST` entier (`backend/src/cti_app/application/investigation_acquisition.py:343-388, 515-537`; `backend/src/cti_app/application/sample_acquisition.py:315-347`). L’outer budget peut être débité malgré zéro nouvel egress. Il faut que l’effect gate décide/règle le réemploi avant consume, et que le test concurrence couvre cette fenêtre.

### Ordre courant et limites du settlement

- **VERIFIED :** le service persiste invocation et réservation avant dispatcher, puis transforme les échecs externes sans preuve en `NEEDS_REVIEW` (`backend/src/cti_app/application/investigation_service.py:1152-1153, 1238-1267`). Le dispatcher enlève toute preuve `framework_pre_submission_proof` retournée par un adapter ; seules ses sorties de pré-dispatch peuvent l’attester (`backend/src/cti_app/application/investigation_tools.py:779-804`). Le type de preuve actuel affirme explicitement que l’adapter n’a pas été appelé (`backend/src/cti_app/domain/investigation.py:30-42`).
- **VERIFIED :** le finalize met d’abord à jour l’invocation puis settle l’enveloppe (`backend/src/cti_app/application/investigation_service.py:1299-1319`); `reconcile_invocation` fait aussi l’invocation avant l’enveloppe (`backend/src/cti_app/application/investigation_service.py:1906-1910`). R7 identifie que ce sens inverse l’ordre proposé de consume (enveloppe avant invocation) et peut deadlocker si les writers sont mélangés (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:20-22`). Tous les writers/reconcilers doivent adopter un seul ordre avant activation.
- **INFERENCE :** SQL et HTTP ne peuvent pas former une transaction atomique. Le commit `CONSUMED` prouve uniquement que l’application a ouvert l’envoi et fourni un permis à usage unique ; il ne prouve ni la réception, ni l’exécution, ni la facturation côté fournisseur. Une perte du retour HTTP après envoi est ambiguë et doit conserver le coût d’egress réservé jusqu’à settlement/revue, pas autoriser un second envoi.

## 3. Contrat proposé : claims, usages et pseudo-SQL

### Interface et invariants

**PROPOSITION — module profond `ExternalEffectCoordinator`.** Son interface publique devrait exposer seulement `prepare(owner, route_id, intent)`, `reuse_or_claim(prepared)`, `consume(dispatch_id) -> OneUsePermit`, `send_one(prepared, permit)` via une route serveur immuable, `attach_response(dispatch_id, response)` et `reconcile(dispatch_id, decision)`. Les callers ne fournissent jamais le transport, le digest attendu, l’occurrence, le coût effectif ou une preuve de non-envoi. La route fixe le normalizer, serializer, transport et politique de coûts. La logique d’autorisation et de settlement reste derrière cette interface ; le fake et l’adapter PostgreSQL franchissent le même seam. Le design R2 va dans ce sens (`docs/mission03-architecture/DESIGN_OPTIONS.md:342-358, 416-422`).

- Le propriétaire est une somme fermée : invocation opérateur, ou tour/run Bridge/Advisor. Chaque branche a une FK dédiée et un `CHECK` exige exactement une branche ; un `owner_id` libre ne fait pas autorité. Le consume recharge le propriétaire, investigation, version d’enveloppe, stop state, route, claim, budget, famille et digest sous le même UOW avant d’émettre un permis.
- Verrouillage global recommandé : enveloppe/version/approval/budget ; état stop si applicable ; invocation ; claim/dispatch ; ligne de famille (acquisition ou slot Retrohunt) ; puis turn/run Bridge. Tous les finalizers, reconciliers, réservations et révocations suivent un ordre compatible ; aucun lock DB ne traverse le transport. Cet ordre suit R2 mais exige de refactorer le sens actuel (`docs/mission03-architecture/DESIGN_OPTIONS.md:359-376`; `docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:20-22`).
- `request_digest` lie les octets préparés au transport final ; route/version ne sont jamais fournis par le caller. Toute route/version inconnue, stale, mal liée au owner ou sans budget échoue avant permis et avant I/O. La production reste deny-all tant que les routes n’ont pas leurs oracles, conformément au gate R7 (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:48-64`).

### Clés et unicités proposées (pseudocode, pas une migration)

```sql
-- Une ligne = une occurrence logique revendiquée, indépendante de chaque HTTP send.
CREATE TABLE external_effect_claims (
  effect_key                 bytea PRIMARY KEY, -- digest versionné + occurrence
  investigation_id           uuid NOT NULL,
  continuity_key             bytea NOT NULL,    -- stable au bump du normalizer
  occurrence_id              text NOT NULL,     -- 'default' ou ID workflow journalisé
  occurrence_authorization_id uuid NULL,        -- obligatoire hors 'default'
  parent_effect_key          bytea NULL REFERENCES external_effect_claims(effect_key),
  normalizer_version         text NOT NULL,
  serializer_version         text NOT NULL,
  versioned_semantic_digest  bytea NOT NULL,
  route_id                    text NOT NULL,
  reuse_policy_version       text NOT NULL,
  state                       text NOT NULL,
  version                     bigint NOT NULL,
  UNIQUE (investigation_id, continuity_key, occurrence_id)
);

-- Une ligne = une requête physique ; un retry confirmé est un nouvel ordinal.
CREATE TABLE external_effect_dispatches (
  dispatch_id                 uuid PRIMARY KEY,
  effect_key                  bytea NOT NULL REFERENCES external_effect_claims(effect_key),
  attempt_ordinal             integer NOT NULL,
  owner_kind                  text NOT NULL,
  invocation_id               uuid NULL,
  bridge_turn_id              uuid NULL,
  model_run_id                uuid NULL,
  reservation_operation_key   text NOT NULL,
  request_blob_ref            text NOT NULL,
  request_digest              bytea NOT NULL,
  state                       text NOT NULL,
  state_version               bigint NOT NULL,
  UNIQUE (effect_key, attempt_ordinal),
  CHECK (/* owner_kind correspond à exactement un ensemble de FK */ TRUE)
);

CREATE TABLE external_effect_events (
  dispatch_id uuid NOT NULL REFERENCES external_effect_dispatches(dispatch_id),
  event_no    bigint NOT NULL,
  from_state  text NULL,
  to_state    text NOT NULL,
  actor_kind  text NOT NULL,
  reason_code text NOT NULL,
  occurred_at timestamptz NOT NULL,
  PRIMARY KEY (dispatch_id, event_no)
);

CREATE TABLE external_effect_uses (
  use_id                    uuid PRIMARY KEY,
  claimant_invocation_id    uuid NULL,
  claimant_bridge_turn_id   uuid NULL,
  source_dispatch_id        uuid NOT NULL REFERENCES external_effect_dispatches(dispatch_id),
  source_invocation_id      uuid NULL,
  reuse_policy_version      text NOT NULL,
  budget_delta              jsonb NOT NULL CHECK (budget_delta = '{}'::jsonb),
  UNIQUE (claimant_invocation_id, source_dispatch_id),
  UNIQUE (claimant_bridge_turn_id, source_dispatch_id),
  CHECK (/* exactement un claimant owner */ TRUE)
);

CREATE TABLE external_effect_use_refs (
  use_id uuid NOT NULL REFERENCES external_effect_uses(use_id),
  ref_kind text NOT NULL CHECK (ref_kind IN ('observation', 'artifact', 'sample')),
  ref_id uuid NOT NULL,
  PRIMARY KEY (use_id, ref_kind, ref_id)
);

CREATE TABLE external_effect_measurements (
  dispatch_id uuid NOT NULL REFERENCES external_effect_dispatches(dispatch_id),
  dimension text NOT NULL,
  units bigint NOT NULL CHECK (units >= 0),
  basis text NOT NULL, -- route policy / framework attempt / validated adapter result
  PRIMARY KEY (dispatch_id, dimension)
);

CREATE TABLE external_effect_settlements (
  investigation_id uuid NOT NULL,
  reservation_operation_key text NOT NULL,
  settled_amounts jsonb NOT NULL,
  settlement_state text NOT NULL,
  version bigint NOT NULL,
  PRIMARY KEY (investigation_id, reservation_operation_key)
);
```

Les `CHECK` commentés sont des exigences de schéma à rendre explicites avec les vraies FKs de chaque type d’owner, pas du DDL prêt à exécuter. Les valeurs `basis` désignent une mesure d’application ou une politique route versionnée, jamais un reçu fournisseur supposé.

**Règles d’unicité :**

- `(investigation_id, continuity_key, occurrence_id)` empêche un bump de version d’obtenir un claim par défaut neuf. En conflit, si `normalizer_version` ou `serializer_version` diffère, retourner `NEEDS_REVIEW` sans alias et sans send.
- `effect_key` contient le digest versionné pour preuve et comparaison ; `continuity_key` fournit le verrou inter-versions. Ne pas transformer le conflit de continuité en nouvel `effect_key`.
- `(effect_key, attempt_ordinal)` identifie chaque envoi physique. L’ordinal suivant n’est possible qu’après `CONFIRMED_NOT_SENT` prouvé ou résultat terminal/retry policy autorisé. `CONSUMED` ambigu ou partiel bloque l’ordinal suivant.
- `(dispatch_id, dimension)` garantit une mesure au plus une fois ; `(investigation_id, reservation_operation_key)` garantit un settlement au plus une fois. Rejouer un même settlement/digest identique est idempotent ; un montant différent est conflit/version-review, pas écrasement.
- `external_effect_uses` est créé dans le même UOW que la réutilisation, avec source dispatch et invocation source, claimant invocation/tour, politique et références de preuve. Refuser un usage sans référence source complète ou depuis un autre scope non autorisé.

### Table de transitions et décisions de réemploi

| Dispatch source | Réemploi vers un claimant | Nouveau send / budget |
|---|---|---|
| `SUCCEEDED` avec réponse complète, digest exact, artefacts valides, même route/version/politique | Permis ; écrire `external_effect_uses` et références source | Zéro send et delta budget zéro |
| Résultat terminal `EMPTY` explicite et complet | Permis seulement si `reuse_policy` qualifie l’absence comme terminale | Zéro send et delta zéro |
| `PARTIAL`, `NEEDS_REVIEW`, `CONSUMED` sans résultat, timeout/perte ack, erreur de transport | Refusé ; garder les artefacts partiels pour revue | Aucun nouvel ordinal ni settlement libéré automatiquement |
| `UNAVAILABLE`, refus ou échec provider/adaptateur | Pas un résultat réutilisable ; une retry n’est permise que si non-envoi prouvé ou retry terminal explicite | Sinon revue ; pas de permit automatique |
| `CONFIRMED_NOT_SENT` | Aucun résultat externe à réutiliser | Settlement à zéro pour ce dispatch ; un nouvel ordinal peut être tenté selon workflow, car l’envoi précédent est prouvé absent |
| Même `continuity_key`, autre normalizer/serializer | Pas d’alias automatique ni de réemploi cross-version | Refus fail-closed jusqu’à une occurrence distincte autorisée par le workflow |

La transition d’état est CAS sur `state_version`, append l’événement et règle le montant dans la même transaction SQL. Un résultat tardif peut s’attacher une seule fois au dispatch exact si route, request digest et owner correspondent ; un CAS perdu déclenche une relecture. Les bytes ou résultats partiels restent artefacts de revue, pas une réponse complète. `RECONCILED_NOT_EXECUTED` est une décision humaine auditée distincte de `CONFIRMED_NOT_SENT` : elle ne devient pas une preuve framework et ne rembourse pas automatiquement un dispatch déjà consommé. `RECONCILED_EXECUTED` n’est réutilisable que si les références attachées établissent une réponse complète au digest exact. Ces règles prolongent R2 (`docs/mission03-architecture/DESIGN_OPTIONS.md:377-405`).

## 4. Autorité budgétaire et settlement proposés par chemin

| Chemin | Autorité recommandée | Mesure/settlement proposé |
|---|---|---|
| **VT reports/relations** | Réservation `budget_snapshot` de l’enveloppe, sous dimension `VT_UNITS` et le coût route versionné. | Réserver le plafond pages connu avant consume ; compter un usage par requête physique/pagination. Settlement unique par opération ; aucun paiement nouveau au réemploi.
| **Shodan** | Enveloppe `SHODAN_CREDITS`; le coût estimé de route est copié dans la réservation avant envoi. | Consommer le coût route configuré par send. Les métadonnées de réponse peuvent corroborer l’observation mais ne deviennent pas un reçu fournisseur.
| **Retrohunt** | `retrohunt_reservations` reste propriétaire du slot de soumission. Enveloppe opérateur est propriétaire des dimensions de coût quand une policy route aura été approuvée. | Chaque submit/status/results physique obtient son dispatch. Le coût actuel `{}` est insuffisant pour activer S3 ; garder deny-all jusqu’à une décision humaine sur le coût/unité de ces routes. Le slot n’est pas un reçu de coût.
| **Acquisition VT** | Enveloppe : coût/opération `VT_UNITS` et `DOWNLOADS`. LoopBudget : plafonds indépendants `VT_READ_UNITS` et `HITS_ACQUIRED`; proposer `NEW_SAMPLES` uniquement à l’insertion d’un nouvel objet Sample vérifié. | Un download physique autorisé consomme un `VT_UNITS` et un `DOWNLOADS` d’essai au consume, même si l’issue devient ambiguë ; `NEW_SAMPLES` ne consomme qu’à la persistance d’un Sample réellement nouveau. Le réemploi local vérifié consomme zéro dans chaque dimension. `HITS_ACQUIRED` est une limite de tentatives hit et doit être validée comme telle par produit/humain. |
| **Bridge/advisor** | Enveloppe : `BRIDGE_TURNS=1` pour le tour logique `advisor.consult`; les compteurs contexte/temps restent les coûts de l’opération. | Chaque POST/ retry physique reçoit un dispatch/mesure distinct. Le retry confirmé ne prélève pas un second tour logique. Pas d’envoi sur état ambigu ; le ModelGateway accepte un send seulement avec dispatch consommé lié à son run. Pas d’atomicité SQL+Bridge ni de reçu inventé.

**Important :** les deux couches acquisition ne s’additionnent pas dans un total fournisseur. `VT_UNITS`/`DOWNLOADS` plafonnent l’effect opérateur ; `VT_READ_UNITS`/`HITS_ACQUIRED` limitent le travail de l’investigation. Elles peuvent toutes deux avancer pour le même send, mais avec une sémantique et une autorité distinctes. `sample_acquisition_attempts` demeure le journal de résultat. Le settlement S3 doit transactionnellement enregistrer consume, unit rows et état de réservation avant de retourner le permis ; la requête HTTP reste hors transaction.

**Pseudo-transaction consume :**

```sql
BEGIN;
  -- ordre global : envelope -> state stop -> owner -> claim/dispatch -> family -> Bridge run/turn
  SELECT * FROM investigation_envelopes WHERE investigation_id = :i FOR UPDATE;
  SELECT * FROM investigation_working_states WHERE investigation_id = :i FOR UPDATE;
  SELECT * FROM tool_invocations WHERE id = :owner_invocation FOR UPDATE; -- ou owner Bridge exact
  SELECT * FROM external_effect_claims WHERE effect_key = :effect_key FOR UPDATE;
  SELECT * FROM external_effect_dispatches WHERE dispatch_id = :d FOR UPDATE;
  -- revalider G2/TLP/do_not_submit/capability/stop, route/version, réservation,
  -- digest, occurrence et tentative admissible ; chaque prédicat est serveur.
  UPDATE external_effect_dispatches
    SET state = 'CONSUMED', state_version = state_version + 1
    WHERE dispatch_id = :d AND state = 'PREPARED' AND state_version = :v;
  INSERT INTO external_effect_events (... 'PREPARED', 'CONSUMED' ...);
  INSERT INTO external_effect_measurements (...); -- clé (dispatch_id, dimension)
  INSERT INTO external_effect_settlements (...);    -- clé reservation operation
  -- débiter les unités route/quota sous leurs clés propres, une seule fois.
COMMIT;
-- Émettre ensuite un permis opaque one-use lié à dispatch_id + route + request_digest.
-- Aucun lock SQL ne reste pendant le transport.
```

Une perte d’ack au retour de `COMMIT` ne permet pas de recréer le permis. Relire le dispatch ; si son statut/ack n’établit pas sans ambiguïté le chemin, basculer en revue et ne pas envoyer. Il n’y a pas de transaction atomique SQL+HTTP.

## 5. Oracles RED proposés (aucun exécuté)

Les fakes contrôlent le nombre exact d’entrées transport et le digest des octets. Les oracles de concurrence utilisent deux sessions PostgreSQL réellement indépendantes ; une barrière en mémoire ne remplace pas ce contrôle. Les couples fake/DB vérifient aussi qu’aucune capacité transport n’est exposée avant consume. Cette forme respecte le premier oracle R7, sans ouvrir une route production (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:52-64`).

1. **Bump normalizer, même intention.** Créer v1, puis préparer la même entrée avec v2. **Fake :** deux appels au coordinateur donnent zéro second send, aucun permis v2 et une décision fail-closed. **PostgreSQL deux sessions :** A et B tentent chacune de créer le claim default sous des normalizers différents ; l’unicité sur `continuity_key + occurrence_id` donne un claim, B relit le claim v1 et ne crée ni réservation ni dispatch additionnels. RED si le bump suffit à deux sends.
2. **Occurrence nouvelle explicitement permise.** Après un résultat terminal certain, le workflow écrit une occurrence enfant avec parent, étape/ordinal et preuve d’autorisation. **Fake :** occurrence autorisée envoie au plus une fois ; la même occurrence répétée ne renvoie pas. **PostgreSQL deux sessions :** A/B créent le même enfant en course, une seule ligne de claim/dispatch gagne. Un caller fournissant seul un UUID arbitraire obtient refus et zéro send.
3. **Terminal complet contre ambigu/partiel.** Préparer une source `SUCCEEDED` complète et une `PARTIAL` ou `NEEDS_REVIEW`. **Fake :** le résultat complet de même version/politique se réutilise sans transport ; les statuts ambigu/partiel/refusés déclenchent zéro use et zéro send. **PostgreSQL :** la ligne `use` claimant→source avec références et delta `{}` est créée une fois pour le terminal complet ; aucune ligne `use` n’est possible pour la source ambiguë.
4. **Double settlement concurrent.** Même opération/dispatch reçoit deux réponses terminales concurrentes. **Fake :** le coordinateur tente les deux callbacks. **PostgreSQL deux sessions :** CAS + unicités `(dispatch_id, dimension)` et `(investigation_id, reservation_operation_key)` n’enregistrent qu’une mesure et un commit ; répétition identique est idempotente, montant différent est conflit. RED si le budget augmente deux fois ou s’écrase silencieusement.
5. **Perte d’acquittement après consume.** Injecter une panne après le commit `CONSUMED` mais avant la remise du permis à l’appelant. **Fake :** aucune invocation transport ; reprise relit le dispatch, n’émet aucun nouveau permis et finit `NEEDS_REVIEW` si le commit/send boundary n’est pas prouvable. **PostgreSQL deux sessions :** A consomme puis perd l’ack, B récupère ; B ne peut pas remettre le même dispatch en `PREPARED`/consume. RED si le restart déclenche un second send.
6. **Réponse tardive ou partielle.** Une session passe un dispatch consommé en revue pendant que l’autre attache une réponse tardive. **Fake :** réponse complète au digest exact se rattache sans second send ; réponse partielle reste artefact et statut non réutilisable. **PostgreSQL deux sessions :** CAS décide d’une seule transition terminale ; mauvais digest/route ou seconde réponse est refusé, sans écraser le premier artefact ni le settlement.
7. **`CONFIRMED_NOT_SENT` limité.** **Fake :** invalidité d’arguments, refus budget/G2, route absente ou révocation revalidée avant entrée transport produit une preuve framework liée au dispatch et zéro appel ; un permis consommé mais refusé par un contrôle final exclusivement framework peut aussi produire cette preuve seulement si le code sait que le transport n’a pas été appelé et qu’aucun autre chemin ne peut l’appeler pour ce dispatch. Timeout, socket/reset, exception d’adapter après entrée, 404/erreur provider, perte d’ack, absence d’un timestamp et bytes partiels ne la produisent pas. **PostgreSQL deux sessions :** preuve append-only et release sont CAS/uniques, et ne peuvent être reconstruits par un lecteur depuis la seule absence d’un champ ; tout état consommé sans preuve framework positive reste facturé/review. Le simple champ de résultat d’adapter n’est jamais une preuve.
8. **Acquisition, local reuse, fresh et partiel.** **Fake :** un sample local/quarantaine avec bytes vérifiés fait zéro download et rapporte delta nul ; un send frais consomme exactement les unités route/loop prévues ; un stream partiel crée un artefact/revue mais aucun `NEW_SAMPLES`. **PostgreSQL deux sessions :** concurrence sur même hash et le ledger succès ne crée qu’un Sample/attempt succès et aucun deuxième egress ; mesurer aussi la fenêtre observée entre `acquire_hit` et son contrôle interne, pour qu’un reuse trouvé tardivement ne règle pas le coût plein.
9. **Bridge retry par envoi physique.** **Fake :** une erreur terminale confirmée donne une occurrence/dispatch de retry séparée et mesure distincte ; `BRIDGE_TURNS` reste 1. Une réponse ambiguë coupe immédiatement les retries. **PostgreSQL deux sessions :** chaque attempt se lie au même turn/run attendu mais a son dispatch ID/ordinal CAS, et ModelGateway n’accepte pas une soumission sans consume lié au run. Cela ferme N3/N5 et L13 avant toute route Bridge (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:28-38`; `docs/mission03-architecture/IMPLEMENTATION_PLAN.md:316-328`).

## 6. UNKNOWN et décisions humaines requises

- **UNKNOWN — continuité exacte des normalizers.** Aucun registry S3 ne définit la projection d’intention par route. Il faut décider/stabiliser, par route, les champs provider pertinents, ceux d’autorisation seulement (justification, refs), l’ordre/encodage, et comment une nouvelle version prouve qu’un input demeure la même intention.
- **Décision humaine — politique inter-versions.** La recommandation ici est fail-closed sans alias. Toute exception future exige un mapping/version de compatibilité revu, des preuves golden exact-request et un avis sur réemploi ; un normalizer bump ne suffit jamais.
- **Décision humaine — sémantique des occurrences.** Nommer les workflows qui peuvent émettre page/poll/retry/correction/répétition, leurs conditions terminales, leur parent, et les situations exigeant permission opérateur. Aucune nouvelle occurrence depuis un résultat ambigu.
- **UNKNOWN — coût fournisseur exact.** VT pagination, Shodan crédits, endpoints Retrohunt, retries et acquisition n’ont pas ici de reçu fournisseur attesté. Les montants doivent venir d’une policy route déterministe et versionnée. Retrohunt est actuellement sans `cost_model` dans ces tools.
- **Décision humaine — quotas acquisition.** Confirmer `DOWNLOADS` comme unité d’essai au consume (recommandé ici) ou téléchargement complet ; confirmer `HITS_ACQUIRED` comme tentative d’acquisition ou succès acquis ; définir le débit `NEW_SAMPLES` au moment où un nouvel objet vérifié est écrit. Ne pas appeler les compteurs actuels un même budget fournisseur.
- **UNKNOWN — relation runtime des budgets.** Le repository `investigation_budgets` est branché mais aucun appelant du chemin S3 étudié n’a été trouvé ; états en production et autres callers non vérifiés. Un audit runtime est hors de cette mission lecture seule.
- **Décision humaine — locks et Bridge.** Harmoniser settlement/reconciliation autour d’un ordre unique (R7 N1), définir le handoff durable `ModelRun`→dispatch consommé et maintenir le comportement retry avec un consume pour chaque send physique (R7 N3/N5). Aucun appel Bridge n’a été effectué.
- **Décision humaine — historique.** Ne pas réutiliser automatiquement les anciennes lignes d’invocation ou claims Retrohunt comme `external_effect_uses`. Décider si un inventaire manuel autorise certains résultats historiques ; sans preuve de route, digest et terminalité, les laisser hors réemploi. Ne pas backfill depuis les paramètres bruts (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:60-64`).
- **Décision humaine — autorisation d’implémentation.** R7-S3-R2 garde le gate S3 non satisfait et n’approuve pas la migration, le cutover, le réemploi cross-invocation ou les appels réels (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:48-56`). Ce rapport ne lève pas ce gate.

## Vérification et portée

- **VERIFIED :** HEAD lu = `e120a36f1afa08261fdf0276e0200872973ad8f2`; le worktree était propre au début de l’analyse.
- **Aucun test/check d’exécution** ; aucun PostgreSQL ; aucun serveur, provider, Bridge, VT, Shodan, Retrohunt, MinIO ou run CTI.
- Le seul fichier créé par cette tâche est ce rapport ; aucune source, migration ou suite de tests modifiée.
