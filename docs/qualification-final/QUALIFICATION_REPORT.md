# M12-Q — Rapport de qualification opérationnelle des agents CTI

Dates : 2026-11-01 → 2026-11-02. Enquête : R3 (`519c2336-1fbb-4558-af6b-ea9c7519bf1a`, `operator_led`, G2 approuvé, TLP:CLEAR, `external_llm_allowed=true`).
Ce rapport est un journal d'orchestration : les preuves restent les observations canoniques de l'enquête. Les rapports d'opérateur ne sont pas des preuves.

## 1. Essais

| Essai | Opérateur (modèle réel) | Code déployé | Skills / playbook | Durée | État R3 en fin d'essai |
|---|---|---|---|---|---|
| attempt-001 | DeepSeek Flash 1M, thinking `max` (`deepseek-claude`), agent `83b66d59` | `main@7d5cb350` (PR #20), déployé le 2026-11-01 avec `RULE_WORKBENCH_ENABLED=false` hérité de `.env` | 9 skills génériques lus (trace vérifiée), playbook sans la ligne « POST en vol » | ~2 h 30 (dont 1 reprise neutre après fin de tour pendant un POST Bridge) | v224, 618 obs., 551 inv., avis v2 ; Bridge 6/200, VT 74/10000 |
| attempt-002 | DeepSeek Flash 1M, thinking `max`, agent `996bd9fd` | `main@0fd7176c` (PR #21), `RULE_WORKBENCH_ENABLED=true` | idem + ligne « POST en vol » | ~1 h 40, aucune intervention | v257, avis v4 ; Bridge 7/200, VT 79/10000, downloads 4/1000, tool_seconds 9508, contexte 11,8 M/16 M tokens |

« DeepSeek Max » = DeepSeek Flash 1M au niveau de réflexion `max` : seul palier exposé par le runtime. Sous-agents de correction : Luna (`gpt-6-luna`) xhigh, sessions Paseo isolées.
Limite d'indépendance : R3 contient les 28 findings et observations d'attempt-001 ; attempt-002 est indépendant par session (aucun rapport transmis), pas par corpus. Isolation de l'opérateur : consignes textuelles seulement ; aucune lecture hors périmètre observée dans les commandes.

## 2. Verdict indépendant par axe

| Axe | attempt-001 (orchestrateur) | attempt-002 (orchestrateur) | Auto-verdict opérateur 002 |
|---|---|---|---|
| A. Vérification d'affirmations | PASS (statique) | PASS (statique), plus complet | PASS |
| B. Invariants / découverte | PARTIAL faible : aucun hash inconnu ; 2 composants documentés réutilisés | PARTIAL : archive du leurre `6e7d9b33…` acquise et ses membres associés (config `0c9869b4…`, hôte `.NET` renommé `8875a41e…`, `RuntimeBrokerApi.dll`, Readme) ; aucun hash inconnu trouvé, VT passif indisponible (§4) | PARTIAL |
| C. Bridge conseiller actif | PARTIAL : 2 réponses reçues mais marquées inutilisables, usage tardif | PASS : réponse lisible, 2 règles utilisées, testées | PASS |
| D. YARA | FAIL mesuré (BLOCKED : workbench désactivé par mon déploiement) | PASS comme candidates (voir §3) | PASS (candidat) |
| E. C2 / Suricata | PARTIAL+ (modèle documenté ; Suricata non testée, aucune PCAP) | PARTIAL (idem, limite de données catégorie C) | PARTIAL |
| F. Avis | PASS brouillon v2 (non audité en détail) | PASS brouillon v4 (statut brouillon conservé) | PASS |

Global : **satisfaisant avec réserves**. Les capacités vérifier / détecter / reconstruire sont démontrées dans leur périmètre ; la découverte d'échantillons réellement inconnus et la validation Suricata ne le sont pas, pour des causes d'environnement et de données (voir §4 et §6), pas pour un défaut de raisonnement de l'opérateur.

## 3. Audit détaillé d'attempt-002

- **Articles** : le secours GitHub Issues dérive une clé AES de 16 octets (MD5) — AES-128-CBC et non AES-256 — vérifié par l'orchestrateur dans la décompilation canonique (attempt-001 et -002 concordent). Le mécanisme `GetFrameworkPaths`, `RuntimeBroker.exe.config` (`etwEnable`, AppDomainManager) était « non vérifié » en attempt-001 ; il est établi en 002 via l'archive du leurre. Association canonique, SHA et taille du membre `RuntimeBroker.exe.config` (551 o) vérifiés par l'orchestrateur ; le texte exact du XML n'a pas été relu par l'orchestrateur (contrôle partiel).
- **YARA (mesures relues par hash dans `rule.test`)** : 5 candidates, YARA 4.5.0, corpus 12 puis 14 fichiers, tous au rôle `new_candidate_unconfirmed` : **aucun goodware ni holdout n'existe**, donc « 0 faux positif » signifie « 0 match hors positif dans ce corpus » et non une validation de famille.
  - règle Blackwood (manuelle et variante issue de Bridge) : 1 match sur 12/14, `76273382…` seul ; les autres fichiers (HTTPService, Injector, etc.) ne matchent pas ;
  - règle du projet MSBuild piégé (manuelle et issue de Bridge) : 1 match, `f5b12772…` seul ;
  - règle « décodeur FieldRVA/Obfuscar » : 2 matchs (`0354862d…`, `76273382…`) — générique, correctement étiquetée non-famille.
  Les deux règles proposées par Bridge compilent sous YARA 4.5.0 (recompilées par l'orchestrateur) ; l'opérateur a dû les re-proposer avec provenance `manual` car `rule.propose` en provenance `bridge` a échoué (`rule_compile_check_failed`, §5).
- **Bridge** : première consultation à +32 min (même délai qu'en 001), 1 tour consommé, dossier de 16,5 Mo. Valeur ajoutée réelle : deux règles complètes (dont la détection du projet MSBuild piégé, nouvelle) et le rappel que YARA doit viser les octets stockés et non les chaînes déchiffrées. Latence mesurée grâce aux jalons de la PR #21 : réception du POST 08:14:22Z → démarrage de l'outil 08:38:18Z (24 min) → dossier prêt 09:05:18Z (27 min) → réponse Bridge 09:10:22Z (5 min) → persistance 09:10:34Z. **Environ 90 % de la latence est côté AutoWork, pas Bridge.**
- **Découverte vs base v224** : 0 hash inconnu ; acquisitions utiles = artefacts documentés (archive du leurre, projet, config, hôte renommé). Les lectures VT passives étaient indisponibles (§4).
- **Intégrité** : aucune exécution d'échantillon, aucun contact C2, aucun téléversement, aucune approbation/publication, aucun `make clean`. 7 invocations VT `needs_review` réconciliées par l'opérateur en `abandon` (état final sans ambiguïté).

## 4. Frictions, classification et corrections

| # | Friction (preuve) | Catégorie | Action | Statut |
|---|---|---|---|---|
| 1 | Workbench YARA désactivé après mon redéploiement (`RULE_WORKBENCH_ENABLED=false` repris de `.env`) | D (dont erreur de procédure de l'orchestrateur) | redéploiement avec la variable à `true` | corrigé, vérifié (`rule.*` dans les 61 outils) |
| 2 | Toute condition YARA contenant `or` rejetée → réponse Bridge complète marquée inutilisable | A | validateur par profondeur de parenthèses (`94f8dfe7`) | corrigé (PR #21) |
| 3 | Réponses Bridge en prose `usable=false`; reparse limité à ~4 Ko | A | `usable` = texte lisible ; `structured_usable`/`canonicalization_eligible` gardent la rigueur pour la promotion ; reparse intégral (`28dcad19`) | corrigé (PR #21), revue provenance PASS |
| 4 | Lectures VT passives `needs_review` (4 en 001 ; `transport_outcome_unknown` doublé en ~70 ms) | A + D | diagnostics d'étape + relance par nouvelle invocation (`3110b642`, `dfba917a`) ; en 002 le code d'étape montre `transport_exception_ConnectError` | diagnostic corrigé ; **cause racine : proxy VT `VIRUSTOTAL_PROXY_URL` injoignable** (« No route to host » depuis le conteneur backend, `www.virustotal.com:443` joignable) — décision d'infrastructure ouverte |
| 5 | Latence Bridge 42–56 min | A | jalons horaires additifs (`fa7fdbb0`) ; mesure : 24 min avant démarrage de l'outil + 27 min de construction du dossier | observabilité livrée ; **cause non corrigée** |
| 6 | Fin de tour de l'opérateur pendant un POST Bridge (001) | B | ligne de playbook (`3fcce620`) | corrigé, effet constaté en 002 (aucune fin de tour) |
| 7 | `rule.propose` provenance `bridge` → `rule_compile_check_failed` sans statut explicite, alors que les règles compilent | A | non traité | ouvert |
| 8 | `GET /operator-status` complet (~750 Ko) utilisé comme lecture de CAS ; le mode `view=compact` existe | B | non traité | ouvert (mineur) |
| 9 | CAS périmé (409) lors d'appels concurrents | opérateur | repris correctement par l'opérateur | sans action |

Sous-agents : Luna-A (validateur), Luna-B (réponses Bridge), Luna-C (diagnostic RO VT/latence), Luna-D (diagnostics VT), Luna-E (jalons), Luna-R (revue critique RO). Les trois premiers correctifs d'implémentation n'avaient exécuté aucun test dans leur sandbox (réseau absent) : l'orchestrateur a rejoué tests, lint et mypy (7 fichiers de tests, lint propre, ensemble d'erreurs mypy identique à `main`, 11) ; il a aussi corrigé un conflit de fusion et un test dépendant de l'ordre d'exécution. Seul échec persistant : `test_yara_draft_preserves_claims_and_sends_only_verified_offsets`, identique sur `main@7d5cb350`.
Revue critique : provenance PASS, secrets/logs PASS ; risques acceptés : la relance VT n'est pas liée à la route physique (atteignable seulement par mauvais usage interne de l'API du coordinateur) ; un `or` imbriqué sans `and` de premier niveau (boucles `for … :`) reste rejeté.

## 5. Régressions et limites des tests

Aucune régression observée sur les fichiers testés. Non exécutés : tests adossés à Postgres, VT et Bridge réels hors essais. Les tests Typst et d'analyse locale échouent dans un worktree sans bundle de polices ; ils passent dans le checkout principal.

## 6. Limites persistantes et conditions avant archivage de R3

1. **Découverte VT** : rétablir la route des lectures passives VT (proxy injoignable, ou décision de produit sur la route directe) puis rejouer un pivot ; sinon l'axe B reste PARTIAL.
2. **Latence Bridge** : réduire le délai de prise en charge du job (24 min) et la construction du dossier (27 min) avant tout usage interactif de Bridge.
3. **Suricata** : aucune PCAP ; règles non testées, bruit non mesuré (catégorie C, pas un défaut d'AutoWork).
4. **YARA** : candidates de spécimen ; aucun goodware/holdout dans le corpus ; aucune règle de famille validée ; `rule.propose` provenance `bridge` à diagnostiquer (§4 #7).
5. **Budgets R3** : contexte 11,8 M/16 M tokens et 47,3 M/67,1 M octets consommés ; un troisième essai sur R3 serait limité. 9 921 unités VT et 996 téléchargements restent disponibles.
6. **Relais** : le relais attempt-002 est resté actif sur R3 à la fin de l'essai ; aucune remise à zéro ni suppression n'a été effectuée (`make clean` non lancé).

## 7. Références

Branches/PR : PR #21 (`m12q/integration`, fusionnée, `0fd7176c`) ; `m12q/playbook-post-wait` (incluse). Journal de bord de l'orchestrateur : `.autowork-runs/qualification-final/REGISTRY.md` (non versionné). Traces d'essai : `.autowork-runs/qualification-final/attempt-00{1,2}/` (journal du relais, outbox, rapports d'opérateur).
