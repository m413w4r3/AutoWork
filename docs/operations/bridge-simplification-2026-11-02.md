# Simplification de l'intégration Bridge — audit et rapport avant/après (2 novembre 2026)

**Base :** `main@e3f9c03d`. Branche d'intégration : `bridge-simp/integration`.
Chemin métier traité : opérateur → `advisor.consult` → dossier → Bridge → réponse persistée → YARA / Suricata / pivots / avis.
Cette note remplace les hypothèses de `bridge-friction-audit-2026-11-01.md` là où elles sont contredites ci-dessous.

## 1. Architecture précédente (constats vérifiés dans le code)

| Constat | Détail |
|---|---|
| Préparation dupliquée | `InvestigationService.execute_tool_call` appelait `reservation_cost` **avant** le dispatch et avant la création de la ligne d'invocation. Cette méthode construisait tout le dossier (lectures, associations, blobs, rendu du prompt). `InvestigationAdvisorTool._execute` refaisait ensuite la même préparation. `advisor_wire_aliases` reconstruisait en plus le dossier alors qu'un manifeste existait : **4 constructions par consultation** (7 à 8 mesurées dans le banc, voir §3). |
| Requêtes N+1 | `sample_associations.list_for_investigation` coûtait `1 + 2S` requêtes SQL (une requête de dernière version et une de changements de qualification par association). Elle était appelée 3 fois par passe, plus une fois par mesure via `_sample_blob_for_hash`. |
| Balayages imbriqués | `_sample_inventory` testait chaque association contre chaque observation (O(S×O) `await`), le cache de payloads (48 entrées) saturait avec des hashes imbriqués. |
| Lectures redondantes | Chaque observation était lue et parsée 2 à 3 fois (extraits, mesures, mesures vérifiées). |
| Transport | Le relais (`docs/mission07/tools/relay.py`) sérialise les POST (1 worker) mais laisse passer 4 GET. La route API attend la fin de l'invocation (`api/investigation.py`), sans acceptation asynchrone. |
| Import YARA Bridge | `rule.propose` reconstruit une règle locale à partir de motifs (SHA différent de la source Bridge). `rule.propose_bridge` stockait la source exacte mais exigeait `canonicalization_eligible`, un catalogue de mesures complet et l'égalité octet par octet de chaque string. |
| Diagnostics | `rule_compile_check_failed` regroupait `invalid_input` (dont les refus du validateur interne), `engine_crashed`, `timeout` et `resource_exceeded`. |
| Réponse brute | Le texte brut était écrit en blob avant le parsing, mais l'ID d'invocation, les références de dossier et le manifeste n'étaient liés qu'**après** le parsing. |
| Champs facultatifs | `measurements` mal formé refusait `draft_text` (`measurements_invalid`), alors que `yara_draft` se repliait déjà sur les mesures vérifiées. |

## 2. Hypothèses confirmées, infirmées, non prouvées

- **Confirmé (code) :** double préparation, N+1 sur les associations, O(S×O), lectures répétées.
- **Probable, non prouvé :** les ~24 min « POST reçu → outil démarré » correspondent à `reservation_cost` exécuté avant `advisor_started`, et les ~27 min de dossier à la seconde préparation. Aucun horodatage existant n'encadrait `reservation_cost`, donc **la corrélation avec les traces M12-Q n'est pas établie**. Le correctif ajoute cette mesure (`reservation_cost_ms`).
- **Non prouvé :** le relais comme cause des 24 min. Le relais journalisait `received_at` / `started_at`, mais aucune trace M12-Q ne les corrèle à cette requête.
- **Infirmé :** « la taille du dossier explique le délai ». Les 16,5 Mo viennent des samples complets en base64 (`yara_draft`, `pivot_strategy`), contenu utile et conservé. Le coût dominant est constitué des lectures et reconstructions répétées.
- **Infirmé :** `rule_compile_check_failed` = erreur de syntaxe YARA. C'était une reconstruction locale (SHA différent) ou un refus du validateur, pas la source Bridge compilée par YARA.
- **Infirmé :** l'échec de `test_yara_draft_preserves_claims_and_sends_only_verified_offsets` n'était pas une assertion de motif périmée : l'attente `len(transport.calls) == 1` ne correspondait plus au comportement toléré (requête avec `measurements` mal formé envoyée avec repli).
- **Rejeté (preuve de code) :** séparer les voies de POST du relais. `advisor.consult` avance l'état de travail avant le dispatch et renvoie cette version sans recharger ; un POST local concurrent (`hypothesis.declare`…) peut faire échouer l'appel suivant en `409 stale_working_state`.

## 3. Mesures — banc hors ligne reproductible (pas une mesure de production)

Banc `backend/tests/test_advisor_consultation_context.py` : environ 40 samples associés, quelques centaines d'observations avec hashes imbriqués, plusieurs Mo de blobs, compteurs sur chaque appel. Les compteurs couvrent `reservation_cost` + `execute`. Les requêtes SQL d'associations sont modélisées (`1 + 2S` par listing). Les temps sont des exécutions uniques, indicatifs.

| Consultation | Requêtes SQL associations | Lectures de payload | Octets lus | Constructions du dossier | Temps du banc |
|---|---:|---:|---:|---:|---:|
| `yara_draft` | 78 246 → 6 | 11 040 → 280 | 8,09 Mo → 2,68 Mo | 8 → 1 | ≈ 5,3 s → 4,2 s |
| `pivot_strategy` | 78 246 → 6 | 11 040 → 280 | 8,09 Mo → 2,68 Mo | 7 → 1 | ≈ 5,3 s → 4,1 s |
| `suricata_draft` | 78 246 → 6 | 11 040 → 240 | 2,87 Mo → 0,06 Mo | 7 → 1 | ≈ 1,1 s → 0,6 s |
| `draft_text` | 26 406 → 6 | 10 400 → 240 | 2,71 Mo → 0,06 Mo | 7 → 1 | ≈ 0,8 s → 0,4 s |

Le banc utilise des repositories en mémoire : le gain de latence réel est dominé par les allers-retours PostgreSQL / MinIO supprimés (78 246 → 6 requêtes), donc **supérieur** à celui du temps du banc. Aucune accélération de production n'est annoncée tant qu'une consultation réelle n'a pas été mesurée (§7).

**Équivalence de contenu :** les SHA-256 du dossier, du prompt et des références de manifeste sont identiques à la base pour les quatre types (test de caractérisation).

## 4. Simplifications retenues

1. **Contexte de consultation** (`AdvisorConsultationContext`, interne) : associations, samples, payloads hash-vérifiés `(blob_id, sha256)`, mesures extraites et index sample → observations construits une fois.
2. **Un seul build du dossier**, réutilisé par le rendu du prompt ; plus de désérialisation / ré-encodage base64 inutile.
3. **`reservation_cost` allégé** : estimation structurelle sans lecture de payload, puis ajustement de la réservation, de l'invocation et de l'autorité d'effet externe aux octets / tokens **mesurés** avant le transport. Le sur-estimé n'engendre pas de refus artificiel. Toute erreur de cette étape est un échec **avant envoi** (jamais ambigu). Revalidation des politiques d'association et de sample juste avant le transport (la revue a trouvé un risque de cache périmé, corrigé).
4. **Requêtes d'associations en O(1)** (3 requêtes par listing).
5. **Instrumentation :** horodatages et durées par étape (`evidence_listing`, `association_resolution`, `blob_reads`, `measurement_extraction`, `sample_selection`, `dossier_rendering`, `reservation_cost_ms`) et compteurs `repo_calls`, `blob_reads`, `bytes_read`, `observations`, `samples` — métadonnées uniquement.
6. **Relais :** champs additifs `received_at`, `started_at`, `completed_at`, `queue_wait_seconds`, `run_seconds`, voie, hash de la clé d'idempotence ; `relayget.py` affiche un statut lisible. Aucun renvoi automatique d'un POST ambigu.
7. **YARA Bridge :** `rule.propose_bridge` conserve la source exacte et son SHA comme candidate attribuée à Bridge ; les strings sans mesure vérifiée sont enregistrées comme **lacunes de justification** (jamais comme vérifiées) ; import idempotent (UUID5 sur investigation + observation + invocation + SHA) ; diagnostics structurés `{stage, status, reason_code}` ; `lifecycle` dérivé `candidate / compiled / tested / validated` ; révision opérateur en source complète (`rule.operator_syntax_fix`, `revision_mode="full_source"`) : v2 `author=operator`, `derived_from=v1`, v1 reste Bridge ; l'approbation reste humaine et consigne automatiquement les lacunes restantes.
8. **Réponse Bridge** (lot T) : voir §6.

## 5. Garde-fous conservés

G2 / TLP / divulgation / `external_llm_allowed` / révocations contrôlés avant tout envoi ; idempotence et non-renvoi d'un POST ambigu (`needs_review`) ; empreintes et liens de provenance ; aucune approbation automatique ; refus du validateur YARA sur la structure (une seule règle, source complète, condition référençant des motifs mesurés, pas de condition sur hash ou taille seule, pas d'import .NET seul). Seul le refus `or_branches_disguised_as_multiple_rules_forbidden` est classé **consultatif** : il reste visible sur la candidate et bloque `validated` / approbation.

## 6. Tolérance aux réponses Bridge (lot T)

- **Lien précoce :** dès la réception de la réponse, avant tout parsing, un enregistrement idempotent (`append_if_absent`) lie réponse brute (blob + SHA-256), invocation, références de dossier / prompt et horodatages. Un échec d'écriture de ce lien après une réponse réussie donne `needs_review` (jamais « non envoyé ») et un rejeu ne renvoie pas de POST.
- **Champs facultatifs :** un `measurements` ou une annotation d'assertion mal formés sont ignorés avec `input_warnings` visibles ; la consultation continue avec les **seules mesures canoniques vérifiées** (celles qu'enverrait un sélecteur omis, dans l'enveloppe déjà autorisée). Les éléments invalides ne deviennent jamais des preuves. Les sélecteurs de portée (`evidence_refs`, `sample_sha256`, `finding_refs`, unités éditoriales, cible d'avis) et les références explicites inaccessibles restent des refus durs.
- **Prose :** une règle YARA ou Suricata dans une réponse libre, sans référence ni en-tête `advisor-wire`, donne une source extraite + SHA-256 (candidate non testée). Les fences d'un autre langage sont ignorées ; une règle recopiée à l'identique depuis le dossier est marquée `echoed_from_dossier` et n'est pas attribuée à Bridge ; plusieurs règles différentes restent non attribuées. Une critique ou un avis d'attribution ambiguë reste un résumé consultatif non attribué (plus de duplication par unité, aucune attribution par rang).
- **Statut :** `consult_status` dérivé (`received → dossier_preparing → sent_to_bridge → response_received → response_persisted`, `error`, `ambiguous`). Priorité : ambigu > erreur > réponse persistée (observation de réponse ou `manifest_persisted`) > réponse reçue (blob brut) > envoyé > préparation > reçu.
- **Test d'acceptation de bout en bout** (`backend/tests/test_bridge_prose_to_candidate_e2e.py`, transport fictif) : réponse en prose avec bloc YARA → réponse brute inchangée (SHA égal) → source extraite → `rule.propose_bridge` → candidate `author=bridge` avec lacune pour la string non mesurée → compilation → `rule.test` → non approuvée. Variante Suricata en prose sans capture : conservée, provenance Bridge, non testée.
- Le test `test_yara_draft_preserves_claims_and_sends_only_verified_offsets` (en échec sur `main`) attendait un seul appel de transport alors que la requête à `measurements` mal formé est désormais envoyée avec repli ; l'attente et l'assertion de motif ont été alignées.

## 6 bis. Vérifications effectuées (environnement orchestrateur)

| Contrôle | Résultat |
|---|---|
| Suite unitaire complète (hors `integration` et `workflow_emulator`), mono-processus | 5 764 réussis, 36 échecs, 3 skips. Les 36 échecs sont **tous** dans la liste de `main` (37 échecs : 34 Typst, `test_publication_detection_band`, `test_ioc_table`, `test_editorial_figures`, golden de prompt) ; un échec de `main` (`test_yara_draft_preserves_claims_and_sends_only_verified_offsets`) est corrigé ; **aucune nouvelle régression**. |
| `ruff check`, `ruff format --check` sur les fichiers modifiés | OK |
| `mypy src` (429 fichiers) | OK |
| Tests du relais (`unittest`) | 6 OK |
| Revues critiques indépendantes (Luna xhigh, lecture seule) | Y : APPROVE_WITH_FIXES → 4 correctifs. P : APPROVE_WITH_FIXES (1 HIGH cache de politique de sample périmé, 1 MEDIUM clé parent / enfant) → corrigés. T : REJECT (2 MEDIUM, 1 LOW) → provenance de la prose et statut corrigés ; le repli sur les mesures vérifiées est conservé par décision (mission §5.5) et prouvé par tests. |

Les régressions que les agents de développement n'avaient pas vues (environnement sans `pefile` / moteur YARA) ont été trouvées par comparaison systématique avec la liste d'échecs de `main` : réservation du gros prompt (P), rejeu du pont de signature (Y), refus d'un brouillon à annotation invalide et statut compact (T).

Skips de garde « dépendance indisponible dans le sandbox » ajoutés par les agents : retirés.

## 7. Non réalisé / reste à faire

- **Aucune consultation Bridge réelle, aucun redéploiement, aucun redémarrage du relais** : non autorisés dans cette session. Le critère « résolu » exige donc une consultation réelle après déploiement (`RULE_WORKBENCH_ENABLED=true` à l'export, cf. mémoire M12-Q) avec lecture de `stage_timestamps_utc`, `reservation_cost_ms` et du journal du relais.
- **Contrat asynchrone** (soumission → invocation `pending` acceptée → statut → récupération, recherche par clé d'idempotence) : non implémenté. C'est le préalable à toute séparation des voies de POST du relais. Dimensionner après mesure : si la préparation tombe à quelques secondes, la durée restante est celle de Bridge (~5 min mesurées).
- Test de course PostgreSQL de l'import idempotent non exécuté (aucun Postgres dans les environnements de travail) : il repose sur l'unicité existante `(candidate_id, version)`.
- Suite d'intégration PostgreSQL et workflow emulator non exécutées.
- Échecs préexistants sur `main` (37, essentiellement Typst) inchangés et hors périmètre.
