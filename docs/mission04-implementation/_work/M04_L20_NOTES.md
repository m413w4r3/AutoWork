# Mission 04 — L20 `advisor_consultation.prepare`

## État et périmètre

- Base demandée : `e120a36f1afa08261fdf0276e0200872973ad8f2`.
- Lot : module pur `prepare`, expand-only, laissé non branché.
- Aucun commit, push, merge, déploiement ou appel Bridge, MinIO, fournisseur ou CTI.
- Aucun changement de persistance, migration, wire output, parse/reparse, batching ou avis.
- `prepare` n'accorde aucune autorisation G2/TLP, ne consomme aucun budget et n'archive ni n'envoie les octets.

## API ajoutée

Dans `backend/src/cti_app/application/advisor_consultation.py` :

- `prepare(projection: Mapping[str, Any]) -> PreparedConsultation | PreparationRefusal`
- `SourceRef(kind, id, sha256)`, dataclass immuable. Le SHA doit être en hexadécimal minuscule sur 64 caractères. Un `opinion` exige un identifiant exact `opinion:<uuid>:vN:<sha256>:<block>`.
- `SourceRefCatalog(refs, transmitted_refs, aliases, review_targets=())`, dataclass immuable. Les conflits `(kind, id)`/SHA font lever `ValueError`; seuls les refs transmis ou les cibles de review versionnées explicitement portées résolvent un alias. Les alias ambigus sont supprimés.
- `SourceRefConflict(kind, id, sha256s)` porte les collisions observées par `prepare`.
- `PreparedConsultation` porte `prompt_utf8`, `prompt_sha256`, `source_catalog`, texte/hash/taille/manifeste du dossier, préflight de complétude et nombre de redactions.
- `PreparationRefusal` porte `reason_code`, résultat de complétude quand disponible, nombre de redactions et collisions.

Les refus observables incluent `invalid_projection`, `secret_field_remaining`, `ref_reference_conflict` et `evidence_preflight_<status>`.

## Règles de préparation

- Réutilisation de `build_advisor_dossier`, `redact_operational_secrets`, `contains_operational_secrets`, `evaluate_advisor_evidence_preflight` et du renderer `_prompt` existant.
- La collision compare les SHA valides issus des refs du projection (dont `transmitted_refs`), du manifeste brut fourni et du manifeste généré. Une identité avec plusieurs SHA refuse sans choisir silencieusement l'une des valeurs.
- L'alias ordinaire doit correspondre au triplet exact d'un ref présent dans `transmitted_refs`. Un ID isolé ou un ref seulement présent dans le manifeste n'est jamais résolu. Le `expected_kind` est appliqué à la résolution.
- Les entrées de `disclosure_restricted_refs` et `disclosure_restricted_finding_refs` suppriment les alias correspondants, y compris lorsqu'une entrée `transmitted_refs` les annonce. Les formes UUID utilisées par le caller et les identifiants préfixés sont comparés.
- L'exception review ne s'applique qu'à une cible de dossier `versioned_review_target` dont UUID stable, version, SHA, block type et `source_ref` exact concordent. La résolution retourne le `source_ref` complet, jamais l'UUID stable seul.
- Le texte de prompt est encodé directement en UTF-8, sans normalisation de CRLF ni d'Unicode; le SHA-256 est calculé sur ces octets.
- Les résultats, catalogues, manifestes et cartes de complétude retournés sont figés ou récursivement immuables.

## Frontière secret et absence d'I/O

Le contrat de `prepare` exige une projection déjà disclosure-filtrée et secret-filtrée par son caller. Le code existant `InvestigationAdvisorTool.execute` applique le secret guard avec ses paramètres runtime avant de construire le dossier. `prepare` réapplique le guard pour une défense supplémentaire, avec `Settings(_env_file=None)` : aucun chargement de dotenv. Les `Path` de configuration sont convertis en chaînes avant le guard pour éviter `Path.resolve`; aucune donnée de chemin n'est ouverte.

Conséquence documentée : une valeur opaque présente uniquement dans `.env` ne peut pas être redécouverte par ce module sans violer l'interdiction de lire ce fichier. Le caller doit donc avoir exécuté le guard avec ses paramètres runtime avant de fournir `projection`. Un appelant qui contournerait cette précondition pourrait laisser passer un tel texte; le module reste non branché dans ce lot.

Un premier essai de l'implémentation a appelé le `get_settings()` par défaut du guard, configuré pour charger `.env`. J'ai retiré ce chemin après l'avoir identifié; je n'ai pas inspecté ni affiché de contenu `.env`. L'oracle final bloque `get_settings`, `open`, `Path.open`, `Path.resolve`, la création de connexions réseau et `subprocess.run`.

La fonction ne persiste rien et n'appelle ni DB, transport, archive, Bridge, MinIO ou fournisseur. `rg` confirme qu'il n'existe aucun appel de production à `prepare` dans ce lot.

La revue indépendante de Mission 04 doit encore statuer sur la fermeture des deux points catalogue/secret. Point à vérifier par le reviewer : le préfiltrage runtime existant et l'interdiction de lire dotenv font de la détection des valeurs dotenv-only une précondition du caller pour toute future intégration, pas un blocage de cette tranche dormante.

## Preuve RED puis GREEN

Cache uv isolé sous `/tmp/autowork-m04-l20-uv-cache`, car le cache utilisateur par défaut est en lecture seule.

1. Test initial écrit avant le module :

   ```text
   UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py -q --tb=short
   ```

   RED à la collecte : `ModuleNotFoundError: No module named 'cti_app.application.advisor_consultation'`; aucun corps de test exécuté.

2. Après ajout du module, les oracles indépendants de construction directe ont été ajoutés avant leur correction :

   ```text
   UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py -q --tb=short
   ```

   RED : `2 failed, 9 passed`; échecs sur collision du constructeur public et secret opaque fourni par le settings-source oracle.

3. Après le correctif du filtre de restriction, l'oracle avec ref marqué restreint et transmis a été exécuté avant le correctif :

   ```text
   UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py::test_restricted_projection_ref_is_not_resolvable_even_if_marked_transmitted -q --tb=short
   ```

   RED : `1 failed`; l'alias restreint résolvait encore.

4. GREEN final ciblé :

   ```text
   UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py -q --tb=short
   ```

   `11 passed`.

## Régressions et formatage

Commande advisor demandée :

```text
UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py tests/test_advisor_completeness.py tests/test_advisor_wire.py tests/test_publication_secret_guard.py tests/test_investigation_advisor_tools.py -q --tb=short
```

Résultat : `163 passed, 1 failed`. L'échec hors portée L20 est `test_archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers` : le test attend `C01_*.input.txt` dans `.autowork-runs/run12-editorial-2026-10-20/bridge-comms/corpus`, mais aucun fichier correspondant n'est présent (`StopIteration`). Je n'ai pas ouvert ni lu le contenu de cet emplacement.

Même commande, avec uniquement ce test exclu :

```text
UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 pytest tests/test_advisor_consultation.py tests/test_advisor_completeness.py tests/test_advisor_wire.py tests/test_publication_secret_guard.py tests/test_investigation_advisor_tools.py -q --tb=short -k 'not archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers'
```

Résultat final : `163 passed, 1 deselected`.

Formatage/lint finaux sur les deux fichiers de code :

```text
UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 ruff check src/cti_app/application/advisor_consultation.py tests/test_advisor_consultation.py
```

`All checks passed!`

```text
UV_CACHE_DIR=/tmp/autowork-m04-l20-uv-cache uv run --python 3.12 ruff format --check src/cti_app/application/advisor_consultation.py tests/test_advisor_consultation.py
```

Résultat : `2 files already formatted`.

## Fichiers et rollback

- Créés : `backend/src/cti_app/application/advisor_consultation.py`, `backend/tests/test_advisor_consultation.py`, ce fichier de notes.
- Aucun fichier existant modifié.
- Rollback local : supprimer ces trois fichiers; aucun état persistant ni migration ne doit être annulé.
- Aucun commit n'a été créé.
