"""AW-011 extraction policy: tier-driven profiles and the architecture cutover.

The tier of the frozen ``ProductionReferenceCorpusV1`` is the only authority
for FULL vs IOC_RULES. These tests lock that policy and the architecture guard
that keeps the canonical extraction module free of the retired REFERENCES
wire-format dependencies.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_extraction, production_workflow
from cti_app.application.production_extraction import (
    build_extraction_plan,
    extraction_profile_for_tier,
)
from cti_app.application.production_parsers import (
    ParsedSource,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionRun,
    SourceExtraction,
    SourceExtractionStatus,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    is_eligible_for_extraction,
)

CANONICAL_MODULE = Path(production_extraction.__file__)


def _record(
    url: str,
    *,
    tier: ProductionReferenceTier,
    role: SourceRole,
    document_id: UUID | None,
    sha256: str | None,
    state: CollectionState = CollectionState.ARCHIVED,
) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=role,
        title=f"Archived {url}",
        publisher="Publisher",
        published_at=date(2026, 7, 10),
        source_collection_id=None,
        source_document_id=document_id,
        discovery_candidate_ids=(),
        collection_state=state,
        content_sha256=sha256,
        relevance_reason=None,
        proposed_by_model=False,
        eligible_for_extraction=is_eligible_for_extraction(
            collection_state=state,
            source_document_id=document_id,
            content_sha256=sha256,
        ),
    )


def _corpus(*sources: ProductionReferenceSourceV1) -> ProductionReferenceCorpusV1:
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=uuid4(),
        research_date=date(2026, 8, 1),
        production_input_hash="a" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )


TIER_POLICY = (
    (ProductionReferenceTier.CORE, SourceRole.PRIMARY, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.INDEPENDENT, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.RELAY, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.AGGREGATOR, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.UNKNOWN, ExtractionProfile.FULL),
    (ProductionReferenceTier.SUPPORTING, SourceRole.PRIMARY, ExtractionProfile.IOC_RULES),
    (ProductionReferenceTier.SUPPORTING, SourceRole.INDEPENDENT, ExtractionProfile.IOC_RULES),
    (ProductionReferenceTier.TECHNICAL, SourceRole.PRIMARY, ExtractionProfile.IOC_RULES),
    (ProductionReferenceTier.TECHNICAL, SourceRole.UNKNOWN, ExtractionProfile.IOC_RULES),
)


@pytest.mark.parametrize(("tier", "role", "expected"), TIER_POLICY)
def test_profile_is_decided_by_tier_never_by_role(
    tier: ProductionReferenceTier,
    role: SourceRole,
    expected: ExtractionProfile,
) -> None:
    assert extraction_profile_for_tier(tier) is expected

    corpus = _corpus(
        _record(
            "https://example.test/declared-core",
            tier=ProductionReferenceTier.CORE,
            role=SourceRole.PRIMARY,
            document_id=uuid4(),
            sha256="c" * 64,
        ),
        _record(
            f"https://example.test/{tier.value}-{role.value}",
            tier=tier,
            role=role,
            document_id=uuid4(),
            sha256="b" * 64,
        ),
    )
    plan = build_extraction_plan(corpus)

    profiles = {source.canonical_url: source.profile for source in plan.sources}

    assert profiles[f"https://example.test/{tier.value}-{role.value}"] is expected


def test_policy_version_participates_in_the_plan() -> None:
    corpus = _corpus(
        _record(
            "https://example.test/core",
            tier=ProductionReferenceTier.CORE,
            role=SourceRole.PRIMARY,
            document_id=uuid4(),
            sha256="b" * 64,
        )
    )

    plan = build_extraction_plan(corpus)

    assert plan.profile_policy_version == "production-reference-tier-v1"


def test_ineligible_source_never_reaches_the_plan() -> None:
    corpus = _corpus(
        _record(
            "https://example.test/core",
            tier=ProductionReferenceTier.CORE,
            role=SourceRole.PRIMARY,
            document_id=uuid4(),
            sha256="b" * 64,
        ),
        _record(
            "https://example.test/unavailable",
            tier=ProductionReferenceTier.SUPPORTING,
            role=SourceRole.PRIMARY,
            document_id=None,
            sha256=None,
            state=CollectionState.UNAVAILABLE,
        ),
    )

    plan = build_extraction_plan(corpus)

    assert [source.canonical_url for source in plan.sources] == ["https://example.test/core"]
    assert [omission.reason.value for omission in plan.omitted_sources] == [
        "reference_not_eligible"
    ]
    assert plan.omitted_sources[0].canonical_url == "https://example.test/unavailable"
    assert plan.omitted_sources[0].tier is ProductionReferenceTier.SUPPORTING
    assert plan.omitted_sources[0].collection_state is CollectionState.UNAVAILABLE


# --- architecture guards ----------------------------------------------------


def _canonical_source() -> str:
    return CANONICAL_MODULE.read_text(encoding="utf-8")


def test_canonical_module_has_no_legacy_references_dependency() -> None:
    source = _canonical_source()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.asname or alias.name for alias in node.names)
    forbidden = {
        "ReferenceReport",
        "ParsedSource",
        "parse_reference_report",
        "load_reference_projection",
    }

    assert imported.isdisjoint(forbidden)
    for name in forbidden:
        assert re.search(rf"\b{name}\b", source) is None


def test_canonical_module_never_decides_the_profile_from_source_role() -> None:
    source = _canonical_source()

    # No role-keyed FULL decision may exist: the tier is the only authority.
    assert re.search(r"SourceRole\.PRIMARY\s*(?:is|==)\s*", source) is None
    assert re.search(r"role\s*(?:is|==)\s*SourceRole\.PRIMARY", source) is None
    assert "extraction_profile_for_tier" in source


def test_live_extraction_stage_no_longer_calls_the_legacy_planner() -> None:
    stage = inspect.getsource(
        production_workflow.ProductionWorkflowOrchestrator._execute_extraction_stage
    )

    assert "plan_q2_extraction_profiles" not in stage
    assert "_execute_direct_url_extraction" not in stage
    assert "load_reference_projection" not in stage
    assert "_canonical_extraction" in stage
    assert "service.execute" in stage


def test_canonical_service_contract_helpers_exist() -> None:
    assert callable(production_extraction.production_extraction_metadata)
    assert production_extraction.source_text_contract_version().startswith(
        production_extraction.SOURCE_TEXT_CONTRACT_VERSION
    )


# --- legacy Q2 harness shared with test_production_q2_batch and
# test_production_q2_source_gate --------------------------------------
# These helpers drive the retired live-URL Q2 path directly; they are kept
# because those suites still exercise it in isolation.


def _source(url: str, published_at: date | None) -> ParsedSource:
    return ParsedSource(
        local_id=url.rsplit("/", 1)[-1],
        title=url.rsplit("/", 1)[-1],
        url=url,
        canonical_url=url,
        publisher="Publisher",
        published_at=published_at,
        role=SourceRole.INDEPENDENT,
    )


def _input_source(url: str, published_at: date | None) -> ProductionInputSource:
    return ProductionInputSource(
        discovery_candidate_id=uuid4(),
        source_candidate_id=uuid4(),
        canonical_url=url,
        role=SourceRole.PRIMARY,
        title="Core",
        publisher="Publisher",
        published_at=published_at,
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
    )


def _snapshot(core_sources: tuple[ProductionInputSource, ...]) -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        edition_id=uuid4(),
        subject_version=1,
        subject_title="Subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=tuple(source.discovery_candidate_id for source in core_sources),
        discovery_summary="Description",
        actor_or_campaign="Actor",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        research_date=date(2026, 8, 1),
        core_sources=core_sources,
        captured_at=datetime.now().astimezone(),
    )


class _CacheRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, ...], SourceExtraction] = {}
        self.lookups = 0

    @staticmethod
    def _key(values: dict[str, str]) -> tuple[str, ...]:
        return tuple(
            values[key]
            for key in (
                "source_content_sha256",
                "profile",
                "contract_version",
                "prompt_version",
                "parser_version",
                "verifier_version",
            )
        )

    async def get_by_identity(self, **values: str) -> SourceExtraction | None:
        self.lookups += 1
        return self.rows.get(self._key(values))

    async def list_for_url(self, canonical_url: str) -> tuple[SourceExtraction, ...]:
        return tuple(row for row in self.rows.values() if row.canonical_url == canonical_url)

    async def claim(self, extraction: SourceExtraction, *, force: bool = False) -> bool:
        identity = {
            "source_content_sha256": extraction.source_content_sha256,
            "profile": extraction.profile.value,
            "contract_version": extraction.contract_version,
            "prompt_version": extraction.prompt_version,
            "parser_version": extraction.parser_version,
            "verifier_version": extraction.verifier_version,
        }
        key = self._key(identity)
        existing = self.rows.get(key)
        if (
            existing is not None
            and existing.status
            in {
                SourceExtractionStatus.RUNNING,
                SourceExtractionStatus.VERIFIED,
            }
            and not force
        ):
            return False
        if existing is not None:
            extraction = replace(extraction, id=existing.id, created_at=existing.created_at)
        self.rows[key] = extraction
        return True

    async def save(self, extraction: SourceExtraction) -> None:
        identity = {
            "source_content_sha256": extraction.source_content_sha256,
            "profile": extraction.profile.value,
            "contract_version": extraction.contract_version,
            "prompt_version": extraction.prompt_version,
            "parser_version": extraction.parser_version,
            "verifier_version": extraction.verifier_version,
        }
        self.rows[self._key(identity)] = extraction


class _CacheStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, object]] = {}

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self.payloads[blob_id]

    async def store_source_extraction_payloads(
        self, *, raw: str, canonical: dict[str, object]
    ) -> tuple[UUID | None, UUID]:
        raw_id, canonical_id = uuid4(), uuid4()
        self.payloads[raw_id] = {"raw": raw}
        self.payloads[canonical_id] = canonical
        return raw_id, canonical_id


class _ArchivedBlobs:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}

    def add(self, content: bytes) -> tuple[UUID, str]:
        blob_id = uuid4()
        self.contents[blob_id] = content
        return blob_id, hashlib.sha256(content).hexdigest()

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        del max_bytes
        return self.contents[blob_id]


class _CacheUow:
    def __init__(self, state: _CacheState) -> None:
        self._state = state
        self.source_extractions = state.extractions
        self.source_collections = state.collections
        self.source_documents = state.documents
        self.production_artifacts = state.artifacts
        self.production_runs = state.runs

    async def __aenter__(self) -> _CacheUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        return None


class _CacheState:
    def __init__(
        self,
        docs: dict[UUID, SimpleNamespace],
        collections: dict[UUID, SimpleNamespace],
    ) -> None:
        self.extractions = _CacheRepository()
        self.documents = SimpleNamespace(
            list_for_subject=lambda subject_id: self._documents(subject_id, docs)
        )
        self.collections = SimpleNamespace(
            list_for_subject=lambda subject_id: self._collections(subject_id, collections)
        )
        self.artifacts = SimpleNamespace(
            get_current=lambda run_id, stage: self._reference_artifact(run_id, stage)
        )
        self.runs = SimpleNamespace(get=lambda run_id: self._run(run_id))
        self._docs_by_id = docs
        self._collections_by_id = collections
        self._runs: dict[UUID, ProductionRun] = {}

    async def _documents(
        self, subject_id: UUID, docs: dict[UUID, SimpleNamespace]
    ) -> list[SimpleNamespace]:
        return [doc for doc in docs.values() if doc.subject_id == subject_id]

    async def _collections(
        self, subject_id: UUID, collections: dict[UUID, SimpleNamespace]
    ) -> list[SimpleNamespace]:
        return [item for item in collections.values() if item.subject_id == subject_id]

    async def _reference_artifact(self, run_id: UUID, stage: str) -> SimpleNamespace:
        del run_id, stage
        return SimpleNamespace(canonical_blob_id=uuid4(), input_hash="a" * 64)

    async def _run(self, run_id: UUID) -> ProductionRun | None:
        return self._runs.get(run_id)


def _archived_document(
    *,
    subject_id: UUID,
    url: str,
    content: bytes,
    blobs: _ArchivedBlobs | None = None,
) -> SimpleNamespace:
    """One collected SourceDocument and its canonical decoded blob."""
    if blobs is None:
        blob_id = uuid4()
        content_sha256 = hashlib.sha256(content).hexdigest()
    else:
        blob_id, content_sha256 = blobs.add(content)
    return SimpleNamespace(
        id=uuid4(),
        subject_id=subject_id,
        final_url=url,
        decoded_sha256=content_sha256,
        decoded_blob_id=blob_id,
        detected_mime_type="text/plain",
    )


def _collection_for(document: SimpleNamespace, url: str) -> SimpleNamespace:
    return SimpleNamespace(
        subject_id=document.subject_id,
        canonical_url=url,
        source_document_id=document.id,
        decoded_blob_id=document.decoded_blob_id,
    )


class _CacheGateway:
    def __init__(self, response: str | None = None) -> None:
        self.calls: list[UUID] = []
        self.source_ids: list[str] = []
        self.prompts: list[str] = []
        self.requests: list[object] = []
        self._response = response

    _DEFAULT_RESPONSE = (
        "FACT malware\n- ExampleRAT :: observed\nIOC confirmed domain\n- c2.example.org\n"
    )

    async def execute(self, request: object, role: object) -> object:
        del role
        run_id = request.run_id
        self.calls.append(run_id)
        self.prompts.append(request.text)
        self.requests.append(request)
        source_id = (
            request.metadata.get("source_id") or request.metadata["source_url"].rsplit("/", 1)[-1]
        )
        self.source_ids.append(str(source_id))
        return SimpleNamespace(
            output_text=self._response or self._DEFAULT_RESPONSE,
            run=SimpleNamespace(
                id=run_id,
                status=production_workflow.ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            metadata={},
        )


class _ExtractionSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def store_extraction_result(self, **values: object) -> SimpleNamespace:
        self.calls.append(values)
        return SimpleNamespace(id=uuid4())


class _Diagnostics:
    def record(self, **values: object) -> None:
        self.events.append(values)

    def record_parse(self, **values: object) -> None:
        del values

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []


def _cached_orchestrator(
    state: _CacheState,
    gateway: _CacheGateway,
    store: _CacheStore,
    sink: _ExtractionSink,
    monkeypatch: pytest.MonkeyPatch,
    blobs: _ArchivedBlobs | None = None,
) -> production_workflow.ProductionWorkflowOrchestrator:
    orchestrator = production_workflow.ProductionWorkflowOrchestrator.__new__(
        production_workflow.ProductionWorkflowOrchestrator
    )
    orchestrator._blob_reader = blobs
    orchestrator._uow_factory = lambda: _CacheUow(state)
    orchestrator._model_gateway = gateway
    orchestrator._artifact_store = store
    orchestrator._extraction = sink
    orchestrator._diagnostics = _Diagnostics()
    orchestrator._correlation_id = "test"
    orchestrator._pacing = SimpleNamespace(model_delay_seconds=lambda: 0.0)

    async def no_reuse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def subject_context(*args: object) -> tuple[str, str]:
        del args
        return "Subject", ""

    async def production_context(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(
            external_llm_allowed=True,
            subject_title="Subject",
            period_start="2026-07-01",
            period_end="2026-07-31",
        )

    orchestrator._reuse_artifact = no_reuse
    orchestrator._subject_context = subject_context
    monkeypatch.setattr(
        production_workflow,
        "build_subject_production_context",
        production_context,
    )
    return orchestrator
