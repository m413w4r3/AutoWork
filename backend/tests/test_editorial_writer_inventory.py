from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOT = BACKEND_ROOT / "src" / "cti_app"

# Keys are source path:function. Keep every editorial writer classification and
# its reason in this one allowlist; method-call entries are discovered by AST.
WRITER_ALLOWLIST: dict[str, tuple[str, str, str]] = {
    "application/editorial_enrichment_candidate.py:persist_candidate": (
        "adoption",
        "Shared candidate persistence core used by adoption and the legacy revision UOW.",
        "method-call",
    ),
    (
        "application/editorial_proposal_adoption.py:"
        "EditorialProposalAdoptionService._persist_candidate"
    ): (
        "adoption",
        "Persists the accepted proposal candidate; adoption applies registry effects "
        "in the same UOW.",
        "method-call",
    ),
    "application/editorial_synthesis/candidate.py:persist_candidate": (
        "adoption",
        "Shared candidate persistence core used by adoption and the legacy revision UOW.",
        "method-call",
    ),
    "application/production_editorial_enrichment.py:ProductionEditorialEnrichmentService.execute": (
        "C",
        "Creates pipeline enrichment for a production run; no editorial proposal is decided.",
        "method-call",
    ),
    (
        "application/production_enrichment_revision.py:"
        "ProductionEditorialEnrichmentRevisionService.restore_stale_enrichment_blocks.persist_restore"
    ): (
        "legacy-avec-effets",
        "Restores verified enrichment history under the revision run guard and snapshot advance.",
        "method-call",
    ),
    (
        "application/production_enrichment_revision.py:"
        "ProductionEditorialEnrichmentRevisionService.revise"
    ): (
        "legacy-avec-effets",
        "Legacy targeted enrichment revision applies shared effects in the same UOW.",
        "method-call",
    ),
    (
        "application/production_repairs.py:"
        "ProductionRepairMaterializationService._materialize_canonical_publication_in_uow"
    ): (
        "legacy-avec-effets",
        "Repair lineage rebase batches synthesis and enrichment writes into one snapshot advance.",
        "direct-stage-append",
    ),
    (
        "application/production_repairs.py:"
        "ProductionRepairMaterializationService._materialize_editorial_enrichment_in_uow"
    ): (
        "legacy-avec-effets",
        "Repair enrichment rebase is collected by the enclosing operation for one shared advance.",
        "direct-stage-append",
    ),
    "application/production_state.py:ProductionStateService.import_state": (
        "C",
        "Imports canonical stages into a newly created needs-review run without an "
        "editorial decision.",
        "direct-stage-append",
    ),
    "application/production_artifact_reuse.py:ProductionArtifactReuseService.find_or_reuse": (
        "C",
        "Reconstructs an exact reusable stage artifact in the destination run.",
        "direct-stage-append",
    ),
    "application/production_synthesis.py:ProductionSynthesisService._draft": (
        "C",
        "Creates the first pipeline synthesis for a production run.",
        "method-call",
    ),
    "application/production_stages.py:SynthesisService.store_synthesis_result": (
        "C",
        "Persists a pipeline synthesis artifact in its caller's UOW.",
        "direct-stage-append",
    ),
    (
        "application/production_stages.py:"
        "EditorialEnrichmentService.store_editorial_enrichment_result"
    ): (
        "C",
        "Persists pipeline editorial enrichment in its caller's UOW.",
        "direct-stage-append",
    ),
    "application/production_source_additions.py:SourceAdditionService.promote_in_uow": (
        "C",
        "Promotes a precomputed production stage artifact without an editorial decision.",
        "direct-stage-append",
    ),
    "application/production_relevance.py:persist_relevance_projection_in_uow": (
        "C",
        "Persists a relevance projection used by pipeline and repair flows.",
        "direct-stage-append",
    ),
    "application/publication_assembly.py:PublicationAssemblyService.assemble_publication": (
        "C",
        "Builds or reuses a PUBLICATION artifact in the production assembly pipeline.",
        "direct-stage-append",
    ),
    (
        "application/production_synthesis_revision.py:"
        "ProductionSynthesisRevisionService._rebase_enrichment_if_valid"
    ): (
        "legacy-avec-effets",
        "Rebases enrichment after a synthesis revision; the enclosing revision "
        "applies effects once.",
        "method-call",
    ),
    (
        "application/production_synthesis_revision.py:"
        "ProductionSynthesisRevisionService._revise_in_uow_impl"
    ): (
        "legacy-avec-effets",
        "Legacy targeted synthesis revision applies shared effects after its full write set.",
        "method-call",
    ),
    "application/publication_text_correction.py:PublicationTextCorrectionService.correct": (
        "C",
        "Publication-only correction is covered by the publication snapshot projection oracle.",
        "direct-stage-append",
    ),
    "application/publication_augmentation.py:PublicationAugmentationArtifactService.publish": (
        "C",
        "Publication-only opinion augmentation is covered by the publication "
        "snapshot projection oracle.",
        "direct-stage-append",
    ),
}

_ARTIFACT_COLLECTIONS = {"production_artifacts", "_production_artifacts"}
_EDITORIAL_ARTIFACT_STAGES = {
    "SYNTHESIS",
    "EDITORIAL_ENRICHMENT",
    "PUBLICATION",
}
_NON_EDITORIAL_STAGE_STORES = {"extraction", "references", "repair_projection"}
_ALLOWED_CLASSIFICATIONS = {"adoption", "legacy-avec-effets", "C"}
_CANDIDATE_MODULES = {
    "cti_app.application.editorial_synthesis.candidate",
    "cti_app.application.editorial_enrichment_candidate",
}


@dataclass(frozen=True)
class _Callable:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner_class: ast.ClassDef | None


def _source_callables(path: Path) -> tuple[ast.Module, dict[str, _Callable], set[str], set[str]]:
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(path))
    callables: dict[str, _Callable] = {}
    method_calls: set[str] = set()

    direct_helpers: dict[str, str] = {}
    module_helpers: dict[str, str] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported_module = (
                    f"{node.module}.{alias.name}"
                    if node.module == "cti_app.application.editorial_synthesis"
                    else node.module
                )
                local_name = alias.asname or alias.name
                if alias.name == "persist_candidate" and imported_module in _CANDIDATE_MODULES:
                    direct_helpers[local_name] = imported_module
                if imported_module in _CANDIDATE_MODULES:
                    module_helpers[local_name] = imported_module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _CANDIDATE_MODULES:
                    module_helpers[alias.asname or alias.name.rsplit(".", 1)[-1]] = alias.name

    def visit(
        node: ast.AST,
        classes: tuple[ast.ClassDef, ...] = (),
        functions: tuple[str, ...] = (),
    ) -> None:
        current_classes = classes
        current_functions = functions
        if isinstance(node, ast.ClassDef):
            current_classes = (*classes, node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current_functions = (*functions, node.name)
            key = ".".join([*(item.name for item in classes), *current_functions])
            callables[key] = _Callable(
                node=node,
                owner_class=classes[-1] if classes else None,
            )
        elif isinstance(node, ast.Call):
            function = node.func
            leaf = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else ""
            )
            is_candidate_helper = False
            if isinstance(function, ast.Name):
                is_candidate_helper = function.id in direct_helpers
            elif (
                isinstance(function, ast.Attribute)
                and function.attr == "persist_candidate"
                and isinstance(function.value, ast.Name)
            ):
                is_candidate_helper = function.value.id in module_helpers
            if _is_stage_store_call(leaf) or is_candidate_helper:
                key = ".".join([*(item.name for item in classes), *functions])
                method_calls.add(key)

        for child in ast.iter_child_nodes(node):
            visit(child, current_classes, current_functions)

    visit(module)
    direct_stage_writes = {
        key
        for key, callable_info in callables.items()
        if _has_editorial_artifact_append(callable_info.node)
    }
    return module, callables, method_calls, direct_stage_writes


def _iter_python_sources() -> list[Path]:
    return sorted(APPLICATION_ROOT.rglob("*.py"))


def _is_stage_store_call(leaf: str) -> bool:
    if not leaf.startswith("store_") or not leaf.endswith("_result"):
        return False
    stage_hint = leaf[len("store_") : -len("_result")]
    return stage_hint not in _NON_EDITORIAL_STAGE_STORES


def _has_editorial_artifact_append(node: ast.AST) -> bool:
    constructors: dict[str, set[str | None]] = {}
    for item in ast.walk(node):
        if not isinstance(item, (ast.Assign, ast.AnnAssign)):
            continue
        value = item.value
        if not isinstance(value, ast.Call) or not _is_production_artifact_constructor(value):
            continue
        stage = next((keyword.value for keyword in value.keywords if keyword.arg == "stage"), None)
        stage_name = (
            stage.attr
            if isinstance(stage, ast.Attribute)
            and isinstance(stage.value, ast.Name)
            and stage.value.id == "ProductionArtifactStage"
            else None
        )
        targets = item.targets if isinstance(item, ast.Assign) else [item.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constructors.setdefault(target.id, set()).add(stage_name)

    for item in ast.walk(node):
        if not (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr in {"append", "add"}
            and isinstance(item.func.value, ast.Attribute)
            and item.func.value.attr in _ARTIFACT_COLLECTIONS
        ):
            continue
        argument = item.args[0] if item.args else None
        argument_name = argument.id if isinstance(argument, ast.Name) else None
        stages = constructors.get(argument_name, {None})
        if None in stages or _EDITORIAL_ARTIFACT_STAGES.intersection(stages):
            return True
    return False


def _is_production_artifact_constructor(node: ast.Call) -> bool:
    return (isinstance(node.func, ast.Name) and node.func.id == "ProductionArtifact") or (
        isinstance(node.func, ast.Attribute) and node.func.attr == "ProductionArtifact"
    )


def _class_has_revision_effects(
    owner: ast.ClassDef | None,
    writer: _Callable,
    class_callables: tuple[_Callable, ...],
) -> bool:
    if owner is None:
        return False
    has_effect_type = any(
        isinstance(node, ast.Name) and node.id == "EditorialRevisionEffects"
        for node in ast.walk(owner)
    )
    if not has_effect_type:
        return False

    methods = {
        callable_info.node.name: callable_info
        for callable_info in class_callables
        if callable_info.owner_class is owner
    }
    calls: dict[str, set[str]] = {name: set() for name in methods}
    effect_methods: set[str] = set()
    for name, callable_info in methods.items():
        for node in ast.walk(callable_info.node):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr in {
                "apply_after_write",
                "apply_after_writes",
            }:
                effect_methods.add(name)
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "self"
                and function.attr in methods
            ):
                calls[name].add(function.attr)
            elif isinstance(function, ast.Name) and function.id in methods:
                calls[name].add(function.id)

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(calls.get(current, set()) - seen)
        return seen

    writer_name = writer.node.name
    if writer_name not in methods:
        return False
    orchestration_paths = (reachable(name) for name in methods if writer_name in reachable(name))
    return any(path.intersection(effect_methods) for path in orchestration_paths)


def test_writer_inventory_covers_all_stage_store_and_candidate_callers() -> None:
    discovered_method_calls: set[str] = set()
    discovered_direct_writes: set[str] = set()
    callable_index: dict[str, _Callable] = {}
    for path in _iter_python_sources():
        relative_path = path.relative_to(APPLICATION_ROOT).as_posix()
        _module, callables, method_calls, direct_stage_writes = _source_callables(path)
        discovered_method_calls.update(f"{relative_path}:{name}" for name in method_calls)
        discovered_direct_writes.update(f"{relative_path}:{name}" for name in direct_stage_writes)
        callable_index.update({f"{relative_path}:{name}": item for name, item in callables.items()})

    expected_method_calls = {
        key
        for key, (_classification, _reason, kind) in WRITER_ALLOWLIST.items()
        if kind == "method-call"
    }
    expected_direct_writes = {
        key
        for key, (_classification, _reason, kind) in WRITER_ALLOWLIST.items()
        if kind == "direct-stage-append"
    }
    discovered_writers = discovered_method_calls | discovered_direct_writes
    expected_writers = expected_method_calls | expected_direct_writes
    unclassified = discovered_writers - expected_writers
    missing = expected_writers - discovered_writers
    assert not unclassified, (
        "Unclassified editorial artifact caller(s): "
        f"{sorted(unclassified)}. Add each path:function to WRITER_ALLOWLIST "
        "with its classification and justification."
    )
    assert not missing, (
        "Stale entries in WRITER_ALLOWLIST: "
        f"{sorted(missing)}. Remove or correct the path:function entry."
    )

    missing_functions = set(WRITER_ALLOWLIST) - set(callable_index)
    assert not missing_functions, (
        "Writer inventory references missing source functions: "
        f"{sorted(missing_functions)}. Update WRITER_ALLOWLIST."
    )

    for key, (classification, _reason, kind) in WRITER_ALLOWLIST.items():
        assert classification in _ALLOWED_CLASSIFICATIONS, (
            f"Unknown writer classification {classification!r} for {key}."
        )
        callable_info = callable_index[key]
        if kind == "direct-stage-append":
            assert _has_editorial_artifact_append(callable_info.node), (
                f"{key} is classified as a direct stage writer but no production artifact "
                "append with a relevant or unresolved stage was found. Update WRITER_ALLOWLIST."
            )
        if classification == "legacy-avec-effets":
            class_callables = tuple(callable_index.values())
            assert _class_has_revision_effects(
                callable_info.owner_class,
                callable_info,
                class_callables,
            ), (
                f"Legacy writer {key} must reference EditorialRevisionEffects and "
                "reach apply_after_write(s) on its writer call path."
            )


def test_inventory_detects_new_stage_store_calls_and_append_or_add_writers(
    tmp_path: Path,
) -> None:
    source = """
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage

class Pipeline:
    def writes_synthesis(self, uow):
        artifact = ProductionArtifact(stage=ProductionArtifactStage.SYNTHESIS)
        uow.production_artifacts.add(artifact)

    def writes_references(self, uow):
        artifact = ProductionArtifact(stage=ProductionArtifactStage.REFERENCES)
        uow.production_artifacts.append(artifact)

    def persists_publication(self, store):
        store.store_publication_result()
"""
    source_path = tmp_path / "writers.py"
    source_path.write_text(source, encoding="utf-8")

    _module, _callables, method_calls, direct_writes = _source_callables(source_path)

    assert method_calls == {"Pipeline.persists_publication"}
    assert direct_writes == {"Pipeline.writes_synthesis"}


def test_legacy_effect_check_follows_the_writer_call_path_only(tmp_path: Path) -> None:
    source = """
class Disconnected:
    effects: EditorialRevisionEffects
    def write(self, uow, artifact):
        uow.production_artifacts.append(artifact)
    def unrelated(self, uow):
        self.effects.apply_after_write(uow)

class Connected:
    effects: EditorialRevisionEffects
    def write(self, uow, artifact):
        uow.production_artifacts.append(artifact)
        self.finish(uow)
    def finish(self, uow):
        self.effects.apply_after_write(uow)
"""
    source_path = tmp_path / "effect_paths.py"
    source_path.write_text(source, encoding="utf-8")
    _module, callables, _method_calls, _direct_writes = _source_callables(source_path)

    assert not _class_has_revision_effects(
        callables["Disconnected.write"].owner_class,
        callables["Disconnected.write"],
        tuple(callables.values()),
    )
    assert _class_has_revision_effects(
        callables["Connected.write"].owner_class,
        callables["Connected.write"],
        tuple(callables.values()),
    )
