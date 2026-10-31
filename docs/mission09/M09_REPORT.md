# M09 — Rendre l'investigation CTI agentique : rapport

Base `main` `65c9c927`. Branche `m09/integration` (worktree `../AutoWork-m09-integ`), déployée sur la stack live avec l'autorisation explicite de l'utilisateur (déploiement uniquement ; **ni fusion dans `main` ni push**). Enquête de test : `5e90caa2-208c-4433-ba78-bf53ff41b6eb` (M08), non réinitialisée ; findings et brouillon d'avis (v6) intacts.

## Verdict : **PARTIAL**

Les huit critères d'acceptation sont satisfaits au minimum, chacun vérifié contre l'état canonique (§4). Je ne retiens pas PASS parce que :
- la réduction d'incertitude est réelle mais mince : aucun claim n'est passé à `supported`/`refuted`, aucun nouvel objet n'a été découvert, et le seul cycle exécuté sur le backend corrigé a retourné une relation VT vide ;
- les deux premiers cycles ont tourné sur l'**ancien** backend : le levier principal observé est le brief (boucle explicite + `hypothesis.declare`/`experiment.record` existants), pas le correctif backend ;
- la délégation à des sous-agents CTI n'a pas été exercée (aucun opérateur n'en a demandé) ; Bridge et YARA n'ont pas été utilisés (non justifiés par les observations) ;
- la reproductibilité sur une enquête neuve n'est pas testée (la mission la prévoit comme étape suivante).

## 1. Causes racines confirmées

Détail : `docs/mission09/M09_PHASE_A.md`. RC1 tri alphabétique + troncature (`investigation_pivots.py`) ; RC2 priorité de frontière sans hypothèse, hypothèses jamais déclarées (`hypotheses: []`) ; RC3 `next_idempotent_action` dominé par la plus ancienne `needs_review` (`investigation_operator_view.py`) ; RC4 prompt `bridge_led` « exécute les pivots pertinents » ; RC5 statut de 330 Ko.
Constat live non anticipé : c'est le **plafond de 24 Kio** (24 483/24 576 octets) qui coupait la liste. Les pivots se classant par hash d'abord, **aucun des 13 IPv4, 15 domaines, 6 URL** de l'inventaire n'était atteignable.

## 2. Modifications et commits

| Commit | Contenu |
|---|---|
| `b7b1f8fd` | Ordre des pivots : disponibles d'abord, indicateur lié à l'observation la plus récente, round-robin par indicateur avant troncature ; champs additifs `source_observation_ids`, `covered_by_tools` ; prompt `bridge_led` : sélection par question ouverte/coût/doublon, arrêt motivé autorisé |
| `b41fb4ae` | `reconciliation_tasks`, `next_analytic_action` (en `operator_led`, jamais remplacée par une réconciliation `needs_review`), drapeaux `blocks_other_tools:false`/`analytic_action_available:true` ; `GET operator-status?view=compact` (défaut = vue complète inchangée) |
| `1c687960` | Corrections de la revue indépendante : pas d'action de réconciliation proposée pour `PENDING`/`RUNNING` ; un pivot bloqué ne remplace jamais un pivot disponible omis à la troncature par octets |
| `e29d48db`, `d7a2cf74` | `M09_PHASE_A.md` (+ mesure RC1) ; `2f8d60fc` = fusion des deux branches |

Aucun contrôle G2/TLP/CAS/idempotence/budget n'a été modifié ; `bridge_led` garde son blocage global (`investigation_service.py:614`, `:2768`), hors périmètre.

## 3. Tests réellement exécutés

Les sessions Luna n'ont exécuté **aucun** test (pas de réseau). Je les ai relancés avec le venv du dépôt principal et `PYTHONPATH=<worktree>/backend/src` :
- D2 : 103 tests passés (`test_available_equals_executable`, `test_investigation_api`, `test_operator_tool_invocation`) ; D1 : 210 passés ; branche fusionnée + correctifs de revue : **242 passés** (pivots, `bridge_investigator`, `research_context_builder`, `available_equals_executable`, `operator_tool_invocation`, `investigation_api`).
- Test de non-régression de la troncature par octets : **échoue sans le correctif**, passe avec (vérifié).
- `mypy` (investigation_pivots, investigation_operator_view, investigation_contracts, api/investigation) et `ruff check/format` : propres.
- Suite unitaire hors `tests/integration` : **39 échecs, ensemble identique à la base `65c9c927`** (36 dans les fichiers Typst/figures/IOC/bande de détection, `test_confined_analysis_dispatch` ×3 ; causes non investiguées ici, M08 signale la police `CascadiaCode` absente) : préexistants, aucun introduit.
- **Non exécutés** : tests d'intégration PostgreSQL (`make test-integration`), frontend. Les 3 tests PG rouges préexistants de M08 restent non corrigés.
- Revue indépendante (Luna xhigh, lecture seule) : rejeu/idempotence OK, divulgation OK, déterminisme/pagination OK ; 2 défauts P2 corrigés (`1c687960`).

## 4. Cycles analytiques (vérifiés contre l'état canonique)

Opérateur 1 (`CTI-OPERATOR-M09`, ancien backend, v176→v187) — 87 requêtes, 11 POST outils, 1 lecture de statut :
- **Cycle 1** — question : les pages restantes de `references.imports` de `RuntimeBroker.dll` (SHA `53f35e49…`) contiennent-elles des références crypto compatibles avec `S3P1.1` ? H1 (marqueur crypto) / H2 (absence). Candidats : pages 200/400 retenues ; méthodes déjà décompilées, VT déjà couvert, `RuntimeBrokerApi.dll` (fichier `data`, métadonnées CLI absentes) écartés. Observation `e695aa72…` : `MD5.Create`, `SHA256.Create`, `Aes.Create`, `CreateDecryptor`, `CryptoStream` (recoupé : présents dans la page 400). H1 soutenue **comme capacité référencée seulement** (aucun call-site, clé/IV, ni exécution). `experiment.record` `4189b12b…` verdict `qualifies`.
- **Cycle 2** — la table complète de `PsProxy.dll` (SHA `3fd810a3…`) porte-t-elle les mêmes références ? Observation `12495e52…` (43 MemberRefs, complète) : `RunspaceFactory.CreateRunspace`, `PowerShell.Create`, `AddScript`, `AddCommand`, `Invoke` ; aucune `Aes`/`CreateDecryptor`/`CryptoStream` (recoupé). H3 réfutée dans le périmètre de la table, H4 soutenue. `experiment.record` `f1aa7995…` v2 `qualifies`. `S3P1.1`, `S3P2.1` restent `inconclusive`.

Opérateur 2 (`CTI-OPERATOR-M09B`, backend corrigé, v187→v192) — 37 requêtes, 5 POST, statut compact 10 696 o :
- **Cycle 3 (Exp. 2, pivot de découverte)** — question : la relation VT `communicating_files` de l'URL publiée `https://github.com/peakyblinders-tm` contient-elle un hash identique à l'un des 15 échantillons documentés ? Cinq candidats comparés, quatre écartés avec raison (autre opération `S4P5.1` ; `resolutions` non comparable par identité ; `intelligence_search` exact-hash peu discriminante ; relations RuntimeBroker : couverture existante + `needs_review`). Appel `vt.relationships` `2f129418…` : statut `empty`, `meta.count=0`, pagination complète ; 1 unité VT. H1 réfutée **sur cette relation**, H2 soutenue ; `experiment.record` `1e5175de…` `qualifies` ; `S5P3.1` reste `inconclusive`. Décision : arrêt de la piste, aucun téléchargement (aucun candidat).

## 5. Pivots retenus et écartés

Retenus : `references.imports` RuntimeBroker (pages 200/400), `references.imports` PsProxy (table complète), `vt.relationships` URL GitHub. Écartés (raisons consignées dans les journaux) : décompilations déjà persistées, VT `file_report`/`behaviour` déjà couverts, `vt.intelligence_search` exact-hash, relations RuntimeBroker (ambiguïté `needs_review`), URL/domaine `g-drive.cam` (opération distincte), `RuntimeBrokerApi.dll` (non CLI). Aucun pivot n'a été sélectionné par l'orchestrateur ; l'orchestrateur a écrit le brief (boucle et règles), pas le choix des pivots.

## 6. Résultats et limites établies

- RuntimeBroker référence l'API AES gérée ; PsProxy non, dans la table complète : **distinction statique**, pas de protocole ni de clé, pas d'exécution, pas de lien opérationnel démontré.
- Aucune relation VT `communicating_files` pour l'URL GitHub (fournisseur, au moment de la collecte, absence ≠ négatif). Aucune règle YARA retentée (pas de nouvelle chaîne/plage justifiant un essai ; la règle M08 reste non validée).

## 7. Coûts et répétitions mesurés (source : statut canonique + `journal.jsonl` du relais)

| Mesure | Valeur |
|---|---|
| Bridge | 35 → 35 (0 consultation) |
| VT | 19 → 20 unités (1 appel) ; téléchargements 0 ; Shodan 0 |
| `tool_seconds` | 5 586 → 5 594 |
| Appels analytiques informatifs | 3 `references.imports` + 1 `vt.relationships` ; redondants : 0 appel d'outil (1 GET d'observation répété) |
| Lectures de statut | op. 1 : 1 × 330 275 o ; op. 2 : 1 × 10 696 o (compact) |
| Refus / CAS d'état périmés | 0 / 0 ; 1 échec `stale_experiment_version` (CAS de flux deviné, corrigé après `experiment.list`) |
| Requêtes relais | op. 1 : 87 (tous HTTP 200) ; op. 2 : 37 (tous 200) ; orchestrateur : 18 GET de lecture |
| Coût fournisseur Bridge ; tokens/coût des sessions Paseo/Codex | **UNKNOWN** |

Comparaison avant/après (même enquête, mêmes données canoniques) : statut 330 Ko → 10,7 Ko (compact) ; `next_idempotent_action` = réconciliation humaine → `next_analytic_action` analytique + 5 `reconciliation_tasks` ; pivots IP/domaine/URL atteignables 0 → 19 ; ordre alphabétique par hash → disponibles d'abord avec couverture (`covered_by_tools` non vide sur 20 pivots). Ce n'est pas une comparaison de performance à tâche égale.

## 8. Blocages et dette restants

- **5 invocations `needs_review`** (VT×4, PCAP×1 : `53454898`, `c925ad26`, `c86f18ec`, `a19bae8e`, `0cf41437`) : réconciliation par un humain vérifié ; non rejouées, non réconciliées. Elles ne bloquent que leur outil+paramètres (vérifié sur 2 opérateurs).
- Le **plafond de 24 Kio** reste ; la liste est encore tronquée (52 pivots) et les 39 pivots `source_references_required` n'apparaissent plus. Aucune information sur le nombre de pivots omis. Les pivots IP/domaine/URL n'ont pas de `source_observation_ids` (provenance par documents sources).
- Les outils d'expérience exigent des `claim_ids` d'article ; un cycle sans claim correspondant n'est pas enregistrable (non rencontré, non testé).
- Message `stale_experiment_version` : le `stream_version` attendu n'est pas indiqué (friction constatée une fois).
- `bridge_led` : le blocage global par une invocation ambiguë est inchangé.
- La version d'enveloppe G2 affichée a avancé (351 → 383) pendant M09 sans décision d'approbation de ma part ; cause **non investiguée** (UNKNOWN), G2 reste approuvé, non révoqué, TLP clear.
- 39 échecs unitaires préexistants (Typst, `confined_analysis_dispatch`) ; 3 tests PG rouges de M08 ; intégration PG non relancée.
- Le checkout principal est en **HEAD détaché** sur le code déployé (la branche est prise par un worktree) ; `main` inchangé.
- Délégation Paseo : la création de sessions par l'orchestrateur fonctionne ; la voie opérateur → sous-agents n'a pas été exercée.

## 9. Décisions réservées à l'utilisateur

1. Fusionner `m09/integration` dans `main` / pousser (non fait). 2. Réconcilier les 5 invocations. 3. Lever ou remonter le plafond de 24 Kio, ou exposer le nombre de pivots omis. 4. Lancer une enquête indépendante sur un nouveau run pour vérifier la reproductibilité, avec test de délégation à 2 sous-agents. 5. Corriger les tests préexistants.
