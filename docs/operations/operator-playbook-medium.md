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

## Déclarer une hypothèse et consigner une expérience

En `operator_led`, déclarer d’abord l’hypothèse choisie avec `hypothesis.declare` (`operator-hypothesis-declare.v1`, capacité `operator.write`, G2 courant). Fournir `hypothesis_id` et `statement`; le rejeu avec la même clé d’idempotence restitue l’invocation, et le même contenu sous une autre clé ne crée pas de doublon. Ensuite, `experiment.record` (`operator-experiment.v1`) n’accepte que cette hypothèse, les claims de l’article et des observations persistées de l’investigation avec leur SHA-256 exact.

Pour `experiment.record`, `expected_version` désigne le `stream_version` du flux d’expériences, pas `working_state_version`. En cas de refus, `experiment_hypothesis_unknown` indique une hypothèse non déclarée, `experiment_claim_unknown` un claim absent de l’article, et `experiment_observation_ref_mismatch` une observation absente/d’une autre investigation ou un SHA-256 discordant. Une observation `not_observed` à couverture `limited` ou `none` ne peut pas réfuter un claim.

Une nouvelle expérience peut fournir `supersedes_experiment_ids` pour remplacer des expériences antérieures du même flux. La consolidation écarte leurs verdicts, conserve l’historique append-only et ne garde que le dernier verdict non remplacé; les contradictions entre verdicts actifs restent « nuancées ». Les anciens enregistrements sans ce champ restent valides.

Les sorties `method.decompile` et `local.run_command` conservent jusqu’à 48 KiB de résultat assaini dans l’observation, en fragments de 4 000 caractères au plus, avec SHA-256. Le JSON CIL .NET est découpé directement; la sortie locale est encodée en `base64-utf8` (`output_encoding`, `output_chunks`, `output_byte_length`, `output_sha256`). La réponse opérateur reste un aperçu. Lire le reste via `observation_url` et ses paramètres `offset`/`length` (pages de 32 KiB maximum); les limites de sortie confinée de chaque outil restent applicables.

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

Pour `advisor.consult`, régler le timeout du client appelant à au moins 200 s. Une réponse Bridge a déjà pris 163 s; un client limité à 120 s peut abandonner alors que l’invocation reste `running`. Ce délai concerne le client opérateur, pas le transport serveur.

## Mesure des seuils

Les fixtures C5 identifient sept causes de checkpoint distinctes, donc au plus sept consultations si chacune est choisie une fois, sous le plafond existant de huit. Le rappel de 12 outils ne sert qu’aux longues séquences sans événement; la fixture le déclenche après exactement 12 outils depuis le dernier avis. Ces mesures synthétiques justifient de garder les valeurs par défaut 12/8; elles ne prétendent pas mesurer une investigation réelle.

## Consultation réelle sur la stack

L’orchestrateur rejoue au minimum `pivot_strategy` et `yara_draft` par l’API réelle sur une investigation `operator_led`. Prérequis: G2 active et non révoquée, fournisseur Bridge autorisé dans l’enveloppe, `external_llm_allowed=true`, `do_not_submit=false`, TLP compatible, capacité `bridge.advisor` disponible, budget `bridge_turns` et budget de contexte non épuisés, délai de pacing respecté, observations de référence appartenant à l’investigation. Utiliser la version CAS courante.

Pour chaque appel, vérifier l’invocation API, son statut, l’observation MinIO `advisor.consult`, son SHA-256, le `consultation_type`, les refs d’entrée/sortie, la réponse structurée et toutes les dimensions de coût. Puis invoquer explicitement une suggestion `pivot_strategy`, relire l’observation produite et consigner `accepted` seulement si l’outil/version correspondent. Sur un second scénario, consigner un refus motivé et vérifier l’action alternative et ses résultats. Ne pas appeler Bridge si TLP/divulgation, G2, capacité ou budget refusent la consultation; vérifier que le refus est persisté sans effet externe.

Les suggestions ne donnent aucune autorisation G2/G3, d’upload, de rescan, d’exécution de binaire, de publication ou de qualification d’échantillon. Une règle YARA reste un brouillon qui doit être testée sur des mesures réelles.

## Actualiser les figures de protocole d’un avis

Les figures D2 sont des pièces jointes immuables du brouillon qui les a sélectionnées. `network.protocol.render` enregistre une observation des spécifications sémantiques; la compilation D2 actuelle et l’attachement des trois SVG ont lieu lors de `opinion.revise`. Un aperçu d’une ancienne version continue d’utiliser ses SVG persistés et affiche un avertissement « Rendu hérité » lorsque leur politique de projection ou leur légende est ancienne.

Pour Run12, l’investigation est `5f66ebfb-1653-45e6-83cd-a29af6f87868`, le modèle documenté est le blob `e535d7e4-7a4d-470d-818f-2c3a9ea9ae7a`, et l’observation de rendu sémantique actuellement attachée est `070bdb45-c9fa-525d-b6ab-66d89310e763`. L’avis brouillon `d9935324-2ccc-5067-9dc9-17efc0637823` est en v26 (`4f9157804db0349215f65e6220b503fe6a8a1ba6be7c38e0494c734fd9ed8a8c`); la publication de base a le SHA-256 `3a436eada0d8162aef1d730b60b3f739e6f7c056b80593dbf9d97510133b96ba`.

1. Relire `GET /api/investigations/5f66ebfb-1653-45e6-83cd-a29af6f87868/opinion`. Vérifier que le brouillon courant est toujours v26, récupérer ses `blocks` et `unresolved_points`, et noter son SHA-256. Si la version a changé, utiliser ses valeurs actuelles; ne pas écraser une révision concurrente.
2. Invoquer `network.protocol.render` avec `arguments: {"protocol_model_blob_id":"e535d7e4-7a4d-470d-818f-2c3a9ea9ae7a"}`. Conserver le nouvel `observation_ref` renvoyé. L’observation `070bdb45-c9fa-525d-b6ab-66d89310e763` est l’ancienne référence et ne doit pas être réutilisée pour cette sélection.
3. Invoquer `opinion.revise` sur l’avis `d9935324-2ccc-5067-9dc9-17efc0637823` avec `action: "revise"`, `expected_opinion_version: 26`, `expected_opinion_sha256: "4f9157804db0349215f65e6220b503fe6a8a1ba6be7c38e0494c734fd9ed8a8c"`, les `blocks` et `unresolved_points` relus à l’étape précédente, et cette sélection:

   ```json
   {
     "protocol_figures": [
       {
         "protocol_model_blob_id": "e535d7e4-7a4d-470d-818f-2c3a9ea9ae7a",
         "render_observation_id": "<nouvel observation_ref de network.protocol.render>",
         "views": ["sequence", "state_machine", "message_format"]
       }
     ]
   }
   ```

   Les trois vues sont sélectionnées ensemble. Vérifier que le résultat est un brouillon v27 et relever le nouveau `content_sha256`; cette révision ne valide ni ne publie l’avis.
4. Refaire l’aperçu sans demander de recompilation implicite: `GET /api/opinions/d9935324-2ccc-5067-9dc9-17efc0637823/preview.pdf?version=27&opinion_sha256=<content_sha256_v27>&base_publication_sha256=3a436eada0d8162aef1d730b60b3f739e6f7c056b80593dbf9d97510133b96ba`. Vérifier les figures de protocole autour des pages 16–17 (et leur nouvelle pagination), les légendes courtes, les libellés de preuve non répétés et l’absence de l’avertissement « Rendu hérité » pour les trois pièces jointes v27.

Ne pas utiliser l’option `protocol_model_blob_id` de la route d’aperçu pour un contrôle en lecture seule: elle compile et persiste des SVG. L’aperçu sans cette option reste strictement lié aux figures déjà persistées dans la version demandée; un avertissement visible identifie les pièces jointes anciennes jusqu’à leur réattachement dans une nouvelle version du brouillon.
