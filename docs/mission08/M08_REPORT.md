# Mission 08 — rapport de l'orchestrateur Paseo (2026-10-30 → 2026-10-31)

Rapport factuel de la mission « déblocage de l'investigation CTI, autonomie des opérateurs et supervision ». Les journaux bruts vivent dans `.autowork-runs/m08/` (ignoré par Git). Les agents de développement et les opérateurs étaient tous `codex/gpt-6-luna` en `xhigh` (modèle vérifié dans les métadonnées de session).

## 1. État livré

- Branche locale **`m08/integration`** (base `cee843f5`, 40 commits dont 25 hors fusions, 62 fichiers, +9144/−748). **`main` n'est pas touché et rien n'est poussé.**
- Déployé : empreinte `60958dbea6fe02d8` sur backend / worker / analysis-worker / job-recovery (Alembic `0007`, Ghidra sain, `RULE_WORKBENCH_ENABLED=true` passé par l'environnement du shell de `safe_deploy.sh`, pas par `.env`). Le HEAD actuel de la branche est `74ee3230`.
- Vérifications au HEAD : `ruff check`, `ruff format --check`, `mypy src` propres ; ~5 400 tests unitaires verts ; PostgreSQL (`tests/integration tests/workflow_emulator`) 504 passent, **3 échecs préexistants à `cee843f5`** (voir §5).

## 2. Lots de code (une branche `m08/*` par lot)

| Lot | Contenu |
|---|---|
| backend-fix | CAS : `expected_sha256` facultatif et renvoyé (`working_state_sha256`), erreur de CAS périmé avec la paire courante ; erreurs de schéma actionnables ; résumé de réconciliation d'invocation (statut réel, transport, réponse persistée, `next_action`) ; vue de métadonnées d'observation sans lire le blob ; 22 erreurs `ruff` corrigées |
| bridge-fix | Préflight YARA sans « mesure vérifiée » absurde ; réponses partielles utilisables exposées ; coupe-circuit local des refus identiques ; plafond heuristique de 4 000 caractères retiré ; nouvelle prose sans le mot interdit |
| tooling-fix | Comptage/exposition des secondes de `sample.analyze`, réutilisation des invocations locales répétées (`SUCCEEDED`), résumé d'outil visible avec les observations |
| cockpit | `GET /api/investigations/{id}/activity` (projection légère, pagination par curseur, heuristiques d'étapes 0–9 marquées comme telles) + vue « Investigation en cours » (en-tête, étapes, chronologie, blocages typés, coûts). Durées, opérateur et modèle sont `unknown` : AutoWork ne dispose pas de ces métadonnées |
| typecheck | 10 erreurs `mypy` et 14 fichiers à reformater |
| review-fixes | Revue critique indépendante (2 bloquants corrigés) : réconciliation évaluée sur **tout** l'historique des claims ; contrôle CAS avant tout règlement de réservation ; `PARTIAL` non rejoué ; `purpose` retiré de `/activity` (liste blanche) |
| availability-fix | Disponibilité d'outil alignée sur l'exécution : `advisor.consult` n'était plus annoncé (`provider_not_authorized`) et les 71 pivots VT étaient `policy_authorization_unverified` → les opérateurs ne pouvaient ni consulter Bridge ni faire de VT |
| download-grounding | Un sha256 documenté par l'article (IOC confirmé + document source) est ancré pour `vt.download_sample` ; refus trompeur `external_disclosure_revoked_before_dispatch` remplacé par `vt_hash_not_grounded` actionnable |
| dossier-autoload | Le serveur charge intégralement les corps des observations citées (plus de `NEEDS_RETRIEVAL` « RETRIEVABLE ») |
| dossier-required-selection | Les mesures « requises » ne sont plus écartées par notre propre sélection ; `measurement_shape_invalid` détaille les formes acceptées |
| units-preflight (+ r2 disclosable-selection) | Références imbriquées `units[].assertions[].refs` chargées par le serveur, cause du refus remontée par lot ; une observation non partageable non citée est omise avec raison, citée explicitement elle est refusée avant transport (le premier jet a cassé le garde OSINT sous PostgreSQL : détecté par l'oracle PG, corrigé avant déploiement) |
| provenance-nolimit | Plafond de 160 Kio sur la lecture de provenance Bridge supprimé |
| yara-prose-normalize / yara-unfenced / correctifs d'orchestrateur | Réponse Bridge « prose + règle » normalisée localement (règle validée, motifs dérivés des octets exacts des mesures) ; extraction tolérante quand l'interface perd les clôtures de code (libellé `yara` + backtick), règle contenant un backtick dans une chaîne, backtick d'ouverture seul sur sa ligne. Validé sur 3 vraies réponses Bridge |

Invariants conservés : G2/G3, TLP, `external_llm_allowed`, secrets AutoWork, identité d'objet, provenance, idempotence, révocations. Aucun nouveau moteur, formulaire d'approbation ni système d'audit.

## 3. Investigation

- Sujet `d22822d4-27ce-47d2-9cc5-f84cf312986a`. Une enquête par run (`get_for_run`) : nouveau run `90638910` par `restart-with-new-sources` (synthèse vérifiée **réutilisée**, SHA `8eb9cbe0…`), enquête **`5e90caa2-208c-4433-ba78-bf53ff41b6eb`** (`operator_led`, politique OSINT_OPEN héritée). Aucune enquête historique supprimée ni copiée.
- Opérateurs : A (arrêt sans effet externe : bug de disponibilité), B, C, D, E (continuité). Isolation par brief ; aucun résultat historique fourni.
- Résultats à la clôture (état v176) :
  - archive documentée réutilisée depuis la quarantaine, membres extraits (`archive.extract_member`), analyses statiques (ILSpy, FLOSS, capa, strings) de `RuntimeBroker.dll`, `PsProxy.dll`, `RuntimeBrokerApi.dll`, projet MSBuild et deux EXE candidats ;
  - rapports/relations VT réels après rétablissement du proxy ; données fournisseur, jamais des preuves d'exécution ;
  - 3 findings : 1 `supported` étroit (instructions MSBuild présentes dans le projet, pas d'exécution), 2 `qualified` ;
  - règle YARA `PsProxy_Specimen_Experimental_Candidate` (Bridge, normalisée localement) testée sur 10 fichiers réels non qualifiés : 1 correspondance sur le spécimen d'origine, 9 non-correspondances ; **non validée** (aucun positif qualifié, goodware, hard negative ni holdout ; sensibilité/FP non calculables) ;
  - avis `09322902-c5a6-59cf-81e9-65805c8d2adc` **v6 `draft`**, six blocs Bridge-authored avec références de rédaction et de relecture ; ni approuvé ni publié ;
  - aucun protocole réseau/crypto établi, aucune figure produite, dynamique indisponible (aucune VM prête).

## 4. Coûts (mesurés ou inconnus)

Bridge 35 tours consommés / 200, VT 19 unités / 10 000, téléchargements 0, Shodan 0, `tool_seconds` 5 586 s, contexte 8,2 M tokens / 32,8 Mo (valeurs du cockpit au 2026-10-31 ~16:30Z). Coût fournisseur Bridge : `usage_unavailable` (UNKNOWN). Tokens et coût des sessions Paseo/Codex : UNKNOWN (non exposés).

## 5. Dette et points ouverts (à ne pas masquer)

- **Tests PG rouges préexistants** (non corrigés) : W15 (sa fixture ne place le hash que dans le texte d'un claim, pas dans `confirmed_iocs`), W16 (un tour `FAILED` au lieu de `NEEDS_REVIEW`), `test_operator_context_cas_and_advisor_egress_guard_postgres` (le dossier auto-assemblé inclut une observation plantée avec URL signée → préflight secrets). W17 Typst : police `CascadiaCode` absente du bundle.
- **Reparse** d'une réponse Bridge persistée avant un changement de parseur (`stored_parser_version` nul) : `raw_advisory_text_available` sans nouvelle observation. Contournement : nouvelle consultation (normalisée à réception).
- **Cinq invocations `needs_review`** (VT ×4, PCAP ×1) : `53454898-ccbb-…`, `c925ad26-28d1-…`, `c86f18ec-01e8-…`, `a19bae8e-6512-…`, `0cf41437-84fc-…` — réconciliation par un humain vérifié requise ; ni rejouées ni réconciliées. Elles datent de la panne du proxy VT.
- **Risque** : le contrôle de secrets (`signed_url`, identifiants configurés) peut bloquer toutes les consultations Bridge d'une enquête si une observation CTI réelle contient une URL signée.
- Le U6 du premier jet v2 avait été écrit par l'opérateur ; en v6 les six blocs sont Bridge-authored (relecture Bridge `partial` globalement, six unités utilisables).
- Interface du cockpit vérifiée par tests (vitest/tsc/eslint/prettier) et par l'API réelle ; **non vérifiée dans un navigateur**.
- Sous-agents CTI : la voie fichier (`subagent_requests/`) a été fournie mais aucun opérateur ne l'a utilisée.

## 6. Infrastructure de l'utilisateur

Le proxy VT `195.154.30.203:3128` était injoignable (ping OK, tout TCP « No route to host », hôte et conteneur) ; **débloqué par l'utilisateur le 2026-10-31**. `.env` n'a été ni lu ni modifié.

## 7. Décisions réservées à l'utilisateur

1. Fusionner `m08/integration` dans `main` et/ou pousser (non fait).
2. Réconcilier les cinq invocations ambiguës (jeton humain).
3. Conserver ou arrêter les processus laissés actifs : relais `m08` (pid 498093) et ancien relais `m07c` (pid 87797), PostgreSQL de test sur 55432.
4. Valider/fournir des positifs qualifiés, un corpus goodware et un holdout pour la règle YARA.
5. Corriger les 3 tests PG préexistants ; décider du reparse rétroactif.
