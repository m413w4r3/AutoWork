# Mission 04 — Revue indépendante de L20

**Verdict : REVISE**

**Base :** `e120a36f1afa08261fdf0276e0200872973ad8f2`
**Lot / HEAD contrôlé :** `9d87fed7f49bb5920f1bd1cd2b1b00d9a8818e70`
**Périmètre :** revue en lecture seule. Aucun fichier du lot n’a été modifié.

## Findings

### HIGH — une collision de SHA de mesure dans le manifeste échappe au refus

`backend/src/cti_app/application/advisor_consultation.py:368-371` convertit les entrées de manifeste `measurement` et `fact` en refs `observation`, en remplaçant `id`/`sha256` par `evidence_id`/`evidence_sha256`. Pour une mesure, cela omet l’identité propre `(measurement, measurement_id, sha256)` portée par le manifeste. Le dossier canonique conserve bien cette identité dans `kind`, `id` et `sha256` (`advisor_dossier.py:904-917`), et les refs transmises contiennent aussi les refs de mesure (`investigation_advisor_tools.py:4498-4501`).

**Scénario vérifié sur une copie temporaire sous `/tmp` :** le manifeste brut porte `measurement:m001` avec le SHA `a…a`, les refs transmises portent `measurement:m001` avec `b…b`, sans ligne `measurements` dans la projection. `prepare` retourne `PreparedConsultation`, sans refus. Le contrôle affichait `PreparedConsultation / no refusal / True False`. La conversion du manifeste ne voit que son ref d’observation associée et perd la collision de mesure.

Cela laisse passer une identité contradictoire et ne satisfait pas le premier oracle R7/L20. Le ledger doit contrôler le ref propre de mesure et, séparément, le ref d’observation source. Ajouter un cas avec cette forme de manifeste et exiger `ref_reference_conflict`.

### MEDIUM — l’oracle zéro-I/O couvre des primitives précises, pas toute la frontière

`backend/tests/test_advisor_consultation.py:222-235` exécute bien `prepare` sous des pièges actifs pour `builtins.open`, `Path.open`, `Path.resolve`, `socket.create_connection`, `subprocess.run` et `publication_secret_guard.get_settings`. Une assertion de ces pièges n’est pas absorbée par `prepare`. La lecture du chemin d’exécution ne montre pas d’appel direct à l’I/O ; `Settings(_env_file=None)` neutralise la source dotenv configurée dans `config.py:54-58`, et les guards reçoivent explicitement les settings.

L’oracle n’intercepte toutefois pas des voies équivalentes comme `io.open`, `os.open`, `subprocess.Popen` ou `socket.socket.connect`; il teste aussi un `dict`, alors que l’API accepte tout `Mapping`, dont les méthodes peuvent avoir des effets. Le test est réel mais ne prouve pas une absence absolue d’I/O face à ces voies. Compléter les pièges, ou réduire explicitement la frontière d’entrée aux projections de données intégrées et pures.

## Vérifications des critères

- **Périmètre confirmé :** `git diff --name-status base..lot` ne contient que trois créations : `backend/src/cti_app/application/advisor_consultation.py`, `backend/tests/test_advisor_consultation.py`, `docs/mission04-implementation/_work/M04_L20_NOTES.md`. Aucun fichier existant n’est modifié. `HEAD` correspond au SHA du lot et `git status --short` était vide avant la création de ce rapport.
- **Non-branchement confirmé :** recherche dans `backend/src/` ne trouve que la définition de `prepare` et son appel interne au renderer `_prompt`. Aucun caller ne branche `prepare` dans `execute`. Le diff n’ajoute ni archive/envoi, persistance, migration, parse/reparse, avis ou batching.
- **Pureté et dotenv :** aucun appel de fichier, résolution de chemin, settings global, connexion ou subprocess n’est présent dans le chemin `prepare`. `Settings(_env_file=None)` remplace le `.env` par défaut de la configuration. Les secrets uniquement disponibles par dotenv devront rester filtrés par le caller avant toute future intégration ; cette précondition est indiquée dans le module et les notes. Aucun log de secret n’est ajouté.
- **Catalogue de refs :** le SHA est vérifié en hexadécimal minuscule ; les conflits détectés refusent sans écrasement ; les alias ordinaires sont limités aux refs transmises exactes ; les alias absents, restreints, stale et de mauvais kind ne résolvent pas dans les cas testés. La cible d’opinion de review est versionnée par `source_ref` complet, et le test vérifie qu’un UUID nu ne résout pas. L’ambiguïté d’alias est supprimée par `SourceRefCatalog.__post_init__` (`advisor_consultation.py:88-103`), mais aucun test dédié ne couvre cette branche.
- **Octets :** le SHA est calculé sur `prompt_utf8` retourné (`:200-222`). Le test garde Unicode et CRLF, compare des appels répétés, vérifie les octets originaux et recalcule le SHA sur les octets émis ; aucune normalisation de newline n’est visible.
- **Réutilisation et preuves :** le module délègue au dossier, au secret guard et au préflight existants (`:169-203`) et transporte la complétude, le manifeste et le compte de redactions. Il n’introduit ni plafond, ni redaction générale, ni allowlist de format CTI. Le type préparé ne contient aucun booléen G2/TLP/DNT, capability, budget, dispatch ou transport ; il n’accorde donc pas d’autorisation.
- **Renderer privé :** `prepare` appelle directement `InvestigationAdvisorTool._prompt` (`:196-200`), donc la forme du prompt provient bien du renderer existant (`investigation_advisor_tools.py:2855-2947`). Pour une tranche dormante sans caller, ce couplage est acceptable et visible ; il reste dépendant d’une méthode privée et doit être réévalué au cutover ou à l’extraction d’un renderer partagé.

## Commandes et résultats

- `git rev-parse HEAD` → `9d87fed7f49bb5920f1bd1cd2b1b00d9a8818e70`.
- `git diff --name-status e120a36f1afa08261fdf0276e0200872973ad8f2..9d87fed7f49bb5920f1bd1cd2b1b00d9a8818e70` → exactement les trois créations listées ci-dessus.
- `git diff --check e120a36f1afa08261fdf0276e0200872973ad8f2..9d87fed7f49bb5920f1bd1cd2b1b00d9a8818e70` → aucun problème.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py -q --tb=short` → **11 passed**.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py tests/test_advisor_completeness.py tests/test_advisor_wire.py tests/test_publication_secret_guard.py tests/test_investigation_advisor_tools.py -q --tb=short -k 'not archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers'` → **163 passed, 1 deselected**. Le replay du corpus exclu n’a pas été exécuté ; aucune donnée locale n’a été ouverte.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 ruff check src/cti_app/application/advisor_consultation.py tests/test_advisor_consultation.py` → `All checks passed!`.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 ruff format --check src/cti_app/application/advisor_consultation.py tests/test_advisor_consultation.py` → `2 files already formatted`.
- Aucun fournisseur, Bridge, PostgreSQL, MinIO, Docker ou service CTI n’a été appelé. Aucun secret/runtime/cache interdit n’a été inspecté.

## Risque résiduel et rollback

Le défaut de collision de mesure doit être corrigé avant d’approuver L20. L’oracle zéro-I/O peut être renforcé indépendamment ; aucun appel I/O actuel n’a été observé dans le chemin source. L’exception de review est bien versionnée, et le renderer privé n’introduit pas de second format tant que le module reste non branché.

Rollback du lot : supprimer les trois fichiers créés par L20. Aucun état persistant, migration, archive ou envoi n’a été ajouté ; aucun commit n’a été créé.
