# M10 — Diagnostic (étape 1, lecture seule)

Base : `main` = `c7679a77` (local = `origin/main`). Mission auditée sur `73b35c40` : delta = 24 fichiers, uniquement skills/docs opérateur (`docs/operations/operator-skills.md`, `scripts/ops/stage_operator_cti_skills.py`, skills) — aucune hypothèse de code M09 invalidée.
Branche `m10/integration` (worktree `../AutoWork-m10-integ`). Sources R3 : `.autowork-runs/m09r/` (`journal.jsonl`, `processed/` = corps de requêtes, `outbox/` = réponses), `.autowork-runs/m09/status_r_final.json`.
Tout ce qui suit est tiré du code ou des traces persistées du relais ; les valeurs de l'état live n'ont **pas** été relues (aucun accès live fait pour ce document).

Légende : **PROUVÉ** = reconstitué à partir d'une trace + chemin de code ; **INFÉRÉ** = cohérent avec les traces, preuve finale à obtenir par test ; **UNKNOWN**.

---

## Lot A — les trois `experiment_invalid`

Payloads exacts retrouvés : `processed/r3-026-experiment-record-c6.json`, `…-033-…-corrected.json`, `…-035-…-precise.json` (le journal ne garde que `arguments_sha256`, le corps est dans `processed/`). Réponse stockée : `outbox/r3-026-…json` → `status=failed`, `error.code=experiment_invalid`, `reason` générique ; l'observation d'échec ne contient que `{"error_code":"experiment_invalid"}` (le `message=str(exc)` de l'adaptateur est perdu à la projection).

Chemin d'erreur : `investigation_experiments.py:1003` regroupe `KeyError/TypeError/ValueError` en `experiment_invalid` ; la projection opérateur (`operator_tool_invocation.py`, `error.reason` générique) ne publie pas le détail.

**Différence discriminante** entre les trois échecs et le record réussi `r3-060` (diff champ à champ des `arguments`) : hors reformulations de texte, seules deux choses changent — `observation_refs` (échecs : 3 refs, dont `b9f0c9ad-80fd-5cea-ba7e-dce7940cb82c` ; succès : 2 refs, sans `b9f0c9ad`) et `next_pivots` (échecs : 2 hashes ; succès : 1). Les timestamps (`00:24:13Z` ×2 puis `00:22:40.628982Z` exact) et `tool_version` ont été modifiés entre les essais 1→2→3 sans changer le résultat : **ce ne sont pas les causes**.

`b9f0c9ad` est la sortie de `sample.get_feature_pack` (invocation `a4f04816-6687-4b62-98f2-c3bedf9ade62`, `outbox/r-129-feature-refs.json`), non citée dans `tool_invocation_refs` (seule `1875e0aa…` l'est). Garde correspondante : `investigation_experiments.py:1073-1078` → `ValueError("observation tool invocation was not cited")` quand `observation.invocation_ref` n'est pas dans les invocations citées.

| invocation_id | cause | statut de preuve | code avant | code après (cible) | test |
|---|---|---|---|---|---|
| `285870b6…` (r3-026) | `observation_refs` contient `b9f0c9ad` dont l'invocation `a4f04816` n'est pas citée | INFÉRÉ fort (payload + garde + réponse du record corrigé) ; `observation.invocation_ref` de la ligne PG non relu | `experiment_invalid` | `experiment_invalid` + `detail_code=observation_invocation_not_cited`, `field=observation_refs` | à écrire (A-red) |
| `a6232868…` (r3-033) | idem (seul `tool_version` modifié) | INFÉRÉ fort | idem | idem | idem |
| `21e103d2…` (r3-035) | idem (timestamps + input_refs corrigés, `b9f0c9ad` toujours cité) | INFÉRÉ fort | idem | idem | idem |

Réserve explicite : sans lire `observation.invocation_ref` en base, la cause reste « prouvée par le code et par l'intersection des échecs », pas observée. Aucune hypothèse alternative n'est retenue par les traces (`next_pivots` valides au sens `_SHA256`, dates chronologiques, version de flux 5 confirmée par `experiment.list`, refs d'invocation `1875e0aa` v3 réutilisées dans le succès). Si la ligne PG ne confirme pas, la classe d'erreur reste couverte par des tests synthétiques et les 3 invocations resteront `UNKNOWN`.

Correctif minimal : diagnostics publics `{code, detail_code, field, correction}` pour **chaque** `raise` de `_execute` et du domaine (`InvestigationExperimentV1.__post_init__`, 12 contrôles) via une enum fermée de `detail_code` ; jamais `str(exc)` ni valeur de champ. `stale_experiment_version` doit exposer `current_stream_version`. Aucune validation n'est assouplie.

## Lot B — `stale_working_state`

Mécanisme (code) : `_invoke_locked` (`operator_tool_invocation.py:1139`) compare `expected_version` à la version **globale** de l'état de travail (`:1333`), puis `state.with_operator_tool_action(...)` (`domain/investigation_state.py:192`, `version+1`, ajoute `operator_tool:<tool>:…` à `recent_actions`) et `save_if_version` (`:1480`) **avant** le dispatch. Donc **tout** POST d'outil — y compris `LOCAL_READ`/`LOCAL_COMPUTE` indépendants — invalide le jeton `(version, sha256)` de tous les autres agents. Les lectures `GET /operator/context[/section/…]` portent aussi un CAS (`:606`, `:691`). L'exclusion mutuelle de l'adaptateur est un `asyncio.Lock` **par préfixe d'idempotence, par processus** (`_ACTIVE_OPERATOR_INVOCATIONS`) : elle ne sérialise rien entre agents. Contrainte `UNIQUE(operation_key)` en base (`models/investigation.py:63`) : filet anti-double-effet inter-processus. Versions distinctes : état (`investigation_working_states.version`), invocation (`tool_invocations.version`), enveloppe/budget G2 (`save_if_version` sur `InvestigationBudgetRecord`), flux d'expériences (`expected_version` du flux), observations (append-only, UUID5).

**Mesure « avant » (journal R3, seul source disponible)** : 7 réponses `409` en 528 requêtes — **6 `stale_working_state`** (R3 : 1 GET ; sous-agent 035 : 2 GET + 1 POST `method.decompile` ; sous-agent 762 : 2 GET) + 1 `409` hors catégorie ; **5 sur 6 sont des GET** de section de contexte, 1 seul POST d'outil. Le rapport M09 (« 4 par sous-agent, 3 pour R3 ») ne se reproduit pas à partir du journal du relais : à rapprocher de la base canonique. Autres erreurs de contexte observées : 422 sur curseurs `available_tools` (exigent un CAS), 503 sur `GET /operator/context`.

Conséquence pour le correctif : (1) le coût réel est surtout sur les **lectures paginées avec jeton** ; (2) le jeton est consommé par chaque POST, d'où la fragilité des curseurs. Un coordinateur côté relais n'est **pas** suffisant : le relais (hors dépôt, mono-thread, non redémarrable ici) sérialise déjà les POST, et les jetons périmés sont détenus par les agents ; réécrire `expected_version` dans le relais contournerait le CAS pour tous les outils, y compris `experiment.record`/`hypothesis.declare`.

Piste retenue (à valider par test rouge) : les outils *indépendants de l'état analytique* (effet `LOCAL_READ`/`LOCAL_COMPUTE`, sans `operator.write`) cessent d'avancer la version globale et leur garde `expected_version` devient une vérification de fraîcheur non bloquante ; les outils d'écriture analytique (`hypothesis.declare`, `experiment.record`, `finding.*`, `opinion.*`), d'acquisition et externes gardent le CAS strict. Risques à examiner par la revue xhigh : (a) `recent_actions` perd la trace des appels locaux (la trace d'audit reste `tool_invocations`) ; (b) le filet anti-doublon inter-processus repose alors sur `UNIQUE(operation_key)` — l'`IntegrityError` doit être mappée sur un rejeu idempotent ; (c) budget `tool_seconds` : CAS d'enveloppe inchangé ; (d) tests existants qui supposent « chaque outil avance le CAS ».

## Lot C — blob .NET de 964 octets

- `local.run_command` est bien disponible et `xxd` est **offert** : `command_id.enum = [archive_extract_hash, archive_list, file, monodis, objdump, readelf, sha256sum, strings, xxd, yara, zip_members]` (`outbox/r-005-available-tools.json`) ; `xxd` exige `offset` **et** `length` (≤ 4096), `sample_sha256` sans chemin libre (`application/local_analysis.py`, `investigation_local_tools.py`). **0 des 103 invocations** ne l'a appelé (mesuré, rapport M09 + journal).
- Le blob est dans l'échantillon **B/762** (`76273382…`, 8 487 424 o), `PrivateImplementationDetails::.cctor` (RID 130) : blob de 964 octets chargé depuis un champ statique de données privé (champ `'3'`, `byte[] '4'`), transformation `b[i] XOR i XOR 0xAA`, 72 tranches UTF-8 (worker 762, obs. `ce88622b…`, `c5cbe34d…`, `a432d86b…`). Ce n'est pas l'échantillon A/035.
- **Aucune observation n'établit le RVA du champ** et le dépôt n'a **aucun** code `FieldRVA` / conversion RVA→offset (grep négatif sur tout le dépôt hors node_modules/.git/var). Le décompilateur ne rend que `ldtoken`/`InitializeArray`. `pefile==2024.8.26` est déjà une dépendance (worker `dotnet_analysis.py` 1272 l.).
- Conclusion : **ce n'est pas seulement un défaut de sélection d'outil**. `xxd` suffirait *si* l'offset fichier était connu ; la barrière réelle est l'absence d'un moyen déterministe d'obtenir `FieldRVA(field 3)` → section → offset fichier, puis de lier la plage lue à l'objet (offset, longueur, SHA-256). Proposition minimale : une opération bornée en lecture seule dans le worker .NET existant (pas un lecteur de chemin) — `FieldRVA` d'un champ identifié par token/RID, conversion RVA→offset testée (sections normales, RVA hors section, tailles raw/virtuelle incohérentes, section tronquée), renvoyant `{rva, section, file_offset, length, sha256}` ; la lecture des octets passe ensuite par `xxd`/`sha256sum` (ou par la même opération avec plafond ≤ 4096). **À ne construire que si le test de lacune est rouge** : vérifier d'abord que `monodis`/`resources.list` n'exposent pas déjà les RVA (non démontré).
- Pas de mécanisme ETW déduit de l'obfuscation.

## Lot D — observations et findings

Observations relues dans `outbox/` (SHA identiques au checkpoint) : `177f1802-4db1-5a19-9da8-7fda5280b5d7` `4e8f5837…` (ctor 035), `5206d963-e16c-5abf-8a34-dd66c82d42dc` `c7631941…` (override 035), `9c64e2a6-66f7-5e16-99fb-bfd93d149de3` `ba28e5d2…` (ctor 762), `faf796e7-98df-58fb-a2fa-1eaf37dbcccc` `3c81dc6d…` (override 762) : toutes `autowork` dotnet, lectures statiques (CIL), `untrusted_data`. Ce qu'elles établissent : héritage direct/appel du ctor `System.AppDomainManager` et surcharge `InitializeNewDomain` — **pas** d'exécution, ni ETW.
Contrat `finding.propose` (`investigation_findings.py:497`) : pour un type « objet identifié », `supported` exige `subject_ref` égal à l'`object_id` d'une observation citée, sinon rétrogradé `qualified`/`inconclusive`. Faisabilité d'un finding `supported` étroit par échantillon : **INFÉRÉ** (à vérifier : `object_id` des observations `dotnet.*` = SHA de l'échantillon ?). Aucun finding plus large.

**D4 — `analysis_status=not_analyzed`** : `research_context_builder.py:~3250/3392/3423` ne passe à `analyzed` que pour les invocations `ghidra.*` et `sample.analyze` (observations dont la source commence par `autowork.analysis_worker`/`ghidra`) ; les outils `assembly.inspect`, `types.list`, `methods.list`, `method.decompile`, `strings.list`, `references.imports`, `resources.list` produisent des observations de source `dotnet.<engine>` (`investigation_dotnet_tools.py:421`) et `local.*` n'est pas compté. Donc le statut est **correct selon son contrat** (opération `sample.analyze`/Ghidra) mais **trompeur** : l'activité statique .NET/locale n'est pas projetée. Correctif proposé : champ additif distinct (ex. `local_activity` : outils, comptes, `observation_ids` par échantillon), `analysis_status` inchangé. Ne pas lancer `sample.analyze`.

## Lot E — discipline

Constat : les `hypothesis.declare` des sous-agents sont postérieurs à leurs appels (journal : déclarations après outils) ; consigné, jamais antidaté. Pour la reprise : nouvelle session opérateur, brief M10 sans conclusions M08. Le chemin de délégation réel R3 = demande écrite → lancement par l'orchestrateur (pas par l'opérateur) : sera décrit comme tel.

## Lot F — réconciliation, acquisitions, G2, CI

- **F2 PROUVÉ** : les deux `vt.download_sample` renvoient `reused_existing_sample:true` (`outbox/r3-047-download-035.json`, `r3-049…`, `r3-056…`). `investigation_acquisition.py:225-290` : réutilisation d'un Sample vérifié (même enquête ou même sujet) ⇒ `budget_consumed={VT_UNITS:0, DOWNLOADS:0}` et message « no VirusTotal download was made ». Les trois notions sont distinctes : association, objet de quarantaine réutilisé, téléchargement physique ; `downloads=0` est **attendu**, pas un défaut de comptage. Reste à documenter (rapport) : le compteur n'est pas ambigu côté code, mais l'UI/checkpoint ne le dit pas.
- **F3 INFÉRÉ** : la version d'enveloppe G2 est un compteur de `save_if_version` (`investigation_acquisition.py:791`, `investigation_service.py:4054`, `osint_open_policy.py:539`) : chaque réservation/engagement/libération budgétaire l'avance ; un outil local qui réserve `tool_seconds` = ≥ 2 incréments. À confirmer sur événements persistés (lecture PG, non faite) ; ne signifie jamais une nouvelle décision d'approbation (`approval_decision_id` reste le marqueur d'autorisation, voir `:1456`).
- **F1** : chemin de réconciliation VT passive implémenté en M09 (`020a4fbe`, `72c65f71`), jamais exercé en réel ; à tester en environnement contrôlé (PG test) ; les 5 invocations M08 ne seront pas touchées.
- **F4** : base CI de `c7679a77` **UNKNOWN** (non consultée) ; base de tests M09 : 39 échecs unitaires préexistants (Typst, `confined_analysis_dispatch`), 3 tests PG M08 rouges.

---

## Ordre d'exécution proposé (étape 2)

| Lot | Session | Fichiers (non chevauchants) | Oracle rouge d'abord |
|---|---|---|---|
| A | Luna high | `investigation_experiments.py`, `domain/investigation_experiment.py`, projection d'erreur dans `operator_tool_invocation.py` (zone d'erreurs uniquement) + tests | test rejouant les 3 payloads → `observation_invocation_not_cited` |
| B | Luna xhigh | `operator_tool_invocation.py` (chemin `_invoke_locked`), `domain/investigation_state.py` + tests PG de concurrence | 2 analyses locales indépendantes, même version initiale |
| C | Luna xhigh (si lacune confirmée) | `workers/dotnet_analysis.py`, `investigation_dotnet_tools.py` + tests PE synthétiques | test RVA→offset ; test de lacune |
| D | Luna high | `research_context_builder.py` (projection additive) + tests | projection d'un échantillon avec `dotnet.*` |
| F1 | Luna high (lecture/test) | tests seulement | scénarios de réconciliation |

A et B touchent tous deux `operator_tool_invocation.py` : B passe en premier sur `_invoke_locked`, A est limité à la zone de projection d'erreurs ; intégration séquentielle par l'orchestrateur.
