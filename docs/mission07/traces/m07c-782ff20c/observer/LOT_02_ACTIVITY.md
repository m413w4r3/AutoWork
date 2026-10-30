# Audit observer — lot 02 (analyse et pivots VT)

Investigation `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03` · opérateur `CTI-OPERATOR-A`

## Fenêtre et état de réception

- Activité couverte : `2026-10-30T00:32:40Z`–`01:05:01Z` (environ 32 min), des premières actions après l'extraction statique jusqu'au GET d'état `a178`.
- Sources : journal, journal opérateur, statut du relais, outbox sélectionnées et contexte canonique déjà récupéré par l'opérateur; tous dans `m07c`.
- Aucun POST ou GET API de la part de l'auditeur. À `a178`, aucune demande n'était en vol/en file; pas de `FINAL_CHECKPOINT.md`.

## Progrès et statuts

- `a112` chaînes obfusquées est resté `partial`; l'opérateur a lu l'observation, puis consulté les chaînes du candidat et VT behaviour. `vt.behaviour_pcap` (`a118`) est `needs_review`; il n'a pas été rejoué à l'identique et il a poursuivi par les pivots disponibles. La relation `contacted_domains` pour le candidat #5 a renvoyé `empty` (`a125`); ce résultat ne prouve pas l'absence de domaines.
- L'analyse du candidat #3 (`a129`) est `partial`, suivie d'`assembly.inspect` et `methods.list`. Le chef a déclaré l'hypothèse proxy et proposé un finding versionné (`a135`). Le finding lu dans `a137` et `a138` est `inconclusive` et se limite à l'égalité du SHA d'un membre listé de l'archive #7 et du projet XML #12; quatre observations sont référencées, dont le listing hashé, le rapport et l'association. La relation d'identité est directement étayée. Tension de qualification à revoir : le finding affirme lui-même que la relation exacte est établie, alors que son assessment est `inconclusive`. Selon le brief, un fait atomique directement observé peut être `supported`; une qualification générale ne devrait pas empêcher ce statut pour cette portée étroite. Ce constat ne soutient ni exécution ni chaîne d'infection plus large.
- Première consultation YARA Bridge (`a144`) : échec de préflight pour référence canonique manquante, aucun tour consommé. La consultation corrigée (`a146`) est `partial`, un tour consommé, `usable=false`, `rule_source=null`; le reparse local (`a147`) échoue `unsupported_legacy_format`, sans nouvelle observation source ni tour Bridge. Le journal déclare l'étape 5 sans code récupérable. L'étape 6 n'a pas de `rule.test` disponible (workbench désactivé) et aucun test n'a été revendiqué.
- Consultation pivot Bridge (`a149`) : réponse `partial`, normalisée `usable=true`, mais sans pivot précis faute de catalogue joint. L'opérateur a alors paginé le catalogue courant `available_pivots` (a151–a155) et n'a exécuté que les pivots listés.
- Le premier `vt.file_report` MD5 (`a156`) a échoué `external_effect_refused`; l'observation rapporte aucune route fournisseur et aucune page. L'opérateur a réconcilié l'état, n'a pas répété la demande identique, puis a lancé les recherches VT sur les 15 SHA du catalogue : a158–a172 sont toutes `succeeded`.
- Les relations VT ont commencé ensuite. `a173`–`a176` sont `succeeded` pour deux candidats et deux types de relation. `a177` (résolution `ipv4`) est `failed`; l'opérateur a immédiatement relu l'état (`a178`) et a laissé le refus distinct d'un résultat vide/négatif. Pas de nouveau pivot après ce point encore.

## Finding et preuve

Le finding canonique observé a l'ID `7ec78429-7c0b-5fb5-b1cb-227cae1ede36`. Son statement se limite à la relation d'identité archive-member → projet-candidat; les observations référencées comprennent le listing exact à hash concordant (`322453f8-c178-5168-ada7-fbc0840f901b`), le rapport du projet et son association. La portée textuelle est prudente et correctement exclut attribution, exécution et chaîne plus large. Seul l'assessment `inconclusive` paraît trop faible au regard du fait exact énoncé; le chef devrait vérifier le statut contre les observations avant la clôture. Aucun autre finding n'est visible dans les sections lues.

## Bridge et avis

Les outboxes Bridge confirment des invocations `advisor.consult` et des champs `advisory_text` en prose; Bridge est la source du conseil, pas de preuve. La réponse YARA normalisée n'a fourni ni source de règle ni résultat exploitable; le parseur local exact n'a pas récupéré de source. Le texte exact des prompts reste absent du journal/artefacts observés, donc l'audit ne peut confirmer qu'ils étaient formulés en prose sans demande de sérialisation machine. Aucun appel `opinion.compose`/`opinion.revise`, texte d'avis courant ou revue d'avis n'est observé à l'état v84.

## Budget et réconciliation au GET `a178`

État canonique v84, SHA `0d02a367ba4dfb6848bffb8accc9b1d4995d52ba50690a220756a31620da952e`; enquête toujours `running`, G2 approuvé/non révoqué v167, TLP `clear`, `external_llm_allowed=true`. Aucun appel ambigu ou à réconcilier, aucune réservation.

- Bridge : 3/200 tours consommés; aucune réservation. Le coût fournisseur reste inconnu lorsque `provider_usage` n'est pas disponible.
- VT : 46/10 000 unités consommées; téléchargements : 8/1 000; Shodan : 0/1 000.
- Outils : 696/86 400 secondes consommées.
- Compteurs de contexte : 186 627/16 000 000 tokens estimés et 746 502/67 108 864 octets. Ce sont les compteurs de politique, pas une mesure de facturation.

## Couverture 0–9 après ce lot

- 0 acquisition : achevée pour les 15 reports et acquisitions tentées; huit acquisitions nouvelles, sept réutilisations.
- 1 plan avec Bridge : effectué mais conseil de planification partiel; le prompt exact reste invérifiable.
- 2 vérification des capacités : effectuée par inventaires/capacités courants; la liste mission conserve son saut de numérotation.
- 3 analyse statique : en cours; résultats `partial`/`unavailable` conservés sans conclusion négative, pivots alternatifs utilisés.
- 4 protocole/réseau/crypto : aucune rétro-ingénierie ou figure observée; un PCAP VT est `needs_review`.
- 5 règle YARA proposée par Bridge : consultation reçue mais aucun texte/source de règle récupérable ou utilisable.
- 6 validation YARA : indisponible, non effectuée.
- 7 pivots VT : recherches SHA terminées avec succès; relations en cours; échecs/vides/needs_review distingués.
- 8 téléchargement/validation/itération de candidats : acquisitions faites à l'étape 0, mais pas de boucle YARA validée; couverture incomplète.
- 9 avis Bridge et relecture : non effectués.

## Appréciation provisoire

Pas d'arrêt prématuré : l'opérateur continue après les refus, avec état v84, invocation non ambiguë et recherche de pivots encore en cours. Aucun résultat `empty`, `failed`, `partial` ou `needs_review` n'est transformé en absence d'activité. Point de correction probatoire à examiner : statut `inconclusive` du finding exact. L'écart d'isolation de la worker signalé au lot précédent demeure le seul accès hors catalogue visible; aucun nouvel accès hors catalogue ni lecture de répertoire interdit n'est documenté dans ce lot. Les journaux seuls ne permettent toujours pas de prouver l'absence de lectures locales non journalisées.

Prochain contrôle : suivre la fin de la boucle de relations/pivots, toute révision du finding, la rédaction Bridge exclusive de l'avis et sa revue; confirmer les budgets après le dernier appel et surveiller le checkpoint final.
