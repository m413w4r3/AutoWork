# Mission 04 — Contre-revue indépendante de L20

**Verdict : APPROVED**

**Base :** `e120a36f1afa08261fdf0276e0200872973ad8f2`
**Révision précédente :** `9d87fed7f49bb5920f1bd1cd2b1b00d9a8818e70`
**HEAD contrôlé :** `d2a2029fbcfb348200280f0116d5cbf19d1834a9`

## Findings

Aucun finding bloquant sur cette correction.

### LOW — le conflit du ref d’observation source n’a pas d’oracle permanent dédié

Les nouveaux tests vérifient la collision du ref propre `measurement`/`fact` et la conservation des deux identités dans le catalogue (`backend/tests/test_advisor_consultation.py:96-143`). Ils ne testent pas séparément une collision où le SHA du ref propre est identique, mais le SHA du ref `observation` associé diverge. J’ai exécuté ce contrôle sur une copie sous `/tmp` pour `measurement` et `fact` : les deux retournent `ref_reference_conflict`. Le code est correct dans ces scénarios ; un test permanent dédié éviterait une régression future.

## Vérifications adversariales

- **Collision initiale corrigée :** `_identity_conflicts` appelle maintenant `_manifest_refs` pour le manifeste brut et le manifeste calculé (`advisor_consultation.py:239-247`). `_manifest_refs` enregistre séparément le ref propre `(measurement|fact, id, sha256)` et le ref source `(observation, evidence_id, evidence_sha256)` (`:371-382`). Les contrôles synthétiques sur copie `/tmp`, sans ligne `measurements` ou `network_facts`, ont refusé `measurement:m001` et `fact:nf001` lorsque le manifeste avait le SHA `a…a` et `transmitted_refs` le SHA `b…b`. Aucun SHA n’est choisi ou écrasé.
- **Refs d’observation séparées :** avec SHA du ref propre égal (`a…a`) mais SHA du ref d’observation du manifeste `c…c` et du ref transmis `d…d`, les cas measurement et fact ont aussi refusé avec `ref_reference_conflict`.
- **Catalogue et alias :** le catalogue conserve les refs du projection, des deux manifestes et des refs transmis (`:256-295`). Les contrôles positifs ont conservé les identités propres et sources measurement/fact. Un alias issu seulement du manifeste brut n’a pas résolu. Un doublon légitime measurement/fact avec SHA cohérents a été accepté ; l’alias `E1` a résolu seulement vers la ref observation transmise exacte et son `expected_kind` reste appliqué. Les tests couvrent absent, restreint, stale, mauvais kind, UUID d’opinion nu et cible review versionnée exacte (`test_advisor_consultation.py:146-229`). Le test dédié confirme qu’un alias pointant vers deux refs transmises distinctes ne résout pas (`:249-258`).
- **Faux conflits :** les doublons cohérents de projection/manifeste/refs transmises sont dédupliqués en refs, sans refus. Les contrôles synthétiques avec une vraie ligne measurement et une vraie ligne `network_facts` ont aussi été acceptés avec leurs digests canoniques et leurs refs parent exactes.
- **Zéro I/O :** le test renforcé piège `builtins.open`, `io.open`, `os.open`, `Path.open`, `Path.resolve`, `socket.create_connection`, `socket.socket.connect`, `subprocess.run`, `subprocess.Popen` et `get_settings`; il appelle `prepare` sur un `MappingProxyType` (`test_advisor_consultation.py:293-310`). Il passe. L’inspection du chemin ne montre aucun accès disque, réseau, processus ni settings global. `Settings(_env_file=None)` reste explicite.
- **Branchement et autorité :** aucune référence au module advisor consultation n’existe dans `backend/src` hors de sa définition propre ; `prepare` reste inutilisé par les callers. Le diff de base ne modifie aucun fichier préexistant : il contient seulement des créations. Le delta `9d..d2` modifie les trois fichiers propres au lot (module, tests, notes) et ajoute le rapport R1 déjà produit lors de la revue précédente ; aucun autre module runtime n’est touché. Aucun champ d’autorisation/capability, budget, dispatch ou transport n’a été ajouté ; aucune migration, persistance, archive, parse/reparse, avis ou batching n’a été modifié.
- **Précondition dotenv :** elle demeure explicitement documentée (`advisor_consultation.py:145-162`, `M04_L20_NOTES.md:49,78-86`) : le caller doit appliquer le secret guard avec ses settings runtime avant `prepare`; cette fonction n’ouvre pas `.env`. Le module restant non branché, aucun chemin caller n’a changé.

## Commandes et résultats

- `git rev-parse HEAD` → `d2a2029fbcfb348200280f0116d5cbf19d1834a9`.
- `git diff --name-status e120a36f1afa08261fdf0276e0200872973ad8f2..d2a2029fbcfb348200280f0116d5cbf19d1834a9` → quatre créations, aucun fichier existant modifié. `M04_L20_REVIEW.md` est le rapport R1 déjà remis et inclus dans cette révision.
- `rg -n 'from cti_app\.application\.advisor_consultation|import cti_app\.application\.advisor_consultation|advisor_consultation\.prepare' backend/src` → aucun caller.
- `git diff --check e120a36f1afa08261fdf0276e0200872973ad8f2..d2a2029fbcfb348200280f0116d5cbf19d1834a9` → aucun problème.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py -q --tb=short` → **16 passed**.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py tests/test_advisor_completeness.py tests/test_advisor_wire.py tests/test_publication_secret_guard.py tests/test_investigation_advisor_tools.py -q --tb=short -k 'not archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers'` → **168 passed, 1 deselected**.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 ruff check src/cti_app/application/advisor_consultation.py tests/test_advisor_consultation.py` → `All checks passed!`.
- `cd backend && UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 ruff format --check src/cti_app/application/advisor_consultation.py tests/test_advisor_consultation.py` → `2 files already formatted`.
- Contrôles négatifs et positifs additionnels exécutés uniquement sur des copies de `backend/src/cti_app` sous `/tmp`; les résultats sont détaillés ci-dessus. Aucun fournisseur, Bridge, PostgreSQL, MinIO, Docker ou service CTI n’a été appelé. Aucun secret/runtime/cache interdit n’a été inspecté.

## Risque résiduel et rollback

Le seul écart relevé est l’absence d’un test permanent spécifique au conflit de SHA du ref d’observation source ; le contrôle sur copie passe pour measurement et fact. Pour une intégration future, le caller devra préserver le filtrage secret avec ses settings runtime avant `prepare`. Le module est toujours dormant et aucun état persistant n’a été ajouté.

Rollback : supprimer le module, son test et les notes L20 pour annuler le lot ; supprimer ce rapport pour annuler uniquement l’artefact de contre-revue. Aucun commit n’a été créé pendant cette contre-revue.
