# Lot C2 — preuves de suppression du code mort

Date : 12 octobre 2026. Worktree : `worktree/brave-stream`, réinitialisé sur
`chantier/pre-pivot` (`0371486`). Le fichier d'audit préalable est
`docs/design/2026-10-12-dead-code-audit.md` ; la liste examinée est
`/tmp/aw-chantier/dead-candidates-non-api.txt`.

Les décisions ci-dessous combinent le graphe AST, les registres et contrats,
les chemins de test et les règles de conservation du lot. Une absence textuelle
seule n'a jamais motivé une suppression.

## Méthode de preuve

Le locator `ctx.py` n'a pas pu utiliser son index dense (`numpy` absent du
cache hors ligne). Après reformulation, son mode lexical a rendu des extraits
liés aux erreurs du gateway ; ils ont été lus. L'analyse de références a ensuite
été faite sur le code courant avec Python 3.12 et l'AST de la bibliothèque
standard : 516 fichiers Python analysés, zéro erreur de parsing. Le parcours
compte définitions, noms, attributs, imports et alias, ainsi que les constantes
chaînes exactes. Il couvre `backend/src/cti_app`, `backend/tests`,
`backend/tools`, `scripts` et `infra`, en ignorant les caches interdits.

Commande de parcours AST exécutée avant suppression :

```sh
UV_CACHE_DIR=/tmp/aw-chantier-ctx-cache uv run --offline --no-project --no-sync --python 3.12 python - <<'PY'
import ast
from pathlib import Path

names = '''accepted_for_publication analyst_handoff_policy_from_sources assert_adapter_contract canonical_provenances claim_is_machine_verified ExtractionResult format_french_date format_french_month hash_bundle_files _ImageElementParser indicator_is_machine_verified is_current_source_checkpoint mapping_to_tuple MeasuredGoodwareCount _parse_proposal_response _persisted_references sample_features_from_vt_view _source_figure_candidates StructuredModelUnavailableError _target_value_for_display validate_features worker_probe AnalystPostSynthesisService collect_indicators compute_font_bundle_version compute_template_bundle_hash _data_uri diagram_semantic_sha256 DramatiqAnalysisJobDispatcher EvidenceExtractionService FilesystemBlobStore InMemoryModelOutputStore load_stage loop_budget_from_settings normalize_discovery_output ProposalConversationService resolve_font_bundle_paths SourceEvidenceProcessingService SynchronousJobDispatcher UnsupportedDiagramStructureError validate_synthesis VirusTotalSampleAcquisitionService'''.split()
name_set = set(names)
roots = [Path('backend/src/cti_app'), Path('backend/tests'), Path('backend/tools'), Path('scripts'), Path('infra')]
files = [p for root in roots if root.exists() for p in root.rglob('*.py')
         if not any(x in {'.git', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules', 'var'} for x in p.parts)]
refs = {name: [] for name in name_set}
errors = []
for path in files:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        errors.append((str(path), type(exc).__name__))
        continue
    for node in ast.walk(tree):
        ident = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ident = node.name
        elif isinstance(node, ast.Name):
            ident = node.id
        elif isinstance(node, ast.Attribute):
            ident = node.attr
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                for value in (alias.name, alias.asname):
                    if value in name_set:
                        refs[value].append((str(path), getattr(node, 'lineno', '?'), 'import'))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            ident = node.value
        if ident in name_set:
            refs[ident].append((str(path), getattr(node, 'lineno', '?'), type(node).__name__))
print('parsed_files=', len(files), 'parse_errors=', len(errors))
for name in names:
    print(name, refs[name])
PY
```

For module/import strings, dynamic lookups and non-Python wiring, these exact
name scans covered source, tests, tools, `frontend`, `scripts`, `infra`, `docs`,
`chpTypst`, `Makefile`, compose files and `pyproject.toml`:

```sh
rg -n '\b(accepted_for_publication|analyst_handoff_policy_from_sources|assert_adapter_contract|canonical_provenances|claim_is_machine_verified|ExtractionResult|format_french_date|format_french_month|hash_bundle_files|_ImageElementParser|indicator_is_machine_verified|is_current_source_checkpoint|mapping_to_tuple|MeasuredGoodwareCount|_parse_proposal_response|_persisted_references|sample_features_from_vt_view|_source_figure_candidates|StructuredModelUnavailableError|_target_value_for_display|validate_features|worker_probe)\b' backend/src backend/tests backend/tools scripts infra docs Makefile pyproject.toml docker-compose.yml compose.yml 2>/dev/null
rg -n '\b(collect_indicators|compute_font_bundle_version|compute_template_bundle_hash|_data_uri|diagram_semantic_sha256|DramatiqAnalysisJobDispatcher|EvidenceExtractionService|FilesystemBlobStore|InMemoryModelOutputStore|load_stage|loop_budget_from_settings|normalize_discovery_output|ProposalConversationService|resolve_font_bundle_paths|SourceEvidenceProcessingService|SynchronousJobDispatcher|UnsupportedDiagramStructureError|validate_synthesis|VirusTotalSampleAcquisitionService)\b' docs README.md backend/README.md Makefile scripts infra frontend pyproject.toml backend/src backend/tests 2>/dev/null
rg -n '(actor_name|actor_name=|get_actor\(|import_module\(|getattr\(|JobRegistry|job_kind|job\.kind|worker_probe|dramatiq)' backend/src/cti_app/workers backend/src/cti_app/infrastructure/jobs.py backend/src/cti_app/application/jobs.py backend/src/cti_app/api docs Makefile infra 2>/dev/null
```

Candidate literals absent outside their defining modules had no matching
registration or lookup. The output also confirmed that `SynchronousJobDispatcher`
is documented. A separate AST import/literal pass over 515 Python files returned
`french_typography_import_or_literal_refs=[]` and zero parse errors. No candidate
was a FastAPI handler or response-schema entry.

The Dramatiq scan checked actor decorators, `get_actor`, explicit actor names,
worker CLI commands, dispatchers, the `JobRegistry`, and job-kind strings.
`worker_probe` is still an automatically registered actor and remains in place;
operator invocation and persisted queue messages were not queried.

## Suppressions

Each suppression has its own code commit. Tests listed here were run after its
change; a missing direct test means the candidate had no test reference. Ruff
was run after each change on the touched file or its surviving source subtree.

| Chemin / symbole | Preuve d'absence d'usage et dépendances vérifiées | Impact et test(s) de protection | Commit / restauration |
| --- | --- | --- | --- |
| `backend/src/cti_app/application/french_typography.py` — `format_french_date`, `format_french_month` | AST : aucun importeur ni nom/chaîne correspondant dans le dépôt ; scan dédié : aucun import ou littéral `french_typography`. Aucun `__all__`, entry point, route, acteur, script, template ou registre. Le chemin Typst actif produit toujours la date publique sans ce module. | Aucun comportement accessible retiré. `test_typst_rendering.py::test_french_publication_dates_use_first_day_typography` : **1 passed**. Ruff application : passé. | `1cb947472ec83bb242371757c6faaac053f8d39d` — `git revert 1cb947472ec83bb242371757c6faaac053f8d39d` |
| `backend/src/cti_app/application/source_figure_inventory.py` — `_ImageElementParser` | Classe privée : seule sa définition figurait dans l'AST ; aucun import, instanciation, `getattr`, chaîne, test, doc ou registre. La classe `SourceFigureInventory` et son parcours d'inventaire restent en place. | Aucun changement au résultat du chemin actif. `test_source_figure_inventory.py` : **22 passed**. Ruff : passé. | `79feea1a09b14112bf3587a4eeca7d8085b4b5fc` — `git revert 79feea1a09b14112bf3587a4eeca7d8085b4b5fc` |
| `backend/src/cti_app/domain/code_features.py` — `mapping_to_tuple` | AST : définition seulement, sans appelant Python, import, schéma, configuration, documentation ou chaîne dynamique. Le reste du modèle de fonctionnalités code n'en dépend pas. Ruff a aussi confirmé puis permis de retirer l'import `Mapping` devenu orphelin. | Aucun changement aux entités/features actives. `test_code_features.py` : **7 passed**. Ruff : passé. | `b6db2fd7b9dea45c547f324933decbc4bb35f092` — `git revert b6db2fd7b9dea45c547f324933decbc4bb35f092` |
| `backend/src/cti_app/integrations/models.py` — `assert_adapter_contract` | AST : définition seulement ; aucun appelant, test, registre, plugin chargé par nom, documentation ou entry point. Le protocole `ModelAdapter`, ses implémentations et leur utilisation par `ModelGateway` demeurent. | Retrait d'une assertion jamais invoquée ; les adaptateurs actifs ne changent pas. `test_model_gateway.py` : **53 passed**. Ruff : passé. | `c4513aad52ae420a5458a17d7d972be6b7fe2e01` — `git revert c4513aad52ae420a5458a17d7d972be6b7fe2e01` |
| `backend/src/cti_app/application/extraction.py` — `ExtractionResult` | AST : définition seulement ; aucun import, annotation, constructeur, discriminant, schéma FastAPI/Pydantic, registre, CLI ou chaîne dynamique. `ParsedDocument`, `EvidenceExtractionService` et les frontières `production_extraction.py` restent. | Retrait d'un dataclass non consommé, sans toucher au service. `test_extraction.py` : **22 passed**. Ruff : passé. | `7be3e410ea0a61c10c2288f7f8a113029ae1aed8` — `git revert 7be3e410ea0a61c10c2288f7f8a113029ae1aed8` |
| `backend/src/cti_app/application/typst_bundle.py` — `hash_bundle_files` | AST : définition seulement ; pas d'appel, d'import dynamique, de schéma, de doc, de config ni d'entry point. Le consommateur actif `hash_bundle_contents` et les empreintes font-content restent. | Seul l'adaptateur fichiers non appelé est retiré ; les calculs actifs d'empreinte restent. `test_typst_template_bundle.py` et `test_typst_compiler.py` : **32 passed**. Ruff : passé. | `34e4cd5a7a181c45802cc2217ce6f7e5919d0bc2` — `git revert 34e4cd5a7a181c45802cc2217ce6f7e5919d0bc2` |
| `backend/src/cti_app/application/model_gateway.py` — `StructuredModelUnavailableError` | AST : aucune construction, levée, interception `except`, import ou annotation ; aucune chaîne `structured_model_unavailable`, map d'erreurs, registre, schéma ou lookup par nom. Les autres sous-classes `ModelGatewayError` restent. | Aucun code d'erreur actif retiré. `test_model_gateway.py` : **53 passed**. Ruff : passé. | `85507f02cb319ffe7337925aff7df78fe7649a25` — `git revert 85507f02cb319ffe7337925aff7df78fe7649a25` |
| `backend/src/cti_app/application/production_synthesis_revision.py` — `_target_value_for_display` | Helper privé : définition seulement ; aucun appel, `getattr`, import dynamique, route, schéma, template, doc ou littéral de registry. Les opérations de révision ciblée et la projection d'API restent. | Aucun changement au flux preview/apply versionné. `test_production_synthesis_application.py` : **254 passed**. Ruff : passé. | `696070f194a3f3185031701f1d3d09176417b1af` — `git revert 696070f194a3f3185031701f1d3d09176417b1af` |

## Candidats conservés

| Candidat | Décision et motif vérifié | Commit |
| --- | --- | --- |
| `accepted_for_publication` | Conservé : garde de publication et de validation ; les règles de décision/publication ne sont pas supprimées. | — |
| `analyst_handoff_policy_from_sources` | Conservé : politique de soumission/TLP et extension explicite du futur pivot analyste. | — |
| `canonical_provenances` | Conservé : touche aux provenances d'invariants et à `invariant_proposals`, domaine explicitement exclu du lot. | — |
| `claim_is_machine_verified`, `indicator_is_machine_verified` | Conservés : règles de preuve source et de vérification de claims/IOC. | — |
| `is_current_source_checkpoint` | Conservé : compatibilité de lecture des checkpoints historiques, explicitement protégée. | — |
| `MeasuredGoodwareCount`, `validate_features`, `load_stage` | Conservés : goodware/index explicitement protégé. | — |
| `_parse_proposal_response`, `_persisted_references`, `ProposalConversationService` | Conservés : propositions et conversations d'invariants explicitement protégées. | — |
| `sample_features_from_vt_view`, `VirusTotalSampleAcquisitionService` | Conservés : VirusTotal/enrichissement analyste et extension future explicitement protégés. | — |
| `_source_figure_candidates` | Conservé : adaptateur privé du flux de médias/figures documenté ; suppression différée faute de preuve que la fonction n'est pas appelée par l'extension de ce flux. | — |
| `worker_probe` | Conservé : acteur Dramatiq enregistré par décorateur et invocable par nom/CLI/opérations. Les messages persistés et l'appel opérateur externe n'ont pas été interrogés. | — |
| `AnalystPostSynthesisService` | Conservé : transfert analyste explicitement exclu et candidat à un point d'extension. | — |
| `collect_indicators` | Conservé : les tests traversent les invariants de complétude/validation IOC, et pas uniquement un utilitaire isolé. | — |
| `compute_font_bundle_version`, `resolve_font_bundle_paths` | Conservés : empreinte et résolution du bundle de polices ; préservent intégrité, reproductibilité et garde de chemins. | — |
| `compute_template_bundle_hash` | Conservé : empreinte de templates utilisée par les tests de rendu/édition ; protège l'identité des bundles. | — |
| `_data_uri` | Conservé : les tests l'emploient pour injecter des médias dans le parcours actif d'inventaire et tester les limites de collecte. | — |
| `diagram_semantic_sha256` | Conservé : digest sémantique et tests d'identité des diagrammes ; ne pas retirer une garde d'intégrité sur le seul constat « tests-only ». | — |
| `DramatiqAnalysisJobDispatcher` | Conservé : dispatcher distinct d'`execute_job` ; usages opératoires et messages/jobs persistés n'ont pas été vérifiés. | — |
| `EvidenceExtractionService` | Conservé : le test de collection le remplace par un échec afin de protéger le chemin actif contre l'ancien appel d'extraction ; supprimer la classe affaiblirait ce garde-fou. | — |
| `FilesystemBlobStore` | Conservé : support de test utilisé par les tests de stockage, d'API et d'intégration qui valident des comportements supérieurs au store lui-même. | — |
| `InMemoryModelOutputStore` | Conservé : faux magasin injecté dans des tests d'application/gateway/discovery, pas seulement des tests de sa propre classe. | — |
| `loop_budget_from_settings` | Conservé : touche au handoff analyste et à l'acquisition de samples, deux domaines explicitement protégés. | — |
| `normalize_discovery_output` | Conservé après restauration : suppression proposée mais auto-review rejetée car `discovery-json-v1` figure dans des contrôles de persistance/intégration et la compatibilité des artefacts est explicitement protégée. Fichiers et tests restaurés. | — |
| `SourceEvidenceProcessingService` | Conservé : lit les snapshots texte archivés via `read_derived_text`, donc reste lié à la compatibilité de lecture d'artefacts ; son module fournit aussi `_canonical_http_url`, importé par `source_figure_inventory.py`. | — |
| `SynchronousJobDispatcher` | Conservé : `docs/async_jobs.md` le désigne pour le développement et la production ; utilisé par les tests API/jobs. | — |
| `UnsupportedDiagramStructureError` | Conservé : code d'erreur stable du protocole `DiagramCompiler`, vérifié par les tests de contrat d'erreurs. | — |
| `validate_synthesis` | Conservé : tests de citation, de source et d'attribution ; suppression différée pour ne pas enlever ces gardes de validité/preuve. | — |

## Contrôles globaux et limites

- Aucun fichier de test n'a été supprimé : aucun test ne ciblait directement les
  symboles supprimés. Les suites de protection listées après chaque ligne de
  suppression ont été exécutées.
- Les scans ont couvert les usages dans `getattr`, `importlib`, imports/alias,
  `__all__`, Pydantic `Literal`/discriminants, routes FastAPI, registres Python,
  `pyproject.toml`, `Makefile`, scripts, `infra`, `compose`, docs et templates.
- Pas de base de production, Redis partagé, fournisseur LLM ou API externe
  interrogés. Les kinds et messages Dramatiq éventuellement persistés, ainsi que
  les commandes opérateur hors dépôt pour `worker_probe`, restent non vérifiés ;
  les acteurs et dispatchers suspects sont conservés.
- Les consommateurs externes qui importeraient directement les helpers supprimés
  ne sont pas vérifiables depuis ce dépôt. Ces symboles n'étaient pas exposés par
  un `__all__`, entry point, contrat documenté ou registre trouvé.
- Vulture et Knip n'ont pas été utilisés ; la preuve repose sur le parcours AST,
  les recherches ciblées de chaînes/registries, l'inspection des dépendances et
  les tests indiqués.
- `make lint-backend` : réussi (`All checks passed!`, 524 fichiers déjà formatés).
- `make typecheck-backend` : réussi (`Success: no issues found in 502 source files`).
- Sous l'environnement corrigé Python 3.12.3, le groupe ciblé après suppressions
  (`tests/test_typst_rendering.py`, `test_source_figure_inventory.py`,
  `test_code_features.py`, `test_model_gateway.py`, `test_extraction.py`,
  `test_typst_template_bundle.py`, `test_typst_compiler.py`,
  `test_production_synthesis_application.py`) a réussi : **412 passed**.
- `make test-backend` complet a collecté 3 010 tests puis a été interrompu :
  `tests/test_goodware.py::test_measurement_prepares_once_and_reuses_uuid_and_fingerprint`
  reste bloqué. Exécuté seul sous Python 3.12.3, ce test restait dans
  `selectors.select` avec un worker thread idle ; interruption manuelle, sans
  échec d'assertion. Une relance excluant ce cas a aussi été interrompue après
  environ deux minutes sans résumé final visible ; d'autres blocages éventuels
  ne sont donc pas identifiés.
- `make test-integration`, sur PostgreSQL jetable `127.0.0.1:55510`, sous Python
  3.12.3 et avec l'accès socket autorisé : **241 passed, 9 failed**. Huit échecs
  (`editorial_enrichment_pipeline_postgres`, `pipeline_happy_path`, les deux
  tests `pipeline_invariants`, `production_state_v5_postgres`,
  `synthesis_canonical` et les deux tests de cache `production_reuse_postgres`)
  figurent déjà dans `/tmp/ci/clean.log`. Le neuvième,
  `test_publication_render_postgres_real_typst_exit_scenario`, nécessite
  `TYPST_BINARY` pointant vers Typst 0.15.1 ; le binaire épinglé n'est pas
  présent. Deux tests d'intégration de `lot34_repair_atomicity` et les deux de
  `production_source_additions`, qui figuraient dans l'ancien journal CI,
  passent maintenant. Un premier lancement sans accès socket a échoué au setup
  des fixtures par `PermissionError: Operation not permitted` ; il ne s'agit pas
  du résultat d'intégration retenu ci-dessus.
- La liste exacte des usages hors dépôt (messages d'acteurs/jobs conservés,
  opérateurs externes) et les validations de production restent non vérifiées ;
  voir les limites ci-dessus. Aucun échec attribué à ces changements de code
  n'a été observé.
