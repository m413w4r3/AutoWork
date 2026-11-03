# M13 — rapport d'orchestration (PR #23, `bridge-simp/integration`)

**Statut : PARTIEL.** Parties I à VII traitées hors ligne et poussées ; partie VIII (qualification DeepSeek sur enquête vierge) **non exécutée**, bloquée par des autorisations et des données d'entrée (voir §7). Aucune validation de production n'est revendiquée.

HEAD de départ `674db1c6` (conforme à la PR) → HEAD poussé `d9dd7bf2`. Rien n'est fusionné, déployé ni réinitialisé ; `make clean` n'a pas été lancé.

## 1. Baseline CI et comparaison avec `main`

La CI de `main` (`e3f9c03d`) est **déjà rouge** sur quatre étapes, identiques à celles de la PR : `mypy src tests`, `Workflow emulator — FAST gate`, `Workflow J2`, `frontend pnpm test:e2e`. Le brief ne mentionnait que mypy. Frontend, J2 et leurs causes n'ont pas été investigués (hors chemins modifiés).

| Contrôle | `main` | PR au départ | PR au HEAD |
| --- | --- | --- | --- |
| `mypy src tests` | 11 erreurs | 20 | **11 (mêmes que `main`)** |
| Ruff check / format | — | — | OK |
| Unitaires hors intégration | 37 échecs, 2 skips | 37 échecs, 3 skips | **36 échecs, 2 skips** |
| Nouveaux échecs vs `main` | — | 1 | **0** |

- Les 9 erreurs mypy introduites (4 + 1 + 4) étaient confirmées. Corrigées sans `ignore`/`cast` : double `MemoryDetectionCandidates` complété (`append_version_idempotently`), `envelope` restreint par `assert`, `capture` typé `dict[str, list[dict[str, Any]]]` (`68a9d639`).
- Échec propre à la PR au premier passage : `test_w17_real_typst_draft_preview…` = **artefact d'environnement** (polices git-ignorées absentes du worktree). Passe (3/3) une fois les polices copiées ; non lié au code.
- Skip supplémentaire : `test_advisor_wire.py:329`, conditionné par le corpus Run12 archivé (git-ignoré, présent seulement dans le checkout principal). Exécuté via lien de lecture seule : **100 passed, 0 skip**. Ce test dépend donc d'un état local non versionné.
- Échec de `main` corrigé par la PR : `test_yara_draft_preserves_claims_and_sends_only_verified_offsets`.
- Les **36 échecs restants** sont ceux de `main` : rendus Typst réels (`test_typst_*`, `test_ioc_table`, `test_publication_detection_band`, `test_protocol_diagrams_typst`, `test_editorial_figures`) et `test_advisor_prompt_golden`. Cause Typst : environnement (binaire/polices, cf. mémoire projet), non examinée davantage. La cause de `test_advisor_prompt_golden` n'a **pas** été diagnostiquée : non pertinent pour ce chantier mais non prouvé inoffensif.
- **FAST gate** (PostgreSQL local de test) : 20 passés, 2 échecs **identiques sur `main`** : `test_w15_…download_finish_refusal…` (le transport simulé attend une association d'échantillon absente après `vt.download_sample` ; chemin `bridge_led`) et `test_w16_…orphan_after_model_unavailable…` (tour `FAILED` au lieu de `NEEDS_REVIEW`). Causes profondes non diagnostiquées ; elles concernent la reprise d'invocations ambiguës et méritent une session dédiée.
- Contrôles G2 conservés : tests de révocation avant/pendant l'archivage du dossier (`test_investigation_advisor_tools.py`) et W16 `g2_revocation…` passent.
- Budget : réservation estimée, ajustement, échec d'ajustement, persistance avant transport et invocation ambiguë sont couverts par des tests de la PR qui passent. Non rejoué : `RULE_WORKBENCH_ENABLED=true` en déploiement (la valeur par défaut du code est `true` ; l'environnement déployé n'a pas été inspecté).

## 2. Persistance idempotente (nouveau)

La PR n'avait aucun test PostgreSQL pour `append_version_idempotently`. Ajout de `tests/integration/test_detection_candidate_idempotent_import_postgres.py` (3 tests, passent) : rejeu séquentiel, deux imports concurrents identiques (un seul stocké, l'autre rejoue), deux imports concurrents divergents sur la même identité (un gagnant, un `VersionConflictError`). Voisins relancés : persistance (4), archive détection (1), corpus de règles (2), vertical (14), tous verts.

## 3. Changements architecturaux

Constat clé : le backend avait déjà des **checkpoints Bridge** (`operator_advisor_guidance`) avec trace automatique « consulted/skipped/denied ». Le défaut venait surtout de ce que les skills ne les enseignaient pas, et de trois lacunes réelles :

1. **Audit** (`53a7c444`) : l'export n'émettait que le SHA du `purpose`. Il expose désormais `operator_trace` par invocation (checkpoint, décision sur recommandation ; raisons hachées, texte libre toujours exclu comme avant).
2. **Disposition d'un conseil en prose** (`fed32a90`) : la trace de recommandation exigeait une entrée structurée `recommended_next_tools` liée à des observations, que Bridge en prose ne fournit pas. Une disposition sans `index` (`accepted`/`adapted`/`refused`/`deferred`, raison hors `accepted`) n'exige plus que l'existence d'un `advisor.consult` canonique de la même enquête. Le contrat indexé strict est inchangé.
3. **Déclencheur manquant** (`e6183068`) : `before_vt_pivot_campaign` → `pivot_strategy` une fois une règle mesurée.

Pas de nouvelle machine à états. Le journal de décision repose sur le `purpose` de chaque invocation (objectif, motif synthétique, lien au conseil), l'export d'audit et le flux d'expériences existants.

## 4. Skills et playbook (`d9dd7bf2`)

Nouveau `autowork-bridge-collaboration` (déclencheurs, agir sur les conseils, **format de trace enfin documenté**, rédaction sans marqueurs internes), relié depuis la boucle, YARA, pivots, protocole, outils, triage et le playbook ; le playbook n'impose plus de modèle opérateur. `stage_operator_cti_skills.py` stage dix guides. Un test (`test_operator_skills_bundle.py`) vérifie que l'exemple de trace est accepté par le vrai parseur, que les types de consultation nommés existent, que les champs de guidance existent, et qu'aucun hash, adresse, run historique ni modèle n'est codé en dur.

**Non démontré** : que ces consignes changent le comportement d'un opérateur réel. Aucune qualification n'a eu lieu.

## 5. Capacités réseau, VT, laboratoire dynamique

Déjà présents : `vt.behaviour_pcap`, `network.capture.inspect`, `network.stream.reassemble`, `network.messages.segment`, `network.protocol.*`, `network.suricata_validate_rules/replay`. Pas de laboratoire dynamique. **Proposition (non implémentée, non autorisée)** : VM d'analyse jetable sur un hôte dédié, sans route vers le réseau de production, sorties bloquées ou simulées, PCAP + journaux capturés par l'hyperviseur, snapshot/restauration, import de la capture comme observation avec SHA et provenance, aucune exécution depuis l'hôte AutoWork. Chaque exécution exigerait une autorisation spécifique.

## 6. Mesures de latence Bridge

**Non mesurées en réel** (aucune consultation autorisée). Le test de caractérisation `test_m12_q_reservation_and_execution_characterization` mesure hors ligne la réservation et l'exécution avec transport simulé ; ses valeurs n'ont pas été relevées ici et ne représentent pas la latence de production.

## 7. Partie VIII : pourquoi la qualification n'a pas été lancée

- **Disponible** : provider `deepseek-claude`/`deepseek-codex` (profil « Orchestrator DeepSeek », `deepseek-flash[1m]`, thinking `max`). Il reste à confirmer que c'est bien « DeepSeek Max » voulu.
- **Manque** : l'article de départ et les hashes initiaux de l'enquête vierge (non fournis) ; le déploiement de la branche sur la stack qui chargera les skills et le code corrigés (opération à autoriser) ; l'autorisation séparée, exigée par `AGENTS.md`, de la recherche CTI live (VT, Bridge) et de la création de l'enquête dans la base canonique. Rien de tout cela n'a été supposé acquis.
- **Protocole prévu** une fois débloqué : enquête neuve (nouveau sujet/édition), nouveau workspace, `stage_operator_cti_skills.py` puis vérification de la lecture effective par le runtime, mandat métier naturel sans plan ni audit antérieur, corpus initial enregistré côté orchestrateur pour juger la découverte sans l'indiquer à l'opérateur, audit indépendant (Luna xhigh en lecture seule) classant plateforme / comportement / limite de connaissance, puis correction et nouvelle session propre.

## 8. Préparation du futur reset (plan à soumettre, rien exécuté)

1. Archiver `var/diagnostics`, exports d'audit (`write_audit_bundle`), rapports et traces `.autowork-runs`.
2. Sauvegarder PostgreSQL (dump), buckets MinIO et volumes (`postgres_data`, `redis_data`, `minio_data`).
3. Vérifier clôture des relais et invocations (aucune `needs_review`/`transport_unknown`).
4. **Blocage de conception** : `safe_local_reset.py` refuse dès qu'une enveloppe G2 ou une invocation existe, et `--confirm-initial-reset` ne lève pas cette garde. Une base ayant servi à une enquête supervisée ne peut donc pas être remise à zéro par ce script ; il faudra une décision humaine explicite (autre procédure ou environnement neuf).
5. Périmètre et conséquences à valider par l'humain avant toute exécution.

## 9. Verdict de fusion de la PR #23

**Non recommandée à ce stade** pour une mise en production. Techniquement : aucune nouvelle régression vérifiée sur les chemins modifiés, mypy à parité avec `main`, PostgreSQL idempotence testée. Réserves : CI toujours rouge comme `main` (4 étapes, dont frontend et J2 non examinés) ; W15/W16 et 36 unitaires préexistants non résolus ; comportement opérateur non qualifié ; `RULE_WORKBENCH_ENABLED` et déploiement non vérifiés ; latence réelle non mesurée. La fusion et le déploiement requièrent une décision humaine.

---

# Mise à jour — qualification DeepSeek et audit (cycle 2, 2026-11-02)

## 10. Journal de qualification (DeepSeek Flash 1M, thinking `max`, enquête vierge)

Autorisations reçues de l'utilisateur : déploiements nécessaires, recherches VT et consultations Bridge autorisées et obligatoires, arrêt de l'opérateur avec signalement si VT ou Bridge échouent, partir d'une analyse vierge.

| Essai | Code déployé | Enquête | Résultat |
| --- | --- | --- | --- |
| 1 | `c44bcc3e` | `b68f65c5…` (run `fcc7abc1`) | **Arrêté par l'orchestrateur** : première consultation Bridge refusée avant envoi (`osint_policy_denied`). Défaut de plateforme, VT fonctionnait (15 rapports + 1 téléchargement). |
| 2 | `619d0f10` | `62be6fcf…` (run `007a0760`) | Terminé : avis brouillon v4, 14 findings canoniques, 5 règles YARA candidates. Audit indépendant : **C**. |

**Défaut de l'essai 1 (corrigé).** Une enquête vierge ne contient que des rapports VT sans échantillon associé ; le garde de divulgation Bridge exigeait un échantillon associé et l'exception ne couvrait que `vt.intelligence_search`. Correctif `e4ce6aa8` puis `619d0f10` : pour Bridge uniquement, une observation `source == "VirusTotal"` n'exige plus d'association, mais reste soumise aux restrictions (TLP RED, licence, veto LLM) de tout échantillon que le sujet détient par digest, `expected_hash` ou `source_object_id`. Deux revues Luna xhigh ont fait échouer la première version (provenance irréaliste dans les fixtures) ; la seconde ferme ces points. Les tests PostgreSQL de chaque garde échouent quand la partie correspondante du correctif est retirée.

**Mesures de l'essai 2 (état canonique, non récit de l'opérateur).**

- Durée 19:38 → 20:36 (≈58 min), 167 invocations : 114 réussies, 26 échecs (dont erreurs de validation d'arguments), 17 partielles, 5 indisponibles, 3 vides, 2 refus de politique.
- Bridge : 5 consultations, première à **+2 min** (essais M12-Q : +33 min) : `plan_investigation` (4 min), `yara_draft` (9 min), `draft_text` (6 min), `review_text` (6 min), plus un échec de validation côté opérateur. Aucun `pivot_strategy`.
- Dossiers Bridge : 29 Ko / 389 ms de construction (plan) ; **23,9 Mo / 79 s** (YARA) ; 2 Mo / 61 s (revue). Le M12-Q avait mesuré ≈27 min pour un dossier sur une enquête bien plus chargée : comparaison indicative, pas à charge égale.
- VT : 21 `file_report`, 12 `download_sample`, 5 `relationships`, 3 `behaviour`, 1 `object_report`, 1 `behaviour_pcap` en échec ; **0 `intelligence_search`**.
- Échantillons associés : 12 = 6 du corpus initial, 3 déjà connus de la plateforme (pool de 240 hérité du sujet), **3 réellement nouveaux** : un conteneur (`Client.zip`), un hôte légitime signé (`vshost32.exe`, 0/62 VT) et sa configuration. Pas de variante : découverte faible.
- Traçabilité : 156 invocations portent un checkpoint (5 `consulted`, **150 `skipped`**) ; 2 dispositions de recommandation (`accepted`).
- Détection : 5 règles YARA candidates compilées (YARA 4.5.0), 1 match et 11 non-matches sur 12 fichiers pour chacune ; aucun goodware ni holdout, donc non validées.

## 11. Audit indépendant (Luna xhigh, lecture seule) — verdict C

Constats vérifiés par l'auditeur : le rapport est utile et nuancé, mais (a) 32 capacités annoncées contre 37 dans l'observation capa ; (b) la configuration Blackwood « déchiffrée » est `supported` alors que le déchiffrement a eu lieu hors enquête, sans observation de sortie ; (c) la classification VT de `vshost32.exe` cite une observation qui n'est pas le rapport VT ; (d) retests YARA sur reconstructions non tracés ; (e) critères des pivots VT non reconstructibles ; (f) bloc `implant_materials` vu en JSON brut dans l'export (c'est la représentation typée rendue ensuite par la publication, à confirmer au rendu).

### Classement

| Catégorie | Défaut | État |
| --- | --- | --- |
| Plateforme | Consultation Bridge refusée en enquête vierge | Corrigé (`619d0f10`), déployé |
| Plateforme | `draft_text` : brouillon exploitable déclaré `usable=false` (manque de liens de preuve, facultatifs) ⇒ référence Bridge refusée dans l'avis, provenance perdue | Corrigé (`82e059fe`), **non déployé** |
| Plateforme | Réponse Bridge à plusieurs règles YARA non canonisable (`exactly_one_rule_required`) | Corrigé (`cec56215`), **non déployé** |
| Plateforme | Checkpoint `first_significant_decompilation` dû pendant 40 min, masquant les suivants | Corrigé (`8b63991e`), non déployé |
| Plateforme | Export d'audit : 95/167 outils `UNKNOWN`, objectif et paramètres des pivots absents | Corrigé (`8b63991e`) : noms lisibles + option `include_operator_text` (désactivée par défaut) |
| Plateforme | Message d'erreur opaque si l'enveloppe d'appel est imbriquée dans `arguments` | Corrigé (`8b63991e`) |
| Plateforme | Lecture PCAP VT impossible : route proxy seule, tout 3xx est une erreur, CDN `vtbehaviour…` non autorisé ; import officiel refuse un PCAP déjà détenu | **Ouvert, décision requise** (routage deny-by-default) |
| Comportement | Valeurs dérivées hors outil citées en `supported` ; chiffres retapés | Skills corrigés (`391bfa49`), à requalifier |
| Comportement | Aucun pivot par motif (`intelligence_search`), aucun `pivot_strategy` malgré `new_pivot_discovered` | Skills + checkpoint corrigés, à requalifier |
| Limite réelle | Pas de PCAP exploitable, charge `RuntimeBrokerApi.dll` non déchiffrée, pas de goodware/holdout | Non corrigeable |

### Reste ouvert (préexistant, non aggravé)

- Résultats Suricata dérivés d'un PCAP VT restreint pouvant entrer dans un dossier Bridge (observation `capture_sha256` non reliée au sample) ; observations MD5/SHA1 citées seules à Bridge non contrôlées ; révocation concurrente après le point de consommation.
- Instruments : `compact` de `/operator-status` n'expose pas `advisor_guidance`.
- Pool de 240 échantillons hérité du sujet : octets réutilisables sans téléchargement (fuite d'octets, pas de conclusions).

## 12. Tests de ce cycle

Unitaire hors intégration : **36 échecs, tous ceux de `main`** (aucun nouveau), 5 821 passés, 2 skips (Ghidra, image d'analyse). PostgreSQL : effets externes 87, import idempotent 3, vertical 14, persistance 4 passés. FAST gate : mêmes 2 échecs que `main` (W15, W16). `test_analyst_opinion_publication_postgres` échoue identiquement sur `main` (même cause que W16). Typage : 11 erreurs, identiques à `main`.

## 13. Non réalisé

(Cycle 2.) La troisième qualification a eu lieu : voir §15. Aucune validation de production n'est revendiquée.

## 14. État de la fusion

PR #23 **non fusionnée** : la fusion demandée par l'utilisateur a été refusée par le contrôle de permissions de l'environnement (CI rouge, pas de revue humaine) et n'a pas été contournée. Les échecs CI sont ceux de `main` (§1 et §12). Décision utilisateur requise.

---

# Cycle 3 — essai 3 et audit (2026-11-03)

## 15. Essai 3 (code déployé `0bdc0f8b`, `RULE_WORKBENCH_ENABLED=true` vérifié dans backend et worker)

Enquête vierge `3c63049f…` (run `7b53c852`), opérateur DeepSeek Flash 1M thinking `max`, prompt de trois phrases sans plan ni résultat antérieur. **Contamination à noter** : j'ai arrêté un premier opérateur trop tôt (après un refus de contexte, alors qu'il venait de se corriger) ; le second a repris le même espace de travail et lu ses notes.

Faits (état canonique, export d'audit avec lecteur d'artefacts et `include_operator_text`, audit Luna xhigh en lecture seule : `AUDIT_REPORT.md` hors dépôt, SHA-256 `059d972a…`) :
- 8 consultations Bridge, 3 réponses persistées (`partial`). Échecs : 1 erreur de référence de l'opérateur ; 3 refus `advisor_context_reservation_mismatch` (dossier mesuré ≈173 Mo / 43 M tokens contre ≈16 M réservés, 245 échantillons dont le pool hérité du sujet ; 0 tour consommé, mais 3 à 5 min de construction) ; 1 `draft_text` (dossier 1,24 Mo) en `bridge_server_error` classé `pre_submission` / `confirmed_non_execution` alors que le journal Bridge montre l'entrée en phase `submission` sans fin (`resp_3a49a257`).
- Cause du gonflement (mesurée) : les observations `investigation.subject_sample_reuse` (2 × 49 Mo, 5 × 8 Mo…) et la jonction en base64 de tous les échantillons associés pour tout type de consultation hors `draft_text`/`review_text`/`suricata_draft`/`figure_plan` (`investigation_advisor_tools.py` ≈l.4400), plus la réinjection d'un échantillon entier quand son observation d'octets est citée (≈l.4449).
- Avis brouillon `29eb7a0f` v1, 7 blocs, sans `bridge_draft_ref` ni revue Bridge ; 5 règles YARA candidates, sans goodware ni holdout ; 2 `vt.intelligence_search` (un vide, un réussi), 24 téléchargements VT ; plan et critique de règles Bridge exploités, un import Bridge de règle YARA échoué (`bridge_yara_observation_not_found`, prédicat exact non exporté).
- Checkpoints : 321 traces `skipped` sur 329 (export final) ; `first_significant_decompilation` apparaît dans 189 traces. Les chiffres 273/280 et 157 que j'avais relevés en cours d'essai sont des mesures intermédiaires ; la raison libre n'est pas exportée (SHA seulement).
- Invocations : 87 `unavailable` (dont 84 `method.decompile`), 30 `failed`.

Note de qualité de l'audit : **C**, même palier que l'essai 2.

### Classement (audit + mesures de l'orchestrateur)
| Catégorie | Gravité | Défaut | État |
| --- | --- | --- | --- |
| Plateforme | Haute | `draft_text` classé `pre_submission` alors que Bridge est entré en soumission ; run sans état terminal | **Ouvert** |
| Plateforme | Haute | Dossiers gonflés par les échantillons entiers et le pool hérité ; refus sans liste des éléments lourds | Chantier `m13/dossier-relevance` (commit local `b34a91e3`, **2 tests rouges, correction en cours, non revu, non déployé**) |
| Plateforme | Moyenne | Un checkpoint ignoré reste dû (`operator_advisor_guidance.py:317-339`, `operator_tool_invocation.py:1411-1431`) | **Ouvert** |
| Plateforme | Moyenne | Import d'une réponse YARA Bridge : prédicat échoué non exposé | **Ouvert** (ne pas retirer la garde de provenance) |
| Plateforme | Moyenne | Audit : sorties, causes d'erreur, ids ModelRun/run Bridge absents ; raison de checkpoint hachée | **Ouvert** |
| Plateforme / environnement | Moyenne | `strings`/`monodis` indisponibles, pagination `methods.list`, extraction `.config` | **Ouvert** |
| Comportement | Moyenne | Poursuite malgré le checkpoint ; avis écrit sans Bridge | À requalifier après correctifs |
| Limite réelle | Moyenne | Pas de goodware/holdout/négatif difficile ; associations `candidate` | Non corrigeable ici |

Aucune validation de production. Fusion de la PR #23, route PCAP, plan de reset et mise en production : décisions de l'utilisateur.

---

# Cycle 4 — dossier ciblé, essais 4 et 5 (2026-11-03)

## 16. Chantier « dossier Bridge par pertinence » (branche `m13/dossier-relevance`, fusionnée dans `bridge-simp/integration` @ `f1b32477`)

Principe demandé par l'utilisateur : ne plus envoyer à Bridge les échantillons entiers ; lui envoyer le désassemblé/décompilé pertinent, les observations pertinentes et des octets ciblés. Pas de plafond d'octets (`backend/AGENTS.md`) : sélection par pertinence, omissions observables avec raison.

- Échantillons : provenance seule par défaut pour tous les types de consultation ; octets uniquement par plage explicite vérifiée (SHA-256 du blob complet calculé en flux par blocs de 1 Mio, mémoire indépendante de la taille du blob). Observations `investigation.subject_sample_reuse` jamais rendues en contenu. Une plage explicite couvrant tout le blob reste envoyée (octet demandé) avec `covers_whole_sample=true` (union d'intervalles par blob) ; plage vide omise avec `empty_blob_range`.
- Réservation G2 == mesure du prompt sélectionné ; un refus liste les éléments les plus lourds.
- Course de divulgation (préexistante à `0bdc0f8b`) : la restriction d'échantillon (TLP RED, `external_llm_allowed=false`, révocation) est revérifiée dans la porte d'envoi, après le pacing et juste avant le transport (`bridge_investigator.py`, `investigation_advisor_tools.py`).
- Trois revues Luna xhigh indépendantes ; la dernière a rendu FAIL sur 3 défauts du marqueur d'audit (pas de fuite), corrigés ensuite (`9f6a3b06`) **sans quatrième revue complète** (choix de l'orchestrateur). Les gardes elles-mêmes (`_sample_disclosure_restriction_sources`) n'ont pas été modifiées.
- Vérifié par l'orchestrateur sur `9f6a3b06` : unitaires 5 837 passés / 39 échecs identiques à la base (le test golden `advisor_prompt` échoue à la base et passe maintenant car ses données attendues ont été modifiées par le chantier), PostgreSQL 129 passés, ruff propre ; mypy et gate FAST non rejoués.
- Mesure réelle (essai 4) : dossier `plan_investigation` de 29 Ko (0,4 s) contre 173 Mo refusés en essai 3.

## 17. Essai 4 (arrêté) et essai 5

- Essai 4 (`df30d96b`) : arrêté par l'orchestrateur, les 3 lectures VT ayant échoué en `transport_exception_ConnectError` (proxy VT injoignable). L'opérateur avait réconcilié ces lectures en `executed_with_result` sans données : elles apparaissent `succeeded` en base (état trompeur, défaut de plateforme).
- Essai 5 (`2143a45c`, run `e55a2661`, opérateur `990ae52b`, espace neuf, VT rétabli par l'utilisateur) : 8 findings (7 supported, 1 qualified), 7 règles YARA candidates (3 avec matches), avis brouillon v2 sans Bridge. VT : 20 `file_report`, 11 téléchargements, 5 `relationships`, 1 `intelligence_search`. Bridge : plan OK ; deux `draft_text` refusés avant envoi (`coordinator_consume_refused / unresolvable_evidence_observation_source`, 0 tour consommé).
- Audit Luna xhigh (SHA-256 du rapport `931f4f04…`) : note **D** (essai 3 : C). Cause du refus : 4 observations VT `file` indexées par MD5 sélectionnées dans les dossiers ; le coordinateur n'accepte que du SHA-256 pour Bridge (`external_effects.py:~1617`) ; **préexistant à `0bdc0f8b`**, pas une régression du chantier. Un assouplissement MD5 risquerait de contourner les contrôles d'association/divulgation : correctif à soumettre à revue.
- Dossiers `draft_text` encore ≈4 Mo et 73–90 Mo de blobs lus : observations des findings et méthodes décompilées complètes, sélection à resserrer.
- Comportement : 215 traces `skipped` / 3 `consulted`, une seule consultation a atteint Bridge, aucun échange YARA, `first_significant_decompilation` toujours dû. Valeurs RC4/IP/port/clé non étayées par des observations de sortie dans l'export.
- Goodware : **aucune baseline importée** (tables vides, cache vide) ; `rule.test` n'a ni sensibilité ni faux positifs mesurables. Sources signalées par l'utilisateur : `/home/nill/tools/yarGen/dbs` (good-exports/imphashes/opcodes/strings). Import en base canonique : à autoriser explicitement.

### Défauts ouverts (plateforme)
| Gravité | Défaut |
| --- | --- |
| Haute | Garde de citation Bridge : observation VT `file` par MD5 non citable (refus opaque, observation fautive non nommée) |
| Haute | `draft_text` classé `pre_submission` / non soumis alors que Bridge est entré en soumission (`resp_3a49a257` sans fin) |
| Moyenne | Réconciliation VT permettant `executed_with_result` sans donnée : état `succeeded` trompeur |
| Moyenne | Checkpoint ignoré (`skipped`) toujours dû ; import d'une réponse YARA Bridge sans prédicat exposé ; export d'audit incomplet |
| Moyenne | Dossiers de rédaction trop gros (findings → observations/méthodes) |
| Limite | Aucun goodware/holdout |

Aucune validation de production. Fusion de la PR #23, route PCAP, import goodware, plan de reset, mise en production : décisions de l'utilisateur.
