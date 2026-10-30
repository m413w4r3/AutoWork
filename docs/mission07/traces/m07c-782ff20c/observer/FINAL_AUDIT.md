# Audit observer final — investigation 782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03

Opérateur observé : `CTI-OPERATOR-A`  
Arrêt de veille : `2026-10-30T02:47Z`, après apparition de `.autowork-runs/m07c/FINAL_CHECKPOINT.md`.

## Conclusion

L'opérateur a poursuivi les pivots statiques et VT après les échecs, refus, sorties `partial`, `empty`, `unavailable` et `needs_review`, en gardant ces états distincts. Le checkpoint final reconnaît correctement que l'étape 9 reste inachevée : Bridge n'a fourni aucun texte d'avis exploitable, aucun avis canonique n'a été composé ou relu, et une réservation Bridge reste liée à l'invocation `a228`.

La mission est donc **incomplète, sans conclusion d'attribution ou d'exécution**. Le checkpoint n'affirme ni que le brouillon existe ni que l'enquête est probatoirement close. Le prochain pas permis est la réconciliation de `a228`; aucun rejeu identique n'est justifié.

## Périmètre et méthode

- Lecture en observation seule de `AGENTS.md`, `OPERATOR_BRIEF.md`, puis des artefacts de ce seul run : `journal.jsonl`, `operator_log.md`, `relay-status.json`, `outbox/`, `workers/`, le checkpoint final. Les rapports d'audit antérieurs sont dans `observer/`.
- Aucun POST/API, aucune opération sur le relais, aucun accès aux anciens runs, dossiers restreints ou contenu CTI hors de ce répertoire; aucun `ps`/`pgrep`.
- Le payload d'observation non fiable d'environ 6,58 millions de caractères attaché à `a228` n'a pas été ouvert par l'auditeur. Seuls son existence, ses métadonnées et les statuts associés ont été consultés.
- Le journal couvre 232 requêtes, de `2026-10-30T00:00:53Z` à `02:40:34Z`. Le relais était sans demande en vol ni en file après `a230`.

## Résultats vérifiés

### Étapes 0–9

| Étape | Audit final |
|---|---|
| 0 — acquisition et rapports VT | Complète pour les 15 échantillons documentés : 15 rapports et 15 tentatives d'acquisition réussis, huit téléchargements nouveaux et sept associations réutilisées. Toutes restent `candidate/not_confirmed`. |
| 1 — claims et plan Bridge | Partielle. Les claims de l'article sont traités comme données source. Le premier plan Bridge a échoué au préflight; le second a donné un conseil partiel, non fiable et non probant, ensuite vérifié par analyse statique. Le contenu exact du prompt n'est pas journalisé; l'auditeur ne peut donc pas confirmer indépendamment que la question envoyée était uniquement en prose. |
| 2 — outils | Le plan de mission saute le numéro 2; le playbook général utilise 2 pour consulter les capacités avant choix. L'inventaire courant a été consulté. La mention « non défini » du checkpoint correspond à la liste de mission, pas à l'absence de contrôle des capacités. |
| 3 — analyse statique | Partielle. Analyses managées et chaîne d'outils statiques utilisées; pas d'exécution d'échantillon. Plusieurs résultats sont partiels ou indisponibles et sont conservés ainsi. L'observation de texte XML/MSBuild n'a ensuite pas pu être relue par la route canonique (GET 404); l'opérateur l'a exclue des findings et du dossier Bridge. |
| 4 — réseau/protocole/crypto | Aucune rétro-ingénierie directe ni figure. Le PCAP VT est `needs_review` sans capture récupérable. Aucune conclusion négative sur le protocole ou la crypto n'est justifiée. |
| 5 — règle YARA Bridge | Inutilisable. Le premier envoi a échoué au préflight; le dossier corrigé a produit une réponse `partial`, sans source de règle exploitable. Le reparse local a répondu `unsupported_legacy_format`; aucun code récupéré. |
| 6 — validation YARA | Indisponible : `rule.test` et le workbench sont désactivés. Aucune compilation ou validation n'est revendiquée. |
| 7 — pivots VT | Partielle au sens du plan complet. Les 15 recherches SHA et 30 relations SHA-256 ont été terminées; deux pages supplémentaires ont été lues avec les curseurs exacts. Les 22 relations non vides et les huit `empty` restent des états distincts. Le report MD5 et une résolution IPv4 ont été refusés avant fournisseur; ces familles n'ont pas été rejouées. |
| 8 — nouveaux candidats et revalidation | Partielle. Les 15 candidats documentés ont été acquis ou associés, mais aucun nouveau candidat n'a été obtenu depuis les pivots. La revalidation YARA n'a pas pu être faite faute de règle et de workbench. |
| 9 — avis rédigé et relu par Bridge | Inachevée. L'appel `a228` est `needs_review`, `model_submission_reconciliation_required`; les six unités sont non traitées, sans texte d'avis ni SHA de sortie exploitable et avec `confirmed_non_execution=false`. Aucun `opinion.compose`/`opinion.revise`, avis courant ou retour de relecture n'existe. |

### Findings et qualité probatoire

Trois findings versionnés sont lus dans la section canonique `finding_versions` :

- `7ec78429-7c0b-5fb5-b1cb-227cae1ede36` — `infection_chain`, assessment `inconclusive`. Les observations établissent le lien d'identité exact entre le membre d'archive #7 et le fichier candidat #12 par égalité du SHA. Le statement est étroit et exclut l'exécution. Pour cette seule identité de bytes, `supported` paraît mieux correspondre au seuil du brief; `inconclusive` est trop prudent sauf si le finding porte une portée plus large que son statement.
- `941dee1b-0688-513b-ab5f-baff9d094916` — `capability`, `qualified`. Le checkpoint le décrit comme une signature de méthode managée à argument chaîne, observée dans un inventaire complet; corps et comportement absents. Cette qualification est prudente. Si le statement versionné se limite exactement à la présence de cette signature, une revue pourrait aussi vérifier si `supported` est indiqué par la preuve atomique.
- `14fd5a52-2d51-5539-bb08-d382a2f90688` — `capability`, `qualified`, basé sur les résultats statiques partiels du candidat #5. L'absence de comportement à l'exécution et de rôle de loader reste explicitement non établie; `qualified` est proportionné.

Les observations d'article ne sont pas transformées en faits techniques. Les associations de téléchargement restent candidates. `empty`, `partial`, `needs_review`, `failed`, `unavailable` et les refus de fournisseur ne sont pas traités comme absence ou preuve négative. Les références de finding disponibles identifient les observations correspondantes; aucune attribution à une campagne ou acteur n'est étayée par les trois findings.

### Bridge

Les consultations de plan, YARA et stratégie de pivots apparaissent sous `advisor.consult`; leurs champs de conseil sont de la prose normalisée par l'application et l'opérateur ne les utilise pas comme preuve. La proposition YARA n'a fourni aucun `rule_source`. Pour l'avis, journal, journal opérateur et checkpoint décrivent `a228` sans texte Bridge exploitable ni `output_sha256`; l'auditeur n'a pas ouvert l'outbox de 1,9 Mo ni le payload de 6,58 millions de caractères, et n'a pas tenté de reparse. Le GET partiel `a230` ne change pas ce constat. La forme exacte des prompts n'étant pas dans le journal (seules leurs empreintes sont conservées), l'exigence « prose, sans demander de sérialisation machine » reste non vérifiable indépendamment.

### Isolation, relais et frictions

- La worker `CTI-OPERATOR-A-W1` a émis huit GET sur des routes hors catalogue (`/working-state`, `/article_claims`, `/openapi.json`, `/evidence`, `/documented_samples`, `/article_long_texts`, `/known_chain`). Tous ont retourné 403; aucun contenu n'a été obtenu par ces requêtes. Son rapport marque correctement rangs et alignements comme non évaluables. Elle a aussi fait quatre GET `/operator-status` à quelques secondes d'intervalle, contrairement à la cadence prescrite.
- Aucun accès réussi à un dossier hôte interdit n'apparaît dans les artefacts inspectés. Les journaux du relais n'instrumentent pas les lectures locales non journalisées : l'absence de trace n'en prouve donc pas l'absence.
- Le chef a utilisé des routes du catalogue autorisé. Les erreurs statiques (`sample.analyze` partial, outils .NET indisponibles, extraction archive échouée, parsing d'un XML par le mauvais outil) ont été suivies de pivots ou outils adaptés sans convertir l'échec en résultat négatif.
- Le GET d'observation binaire `a056` a répondu 415; il n'a pas été répété. Le GET du projet `a221` a répondu 404 et a été exclu des conclusions ultérieures.
- Le report MD5 `a156` et la résolution IPv4 `a177` ont été refusés avant tout appel fournisseur; l'opérateur a poursuivi les recherches SHA et les relations permises.
- La consultation d'avis `a228` a duré environ 55 minutes avant de finir `needs_review`. Elle n'a pas été rejouée. Le statut canonique indique des listes d'ambiguïtés/réconciliation vides, mais l'invocation individuelle reste en `needs_review` et une réservation est maintenue : il ne faut pas assimiler ces listes vides à une réconciliation complète.
- Le checkpoint mentionne une tentative de délégation rejetée par auto-review; ce fait est rapporté par l'opérateur, non corroboré par le journal du relais examiné. Aucun contournement n'est indiqué.

## État et budgets au statut final v113

SHA de l'état : `7fff517f192405b10e92fc3190bd25510f15dd603aea7114956ef3a8d8ded200`. Enquête `running`; G2 approuvé et non révoqué, TLP `Clear`, `external_llm_allowed=true`; aucun avis courant.

- Bridge : 3 consommés, 1 réservé sur 200. Usage fournisseur inconnu.
- VT : 72 consommés, 0 réservé sur 10 000.
- Téléchargements : 8 consommés, 0 réservé sur 1 000.
- Shodan : 0 consommé sur 1 000.
- Temps outil : 745 s consommées, 3 600 s réservées sur 86 400.
- Contexte : 186 627 tokens estimés consommés + 544 706 réservés; 746 502 octets consommés + 2 178 822 réservés.

L'invocation `a228` (`24554df0-65b6-4253-8514-2d6d3af0278d`) est le seul point encore retenu dans une réservation. Son résultat n'a ni coût fournisseur confirmé ni preuve de non-exécution. Aucun autre appel n'était en vol lors de la dernière lecture.

## Prochaine action permise

Réconcilier l'invocation exacte `a228` et sa réservation. Ne pas resoumettre tant que la soumission n'est pas prouvée non exécutée et la réservation libérée. Si une réponse Bridge complète est canonique, la reparser localement avec le parseur exact; sinon ne pas traiter la grande observation non fiable comme une réponse. Ne composer un avis qu'à partir d'un texte effectivement rédigé par Bridge, puis demander une relecture sur le texte et SHA exacts. Garder l'avis en brouillon. Aucun test logiciel n'a été lancé par cet auditeur.

## Artefacts d'audit

Les rapports précédents par lots sont `LOT_00_START.md`, `LOT_01_ACTIVITY.md`, `LOT_02_ACTIVITY.md`, `LOT_03_ACTIVITY.md` et `LOT_04_PENDING_BRIDGE.md` sous `observer/`. Leurs SHA-256 sont : `0d40ca1d0aa1f0b7e81df84ed4a6c242559f4248767522201e0008f709f1e88c`, `30531143b1d1677d29855ff3d910516ba4cbba3c52ffc9ffdaf7c690c35e022d`, `9aa90bf457f5d0cb9106d1e34dc6aeadd6c3e919a44775f6dc89d4826b018567`, `c65996dabb16b9a82620fa6ce936508eb12faffe7fcbdf61972a2a3bd64680a1` et `d060cea6058277c8250846af45e07871a0341deca62520ab5e5a5af255b847d2`. Le SHA-256 de ce rapport est communiqué dans le récapitulatif de livraison. Ces rapports ne sont pas des preuves CTI canoniques.
