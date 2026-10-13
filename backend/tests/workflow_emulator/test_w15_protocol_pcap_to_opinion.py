"""FULL PostgreSQL proof of the passive PCAP-to-protocol-opinion workflow."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import struct
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OpinionRevisionEdits,
)
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_rendering import EditionRenderService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.investigation_composition import (
    InvestigationCompositionFactory,
    OptionalInvestigationToolPorts,
)
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.network_protocol_model import NetworkProtocolWorkbench
from cti_app.application.network_workbench import (
    NetworkWorkbench,
    ProductionNetworkCaptureBlobStore,
    QuarantinedCapture,
    QuarantinedCaptureTooLargeError,
)
from cti_app.application.pdf_inspection import PdfInspectionService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.protocol_diagram_projection import (
    compile_protocol_render_result,
    opinion_protocol_figures,
)
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.analyst_opinion import (
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.investigation import (
    FindingAssessment,
    FindingKind,
)
from cti_app.domain.network_protocol import (
    EvidenceStatus,
    NetworkCaptureRefV1,
    NetworkExtractionLimitsV1,
    StreamDirection,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.protocol_model import (
    HypothesisFieldV1,
    HypothesisMessageV1,
    HypothesisTransitionV1,
    ProtocolDiagramKind,
    ProtocolHypothesisProposalV1,
    ProtocolModelV1,
    ProtocolTestKind,
    ProtocolTestPlanStepV1,
    ProtocolValidationStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.database.repositories.pdf_inspection import (
    SqlAlchemyPdfInspectionSourceRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.local_command_runner import LocalCommandRunner
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache, PopplerSubprocess
from cti_app.infrastructure.tshark_adapter import TsharkAdapter
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs
from tests.workflow_emulator import invariants
from tests.workflow_emulator.driver import WorkflowDriver
from tests.workflow_emulator.investigation_support import (
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_envelope_http,
    approve_opinion_http,
    article_document_v5,
    build_investigation_app,
    compose_opinion_http,
    composition_factory,
    investigation_settings,
    publish_opinion_http,
    read_composition,
    read_manifest,
    run_http_job,
    seed_investigation_for_existing_article,
    start_investigation_http,
)

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


class _CaptureQuarantine:
    def __init__(self, captures: Mapping[str, bytes]) -> None:
        self._captures = dict(captures)

    async def read_quarantined_capture(
        self, source_reference: str, *, max_bytes: int
    ) -> QuarantinedCapture:
        content = self._captures[source_reference]
        if len(content) > max_bytes:
            raise QuarantinedCaptureTooLargeError(source_reference)
        return QuarantinedCapture(source_reference, content, hashlib.sha256(content).hexdigest())


class _TestConfinedLocalCommandRunner(LocalCommandRunner):
    """Allow only the known local tools to inspect these inert in-memory fixtures."""

    @property
    def filesystem_confined(self) -> bool:
        return True


def _tcp_frame(
    *,
    source_port: int,
    sequence: int,
    payload: bytes = b"",
    flags: int = 0x10,
    source: str = "10.0.0.2",
    destination: str = "203.0.113.10",
    destination_port: int = 443,
) -> bytes:
    tcp_header = struct.pack(
        "!HHIIHHHH",
        source_port,
        destination_port,
        sequence,
        0,
        (5 << 12) | flags,
        65_535,
        0,
        0,
    )
    ip_length = 20 + len(tcp_header) + len(payload)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        ip_length,
        1,
        0,
        64,
        6,
        0,
        ipaddress.IPv4Address(source).packed,
        ipaddress.IPv4Address(destination).packed,
    )
    return bytes.fromhex("00112233445566778899aabb0800") + ip_header + tcp_header + payload


def _pcap(frames: Sequence[bytes]) -> bytes:
    data = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1))
    for index, frame in enumerate(frames):
        seconds, microseconds = divmod(index * 100_000, 1_000_000)
        data.extend(struct.pack("<IIII", 1 + seconds, microseconds, len(frame), len(frame)))
        data.extend(frame)
    return bytes(data)


def _protocol_messages(first_opcode: int, second_opcode: int, suffix: bytes) -> bytes:
    if len(suffix) != 4:
        raise ValueError("the synthetic messages use two two-byte bodies")
    first = b"\x00\x05" + bytes((first_opcode,)) + suffix[:2]
    second = b"\x00\x05" + bytes((second_opcode,)) + suffix[2:]
    return first + second


def _synthetic_captures() -> tuple[bytes, bytes]:
    training_a = _protocol_messages(0x01, 0x02, b"ABCD")
    training_b = _protocol_messages(0x01, 0x02, b"EFGH")
    holdout = _protocol_messages(0x7F, 0x02, b"IJKL")
    encrypted_tls_record = b"\x17\x03\x03\x00\x04\xde\xad\xbe\xef"
    first_capture = _pcap(
        (
            _tcp_frame(source_port=41001, sequence=100, flags=0x02),
            _tcp_frame(source_port=41001, sequence=101, payload=training_a[:4], flags=0x18),
            _tcp_frame(source_port=41001, sequence=105, payload=training_a[4:], flags=0x18),
            _tcp_frame(source_port=41004, sequence=400, flags=0x02),
            _tcp_frame(source_port=41004, sequence=401, payload=encrypted_tls_record, flags=0x18),
        )
    )
    second_capture = _pcap(
        (
            _tcp_frame(source_port=41002, sequence=200, flags=0x02),
            _tcp_frame(source_port=41002, sequence=201, payload=training_b, flags=0x18),
            _tcp_frame(source_port=41003, sequence=300, flags=0x02),
            _tcp_frame(source_port=41003, sequence=301, payload=holdout, flags=0x18),
        )
    )
    return first_capture, second_capture


def _flow_id(client_port: int) -> str:
    return hashlib.sha256(f"tcp|10.0.0.2:{client_port}|203.0.113.10:443".encode()).hexdigest()[:24]


def _capture_dict(reference: NetworkCaptureRefV1) -> dict[str, object]:
    return reference.to_dict()


def _trace_dict(
    reference: NetworkCaptureRefV1, flow_id: str, *, holdout: bool
) -> dict[str, object]:
    return {
        "capture_ref": _capture_dict(reference),
        "flow_id": flow_id,
        "sample_sha256": None,
        "report_id": None,
        "holdout": holdout,
    }


def _tool_call(name: str, arguments: Mapping[str, Any], purpose: str) -> dict[str, object]:
    return {
        "tool_name": name,
        "schema_version": "1",
        "arguments": dict(arguments),
        "purpose": purpose,
    }


def _scripted_decision(
    reasoning: str,
    *,
    calls: Sequence[Mapping[str, object]] = (),
    finding_proposals: Sequence[Mapping[str, object]] = (),
    next_action: str = "continue",
) -> dict[str, object]:
    return {
        "reasoning_summary": reasoning,
        "goal": "Establish only the protocol facts supported by the two quarantined captures.",
        "proposed_tool_calls": [dict(item) for item in calls],
        "finding_proposals": [dict(item) for item in finding_proposals],
        "next_action": next_action,
    }


def _initial_hypothesis() -> dict[str, object]:
    proposal = ProtocolHypothesisProposalV1(
        schema_version="ProtocolHypothesisProposalV1",
        messages=(
            HypothesisMessageV1(
                "client_message_1",
                "client_message_1",
                1,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
            HypothesisMessageV1(
                "client_message_2",
                "client_message_2",
                2,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
        ),
        fields=(
            HypothesisFieldV1(
                "opcode_1", "client_message_1", "opcode", 2, 1, None, "hex", None, "01"
            ),
            HypothesisFieldV1(
                "opcode_2", "client_message_2", "opcode", 2, 1, None, "hex", None, "02"
            ),
        ),
        transitions=(
            HypothesisTransitionV1(
                "transition_connect",
                "start",
                "connected",
                "client_message_1",
                "message_received",
            ),
            HypothesisTransitionV1(
                "transition_beacon",
                "connected",
                "active",
                "client_message_2",
                "message_received",
            ),
        ),
        test_plan=(
            ProtocolTestPlanStepV1(
                ProtocolTestKind.FIELD_VALUE, "client_message_1", field_id="opcode_1"
            ),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.FIELD_VALUE, "client_message_2", field_id="opcode_2"
            ),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.STATE_TRANSITION,
                "client_message_1",
                transition_id="transition_connect",
            ),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.STATE_TRANSITION,
                "client_message_2",
                transition_id="transition_beacon",
            ),
        ),
    )
    return proposal.to_dict()


class _W15ProtocolBridge(ScriptedInvestigationTransport):
    def __init__(
        self,
        *,
        uow_factory: Any,
        investigation_id: UUID,
        material: SyntheticSampleMaterial,
        artifact_store: ProductionArtifactStore,
        references: tuple[NetworkCaptureRefV1, NetworkCaptureRefV1],
    ) -> None:
        self._references = references
        self._artifact_store = artifact_store
        self.received_prompts: list[str] = []
        self.observations_before_turn: list[tuple[UUID, ...]] = []
        super().__init__(
            (),
            uow_factory=uow_factory,
            investigation_id=investigation_id,
            material=material,
            inject_analysis_at_turn=None,
            final_response_builder=self._final_response,
        )

    async def _latest_model_blob_id(self, source: str) -> str:
        async with self._uow_factory() as uow:
            observations = await uow.evidence_observations.list_for_investigation(
                self._investigation_id
            )
        matches = sorted(
            (item for item in observations if item.source == source),
            key=lambda item: (item.observed_at, str(item.id)),
        )
        if not matches:
            raise AssertionError(f"No persisted protocol model observation from {source}")
        payload = await self._artifact_store.read_json(matches[-1].blob_id)
        model_blob_id = payload.get("model_blob_id")
        if not isinstance(model_blob_id, str):
            raise AssertionError(f"{source} evidence omitted its immutable model blob id")
        return model_blob_id

    async def _latest_verification_finding(self) -> dict[str, object]:
        async with self._uow_factory() as uow:
            observations = await uow.evidence_observations.list_for_investigation(
                self._investigation_id
            )
        matches = [item for item in observations if item.source == "network.protocol.verify"]
        if not matches:
            raise AssertionError("The scripted Bridge cannot revise before a holdout result")
        finding = sorted(matches, key=lambda item: (item.observed_at, str(item.id)))[-1]
        return {
            "kind": FindingKind.CLAIM_VERIFICATION.value,
            "assessment": FindingAssessment.QUALIFIED.value,
            "statement": (
                "Le flux holdout contredit la valeur opcode candidate 01; cette valeur reste "
                "une hypothèse limitée aux observations d'entraînement."
            ),
            "subject_ref": None,
            "observation_refs": [str(finding.id)],
            "claim_refs": [],
            "hypothesis_refs": [],
            "novelty": None,
            "scope": "Comparaison locale entre deux flux d'entraînement et un flux holdout.",
            "limitations": (
                "La sémantique de l'octet opcode n'est pas décodée.",
                "Le résultat ne prouve aucune exécution de commande.",
            ),
        }

    async def _final_response(self, observations: tuple[Any, ...]) -> Mapping[str, Any]:
        by_source: dict[str, list[Any]] = {}
        for item in observations:
            by_source.setdefault(item.source, []).append(item)
        inspection_refs = [item.id for item in by_source.get("network.capture.inspect", ())]
        segment_refs = [item.id for item in by_source.get("network.messages.segment", ())]
        verification_refs = [item.id for item in by_source.get("network.protocol.verify", ())]
        if len(inspection_refs) != 2 or len(segment_refs) < 4 or not verification_refs:
            raise AssertionError(
                "W15 final opinion lacks capture, segmentation, or holdout evidence"
            )
        network_refs = [str(item) for item in (*inspection_refs, *segment_refs)]
        protocol_refs = [str(item) for item in (*segment_refs, *verification_refs[-1:])]
        return _scripted_decision(
            "Retain the measured message framing and state transitions; keep the opcode result "
            "qualified because the third flow contradicted the training hypothesis.",
            finding_proposals=(
                {
                    "kind": FindingKind.NETWORK_BEHAVIOUR.value,
                    "assessment": FindingAssessment.SUPPORTED.value,
                    "statement": (
                        "TShark and native reassembly observed ordered TCP conversations; two "
                        "training flows support five-byte length-prefixed boundaries, while the "
                        "TLS application-data flow remains opaque."
                    ),
                    "subject_ref": None,
                    "observation_refs": network_refs,
                    "claim_refs": [],
                    "hypothesis_refs": [],
                    "novelty": None,
                    "scope": "Deux PCAP synthétiques quarantainés; aucune capture live.",
                    "limitations": (
                        "Les frontières propriétaires restent des candidates inférées.",
                        "Le flux TLS reste opaque et n'a pas été décodé.",
                    ),
                },
                {
                    "kind": FindingKind.CAPABILITY.value,
                    "assessment": FindingAssessment.SUPPORTED.value,
                    "statement": (
                        "Les deux traces d'entraînement mesurent les transitions proposées entre "
                        "message 1 et message 2; leur sémantique reste inconnue."
                    ),
                    "subject_ref": None,
                    "observation_refs": protocol_refs,
                    "claim_refs": [],
                    "hypothesis_refs": [],
                    "novelty": None,
                    "scope": "Modèle de protocole local versionné, vérifié sur un holdout.",
                    "limitations": (
                        "La contradiction demeure ouverte.",
                        "Aucune capacité de commande ni de chiffrement n'est confirmée.",
                    ),
                },
            ),
            next_action="finish",
        )

    async def submit(self, **kwargs: Any):
        turn_index = int(kwargs["turn_index"])
        prompt = str(kwargs["prompt"])
        self.received_prompts.append(prompt)
        async with self._uow_factory() as uow:
            before = tuple(
                await uow.evidence_observations.list_for_investigation(self._investigation_id)
            )
        self.observations_before_turn.append(tuple(item.id for item in before))
        ref_a, ref_b = self._references
        traces = (
            _trace_dict(ref_a, _flow_id(41001), holdout=False),
            _trace_dict(ref_b, _flow_id(41002), holdout=False),
        )
        holdout_trace = _trace_dict(ref_b, _flow_id(41003), holdout=True)
        subject_id = str(self._investigation_id)
        all_flows = (
            (ref_a, 41001),
            (ref_a, 41004),
            (ref_b, 41002),
            (ref_b, 41003),
        )

        if turn_index == 0:
            response = _scripted_decision(
                "Inspect both quarantined captures with TShark before selecting streams.",
                calls=(
                    _tool_call(
                        "network.capture.inspect",
                        {"capture_ref": _capture_dict(ref_a)},
                        "Read metadata and observed conversations from the first synthetic PCAP.",
                    ),
                    _tool_call(
                        "network.capture.inspect",
                        {"capture_ref": _capture_dict(ref_b)},
                        "Read metadata and observed conversations from the second synthetic PCAP.",
                    ),
                ),
            )
        elif turn_index in {1, 2}:
            tool_name = (
                "network.stream.reassemble" if turn_index == 1 else "network.messages.segment"
            )
            operation = (
                "Reassemble observed bytes and directions."
                if turn_index == 1
                else ("Measure candidate message boundaries and fields.")
            )
            response = _scripted_decision(
                f"{operation} Include the third flow and the encrypted opaque flow.",
                calls=tuple(
                    _tool_call(
                        tool_name,
                        {"capture_ref": _capture_dict(reference), "flow_id": _flow_id(port)},
                        f"{operation} Flow from source port {port}.",
                    )
                    for reference, port in all_flows
                ),
            )
        elif turn_index == 3:
            response = _scripted_decision(
                "Compare the two consistent training flows and preserve the separate holdout.",
                calls=(
                    _tool_call(
                        "network.protocol.compare",
                        {"subject_id": subject_id, "traces": list(traces)},
                        "Compare framing and fields across the two training traces.",
                    ),
                ),
            )
        elif turn_index == 4:
            response = _scripted_decision(
                "Replay a length, opcode, and state-transition hypothesis on training data.",
                calls=(
                    _tool_call(
                        "network.protocol.hypothesize",
                        {
                            "subject_id": subject_id,
                            "proposal": _initial_hypothesis(),
                            "traces": list(traces),
                        },
                        "Test candidate opcodes and observed transitions on training traces.",
                    ),
                ),
            )
        elif turn_index == 5:
            model_blob_id = await self._latest_model_blob_id("network.protocol.hypothesize")
            response = _scripted_decision(
                "Hold out the third flow to check whether the first opcode hypothesis generalizes.",
                calls=(
                    _tool_call(
                        "network.protocol.verify",
                        {"model_blob_id": model_blob_id, "traces": [holdout_trace]},
                        "Verify the stored model against the contradictory third flow.",
                    ),
                ),
            )
        elif turn_index == 6:
            model_blob_id = await self._latest_model_blob_id("network.protocol.verify")
            response = _scripted_decision(
                "Revise the assessment: opcode 01 is contradicted by the holdout and remains "
                "unconfirmed. Recheck the model so validation retains that contradiction.",
                calls=(
                    _tool_call(
                        "network.protocol.verify",
                        {"model_blob_id": model_blob_id, "traces": [holdout_trace]},
                        "Repeat holdout validation after revising the Bridge assessment.",
                    ),
                ),
                finding_proposals=(await self._latest_verification_finding(),),
            )
        elif turn_index == 7:
            model_blob_id = await self._latest_model_blob_id("network.protocol.verify")
            response = _scripted_decision(
                "Render the revalidated model, retaining observed, inferred, unresolved, and "
                "contradicted statuses in the three semantic views.",
                calls=(
                    _tool_call(
                        "network.protocol.render",
                        {"model_blob_id": model_blob_id},
                        "Generate sequence, state-machine, and message-format specifications.",
                    ),
                ),
            )
        elif turn_index == 8:
            response = await self._final_response(tuple(before))
        else:
            raise AssertionError(f"Unexpected W15 Bridge turn index {turn_index}")

        self.enqueue(response)
        return await super().submit(**kwargs)


def _extract_pdf_text(pdf_path: Path) -> str:
    result = subprocess.run(
        ("pdftotext", str(pdf_path), "-"),
        capture_output=True,
        check=True,
        timeout=30,
        env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    return " ".join(result.stdout.decode("utf-8", errors="replace").split())


def _verify_pdf(
    pdf_path: Path, *, captions: Sequence[str], require_status_legend: bool = True
) -> str:
    info = subprocess.run(
        ("pdfinfo", str(pdf_path)), capture_output=True, check=True, timeout=30
    ).stdout.decode("utf-8", errors="replace")
    pages_line = next((line for line in info.splitlines() if line.startswith("Pages:")), "Pages: 0")
    assert int(pages_line.split(":", 1)[1].strip()) > 0, info
    text = _extract_pdf_text(pdf_path)
    normalized = text.casefold()
    for caption in captions:
        assert caption.casefold() in normalized, (caption, text[-2_000:])
    if require_status_legend:
        for status in ("observed", "inferred", "unresolved", "contradicted"):
            assert status in normalized, (status, text[-2_000:])
    raster_prefix = pdf_path.with_name(f"{pdf_path.stem}-page")
    subprocess.run(
        (
            "pdftoppm",
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            "-png",
            str(pdf_path),
            str(raster_prefix),
        ),
        capture_output=True,
        check=True,
        timeout=45,
    )
    raster_path = raster_prefix.with_suffix(".png")
    assert raster_path.is_file() and raster_path.stat().st_size > 1_024
    return text


@pytest.mark.asyncio
async def test_w15_protocol_pcap_to_opinion_with_real_d2_typst_and_multibatch_pdf(
    workflow_driver: WorkflowDriver,
    uow_factory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("W15-protocol-pcap-to-opinion")
    async with driver:
        typst_binary = os.environ.get("TYPST_BINARY", "typst")
        typst_path = shutil.which(typst_binary)
        d2_binary = os.environ.get("D2_BINARY", "d2")
        d2_path = shutil.which(d2_binary)
        template_root, font_root, fonts_lock = typst_bundle_paths()
        invariants.require(
            typst_path is not None
            and d2_path is not None
            and shutil.which("tshark") is not None
            and shutil.which("capinfos") is not None
            and shutil.which("pdfinfo") is not None
            and shutil.which("pdftotext") is not None
            and shutil.which("pdftoppm") is not None,
            driver.scenario,
            "protocol_render_prerequisites",
            "TShark, capinfos, D2, Typst, and Poppler are required for W15.",
        )
        invariants.require(
            template_root.is_dir() and font_root.is_dir() and fonts_lock.is_file(),
            driver.scenario,
            "protocol_render_prerequisites",
            "Versioned Typst templates, fonts, and font lock are required for W15.",
        )

        edition, artifact_store, subjects, batches = await driver._perform(
            "seed_three_article_multibatch_edition",
            lambda: _compose_three_runs(
                uow_factory,
                tmp_path,
                production_order=("A", "B", "C"),
                composition_order=(),
            ),
            via="fixture",
            action_name="seed three independent synthetic article batches for multibatch rendering",
            assertions=("three-distinct-subjects", "three-distinct-production-batches"),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)

        runs: dict[str, Any] = {}
        base_publications: dict[str, ProductionArtifact] = {}
        base_documents: dict[str, Any] = {}
        for label in ("A", "B", "C"):
            async with uow_factory() as uow:
                subject_runs = await uow.production_runs.list_for_subject(subjects[label].id)
            assert len(subject_runs) == 1
            run = subject_runs[0]
            document = article_document_v5(subjects[label].id, label, version="W15")
            blob_id, document_sha = await artifact_store.put_canonical_json(
                serialize_publication_document(document), bucket="w15-v5-publications"
            )
            publication = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subjects[label].id,
                stage=ProductionArtifactStage.PUBLICATION,
                version=2,
                input_hash=document_sha,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=blob_id,
            )
            async with uow_factory() as uow:
                await uow.production_artifacts.append(publication)
                await uow.commit()
            runs[label] = run
            base_publications[label] = publication
            base_documents[label] = document

        invariants.require(
            len({runs[label].id for label in ("A", "B", "C")}) == 3
            and len({batches[label] for label in ("A", "B", "C")}) == 3,
            driver.scenario,
            "seed_three_article_multibatch_edition",
            "each article must remain in its own production run and batch",
        )
        await driver.compose(
            tuple(subjects[label].id for label in ("A", "B", "C")),
            provenance_batch_ids={subjects[label].id: batches[label] for label in ("A", "B", "C")},
        )
        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted_v1 = await driver.accept()
        release_v1 = await driver.assemble()
        composition_v1 = await read_composition(uow_factory, edition.id)
        manifest_v1 = await read_manifest(uow_factory, edition.id)
        assert composition_v1 is not None and manifest_v1 is not None

        material = SyntheticSampleMaterial.unique()
        seed = await seed_investigation_for_existing_article(
            uow_factory,
            artifact_store,
            edition=edition,
            subject=subjects["A"],
            run=runs["A"],
            publication=base_publications["A"],
            publication_bytes=await artifact_store.read_bytes(
                cast(UUID, base_publications["A"].canonical_blob_id)
            ),
            material=material,
            article_claim=(
                "C1: les traces réseau synthétiques décrivent un échange length-préfixé; "
                "aucune sémantique opcode n'est établie."
            ),
        )
        base_publication_bytes = await artifact_store.read_bytes(
            cast(UUID, base_publications["A"].canonical_blob_id)
        )
        first_capture_bytes, second_capture_bytes = _synthetic_captures()
        quarantine = _CaptureQuarantine(
            {
                "w15:training-and-encrypted": first_capture_bytes,
                "w15:training-and-holdout": second_capture_bytes,
            }
        )
        network_limits = NetworkExtractionLimitsV1()
        tshark = TsharkAdapter(
            runner=_TestConfinedLocalCommandRunner(
                max_sample_bytes=network_limits.max_capture_bytes
            ),
            limits=network_limits,
        )
        invariants.require(
            tshark.available,
            driver.scenario,
            "tshark_capability",
            "The installed TShark and capinfos binaries must be visible to the bounded adapter.",
        )
        async with uow_factory() as uow:
            recorder = ProductionEvidenceRecorder(artifact_store, uow.evidence_observations)
            capture_workbench = NetworkWorkbench(
                quarantine=quarantine,
                blobs=ProductionNetworkCaptureBlobStore(artifact_store),
                tshark=tshark,
                recorder=recorder,
                limits=network_limits,
            )
            reference_a = await capture_workbench.accept_quarantined_capture(
                "w15:training-and-encrypted",
                investigation_id=seed.investigation.id,
                idempotency_key=f"w15-capture-a-{seed.investigation.id}",
            )
            reference_b = await capture_workbench.accept_quarantined_capture(
                "w15:training-and-holdout",
                investigation_id=seed.investigation.id,
                idempotency_key=f"w15-capture-b-{seed.investigation.id}",
            )
            await uow.commit()

        transport = _W15ProtocolBridge(
            uow_factory=uow_factory,
            investigation_id=seed.investigation.id,
            material=material,
            artifact_store=artifact_store,
            references=(reference_a, reference_b),
        )
        settings = investigation_settings(providers=False)
        base_factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            settings=settings,
            transport=transport,
        )
        ports = replace(
            base_factory.optional_tool_ports or OptionalInvestigationToolPorts(),
            network_quarantine=quarantine,
            tshark=tshark,
        )
        investigation_factory = replace(base_factory, optional_tool_ports=ports)
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            composition_factory=cast(InvestigationCompositionFactory, investigation_factory),
        )
        engine = create_postgres_engine(migrated_postgres_url)
        output_root = Path("/tmp") / f"autowork-w15-protocol-{seed.investigation.id.hex}"
        output_root.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                started = await start_investigation_http(client, seed)
                investigation_id = seed.investigation.id
                envelope = await approve_envelope_http(
                    client,
                    investigation_id,
                    providers=(),
                    budget_limits={"bridge_turns": 12, "tool_seconds": 1_200},
                    allow_acquisition=False,
                )
                assert envelope.status_code == 200, envelope.text
                assert envelope.json()["approved"] is True
                await run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))

                assert len(transport.prompts) == 9, len(transport.prompts)
                assert transport.received_prompts == transport.prompts
                for index in range(len(transport.prompts) - 1):
                    before_ids = set(transport.observations_before_turn[index])
                    following_ids = set(transport.observations_before_turn[index + 1])
                    new_observation_ids = following_ids - before_ids
                    assert new_observation_ids, f"turn {index} produced no persisted observations"
                    assert all(
                        str(observation_id) in transport.prompts[index + 1]
                        for observation_id in new_observation_ids
                    ), (
                        f"turn {index + 1} did not receive every new observation: "
                        f"{sorted(map(str, new_observation_ids))}"
                    )
                assert "contradicted" in transport.prompts[6].casefold()
                assert "third flow" in transport.prompts[6].casefold()

                async with uow_factory() as uow:
                    observations = tuple(
                        await uow.evidence_observations.list_for_investigation(investigation_id)
                    )
                    findings = tuple(
                        await uow.investigation_findings.list_for_investigation(investigation_id)
                    )
                by_source: dict[str, list[Any]] = {}
                for observation in observations:
                    by_source.setdefault(observation.source, []).append(observation)
                assert len(by_source["network.capture.inspect"]) == 2
                assert len(by_source["network.stream.reassemble"]) == 4
                assert len(by_source["network.messages.segment"]) == 4
                assert len(by_source["network.protocol.compare"]) == 1
                assert len(by_source["network.protocol.hypothesize"]) == 1
                assert len(by_source["network.protocol.verify"]) == 2
                assert len(by_source["network.protocol.render"]) == 1

                report_payloads = [
                    await artifact_store.read_json(item.blob_id)
                    for item in by_source["network.capture.inspect"]
                ]
                reports = {item["capture"]["sha256"]: item for item in report_payloads}
                report_a = reports[reference_a.sha256]
                report_b = reports[reference_b.sha256]
                assert all(int(item["packet_count"]) >= 4 for item in reports.values())
                assert all(item["conversations"] for item in reports.values())
                first_protocol_names = {
                    str(protocol["protocol"]).casefold() for protocol in report_a["protocols"]
                }
                assert any("tls" in item for item in first_protocol_names), first_protocol_names
                assert all(
                    any(flow["flow_id"] == _flow_id(port) for flow in report["conversations"])
                    for report, port in (
                        (report_a, 41001),
                        (report_b, 41002),
                        (report_b, 41003),
                    )
                )

                reassembly_payloads = [
                    await artifact_store.read_json(item.blob_id)
                    for item in by_source["network.stream.reassemble"]
                ]
                streams_by_flow = {
                    item["flow_id"]: item
                    for payload in reassembly_payloads
                    for item in payload["streams"]
                }
                for flow_id in (_flow_id(41001), _flow_id(41002), _flow_id(41003)):
                    stream = streams_by_flow[flow_id]
                    assert stream["direction"] == "originator_to_responder"
                    assert stream["frame_numbers"] == sorted(stream["frame_numbers"])
                    offsets = [item["start_offset"] for item in stream["segments"]]
                    assert offsets == sorted(offsets)
                assert streams_by_flow[_flow_id(41004)]["encrypted_opaque"] is True

                segmentation_payloads = [
                    await artifact_store.read_json(item.blob_id)
                    for item in by_source["network.messages.segment"]
                ]
                by_flow = {
                    item["flow_id"]: item
                    for payload in segmentation_payloads
                    for item in payload["segmentations"]
                }
                for training_flow in (_flow_id(41001), _flow_id(41002)):
                    segmentation = by_flow[training_flow]
                    assert segmentation["direction"] == "originator_to_responder"
                    assert len(segmentation["boundaries"]) == 2
                    assert [item["message_index"] for item in segmentation["boundaries"]] == [1, 2]
                    assert any(
                        field["name"] == "length_prefix"
                        and field["status"] == EvidenceStatus.INFERRED.value
                        for field in segmentation["fields"]
                    )
                encrypted_segmentation = by_flow[_flow_id(41004)]
                assert encrypted_segmentation["encrypted_opaque"] is True
                assert any(
                    field["name"] == "encrypted_opaque_payload"
                    and field["status"] == EvidenceStatus.UNRESOLVED.value
                    for field in encrypted_segmentation["fields"]
                )
                assert all(
                    "decrypted" not in json.dumps(payload).casefold()
                    for payload in segmentation_payloads
                )

                verification_observations = sorted(
                    by_source["network.protocol.verify"],
                    key=lambda item: (item.observed_at, str(item.id)),
                )
                verification_payload = await artifact_store.read_json(
                    verification_observations[-1].blob_id
                )
                verified_model_id = UUID(verification_payload["model_blob_id"])
                model = ProtocolModelV1.from_dict(await artifact_store.read_json(verified_model_id))
                opcode = next(item for item in model.fields if item.field_id == "opcode_1")
                assert opcode.validation_status is ProtocolValidationStatus.CONTRADICTED
                assert model.validation_status is ProtocolValidationStatus.CONTRADICTED
                assert any(
                    item.item_id == "opcode_1"
                    and item.validation_status is ProtocolValidationStatus.CONTRADICTED
                    and item.evidence
                    and any(
                        evidence.capture_sha256 == reference_b.sha256
                        and evidence.flow_id == _flow_id(41003)
                        and evidence.frame_numbers
                        for evidence in item.evidence
                    )
                    for item in model.verification_findings
                )
                assert len(model.transitions) == 2
                assert all(
                    transition.evidence
                    and all(proof.frame_numbers for proof in transition.evidence)
                    for transition in model.transitions
                )
                assert any(
                    item.kind is FindingKind.CLAIM_VERIFICATION
                    and item.assessment is FindingAssessment.QUALIFIED
                    for item in findings
                )

                render_observation = by_source["network.protocol.render"][0]
                stored_render_result = await artifact_store.read_json(render_observation.blob_id)
                semantic_render_result = NetworkProtocolWorkbench(capture_workbench).render(model)
                assert stored_render_result == semantic_render_result.to_dict()
                d2 = D2DiagramCompiler(binary=d2_path or d2_binary)
                media_store = MediaAssetStore(artifact_store, uow_factory)
                rendered_figures = await compile_protocol_render_result(
                    semantic_render_result,
                    compiler=d2,
                    media_asset_store=media_store,
                    production_run_id=seed.run.id,
                )
                figures = opinion_protocol_figures(rendered_figures)
                assert {item.kind for item in figures} == set(ProtocolDiagramKind)
                for rendered in rendered_figures:
                    preview_path = output_root / f"{rendered.spec.kind.value}.png"
                    preview_path.write_bytes(rendered.preview.media_bytes)
                    assert preview_path.stat().st_size > 1_024

                draft_response = await compose_opinion_http(client, investigation_id)
                assert draft_response.status_code == 200, draft_response.text
                draft_payload = draft_response.json()["draft"]
                opinion_service = AnalystOpinionService(
                    uow_factory=uow_factory,
                    artifact_store=artifact_store,
                )
                current = await opinion_service.get_draft(investigation_id)
                assert current.opinion is not None
                block_types = {item.block_type for item in current.opinion.blocks}
                assert OpinionBlockType.NETWORK_AND_INFRASTRUCTURE in block_types
                assert OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES in block_types
                assert OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS in block_types
                opinion_blocks = tuple(
                    replace(
                        block,
                        qualification=OpinionClaimQualification.INFERENCE,
                        certainty=0.55,
                    )
                    if block.block_type is OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES
                    else block
                    for block in current.opinion.blocks
                )
                revised = await opinion_service.revise(
                    current.opinion.id,
                    current.opinion.version,
                    OpinionRevisionEdits(
                        blocks=opinion_blocks,
                        unresolved_points=(
                            "L'octet opcode reste contredit par le troisième flux.",
                            "La charge TLS est opaque; aucun décodage TLS n'est établi.",
                        ),
                        protocol_figures=figures,
                    ),
                    actor="human:w15-reviewer",
                )
                assert revised.protocol_figures == figures
                assert all(
                    block.qualification is not OpinionClaimQualification.FACT
                    for block in revised.blocks
                    if block.block_type
                    in {
                        OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES,
                        OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
                    }
                )
                assert all(block.finding_refs for block in revised.blocks if block.is_renderable)
                approved = await approve_opinion_http(
                    client,
                    revised.id,
                    version=revised.version,
                    sha256=revised.content_sha256,
                )
                assert approved.status_code == 200, approved.text
                approved_body = approved.json()["approved"]
                assert approved_body["version"] == revised.version + 1
                assert approved_body["content_sha256"] != revised.content_sha256
                published = await publish_opinion_http(
                    client,
                    revised.id,
                    version=approved_body["version"],
                    base_publication_sha256=approved_body["provenance"]["base_publication_sha256"],
                )
                assert published.status_code == 200, published.text
                assert published.json()["composition_repin_required"] is True
                v6_id = UUID(published.json()["publication"]["artifact_id"])
                async with uow_factory() as uow:
                    stored_v6 = await uow.production_artifacts.get(v6_id)
                    v6_blob = (
                        await uow.blobs.get(stored_v6.canonical_blob_id)
                        if stored_v6 is not None and stored_v6.canonical_blob_id is not None
                        else None
                    )
                assert stored_v6 is not None and v6_blob is not None
                assert stored_v6.canonical_blob_id is not None
                v6_document = publication_document_from_json(
                    await artifact_store.read_json(stored_v6.canonical_blob_id)
                )
                assert isinstance(v6_document, PublicationDocumentV6)
                assert v6_document.rendered_opinion is not None
                assert v6_document.rendered_opinion.protocol_figures == figures
                assert (
                    await artifact_store.read_bytes(
                        cast(UUID, base_publications["A"].canonical_blob_id)
                    )
                    == base_publication_bytes
                )

                typst_binary = typst_path or typst_binary
                publication_renderer = PublicationRenderService(
                    uow_factory=uow_factory,
                    artifact_store=artifact_store,
                    media_asset_store=media_store,
                    renderer=TypstRenderer(),
                    compiler=TypstSubprocessCompiler(binary=typst_binary),
                    chp_typst_root=template_root,
                    font_bundle_root=font_root,
                    typst_fonts_lock_path=fonts_lock,
                )
                article_render = await publication_renderer.render_pdf(v6_id)
                assert article_render.output_blob_id is not None
                article_bytes = await artifact_store.read_bytes(article_render.output_blob_id)
                article_sha256 = hashlib.sha256(article_bytes).hexdigest()
                assert article_sha256 == article_render.output_sha256
                article_pdf_path = output_root / "article.pdf"
                article_pdf_path.write_bytes(article_bytes)
                captions = tuple(item.caption for item in figures)
                article_text = _verify_pdf(article_pdf_path, captions=captions)
                assert "charge TLS" in article_text.casefold()
                assert "opcode candidate" in article_text.casefold()

                async def read_release_snapshot(
                    release_id: UUID, manifest_id: UUID, render_id: UUID
                ):
                    async with uow_factory() as uow:
                        release = await uow.edition_releases.get(release_id)
                        manifest = await uow.publication_manifests.get(manifest_id)
                        manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest_id)
                        manifest_blob = (
                            await uow.blobs.get(manifest_blob_id) if manifest_blob_id else None
                        )
                        render = await uow.edition_renders.get(render_id)
                    assert release is not None and manifest is not None
                    assert manifest_blob_id is not None and manifest_blob is not None
                    assert render is not None
                    assert render.output_blob_id is not None
                    document_bytes = await artifact_store.read_bytes(
                        release.edition_document_blob_id
                    )
                    manifest_bytes = await artifact_store.read_bytes(manifest_blob_id)
                    pdf_bytes = await artifact_store.read_bytes(render.output_blob_id)
                    assert hashlib.sha256(document_bytes).hexdigest() == (
                        release.edition_document_sha256
                    )
                    assert hashlib.sha256(manifest_bytes).hexdigest() == (
                        manifest_blob.descriptor.sha256
                    )
                    assert hashlib.sha256(pdf_bytes).hexdigest() == render.output_sha256
                    return release, manifest, document_bytes, manifest_bytes, pdf_bytes

                initial_typst_binary_path = typst_binary
                pdf_inspector = PdfInspectionService(
                    SqlAlchemyPdfInspectionSourceRepository(create_session_factory(engine)),
                    artifact_store,
                    PopplerSubprocess(temp_root=tmp_path / "w15-poppler"),
                    PdfInspectionCache(tmp_path / "w15-pdf-cache"),
                )
                edition_renderer = EditionRenderService(
                    uow_factory=uow_factory,
                    artifact_store=artifact_store,
                    media_asset_store=media_store,
                    renderer=EditionTypstRenderer(),
                    compiler=TypstSubprocessCompiler(binary=initial_typst_binary_path),
                    chp_typst_root=template_root,
                    font_bundle_root=font_root,
                    typst_fonts_lock_path=fonts_lock,
                )

                async def render_edition(release) -> tuple[Any, bytes, str]:
                    rendered = await edition_renderer.render_pdf(release.id)
                    assert rendered.output_blob_id is not None
                    pdf_bytes = await artifact_store.read_bytes(rendered.output_blob_id)
                    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
                    assert sha256 == rendered.output_sha256
                    metadata = await pdf_inspector.metadata(sha256)
                    assert metadata["pdf"]["page_count"] > 0
                    assert metadata["diagnostics"]["status"] == "ready"
                    return rendered, pdf_bytes, sha256

                rendered_v1, edition_v1_bytes, edition_v1_sha = await render_edition(release_v1)
                v1_pdf_path = output_root / "edition-v1.pdf"
                v1_pdf_path.write_bytes(edition_v1_bytes)
                _verify_pdf(v1_pdf_path, captions=(), require_status_legend=False)
                v1_snapshot = await read_release_snapshot(
                    release_v1.id, accepted_v1.manifest.id, rendered_v1.id
                )
                assert v1_snapshot[4] == edition_v1_bytes

                async def human_repin_a():
                    service = EditionCompositionService(uow_factory)
                    current_composition = await service.get(edition.id)
                    assert current_composition.composition is not None
                    entries = tuple(
                        replace(
                            entry,
                            artifact_id=stored_v6.id,
                            artifact_version=stored_v6.version,
                            artifact_input_hash=stored_v6.input_hash,
                            document_sha256=v6_blob.descriptor.sha256,
                        )
                        if entry.subject_id == subjects["A"].id
                        else entry
                        for entry in current_composition.composition.entries
                    )
                    return await service.replace_entries(
                        edition.id,
                        entries,
                        expected_version=current_composition.composition.version,
                        idempotency_key=f"w15-human-repin-{uuid4()}",
                        actor_id="human:w15-editor",
                        correlation_id=f"w15-repin:{uuid4()}",
                    )

                repinned = await human_repin_a()
                assert repinned.version == composition_v1.version + 1
                await driver.review(expect_can_accept=True)
                await driver.validate()
                accepted_v2 = await driver.accept()
                release_v2 = await driver.assemble()
                rendered_v2, edition_v2_bytes, edition_v2_sha = await render_edition(release_v2)
                edition_pdf_path = output_root / "edition.pdf"
                edition_pdf_path.write_bytes(edition_v2_bytes)
                edition_text = _verify_pdf(edition_pdf_path, captions=captions)
                assert "charge TLS" in edition_text.casefold()
                assert "opcode candidate" in edition_text.casefold()

                composition_v2 = await read_composition(uow_factory, edition.id)
                manifest_v2 = await read_manifest(uow_factory, edition.id)
                assert composition_v2 is not None and manifest_v2 is not None
                old_entries = {item.subject_id: item for item in composition_v1.entries}
                new_entries = {item.subject_id: item for item in composition_v2.entries}
                assert new_entries[subjects["A"].id].artifact_id == stored_v6.id
                assert all(
                    new_entries[subjects[label].id] == old_entries[subjects[label].id]
                    for label in ("B", "C")
                )
                assert accepted_v2.manifest.id == manifest_v2.id
                v2_snapshot = await read_release_snapshot(
                    release_v2.id, accepted_v2.manifest.id, rendered_v2.id
                )
                assert v2_snapshot[4] == edition_v2_bytes
                historical = await read_release_snapshot(
                    release_v1.id, accepted_v1.manifest.id, rendered_v1.id
                )
                assert historical == v1_snapshot
                assert hashlib.sha256(v1_snapshot[3]).hexdigest() == (
                    hashlib.sha256(historical[3]).hexdigest()
                )
                assert hashlib.sha256(v1_snapshot[4]).hexdigest() == (
                    hashlib.sha256(historical[4]).hexdigest()
                )
                assert edition_v1_sha == hashlib.sha256(v1_snapshot[4]).hexdigest()
                assert edition_v2_sha == hashlib.sha256(edition_v2_bytes).hexdigest()
                assert (
                    hashlib.sha256(v1_snapshot[3]).hexdigest()
                    != hashlib.sha256(v2_snapshot[3]).hexdigest()
                )
                assert len(figures) == 3
                assert all(
                    any(figure.kind is kind for figure in figures) for kind in ProtocolDiagramKind
                )
                assert all(block.finding_refs for block in revised.blocks if block.is_renderable)
                assert not any(
                    "TLS decoded" in block.text or "TLS déchiffré" in block.text
                    for block in revised.blocks
                )
                assert draft_payload["id"] == str(revised.id)

                invariants.require(
                    len(report_a["conversations"]) >= 2
                    and len(report_b["conversations"]) >= 2
                    and model.validation_status is ProtocolValidationStatus.CONTRADICTED
                    and set(item.kind for item in figures) == set(ProtocolDiagramKind)
                    and v1_snapshot == historical,
                    driver.scenario,
                    "pcap_to_opinion_pdf",
                    "W15 must preserve measured flows, the holdout contradiction, all three D2 "
                    "figures, and the immutable previous edition release.",
                )
        finally:
            await engine.dispose()
