# M14 — rapport final de préparation à la production

**Branche** `m14/final-production-readiness` (base `main` @ `684e6546`). **Rien n'est poussé, déployé ni fusionné dans `main`.** `make clean` n'a pas été lancé. Aucune recherche CTI live, aucune consultation Bridge réelle, aucune approbation ni publication n'a eu lieu.

**Verdict : PRÊT SOUS RÉSERVES pour une qualification, PAS validé pour la production.** Le code est corrigé, revu et testé hors ligne ; la qualification avec un opérateur neuf (§7.2 du mandat) n'a **pas** été exécutée (autorisation live, article et hashes manquants — voir §6).

Méthode : lots implémentés par des sessions Paseo GPT-6 Luna (xhigh) en worktrees isolés, relus par des sessions Luna distinctes en lecture seule ; **tous les tests ont été rejoués par l'orchestrateur** (les sandboxes Luna n'atteignent pas PostgreSQL). Le coût fournisseur est **UNKNOWN**.

## 1. Défauts M13 corrigés (vérifiés)

| Point du mandat | Cause racine vérifiée | Correctif | Preuve |
| --- | --- | --- | --- |
| 1.1 Transport Bridge avant/après émission | marqueur « soumission commencée » non mis à jour sur le chemin `ModelConversationTurnTransport` ; un 5xx après POST classé `pre_submission` si la phase manquait ; `UnsupportedProtocol` non reconnu sur le chemin coordonné | marqueur monotone avant le callback ; 5xx après POST = ambigu (`NEEDS_REVIEW / external_state_unknown`) + réconciliation GET lecture seule, jamais de 2ᵉ POST ; schémas non HTTP(S) refusés avant émission (lots A, A2) | tests unitaires (302 + 477) ; test M13 « 502 typé pre_submission » mis à jour : une affirmation nue de Bridge n'est pas une preuve de non-exécution |
| 1.1 clé d'idempotence en clair | `ProductionReconciliationView`, vue de statut opérateur (`last_operation.id`, préexistant), messages | empreinte `sha256:<16 hex>` partagée (`bridge_identity.py`) ; clé réelle conservée dans le `ModelRun` (lots A, A3) | tests canari |
| (trouvé par M14) réconciliation de production inopérante | depuis M13 les `error_details` ne portent que l'empreinte, et le résolveur s'en servait comme alias de recherche Bridge (toujours 404) ; `test_pipeline_recovery` rouge **sur la base** | une empreinte n'est jamais une clé de recherche ; clé réelle dérivée du ModelRun ; double de test représentant réellement Bridge (A3, `ff3c935f`) | `tests/integration/production` 82 passés |
| 1.2 citations VT par MD5/SHA-1 | `data.id` = MD5 demandé accepté ; observation `negative_result` acceptée ; `attributes.sha256` absent toléré ; `LOCK TABLE samples IN SHARE MODE` global | `data.id == attributes.sha256 == SHA-256 canonique` exigé, attribut md5/sha1 correspondant exigé, `kind=vt_file_report` ; `FOR UPDATE` sur le sujet et ses samples (lots B, B2) | 114 tests PG `external_effects` ×5 exécutions ; erreurs nommant l'observation (motif `=` autorisé dans une grammaire stricte, A3) |
| 1.3 réconciliation VT | lecture échouée réconciliée `executed_with_result` | issues distinctes (vérifiable / vide / transport / inconnu / indisponible) ; jamais `succeeded` sans résultat (lot A) | tests unitaires |
| 2.2 import YARA concurrent | prélecture périmée puis `append_version` retrouvant la ligne du gagnant sans signaler le rejeu | insertion atomique + relecture ; gagnant « nouveau », doublon « replayed », source divergente = conflit (lot C) | **base : échec 12/40 exécutions ; corrigé : 0/40** (3 tests PG) |
| 2.3 plusieurs règles Bridge | déjà acceptées (M13) ; fins de ligne CRLF/LF altérées | texte exact et SHA préservés (lot C) | tests |

## 2. Bridge — dossiers, échanges, rédaction

- **Octets de mesure (48 octets C09)** : cause = critère trop étroit (`source`/`blob_read_verified` exigés alors que l'archive porte `binary_analysis`/`binary_string` ou seulement la mesure). Les mesures d'outil sont jointes avec le statut fidèle `canonical_tool_observation / non revérifiée contre le blob` ; VT/externe jamais promu ; échec d'intégrité du blob = pas de valeur. **Le test d'archive réel C01–C13 passe** dans le checkout principal (corpus Run12). Limite de la revue : P3, l'aide `_canonical_tool_measurement` sans ligne d'observation fait confiance à des champs internes (non fournis par l'opérateur) ; resserrer sur `source_file` a cassé des mesures archivées, non retenu.
- **Sélection par pertinence (D, D2)** : échantillons historiques `subject_seed` non inclus par défaut, omissions observables (nombre, raison, ressource, suivi ciblé), mesures du dossier persistées (temps, lectures de blobs, octets, SHA). Benchmark hors ligne : **0 et 250 samples historiques → 0 lecture de blob, dossier < 10 Ko, prompt < 20 Ko**. Invariant G2 « réservation ≥ prompt mesuré, une seule construction » vérifié (12 tests + golden mis à jour, un défaut réel `Limitations:` vide corrigé).
- **Mesures réelles de latence Bridge : non faites** (aucune consultation autorisée).
- **Frictions locales (E1, E4)** : arguments facultatifs incorrects ignorés et signalés ; référence nommée dans la question et inexistante = erreur avec références utilisables ; observations VT `file` non citables retirées du dossier avec omission (`vt_report_not_archived_or_unverifiable`) au lieu de refuser toute la consultation ; garde de divulgation (TLP RED, `external_llm_allowed`, licence, révocation) inchangée. Pacing : déjà « attendre jusqu'à l'échéance », rien à changer. **Non tranché (décision utilisateur)** : F1 (réservation de contexte), TLP AMBER, filtre de secrets.
- **Parsing permissif (E1)** : réponses en prose, JSON partiel, 2 règles YARA, Suricata en bloc de code, pivots numérotés ; brut toujours conservé.
- **Skills (E1)** : six skills renforcent le cycle cadrage → analyse → critique → test → réévaluation, la rédaction par section via Bridge, la conduite si Bridge est indisponible (pas de quotas). Effet sur un opérateur réel : **non démontré**.
- **Rédaction par Bridge (E2, E3)** : un avis dont des sections n'ont pas de `draft_text` Bridge lié est signalé (résumé de couverture + pré-publication) et **l'approbation/publication exigent un accusé humain explicite** (`bridge_drafting_gap_acknowledged`, frontend inclus : avertissement + confirmation, jamais automatique) ; provenance (invocation, texte reçu, SHA Bridge **et** SHA final) conservée après adaptation ; linter de marqueurs internes sans suppression des réserves ; l'aperçu PDF ne modifie ni approbation ni publication ; le champ ajouté est accepté par le parseur strict du document de publication (régression trouvée par M14 et corrigée).

## 3. YARA et Suricata

Import exact + provenance + idempotence concurrente : voir §1. Rapport de tests : absence de mesure goodware explicitement signalée, intervalle non nul malgré 0 faux positif, un seul match = « non évalué comme règle de famille ». **Limite** : le modèle n'a pas de rôles canoniques distincts « variant confirmé » et « rôle inconnu » (non déduits, pas inventés). **Goodware : aucune baseline chargée, aucun binaire bénin fourni** → aucune validation goodware possible (les index yarGen sont des fréquences, pas des fichiers). `make goodware-*` non exécuté sur la base live.

## 4. Annexes (F, F2, F3, F4 ; F5 voir §7)

`backend/scripts/export_investigation_annex.py` → `manifest.json` + `manifest.sha256` déterministes, YARA (toutes versions, auteur/origine **dérivés de l'état canonique**, `not_recorded` sinon), Suricata (« non testée sur PCAP », jamais de replay inventé), IOC, inventaire samples, relations, hors-ligne depuis PostgreSQL + blobs vérifiés par SHA. Vue **publique** par défaut sans octets ; archive **restreinte** sur drapeau explicite et politique. Test PG de bout en bout (11 tests dont PG). **Défaut de sécurité réel trouvé par ce test et corrigé** : `samples.tlp` stocké en `RED` majuscules comparé à `'red'` → un échantillon TLP RED aurait été exportable. Revue indépendante : fuites de politique dans la vue publique (IOC, métadonnées, hash partagé entre sujets, règles) corrigées en F4.

## 5. Tests exécutés (par l'orchestrateur, Python 3.12, sauf mention)

| Contrôle | Base `684e6546` | Fin M14 |
| --- | --- | --- |
| Unitaires hors intégration | 39 échecs | **38 échecs, 5 964 passés** (aucun nouveau ; les 35 « Typst » passent avec le binaire épinglé `TYPST_BINARY=~/.local/share/autowork-tools/typst`, 3 `test_confined_analysis_dispatch` = environnement) |
| PostgreSQL (`tests/integration`) | 9 échecs | **à reprendre en §7** |
| Workflow emulator FAST | 20 passés, 2 échecs (W15, W16) | **20 passés, mêmes 2 échecs préexistants** |
| `ruff check` / `format` | — | propre |
| `mypy src tests` | 32 erreurs | 31–32 (aucune ligne modifiée) — l'écart avec la référence « 11 » de la mission est antérieur |
| Frontend (investigation + api), typecheck, lint | non lancé | **29 tests passés, typecheck et lint propres** |

## 6. Non fait / décisions utilisateur

1. **Qualification opérateur neuf (7.2/7.3) : non exécutée.** Prérequis manquants : autorisation live VT/Bridge, article + hashes de départ, déploiement autorisé. Protocole prêt : `M14_QUALIFICATION_PROTOCOL.md`.
2. **Archive et plan de base neuve** : archive faite et **vérifiée** (restauration PostgreSQL dans une base jetable : 105 tables aux comptages identiques ; 6 858 objets MinIO = 6 858 lignes `blobs`) dans `/home/nill/perso/work/AutoWork-backups/m14-pre-reset-20261103/` ; **restauration complète MinIO non testée**. Plan : `M14_RESET_PLAN.md` (option A recommandée ; le garde de `safe_local_reset.py` refuse volontairement — non contourné).
3. Décisions : fusion de la branche, déploiement, qualification live, F1, TLP AMBER, filtre de secrets, binaires bénins, route PCAP, reset.

## 7. Risques et points ouverts connus

- **P1 (revue finale, F)** : l'export public pouvait inclure la source de règles quand l'enveloppe est absente/révoquée — correctif fail-closed en cours (lot F5), à vérifier avant tout usage de l'export.
- **Préexistant** : un scénario d'intégration (`test_operator_context_cas_and_advisor_egress_guard_postgres`, 2ᵉ consultation « autorisée ») échoue **aussi sur la base** : des preuves non citées ajoutées automatiquement au dossier portent des secrets AutoWork et le préflight bloque toute la consultation. Piste : écarter ces preuves auto-ajoutées avec omission observable plutôt que refuser.
- **Plausible P2** : une révocation concurrente peut être validée entre la consommation et le handoff (la consommation est le point de linéarisation actuel).
- W15/W16 (reprise d'invocations ambiguës) toujours rouges comme sur la base.
- Un seul opérateur DeepSeek réel n'a pas été relancé : le gain de comportement dû aux skills est **non mesuré**.
