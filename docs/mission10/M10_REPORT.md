# M10 — Déblocage de l'investigation CTI : rapport

Base : `main` `c7679a77` (= `origin/main`; mission auditée sur `73b35c40`, delta = skills/docs uniquement). Branche `m10/integration` @ `39d06d7f` (worktree `../AutoWork-m10-integ`). **Non poussée, non fusionnée dans `main`, non déployée.** Aucun accès live, aucune lecture de la base canonique, aucun appel fournisseur, aucune reprise d'enquête (E non exécuté : exige déploiement + autorisation live séparés).

## Verdict : **PARTIAL**

Les réparations A, B, C, D sont implémentées, testées hors ligne et relues indépendamment (Luna xhigh pour B et C). Je ne retiens pas PASS parce que : (1) le code n'est pas déployé et l'enquête R3 n'a pas été reprise : la capacité « opérateur + deux sous-agents sans débogage permanent » n'est **pas démontrée en live** (critères 7 et la partie E) ; (2) le blob de 964 octets n'a **pas** été lu (il faut le code déployé + l'image du worker d'analyse reconstruite) ; (3) F3 (version G2) et F4 (CI distante) restent `UNKNOWN`/`INFÉRÉ`.

## 1. Base et incidents : cause démontrée

| Lot | Incident | Cause | Statut de preuve |
|---|---|---|---|
| A | 3 × `experiment_invalid` (`285870b6…`, `a6232868…`, `21e103d2…`) | `observation_refs` contenait `b9f0c9ad-80fd-5cea-ba7e-dce7940cb82c`, sortie de `sample.get_feature_pack` (invocation `a4f04816…`), alors que `tool_invocation_refs` ne citait que `1875e0aa…` → `ValueError("observation tool invocation was not cited")` (`investigation_experiments.py`), regroupé en `experiment_invalid` | **INFÉRÉ fort** : payloads exacts retrouvés dans `.autowork-runs/m09r/processed/`, diff champ à champ avec le record réussi `r3-060` (seuls `observation_refs` et `next_pivots` diffèrent matériellement ; timestamps et `tool_version` modifiés sans effet), garde du code identifiée. `invocation_ref` de la ligne PG de `b9f0c9ad` **non relu** → la cause reste inférée, pas observée |
| B | `stale_working_state` entre agents | Tout POST d'outil, même `LOCAL_*`, faisait avancer la version globale de l'état (`with_operator_tool_action` + `save_if_version` avant dispatch) et périmait les jetons des autres agents | **PROUVÉ** (code + test rouge sur la base, voir §4) |
| C | Blob de 964 octets non lu | `xxd` était **offert** (`command_id` enum) mais exige un offset fichier ; **aucun outil n'exposait le RVA du champ** (`FieldRVA`) et le dépôt n'avait aucune conversion RVA→offset. Le blob est dans l'échantillon B (`76273382…`), `.cctor` RID 130. Ce n'est donc pas seulement un défaut de sélection d'outil | **PROUVÉ** (grep négatif + test de lacune rouge) |
| D | `analysis_status=not_analyzed` | Contrat correct : `analyzed` seulement pour `sample.analyze` / `ghidra.*` ; les outils `dotnet.*` et `local.*` ne sont pas projetés | **PROUVÉ** (code) |
| F2 | `downloads=0` malgré 2 `vt.download_sample` réussis | Réutilisation d'un Sample vérifié (`reused_existing_sample:true` dans les réponses R3) ⇒ `budget_consumed={VT_UNITS:0, DOWNLOADS:0}` par contrat | **PROUVÉ** (code + réponses stockées) |

## 2. Lots, fichiers et commits

| Lot | Commits (branche `m10/lot-*`) | Contenu |
|---|---|---|
| A | `1eb5bd0c`, `d0bcd608`, `35808034` | Enum fermée de `detail_code` + `field` + `correction` fixes pour `experiment.record` (`observation_invocation_not_cited`, `experiment_version_stale`, `invocation_version_mismatch`, `invocation_tool_mismatch`, `next_pivot_invalid`, `timestamp_order_invalid`, `field_invalid`) ; `stale_experiment_version` donne `current_stream_version` ; jamais `str(exc)` ni valeur de champ ; aucune validation assouplie |
| B | `fd93630d`, `782a64f4`, `a604c49e`, `39d06d7f` | Outils `LOCAL_READ/LOCAL_COMPUTE` non écrivains acceptent un jeton périmé et n'avancent plus la version globale ; 13 écrivains analytiques + `archive.extract_member` restent en CAS strict ; verrou consultatif PG par préfixe d'idempotence ; rejeu (sans redispatch) d'un `RUNNING` pour les effets locaux ; copie privée d'enveloppe + 3 tentatives d'admission budgétaire pour les outils locaux uniquement ; verrou global du dispatcher relâché pour des opérations locales distinctes |
| C | `618dacf4`, `e9e8965c`, `36b4b251` | Nouvel outil `field.data.read` (capacité `investigation.dotnet`) : FieldRVA → section → offset fichier, ≤ 4 096 octets, échantillon associé en quarantaine adressé par SHA-256 (aucun chemin), SHA-256 des octets renvoyés, observation canonique normale ; budget agrégé de 16 Mio de chaînes de métadonnées ; taille inconnue ≠ troncature confirmée ; schéma == validation |
| D | `676adeb0`, `59c320ae`, `ca7a8e55` | Champ additif `local_activity` par association (comptes par outil, ≤ 8 ids d'observation, compteurs de troncature) ; `analysis_status` inchangé ; doc dans `docs/investigation_contracts.md` |

## 3. Tests exécutés (réellement)

Commande : `PYTHONPATH=<worktree>/backend/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest …` (`uv run` impossible hors ligne : DNS ; ne pas utiliser le Conda actif).

- Suite unitaire complète `tests/` hors `integration` et `workflow_emulator`, sur `m10/integration` @ `d8516c07` : **5 580 passés, 38 échecs, 3 ignorés**. Les 38 échecs sont **identiques** à ceux de `main` `c7679a77` (comparaison fichier par fichier : Typst ×30, figures, IOC, bande de détection, protocol diagrams ×2, `confined_analysis_dispatch` ×3) : **préexistants, aucun introduit**.
- Après `39d06d7f` (classifieur + `archive.extract_member`) : 135 passés (`test_investigation_tools_freshness_classifier`, `test_operator_tool_invocation`, `test_available_equals_executable`).
- PostgreSQL (conteneur `postgres-test`, 127.0.0.1:55432, DSN de test du Makefile ; base live non touchée) sur `d8516c07` : `test_investigation_persistence_postgres`, `…_experiments_…`, `…_acquisition_…`, `…_vertical_…`, `…_api_…` : **30 passés** (dont verrou d'admission par préfixe et rejeu concurrent d'`operation_key`).
- ruff check/format et mypy propres sur les fichiers touchés de chaque lot.
- **Non exécutés** : `tests/workflow_emulator`, frontend, `make test-integration` complet, tests nécessitant l'image du worker d'analyse / Docker pour `field.data.read` (tests synthétiques PE/CLI uniquement), CI distante.

## 4. Comparaison des erreurs CAS

- Avant (journal du relais R3, 528 requêtes) : **6 `stale_working_state`** (5 GET de section de contexte, 1 POST `method.decompile`) ; le « 4 par sous-agent, 3 pour R3 » du rapport M09 **ne se reproduit pas** depuis ce journal.
- Oracle de concurrence (deux outils locaux indépendants démarrant du même `(version, sha256)`) : code de **base** = **1 `stale_working_state` sur 2** (mesuré en exécutant les tests M10 contre le source de `300e7e57`) ; M10 = **0 sur 2**, deux observations distinctes, `tool_seconds` exacts (8). Un écrivain analytique conserve le CAS strict (5 cas paramétrés + test exhaustif du classifieur).
- Non mesuré en live (aucun déploiement) : réduction réelle sur trois agents.

## 5. Lot C — lecture du blob

`field.data.read` est testé sur images PE/CLI synthétiques (RVA en-tête, section, trou, queue virtuelle, sections qui se chevauchent/malformées, fichier tronqué, RVA nul/énorme/débordement). **Le blob de 964 octets de `76273382…` n'a pas été récupéré** : cela exige le déploiement du code et la reconstruction de l'image du worker. Limites connues : seul le flux de métadonnées optimisé `#~` est supporté (`#-` refusé fermé) ; si la taille du champ n'est pas déductible du type, l'appelant doit fournir la longueur et la complétude est déclarée « inconnue ». Aucun mécanisme ETW n'est déduit.

## 6. Revues indépendantes

- Lot C (Luna xhigh, lecture seule) : APPROVE_WITH_FIXES → P2 amplification mémoire, P3 drapeau de troncature, P3 schéma ; **corrigés** (`36b4b251`), vérifiés. Aucun chemin arbitraire / autre échantillon / mauvais offset.
- Lot B (Luna xhigh, lecture seule) : APPROVE_WITH_FIXES, pas de P0/P1 ; 13 écrivains analytiques correctement stricts, pas d'inversion d'ordre de verrous, une `AsyncSession` par requête. **Appliqué** : `archive.extract_member` rendu strict, test exhaustif du classifieur. **Résiduels** : (P2) après crash d'un processus ayant commité `RUNNING`, le rejeu renvoie `RUNNING` sans redispatch (pas de double effet) ; la réservation reste tenue jusqu'à `resume()` → `NEEDS_REVIEW` ; (P3) le test PG du verrou utilise un délai de 1 s qui peut donner un faux positif ; pas de test forçant l'échec des 3 tentatives d'admission ; un jeton plus ancien n'est pas revérifié contre le sha historique (indice de fraîcheur pour les outils locaux).
- Lots A et D : relus par l'orchestrateur (correctifs appliqués : table de diagnostics unique, `repository.version()`, typage ; formatage).

## 7. Observations canoniques relues (D2)

`177f1802-4db1-5a19-9da8-7fda5280b5d7` `4e8f5837…`, `5206d963-e16c-5abf-8a34-dd66c82d42dc` `c7631941…`, `9c64e2a6-66f7-5e16-99fb-bfd93d149de3` `ba28e5d2…`, `faf796e7-98df-58fb-a2fa-1eaf37dbcccc` `3c81dc6d…` : relues dans les réponses stockées du relais (`outbox/`), SHA identiques au checkpoint ; lecture statique du CIL uniquement ; elles établissent l'appel du ctor `System.AppDomainManager` et la surcharge `InitializeNewDomain`, ni exécution ni lien ETW. (Relues depuis le relais, pas depuis la base canonique.)

## 8. Findings (D3) et expériences

Aucun finding ni expérience créé (aucune session live). Contrat établi : les observations `dotnet.*` ont `object_id = "{sample_sha256}:{operation}:{fingerprint}"` ; `constrain_supported_assessment` exige `subject_ref == observation.object_id`, donc un finding `supported` avec `subject_ref` = SHA de l'échantillon est rétrogradé en `qualified` (limitation « l'observation citée ne porte pas sur l'objet visé »). Décision : **règle non modifiée** (« ne pas forcer l'assessment »). Un finding `qualified` étroit est possible aujourd'hui ; un appariement par préfixe `{sha}:` pour `object_type=dotnet_analysis` serait un changement du contrat de provenance, à décider séparément avec revue xhigh. `S2P2.1` peut rester `inconclusive`.

## 9. Lot F

- **F1** : 17 tests M09 de réconciliation (API, doubles de test) passent (`not_executed` refusé quand des résultats sont persistés, `abandon`, outils non passifs refusés, clé d'idempotence obligatoire). Non fait : scénario PostgreSQL avec réservations réelles. Les 5 invocations `needs_review` de M08 **non touchées**.
- **F2** : voir §1. **F3** : INFÉRÉ — la version d'enveloppe est un compteur de `save_if_version` (`investigation_acquisition.py:791`, `investigation_service.py:4054`, `osint_open_policy.py:539`) avancé par chaque réservation/engagement/libération (un outil local réservant `tool_seconds` ≥ 2 incréments) ; ne correspond pas à une nouvelle approbation (`approval_decision_id`). Non confirmé sur les événements persistés (aucune lecture PG live). **F4** : CI distante de `c7679a77` **UNKNOWN** (non consultée) ; base locale = 38 échecs préexistants identiques.

## 10. Budgets, fournisseurs, sous-agents

Budget avant/après et effets fournisseurs réels : **aucun** (aucune opération live). Sous-agents Paseo (Luna, `auto-review`) : lots A, B, C, D, une réparation B, un correctif C, deux revues ; les sessions n'avaient pas accès au réseau (`uv sync` impossible) et n'ont pas pu lancer PostgreSQL : toutes les vérifications PG et la suite complète sont de l'orchestrateur. Premier livrable du lot B : **7 tests en échec** (non exécutés par la session), réparé par une session neuve. Coût tokens : UNKNOWN.

## 11. UNKNOWN / BLOCKED / décisions réservées

1. **Déploiement** de `m10/integration` (relais en pause, `safe_deploy.sh`, image du worker d'analyse) — requis pour C en live et pour la reprise ; non autorisé ici.
2. **Reprise de l'enquête R3** (session opérateur neuve, ≤ 2 sous-agents, `field.data.read`/`xxd`, `experiment.record`, `finding.propose`) — exige l'autorisation live effective.
3. Relecture PG de `observation.invocation_ref` de `b9f0c9ad…` pour passer la cause du lot A de « inférée » à « observée ».
4. Décision sur l'appariement par préfixe pour `finding.propose` (§8).
5. Fusion/push de `m10/integration`.
