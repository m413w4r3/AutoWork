# État de la phase 01 — correction structurelle (2026-10-19)

Orchestrateur : session Claude Code `417a8e01-27cc-4bc7-8329-30b87e5048a9`. Mandat : `nimbalyst-local/reference/AutoWork_PROMPT_01_CORRECTION.md`
+ `AutoWork_AUDIT_2026-10-19.md`. Journal des lots : `nimbalyst-local/orchestration-2026-10-19.md`. Sous-sessions : `openai-codex:gpt-6-luna`, `xhigh`.

## 1. Git et environnement
- HEAD `6dc3f28` (identique à GitHub `main` au départ). Arbre propre au départ ; **rien n'est committé** : ~99 fichiers modifiés/nouveaux (aucun commit demandé).
- Stack redéployée avec `make up` (accord utilisateur, sans reset ni `down -v`) après contrôle : aucun job actif (jobs : 273 succeeded / 10 failed / 2 cancelled). Tous les services `healthy` ; `ilspycmd 9.1.0.7988` présent dans le worker déployé, `DOTNET_ANALYSIS_ADDRESS_SPACE_LIMIT_BYTES=8589934592`. `.env` sauvegardé (`/tmp/.env.pre-c1c10-*`) puis complété avec `DOTNET_ANALYSIS_ADDRESS_SPACE_LIMIT_BYTES` et `OPERATOR_ADVISOR_*` (valeurs par défaut explicites, aucun secret affiché).
- DB live : `alembic_version = 0001_baseline`, **aucune DDL appliquée** (non autorisée) ; la table `investigation_experiments` (C2) n'existe donc pas en live : les outils `experiment.record|list` et `GET …/experiments` sont déployés mais **non utilisables** tant que la DDL n'est pas appliquée. Investigation 12 `5f66ebfb-…` : `running`, non touchée.
- `.env` pour le prochain run : budgets existants conservés (`INVESTIGATION_MAX_CYCLES=30`, `…HITS_ACQUIRED=20`, `…NEW_SAMPLES=20`, `…VT_READ_UNITS=500`, `…SHODAN_CREDITS=100`), fournisseurs VT/Shodan activés, `ANALYSIS_WORKER_ENABLED`/`GHIDRA_ENABLED`/`LOCAL_ANALYSIS_ENABLED`/`RULE_WORKBENCH_ENABLED` = true, `LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT=false` (valeur héritée, le confinement est assuré par le worker dédié), `NATIVE_ANALYSIS_ENABLED=false` (REA indisponible), `COMPOSE_PROFILES=ghidra`. Les clés VT/Shodan et jetons opérateur sont présents (jamais affichés).
- Environnement de test : le `typst` snap ne fonctionne pas depuis le shell de l'agent (`snap-confine`) ; un binaire autonome extrait est utilisé : `PATH=$HOME/audit-tmp/c8/bin:$PATH TYPST_BINARY=$HOME/audit-tmp/c8/bin/typst TMPDIR=$HOME/audit-tmp`.
- Incident : les sous-sessions Codex ont été indisponibles ~15 min (`401 Unauthorized`, jeton OpenAI absent) ; réauthentifié par l'utilisateur ; aucun autre modèle n'a été utilisé.

## 2. Validation exécutée (résultats exacts, état final)
| Contrôle | Résultat |
|---|---|
| `make lint` (backend + frontend) | OK (809 fichiers formatés, ESLint + Prettier OK) |
| `make typecheck` | OK (mypy 784 fichiers ; tsc OK) |
| `make test-backend` | 4192 passed, 2 skipped (test d'image, Ghidra absent), 30 deselected |
| Test d'image réel `ANALYSIS_WORKER_IMAGE_TEST=1 … test_confined_analysis_image.py` | 1 passed (engine ilspycmd, `SUCCEEDED`, token `0x06000001`, `ldc.i4.7`/`ret`) |
| `make test-integration` (PostgreSQL) | 333 passed |
| `make test-workflow` | 22 passed (garde JUnit OK) |
| Frontend `pnpm test --run` | 38 fichiers / 291 passed |

Non exécutés : `make test-workflow-full`, `make test-workflow-browser*` (navigateur), `test_ghidra_service_real` (distribution Ghidra locale absente).

## 3. Matrice des lots
| Lot | Livré (chemin fonctionnel) | Preuve automatisée | Preuve RÉELLE (stack/données de l'enquête) |
|---|---|---|---|
| C1 pivots | Projection canonique `investigation-pivot-frontier.v1` ; `GET /api/investigations/{id}/pivots?limit&cursor` ; nœuds/arêtes/chemins ; états `rejected`/`confirmed` alimentés par C2 | tests unitaires + intégration endpoint | NON FAITE |
| C2 expériences | `experiment.record`, `experiment.list`, `GET /api/investigations/{id}/experiments` ; schémas `investigation-experiment.v1`, `…-timeline.v1`, `operator-experiment.v1` ; table append-only + trigger ; findings : identifiant logique/version courante/chaîne | unitaires + intégration PG + migrations (111) | NON FAITE (DDL live en attente d'accord) |
| C3 .NET | `ConfinedAnalysisTool.DOTNET_ILSPY` ; opérations `assembly.inspect`, `types.list`, `methods.list`, `references.imports`, `strings.list`, `resources.list`, `method.decompile` (RID stable) ; ILSpyCmd 9.1.0.7988 (MIT, SHA-512 épinglé) ; repli `monodis` honnête (`PARTIAL`) | unitaires + **test d'image réel** (fixture bénigne, assembly invalide → échec propre) | Fixture bénigne OK dans le worker réel ; échantillons en quarantaine (PsProxy/Blackwood/RuntimeBroker) NON rejoués |
| C4 réseau | Niveaux de preuve (`article_documented`, `static_inference`, `sandbox_capture_observed`, `hypothetical`, `verified_on_holdout`) ; train/holdout `verified/rejected/partial/not_testable` ; figures « documenté, non observé » ; capture 0 paquet ≠ « aucun réseau » | unitaires (fixtures complet/tronqué/0 paquet) | 4 PCAP VT (52/8/16/0) NON rejoués ; Suricata capture vide NON revérifié |
| C5 Bridge | `advisor-response.v2`, checkpoints (`consulted/skipped/denied`), recommandation→action→résultat, validation contre observations ; seuils 12 outils / 8 consultations **inchangés** (pas de mesure réelle) | unitaires | `advisor.consult yara_draft`/`pivot_strategy` NON essayés en réel |
| C6 YARA | Intervalles de Wilson 95 %, sensibilité `not_calculable`, dev/holdout séparés, match ≠ confirmation de famille, règle générique (imports .NET communs) rejetée, `real_partial` honnête | unitaires + workflows W14 | Réel (lecture seule, 12 DLL Mono bénignes, YARA 4.5.0, corpus sha `15b6053a…`) : 4 règles stockées (4, pas 3), 0/12 match, IC95 FP [0 ; 24,25 %] — **sensibilité inconnue**, aucun positif qualifié |
| C7 UI | 5 vues (Claims/Hypothèses, Pivots, Échantillons, Réseau & détections, Journal & revue) | RTL + parcours (frontend 291) | NON FAITE (accès navigateur à `operator/context/section/article_claims` à vérifier) |
| C8 Typst | Notes grises « Note de l'analyste » ancrées, chronologie unique, IOC en listes par type (corps) + `IOC originaux` / `Pistes / IOC candidats non confirmés` séparés, tableau en annexe, figures doc/observé ; versions de renderer bumpées | unitaires + 58 tests Typst réels + aperçu PDF fixture inspecté visuellement | Rendu de l'avis v14 réel et comparaison page à page vs CN2609 : NON FAITS |
| C9 contexte | `GET …/operator/context?view=summary` (`investigation-operator-context-summary.v1`), pointeurs section+cursor, ancienne forme conservée | unitaires + intégration PG ; fixture : 247 211 o → 4 571 o (−98,15 %, ≈61 762 → 1 143 tokens, chars/4) | Cas réel 251 384 o NON remesuré ; enveloppe de prompt côté client non mesurée |
| C10 REA | Diagnostic : `unavailable(reason)` — produit réel `rea-agents` 6.7.0 (HEAD `dc936f43…`), mapping/allowlist/confinement non validés en runtime ; adaptateur inchangé ; `docs/operations/rea-diagnostic-2026-10-19.md` | n/a | n/a (aucun faux `native.inspect`) |

## 4. Défauts trouvés à l'intégration (corrigés)
- Régression C6 : garde `verify_dotnet_source_motifs` bloquait les motifs de similarité du Bridge → limitée aux règles à imports .NET communs.
- Régression C6 : le statut de cycle de vie `candidate→tested` était rétrogradé quand la signature avait des limitations → statut = résultat mesuré, `validation_status` reste `real_partial`.
- Défaut réel C3 : `RLIMIT_AS` 512 MiB tuait CoreCLR → ILSpy jamais utilisé dans le worker ; réglage dédié `DOTNET_ANALYSIS_ADDRESS_SPACE_LIMIT_BYTES` (8 GiB par défaut, 8–32 GiB), `DOTNET_GCHeapHardLimit` 256 MiB, GC concurrent désactivé ; cgroup worker 2 GiB / 128 pids / 2 CPU.
- C8 : 18 régressions de tests (versions épinglées, clé `timeline` conservée en alias pour les payloads legacy, ordre des sections, chevauchements de titres, tableau du graphe).
- C5 : appel systématique à `external_llm_allowed` sur la fixture de snapshot (champs `tlp`/`do_not_submit` ajoutés à la fixture) ; golden hash de contexte mis à jour (rappel `advisor_guidance.reminder` enrichi, déterministe).
- Migrations : le test d'inventaire de triggers attendait l'ancien jeu (trigger append-only C2 ajouté).

## 5. Migrations / DDL (NON appliquées)
- `0001_baseline` (mutable) édité pour `investigation_experiments` + fonction/trigger append-only `reject_investigation_experiment_mutation`.
- SQL additif + rollback pour la DB live : `docs/operations/c2-experiments-ddl-2026-10-19.md`. **Requiert l'accord explicite de l'utilisateur** + sauvegarde préalable (`pg_dump` du schéma/tables concernées).
- Nouveaux réglages (sans secret) : `DOTNET_ANALYSIS_ADDRESS_SPACE_LIMIT_BYTES` (`.env.example`, `compose.yaml`).

## 6. Endpoints / schémas nouveaux
`GET /api/investigations/{id}/pivots` · `GET /api/investigations/{id}/experiments` · `GET …/operator/context?view=summary` · sections de contexte additionnelles (`finding_versions`, `available_tools`, `available_pivots`, `latest_tool_results`, `previous_actions`, `operator_requests`, `agenda`, `blocked_capabilities`, `budget_remaining`, `advisor_guidance`, `unavailable_tools`, `indicator_inventory`, `current_opinion`, `truncation`, `source`) · outils `experiment.record|list`, outils .NET, `advisor-response.v2`.

## 7. Erreurs / limites non résolues
- Aucune preuve réelle des capacités C1, C2, C4, C5, C7, C8, C9 (tests automatisés uniquement) ; C3 et C6 partiellement (fixture bénigne / corpus bénin).
- Sensibilité YARA inconnue : aucun positif confirmé ; qualification des 4 échantillons = décision humaine.
- REA indisponible ; MWCP : liste de parseurs vide ; Shodan plan `oss` ; Retrohunt désactivé.
- Clé VT potentiellement exposée (observée une fois dans une sortie d'outil, 2026-10-18) : **rotation à confirmer par l'utilisateur** ; aucune clé n'a été réaffichée.
- G3, publication, repin d'édition, rendu final de la release : non faits (décisions humaines).
- Frais/quotas : aucun appel fournisseur payant ni G2 consommé pendant cette phase.

## 8. Go / no-go pour la phase 02
**NO-GO conditionnel.** Le code est complet et validé en test (C1–C9 ont un chemin fonctionnel, C10 honnêtement indisponible), mais la phase 01 exige une preuve sur les objets réels. Conditions avant la phase 02 :
1. Accord utilisateur : sauvegarde + DDL C2 live (**non donné à ce jour**).
2. Redéploiement `make up` : **fait** (accord donné).
3. Vérifications réelles bornées, locales et passives (analysis-worker attesté, aucun appel externe, aucune consommation G2) : `.NET` sur PsProxy/Blackwood/RuntimeBroker ; `network.capture.inspect` + `network.suricata_replay` sur les 4 PCAP (dont 0 paquet) ; `sample.analyze` capa ; `GET /pivots`, `/experiments`, `?view=summary` sur l'investigation 12 ; rendu PDF de l'avis réel + comparaison à CN2609.
4. Décision humaine sur G2 pour les consultations Bridge réelles et pivots VT/Shodan de la phase 02 ; rotation de la clé VT.
