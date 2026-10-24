# Mission 04 / Phase 2 — Résultats des lots

**Base d'entrée :** `e120a36f1afa08261fdf0276e0200872973ad8f2`
**Branche d'intégration locale :** `mission04/implementation`
**SHA d'intégration code/tests de la branche :** `e8b80c5d35e2c14a23cb521e9d899f1301a15056` (le commit documentaire final lui est postérieur)
**Nature :** code et tests locaux ; aucun push, merge sur `main`, déploiement,
appel fournisseur, run CTI, migration ou mutation live.

## 1. L1 — P10, credential JSON échappé : `PARTIAL`, NON INTÉGRÉ

| Élément | Valeur |
|---|---|
| Branche | `mission04/l1-p10-escaped` |
| SHA final de branche | `b9115d0ec2bbd8d78cbd5aed8ad84144382b4a7b` |
| Intégration | **non** (revue R4 `REVISE`, condition d'arrêt L1) |
| Fichiers de la branche | `backend/src/cti_app/application/invariant_proposals.py`, `backend/tests/test_invariant_proposals.py`, `docs/mission04-implementation/_work/M04_L1_NOTES.md` |
| Tests sur la branche | P10 : **83 passed** ; groupe ciblé : **20 passed** ; Ruff check/format verts |
| Performance | 25 613 caractères / 1 600 labels non fermés : **0,0043 s** normalisation→rendu |
| Version P10 | `P10_PROMPT_VERSION = "4"` inchangée ; `NEEDS_DECISION_P10_VERSION` ouvert |

### Itérations et RED/GREEN

1. **Correctif initial** (`895a24fd`) : matcher d'affectation échappée avec
   guillemets optionnellement précédés d'un antislash. RED : le credential
   `{\"auth\": \"FIXTURE_ESCAPED_AUTH\"}` apparaissait dans le prompt rendu
   (`1 failed`). GREEN : `59 passed`. Revue R1 `REVISE` : espaces et
   guillemets internes laissaient un suffixe ; le délimiteur fermant échappé
   était corrompu.
2. **Réparation 1** (`e0d1fb6d`) : matcher dédié consommant le scalaire
   complet. RED : `3 failed` sur les valeurs espacées/échappées et le
   délimiteur. GREEN : `64 passed`. Revue R2 `REVISE` : collision avec le
   préfixe littéral `[REDACTED]`, coût quadratique mesuré (751 ms puis
   2 969 ms quand la taille doublait), perte du délimiteur pour une valeur
   finissant par un antislash.
3. **Réparation 2** (`0f5371e0`) : scanner linéaire monotone, pré-index des
   fermetures et terminateurs. GREEN : `67 passed`. Revue R3 `REVISE` :
   fermeture prématurée sur un guillemet interne suivi de `}`/`]` (fuite du
   suffixe), et perte d'une preuve CVE sur une clé adjacente mal formée.
4. **Réparation 3** (`b9115d0e`) : règle de parité modulo 4 (guillemet
   structurel ≡ 1 mod 4, guillemet interne échappé ≡ 3 mod 4), frontière
   virgule/terminateur, repli borné au prochain terminateur. RED : `6 failed`
   (guillemets internes, preuve après clé mal formée, terminateur). GREEN :
   `83 passed`, `20` ciblés, Ruff vert. Revue **R4 `REVISE`** : un claim non
   fermé suivi d'une newline puis d'une CVE déclenche encore le repli jusqu'à
   EOF et efface la preuve CTI adjacente (`prefix {\"auth\":\"FIXTURE_AUTH\nCVE-2025-31337: observed persistence`).

### Décision et condition de reprise

La condition d'arrêt explicite de L1 est déclenchée (« arrêt si le matcher
masque des preuves autorisées ») et la limite de trois réparations est
atteinte. Le lot n'est **pas intégré**. La branche est préservée pour une
mission ultérieure.

**Condition R4 pour reprendre** : borner le repli à une frontière de claim
fiable (LF/CRLF après l'extrait non fermé), couvrir le cas via
`_normalize_context` → `_render_p10_prompt`, et rendre observable toute
omission restante. Le correctif des fuites (guillemets internes, espaces,
marqueur, antislash final) est déjà couvert par 83 tests et peut servir de
base à cette reprise.

**Rollback** : la branche n'étant pas intégrée, aucun rollback n'est nécessaire
sur `mission04/implementation` ; supprimer la branche/worktree si elle est
abandonnée.

## 2. L2 — P10, refus terminal : `NARROW_FIXES_VERIFIED`, INTÉGRÉ

| Élément | Valeur |
|---|---|
| Base | `df9e091076cbfd3147690f184c5562d626d8ae66` (sans L1) |
| Branche | `mission04/l2-p10-refusal` |
| Commits | `cf226b5a`, `0ee3504c`, `5ab00368`, revue `81b59e2d` |
| Merge d'intégration | `e8b80c5d` |
| Fichiers | `backend/src/cti_app/application/invariant_proposals.py`, `backend/src/cti_app/domain/invariant_proposals.py`, `backend/tests/test_invariant_proposals.py`, notes et revues |
| Tests finaux | P10 : **69 passed** ; groupe ciblé : **12 passed** ; Ruff check/format verts ; mypy ciblé `Success` |
| Revue finale | R3 `APPROVED` |

### Contrat implémenté

- `ProposalOutcome` (`succeeded`, `partial`, `refused`) ajouté au domaine ;
  `outcome`, `refusal_reason` et `refusal_code` sont additifs sur
  `ProposalConversationResult` avec des valeurs par défaut compatibles.
- `_ParsedProposalResponse` porte le statut wire parsé explicitement ; le
  warning textuel n'est plus la source de décision.
- Un `STATUS: refused` est terminal, y compris s'il est répété après
  `succeeded` ou `partial` : aucun candidat n'est transmis à P09, sur le
  parcours frais **et** sur le replay, via le gate partagé
  `_process_response_for_p09`.
- `partial` sans refus conserve la transmission antérieure des candidats à P09.
- Un statut présent mais inconnu ou vide lève `ProposalOutputValidationError`
  avant P09. Un `STATUS` absent conserve la compatibilité historique
  `SUCCEEDED` + warning (documentée comme limite acceptée ; un candidat peut
  encore passer à P09).
- Les champs `REFUSAL_REASON`/`REFUSAL_CODE` sont prioritaires sur leurs alias
  `REASON`/`CODE` ; la première valeur non vide est retenue, et un conflit
  (même champ répété ou canonique/alias divergent) produit un warning
  observable dans les transformations.

### Itérations

- Initial (`cf226b5a`) : refus terminal frais/replay. RED : `4 failed,
  1 passed, 58 deselected` (candidat refusé transmis, y compris au replay).
  GREEN : `64 passed`, `6` ciblés. Revue R1 `REVISE` : statut manquant/inconnu
  accepté comme succès, champs de refus répétés mal sélectionnés.
- Réparation 1 (`0ee3504c`) : statut inconnu rejeté, statut absent documenté,
  première valeur non vide. RED : `2 failed, 1 passed, 64 deselected`.
  GREEN : `67 passed`, `9` ciblés. Revue R2 `REVISE` : conflit entre champ
  canonique et alias sans warning.
- Réparation 2 (`5ab00368`) : conflit canonique/alias observable. RED :
  `1 failed, 1 passed, 67 deselected`. GREEN : `69 passed`, `12` ciblés.
  Revue R3 `APPROVED`.

**Risque résiduel accepté** : un wire sans `STATUS` reste `SUCCEEDED` avec
warning et peut transmettre un candidat à P09 ; la revue signale aussi un
manque de tests permanents pour les alias identiques et le fallback, dont les
probes de service ont réussi.

**Rollback** : revert des commits `cf226b5a`, `0ee3504c`, `5ab00368` ; aucune
migration ni état persistant.

## 3. L3 — Oracle PostgreSQL réel de l'export d'audit : `NARROW_FIXES_VERIFIED`, INTÉGRÉ

| Élément | Valeur |
|---|---|
| Branche | `mission04/l3-audit-pg` |
| Commits | `84b8688c`, `600a317f`, revue `1b645ecb` |
| Merge d'intégration | `eea2c1f5` |
| Fichier | `backend/tests/integration/test_investigation_audit_export_postgres.py` (test-only) |
| PostgreSQL réel | **1 passed** sur `postgres-test` jetable (1,02 s ; 1,19 s et 1,30 s en revalidation) |
| Revue finale | R2 `APPROVED` |

### Couverture de l'oracle

- Seed via la vraie UOW/repositories : investigation cible et investigation
  étrangère, invocations versionnées, observations, findings versionnés,
  états, opinions versionnées, décision analyste, conversation pivot, tours
  et `ModelRun` liés.
- `read_investigation_audit_snapshot` exécute la transaction
  `REPEATABLE READ READ ONLY` ; `build_audit_bundle` et `write_audit_bundle`
  produisent `RUN_EVENTS`, `RUN_TRACE`, `RUN_INDEX`, `RUN_MANIFEST`.
- Assertions indépendantes sur IDs, comptes, versions, portées, hashes,
  omissions, ordre et reprise sans relecture SQL (capture de statements).
- Piège inter-investigations, référence absente, hash incohérent → payload
  `unavailable` explicite.
- Égalité exacte des ressources `RUN_INDEX`, y compris les artefacts
  `synthesis_artifact` et `base_publication_artifact`.
- Complétude `RUN_TRACE` par événement/version, y compris les deux versions
  d'opinion partageant le même ID.
- Perturbations de sensibilité déterministes (omission, mauvais scope, hash
  altéré, décision/cible/version de trace) qui échouent réellement.

### Itérations

- Premier run parental après écriture du test : `1 failed` (fixture
  `InvestigationAgenda` avec une seule question). Correction de fixture ;
  relance : **1 passed in 1.25s**.
- Revue R1 `REVISE` : décision analyste absente, `RUN_INDEX` vérifié par
  sous-ensemble, complétude `RUN_TRACE` insuffisante.
- Réparation : ajout décision, égalité exacte, complétude par version.
  Deuxième run parental : `1 failed` (attendu de ressources incomplet) ;
  correction de l'attendu ; relance : **1 passed in 1.02s**. Revue R2
  `APPROVED`.

**Limites** : le lecteur d'artefacts est synthétique pour le cas de hash
incohérent ; MinIO réel n'est pas prouvé. Aucun PostgreSQL live n'a été
touché.

**Rollback** : supprimer le fichier de test ; aucune modification de
production.

## 4. L20 — Advisor, `prepare` pur : `L20_PURE_PREPARE_VERIFIED`, INTÉGRÉ

| Élément | Valeur |
|---|---|
| Branche | `mission04/l20-advisor-prepare` |
| Commits | `9d87fed7`, `d2a2029f`, revue `f667369a` |
| Merge d'intégration | `df9e0910` |
| Fichiers | `backend/src/cti_app/application/advisor_consultation.py` (nouveau), `backend/tests/test_advisor_consultation.py` (nouveau), notes et revues |
| Tests finaux | L20 : **16 passed** ; régressions advisor : **168 passed / 1 deselected** ; Ruff vert |
| Revue finale | R2 `APPROVED` |

### API et garanties

- `prepare(projection) -> PreparedConsultation | PreparationRefusal`.
- `SourceRef(kind, id, sha256)` et `SourceRefCatalog` immuables ; SHA-256
  minuscule sur 64 caractères ; les opinions exigent un `source_ref` versionné
  complet (`opinion:<uuid>:v<n>:<sha>:<block>`).
- Conflit `(kind,id)` avec SHA différents → `ref_reference_conflict`, sans
  écrasement ; les identités propres des mesures et des faits, ainsi que
  leurs refs d'observation dérivées, sont contrôlées séparément.
- Alias limités aux refs réellement transmises ou à une cible de review
  versionnée explicitement typée ; aucune résolution par ID seul ; alias
  absents/restreints/stale/mauvais kind non résolus.
- `prompt_utf8` et `prompt_sha256` déterministes, Unicode et CRLF préservés
  sans normalisation silencieuse.
- Réutilisation de `build_advisor_dossier`, du secret guard et du préflight
  de complétude ; aucune autorisation G2/TLP/DNT, aucun budget, dispatch ou
  transport ; zéro I/O ; module dormant (aucun caller de production).

### Itérations

- Initial (`9d87fed7`) : RED par absence de module puis `2 failed, 9 passed`
  sur collision et secret ; GREEN `11 passed`. Revue R1 `REVISE` : collision
  de mesure/fait perdue par la projection d'observation.
- Réparation (`d2a2029f`) : ledger contrôlant l'identité propre et la ref
  d'observation, pièges zéro-I/O renforcés, test d'ambiguïté d'alias.
  RED `4 failed, 12 passed` ; GREEN `16 passed`, régressions `168 passed /
  1 deselected`. Revue R2 `APPROVED`.

**Risque résiduel** : la projection doit être préfiltrée avec les paramètres
runtime du caller ; une valeur opaque uniquement présente dans `.env` dépend
de ce préfiltrage, que `prepare` ne charge pas. La revue signale aussi un test
permanent à ajouter pour une collision portant uniquement sur le SHA de la ref
d'observation source (non bloquant).

**Rollback** : supprimer le module et ses tests ; aucun état persistant.

## 5. L6 — Reparse Suricata (lot optionnel) : APPROVED, INTÉGRÉ

| Élément | Valeur |
|---|---|
| Base | `df9e0910` |
| Branche | `mission04/l6-suricata-reparse` |
| Commits | `40f1ac27`, revue `d13ac067` |
| Merge d'intégration | `6db6b71c` |
| Fichiers | `backend/src/cti_app/application/investigation_advisor_tools.py`, `backend/tests/test_advisor_output_reparse.py`, `backend/tests/test_investigation_composition.py`, notes et revue |
| Tests | **18 passed** (reparse + composition) ; groupe ciblé **7 passed** ; Ruff/format/mypy ciblés verts |
| Revue finale | `APPROVED` avec risque P3 |

### Correctif borné

- `suricata_draft` ajouté aux deux ensembles d'admission du reparse
  persisté, sans toucher au writer, au transport Bridge, aux lecteurs de
  preuves ni aux autres types.
- Garde locale fail-closed : `reparse_context` présent, `transmitted_refs`
  identiques aux refs normalisées, `network_facts` non vides et complets
  (`fact_id`, `evidence_id`, `observation_sha256` valide, `field`, `value`),
  chaque fait lié à une observation transmise, `fact_id` non dupliqué.
  Contexte absent/incomplet ou refs discordantes → refus explicite avant
  `record_json`, zéro écriture.
- Tests : chemin valide avec sortie stockée vérifiée par hash, `fact_refs`
  résolus, refs conservées, zéro Bridge, aucune relecture des preuves sources
  au-delà de l'observation advisor et de son blob, une seule observation
  versionnée/idempotente ; contextes absent/incomplet et conflits → zéro
  recorder ; composition `operator_mode=True` expose `advisor.reparse`.
- Le runner worker standard (`operator_mode=False`) n'expose pas
  `advisor.reparse` ; cette limite est documentée et le mode worker n'est pas
  modifié dans L6.

**Risque résiduel P3** : avec un contexte complet mais une sortie Suricata
impossible à parser, le fallback générique ajoute `figure_plan=None` au lieu
d'une forme partielle Suricata dédiée ; le résultat reste non utilisable.
À traiter dans un scope séparé si un consommateur exige cette forme.

**Rollback** : revert de `40f1ac27` et `d13ac067` ; aucune migration ni état.

## 6. L5 — Complétude publication (optionnel) : NO_GO, NON IMPLÉMENTÉ

Le scout L5 conclut `NO_GO` pour le lot complet en test-only :

- l'oracle de complétude chronologie→PDF est réalisable sans changer la
  production ni le template ;
- le `pdf_lineage` actuel n'expose pas les IDs reviewer/approver ; les
  ajouter exige une modification de production et une décision éditoriale ;
- le bundle de 102 polices verrouillées est absent de `chpTypst/` ; Typst
  0.15.1 et Poppler sont disponibles.

Aucun fichier de code n'a été modifié. Reprise éventuelle : mission distincte
avec décision éditoriale sur le lineage et bundle de polices disponible.

## 7. L4 — Contexte full/paged : non lancé

`BLOCKED_CONTRACT` par construction : la sémantique de `findings` (versions
courantes vs toutes versions), l'ordre de l'`evidence_index` et l'identité
du curseur opérateur exigent une décision humaine documentée. Aucune
modification de code n'a été faite.

## 8. Rollback global

L'intégration est locale sur `mission04/implementation`. Pour revenir à la
base : `git reset --hard e120a36f` sur cette branche (les branches de lots
restent disponibles). Aucun état PostgreSQL, MinIO, provider ou fichier live
n'a été modifié par les lots.
