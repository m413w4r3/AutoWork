# Mission 04 — revue indépendante du lot L2

**Verdict : REVISE**

**Base :** `df9e091076cbfd3147690f184c5562d626d8ae66`

**Lot :** `cf226b5a94b390ff9ce4226c26862908324377c7` (`mission04/l2-p10-refusal`)
**Périmètre :** revue en lecture seule des fichiers de code, tests et note L2. Aucun provider, Bridge, DB ou MinIO n’a été appelé. Le seul fichier créé dans le dépôt est ce rapport.

Le gate commun protège bien les deux chemins contre un `STATUS: refused` explicite. Je recommande toutefois de corriger ou de trancher les deux cas ci-dessous avant d’approuver le lot : les statuts manquant/inconnu laissent passer un candidat en P09, et les métadonnées de refus répétées peuvent masquer une raison ou un code pourtant fourni.

## Findings

### MEDIUM — un statut manquant ou inconnu est accepté comme `SUCCEEDED`

Dans `_proposal_wire_payload`, `outcome` démarre à `SUCCEEDED`. Un statut absent ajoute seulement un warning; un statut inconnu ne déclenche même pas de warning. Le candidat est ensuite transmis à P09. Les contrôles scratch exécutés depuis `/tmp` à travers `ProposalConversationService.propose` ont observé, pour chacun des fils `STATUS` absent et `STATUS: unknown`, `outcome=succeeded`, `p09_calls=1` et un résultat invariant. Voir [invariant_proposals.py](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariant_proposals.py:1291) et [la validation existante du wire](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariant_proposals.py:1285).

Le format demandé par le prompt limite pourtant le statut à `succeeded`, `partial` ou `refused`. Cela maintient le comportement permissif historique pour une réponse sans refus explicite, mais laisse une sortie wire invalide aboutir à P09. Fixer explicitement le contrat : si l’absence et les valeurs inconnues doivent être invalides, les rejeter avec `ProposalOutputValidationError` avant P09; sinon, documenter cette compatibilité et la garder comme limite acceptée.

### MEDIUM — les champs de refus répétés ne sélectionnent que la première valeur

Le parseur lit uniquement le premier `REFUSAL_REASON` et le premier `REFUSAL_CODE`, puis utilise leur valeur générique ou le défaut s’ils sont vides. Un contrôle `/tmp` via `propose` avec des champs vides suivis de `REFUSAL_REASON: specific reason omitted` et `REFUSAL_CODE: supplied_code` a retourné la raison générique, aucun code, et zéro appel P09. Le refus reste terminal, mais son code fourni est perdu et son motif précis n’est pas visible. Voir [l’extraction des métadonnées](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariant_proposals.py:1304).

Définir une règle déterministe pour les répétitions (par exemple première valeur non vide et politique explicite en cas de valeurs contradictoires) et la couvrir. Le motif par défaut reste approprié si aucune valeur utile n’est fournie.

## Contrats vérifiés

- Le wire avec `STATUS: refused` et candidat retourne `ProposalOutcome.REFUSED`, garde raison/code, rend `invariant_results == ()` et n’appelle pas P09. Le refus sans candidat a le même outcome et zéro appel. Les tests traversent `ProposalConversationService.propose` et vérifient `_Registry.calls` : [tests P10](/home/nill/perso/work/AutoWork_m04_l2/backend/tests/test_invariant_proposals.py:1342).
- Toute occurrence `refused` l’emporte sur les statuts précédents. Le test vérifie `succeeded` puis `refused`; un contrôle `/tmp` traversant `propose` a aussi vérifié `partial` puis `refused` avec candidat : zéro appel P09 et résultat vide. Le replay d’un refus garde outcome, raison/code, effectue un seul appel conversationnel et aucun appel P09 : [test de replay refusé](/home/nill/perso/work/AutoWork_m04_l2/backend/tests/test_invariant_proposals.py:1441).
- `partial` sans refus continue d’appeler P09 une fois. Un candidat invalide sous un statut refusé conserve `ProposalOutputValidationError` et ne passe pas dans P09 : [tests ciblés](/home/nill/perso/work/AutoWork_m04_l2/backend/tests/test_invariant_proposals.py:1404).
- Le JSON typé avec un champ `status` de refus est rejeté par la validation stricte de `ProposalResponse`; un contrôle `/tmp` a observé `ProposalOutputValidationError`. Le JSON ne convertit donc pas silencieusement le refus en succès. Un candidat vide/mal formé dans un wire refusé est ignoré et le refus reste terminal.
- `ProposalOutcome` est un enum de domaine; `outcome`, `refusal_reason` et `refusal_code` sont additifs sur `ProposalConversationResult`. `ProposalResponse`, les IDs de candidat, le schéma/version wire et `P10_PROMPT_VERSION` restent inchangés. La liste du diff contient seulement les deux modules P10, leurs tests et `M04_L2_NOTES.md`; aucun fichier DB, provider ou migration.
- Le replay d’une réponse réussie appelle de nouveau `InvariantRegistryService.propose` dans le test existant (`_Registry.calls == 2`). Ce chemin P09 relit d’abord le résultat associé à une clé de proposition déterministe et retourne le résultat existant; le repository utilise une contrainte unique et `ON CONFLICT DO NOTHING` pour invariant/rejet ([service](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariants.py:119), [repository](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/infrastructure/database/repositories/invariants.py:197)). Ainsi, le replay réexécute le handoff pour réparer un résultat P09 manquant sans créer une seconde ligne canonique. C’est une vérification par source, pas un test PostgreSQL exécuté ici. Le replay d’un refus ne fait aucun handoff.

## Axes indépendants

### Standards

Aucune violation ferme des règles documentées n’a été relevée. Le nouvel état est typé et le gate reste dans la couche application. Possible odeur mineure, jugement seulement : les chemins frais et replay construisent séparément le même résultat étendu ([constructeur replay](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariant_proposals.py:240), [constructeur frais](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariant_proposals.py:289)). Le rapport indépendant de l’axe Standards n’a relevé aucun autre point.

### Spec

La revue indépendante limitée au contrat L2 écrit dans `IMPLEMENTATION_PLAN.md` n’a pas relevé de divergence sur le refus explicite, le refus sans candidat, le passage `partial` ou le résultat additif. Les deux findings ci-dessus proviennent des contrôles adversariaux supplémentaires demandés pour statut manquant/inconnu et champs répétés; ils dépassent les exemples du test d’acceptation de base.

## Commandes et résultats

Les commandes pytest/Ruff/mypy ciblées ont été lancées depuis `backend`, avec caches et artefacts Python désactivés ou redirigés vers `/tmp`.

- `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short` — **64 passed**.
- `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -k 'refused_wire_with_candidate or later_refusal_status_is_terminal_for_p09 or refused_wire_without_candidate or partial_wire_without_refusal or invalid_refused_wire or replay_of_refused_wire' -q --tb=short` — **6 passed, 58 deselected**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` — **All checks passed**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` — **3 files already formatted**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 mypy src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py` — **Success: no issues found in 2 source files**.
- Depuis la racine, `PYTHONDONTWRITEBYTECODE=1 MYPY_CACHE_DIR=/tmp/mypy-cache make typecheck-backend UV_SYNC=true UV_RUN='UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12'` — **échec global : 60 erreurs dans 13 fichiers hors lot**. Aucune erreur n’est signalée dans les trois fichiers modifiés. Cette vérification n’établit pas que ces erreurs préexistaient à la base; aucun typecheck global de la base n’a été exécuté. L’override évite que la cible `backend-sync` modifie `backend/.venv` pendant cette revue en lecture seule.
- `git diff --check df9e091076cbfd3147690f184c5562d626d8ae66..cf226b5a94b390ff9ce4226c26862908324377c7` — **aucune erreur**.
- Les contrôles scratch complémentaires ont été écrits dans `/tmp` et exécutés via `ProposalConversationService.propose`; aucun fichier test ou code du dépôt n’a été modifié.

## Risque résiduel et rollback

Le risque résiduel principal est qu’un wire P10 mal formé sans statut, ou avec statut inconnu, soit traité comme réussi et transmette un candidat à P09. Les répétitions ambiguës peuvent aussi perdre une raison/code précis. Le typecheck global demeure rouge hors lot; l’antériorité exacte de ces erreurs reste inconnue.

Rollback : retirer le commit `cf226b5a` (ou remettre ses changements aux fichiers listés dans le diff sur la base `df9e0910`). Aucun état DB, schéma, provider ou objet MinIO n’a été modifié par ce lot ou cette revue; aucune migration de rollback n’est nécessaire.
