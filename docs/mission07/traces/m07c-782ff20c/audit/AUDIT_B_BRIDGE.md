# Audit Bridge — Mission 07C

## Périmètre et méthode

Audit en lecture seule du dépôt à `c921848c301407aa8718b833ab35793d3e0bbeca`. Instructions lues : `AGENTS.md`, `backend/AGENTS.md`, `docs/README.md` et la skill `autowork-run-audit`. Les traces m07c n’ont été consultées qu’au niveau des identifiants d’appel, statuts, codes d’erreur, tailles et références de claims ; aucun prompt, texte d’article, sortie Bridge, secret ou contenu CTI n’est reproduit ici. Les fichiers de run sont des notes opérateur, pas des preuves canoniques. Aucun test n’a été exécuté et aucun code n’a été modifié.

## Synthèse

| Point | Diagnostic | Exigence réelle | Friction observée |
|---|---|---|---|
| Figure : finding obligatoire | Rejet de précontrôle confirmé alors que des passages d’article étaient transmis | Exiger au moins une preuve canonique SHA-vérifiée présente dans la requête | Exiger spécifiquement un finding |
| Références d’article | Le chargement local fonctionne ; le contrat de consultation ne charge que les passages transmis et rejette une forme de référence texte | Conserver l’identité article/claim et vérifier qu’elle figure dans le contexte transmis | Résolution absente à la consultation et représentation trop stricte |
| Prose, blocs, « partial/invalid », reparse | Sortie figure réellement dégradée ; reparse ancien refusé. L’échec de parsing YARA du tour ambigu n’est pas établi | Vérifier séparément les références et tout candidat structuré destiné à devenir un artefact | Exiger le wire `advisor-wire`, ignorer la prose hors wire et borner/tronquer le résultat |
| Avis | Un échec de validation locale est observé ; la cause précise « références mal saisies » ne l’est pas | Lier les assertions avant toute promotion canonique | Exiger ce lien pour afficher un simple brouillon |
| Ambiguïté et délai | Le non-renvoi après handoff incertain est une garantie réelle ; une voie d’abandon permet pourtant un nouvel envoi | Résoudre par identité stable et CAS ; ne jamais renvoyer si l’effet peut avoir eu lieu | L’opérateur ne dispose pas toujours d’un chemin de récupération autonome |
| Réservations | Libérer après preuve de non-envoi ; conserver après état externe inconnu | Comptabilité conservatrice et preuve one-use | Pas de réconciliation opérateur assez visible ; le run ne prouve pas une fuite de réservation pré-envoi |

Les propositions ci-dessous respectent les contrôles G2/TLP, `external_llm_allowed`, l’identité SHA, la provenance et les approbations de publication. Une sortie prose exploitable devient un **résultat consultatif non fiable** ; elle ne devient pas automatiquement une observation canonique, une règle validée, un finding ou un avis publiable.

## 1. Précontrôle `figure_plan` exigeant un finding

**Faits vérifiés.** Dans `backend/src/cti_app/application/advisor_dossier.py:2016–2028`, le dossier ajoute artificiellement la preuve requise `figure-findings` quand le type est `figure_plan` et qu’aucun finding n’est présent. Le contrôle générique qui suit (`2030–2036`) sait pourtant accepter `observation`, `finding`, `article`, `measurement` ou `fact`, sous réserve d’un corps et d’une preuve de récupération. Le chemin d’articles vérifie passages, identifiants de claim et SHA (`1635–1668`). Le refus est retourné avant l’envoi par `investigation_advisor_tools.py:875–907`.

Dans m07c, `c041-figure-plan-static-only` est `failed` à `evidence_preflight`, avec `required_canonical_evidence_not_in_payload`, `confirmed_non_execution=true` et zéro tour Bridge. Son manifeste indique que S3P2.1 et S4P1.1 sont présents dans le dossier. `c049-figure-plan-with-finding` passe au précontrôle, mais reçoit ensuite une réponse partielle et inutilisable.

**Qualification.** La présence d’une preuve canonique transmise est un vrai contrôle de provenance. L’obligation d’un finding particulier est une friction : un plan de figure peut être demandé à partir de passages d’article vérifiés sans qu’un finding ait déjà été créé.

**Diff minimal proposé.** Supprimer le bloc synthétique `figure-findings` (`2016–2028`) et appliquer à `figure_plan` le contrôle générique existant. Il doit rester au moins une référence canonique retenue et vérifiée dans le dossier ; un simple texte libre ou un identifiant qui n’est pas transmis reste insuffisant.

**Test ciblé à ajouter/adapter** dans `backend/tests/test_advisor_completeness.py` : `figure_plan` avec passage d’article et claim SHA-vérifiés, sans finding, passe ; le même plan sans aucune référence canonique reste bloqué. Tests proposés, non exécutés.

## 2. Références d’articles et claims

**Faits vérifiés.** `article.get_claims` est un outil local en lecture dans `backend/src/cti_app/application/research_context_builder.py:3529–3555, 3624–3642`. Les appels m07c `a009-article-claims` et `b059-article-claims` répondent HTTP 200. Cela ne signifie pas que la consultation Bridge peut retrouver ces claims après coup : `advisor_dossier.py:653–688` construit le dossier à partir des `article_passages` qui lui sont fournis ; il ne déclenche pas `article.get_claims` dynamiquement.

Dans le chemin de consultation, `investigation_advisor_tools.py:983–989, 1057–1063` transforme les références article transmises et accepte une entrée `article:<sha>` bien formée. En revanche, la référence **de sortie typée** doit être un objet `{kind, id}` (`backend/src/cti_app/domain/advisor_figure_plan.py:205–213`), puis correspondre à une référence effectivement transmise (`backend/src/cti_app/application/advisor_contracts.py:2423–2431`). Une chaîne `article:<sha>` dans cette sortie typée ne respecte donc pas le schéma. Il existe un écart de représentation, pas une raison de rejeter l’identité SHA elle-même.

La preuve de run est nuancée : les claims S3P2.1 et S4P1.1 apparaissent dans le manifeste de `c041`, mais celui-ci échoue sur le finding requis. Les appels `b062` et `b063` reçoivent 422 pour une enveloppe d’entrée incorrecte ; ils ne démontrent pas que la référence d’article en était la cause. La cause de l’échec `b064` n’est pas déterminable depuis les métadonnées consultées.

**Qualification.** Lier toute citation à un article/claim exact présent dans le dossier est une exigence de provenance. Forcer l’opérateur à recopier manuellement des passages déjà accessibles et rejeter la notation `article:<sha>` avant normalisation sont des frictions.

**Diff minimal proposé.** Lors de la consultation, résoudre les IDs de claims explicitement cités contre les claims locaux, puis transmettre le passage avec `claim_id`, SHA de l’article et, si disponible, l’ancre/offset. Normaliser `article:<sha>` en référence interne seulement si ce SHA et l’ancre correspondante sont dans le contexte transmis. Aucun hash inconnu ou texte non transmis ne passe le contrôle.

**Tests ciblés** dans `backend/tests/test_advisor_wire.py` et/ou le test du builder de dossier : nommer S3P2.1/S4P1.1 les fait apparaître dans le dossier ; la forme texte est normalisée quand elle correspond exactement à une référence transmise ; hash absent du dossier toujours rejeté. Vérifier que le contrôle de politique de divulgation reste inchangé.

## 3. Prose Bridge, blocs fencés, réponses partielles et reparse

**Faits vérifiés.** Le prompt de `backend/src/cti_app/application/advisor_prompt.py:15–27, 139–210` demande actuellement `advisor-wire` avec `TYPE`, `STATUS` et champs imposés. Pour YARA il demande une source complète et des champs MOTIF/INVARIANT ; pour une figure, titres, nœuds, arêtes et directions. `backend/src/cti_app/application/advisor_wire.py:44–86, 276–310` reconnaît les champs wire et n’extrait une source que d’un bloc de code complet et non ambigu ; le texte libre non reconnu devient `ignored_prose`. `advisor_contracts.py:61–130, 1982–2213, 2216–2315, 2401–2490` applique ensuite des contrats typés stricts. Le parseur contient déjà des tolérances de prose pour certains types, mais pas une garantie générale qu’une réponse prose non vide donne un résultat.

Cas concret : `c049` a une sortie Bridge de 3 157 octets, statut `partial`, `usable=false`, avec les avertissements `wire_prose_ignored`, `source_alias_in_prose` et `figure_plan_invalid`. Le reparse `a147-reparse-yara-source` retourne `partial` avec `unsupported_legacy_format` et `new_observation_written=false`. Le test existant `backend/tests/test_advisor_output_reparse.py:218–232` confirme ce comportement pour une ancienne sortie wire : le reparse est auditable mais ne crée pas d’observation. En revanche, le tour YARA `b069` est ambigu et n’a pas de réponse exploitable enregistrée ; ses métadonnées ne prouvent pas un rejet de parsing YARA. Les tentatives antérieures mentionnées dans le run échouent au précontrôle source, pas au parseur.

Le plafond de sortie brut `_MAX_ADVISOR_RAW_OUTPUT_CHARS = 64 * 1024` et son application sont dans `backend/src/cti_app/application/advisor_contracts.py:215, 731–747`. Le contrat figure ajoute aussi des longueurs et nombres maximum (`backend/src/cti_app/domain/advisor_figure_plan.py:25–53, 120–145`). Les contraintes d’extraction de fragments d’échantillon sont une borne locale distincte (`investigation_advisor_tools.py:118`) ; elles ne démontrent pas une limite globale de prompt. Le test `test_yara_source_above_legacy_limit_is_preserved` dans `backend/tests/test_advisor_wire.py:740` couvre la source seule, mais ne lève pas le plafond appliqué à la réponse entière.

**Qualification.** La vérification des références, du hash et de l’état d’un candidat destiné à devenir une règle ou une figure canonique est réelle. Exiger un wire ad hoc pour qu’une prose compréhensible soit considérée comme un résultat, ignorer la prose hors wire, rejeter un ancien texte lors du reparse sans possibilité de le restituer, et couper une réponse à 64 KiB sont des frictions. La limite figure par nœuds/arêtes est également incompatible avec la consigne « aucune limite de taille ».

**Diff minimal proposé.** Remplacer la consigne de wire/JSON par : réponse en français libre, blocs fencés facultatifs pour YARA/Suricata ou schéma de figure. Pour toute réponse non vide, conserver et retourner le texte complet avec son SHA comme résultat consultatif. Extraire en plus, au mieux, un candidat structuré ; les avertissements de parsing ne doivent pas effacer le texte. Supprimer la troncature 64 KiB et les plafonds applicatifs de taille/compte ; préserver les octets complets et le SHA dans le stockage existant. Un candidat non validé reste non promu, mais la prose reste visible. Le reparse peut ajouter un parseur explicitement versionné pour le texte/blocs anciens et retourner la source originale même si aucun candidat n’est extrait ; garder la liaison au hash et l’absence de promotion automatique.

**Tests ciblés** dans `backend/tests/test_advisor_wire.py` et `backend/tests/test_advisor_output_reparse.py` : prose libre non vide retourne un résultat texte ; prose plus un ou plusieurs blocs fencés garde le texte et extrait les candidats valides ; format de bloc incomplet donne un avertissement sans supprimer le texte ; sortie dépassant 64 KiB est conservée intégralement avec SHA stable ; ancien résultat est reparseable sous un parseur explicitement choisi sans observation canonique en l’absence de validation. Tests existants de prose, de source longue et de reparse restent pertinents, mais ne couvrent pas cette garantie générale.

## 4. Rédaction d’avis et assertions structurées

**Faits vérifiés.** Pour `draft_text`, `backend/src/cti_app/application/advisor_dossier.py:1957–2014` exige des assertions ID/texte et des références d’observation/finding SHA-vérifiées ; le prompt (`advisor_prompt.py:62–78, 160–171`) demande un texte par alias ; le parseur (`advisor_contracts.py:1982–2213`) associe les blocs/sentences aux IDs et références connus. Dans m07c, `b071-opinion-draft-compact` est refusé en prévalidation locale, `pre_transport_validation`, `confirmed_non_execution=true`, zéro tour Bridge. Les métadonnées ne disent pas quelle référence précise a provoqué le rejet. `b073-opinion-draft-typed-assertions` finit en `provider_submission_ambiguous` ; il ne confirme pas un rejet de schéma côté réponse.

**Qualification.** Les assertions liées aux preuves sont nécessaires pour transformer un avis en contenu canonique ou publiable. Les exiger pour simplement obtenir une première rédaction prose non fiable est une friction. Le détail « références mal saisies » n’est pas établi pour ces appels précis.

**Diff minimal proposé.** Quand `draft_text` ne peut pas être associé aux assertions, retourner tout de même `advisory_text` avec un statut consultatif et un avertissement « liens de preuve absents ». Garder les liens par assertion comme enrichissement facultatif. N’autoriser aucune promotion vers une version d’avis, un finding ou une publication sans le lien et la validation humaine requis.

**Test ciblé** dans les tests `draft_text` de `backend/tests/test_advisor_wire.py` : réponse prose sans assertions retourne un brouillon texte et avertissement, ne modifie aucune version canonique ; réponse avec alias valide continue à produire les associations. Test proposé, non exécuté.

## 5. Appels ambigus, délai et réconciliation par l’opérateur

**Faits vérifiés.** Les paramètres source ne confirment pas un délai fixe de onze minutes : `backend/src/cti_app/config.py:113–122` donne un défaut d’attente Bridge de 3 600 s et un défaut recherche de 7 200 s ; le constructeur du transport Responses a un défaut distinct de 900 s (`backend/src/cti_app/integrations/models.py:90–150`). La configuration effective est inconnue ; les horodatages du journal m07c ne suffisent pas à établir la durée d’un appel. Aucune configuration secrète n’a été lue.

Dans `integrations/models.py:398–475, 532–571, 610–623`, l’échec après handoff qui n’est ni une réponse complète ni un refus sûrement avant soumission est `provider_submission_ambiguous`/`external_state_unknown` et n’est pas rejoué automatiquement. C’est le garde-fou anti-double effet attendu. La récupération visible de `model_conversations.py:501–510, 520–565` nécessite un `response_id` déjà connu ; elle permet alors de prévisualiser le résultat. L’adoption (`567–633`) est liée à un hash attendu. Elle ne permet pas de retrouver une réponse quand aucun identifiant fournisseur n’a été capturé.

Le statut opérateur (`backend/src/cti_app/application/investigation_operator_view.py:192–207, 224–268, 309–321, 466–487`) distingue invocations non résolues, tour Bridge en attente et réservations. Une réservation expose son `operation_key` mais pas directement un ID d’invocation ; le rapprochement repose sur cette clé. Une invocation exposée n’inclut pas d’identifiant fournisseur dans `_invocation_view`.

Point à corriger en priorité : `model_conversations.py:763–773` vérifie que le tour est `NEEDS_REVIEW` et que son identité correspond, mais pas que le fournisseur n’a pas reçu la requête. Puis `865–875` l’abandonne et incrémente le numéro de tentative. Le test `backend/tests/test_model_conversations.py:1895–2025`, notamment `1984–2024`, confirme qu’une demande d’abandon avec raison libre conduit à une seconde soumission alors que l’ancien `ModelRun.submission_state` reste `external_state_unknown` et que le nouvel essai reçoit une clé différente. Cela peut produire un double effet ; la raison saisie par l’opérateur n’est pas une preuve de non-envoi. L’agent de revue a aussi relevé qu’il faut contrôler ce chemin contre le claim durable PostgreSQL, pas seulement contre l’adaptateur de test.

**Réconciliation autonome sans jeton humain ni double envoi.** L’opérateur doit pouvoir lancer une action backend par `invocation_id`/version attendue. Le backend, avec son identité de service déjà configurée, consulte par `response_id` ou par une clé fournisseur stable conservée avec le claim. Si une réponse exacte existe, il l’adopte une seule fois via CAS et stocke la même réponse/SHA. Si le fournisseur atteste de façon vérifiable qu’aucun handoff n’a eu lieu, il scelle cette preuve, libère la réservation et permet la reprise du même travail logique. Si le fournisseur ne sait pas trancher, le résultat reste `NEEDS_REVIEW`, la réservation reste en place et aucun nouvel envoi n’est proposé. Un opérateur peut déclencher et voir cette réconciliation ; sa saisie libre ne devient jamais la preuve.

**Diff minimal proposé.** Remplacer la transition `abandon -> tentative suivante` par l’adoption du résultat fournisseur, ou par une transition CAS autorisée seulement par une preuve de non-handoff, opaque, bornée à cette invocation/version et à usage unique. Capturer et exposer l’identifiant de réponse dès qu’il existe. Ajouter une action opérateur de réconciliation qui utilise le transport serveur existant et ne demande pas de jeton humain. Si aucun identifiant ou mécanisme fournisseur sûr n’existe, le seul état sûr est « encore inconnu, ne pas renvoyer ».

**Tests ciblés.** Remplacer/compléter `test_abandoning_investigation_turn_records_next_attempt_without_reusing_key` : une raison seule ne permet pas de renvoyer un tour `external_state_unknown`. Ajouter adoption idempotente d’un résultat retrouvé sans nouveau POST, blocage du nouvel envoi si l’état reste inconnu, et reprise après preuve no-handoff. Le test actuel montre que le chemin dangereux est couvert par le test comme comportement attendu.

## 6. Réservation non libérée et reprise avant envoi

**Faits vérifiés.** Le service distingue déjà les cas. `backend/src/cti_app/application/investigation_service.py:1814–1875` appelle `release_confirmed_non_submission` quand il reçoit une preuve typée de non-exécution. Le précontrôle advisor persiste un échec zéro-tour et tente cette attestation (`investigation_advisor_tools.py:4261–4345`). Dans le coordinateur d’effet externe (`backend/src/cti_app/domain/external_effects.py:677–698, 776–799, 859–933`), une preuve no-handoff one-use autorise le retry ; une erreur post-handoff sans résultat garde l’effet ambigu et bloque un renvoi. Ces deux comportements sont cohérents avec l’idempotence.

Le run `c041` indique un échec avant envoi, zéro tour et non-exécution confirmée. Le code a donc un chemin de libération applicable à cette classe de résultat, mais les snapshots consultés ne prouvent pas qu’une réservation précise avait été créée puis libérée pour `c041`. Les instantanés ultérieurs montrent trois réservations encore `reserved` après des appels ambigus du run. Leur association exacte à `a228`, `b069` et `b073` est une inférence (les statuts ne donnent pas une relation directe à l’ID de tour), pas un fait établi. Une réservation conservée pour un envoi ambigu n’est pas une fuite : l’usage externe peut déjà avoir eu lieu.

**Qualification.** Libérer un budget après une preuve fiable de non-envoi et rendre le même travail logique rejouable est une exigence fonctionnelle et d’intégrité. Garder la réservation quand l’état externe reste inconnu est une exigence de comptabilité/sûreté ; la libérer à l’aveugle rendrait possible un second effet. La friction est l’absence d’une réconciliation opérateur qui fasse passer l’ambiguïté à un état résolu et rende la libération/reprise vérifiable.

**Diff minimal proposé.** Faire porter à toute défaillance pré-transport la même attestation durable de non-envoi et la libération idempotente de la réservation ; exposer dans le statut opérateur l’association réservation ↔ opération/invocation et la décision attendue. La reprise conserve l’identité logique de la requête ; seul le numéro de tentative change après le scellement de la preuve. Ne jamais libérer une réservation `external_state_unknown` avant récupération du résultat ou preuve no-handoff.

**Tests ciblés** : étendre le test du précontrôle pour vérifier `reserved -> released`, zéro appel Bridge, puis reprise du même travail logique ; réaffirmer qu’un résultat ambigu garde la réservation et ne peut pas être resoumis ; ajouter le lien d’ID de réservation dans le statut. Les tests `test_no_handoff_proofs_are_opaque_bound_and_one_use`, `test_physical_submit_failure_is_never_classified_not_submitted` et `test_connect_error_after_handoff_is_needs_review_and_never_retried` sont des contrôles existants à conserver. Tests proposés, non exécutés.

## Vérification et suite permise

- HEAD vérifié : `c921848c301407aa8718b833ab35793d3e0bbeca`.
- Tests : aucun lancé, conformément au périmètre d’audit.
- Modification du dépôt : uniquement ce rapport.
- Risque principal : le chemin d’abandon actuellement couvert par test peut renvoyer une consultation dont l’état fournisseur reste inconnu.
- Prochaine action permise : implémenter séparément les simplifications après validation du propriétaire ; tout appel fournisseur réel, publication ou action destructive reste hors de cet audit.
