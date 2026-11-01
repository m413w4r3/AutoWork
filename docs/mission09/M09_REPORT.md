# M09 — Rendre l'investigation CTI agentique : rapport

> Mise à jour finale : la **phase 2** (plafond 500 Ko, réconciliation par l'opérateur, fusion dans `main`, enquête indépendante avec deux sous-agents) est en §10. Les §1-9 décrivent la phase 1 (état au commit `14a5401d`).

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

## 10. Phase 2 — demandes de l'utilisateur et enquête indépendante

### 10.1 Demandes et résultat

| Demande | Résultat |
|---|---|
| Monter le plafond à 500 Ko | **Fait** : budget des pivots 24 Kio → 500 Kio, 128 → 2048 pivots (`41f01c93`, `ba0137c1`) ; test : 220 hashes non tronqués. |
| L'opérateur règle les `needs_review` | **Fait, limité aux lectures VT passives** (`vt.file_report`, `object_report`, `relationships`, `intelligence_search`, `behaviour`, `behaviour_pcap`, effet `EXTERNAL_READ` vérifié sur la définition enregistrée) : `020a4fbe`, durci par la revue (`72c65f71`). Tout autre outil reste réservé à un humain vérifié (422 `operator_reconciliation_not_permitted_for_tool`). Gardes serveur : refs ⊆ réponse persistée de CETTE invocation, `not_executed` refusé si une réponse est persistée, `PENDING`/`RUNNING` jamais réconciliables, `Idempotency-Key` obligatoire, provenance `operator_client` auditée. **Non exercé en réel** : les 5 invocations de M08 sont toujours `needs_review` (la vue les annonce réglables par l'opérateur : vérifié en lecture), et la nouvelle enquête n'en a produit aucune. |
| Fusionner | **Fait localement** : `main` = `6c1a03b0` (+ docs). **Non poussé.** |
| Enquête indépendante sur un nouveau run, deux sous-agents | **Faite en trois sessions** (R, R2, R3) ; voir §10.3. |

Déploiement : après chaque fusion, redéploiement via `safe_deploy.sh` (relais en pause, `RULE_WORKBENCH_ENABLED=true`, réseaux conservés) ; code live = `main` vérifié par somme de contrôle des sources à chaque fois ; Bridge 200.

### 10.2 Défauts révélés par l'enquête indépendante (corrigés, avec tests)

1. **Refus de politique sans cause** (`0d99cee9`) : `strings.list` renvoyait `tool_policy_denied` alors que l'outil connaissait la raison (échantillon non acquis). L'opérateur s'est arrêté au lieu d'acquérir. Correctif : `error.detail_code` publié s'il passe le filtre des codes sûrs ; test qui échoue sans correctif.
2. **Pivot « disponible » mais refusé à l'exécution** (`a07899b0`, `6c1a03b0`) : sur une enquête sans association ni observation, un hash ancré par des documents sources donnait des refs `[sample_hash, source_document…]` alors que la garde (`_invocation_hash_source_allowed`) exigeait exactement `[sample_hash]` → `virustotal_external_effect_refused_osint_policy_denied`. En M08 des associations existaient, ce qui masquait le défaut. Correctif : refs supplémentaires de kind `source_document`/`evidence_observation` acceptées si persistées == préparées ; contrôles de sujet/TLP RED/licence/observation inchangés. Revue indépendante : aucun contournement. Test PostgreSQL ajouté (échoue sans correctif ; fichier PG complet : 69 passés).

### 10.3 L'enquête indépendante (`519c2336-1fbb-4558-af6b-ea9c7519bf1a`, run `2d93d8b1`)

Nouveau run via `restart-with-new-sources` ; synthèse vérifiée de même contenu que M08 (SHA `8eb9cbe0…`) : l'entrée est identique, l'enquête est neuve, l'opérateur est isolé (aucun résultat antérieur fourni).

| Session | Requêtes relais | POST d'outils | Résultat |
|---|---:|---:|---|
| R (ancien backend) | 195 | 47 (21 ok, 22 `empty`, 4 `policy_denied`) | 4 cycles locaux, 0 VT/Bridge/téléchargement, arrêt prématuré (défaut 1) |
| R2 | 55 | 5 | 1 refus VT (défaut 2), arrêt, 0 sous-agent |
| R3 (backend corrigé) | 131 | 24 (21 ok, 3 `experiment_invalid`) | rapport VT réussi, 2 acquisitions, 4 cycles, 2 sous-agents |
| Sous-agent 035 | 70 | 15 (tous réussis) | `HTTPService.Init` dérive de `System.AppDomainManager` |
| Sous-agent 762 | 72 | 13 (tous réussis) | `Blackwood.Init` dérive de `System.AppDomainManager` |

**Sous-agents : réellement actifs** (journaux + requêtes du relais sous leurs propres labels, uniquement `assembly.inspect`, `types.list`, `methods.list`, `method.decompile`, `strings.list` : aucun outil interdit). Lancés par l'orchestrateur sur demande écrite de l'opérateur (deux questions indépendantes sur deux échantillons acquis) ; la voie « l'opérateur lance lui-même des sessions Paseo » n'a pas été testée.

**Résultats techniques (observations canoniques relues, statique uniquement)** : les deux échantillons (`0354862d…`, 23 040 o ; `76273382…`, 8 487 424 o) définissent un type utilisateur qui appelle `System.AppDomainManager::.ctor()` et surcharge `InitializeNewDomain` (obs. `177f1802…`, `5206d963…`, `9c64e2a6…`, `faf796e7…`). Aucun lien avec `RuntimeBroker.exe.config`, `etwEnable` ou ETW n'est démontré ; les chaînes sont dans un blob de 964 octets (XOR index/`0xAA`) dont les octets bruts ne sont pas exposés par les outils autorisés ; une assembly est chargée dynamiquement. Verdicts d'expérience `qualifies` ; claim `S2P2.1` **`inconclusive`** ; **0 finding formel** (l'opérateur n'a pas proposé de finding `supported` pour le fait atomique d'héritage, qu'il aurait pu qualifier).

**Vérifié contre l'état canonique** : v104, SHA `b4ab7f9b…`, G2 approuvé non révoqué, 0 `needs_review`, VT 1 unité (le seul `vt.file_report`), Bridge 0, `tool_seconds` 64 ; invocations nommées dans les checkpoints présentes.

### 10.4 Verdict : **PARTIAL** (maintenu)

Raisons : (1) l'enquête indépendante n'a **pas** reproduit le parcours de M08 d'emblée : deux sessions sur trois se sont arrêtées sur des défauts de plateforme, trouvés puis corrigés ; la reproductibilité « de bout en bout sans intervention » n'est pas démontrée. (2) Une fois les défauts corrigés, la boucle a fonctionné (hypothèses, candidats retenus/écartés, prédictions avant appel, acquisition ciblée, délégation à deux sous-agents réels, verdicts qualifiés), mais le résultat est limité : aucun finding, un claim toujours `inconclusive`. (3) La réconciliation par l'opérateur et la sous-délégation depuis l'opérateur ne sont pas exercées en réel. (4) Les rapports restent modestes en réduction d'incertitude : un fait statique nouveau (héritage `AppDomainManager`), pas de protocole, pas de règle.

### 10.5 Coûts et dette (phase 2)

Bridge 0 tour, VT 1 unité, 2 téléchargements (comptés 0 : engagements `committed_amounts` à 0, très probablement parce que les échantillons existaient déjà — `reused_existing_sample=true` — **non vérifié**), `tool_seconds` 64. Coût des sessions Paseo/Codex : **UNKNOWN**.
Frictions ouvertes (non corrigées) : `experiment.record` → `experiment_invalid` ×3 sans cause exposée (même schéma d'opacité que le défaut 1) ; 409 `stale_working_state` fréquents entre agents en parallèle (4 par sous-agent, 3 pour R3) car toute mutation avance le CAS global ; outils d'extraction statique sans accès aux octets bruts d'un champ RVA privé ; 38-39 échecs unitaires préexistants (Typst, `confined_analysis_dispatch`) identiques à la base ; `main` non poussé ; défaut mineur d'annonce de pivot pour une source restreinte (revue D4) ; enveloppe G2 dont la version avance sans décision (UNKNOWN).
