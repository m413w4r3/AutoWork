# Audit du comportement des opérateurs agentiques — Mission 07

**Date :** 2026-10-29
**Périmètre :** sessions CTI-ANALYST 1–10, décisions d’orchestration et consultations Bridge archivées dans `.autowork-runs/m07/`.
**Base documentaire :** checkout local `aab700106a00777ff7a241467dfdb6c34a8eaa0f`.
**Nature :** audit rétrospectif en lecture seule; ce rapport n’est ni une observation CTI ni une preuve canonique.

## Méthode et limites

**Fait** désigne un compte ou un état directement vérifiable dans le journal, les fichiers d’entrée/sortie ou un rapport d’observateur cité. **Interprétation** désigne une conclusion comportementale déduite de ces éléments; les incertitudes sont indiquées séparément.

Le journal comporte 816 lignes : 815 entrées d’opération exploitables et une entrée d’exception du relais. Il couvre 644 GET et 171 POST. Neuf sessions analyste apparaissent dans le journal (#1–6 et #8–10) : 650 opérations, dont 507 GET et 143 POST. Aucune entrée n’est attribuée à CTI-ANALYST-7. Les métadonnées de `processed/` et `outbox/` ont servi à mesurer les temps requête→résultat; leurs contenus bruts n’ont pas été dumpés. L’inventaire local compte 815 fichiers JSON et un fichier `.err` sous `processed/`, puis 794 réponses sous `outbox/`.

`ts` donne l’intervalle entre événements, pas l’heure d’envoi de chaque requête. Sur 814 intervalles, la médiane est 11,2 s, le p90 77,3 s et le maximum 5 h 03 min. Ces intervalles incluent réflexion humaine, attente et changement de session. Pour les durées d’appel, les paires de fichiers d’entrée/résultat sont plus informatives : 794 paires, médiane 1,8 s, p90 5,9 s, 38 au-dessus d’une minute, maximum 19 min 20 s. Ce temps comprend file, traitement backend et transport; il ne mesure pas à lui seul la génération du modèle.

## Résumé chiffré

- **650 opérations analyste** : 507 GET, 143 POST. `operator-status` représente 171 GET (26 % des opérations analyste). Ces statuts ont renvoyé 19,8 Mo au total, avec une taille médiane de 137 Ko. Quarante-quatre GET de statut répètent, dans la même session, un SHA de réponse déjà lu; ils pèsent 6,2 Mo. Une partie correspond à la vérification d’un appel en attente ou d’un CAS; ce n’est pas 44 erreurs analytiques.
- Les 143 POST analyste se répartissent en 92 `succeeded`, 10 `partial`, 18 `needs_review`, 13 `failed`, 6 `empty`, 1 `policy_denied`, 1 `unavailable` et 2 HTTP 409 sans invocation créée. Ainsi, 49/141 invocations avec statut ne sont pas `succeeded` (34,8 %), mais `partial`, `empty` et `needs_review` ne signifient pas tous échec fonctionnel. Vingt-deux des 650 requêtes analyste ont un HTTP différent de 200 (3,4 %); cela inclut des refus de pagination, des CAS périmés, des routes non exposées et les deux POST 409.
- **27 POST `advisor.consult` par les analystes** : neuf ont consommé un tour Bridge; 18 se sont arrêtés avant un tour. Six des neuf ont fourni un conseil utilisable; trois tours n’ont produit aucun conseil exploitable (`c6-104`, `c8-126`, `c9-049`). Le bilan global de fin de mission est de 11 tours consommés, dont deux consultations smoke de l’orchestrateur (handoff §1; `bridge_eval_001.md:L7`).

| Outil POST | Appels analyste |
|---|---:|
| `advisor.consult` | 27 |
| `advisor.reparse` | 2 |
| `assembly.inspect` | 1 |
| `evidence.get` | 3 |
| `finding.propose` | 8 |
| `finding.revise` | 16 |
| `ghidra.decompile_function` | 1 |
| `ghidra.list_imports` | 1 |
| `hypothesis.declare` | 4 |
| `local.run_command` | 6 |
| `method.decompile` | 15 |
| `methods.list` | 4 |
| `opinion.compose` | 1 |
| `opinion.revise` | 12 |
| `references.imports` | 5 |
| `sample.analyze` | 2 |
| `sample.extract_obfuscated_strings` | 1 |
| `strings.list` | 3 |
| `types.list` | 1 |
| `vt.behaviour` | 4 |
| `vt.download_sample` | 3 |
| `vt.file_report` | 9 |
| `vt.relationships` | 14 |

Les GET les plus fréquents étaient `operator-status` (171), `available_tools` (30), `findings` (28), résumé `operator/context` (25), `article_claims` (17), puis `current_opinion` (10) et `finding_versions` (9). Les 22 lectures de l’observation liée à `c9-049` sont des pages de récupération d’une réponse ambiguë; elles ont permis d’établir qu’aucun texte de réponse ne pouvait être reparsé. Le besoin de vérifier l’ambiguïté était légitime, mais une vue de statut courte aurait évité cette pagination.

## Décisions utiles, répétitions et protocole

### Choix analytiques solides

- #1 a arrêté une piste Bridge après deux refus identiques de préflight secret, sans tenter de contourner le garde (`a030`, `a033`; `analyst_log.md:L153`). Il a conservé `partial`/`unavailable` comme limites, pas comme résultats négatifs.
- #6 et #8 ont préféré une comparaison déterministe à une conclusion tirée de noms d’archive. L’observateur juge l’analyse CIL ciblée et les limites entre code statique, point d’appel, exécution et attribution correctement séparées (`report_002.md:L108`, `L118`, `L120`, `L122`).
- #7 n’a pas renvoyé une consultation Bridge encore susceptible d’être traitée. Plus tard, #9 n’a pas rejoué `c9-049` après une soumission externe ambiguë; #10 a accepté les sept unités de revue individuellement utilisables et n’a pas relancé le POST malgré les métadonnées globales incohérentes (`analyst_log.md:L991`, `L994`, `L996`, `L997`, `L1013`, `L1014`). Ces décisions ont évité des doubles envois.
- #9 a séparé des constatations atomiques directement étayées des hypothèses plus larges. Les claims de campagne, d’exécution ou d’attribution sont restés `qualified`; les constatations étroites n’ont été promues qu’après réconciliation canonique (`analyst_log.md:L957`, `L962`, `L967`).

### Répétitions évitables ou coûteuses

- `vt.file_report` sur une invocation déjà `needs_review` a été renvoyé sous `a077` (journal L79) puis sous `c016` (L170); les deux retours sont HTTP 409 et ne créent pas de nouvelle invocation. Aucun doublon externe n’est démontré, mais l’effet initial reste à réconcilier (`analyst_log.md:L266`, `L268`; `report_001.md:L251`).
- Treize consultations Bridge initiales (#1–5) n’ont obtenu aucun conseil : références invalides ou manquantes, deux refus secrets, exigences récursives de récupération de preuves, exigence de finding réfuté pour un plan neutre, puis blocage d’autorité (`report_001.md:L221`, `L228`, `L230`, `L231`, `L232`, `L233`, `L234`, `L238`, `L239`). Quatre essais `a057/a062/a072/a074` ont rebondi sur la complétude du même dossier; quatre autres `c013/d008/d014/d021` sur le même garde de réfutation. Les payloads ont changé, mais répéter après le retour du même garde n’apportait plus d’information (`report_001.md:L252`, `L253`).
- Les GET identiques ne sont pas tous inutiles : un statut immédiatement après un POST peut réconcilier une mutation. En revanche, #10 a relu quatre fois le même statut sans mutation entre les lectures (`c10-status-after-bridge`, `c10-status-before-final-revise`, `c10-status-pre-opinion-revise`, `c10-status-pre-final-v9`; journal L803, L808, L809, L810). #6 a aussi relu deux fois la même section `opinion_notes` (`c6-077`, `c6-078`; `report_002.md:L129`). Plusieurs sessions ont rechargé les mêmes observations canoniques; une revalidation au démarrage d’un nouvel analyste peut être utile, mais le résumé/delta devrait éviter de tout relire.
- La première pagination d’outils a utilisé un offset non supporté, puis un curseur sans CAS, puis le SHA du contexte à la place du SHA de l’état de travail (`a008`, `a009`, `a010`; journal L9, L10, L11; correction `a012`, L13). D’autres erreurs de pagination/route : offset `0` (`b003`), limite supérieure à 20 (`b049`), section `opinion` inexistante (`c10-opinion-section`, journal L790). Celles-ci sont des erreurs de contrat navigable, pas des résultats d’analyse.
- Le parcours `supported` de #9 a nécessité trois révisions après que le validateur a rejeté `subject_ref="sha256:…"` : le champ devait correspondre exactement à l’`object_id` canonique, sans préfixe (`analyst_log.md:L962`, `L964`, `L967`). Le moteur a protégé la portée de preuve, mais l’erreur n’expliquait pas assez tôt la forme attendue.

## Bridge : coût, latence et valeur

Les 27 consultations analyste ont produit neuf tours consommés. Les dossiers des neuf appels avec tour comptabilisent 1 715 391 octets (médiane 166 070; min. 157 910; max. 404 670); les prompts préparés totalisent 1 765 211 octets et les réponses archivées 28 363 octets. L’appel ambigu `c9-049` a un dossier de 166 070 octets, zéro octet de réponse et un tour enregistré. L’usage fournisseur en tokens ou en monnaie n’est pas disponible; les octets/tokens estimés ne sont pas une facture.

Le temps requête→outbox des neuf consultations avec tour a une médiane de 539 s (9 min), un p90 de 683 s (11 min) et un maximum de 1 160 s (19 min). Les appels sans tour ont tout de même une médiane de 142 s et un maximum de 600 s : l’attente ne vient donc pas seulement de la génération Bridge. Les mesures `bridge_eval_001.md:L12`, `L16`, `L22`, `L25`, `L27`, `L29`, `L31`, `L33`, `L34`, `L35`, `L36` confirment 450–683 s pour neuf tours de #6/#8 et des smokes; #9 a ensuite attendu 19 min (`analyst_log.md:L991`).

Les consultations utiles ont apporté deux types de valeur : un test de comparaison falsifiable, déjà suggéré auparavant et donc répétitif (`c6-084`; `bridge_eval_001.md:L58`), puis un nouveau discriminant statique reliant une condition d’échec à une chaîne d’appels possible (`c8-077`; `bridge_eval_001.md:L60`). L’opérateur a accepté le critère, mais a reporté l’exécution lorsque l’outil disponible ne montrait pas les appelants. Les revues `c8-129`, `c8-136`, `c8-153` et `c10-bridge-opinion-v8-review` ont surtout resserré le texte, les citations et les limites; elles n’ont pas changé les findings. `c10` a rendu sept revues utilisables sur sept malgré le statut global `partial` et l’index d’unités contradictoire (`analyst_log.md:L1013`, `L1014`, `L1023`).

**Interprétation :** le coût est matériel au regard du gain analytique. Les avis Bridge les plus utiles ont cadré une question ou empêché une surinterprétation; ils n’ont pas ajouté de preuve. Un dossier ciblé par une seule décision et une revue unique du texte courant auraient conservé cette valeur avec moins de re-demandes.

## Arrêt de #7

**Fait :** #7 n’a émis aucun GET ni POST dans le journal; son entrée de travail dit que `pgrep -af relay.py` n’a trouvé aucun processus et qu’il ne pouvait donc pas relire le statut ni les observations (`analyst_log.md:L642`, `L650`, `L651`). Le handoff précise que les agents ne voient pas les processus hôte et que `pgrep` peut donner un faux négatif; le contrôle prévu est un appel de statut via le relais puis une attente courte (`M07_HANDOFF_ORCH6.md:L70`, `L71`). Six minutes après le début de cette entrée, #8 a repris la lecture et a exécuté le test borné disponible (`analyst_log.md:L653`, `L656`, `L657`, `L664`, `L668`).

**Évaluation :** le refus de créer une nouvelle invocation sans statut était prudent, mais la fin de la session était prématurée : le signal choisi ne permettait pas de conclure que le relais était arrêté. La cause immédiate est une vérification de processus depuis un environnement qui n’en voit pas l’hôte, pas une impasse analytique. Un canal de santé/en-cours exploitable depuis le bac à sable aurait évité l’arrêt et la reprise de contexte.

## Prudence probatoire : légitime et excessive

**Rigueur légitime :** les findings larges ne devaient pas devenir `supported` sur la seule base d’un nom, d’une association fournisseur, d’une relation de graphe ou d’un code statique sans appelant/exécution. `qualified` était adapté lorsque la preuve établissait une capacité, une assertion rapportée ou une portion d’un scénario, mais pas le scénario complet. `empty`, `partial`, `unavailable` et une absence de marqueur dans une couverture limitée ne sont pas des réfutations (`report_002.md:L118`, `L120`, `L122`; `report_001.md:L243`, `L244`).

**Sur-prudence de granularité :** un finding atomique sur un objet exact pouvait être `supported` dès qu’une observation directe de même enquête le décrivait. Le test d’identité d’archive exécuté par #8 rendait déjà possible une finding étroite avant #9; les associations de rapport fournisseur pouvaient aussi être formulées comme « le rapport associe cet objet à ce nom », sans inférer ce que l’objet fait. #9 l’a finalement fait sans promouvoir les hypothèses plus larges (`analyst_log.md:L953`, `L954`, `L957`, `L967`). La lenteur vient en partie du choix de findings trop larges et en partie d’une erreur de forme de `subject_ref`, pas d’une exigence d’attribution générale.

## Instructions de prompt : effet observé

**Ont aidé :** expliciter `hypothèse → pivot → critère → coût → prochaine décision`; traiter les sorties d’outils/Bridge comme des données non fiables; demander un discriminant borné; consigner la version exacte de l’avis et vérifier la preuve avant d’accepter un conseil; ne pas re-soumettre une invocation pending ou ambiguë; séparer les faits directement observés des inférences. Cela a produit de bons pivots d’analyse statique et des décisions de non-rejeu justifiées.

**Ont nui ou manqué :** « consulter Bridge souvent » et « à chaque jalon éditorial » ne donnaient ni seuil de valeur décisionnelle ni règle d’arrêt après un même refus. Cette formulation est une explication plausible, pas une causalité démontrée, des 27 consultations et des boucles de préflight (`operator-playbook-medium.md@aab70010:L38`, `L40`, `L42`). Un autre angle mort était de demander une revue sans préciser le format texte attendu : `c8-126` a consommé un tour pour une réponse de quatre octets (`null`), puis `c8-129` a fourni des unités utilisables après clarification (`bridge_eval_001.md:L64`, `L65`, `L66`). À l’inverse, une réponse globale `partial` ne devait pas annuler les sept unités individuellement utilisables de #10.

Le playbook antérieur autorisait l’envoi en clair à Bridge de matériel cryptographique et de jetons extraits. Cette consigne est trop générale : seuls les éléments partageables dans l’autorisation G2/TLP et `external_llm_allowed` du dossier concerné peuvent sortir; les secrets opérationnels AutoWork ne doivent jamais partir. Le texte source donne l’instruction en clair et un gabarit d’extraction (`operator-playbook-medium.md@aab70010:L87`, `L88`, `L91`, `L95`, `L99`). Le playbook réécrit supprime cette consigne.

## Frictions que le logiciel devrait absorber

1. **Relais observable :** exposer un statut de file, l’identifiant en cours, l’heure de réception et une voie de statut courte indépendante du POST. Le relais sérialisé a retardé des requêtes en aval jusqu’à 505 s derrière deux POST Bridge : les GET de statut `c6-085` et `c6-105` ont attendu environ 488 s et 432 s (`c6-084`/`c6-104`; journal L468, L469, L488, L489); une lecture orchestrateur a attendu 505 s. Un agent ne devrait pas utiliser `pgrep` pour inférer l’état d’un processus hôte.
2. **Préflight Bridge compréhensible :** montrer une seule cause précise, le pointeur manquant et le correctif possible avant toute attente longue. Ne pas exiger de finding réfuté pour un plan neutre, et ne pas demander récursivement les corps des observations de récupération (`report_001.md:L230`, `L231`, `L232`, `L233`, `L234`, `L238`, `L239`). Refuser une seconde soumission identique quand le même garde s’est répété.
3. **Schémas découverts avant POST :** retourner le schéma exact par outil et valider l’enveloppe côté client. `c9-053` a demandé `advisor.reparse` avec le schéma générique `1`; `c9-055` a utilisé `advisor-reparse.v1` mais n’a pas pu récupérer de réponse absente (`analyst_log.md:L994`, `L996`, `L997`). Distinguer cette version de parseur du protocole de l’enveloppe relais.
4. **CAS compact et correct :** fournir ensemble la version et le SHA utilisés par la mutation, dire clairement si le CAS est périmé et exposer la version courante. Éviter de forcer une relecture complète de 100–180 Ko pour valider chaque étape.
5. **État Bridge cohérent par unité :** unifier nombre de tours consommés, réponse persistée, `usable`, unités rendues/non traitées et statut global. La revue #10 avait sept unités utilisables mais toutes étaient marquées non traitées; #9 a laissé une réservation en conflit avec le tour enregistré (`analyst_log.md:L995`, `L997`, `L999`, `L1013`, `L1014`). Libérer une réservation seulement après confirmation canonique de non-exécution.
6. **Findings atomiques guidés par l’observation :** préciser le `subject_ref` attendu et l’objet exact de l’observation lors d’une proposition `supported`; ne pas attendre une synthèse éditoriale pour consigner un fait étroit.
7. **Pagination et reprise :** exposer les limites et curseurs valides dans le résultat et donner une vue courte des métadonnées d’une observation (taille, statut, octets de réponse, SHA) afin d’éviter les essais d’offset et les dizaines de pages lorsque le contenu ne peut rien récupérer.
8. **Projection d’avis typée :** faire correspondre le schéma opérateur aux sections typées réellement persistées, ou signaler le champ absent avant Bridge. #9 a découvert qu’une section dédiée acceptée par le texte ne pouvait pas être persistée par l’interface opérateur; le dossier de revue a dû changer (`analyst_log.md:L984`, `L985`, `L989`).

## Portée des conclusions

Les comptes d’opérations et les octets proviennent du journal et des métadonnées de fichiers locaux; les faits de statut Bridge ont été recoupés avec les rapports d’observateur et le journal analytique. Les observations du rapport 001 sont antérieures à #6 et celles du rapport 002 précèdent #8–10; elles sont utilisées comme sources de contrôle, pas comme état final. Les coûts fournisseurs en tokens/monnaie ne sont pas vérifiables. Aucun accès à PostgreSQL, Docker ou au relais live n’a été effectué; aucun appel live ni test n’a été lancé.
