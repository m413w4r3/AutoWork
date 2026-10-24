# Mission 04 — contre-revue indépendante L2 R2

**Verdict : REVISE**

**Base du delta :** `cf226b5a94b390ff9ce4226c26862908324377c7`

**HEAD revu :** `0ee3504cd085c2379323f258276b5d5d24ac2f1f`

**Base du lot :** `df9e091076cbfd3147690f184c5562d626d8ae66`

## Finding

### MEDIUM — les conflits entre les champs canoniques et leurs alias ne sont pas signalés

Le parseur détecte les valeurs distinctes répétées pour une même clé, mais les alias `REASON`/`CODE` ne sont inspectés que si `REFUSAL_REASON`/`REFUSAL_CODE` ne contiennent aucune valeur. Avec `REFUSAL_REASON: canonical reason` puis `REASON: alias reason` (et les deux champs de code également différents), le contrôle scratch via `ProposalConversationService.propose` conserve les valeurs canoniques et retourne zéro appel P09, mais n’archive aucun warning de conflit. Le court-circuit est visible dans [l’extraction des champs](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariant_proposals.py:1324).

La règle de R2 demande un warning observable pour des valeurs non vides contradictoires. Ici, les deux noms désignent les mêmes métadonnées de refus et le wire peut fournir les deux. Comparer les valeurs non vides canoniques et alias avant de retenir la valeur canonique, sans inclure leur contenu dans le warning, ou préciser dans les notes que les conflits entre alias sont hors de cette règle et acceptés.

## Vérifications adversariales

- `STATUS: unknown` lève `ProposalOutputValidationError` avant P09; le test de service vérifie `_Registry.calls == []` : [test](/home/nill/perso/work/AutoWork_m04_l2/backend/tests/test_invariant_proposals.py:1425).
- `STATUS:` présent mais vide produit aussi `ProposalOutputValidationError` et zéro appel P09 dans un contrôle scratch `/tmp`. Il n’existe pas encore de test dédié à la valeur vide.
- Un `STATUS` absent conserve explicitement la compatibilité `SUCCEEDED`, archive le warning `proposal status missing` et transmet le candidat une fois à P09; le test traverse `propose` : [test](/home/nill/perso/work/AutoWork_m04_l2/backend/tests/test_invariant_proposals.py:1404). La note documente ce choix [ici](/home/nill/perso/work/AutoWork_m04_l2/docs/mission04-implementation/_work/M04_L2_NOTES.md:13).
- Les champs répétés de même nom retiennent la première valeur non vide, signalent les contradictions sans révéler les valeurs et gardent P09 à zéro pour un refus : [test](/home/nill/perso/work/AutoWork_m04_l2/backend/tests/test_invariant_proposals.py:1443). Le fallback générique et `refusal_code=None` lorsque rien n’est fourni sont confirmés par un contrôle scratch.
- `partial` suivi de `refused` reste refusé et appelle P09 zéro fois dans un contrôle scratch à travers `propose`. Le test commité couvre `succeeded` puis `refused`; aucun test commité ne couvre encore le premier ordre.
- Refus avec/sans candidat, transmission d’un candidat `partial`, erreur de validation pour un candidat wire invalide, replay d’un refus et propagation de l’outcome/raison/code sont couverts par les tests existants. Le replay refusé fait un seul appel conversationnel et zéro appel P09 : [test](/home/nill/perso/work/AutoWork_m04_l2/backend/tests/test_invariant_proposals.py:1512).
- Le replay réussi rappelle le service P09 selon le comportement historique; la clé de proposition et le repository assurent l’idempotence canonique ([recherche du résultat existant](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariants.py:119), [clé déterministe](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/domain/invariants.py:330), [contrainte `ON CONFLICT`](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/infrastructure/database/repositories/invariants.py:197)). Aucun test PostgreSQL n’a été exécuté dans cette contre-revue.
- La réponse JSON typée conserve la validation stricte; un statut JSON supplémentaire reste une erreur de validation, sans conversion en refus. Le schéma/version wire, les IDs de candidat et `ProposalResponse` n’ont pas changé. Le diff du lot ne comporte ni changement DB, provider, migration ni modification de persistance.

### Résultat des axes indépendants

- **Standards :** aucune violation ferme documentée. Possible odeur mineure, jugement seulement : le statut validé reste comparé avec des chaînes avant de produire l’enum ([invariant_proposals.py](/home/nill/perso/work/AutoWork_m04_l2/backend/src/cti_app/application/invariant_proposals.py:1304)).
- **Spec :** la vérification indépendante confirme les cas du contrat L2 et relève comme lacunes de couverture les statuts vides et `partial` puis `refused`. Le finding de cette contre-revue concerne le conflit entre noms canoniques et alias, qui n’est pas exercé par le test des répétitions d’une même clé.

## Commandes et résultats

Toutes les commandes Python ciblées ont été exécutées depuis `backend`, avec cache et artefacts bytecode redirigés ou désactivés.

- `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short` — **67 passed**.
- `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -k 'refused_wire or later_refusal_status or missing_status or unknown_status_wire or repeated_refusal_fields or partial_wire_without_refusal' -q --tb=short` — **9 passed, 58 deselected**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` — **All checks passed**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py tests/test_invariant_proposals.py` — **3 files already formatted**.
- `MYPY_CACHE_DIR=/tmp/mypy-cache UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 mypy src/cti_app/application/invariant_proposals.py src/cti_app/domain/invariant_proposals.py` — **Success: no issues found in 2 source files**.
- Depuis la racine : `PYTHONDONTWRITEBYTECODE=1 MYPY_CACHE_DIR=/tmp/mypy-global-r2-cache make typecheck-backend UV_SYNC=true UV_RUN='UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12'` — **échec global : 60 erreurs dans 13 fichiers hors lot**, aucune dans les fichiers modifiés. Le même total avait été observé pendant la première revue, mais aucun typecheck de la base n’a été fait; leur antériorité exacte reste UNKNOWN. Les overrides évitent l’écriture dans `backend/.venv` pendant la revue.
- `git diff --check df9e091076cbfd3147690f184c5562d626d8ae66..0ee3504cd085c2379323f258276b5d5d24ac2f1f` — **aucune erreur**.
- Les contrôles scratch des cas non présents dans les tests commitées ont été exécutés depuis `/tmp`; aucun code ni test du dépôt n’a été modifié par cette revue.

## Risque résiduel et rollback

Les refus explicites, statuts vides/inconnus et répétitions d’un même champ respectent le comportement attendu. Le conflit entre un champ canonique et son alias reste silencieux, ce qui rend une partie des métadonnées ambiguës non observable. Le typecheck global reste en échec hors lot, sans preuve que ces erreurs précèdent la base.

Rollback : annuler `0ee3504c` pour revenir à `cf226b5a`; cela réintroduit les deux findings de la première revue et ne constitue donc pas un état intégrable. Aucun état DB, provider ou MinIO n’a été modifié; aucune migration de rollback n’est nécessaire.
