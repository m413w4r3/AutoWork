from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import (
    augment_publication_with_opinion,
)
from cti_app.application.publication_builder import build_publication_document_v5
from cti_app.application.publication_detection import (
    PublicationDetectionDataError,
    _provenance_counts,
    _validation_fields,
    build_publication_detection,
)
from cti_app.application.publication_qa import (
    PublicationQaErrorCode,
    qa_publication_v6,
)
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import TypstRenderer, load_template_bundle
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
    MotifProvenance,
)
from cti_app.domain.publication_document import (
    PublicationDetectionReason,
    PublicationDetectionStatus,
    PublicationDetectionV1,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.domain.retrohunt import RetrohuntReservation, RetrohuntReservationState
from cti_app.domain.rule_workbench import RuleValidationStatus
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_builder_v4 import _canonical_inputs, _enrichment_with
from tests.test_publication_document_v6 import _approved_opinion

pytest_plugins = ("tests.test_typst_compiler_runtime",)


def test_publication_detection_projects_only_calculable_yara_metrics() -> None:
    claimed = {
        "execution_status": "ok",
        "validation_status": "real_validated",
        "validation_reason": "real_positives_and_goodware_tested",
    }
    status, reason = _validation_fields(claimed)
    assert status is RuleValidationStatus.REAL_PARTIAL
    assert reason is PublicationDetectionReason.SAMPLE_BYTES_MISSING
    measured = {
        **claimed,
        "metrics": {
            "real_positive_sensitivity": {
                "status": "calculated",
                "numerator": 2,
                "denominator": 2,
                "confidence_interval_95": {"lower": 0.34, "upper": 1.0},
                "method": "wilson_score_two_sided_95",
            },
            "real_goodware_false_positive_rate": {
                "status": "calculated",
                "numerator": 0,
                "denominator": 12,
                "confidence_interval_95": {"lower": 0.0, "upper": 0.24},
                "method": "wilson_score_two_sided_95",
            },
        },
    }
    status, reason = _validation_fields(measured)
    assert status is RuleValidationStatus.REAL_VALIDATED
    assert reason is PublicationDetectionReason.VALIDATED

    incomplete_signature = {
        **measured,
        "signature_limitations": ["source bytes are not verified"],
    }
    status, reason = _validation_fields(incomplete_signature)
    assert status is RuleValidationStatus.REAL_PARTIAL
    assert reason is PublicationDetectionReason.SAMPLE_BYTES_MISSING


@pytest.mark.parametrize("positive_role", ["human_confirmed", "confirmed_positive"])
def test_publication_detection_reads_canonical_and_historical_positive_count_keys(
    positive_role: str,
) -> None:
    counts = _provenance_counts(
        {
            "provenance_counts_by_role": {
                positive_role: {"real": 2, "synthetic": 1},
                "goodware": {"real": 3, "synthetic": 4},
            }
        }
    )

    assert counts["human_confirmed"] == (2, 1)
    assert counts["goodware"] == (3, 4)


def test_publication_detection_rejects_canonical_and_legacy_positive_count_duplicates() -> None:
    with pytest.raises(PublicationDetectionDataError, match="both canonical and legacy"):
        _provenance_counts(
            {
                "provenance_counts_by_role": {
                    "human_confirmed": {"real": 1, "synthetic": 0},
                    "confirmed_positive": {"real": 1, "synthetic": 0},
                }
            }
        )


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CHP_TYPST_ROOT = _REPOSITORY_ROOT / "chpTypst"
_FONT_BUNDLE_LOCK = _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"


def _rule_source() -> str:
    comments = "\n".join(
        f"// Coverage note {index:02d}: observed marker and documented matching boundary "
        "with a deliberately long but readable source comment."
        for index in range(1, 81)
    )
    long_marker = "LOT4C_" + "x" * 180
    return (
        f"{comments}\n"
        "rule lot4c_detection_candidate {\n"
        "  meta:\n"
        '    description = "Candidate rule included with its measured validation status"\n'
        "  strings:\n"
        f'    $marker = "{long_marker}"\n'
        "  condition:\n"
        "    uint16(0) == 0x5a4d and $marker\n"
        "}"
    )


def _published_rule(
    *,
    canonical_status: RuleValidationStatus,
    status: PublicationDetectionStatus,
    reason: PublicationDetectionReason,
    positive_real_count: int,
    positive_synthetic_count: int,
    goodware_tested_count: int,
    false_positive_count: int,
    tested: bool = True,
    retrohunt_submitted: bool | None = False,
) -> PublicationDetectionV1:
    source = _rule_source()
    return PublicationDetectionV1(
        candidate_id=UUID("00000000-0000-0000-0000-000000000401"),
        version_id=UUID("00000000-0000-0000-0000-000000000402"),
        version=2,
        source=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        candidate_status=DetectionCandidateStatus.TESTED,
        rule_validation_status=canonical_status,
        validation_status=status,
        validation_reason=reason,
        positive_real_count=positive_real_count,
        positive_synthetic_count=positive_synthetic_count,
        goodware_tested_count=goodware_tested_count,
        false_positive_count=false_positive_count,
        engine_version="YARA 4.5.0" if tested else None,
        coverage_statement="Caractéristiques locales mesurées sur le corpus disponible."
        if tested
        else None,
        limitations=("Le corpus ne représente pas toutes les variantes.",) if tested else (),
        created_at=datetime(2026, 10, 17, 10, 0, tzinfo=UTC),
        tested_at=datetime(2026, 10, 17, 10, 5, tzinfo=UTC) if tested else None,
        test_report_observation_id=uuid4() if tested else None,
        test_report_sha256=hashlib.sha256(b"canonical YARA report").hexdigest() if tested else None,
        retrohunt_submitted=retrohunt_submitted,
    )


def _base_inputs():
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    base = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    return snapshot, references, extraction, synthesis, enrichment, base


class _CandidateRepository:
    def __init__(self, versions: Sequence[DetectionCandidateVersion]) -> None:
        self.versions = tuple(versions)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> Sequence[DetectionCandidateVersion]:
        return tuple(item for item in self.versions if item.investigation_id == investigation_id)


class _ObservationRepository:
    def __init__(self, observations: Sequence[EvidenceObservation]) -> None:
        self.observations = tuple(observations)

    async def list_for_investigation(self, investigation_id: UUID) -> Sequence[EvidenceObservation]:
        return tuple(
            item for item in self.observations if item.investigation_id == investigation_id
        )


class _RetrohuntRepository:
    def __init__(self, reservations: Sequence[RetrohuntReservation] = ()) -> None:
        self.reservations = tuple(reservations)

    async def list_for_subject(self, subject_id: UUID) -> Sequence[RetrohuntReservation]:
        return tuple(item for item in self.reservations if item.subject_id == subject_id)


class _FakeUnitOfWork:
    def __init__(
        self,
        versions: Sequence[DetectionCandidateVersion],
        observations: Sequence[EvidenceObservation],
        reservations: Sequence[RetrohuntReservation] = (),
    ) -> None:
        self.detection_candidates = _CandidateRepository(versions)
        self.evidence_observations = _ObservationRepository(observations)
        self.retrohunt_reservations = _RetrohuntRepository(reservations)


class _FakePayloadReader:
    def __init__(self, payloads: Mapping[UUID, Mapping[str, Any]]) -> None:
        self.payloads = payloads

    async def read_json(self, blob_id: UUID) -> Any:
        return self.payloads[blob_id]


@pytest.mark.asyncio
async def test_publication_detection_freezes_canonical_test_report_and_retrohunt_state() -> None:
    _snapshot, _references, _extraction, _synthesis, _enrichment, base = _base_inputs()
    opinion, _citation_context = _approved_opinion(base)
    investigation_id = opinion.investigation_id
    subject_id = base.subject_id
    source = _rule_source()
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    version = DetectionCandidateVersion(
        investigation_id=investigation_id,
        source=source,
        source_sha256=source_sha256,
        motif_provenance=(
            MotifProvenance(
                motif="$marker", observation_refs=(uuid4(),), rationale="p09_invariant"
            ),
        ),
        status=DetectionCandidateStatus.TESTED,
        id=uuid4(),
        created_at=datetime(2026, 10, 16, tzinfo=UTC),
    )
    observation_id = uuid4()
    blob_id = uuid4()
    observed_at = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
    report_sha256 = hashlib.sha256(b"stored report payload").hexdigest()
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.YARA_MATCH,
        blob_id=blob_id,
        sha256=report_sha256,
        source="AutoWork RuleWorkbench",
        source_version="rule-test.v1",
        observed_at=observed_at,
        object_type="detection_candidate_version",
        object_id=str(version.id),
        id=observation_id,
    )
    report = {
        "schema": "investigation-yara-test.v1",
        "operation": "test",
        "source_sha256": source_sha256,
        "validation_status": "synthetic_only",
        "validation_reason": "positive_samples_are_synthetic",
        "provenance_counts_by_role": {
            "human_confirmed": {"real": 0, "synthetic": 2},
            "goodware": {"real": 1, "synthetic": 2},
        },
        "false_positive_count": 0,
        "engine_version": "YARA 4.5.0",
        "coverage_statement": (
            "Contrôle du marqueur sur trois cas bénins et deux positifs synthétiques."
        ),
        "limitations": ["Aucun positif réel testé."],
    }
    reservation = RetrohuntReservation(
        subject_id=subject_id,
        investigation_id=investigation_id,
        slot_number=1,
        operation_key="b" * 64,
        rule_version_id=version.id,
        rule_source_sha256=source_sha256,
        corpus_id="controlled-test-corpus",
        corpus_sha256="c" * 64,
        parameters={},
        state=RetrohuntReservationState.SUBMITTED,
        provider_job_id="vt-job-canonical-1",
    )
    result = await build_publication_detection(
        _FakeUnitOfWork((version,), (observation,), (reservation,)),
        _FakePayloadReader({blob_id: report}),
        investigation_id=investigation_id,
        subject_id=subject_id,
        opinion=opinion,
    )

    assert result is not None
    assert result.version_id == version.id
    assert result.source_sha256 == source_sha256
    assert result.rule_validation_status is RuleValidationStatus.SYNTHETIC_ONLY
    assert result.validation_status is PublicationDetectionStatus.CANDIDATE
    assert result.validation_reason is PublicationDetectionReason.SYNTHETIC_POSITIVES
    assert result.positive_real_count == 0
    assert result.positive_synthetic_count == 2
    assert result.goodware_tested_count == 3
    assert result.false_positive_count == 0
    assert result.engine_version == "YARA 4.5.0"
    assert result.coverage_statement == report["coverage_statement"]
    assert result.limitations == ("Aucun positif réel testé.",)
    assert result.tested_at == observed_at
    assert result.test_report_observation_id == observation_id
    assert result.test_report_sha256 == report_sha256
    assert result.retrohunt_submitted is True
    assert result.validation_status_is_coherent


@pytest.mark.asyncio
async def test_publication_detection_requires_explicit_test() -> None:
    _snapshot, _references, _extraction, _synthesis, _enrichment, base = _base_inputs()
    opinion, _citation_context = _approved_opinion(base)
    source = _rule_source()
    version = DetectionCandidateVersion(
        investigation_id=opinion.investigation_id,
        source=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        motif_provenance=(
            MotifProvenance(
                motif="$marker", observation_refs=(uuid4(),), rationale="p09_invariant"
            ),
        ),
        id=uuid4(),
        created_at=datetime(2026, 10, 16, tzinfo=UTC),
    )
    proposal_observation = EvidenceObservation(
        investigation_id=opinion.investigation_id,
        kind=EvidenceKind.YARA_MATCH,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"compile check is not a test").hexdigest(),
        source="AutoWork RuleWorkbench",
        source_version="rule-test.v1",
        observed_at=datetime(2026, 10, 17, 10, 5, tzinfo=UTC),
        object_type="detection_candidate_version",
        object_id=str(version.id),
    )
    result = await build_publication_detection(
        _FakeUnitOfWork((version,), (proposal_observation,)),
        _FakePayloadReader(
            {
                proposal_observation.blob_id: {
                    "schema": "investigation-yara-test.v1",
                    "operation": "propose",
                    "source_sha256": version.source_sha256,
                    "validation_status": "real_validated",
                    "validation_reason": "real_positives_and_goodware_tested",
                    "false_positive_count": 0,
                }
            }
        ),
        investigation_id=opinion.investigation_id,
        subject_id=base.subject_id,
        opinion=opinion,
    )
    assert result is not None
    assert result.rule_validation_status is RuleValidationStatus.UNTESTED
    assert result.validation_status is PublicationDetectionStatus.UNTESTED
    assert result.validation_reason is PublicationDetectionReason.EXPLICIT_TEST_REQUIRED
    assert result.tested_at is None
    assert result.engine_version is None
    assert result.retrohunt_submitted is False


def test_v6_without_detection_keeps_legacy_payload_shape() -> None:
    _snapshot, _references, _extraction, _synthesis, _enrichment, base = _base_inputs()
    opinion, citation_context = _approved_opinion(base)
    document = augment_publication_with_opinion(base, opinion, citation_context)

    payload = serialize_publication_document(document)
    round_trip = publication_document_from_json(payload)

    assert "detection" not in payload
    assert serialize_publication_document(round_trip) == payload
    canonical_round_trip = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(round_trip)
    )
    assert canonical_round_trip == ProductionArtifactStore.canonical_json_bytes(payload)


def test_publication_qa_rejects_status_that_disagrees_with_canonical_status() -> None:
    snapshot, references, extraction, synthesis, enrichment, base = _base_inputs()
    opinion, citation_context = _approved_opinion(base)
    coherent = _published_rule(
        canonical_status=RuleValidationStatus.REAL_VALIDATED,
        status=PublicationDetectionStatus.VALIDATED,
        reason=PublicationDetectionReason.VALIDATED,
        positive_real_count=2,
        positive_synthetic_count=0,
        goodware_tested_count=3,
        false_positive_count=0,
    )
    inconsistent = replace(
        coherent,
        rule_validation_status=RuleValidationStatus.SYNTHETIC_ONLY,
    )
    document = augment_publication_with_opinion(
        base, opinion, citation_context, detection=inconsistent
    )

    result = qa_publication_v6(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        publication=document,
    )

    assert result["passed"] is False
    assert result["checks"]["detection_validation_status"] is False
    assert PublicationQaErrorCode.DETECTION_STATUS_INCONSISTENT.value in result["error_codes"]


@pytest.mark.asyncio
async def test_real_typst_detection_band_reasons_and_rule_source_are_not_truncated(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    _snapshot, _references, _extraction, _synthesis, _enrichment, base = _base_inputs()
    opinion, citation_context = _approved_opinion(base)
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    pdffonts = shutil.which("pdffonts")
    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    assert pdffonts is not None, "Poppler pdffonts is required for embedded font verification"
    assert pdftotext is not None, "Poppler pdftotext is required for YARA detection verification"
    assert pdftoppm is not None, "Poppler pdftoppm is required for YARA detection verification"

    cases = (
        (
            "validated",
            _published_rule(
                canonical_status=RuleValidationStatus.REAL_VALIDATED,
                status=PublicationDetectionStatus.VALIDATED,
                reason=PublicationDetectionReason.VALIDATED,
                positive_real_count=2,
                positive_synthetic_count=0,
                goodware_tested_count=3,
                false_positive_count=0,
            ),
            "VALIDÉE (positifs réels et corpus bénin testés)",
            None,
        ),
        (
            "synthetic",
            _published_rule(
                canonical_status=RuleValidationStatus.SYNTHETIC_ONLY,
                status=PublicationDetectionStatus.CANDIDATE,
                reason=PublicationDetectionReason.SYNTHETIC_POSITIVES,
                positive_real_count=0,
                positive_synthetic_count=2,
                goodware_tested_count=3,
                false_positive_count=0,
            ),
            "CANDIDATE — NON VALIDÉE",
            "positifs uniquement synthétiques",
        ),
        (
            "untested",
            _published_rule(
                canonical_status=RuleValidationStatus.UNTESTED,
                status=PublicationDetectionStatus.UNTESTED,
                reason=PublicationDetectionReason.EXPLICIT_TEST_REQUIRED,
                positive_real_count=0,
                positive_synthetic_count=0,
                goodware_tested_count=0,
                false_positive_count=0,
                tested=False,
            ),
            "CANDIDATE — NON VALIDÉE",
            "test explicite requis",
        ),
    )

    for name, rule, expected_banner, expected_reason in cases:
        publication = augment_publication_with_opinion(
            base,
            opinion,
            citation_context,
            detection=rule,
        )
        render_source = TypstRenderer().render(
            publication,
            template_bundle,
            document_label=f"Publication enrichie — avis approuvé v1; fixture {name}",
        )
        case_root = tmp_path / name
        workspace = case_root / "workspace"
        workspace.mkdir(parents=True)
        for template_file in template_bundle.files:
            destination = workspace / template_file.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(template_file.content)
        renderer_root = workspace / "RENDERER"
        (renderer_root / "render-data.json").write_bytes(render_source.render_data_bytes)
        media_root = renderer_root / "media"
        media_root.mkdir(parents=True, exist_ok=True)
        for media_ref in render_source.media_refs:
            destination = workspace / "RENDERER" / media_ref.media_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if media_ref.expected_mime_type == "image/svg+xml":
                destination.write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                    '<rect width="16" height="16" fill="#eee"/></svg>',
                    encoding="utf-8",
                )
            elif media_ref.expected_mime_type == "image/png":
                shutil.copyfile(_CHP_TYPST_ROOT / "UTILS" / "chap.png", destination)
            else:
                raise AssertionError(
                    f"Unexpected fixture media type: {media_ref.expected_mime_type}"
                )
        font_root = case_root / "font-snapshot"
        font_root.mkdir()
        compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(
            TypstCompileRequest(
                workspace_root=workspace,
                entrypoint_relative_path="RENDERER/publication.typ",
                font_paths=materialize_font_bundle(font_snapshot, font_root),
            )
        )
        pdf_path = tmp_path / f"publication-detection-{name}.pdf"
        pdf_path.write_bytes(compiled.content)
        reader = PdfReader(BytesIO(compiled.content), strict=True)
        assert reader.pages
        font_result = await asyncio.to_thread(
            subprocess.run,
            (pdffonts, str(pdf_path)),
            capture_output=True,
            check=True,
            timeout=10,
        )
        font_rows = font_result.stdout.decode("utf-8", errors="replace").splitlines()
        assert any(
            re.search(r"Cascadia\s*Mono", row, re.IGNORECASE)
            and re.search(r"\byes\s+yes\s+yes\b", row, re.IGNORECASE)
            for row in font_rows
        ), font_result.stdout.decode("utf-8", errors="replace")
        text_result = await asyncio.to_thread(
            subprocess.run,
            (pdftotext, str(pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=10,
        )
        extracted = text_result.stdout.decode("utf-8", errors="replace")
        normalized = re.sub(r"\s+", " ", extracted).upper()
        assert "DÉTECTION" in normalized
        assert expected_banner.upper() in normalized
        if expected_reason is not None:
            assert expected_reason.upper() in normalized
        assert "AUCUN RETROHUNT SOUMIS" in normalized
        source_normalized = re.sub(r"\s+", "", rule.source)
        page_text_without_footers = "".join(
            "\n".join(
                line
                for line in page.splitlines()
                if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", line) is None
                and line.strip() != "Avis — publication approuvée v2"
            )
            for page in extracted.split("\f")
        )
        assert source_normalized in re.sub(r"\s+", "", page_text_without_footers)
        detection_page = next(
            index + 1
            for index, page in enumerate(reader.pages)
            if "DÉTECTION" in (page.extract_text() or "").upper()
        )
        image_prefix = tmp_path / f"detection-{name}-page"
        await asyncio.to_thread(
            subprocess.run,
            (
                pdftoppm,
                "-f",
                str(detection_page),
                "-l",
                str(detection_page),
                "-singlefile",
                "-png",
                str(pdf_path),
                str(image_prefix),
            ),
            capture_output=True,
            check=True,
            timeout=15,
        )
        assert image_prefix.with_suffix(".png").is_file()

    raw_source = 'rule raw_probe { condition: "' + "x" * 180 + '" }'
    raw_workspace = tmp_path / "raw-code-workspace"
    raw_workspace.mkdir()
    for template_file in template_bundle.files:
        destination = raw_workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    raw_entrypoint = raw_workspace / "RENDERER" / "raw-font-check.typ"
    raw_entrypoint.write_text(
        '#import "../UTILS/document_style.typ": apply-document-style\n'
        "#apply-document-style()[\n"
        "```\n"
        f"{raw_source}\n"
        "```\n"
        "]\n",
        encoding="utf-8",
    )
    raw_font_root = tmp_path / "raw-font-snapshot"
    raw_font_root.mkdir()
    raw_compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(
        TypstCompileRequest(
            workspace_root=raw_workspace,
            entrypoint_relative_path="RENDERER/raw-font-check.typ",
            font_paths=materialize_font_bundle(font_snapshot, raw_font_root),
        )
    )
    raw_pdf = tmp_path / "raw-font-check.pdf"
    raw_pdf.write_bytes(raw_compiled.content)
    raw_text = await asyncio.to_thread(
        subprocess.run,
        (pdftotext, str(raw_pdf), "-"),
        capture_output=True,
        check=True,
        timeout=10,
    )
    raw_extracted = raw_text.stdout.decode("utf-8", errors="replace")
    raw_pages_without_footers = "".join(
        "\n".join(
            line for line in page.splitlines() if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", line) is None
        )
        for page in raw_extracted.split("\f")
    )
    assert re.sub(r"\s+", "", raw_source) in re.sub(r"\s+", "", raw_pages_without_footers)
    raw_font_result = await asyncio.to_thread(
        subprocess.run,
        (pdffonts, str(raw_pdf)),
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert re.search(
        r"Cascadia\s*Mono.*\byes\s+yes\s+yes\b",
        raw_font_result.stdout.decode("utf-8", errors="replace"),
        re.IGNORECASE,
    )
