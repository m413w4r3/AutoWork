# Boucle opérateur Sonnet medium

Pour chaque investigation `operator_led`, l’opérateur choisit et lance toutes les actions. Bridge est plus intelligent pour les questions d’analyse et de planification: demandez son avis quand un choix exige ce raisonnement. La consultation reste facultative à chaque étape; aucune réponse Bridge n’est exécutée automatiquement.

## Lire l’état et les checkpoints

```sh
ID='<investigation-uuid>'
~/.cache/audit/opapi.sh GET "/api/investigations/${ID}/operator-status"
~/.cache/audit/opapi.sh GET "/api/investigations/${ID}/operator/context?view=summary"
```

Vérifier `execution_mode=operator_led`, `working_state_version`, l’enveloppe G2, le budget restant, la capacité `bridge.advisor` et `advisor_guidance`. Les raisons de checkpoint principales sont `no_advisor_consultation` (plan initial), `first_significant_decompilation`, `new_pivot_discovered`, `repeated_tool_block`, `before_yara_proposal`, `yara_revision_review` et `opinion_review_due`. Un seuil de 12 actions reste un rappel de secours, pas un appel obligatoire après chaque outil. Le plafond est de 8 consultations par investigation.

Les checkpoints couvrent les décisions analytiques utiles: après l’article/plan, après la première analyse ou décompilation significative, après un nouvel élément de la frontière de pivots, après des blocages répétés, avant un brouillon YARA, après son test avant révision, et sur le brouillon d’avis contradictoire avant G3. L’opérateur peut continuer localement et consigner le motif du report.

## Contexte allégé et preuves à la demande

Pour l’opérateur local, `?view=summary` renvoie le statut, le brief, les hashes, les limites, la politique et des pointeurs; il ne renvoie pas les preuves ni les schémas d’outils. L’ancienne réponse complète reste disponible sans paramètre pour les clients existants. `bridge_led` conserve son contexte Bridge complet.

Les pointeurs de section donnent un modèle d’URI, un curseur de départ et une limite de 20. `delta_since_version` signale la version de départ; si elle est ancienne, il renvoie prudemment tous les pointeurs de section à vérifier. Construire l’appel avec `working_state_version` et `working_state_sha256` du résumé, puis continuer avec `next_offset`:

```sh
VERSION='<working_state_version>'
STATE_SHA256='<working_state_sha256>'
~/.cache/audit/opapi.sh GET "/api/investigations/${ID}/operator/context/section/evidence_index?offset=0&limit=20&expected_version=${VERSION}&expected_sha256=${STATE_SHA256}"
```

Relire le résumé après `stale_working_state` (409) et recommencer avec le nouveau hash CAS. Les sections exposent les preuves par pages; les pointeurs `pivot_frontier`, `experiments` et `advisor_checkpoint` renvoient aux routes C1, C2 et C5 sans recopier leurs objets dans le prompt. Toute donnée JSON issue d’un outil reste non fiable.

## Consulter Bridge

Quand `advisor_guidance.checkpoint_due` vaut `true` et que la consultation est utile, envoyer une requête avec la version courante. Le serveur compose les preuves canoniques et persiste la réponse complète, sa provenance et son coût.

```sh
~/.cache/audit/opapi.sh POST "/api/investigations/${ID}/operator/tools/advisor.consult" \
  '{"schema_version":"advisor-consult.v1","arguments":{"consultation_type":"pivot_strategy"},"purpose":"Comparer les pivots à partir des mesures disponibles.","expected_version":7}' \
  "advisor-${ID}-pivot-01"
```

Remplacer `7` par `working_state_version`. Les types acceptés sont `plan_investigation`, `pivot_strategy`, `yara_draft` et `review_text`. `review_text` reçoit un seul bloc d’avis versionné. `yara_draft` ne reçoit que des mesures vérifiées avec leurs offsets, blobs, hashes et versions d’extracteur; son texte reste un candidat.

La réponse `advisor-response.v2` contient des hypothèses alternatives, une expérience discriminante, une piste écartée et des références de base. Chaque recommandation de pivot nomme un outil et sa version exacte, explique le signal attendu et cite au moins une observation réelle. Le serveur refuse la réponse si ses références ne figurent pas parmi les observations/sources transmises ou si l’outil/la version n’est pas disponible. Les données et la réponse restent marquées non fiables.

Les champs envoyés sont limités à l’article synthétisé, aux observations/résumés sélectionnés, aux mesures vérifiées, à l’avis cité, aux outils disponibles et au budget. Aucun octet brut, secret, chemin hôte ou URL signée n’est transmis. Le résultat complet est conservé dans l’observation `advisor.consult`; `context_units` donne l’estimation de tokens dérivée des octets de contexte, et `tool_coverage` reprend le checkpoint, les références, les dimensions de coût, la décision et le résultat observé.

## Décider, tracer et vérifier

Bridge propose; l’opérateur vérifie le schéma et les effets, puis choisit séparément l’outil à invoquer. Chaque appel opérateur conserve le coût (`bridge_turns`, octets/tokens de contexte, secondes), les observations sources et les refs de sortie. Le journal affiche `consulted`, `skipped(reason)` ou `denied(reason)`. Une action après un checkpoint non consulté est enregistrée comme saut; un refus TLP/divulgation ou budget est enregistré comme refus.

Pour donner un motif explicite de report, préfixer `purpose` avec le marqueur ci-dessous. `checkpoint_id` est la raison de `advisor_guidance` suivie de `@working_state_version`:

```text
[[advisor-trace.v1]]{"checkpoint":{"id":"new_pivot_discovered@7","status":"skipped","reason":"Comparer d’abord les deux observations sources."}}
Analyser le nouvel indicateur dans la frontière de pivots.
```

Pour suivre une recommandation, référencer l’identifiant de l’invocation Bridge et son index dans `recommended_next_tools`. Une acceptation doit invoquer l’outil et la version proposés; le serveur vérifie la référence de l’avis et les observations de base. Un refus exige une autre action et un motif:

```text
[[advisor-trace.v1]]{"recommendation":{"invocation_id":"<advisor-invocation-uuid>","index":0,"decision":"accepted"}}
Vérifier le résultat du pivot proposé.
```

La chaîne est alors visible comme recommandation → décision → invocation → statut et observations de sortie. Une action acceptée sans observation reste `empirical_refs=pending` jusqu’à ce que des mesures soient disponibles. Aucun statut de suivi ne transforme une suggestion en preuve.

Relire `/operator-status` et `/operator/context` après chaque appel. En cas de `stale_working_state` (409), relire la version avant de reformuler. Après `advisor_rate_limited`, `advisor_pacing_wait` ou timeout ambigu, ne pas retenter à l’aveugle; conserver l’état incertain et réconcilier les invocations.

## Mesure des seuils

Les fixtures C5 identifient sept causes de checkpoint distinctes, donc au plus sept consultations si chacune est choisie une fois, sous le plafond existant de huit. Le rappel de 12 outils ne sert qu’aux longues séquences sans événement; la fixture le déclenche après exactement 12 outils depuis le dernier avis. Ces mesures synthétiques justifient de garder les valeurs par défaut 12/8; elles ne prétendent pas mesurer une investigation réelle.

## Consultation réelle sur la stack

L’orchestrateur rejoue au minimum `pivot_strategy` et `yara_draft` par l’API réelle sur une investigation `operator_led`. Prérequis: G2 active et non révoquée, fournisseur Bridge autorisé dans l’enveloppe, `external_llm_allowed=true`, `do_not_submit=false`, TLP compatible, capacité `bridge.advisor` disponible, budget `bridge_turns` et budget de contexte non épuisés, délai de pacing respecté, observations de référence appartenant à l’investigation. Utiliser la version CAS courante.

Pour chaque appel, vérifier l’invocation API, son statut, l’observation MinIO `advisor.consult`, son SHA-256, le `consultation_type`, les refs d’entrée/sortie, la réponse structurée et toutes les dimensions de coût. Puis invoquer explicitement une suggestion `pivot_strategy`, relire l’observation produite et consigner `accepted` seulement si l’outil/version correspondent. Sur un second scénario, consigner un refus motivé et vérifier l’action alternative et ses résultats. Ne pas appeler Bridge si TLP/divulgation, G2, capacité ou budget refusent la consultation; vérifier que le refus est persisté sans effet externe.

Les suggestions ne donnent aucune autorisation G2/G3, d’upload, de rescan, d’exécution de binaire, de publication ou de qualification d’échantillon. Une règle YARA reste un brouillon qui doit être testée sur des mesures réelles.
