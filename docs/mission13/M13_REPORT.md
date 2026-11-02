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
