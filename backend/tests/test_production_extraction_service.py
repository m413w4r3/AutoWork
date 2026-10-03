"""AW-011 canonical, archive-backed EXTRACTION service tests."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_extraction
from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_extraction import (
    ExtractionControlCode,
    ExtractionExecutionStatus,
    ProductionExtractionControlError,
    ProductionExtractionExecution,
    ProductionExtractionService,
    build_extraction_plan,
    extraction_compatibility_view,
    load_reference_corpus,
    merge_q2_source_outputs,
    project_legacy_technical_extraction,
)
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2EventProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
)
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
    SourceExtraction,
    SourceExtractionStatus,
)
from cti_app.domain.production_extraction import (
    ExtractionReuseState,
    ProductionExtractionOmissionReason,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    is_eligible_for_extraction,
)

CORE_URL = "https://example.test/core"
SUPPORT_URL = "https://example.test/support"
TECH_URL = "https://example.test/technical"
MIRROR_URL = "https://example.test/core-mirror"

CORE_TEXT = (
    "ExampleRAT was deployed by Actor-X on 2026-07-10.\n"
    "The operator ran the command powershell -enc ZXhhbXBsZQ== during the intrusion.\n"
    "The implant beaconed to evil.security-lab.io and exploited CVE-2026-12345.\n"
    "rule ExampleRAT { condition: true }\n"
)
SUPPORT_TEXT = (
    "The loader beaconed to loader.security-lab.io on 2026-07-11.\n"
    "rule SupportRule { condition: true }\n"
)
TECH_TEXT = "The sample hash d41d8cd98f00b204e9800998ecf8427e was analysed.\n"


def _full_output() -> Q2SourceOutput:
    return Q2SourceOutput(
        facts=[
            Q2FactProposal(category="malware", value="ExampleRAT"),
            Q2FactProposal(category="commands", value="powershell -enc ZXhhbXBsZQ=="),
            Q2FactProposal(category="actors", value="Actor-X"),
            Q2FactProposal(category="victimology", value="healthcare providers"),
        ],
        events=[
            Q2EventProposal(
                event_date=date(2026, 7, 10),
                text="ExampleRAT was deployed by Actor-X on 2026-07-10.",
            ),
            Q2EventProposal(event_date=date(2020, 1, 1), text="ExampleRAT was deployed by Actor-X"),
        ],
        artifacts=[
            Q2ArtifactProposal(
                value="evil.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
            Q2ArtifactProposal(
                value="CVE-2026-12345", artifact_type="cve", indicator_status="contextual"
            ),
            Q2ArtifactProposal(
                value="ghost.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
        ],
        rules=[
            Q2RuleProposal(
                rule_type=DetectionRuleType.YARA,
                name="ExampleRAT",
                body="rule ExampleRAT { condition: true }",
            )
        ],
        uncertainties=["Attribution of Actor-X remains unconfirmed."],
    )


def _support_output() -> Q2SourceOutput:
    return Q2SourceOutput(
        facts=[Q2FactProposal(category="malware", value="SupportLoader")],
        events=[Q2EventProposal(text="The loader beaconed to loader.security-lab.io.")],
        artifacts=[
            Q2ArtifactProposal(
                value="loader.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            )
        ],
        rules=[
            Q2RuleProposal(
                rule_type=DetectionRuleType.YARA,
                name="SupportRule",
                body="rule SupportRule { condition: true }",
            )
        ],
        uncertainties=["The loader family is unconfirmed."],
    )


def _tech_output() -> Q2SourceOutput:
    return Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="d41d8cd98f00b204e9800998ecf8427e",
                artifact_type="hash",
                indicator_status="contextual",
            )
        ],
        uncertainties=["The hash source is unconfirmed."],
    )


# --- fake infrastructure ----------------------------------------------------


class _BlobStore:
    def __init__(self) -> None:
        self.bytes: dict[UUID, bytes] = {}
        self.payloads: dict[UUID, dict[str, Any]] = {}
        self.raws: dict[UUID, str] = {}
        self.writes = 0

    def put_bytes(self, content: bytes) -> tuple[UUID, str]:
        blob_id = uuid4()
        self.bytes[blob_id] = content
        return blob_id, hashlib.sha256(content).hexdigest()

    def put_json(self, payload: dict[str, Any]) -> UUID:
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        del max_bytes
        return self.bytes[blob_id]

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]

    async def store_source_extraction_payloads(
        self, *, raw: str, canonical: dict[str, Any]
    ) -> tuple[UUID | None, UUID]:
        self.writes += 1
        raw_id: UUID | None = None
        if raw:
            raw_id = uuid4()
            self.raws[raw_id] = raw
        return raw_id, self.put_json(canonical)


class _ArtifactRepository:
    def __init__(self) -> None:
        self.artifact: object | None = None

    async def get_current(self, run_id: UUID, stage: str) -> object | None:
        del run_id, stage
        return self.artifact


class _DocumentRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, object] = {}

    async def get(self, document_id: UUID) -> object | None:
        return self.rows.get(document_id)


class _CollectionRepository(_DocumentRepository):
    pass


class _CheckpointRepository:
    FIELDS = (
        "source_content_sha256",
        "profile",
        "contract_version",
        "prompt_version",
        "parser_version",
        "verifier_version",
        "source_text_contract_version",
        "model_policy_version",
        "routing_policy_version",
    )

    def __init__(self) -> None:
        self.rows: dict[tuple[str, ...], SourceExtraction] = {}
        self.lookups = 0

    @classmethod
    def _key(cls, values: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(values[field] for field in cls.FIELDS)

    @classmethod
    def _row_key(cls, row: SourceExtraction) -> tuple[str, ...]:
        return cls._key(
            {
                "source_content_sha256": row.source_content_sha256,
                "profile": row.profile.value,
                "contract_version": row.contract_version,
                "prompt_version": row.prompt_version,
                "parser_version": row.parser_version,
                "verifier_version": row.verifier_version,
                "source_text_contract_version": row.source_text_contract_version,
                "model_policy_version": row.model_policy_version,
                "routing_policy_version": row.routing_policy_version,
            }
        )

    async def get_by_identity(self, **values: str) -> SourceExtraction | None:
        self.lookups += 1
        return self.rows.get(self._key(values))

    async def claim(self, extraction: SourceExtraction, *, force: bool = False) -> bool:
        key = self._row_key(extraction)
        existing = self.rows.get(key)
        if (
            existing is not None
            and existing.status is SourceExtractionStatus.VERIFIED
            and not force
        ):
            return False
        self.rows[key] = extraction
        return True

    async def save(self, extraction: SourceExtraction) -> None:
        self.rows[self._row_key(extraction)] = extraction


class _Uow:
    def __init__(self, world: _World) -> None:
        self.production_artifacts = world.artifacts
        self.source_documents = world.documents
        self.source_collections = world.collections
        self.source_extractions = world.extractions

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        return None


class _World:
    def __init__(self) -> None:
        self.blobs = _BlobStore()
        self.artifacts = _ArtifactRepository()
        self.documents = _DocumentRepository()
        self.collections = _CollectionRepository()
        self.extractions = _CheckpointRepository()

    def uow(self) -> _Uow:
        return _Uow(self)


class _PreSubmissionFailure(ModelGatewayError):
    code = "bridge_unreachable"
    retryable = True


def _q2_wire_output(output: Q2SourceOutput) -> str:
    lines: list[str] = []

    def add_item(value: str, context: str = "") -> None:
        suffix = f" :: {context}" if context else ""
        lines.append(f"- {value}{suffix}")

    for fact in output.facts:
        lines.append(f"FACT {fact.category}")
        add_item(fact.value, fact.evidence_quote or fact.value)
    for event in output.events:
        event_date = event.event_date.isoformat() if event.event_date is not None else ""
        date_text = event.date_text or ""
        specification = event_date or date_text
        lines.append(f"EVENT {specification}".rstrip())
        add_item(event.text, event.evidence_quote or event.text)
    for artifact in output.artifacts:
        if artifact.indicator_status == "excluded":
            continue
        status = "confirmed" if artifact.indicator_status == "confirmed_ioc" else "contextual"
        lines.append(f"IOC {status} {artifact.artifact_type}")
        add_item(artifact.value, artifact.context)
    for rule in output.rules:
        name = f": {rule.name}" if rule.name else ""
        lines.append(f"RULE {rule.rule_type.value}{name}")
        fence = chr(96) * 3
        lines.extend((f"{fence}{rule.rule_type.value}", rule.body, fence))
    if output.uncertainties:
        lines.append("UNCERTAINTIES")
        for uncertainty in output.uncertainties:
            add_item(uncertainty)
    return "\n".join(lines) if lines else "EMPTY"


class _StructuredGateway:
    """A fake gateway that exposes only text output to canonical extraction."""

    name = "provider-a-structured"

    def __init__(self, outputs: Mapping[str, Q2SourceOutput] | None = None) -> None:
        self.outputs = dict(outputs or {})
        self.calls: list[ModelRequest] = []
        self.draft_calls: list[ModelRequest] = []
        self.extract_calls = 0
        self.ambiguity = False
        self.fail_markers: set[str] = set()
        self.transient_markers: set[str] = set()

    def output_for(self, text: str) -> Q2SourceOutput:
        for marker, output in self.outputs.items():
            if marker in text:
                return output
        return Q2SourceOutput()

    async def extract(self, request: ModelRequest, output_schema: type[Any]) -> object:
        del request, output_schema
        self.extract_calls += 1
        raise AssertionError("Canonical extraction must not request structured output")

    async def draft(self, request: ModelRequest) -> object:
        self.calls.append(request)
        self.draft_calls.append(request)
        if not request.external_llm_allowed:
            raise ExternalModelBlockedError("external model use is not allowed")
        if self.ambiguity:
            raise ModelSubmissionReconciliationRequiredError(
                "The submission state is unknown",
                details={"provider_reference": "opaque"},
            )
        if any(marker in request.text for marker in self.transient_markers):
            raise _PreSubmissionFailure("the provider was not reached")
        if any(marker in request.text for marker in self.fail_markers):
            return self._unreadable()
        text = (
            self._batch_wire_text(request.text)
            if request.prompt_template_id.endswith("-batch")
            else self._single_wire_text(request)
        )
        return self._execution(text)

    def _single_wire_text(self, request: ModelRequest) -> str:
        return _q2_wire_output(self.output_for(request.text))

    def _batch_wire_text(self, prompt: str) -> str:
        return "\n\n".join(
            f"@@Q2:{handle}@@\n{_q2_wire_output(self.output_for(body))}"
            for handle, body in _batch_blocks(prompt)
        )

    def _unreadable(self) -> object:
        return SimpleNamespace(
            run=SimpleNamespace(
                id=uuid4(),
                status=ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            structured_output=None,
            output_text="",
            metadata={},
        )

    def _execution(self, text: str) -> object:
        return SimpleNamespace(
            run=SimpleNamespace(
                id=uuid4(),
                status=ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            structured_output=None,
            output_text=text,
            metadata={},
        )


class _JsonTextGateway(_StructuredGateway):
    """Provider B: emits plain wire text with a bridge citation marker."""

    name = "provider-b-json-text"

    def _single_wire_text(self, request: ModelRequest) -> str:
        lines = _q2_wire_output(self.output_for(request.text)).splitlines()
        in_uncertainties = False
        for index, line in enumerate(lines):
            if line.strip().casefold() == "uncertainties":
                in_uncertainties = True
            elif in_uncertainties and line.startswith("- "):
                lines[index] = f'{line} :chatgpt-content-reference{{index="0"}}'
                break
        return "\n".join(lines)


class _UnreadableGateway(_StructuredGateway):
    """Provider C: answers with nothing usable, without inventing content."""

    name = "provider-c-unreadable"

    async def draft(self, request: ModelRequest) -> object:
        self.calls.append(request)
        self.draft_calls.append(request)
        return self._execution("")


def _batch_blocks(prompt: str) -> tuple[tuple[str, str], ...]:
    matches = list(re.finditer(r"@@Q2:(B\d+)@@", prompt))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        blocks.append((match.group(1), prompt[match.end() : end]))
    return tuple(blocks)


# --- scenario helpers -------------------------------------------------------


def _snapshot(subject_id: UUID) -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        edition_id=uuid4(),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Summary",
        actor_or_campaign="Actor-X",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        publication_language="fr",
        research_date=date(2026, 8, 1),
    )


def _register_source(
    world: _World,
    *,
    subject_id: UUID,
    url: str,
    text: str,
    tlp: TLP = TLP.CLEAR,
    external: bool = True,
    do_not_submit: bool = False,
) -> tuple[UUID, str]:
    blob_id, sha256 = world.blobs.put_bytes(text.encode("utf-8"))
    document_id, collection_id = uuid4(), uuid4()
    world.documents.rows[document_id] = SimpleNamespace(
        id=document_id,
        subject_id=subject_id,
        source_collection_id=collection_id,
        decoded_blob_id=blob_id,
        detected_mime_type="text/plain",
        tlp=tlp,
        external_llm_allowed=external,
        do_not_submit=do_not_submit,
    )
    world.collections.rows[collection_id] = SimpleNamespace(
        id=collection_id,
        subject_id=subject_id,
        canonical_url=url,
        source_document_id=document_id,
        source_tlp=tlp,
        sensitivity="public",
        external_llm_allowed=external,
        do_not_submit=do_not_submit,
    )
    return document_id, sha256


def _reference(
    *,
    url: str,
    tier: ProductionReferenceTier,
    document_id: UUID | None,
    sha256: str | None,
    role: SourceRole = SourceRole.PRIMARY,
    kind: ProductionReferenceKind = ProductionReferenceKind.PUBLICATION,
    state: CollectionState = CollectionState.ARCHIVED,
) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=kind,
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


def _corpus(
    *,
    subject_id: UUID,
    input_hash: str,
    sources: tuple[ProductionReferenceSourceV1, ...],
) -> ProductionReferenceCorpusV1:
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=date(2026, 8, 1),
        production_input_hash=input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )


def _publish(
    world: _World,
    corpus: ProductionReferenceCorpusV1,
    *,
    status: ProductionArtifactStatus = ProductionArtifactStatus.VERIFIED,
) -> None:
    blob_id = world.blobs.put_json(production_reference_corpus_to_json(corpus))
    world.artifacts.artifact = SimpleNamespace(id=uuid4(), status=status, canonical_blob_id=blob_id)


async def _execute(
    world: _World,
    gateway: object,
    *,
    corpus: ProductionReferenceCorpusV1,
    subject_id: UUID,
    snapshot: ProductionInputSnapshot,
    batching: bool = True,
) -> ProductionExtractionExecution:
    service = ProductionExtractionService(
        uow_factory=world.uow,
        model_gateway=gateway,  # type: ignore[arg-type]
        artifact_store=world.blobs,
        ioc_rules_batching=batching,
    )
    run = ProductionRun(subject_id=subject_id, edition_id=uuid4())
    return await service.execute(run=run, snapshot=snapshot)


def _core_world() -> tuple[_World, ProductionInputSnapshot, ProductionReferenceCorpusV1]:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    document_id, sha256 = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=document_id,
                sha256=sha256,
            ),
        ),
    )
    _publish(world, corpus)
    return world, snapshot, corpus


# --- planning ---------------------------------------------------------------


def test_plan_profile_follows_tier_and_never_role() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    sources = []
    for index, role in enumerate(
        (
            SourceRole.PRIMARY,
            SourceRole.INDEPENDENT,
            SourceRole.RELAY,
            SourceRole.AGGREGATOR,
            SourceRole.UNKNOWN,
        )
    ):
        document_id, sha256 = _register_source(
            world,
            subject_id=subject_id,
            url=f"https://example.test/core-{index}",
            text=f"Core capture {index}",
        )
        sources.append(
            _reference(
                url=f"https://example.test/core-{index}",
                tier=ProductionReferenceTier.CORE,
                document_id=document_id,
                sha256=sha256,
                role=role,
            )
        )
    for index, (tier, role) in enumerate(
        (
            (ProductionReferenceTier.SUPPORTING, SourceRole.PRIMARY),
            (ProductionReferenceTier.SUPPORTING, SourceRole.INDEPENDENT),
            (ProductionReferenceTier.TECHNICAL, SourceRole.PRIMARY),
            (ProductionReferenceTier.TECHNICAL, SourceRole.UNKNOWN),
        )
    ):
        document_id, sha256 = _register_source(
            world,
            subject_id=subject_id,
            url=f"https://example.test/other-{index}",
            text=f"Other capture {index}",
        )
        sources.append(
            _reference(
                url=f"https://example.test/other-{index}",
                tier=tier,
                document_id=document_id,
                sha256=sha256,
                role=role,
            )
        )
    corpus = _corpus(subject_id=subject_id, input_hash=snapshot.input_hash, sources=tuple(sources))

    plan = build_extraction_plan(corpus)

    profiles = {source.canonical_url: source.profile for source in plan.sources}
    for index in range(5):
        assert profiles[f"https://example.test/core-{index}"] is ExtractionProfile.FULL
    assert profiles["https://example.test/other-0"] is ExtractionProfile.IOC_RULES
    assert profiles["https://example.test/other-1"] is ExtractionProfile.IOC_RULES
    assert profiles["https://example.test/other-2"] is ExtractionProfile.IOC_RULES
    assert profiles["https://example.test/other-3"] is ExtractionProfile.IOC_RULES
    assert plan.profile_policy_version == "production-reference-tier-core-only-v3"


def test_plan_and_hashes_are_stable_when_corpus_order_changes() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    references = []
    for index in range(3):
        document_id, sha256 = _register_source(
            world,
            subject_id=subject_id,
            url=f"https://example.test/stable-{index}",
            text=f"Capture {index}",
        )
        references.append(
            _reference(
                url=f"https://example.test/stable-{index}",
                tier=ProductionReferenceTier.CORE,
                document_id=document_id,
                sha256=sha256,
            )
        )
    first = _corpus(
        subject_id=subject_id, input_hash=snapshot.input_hash, sources=tuple(references)
    )
    second = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=tuple(reversed(references)),
    )

    plan_a = build_extraction_plan(first)
    plan_b = build_extraction_plan(second)

    assert plan_a.input_hash == plan_b.input_hash
    assert [source.source_document_id for source in plan_a.sources] == [
        source.source_document_id for source in plan_b.sources
    ]
    assert production_extraction.references_corpus_hash(first) == (
        production_extraction.references_corpus_hash(second)
    )


def test_plan_requires_one_usable_core_source() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    document_id, sha256 = _register_source(
        world, subject_id=subject_id, url=SUPPORT_URL, text=SUPPORT_TEXT
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=SUPPORT_URL,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=document_id,
                sha256=sha256,
            ),
        ),
    )

    with pytest.raises(ProductionExtractionControlError) as raised:
        build_extraction_plan(corpus)

    assert raised.value.code == ExtractionControlCode.REFERENCE_CORPUS_INVALID.value


# --- corpus control invariants ---------------------------------------------


async def test_references_artifact_must_be_verified_with_a_canonical_corpus() -> None:
    world, snapshot, corpus = _core_world()
    world.artifacts.artifact = None

    missing = await _execute(
        world,
        _StructuredGateway(),
        corpus=corpus,
        subject_id=snapshot.subject_id,
        snapshot=snapshot,
    )
    assert missing.status is ExtractionExecutionStatus.BLOCKED
    assert missing.error_code == ExtractionControlCode.REFERENCE_CORPUS_MISSING.value

    unverified = SimpleNamespace(
        id=uuid4(), status=ProductionArtifactStatus.STALE, canonical_blob_id=uuid4()
    )
    world.artifacts.artifact = unverified
    stale = await _execute(
        world,
        _StructuredGateway(),
        corpus=corpus,
        subject_id=snapshot.subject_id,
        snapshot=snapshot,
    )
    assert stale.status is ExtractionExecutionStatus.BLOCKED
    assert stale.error_code == ExtractionControlCode.REFERENCE_CORPUS_INVALID.value

    world.artifacts.artifact = SimpleNamespace(
        id=uuid4(), status=ProductionArtifactStatus.VERIFIED, canonical_blob_id=None
    )
    raw_only = await _execute(
        world,
        _StructuredGateway(),
        corpus=corpus,
        subject_id=snapshot.subject_id,
        snapshot=snapshot,
    )
    assert raw_only.status is ExtractionExecutionStatus.BLOCKED
    assert raw_only.error_code == ExtractionControlCode.REFERENCE_CORPUS_MISSING.value


async def test_corpus_subject_and_snapshot_mismatch_are_control_errors() -> None:
    world, snapshot, corpus = _core_world()
    other_subject = uuid4()
    other = _corpus(
        subject_id=other_subject,
        input_hash=snapshot.input_hash,
        sources=corpus.sources,
    )
    _publish(world, other)

    async with world.uow() as uow:
        with pytest.raises(ProductionExtractionControlError) as subject_error:
            await load_reference_corpus(
                uow=uow,
                run=ProductionRun(subject_id=snapshot.subject_id, edition_id=uuid4()),
                snapshot=snapshot,
                artifact_store=world.blobs,
            )
    assert subject_error.value.code == ExtractionControlCode.REFERENCE_SUBJECT_MISMATCH.value

    drifted = _corpus(subject_id=snapshot.subject_id, input_hash="c" * 64, sources=corpus.sources)
    _publish(world, drifted)
    async with world.uow() as uow:
        with pytest.raises(ProductionExtractionControlError) as snapshot_error:
            await load_reference_corpus(
                uow=uow,
                run=ProductionRun(subject_id=snapshot.subject_id, edition_id=uuid4()),
                snapshot=snapshot,
                artifact_store=world.blobs,
            )
    assert snapshot_error.value.code == ExtractionControlCode.REFERENCE_SNAPSHOT_MISMATCH.value


async def test_ineligible_source_is_omitted_without_any_model_call() -> None:
    world, snapshot, corpus = _core_world()
    unavailable = _reference(
        url=TECH_URL,
        tier=ProductionReferenceTier.TECHNICAL,
        document_id=None,
        sha256=None,
        role=SourceRole.INDEPENDENT,
        state=CollectionState.UNAVAILABLE,
    )
    corpus = _corpus(
        subject_id=snapshot.subject_id,
        input_hash=snapshot.input_hash,
        sources=(*corpus.sources, unavailable),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 1
    assert execution.extraction is not None
    assert execution.extraction.omitted_sources == (
        production_extraction.ProductionExtractionOmissionV1(
            canonical_url=TECH_URL,
            tier=ProductionReferenceTier.TECHNICAL,
            collection_state=CollectionState.UNAVAILABLE,
            reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
            error_code=None,
        ),
    )
    assert any("reference_not_eligible" in warning for warning in execution.extraction.warnings)


# --- exact document integrity ----------------------------------------------


async def test_archived_document_sha_divergence_blocks_before_any_model_call() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    document_id, _real_sha256 = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=document_id,
                sha256="0" * 64,
            ),
        ),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.BLOCKED
    assert execution.error_code == ExtractionControlCode.SOURCE_CONTENT_MISMATCH.value
    assert execution.extraction is None
    assert gateway.calls == []


async def test_missing_exact_document_is_an_explicit_control_error() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=uuid4(),
                sha256="a" * 64,
            ),
        ),
    )
    _publish(world, corpus)

    execution = await _execute(
        world,
        _StructuredGateway(),
        corpus=corpus,
        subject_id=subject_id,
        snapshot=snapshot,
    )

    assert execution.status is ExtractionExecutionStatus.BLOCKED
    assert execution.error_code == ExtractionControlCode.SOURCE_DOCUMENT_MISSING.value


# --- provider-facing contract ----------------------------------------------


async def test_archived_capture_is_the_only_material_sent_to_the_model() -> None:
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    request = gateway.calls[0]
    assert request.web_search is False
    assert request.routing_hint is ModelRoutingHint.BULK_EXTRACTION
    assert request.external_llm_allowed is True
    assert CORE_TEXT.splitlines()[0] in request.text
    assert CORE_URL not in request.text
    assert str(snapshot.subject_id) not in request.text
    assert request.metadata["profile"] == ExtractionProfile.FULL.value
    assert request.metadata["tier"] == ProductionReferenceTier.CORE.value
    assert request.metadata["tlp"] == TLP.CLEAR.value
    assert request.metadata["do_not_submit"] is False


async def test_dirty_bridge_text_is_partial_and_raw_checkpoint_is_preserved() -> None:
    raw_text = """FACT unknown_category
- ignored
FACT malware
- ExampleRAT
IOC confirmed domain
- evil.security-lab.io :: C2 indicator :chatgpt-content-reference{index="0"}
UNCERTAINTIES
- Attribution of Actor-X remains unconfirmed.
"""

    class _DirtyBridgeGateway(_StructuredGateway):
        def _single_wire_text(self, request: ModelRequest) -> str:
            del request
            return raw_text

    world, snapshot, corpus = _core_world()
    gateway = _DirtyBridgeGateway()

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert execution.extraction is not None
    source = execution.extraction.sources[0]
    assert [fact.value for fact in source.facts] == ["ExampleRAT"]
    assert [indicator.value for indicator in source.indicators] == ["evil.security-lab.io"]
    assert any(
        warning == "q2_parse_warning:q2_unknown_fact_category"
        for warning in execution.extraction.warnings
    )
    assert raw_text in world.blobs.raws.values()
    assert gateway.extract_calls == 0
    assert len(gateway.draft_calls) == 1


async def test_unrecognized_chunk_does_not_sink_a_source_with_other_usable_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(production_extraction, "SOURCE_CHUNK_MAX_CHARS", 120)

    class _PartialChunkGateway(_StructuredGateway):
        def _single_wire_text(self, request: ModelRequest) -> str:
            if request.metadata["chunk_index"] == 1:
                return "bridge prose with no recognized Q2 item"
            return _q2_wire_output(_full_output())

    world, snapshot, corpus = _core_world()
    gateway = _PartialChunkGateway()

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert execution.extraction is not None
    assert execution.extraction.sources[0].facts
    assert any(
        warning == "q2_parse_error:q2_compact_sections_missing"
        for warning in execution.extraction.warnings
    )


def test_archived_source_chunks_preserve_word_boundaries_and_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(production_extraction, "SOURCE_CHUNK_MAX_CHARS", 48)
    monkeypatch.setattr(production_extraction, "SOURCE_CHUNK_OVERLAP_CHARS", 12)
    source_text = "alpha beta gamma delta epsilon zeta eta theta Address-port " + "remaining " * 16

    chunks = production_extraction.archived_source_chunks(source_text)

    assert len(chunks) > 1
    raw_words = source_text.split()
    retained_words = {word for chunk in chunks for word in chunk.split()}
    assert sum(word in retained_words for word in raw_words) == len(raw_words)
    assert all(not chunk.endswith("Address-po") for chunk in chunks)
    assert all(not chunk.startswith("rt ") for chunk in chunks)
    assert any("Address-port" in chunk for chunk in chunks)
    assert set(chunks[0].split()) & set(chunks[1].split())


async def test_two_providers_produce_the_same_canonical_contract() -> None:
    outputs = {"ExampleRAT": _full_output()}
    first_world, first_snapshot, first_corpus = _core_world()
    second_world, second_snapshot, second_corpus = _core_world()
    first_gateway = _StructuredGateway(outputs)
    second_gateway = _JsonTextGateway(outputs)

    first = await _execute(
        first_world,
        first_gateway,
        corpus=first_corpus,
        subject_id=first_snapshot.subject_id,
        snapshot=first_snapshot,
    )
    second = await _execute(
        second_world,
        second_gateway,
        corpus=second_corpus,
        subject_id=second_snapshot.subject_id,
        snapshot=second_snapshot,
    )

    assert first.status is ExtractionExecutionStatus.SUCCEEDED
    assert second.status is ExtractionExecutionStatus.SUCCEEDED
    assert first.extraction is not None and second.extraction is not None
    first_source = first.extraction.sources[0]
    second_source = second.extraction.sources[0]
    assert [fact.value for fact in first_source.facts] == [
        fact.value for fact in second_source.facts
    ]
    assert [indicator.value for indicator in first_source.indicators] == [
        indicator.value for indicator in second_source.indicators
    ]
    assert [rule.sha256 for rule in first_source.rules] == [
        rule.sha256 for rule in second_source.rules
    ]
    assert not any("provider" in warning for warning in first.extraction.warnings)


async def test_only_locally_proven_proposals_become_canonical() -> None:
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.extraction is not None
    source = execution.extraction.sources[0]
    assert [fact.value for fact in source.facts] == [
        "ExampleRAT",
        "powershell -enc ZXhhbXBsZQ==",
        "Actor-X",
    ]
    assert [event.text for event in source.events] == [
        "ExampleRAT was deployed by Actor-X on 2026-07-10."
    ]
    assert [indicator.value for indicator in source.indicators] == [
        "evil.security-lab.io",
        "CVE-2026-12345",
    ]
    assert [rule.name for rule in source.rules] == ["ExampleRAT"]
    assert source.uncertainties == ("Attribution of Actor-X remains unconfirmed.",)
    for item in (*source.facts, *source.events, *source.indicators, *source.rules):
        assert item.evidence_basis is ProductionEvidenceBasis.SOURCE_VERIFIED
        assert item.source_document_ids == (source.source_document_id,)
        assert item.evidence_quote
    assert any(
        "extraction_proposal_rejected" in warning for warning in execution.extraction.warnings
    )


async def test_ioc_rules_projects_away_narrative_content() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    core_document, core_sha = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    support_document, support_sha = _register_source(
        world, subject_id=subject_id, url=SUPPORT_URL, text=SUPPORT_TEXT
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_document,
                sha256=core_sha,
            ),
            _reference(
                url=SUPPORT_URL,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=support_document,
                sha256=support_sha,
            ),
        ),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway(
        {"ExampleRAT": _full_output(), "loader.security-lab.io": _support_output()}
    )

    execution = await _execute(
        world,
        gateway,
        corpus=corpus,
        subject_id=subject_id,
        snapshot=snapshot,
        batching=False,
    )

    assert execution.extraction is not None
    by_url = {source.canonical_url: source for source in execution.extraction.sources}
    support = by_url[SUPPORT_URL]
    assert support.profile is ExtractionProfile.IOC_RULES
    assert support.facts == ()
    assert support.events == ()
    assert [indicator.value for indicator in support.indicators] == ["loader.security-lab.io"]
    assert [rule.name for rule in support.rules] == ["SupportRule"]
    assert support.uncertainties == ("The loader family is unconfirmed.",)


# --- checkpoints and reuse --------------------------------------------------


async def test_checkpoint_reuse_never_calls_the_provider_again() -> None:
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    first = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )
    second = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert first.extraction is not None and second.extraction is not None
    assert len(gateway.calls) == 1
    assert first.extraction.sources[0].reuse_state is ExtractionReuseState.FRESH
    assert second.extraction.sources[0].reuse_state is ExtractionReuseState.REUSED
    assert second.extraction.sources[0].checkpoint_id == first.extraction.sources[0].checkpoint_id
    assert second.extraction.sources[0].facts == first.extraction.sources[0].facts


async def test_full_checkpoint_satisfies_ioc_rules_but_not_the_reverse() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    core_document, core_sha = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    mirror_document, mirror_sha = _register_source(
        world, subject_id=subject_id, url=MIRROR_URL, text=CORE_TEXT
    )
    support_document, support_sha = _register_source(
        world, subject_id=subject_id, url=SUPPORT_URL, text=SUPPORT_TEXT
    )
    core_only = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_document,
                sha256=core_sha,
            ),
        ),
    )
    _publish(world, core_only)
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    first = await _execute(
        world, gateway, corpus=core_only, subject_id=subject_id, snapshot=snapshot
    )
    assert first.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 1

    # The same bytes, reached through a SUPPORTING and a TECHNICAL capture.
    support_corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_document,
                sha256=core_sha,
            ),
            _reference(
                url=MIRROR_URL,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=mirror_document,
                sha256=mirror_sha,
            ),
            _reference(
                url=SUPPORT_URL,
                tier=ProductionReferenceTier.TECHNICAL,
                document_id=support_document,
                sha256=support_sha,
            ),
        ),
    )
    _publish(world, support_corpus)

    second = await _execute(
        world,
        gateway,
        corpus=support_corpus,
        subject_id=subject_id,
        snapshot=snapshot,
        batching=False,
    )

    assert second.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 2
    assert second.extraction is not None
    by_url = {source.canonical_url: source for source in second.extraction.sources}
    # The FULL checkpoint of the same bytes satisfies the lighter profile.
    assert by_url[MIRROR_URL].reuse_state is ExtractionReuseState.REUSED
    assert by_url[MIRROR_URL].facts == ()  # projected from the FULL checkpoint
    assert [indicator.value for indicator in by_url[MIRROR_URL].indicators] == [
        "evil.security-lab.io",
        "CVE-2026-12345",
    ]

    # An IOC_RULES checkpoint never satisfies a FULL demand.
    reversed_corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_document,
                sha256=core_sha,
            ),
            _reference(
                url=SUPPORT_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=support_document,
                sha256=support_sha,
            ),
        ),
    )
    _publish(world, reversed_corpus)
    gateway.outputs["loader.security-lab.io"] = _support_output()

    third = await _execute(
        world,
        gateway,
        corpus=reversed_corpus,
        subject_id=subject_id,
        snapshot=snapshot,
        batching=False,
    )

    assert third.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 3
    assert third.extraction is not None
    upgraded = {source.canonical_url: source for source in third.extraction.sources}[SUPPORT_URL]
    assert upgraded.profile is ExtractionProfile.FULL
    assert upgraded.reuse_state is ExtractionReuseState.FRESH


async def test_content_change_and_policy_change_never_reuse_a_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})
    first = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )
    assert first.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 1

    changed_document, changed_sha = _register_source(
        world,
        subject_id=snapshot.subject_id,
        url=CORE_URL,
        text=CORE_TEXT + "A new paragraph was appended.\n",
    )
    changed_corpus = _corpus(
        subject_id=snapshot.subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=changed_document,
                sha256=changed_sha,
            ),
        ),
    )
    _publish(world, changed_corpus)
    second = await _execute(
        world, gateway, corpus=changed_corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )
    assert second.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 2
    assert second.extraction is not None
    assert second.extraction.sources[0].reuse_state is ExtractionReuseState.FRESH

    monkeypatch.setattr(
        production_extraction, "EXTRACTION_MODEL_POLICY_VERSION", "archive-source-extraction-v2"
    )
    third = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )
    assert third.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 3
    assert third.extraction is not None
    assert third.extraction.sources[0].reuse_state is ExtractionReuseState.FRESH


# --- duplicates and batching ------------------------------------------------


def test_chunk_merge_keeps_later_evidence_quotes_for_duplicate_proposals() -> None:
    merged = merge_q2_source_outputs(
        (
            Q2SourceOutput(
                facts=[Q2FactProposal(category="malware", value="ExampleRAT")],
                events=[Q2EventProposal(text="Operators deployed ExampleRAT")],
                artifacts=[
                    Q2ArtifactProposal(
                        value="evil.example",
                        artifact_type="domain",
                        indicator_status="confirmed_ioc",
                    )
                ],
            ),
            Q2SourceOutput(
                facts=[
                    Q2FactProposal(
                        category="malware",
                        value="ExampleRAT",
                        evidence_quote="The campaign used ExampleRAT.",
                    )
                ],
                events=[
                    Q2EventProposal(
                        text="Operators deployed ExampleRAT",
                        evidence_quote="On 2024-03-02 operators deployed ExampleRAT.",
                    )
                ],
                artifacts=[
                    Q2ArtifactProposal(
                        value="evil.example",
                        artifact_type="domain",
                        indicator_status="confirmed_ioc",
                        evidence_quote="C2 was hosted at evil.example.",
                    )
                ],
            ),
        )
    )

    assert len(merged.facts) == len(merged.events) == len(merged.artifacts) == 1
    assert merged.facts[0].evidence_quote == "The campaign used ExampleRAT."
    assert merged.events[0].evidence_quote == "On 2024-03-02 operators deployed ExampleRAT."
    assert merged.artifacts[0].evidence_quote == "C2 was hosted at evil.example."


async def test_duplicate_content_is_computed_once_and_keeps_both_sources() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    first_document, first_sha = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    mirror_document, mirror_sha = _register_source(
        world, subject_id=subject_id, url=MIRROR_URL, text=CORE_TEXT
    )
    assert first_sha == mirror_sha
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=first_document,
                sha256=first_sha,
            ),
            _reference(
                url=MIRROR_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=mirror_document,
                sha256=mirror_sha,
            ),
        ),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) == 1
    assert execution.extraction is not None
    by_url = {source.canonical_url: source for source in execution.extraction.sources}
    assert set(by_url) == {CORE_URL, MIRROR_URL}
    assert by_url[CORE_URL].reuse_state is ExtractionReuseState.FRESH
    assert by_url[MIRROR_URL].reuse_state is ExtractionReuseState.CONTENT_DUPLICATE
    assert by_url[MIRROR_URL].source_document_id == mirror_document
    assert by_url[MIRROR_URL].content_sha256 == first_sha
    assert [fact.value for fact in by_url[MIRROR_URL].facts] == [
        fact.value for fact in by_url[CORE_URL].facts
    ]


async def test_ioc_rules_batch_keeps_an_unambiguous_source_mapping() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    core_document, core_sha = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    references = [
        _reference(
            url=CORE_URL,
            tier=ProductionReferenceTier.CORE,
            document_id=core_document,
            sha256=core_sha,
        )
    ]
    outputs: dict[str, Q2SourceOutput] = {"ExampleRAT": _full_output()}
    for index in range(1, 4):
        marker = f"Marker{index}"
        url = f"https://example.test/batch-{index}"
        text = f"{marker} beaconed to host{index}.security-lab.io in July 2026.\n"
        document_id, sha256 = _register_source(world, subject_id=subject_id, url=url, text=text)
        references.append(
            _reference(
                url=url,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=document_id,
                sha256=sha256,
            )
        )
        outputs[marker] = Q2SourceOutput(
            artifacts=[
                Q2ArtifactProposal(
                    value=f"host{index}.security-lab.io",
                    artifact_type="domain",
                    indicator_status="confirmed_ioc",
                )
            ]
        )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=tuple(references),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway(outputs)

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    batch_calls = [request for request in gateway.calls if "@@Q2:B1@@" in request.text]
    assert len(batch_calls) == 1
    assert len(gateway.calls) == 2
    assert len(batch_calls[0].metadata["batch_sources"]) == 3
    assert execution.extraction is not None
    by_url = {source.canonical_url: source for source in execution.extraction.sources}
    for index in range(1, 4):
        source = by_url[f"https://example.test/batch-{index}"]
        assert [indicator.value for indicator in source.indicators] == [
            f"host{index}.security-lab.io"
        ]
        assert source.indicators[0].source_document_ids == (source.source_document_id,)


async def test_ambiguous_batch_handle_falls_back_to_individual_readings() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    references = []
    outputs: dict[str, Q2SourceOutput] = {}
    for index in range(1, 4):
        marker = f"Marker{index}"
        url = f"https://example.test/fallback-{index}"
        text = f"{marker} beaconed to host{index}.security-lab.io in July 2026.\n"
        document_id, sha256 = _register_source(world, subject_id=subject_id, url=url, text=text)
        references.append(
            _reference(
                url=url,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=document_id,
                sha256=sha256,
            )
        )
        outputs[marker] = Q2SourceOutput(
            artifacts=[
                Q2ArtifactProposal(
                    value=f"host{index}.security-lab.io",
                    artifact_type="domain",
                    indicator_status="confirmed_ioc",
                )
            ]
        )
    core_document, core_sha = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    references.insert(
        0,
        _reference(
            url=CORE_URL,
            tier=ProductionReferenceTier.CORE,
            document_id=core_document,
            sha256=core_sha,
        ),
    )
    outputs["ExampleRAT"] = _full_output()
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=tuple(references),
    )
    _publish(world, corpus)

    class _AmbiguousBatchGateway(_StructuredGateway):
        def _batch_wire_text(self, prompt: str) -> str:
            handle, body = _batch_blocks(prompt)[0]
            output_text = _q2_wire_output(self.output_for(body))
            return f"@@Q2:{handle}@@\n{output_text}\n@@Q2:{handle}@@\n{output_text}"

    gateway = _AmbiguousBatchGateway(outputs)

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert execution.extraction is not None
    by_url = {source.canonical_url: source for source in execution.extraction.sources}
    for index in range(1, 4):
        source = by_url[f"https://example.test/fallback-{index}"]
        assert [indicator.value for indicator in source.indicators] == [
            f"host{index}.security-lab.io"
        ]
    assert execution.extraction is not None
    assert any(
        "extraction_batch_source_unattributed" in warning
        for warning in execution.extraction.warnings
    )


# --- failure classes --------------------------------------------------------


async def test_submission_ambiguity_returns_needs_review_without_replay() -> None:
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})
    gateway.ambiguity = True

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.NEEDS_REVIEW
    assert execution.extraction is None
    assert execution.error_code == "model_submission_reconciliation_required"
    assert execution.details["provider_reference"] == "opaque"
    # The job handler records exactly this ModelRun as the one to reconcile.
    assert execution.details["model_run_id"] == str(gateway.calls[0].run_id)
    assert len(gateway.calls) == 1


async def test_core_source_failure_blocks_the_stage() -> None:
    world, snapshot, corpus = _core_world()
    gateway = _UnreadableGateway()  # the provider answers nothing usable

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.BLOCKED
    assert execution.extraction is None
    assert execution.error_code == "extraction_core_source_failed"
    assert execution.details["source_failure_code"] == "extraction_source_output_invalid"
    assert len(gateway.calls) == 1


async def test_non_core_failure_is_a_controlled_omission() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    core_document, core_sha = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    support_document, support_sha = _register_source(
        world, subject_id=subject_id, url=SUPPORT_URL, text=SUPPORT_TEXT
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_document,
                sha256=core_sha,
            ),
            _reference(
                url=SUPPORT_URL,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=support_document,
                sha256=support_sha,
            ),
        ),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})
    gateway.fail_markers = {"loader.security-lab.io"}

    execution = await _execute(
        world,
        gateway,
        corpus=corpus,
        subject_id=subject_id,
        snapshot=snapshot,
        batching=False,
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert execution.extraction is not None
    assert [source.canonical_url for source in execution.extraction.sources] == [CORE_URL]
    assert any(
        f"extraction_source_skipped:{SUPPORT_URL}:extraction_source_output_invalid" in warning
        for warning in execution.extraction.warnings
    )
    assert execution.extraction.omitted_sources == (
        production_extraction.ProductionExtractionOmissionV1(
            canonical_url=SUPPORT_URL,
            tier=ProductionReferenceTier.SUPPORTING,
            collection_state=CollectionState.ARCHIVED,
            reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
            error_code="extraction_source_output_invalid",
        ),
    )


async def test_do_not_submit_policy_is_never_overridden() -> None:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    document_id, sha256 = _register_source(
        world,
        subject_id=subject_id,
        url=CORE_URL,
        text=CORE_TEXT,
        do_not_submit=True,
        external=False,
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=document_id,
                sha256=sha256,
            ),
        ),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.BLOCKED
    assert execution.details["source_failure_code"] == "extraction_source_policy_blocked"
    assert gateway.calls[0].external_llm_allowed is False


async def test_oversized_capture_is_chunked_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(production_extraction, "SOURCE_CHUNK_MAX_CHARS", 120)
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert len(gateway.calls) > 1
    assert all(request.web_search is False for request in gateway.calls)
    assert execution.extraction is not None
    source = execution.extraction.sources[0]
    assert [fact.value for fact in source.facts] == [
        "ExampleRAT",
        "powershell -enc ZXhhbXBsZQ==",
        "Actor-X",
    ]
    assert len({fact.value for fact in source.facts}) == len(source.facts)


# --- legacy compatibility boundary -----------------------------------------


async def test_legacy_projection_is_one_way_and_reproducible() -> None:
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})
    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )
    assert execution.extraction is not None
    canonical = execution.extraction

    projected = project_legacy_technical_extraction(canonical)

    assert [item.value for item in projected.items] == [
        "ExampleRAT",
        "powershell -enc ZXhhbXBsZQ==",
        "Actor-X",
        "ExampleRAT was deployed by Actor-X on 2026-07-10.",
        "evil.security-lab.io",
        "CVE-2026-12345",
    ]
    assert {rule.name for rule in projected.rules} == {"ExampleRAT"}
    assert projected.uncertainties == ("Attribution of Actor-X remains unconfirmed.",)
    assert all(item.supported for item in projected.items)
    assert all(
        item.source_ids == (str(canonical.sources[0].source_document_id),)
        for item in projected.items
    )

    view = extraction_compatibility_view(production_extraction_to_json(canonical))
    assert view.canonical == canonical
    assert view.legacy == projected


# --- execution identities and safety ----------------------------------------


def _two_source_world(
    *,
    support_tier: ProductionReferenceTier = ProductionReferenceTier.SUPPORTING,
    support_external: bool = True,
    support_text: str = SUPPORT_TEXT,
) -> tuple[_World, ProductionInputSnapshot, ProductionReferenceCorpusV1]:
    world = _World()
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    core_document, core_sha = _register_source(
        world, subject_id=subject_id, url=CORE_URL, text=CORE_TEXT
    )
    support_document, support_sha = _register_source(
        world,
        subject_id=subject_id,
        url=SUPPORT_URL,
        text=support_text,
        external=support_external,
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_document,
                sha256=core_sha,
            ),
            _reference(
                url=SUPPORT_URL,
                tier=support_tier,
                document_id=support_document,
                sha256=support_sha,
            ),
        ),
    )
    _publish(world, corpus)
    return world, snapshot, corpus


async def test_model_run_identity_is_stable_within_a_generation_only() -> None:
    world, snapshot, _ = _core_world()
    world.extractions = _CheckpointRepository()
    run = ProductionRun(subject_id=snapshot.subject_id, edition_id=uuid4())

    async def run_ids(target: ProductionRun) -> list[UUID | None]:
        world.extractions.rows.clear()
        gateway = _StructuredGateway({"ExampleRAT": _full_output()})
        service = ProductionExtractionService(
            uow_factory=world.uow,
            model_gateway=gateway,  # type: ignore[arg-type]
            artifact_store=world.blobs,
        )
        await service.execute(run=target, snapshot=snapshot)
        assert all(request.allow_failed_resubmit for request in gateway.calls)
        return [request.run_id for request in gateway.calls]

    first = await run_ids(run)
    assert first == await run_ids(run)
    assert all(run_id is not None for run_id in first)
    run.pipeline_generation += 1
    assert set(first).isdisjoint(await run_ids(run))


async def test_a_batch_never_mixes_diffusion_policies() -> None:
    world, snapshot, corpus = _two_source_world(
        support_tier=ProductionReferenceTier.SUPPORTING, support_external=False
    )
    tech_document, tech_sha = _register_source(
        world, subject_id=snapshot.subject_id, url=TECH_URL, text=TECH_TEXT
    )
    corpus = _corpus(
        subject_id=snapshot.subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            *corpus.sources,
            _reference(
                url=TECH_URL,
                tier=ProductionReferenceTier.TECHNICAL,
                document_id=tech_document,
                sha256=tech_sha,
            ),
        ),
    )
    _publish(world, corpus)
    gateway = _StructuredGateway(
        {
            "ExampleRAT": _full_output(),
            "loader.security-lab.io": _support_output(),
            "d41d8cd98f00b204e9800998ecf8427e": _tech_output(),
        }
    )

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    # The restricted SUPPORTING capture is never batched with the TECHNICAL
    # one that may leave: each is sent alone, under its own policy.
    assert gateway.extract_calls == 0
    assert len(gateway.draft_calls) == len(gateway.calls)
    restricted = [call for call in gateway.calls if "loader.security-lab.io" in call.text]
    assert [call.external_llm_allowed for call in restricted] == [False]
    assert execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert execution.extraction is not None
    assert [omission.canonical_url for omission in execution.extraction.omitted_sources] == [
        SUPPORT_URL
    ]


async def test_evidence_rejections_are_reported_for_the_repair_desk() -> None:
    world, snapshot, corpus = _core_world()
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.succeeded
    rejected = [
        (item.rejection.proposal_kind, item.rejection.value) for item in execution.rejections
    ]
    # Only repair-addressable kinds (artifacts, rules) reach the Repair Desk.
    assert rejected == [("artifact", "ghost.security-lab.io")]
    assert len(execution.rejections) == 1
    assert execution.rejections[0].rejection.reason_code == "source_evidence_missing"
    assert execution.rejections[0].source.canonical_url == CORE_URL
    assert execution.rejections[0].model_run_id is not None


async def test_a_transient_failure_defers_only_its_own_source() -> None:
    world, snapshot, corpus = _two_source_world(support_tier=ProductionReferenceTier.CORE)
    gateway = _StructuredGateway(
        {"ExampleRAT": _full_output(), "loader.security-lab.io": _support_output()}
    )
    gateway.transient_markers = {"ExampleRAT"}

    with pytest.raises(ModelGatewayError):
        await _execute(
            world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
        )

    # The other CORE capture still reached its durable checkpoint.
    assert [row.canonical_url for row in world.extractions.rows.values()] == [SUPPORT_URL]


async def test_a_full_extraction_carries_facts_events_and_union_provenance() -> None:
    world, snapshot, corpus = _two_source_world(
        support_tier=ProductionReferenceTier.CORE,
        support_text=CORE_TEXT.replace("evil.security-lab.io", "other.security-lab.io"),
    )
    gateway = _StructuredGateway({"ExampleRAT": _full_output()})

    execution = await _execute(
        world, gateway, corpus=corpus, subject_id=snapshot.subject_id, snapshot=snapshot
    )

    assert execution.extraction is not None
    sources = {source.canonical_url: source for source in execution.extraction.sources}
    both = tuple(sorted((source.source_document_id for source in sources.values()), key=str))
    core = sources[CORE_URL]
    assert [event.event_date for event in core.events] == [date(2026, 7, 10)]
    # ExampleRAT is published by both documents; the domain only by one.
    malware = next(fact for fact in core.facts if fact.value == "ExampleRAT")
    assert malware.source_document_ids == both
    assert [
        indicator.source_document_ids
        for indicator in core.indicators
        if indicator.value == "evil.security-lab.io"
    ] == [(core.source_document_id,)]
