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

_STORE_METHODS = {"store_synthesis_result", "store_editorial_enrichment_result"}
_CANDIDATE_MODULES = {
    "cti_app.application.editorial_synthesis.candidate",
    "cti_app.application.editorial_enrichment_candidate",
}


@dataclass(frozen=True)
class _Callable:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner_class: ast.ClassDef | None


def _source_callables(
    path: Path,
) -> tuple[ast.Module, dict[str, _Callable], set[str]]:
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
            if leaf in _STORE_METHODS or is_candidate_helper:
                key = ".".join([*(item.name for item in classes), *functions])
                method_calls.add(key)

        for child in ast.iter_child_nodes(node):
            visit(child, current_classes, current_functions)

    visit(module)
    return module, callables, method_calls


def _iter_python_sources() -> list[Path]:
    return sorted(APPLICATION_ROOT.rglob("*.py"))


def _has_production_artifact_append(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "append"
        and isinstance(item.func.value, ast.Attribute)
        and item.func.value.attr in {"production_artifacts", "_production_artifacts"}
        for item in ast.walk(node)
    )


def _class_has_revision_effects(owner: ast.ClassDef | None) -> bool:
    if owner is None:
        return False
    has_effect_type = any(
        isinstance(node, ast.Name) and node.id == "EditorialRevisionEffects"
        for node in ast.walk(owner)
    )
    has_effect_call = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"apply_after_write", "apply_after_writes"}
        for node in ast.walk(owner)
    )
    return has_effect_type and has_effect_call


def test_writer_inventory_covers_all_stage_store_and_candidate_callers() -> None:
    discovered_method_calls: set[str] = set()
    callable_index: dict[str, _Callable] = {}
    for path in _iter_python_sources():
        relative_path = path.relative_to(APPLICATION_ROOT).as_posix()
        _module, callables, method_calls = _source_callables(path)
        discovered_method_calls.update(f"{relative_path}:{name}" for name in method_calls)
        callable_index.update({f"{relative_path}:{name}": item for name, item in callables.items()})

    expected_method_calls = {
        key
        for key, (_classification, _reason, kind) in WRITER_ALLOWLIST.items()
        if kind == "method-call"
    }
    unclassified = discovered_method_calls - expected_method_calls
    missing = expected_method_calls - discovered_method_calls
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
        callable_info = callable_index[key]
        if kind == "direct-stage-append":
            assert _has_production_artifact_append(callable_info.node), (
                f"{key} is classified as a direct stage writer but no production artifact "
                "append was found. Update WRITER_ALLOWLIST."
            )
        if classification == "legacy-avec-effets":
            assert _class_has_revision_effects(callable_info.owner_class), (
                f"Legacy writer {key} must reference EditorialRevisionEffects and "
                "apply_after_write(s) in its owning service class."
            )
