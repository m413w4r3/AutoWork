"""LOT 36 coverage for immutable value corrections and source context."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import TechnicalExtraction
from cti_app.application.production_repairs import (
    EffectiveExtractionProjector,
    ProductionRepairAdjudicationService,
    ProductionRepairAuditReasonRequiredError,
    ProductionRepairIssueDetail,
    ProductionRepairIssueView,
    ProductionRepairValueNotVerifiableError,
    classify_repair_impact,
    extraction_item_contributes_to_synthesis,
    production_repair_correction_identity,
)
from cti_app.application.production_source_evidence import SourceEvidenceSpanKind
from cti_app.domain.production import (
    ProductionEvidenceBasis,
    ProductionRepairAction,
    ProductionRepairCorrection,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    ProductionRepairVerificationState,
)

EDITION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SUBJECT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RUN_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ARTIFACT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
SOURCE_URL = "https://source.example/report"
REPAIR_KEY = "1" * 64
SOURCE_ID = "S1"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _issue(value: str = "ev1l.lot25-desk.com") -> ProductionRepairIssueView:
    return ProductionRepairIssueView(
        repair_key=REPAIR_KEY,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        artifact_type="domain",
        source_id=SOURCE_ID,
        source_title="Source",
        is_publication_ioc=True,
        source_url=SOURCE_URL,
        reason_code="source_evidence_missing",
        value_sha256=_sha256(value),
        preview=value,
        payload_available=True,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_artifact_version=1,
        observed_pipeline_generation=2,
        subject_id=SUBJECT_ID,
    )


def _correction(value: str, state: ProductionRepairVerificationState) -> ProductionRepairCorrection:
    return ProductionRepairCorrection(
        id=production_repair_correction_identity(
            original_repair_key=REPAIR_KEY,
            artifact_type="domain",
            source_id=SOURCE_ID,
            source_url=SOURCE_URL,
            replacement_value_sha256=_sha256(value),
            verification_state=state,
        ),
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        original_repair_key=REPAIR_KEY,
        artifact_type="domain",
        source_id=SOURCE_ID,
        source_url=SOURCE_URL,
        replacement_value_sha256=_sha256(value),
        replacement_payload_blob_id=uuid4(),
        actor_id="analyst",
        verification_state=state,
    )


def _decision(
    action: ProductionRepairAction, correction_id: UUID | None = None
) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_pipeline_generation=2,
        repair_key=REPAIR_KEY,
        issue_kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        action=action,
        actor_id="analyst",
        correction_id=correction_id,
    )


def test_correction_identity_ignores_run_and_artifact_version() -> None:
    first = production_repair_correction_identity(
        original_repair_key=REPAIR_KEY,
        artifact_type="domain",
        source_id=SOURCE_ID,
        source_url=SOURCE_URL,
        replacement_value_sha256=_sha256("evil.example"),
        verification_state=ProductionRepairVerificationState.SOURCE_VERIFIED,
    )
    second = production_repair_correction_identity(
        original_repair_key=REPAIR_KEY,
        artifact_type="domain",
        source_id=SOURCE_ID,
        source_url=SOURCE_URL,
        replacement_value_sha256=_sha256("evil.example"),
        verification_state=ProductionRepairVerificationState.SOURCE_VERIFIED,
    )
    assert first == second


def test_replace_requires_an_immutable_correction() -> None:
    with pytest.raises(ValueError, match="exactly one immutable correction"):
        _decision(ProductionRepairAction.REPLACE)
    with pytest.raises(ValueError, match="exactly one immutable correction"):
        _decision(ProductionRepairAction.EXCLUDE, uuid4())


def test_replace_projects_corrected_value_and_exclude_removes_it() -> None:
    corrected = "evil.lot25-desk.com"
    correction = _correction(corrected, ProductionRepairVerificationState.SOURCE_VERIFIED)
    entry = {
        "repair_key": REPAIR_KEY,
        "kind": ProductionRepairIssueKind.REJECTED_INDICATOR.value,
        "artifact_type": "domain",
        "source_id": SOURCE_ID,
        "value_sha256": correction.replacement_value_sha256,
        "evidence_basis": ProductionEvidenceBasis.SOURCE_VERIFIED.value,
    }
    replaced = EffectiveExtractionProjector().project(
        base=TechnicalExtraction(items=()),
        repair_entries=[entry],
        effective_decisions=[_decision(ProductionRepairAction.REPLACE, correction.id)],
        resolved_payloads={REPAIR_KEY: corrected},
    )

    assert [item.value for item in replaced.extraction.items] == [corrected]
    item = replaced.extraction.items[0]
    assert item.evidence_basis is ProductionEvidenceBasis.SOURCE_VERIFIED
    assert not extraction_item_contributes_to_synthesis(item)

    excluded = EffectiveExtractionProjector().project(
        base=TechnicalExtraction(items=()),
        repair_entries=[entry],
        effective_decisions=[_decision(ProductionRepairAction.EXCLUDE)],
        resolved_payloads={},
    )
    assert excluded.extraction.items == ()


def test_corrected_hash_is_publication_only_and_creates_no_q4() -> None:
    issue = _issue("not-a-real-hash")
    issue = replace(issue, artifact_type="hash", is_publication_ioc=True)
    corrected = "a" * 64
    correction = _correction(corrected, ProductionRepairVerificationState.SOURCE_VERIFIED)
    decision = _decision(ProductionRepairAction.REPLACE, correction.id)
    impact = classify_repair_impact(issue, decision)

    assert impact.kind.value == "publication_only"
    assert not impact.model_call_required


class _Catalog:
    def __init__(self, content: bytes) -> None:
        self.blob_id = uuid4()
        self.contents = {self.blob_id: content}

    async def ingest(self, source: object, *, logical_bucket: str, mime_type: str) -> object:
        del logical_bucket, mime_type
        content = source.read()  # type: ignore[union-attr]
        blob_id = uuid4()
        self.contents[blob_id] = content
        return SimpleNamespace(id=blob_id)

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        return self.contents[blob_id][:max_bytes]


class _ArchiveUow:
    def __init__(self, blob_id: UUID, content: bytes) -> None:
        self.blob_id = blob_id
        self.source_collections = SimpleNamespace(
            list_for_subject=lambda _subject_id: self._collections()
        )
        self.source_documents = SimpleNamespace(get=lambda _document_id: self._document())
        self.blobs = SimpleNamespace(get=lambda _blob_id: self._blob(blob_id, content))

    async def _collections(self) -> list[object]:
        return [
            SimpleNamespace(
                canonical_url=SOURCE_URL,
                source_document_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                decoded_blob_id=None,
            )
        ]

    async def _document(self) -> object:
        return SimpleNamespace(
            decoded_blob_id=self.blob_id,
            decoded_sha256=None,
            detected_mime_type="text/html",
        )

    @staticmethod
    async def _blob(blob_id: UUID, content: bytes) -> object:
        return SimpleNamespace(
            id=blob_id,
            descriptor=SimpleNamespace(sha256=_sha256(content.decode()), mime_type="text/html"),
        )

    async def __aenter__(self) -> _ArchiveUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_source_verified_replacement_and_explicit_override_policy() -> None:
    content = b"<table><tr><td>evil.lot25-desk.com</td></tr></table>"
    catalog = _Catalog(content)
    store = ProductionArtifactStore(catalog)  # type: ignore[arg-type]
    uow = _ArchiveUow(catalog.blob_id, content)
    service = ProductionRepairAdjudicationService(lambda: uow, artifact_store=store)
    issue = _issue()
    detail = ProductionRepairIssueDetail(issue=issue, value="ev1l.lot25-desk.com")

    correction = await service._prepare_correction(
        edition_id=EDITION_ID,
        issue=issue,
        detail=detail,
        replacement_value="evil.lot25-desk.com",
        force_override=False,
        actor_id="analyst",
        reason=None,
    )
    assert correction.verification_state is ProductionRepairVerificationState.SOURCE_VERIFIED
    assert correction.replacement_value_sha256 == _sha256("evil.lot25-desk.com")
    assert await store.read_text(correction.replacement_payload_blob_id) == "evil.lot25-desk.com"

    with pytest.raises(ProductionRepairValueNotVerifiableError):
        await service._prepare_correction(
            edition_id=EDITION_ID,
            issue=issue,
            detail=detail,
            replacement_value="missing.lot25-desk.com",
            force_override=False,
            actor_id="analyst",
            reason=None,
        )

    override = await service._prepare_correction(
        edition_id=EDITION_ID,
        issue=issue,
        detail=detail,
        replacement_value="missing.lot25-desk.com",
        force_override=True,
        actor_id="analyst",
        reason="manual confirmation outside archived text",
    )
    assert override.verification_state is ProductionRepairVerificationState.ANALYST_OVERRIDE

    with pytest.raises(ProductionRepairAuditReasonRequiredError):
        await service._prepare_correction(
            edition_id=EDITION_ID,
            issue=issue,
            detail=detail,
            replacement_value="missing.lot25-desk.com",
            force_override=True,
            actor_id="analyst",
            reason=None,
        )


def _rule_issue(value: str) -> ProductionRepairIssueView:
    return ProductionRepairIssueView(
        repair_key=REPAIR_KEY,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        artifact_type="yara",
        source_id=SOURCE_ID,
        source_title="Source",
        is_publication_ioc=False,
        source_url=SOURCE_URL,
        reason_code="source_evidence_missing",
        value_sha256=_sha256(value),
        preview=value,
        payload_available=True,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_artifact_version=1,
        observed_pipeline_generation=2,
        subject_id=SUBJECT_ID,
    )


@pytest.mark.asyncio
async def test_a_detection_rule_correction_reaches_the_source_gate() -> None:
    """A rule REPLACE must be shape-checked and then gated on the archive.

    Reading verified rules off the verification result instead of its canonical
    extraction made every rule correction raise before the gate, so the
    publication-only rule case of LOT 36 could never be exercised.
    """
    rule_body = 'rule LotForty { strings: $a = "evil" condition: $a }'
    content = f"<pre>{rule_body}</pre>".encode()
    catalog = _Catalog(content)
    store = ProductionArtifactStore(catalog)  # type: ignore[arg-type]
    uow = _ArchiveUow(catalog.blob_id, content)
    service = ProductionRepairAdjudicationService(lambda: uow, artifact_store=store)
    issue = _rule_issue("rule Broken { condition: false }")

    verification = await service._validate_replacement(
        issue=issue,
        value=rule_body,
        require_source=True,
    )

    assert verification.format_valid is True
    assert verification.verified is True
    assert verification.verification_state is ProductionRepairVerificationState.SOURCE_VERIFIED
    assert verification.normalized_value == rule_body
    # The structural context is reported, and never a visual span.
    assert verification.context_spans
    assert all(
        span.kind is not SourceEvidenceSpanKind.VISUAL_UNLOCATED
        for span in verification.context_spans
    )

    absent = await service._validate_replacement(
        issue=issue,
        value='rule Absent { strings: $a = "nowhere" condition: $a }',
        require_source=True,
    )
    assert absent.format_valid is True
    assert absent.verified is False
    assert absent.verification_state is None

    malformed = await service._validate_replacement(
        issue=_rule_issue("x"),
        value="",
        require_source=True,
    )
    assert malformed.format_valid is False
    assert malformed.reason_code == "replacement_value_empty"
