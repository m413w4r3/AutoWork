# Outils locaux `rule.*` pour une enquête

Le RuleWorkbench est un outil facultatif appelé par le registre de l’enquête. Il
ne fait pas partie des préconditions pour conclure une enquête de chaîne
d’infection. Si le moteur YARA, les octets ou les dépendances locales manquent,
le résultat est limité à l’outil (`unavailable` ou `partial`) et la boucle
d’enquête peut continuer.

`register_rule_tools(registry, *, workbench, candidates, associations,
corpus_bytes, recorder, ...)` ajoute les cinq outils sans modifier la
composition de l’enquête. Leurs schémas sont fermés (`additionalProperties:
false`) et déclarent JSON Schema Draft 2020-12. Le registre valide les
arguments avant d’appeler les adaptateurs.

## Outils et entrées

| Outil | Entrées principales | Effet |
| --- | --- | --- |
| `rule.propose` | `condition` obligatoire ; `motifs`, `feature_refs`, `candidate_id`, `meta` facultatifs | Valide les motifs et compile/teste une proposition avant d’enregistrer une version candidate. Les features P09/P10 sont chargées par `feature_refs` côté application. |
| `rule.test` | `candidate_id`, `version` facultatif | Recompile et teste la version sélectionnée (par défaut la dernière) contre le corpus courant. Ajoute une observation de rapport, sans créer de nouvelle version. |
| `rule.revise` | `candidate_id`, `parent_version` ; au moins un de `motifs` ou `condition` à l’exécution ; `meta` facultatif | Construit, compile et teste la nouvelle version. Le parent doit être la dernière version. |
| `rule.compare` | `candidate_id`, `version_a`, `version_b` | Compare les résultats des deux sources sur le même corpus courant et propose un finding `detection` avec les références d’observation. |
| `rule.get` | `candidate_id`, `version` facultatif, `include_history` facultatif | Retourne les résumés et rapports enregistrés ; l’historique est borné aux 64 dernières versions. |

Les entrées de `motifs` sont limitées à 128 motifs, chacun avec son type,
provenance et références d’observation. Chaque chaîne est bornée à 512
caractères ; les références et hashes sont validés. `validate_motifs`, la
construction et la compilation du RuleWorkbench restent l’autorité pour
refuser les motifs triviaux ou non compilables. Une proposition sans motif
exploitable échoue localement.

Les outils n’exposent aucune opération d’approbation ni de publication. Les
versions sont enregistrées avec leurs hashes et leur parent ; les états
produits sont `candidate`, `experimental`, `tested` ou `rejected`. Une version
`approved` ne peut être révisée par cet adaptateur. L’approbation et
l’archivage restent une revue humaine séparée.

## Capacités

`rule_tool_capabilities(...)` projette les ports présents sur les clés du
registre :

| Capacité | Dépendances nécessaires |
| --- | --- |
| `rule.workbench` | `workbench` |
| `rule.propose`, `rule.test`, `rule.revise`, `rule.compare` | `workbench`, dépôt de candidats, associations, port d’octets et `EvidenceRecorder` |
| `rule.get` | dépôt de candidats, dépôt d’observations et lecteur de payloads |

La présence de la capacité ne garantit pas que le moteur ou chaque octet soit
disponible au moment de l’appel. Ces indisponibilités sont rendues dans le
statut local de l’outil.

## Corpus d’enquête et octets

Le corpus est reconstruit à chaque test à partir des associations et index
disponibles pour l’enquête :

- associations `confirmed` et seeds documentés non rejetés : `confirmed_positive` ;
- associations restantes (`candidate`, `related`, `rejected`, `uncertain`) :
  `out_of_cluster_candidate` ;
- sources du baseline goodware lié à l’enquête : `goodware` ;
- hashes du holdout indépendant : `holdout`, lorsque le fournisseur est configuré.

Les cas sont dédupliqués par rôle et SHA-256. Le rapport conserve les hashes
exacts par rôle, le biais de sélection, la couverture, ainsi que les résultats
`matches`, `non_matches`, `non_measurable` et erreurs pour chaque rôle. Un
octet absent ou non lisible reste non mesurable ; il n’est pas traité comme un
non-match.

`QuarantineRuleCorpusBytes` résout les objets par SHA-256 dans la quarantaine
des échantillons ou le bucket des baselines goodware. Il impose une taille
maximale (64 Mio par défaut), puis vérifie taille et SHA-256 après lecture. Il
ne journalise pas les octets. Aucun seuil universel de taille du corpus ne
bloque un résultat : une règle testée sur un seul positif mesurable est
`experimental`.

## État local des opérations

Les statuts d’invocation et les statuts des versions sont distincts :

| Situation | Invocation | Version candidate |
| --- | --- | --- |
| Moteur indisponible, aucun cas mesurable ou aucun octet exploitable | `unavailable` | Pas de nouvelle version sans exécution exploitable |
| Rapport incomplet : octets non mesurables, erreurs de cas ou limites du corpus | `partial` | `candidate`, sauf signal de rejet ou statut expérimental/testé établi |
| Compilation invalide ou correspondance goodware | `succeeded` avec le rapport local | `rejected` |
| Un seul positif correspondant, sans autres résultats positifs | `succeeded` ou `partial` selon la couverture | `experimental` |
| Plusieurs positifs mesurés sans faux positif dans le corpus complet | `succeeded` | `tested` |
| Pas de candidat pour `rule.get` | `empty` | — |

Une correspondance goodware entraîne `rejected`, même si l’avis de l’enquête
reste publiable. Le workbench ne décide pas de la publication de l’avis.

## Observations, versions et rejouabilité

Chaque test et comparaison est consigné par `EvidenceRecorder` comme
`EvidenceKind.YARA_MATCH`. Les rapports incluent le SHA-256 de la source de
règle, la version du moteur, le corpus exact par rôle et les résultats par
hash. Les comparaisons incluent les correspondances gagnées/perdues,
non-comparables et les faux positifs ajoutés/retirés. Le finding `detection`
retourné par `rule.compare` est une proposition à l’agent, pas un finding
persisté automatiquement.

La source YARA, les octets et les extraits ne sont jamais renvoyés par les
outils ni incorporés aux observations. Les sorties et rapports exposent des
hashes et des résumés bornés. Les chaînes de motifs restent des données non
fiables validées localement avant compilation.

Les versions persistées sont immuables et portent leur parent (`v1`, `v2`, …).
Le rejeu d’un appel avec la même clé d’idempotence renvoie le résultat mis en
cache par le dispatcher et ne relance pas la compilation. Les clés des
observations comprennent aussi l’opération et l’empreinte du rapport afin de
lier le contenu exact à son enregistrement.
