"""Supervised first real CTI workflow pilot over the production tool assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import httpx
import pytest

from cti_app.api.investigation_captures import router as capture_import_router
from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OpinionRevisionEdits,
)
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_rendering import EditionRenderService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_contracts import FindingProposalV1
from cti_app.application.investigation_service import _FINDING_NAMESPACE
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.network_capture_import import NetworkCaptureImportService
from cti_app.application.network_protocol_model import NetworkProtocolWorkbench
from cti_app.application.network_workbench import (
    NetworkWorkbench,
    ProductionNetworkCaptureBlobStore,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.application.virustotal import VirusTotalCapabilities
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.domain.analyst_opinion import (
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.investigation import (
    AgentNextAction,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.network_protocol import (
    NetworkCaptureFormat,
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
    HypothesisMessageV1,
    HypothesisTransitionV1,
    ProtocolHypothesisProposalV1,
    ProtocolTestKind,
    ProtocolTestPlanStepV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationDocumentV6,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import SemanticRole, SemanticTextSpanV1
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.investigation_runtime_ports import (
    build_investigation_runtime_composition_factory,
    describe_tool_inventory,
)
from cti_app.infrastructure.local_command_runner import LocalCommandRunner
from cti_app.infrastructure.quarantined_capture_source import ProductionQuarantinedCaptureSource
from cti_app.infrastructure.tshark_adapter import TsharkAdapter
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs
from tests.shodan_support import FakeShodanPort
from tests.workflow_emulator import invariants
from tests.workflow_emulator.driver import WorkflowDriver
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal,
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_envelope_http,
    approve_opinion_http,
    article_document_v5,
    build_investigation_app,
    compose_opinion_http,
    decision,
    investigation_settings,
    persist_sample_association_fixture,
    publish_opinion_http,
    qualify_sample_http,
    read_composition,
    read_manifest,
    run_http_job,
    seed_investigation_for_existing_article,
    start_investigation_http,
)
from tests.workflow_emulator.test_w14_agent_opinion import (
    _family_similarity,
    _feature_pack,
    _persist_observation,
)
from tests.workflow_emulator.test_w15_protocol_pcap_to_opinion import (
    _capture_dict,
    _flow_id,
    _pcap,
    _protocol_messages,
    _tcp_frame,
)

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


class _PilotConfinedLocalCommandRunner(LocalCommandRunner):
    """Run the bounded local capture commands over the test's inert bytes."""

    @property
    def filesystem_confined(self) -> bool:
        return True


def _call(name: str, arguments: Mapping[str, object], purpose: str) -> dict[str, object]:
    return {
        "tool_name": name,
        "schema_version": "1",
        "arguments": dict(arguments),
        "purpose": purpose,
    }


def _batch_decision(reasoning: str, calls: Sequence[Mapping[str, object]]) -> dict[str, Any]:
    result = decision(reasoning)
    result["proposed_tool_calls"] = [dict(item) for item in calls]
    return result


def _json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"workflow trace cannot encode {type(value).__name__}")


async def _run_process(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return await asyncio.to_thread(subprocess.run, arguments, **kwargs)


def _with_article_claim(document: PublicationDocumentV5) -> PublicationDocumentV5:
    claim = (
        "L1.1: Cet article affirme que l'échantillon a exécuté le protocole propriétaire "
        "et exfiltré des identifiants."
    )
    paragraph = replace(document.document.lead[0], text=claim)
    base = replace(document.document, lead=(paragraph,))
    semantic = replace(
        document.semantic_text,
        paragraphs=tuple(
            replace(
                item,
                spans=(SemanticTextSpanV1(SemanticRole.TEXT, claim),),
            )
            if item.anchor == "lead:0001"
            else item
            for item in document.semantic_text.paragraphs
        ),
    )
    return replace(document, document=base, semantic_text=semantic)


def _unique_captures(token: bytes) -> tuple[bytes, bytes]:
    if len(token) < 12:
        raise ValueError("capture token must contain at least 12 bytes")
    training_a = _protocol_messages(0x01, 0x02, token[:4])
    training_b = _protocol_messages(0x01, 0x02, token[4:8])
    holdout = _protocol_messages(0x7F, 0x02, token[8:12])
    opaque_tls = b"\x17\x03\x03\x00\x04\xde\xad\xbe\xef"
    return (
        _pcap(
            (
                _tcp_frame(source_port=41001, sequence=100, flags=0x02),
                _tcp_frame(source_port=41001, sequence=101, payload=training_a, flags=0x18),
                _tcp_frame(source_port=41004, sequence=400, flags=0x02),
                _tcp_frame(source_port=41004, sequence=401, payload=opaque_tls, flags=0x18),
            )
        ),
        _pcap(
            (
                _tcp_frame(source_port=41002, sequence=200, flags=0x02),
                _tcp_frame(source_port=41002, sequence=201, payload=training_b, flags=0x18),
                _tcp_frame(source_port=41003, sequence=300, flags=0x02),
                _tcp_frame(source_port=41003, sequence=301, payload=holdout, flags=0x18),
            )
        ),
    )


def _observed_frame_hypothesis() -> dict[str, object]:
    proposal = ProtocolHypothesisProposalV1(
        schema_version="ProtocolHypothesisProposalV1",
        messages=(
            HypothesisMessageV1(
                "observed_message_1",
                "observed_message_1",
                1,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
            HypothesisMessageV1(
                "observed_message_2",
                "observed_message_2",
                2,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
        ),
        fields=(),
        transitions=(
            HypothesisTransitionV1(
                "observed_transition_1",
                "observed_state_0",
                "observed_state_1",
                "observed_message_1",
                "message_observed",
            ),
            HypothesisTransitionV1(
                "observed_transition_2",
                "observed_state_1",
                "observed_state_2",
                "observed_message_2",
                "message_observed",
            ),
        ),
        test_plan=(
            ProtocolTestPlanStepV1(ProtocolTestKind.MESSAGE_LENGTH, "observed_message_1"),
            ProtocolTestPlanStepV1(ProtocolTestKind.MESSAGE_LENGTH, "observed_message_2"),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.STATE_TRANSITION,
                "observed_message_1",
                transition_id="observed_transition_1",
            ),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.STATE_TRANSITION,
                "observed_message_2",
                transition_id="observed_transition_2",
            ),
        ),
    )
    return proposal.to_dict()


class _PilotBridge(ScriptedInvestigationTransport):
    """Choose each next tool batch from DB observations archived by prior work."""

    bridge_identity = "bridge:scripted-pilot"

    def __init__(
        self,
        *,
        uow_factory: Any,
        investigation_id: UUID,
        artifact_store: ProductionArtifactStore,
        material: SyntheticSampleMaterial,
    ) -> None:
        self._uow_factory = uow_factory
        self._investigation_id = investigation_id
        self._artifact_store = artifact_store
        self.received_prompts: list[str] = []
        self.observations_before_turn: list[tuple[UUID, ...]] = []
        self.dynamic_choices: list[dict[str, object]] = []
        super().__init__(
            (),
            uow_factory=uow_factory,
            investigation_id=investigation_id,
            material=material,
            inject_analysis_at_turn=None,
        )

    async def _archive(
        self,
    ) -> tuple[
        tuple[EvidenceObservation, ...],
        tuple[Any, ...],
        tuple[Any, ...],
        dict[UUID, dict[str, Any]],
    ]:
        async with self._uow_factory() as uow:
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(self._investigation_id)
            )
            invocations = tuple(
                await uow.tool_invocations.list_for_investigation(self._investigation_id)
            )
            associations = tuple(
                await uow.sample_associations.list_for_investigation(self._investigation_id)
            )
        payloads: dict[UUID, dict[str, Any]] = {}
        raw_sources = {"synthetic-article-seed", "network.quarantine"}
        for observation in observations:
            if observation.source in raw_sources:
                continue
            payload = await self._artifact_store.read_json(observation.blob_id)
            if isinstance(payload, dict):
                payloads[observation.id] = payload
        return observations, invocations, associations, payloads

    async def _capture_refs(
        self, observations: Sequence[EvidenceObservation]
    ) -> dict[str, NetworkCaptureRefV1]:
        captures = [item for item in observations if item.source == "network.quarantine"]
        refs: dict[str, NetworkCaptureRefV1] = {}
        async with self._uow_factory() as uow:
            for observation in captures:
                blob = await uow.blobs.get(observation.blob_id)
                if blob is None:
                    raise AssertionError("archived PCAP observation has no canonical blob")
                mime = blob.descriptor.mime_type
                capture_format = (
                    NetworkCaptureFormat.PCAPNG
                    if mime == "application/x-pcapng"
                    else NetworkCaptureFormat.PCAP
                )
                refs[observation.sha256] = NetworkCaptureRefV1(
                    observation.blob_id,
                    observation.sha256,
                    blob.descriptor.size,
                    capture_format,
                )
        return refs

    @staticmethod
    def _source(
        observations: Sequence[EvidenceObservation], source: str
    ) -> list[EvidenceObservation]:
        return [item for item in observations if item.source == source]

    async def _choose(
        self,
        observations: tuple[EvidenceObservation, ...],
        invocations: tuple[Any, ...],
        associations: tuple[Any, ...],
        payloads: Mapping[UUID, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        names = {item.tool_name for item in invocations}
        captures = await self._capture_refs(observations)
        inspected = self._source(observations, "network.capture.inspect")
        inspected_hashes = {item.object_id for item in inspected}
        stream_rows = self._source(observations, "network.stream.reassemble")
        segment_rows = self._source(observations, "network.messages.segment")

        # Import evidence is usually the newest archived family at the first turn.
        # Its position, associated IDs and actual stored report payloads select the
        # opening operation and determine the order within every independent batch.
        pending_imports = [
            item for item in captures.values() if item.sha256 not in inspected_hashes
        ]
        if pending_imports:
            capture_calls = [
                _call(
                    "network.capture.inspect",
                    {"capture_ref": _capture_dict(reference)},
                    f"Inspect operator-imported inert PCAP {reference.sha256[:12]} with TShark.",
                )
                for reference in pending_imports
            ]
            latest = max(observations, key=lambda item: (item.observed_at, str(item.id)))
            capture_calls.sort(
                key=lambda item: hashlib.sha256(
                    f"{latest.id}:{item['tool_name']}:{item['arguments']}".encode()
                ).digest()
            )
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(item.id) for item in observations],
                    "selected_tools": [str(item["tool_name"]) for item in capture_calls],
                    "selection": "latest archived evidence family: imported capture",
                }
            )
            return _batch_decision(
                "The latest archived evidence is an operator-imported capture. Inspect its exact "
                "quarantined bytes before selecting streams.",
                capture_calls,
            )

        if not all(
            name in names
            for name in (
                "native.inspect",
                "sample.extract_obfuscated_strings",
                "sample.extract_configuration",
                "vt.file_report",
                "shodan.host",
                "sample.analyze",
                "samples.compare",
            )
        ):
            ip_observation = next(
                (item for item in observations if item.object_type == "ipv4"), None
            )
            confirmed = next(
                (
                    item
                    for item in associations
                    if item.qualification is SampleQualification.CONFIRMED
                ),
                None,
            )
            hashes = sorted({item.sample_sha256 for item in associations})
            sample_observation = next(
                (
                    item
                    for item in observations
                    if item.object_type == "sha256"
                    and confirmed is not None
                    and item.object_id == confirmed.sample_sha256
                ),
                None,
            )
            if sample_observation is None or ip_observation is None or confirmed is None:
                raise AssertionError(
                    "archived article evidence and human-confirmed sample are required "
                    "for the pilot"
                )
            calls: list[dict[str, object]] = []
            if "native.inspect" not in names:
                calls.append(
                    _call(
                        "native.inspect",
                        {"sample_sha256": sample_observation.object_id, "operation": "imports"},
                        "Check the unavailable REA capability without changing its policy.",
                    )
                )
            if "sample.extract_obfuscated_strings" not in names:
                calls.append(
                    _call(
                        "sample.extract_obfuscated_strings",
                        {"sample_sha256": confirmed.sample_sha256},
                        "Check optional FLOSS availability for the confirmed inert sample.",
                    )
                )
            if "sample.extract_configuration" not in names:
                calls.append(
                    _call(
                        "sample.extract_configuration",
                        {
                            "sample_sha256": confirmed.sample_sha256,
                            "parser_id": "pilot-unavailable-parser",
                        },
                        "Check optional MWCP availability for the confirmed inert sample.",
                    )
                )
            if "vt.file_report" not in names:
                calls.append(
                    _call(
                        "vt.file_report",
                        {"file_hash": sample_observation.object_id},
                        "Pivot the article's archived SHA-256 through the scripted VT port.",
                    )
                )
            if "shodan.host" not in names:
                calls.append(
                    _call(
                        "shodan.host",
                        {"ip": ip_observation.object_id},
                        "Read the article's archived IPv4 through the scripted Shodan port.",
                    )
                )
            if "sample.analyze" not in names:
                sample_refs = [
                    str(item.id)
                    for item in observations
                    if item.object_id == confirmed.sample_sha256
                ]
                calls.append(
                    _call(
                        "sample.analyze",
                        {
                            "association_id": str(confirmed.id),
                            "purpose": (
                                "Bounded static analysis of the human-confirmed inert sample."
                            ),
                            "evidence_refs": sample_refs,
                        },
                        "Run local static analysis on the archived, confirmed sample.",
                    )
                )
            if "samples.compare" not in names:
                calls.append(
                    _call(
                        "samples.compare",
                        {"sample_sha256s": hashes},
                        "Compare the three exact inert files in the persisted investigation "
                        "corpus.",
                    )
                )
            latest = max(observations, key=lambda item: (item.observed_at, str(item.id)))
            calls.sort(
                key=lambda item: hashlib.sha256(
                    f"{latest.id}:{item['tool_name']}:{item['arguments']}".encode()
                ).digest()
            )
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(item.id) for item in observations],
                    "selected_tools": [str(item["tool_name"]) for item in calls],
                    "selection": (
                        "pending capabilities ranked from latest archived observation identity"
                    ),
                }
            )
            return _batch_decision(
                "Select pending local and provider tools from archived article evidence and the "
                "confirmed corpus; independent calls are ordered from persisted observation IDs.",
                calls,
            )

        flows: list[tuple[NetworkCaptureRefV1, str]] = []
        for inspection in inspected:
            payload = payloads.get(inspection.id, {})
            reference = captures.get(inspection.object_id)
            conversations = payload.get("conversations", [])
            if reference is None or not isinstance(conversations, list):
                continue
            for flow in conversations:
                if isinstance(flow, Mapping) and isinstance(flow.get("flow_id"), str):
                    flows.append((reference, str(flow["flow_id"])))

        stream_flow_ids = {
            str(stream["flow_id"])
            for item in stream_rows
            for stream in payloads.get(item.id, {}).get("streams", [])
            if isinstance(stream, Mapping) and isinstance(stream.get("flow_id"), str)
        }
        pending_streams = [
            (ref, flow_id) for ref, flow_id in flows if flow_id not in stream_flow_ids
        ]
        if pending_streams:
            latest_protocol = max(inspected, key=lambda item: (item.observed_at, str(item.id)))
            calls = [
                _call(
                    "network.stream.reassemble",
                    {"capture_ref": _capture_dict(reference), "flow_id": flow_id},
                    "Reassemble the observed TCP stream in capture order.",
                )
                for reference, flow_id in pending_streams
            ]
            calls.sort(
                key=lambda item: hashlib.sha256(
                    f"{latest_protocol.id}:{item['arguments']}".encode()
                ).digest()
            )
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(item.id) for item in inspected],
                    "selected_tools": [str(item["tool_name"]) for item in calls],
                    "selection": "stored TShark conversations expose unreassembled flow IDs",
                }
            )
            return _batch_decision(
                "The archived TShark reports expose these unreassembled flows; reassemble their "
                "stored bytes before interpreting messages.",
                calls,
            )

        segmented_flow_ids = {
            str(segment["flow_id"])
            for item in segment_rows
            for segment in payloads.get(item.id, {}).get("segmentations", [])
            if isinstance(segment, Mapping) and isinstance(segment.get("flow_id"), str)
        }
        pending_segments = [
            (ref, flow_id) for ref, flow_id in flows if flow_id not in segmented_flow_ids
        ]
        if pending_segments:
            latest_stream = max(stream_rows, key=lambda item: (item.observed_at, str(item.id)))
            calls = [
                _call(
                    "network.messages.segment",
                    {"capture_ref": _capture_dict(reference), "flow_id": flow_id},
                    "Measure candidate message boundaries from the archived stream bytes.",
                )
                for reference, flow_id in pending_segments
            ]
            calls.sort(
                key=lambda item: hashlib.sha256(
                    f"{latest_stream.id}:{item['arguments']}".encode()
                ).digest()
            )
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(item.id) for item in stream_rows],
                    "selected_tools": [str(item["tool_name"]) for item in calls],
                    "selection": "archived stream observations lack message segmentation",
                }
            )
            return _batch_decision(
                "The archived stream observations contain bytes but no message boundaries yet; "
                "measure boundaries without assigning semantics.",
                calls,
            )

        segmented = self._source(observations, "network.messages.segment")
        if segmented and "network.protocol.compare" not in names:
            by_flow = {flow_id: reference for reference, flow_id in flows}
            training_ids = (_flow_id(41001), _flow_id(41002))
            if not all(item in by_flow for item in training_ids):
                raise AssertionError("archived segmentation omitted the two training flows")
            traces = [
                {
                    "capture_ref": _capture_dict(by_flow[flow_id]),
                    "flow_id": flow_id,
                    "sample_sha256": None,
                    "report_id": None,
                    "holdout": False,
                }
                for flow_id in training_ids
            ]
            latest = max(segmented, key=lambda item: (item.observed_at, str(item.id)))
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(item.id) for item in segmented],
                    "selected_tools": ["network.protocol.compare"],
                    "selection": "compare only training traces present in archived segmentations",
                }
            )
            return decision(
                f"Compare the two archived training segmentations (latest evidence {latest.id}).",
                tool_call=_call(
                    "network.protocol.compare",
                    {"subject_id": str(self._investigation_id), "traces": traces},
                    "Compare measured framing across two separate training captures.",
                ),
            )

        comparisons = self._source(observations, "network.protocol.compare")
        if comparisons and "network.protocol.hypothesize" not in names:
            by_flow = {flow_id: reference for reference, flow_id in flows}
            training_ids = (_flow_id(41001), _flow_id(41002))
            segment_evidence = [
                item
                for item in segmented
                if item.id in payloads
                and any(
                    isinstance(value, Mapping) and value.get("flow_id") in training_ids
                    for value in payloads[item.id].get("segmentations", [])
                )
            ]
            if not segment_evidence:
                raise AssertionError("hypothesis has no archived training segmentation")
            proposal = _observed_frame_hypothesis()
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(item.id) for item in segment_evidence],
                    "selected_tools": ["network.protocol.hypothesize"],
                    "selection": "hypothesis is bounded by archived training segmentations",
                }
            )
            return decision(
                "Propose a bounded framing hypothesis from the archived training observations; "
                "leave message meaning unconfirmed.",
                tool_call=_call(
                    "network.protocol.hypothesize",
                    {
                        "subject_id": str(self._investigation_id),
                        "proposal": proposal,
                        "traces": [
                            {
                                "capture_ref": _capture_dict(by_flow[flow_id]),
                                "flow_id": flow_id,
                                "sample_sha256": None,
                                "report_id": None,
                                "holdout": False,
                            }
                            for flow_id in training_ids
                        ],
                    },
                    "Test candidate framing against archived training traces only.",
                ),
            )

        hypotheses = self._source(observations, "network.protocol.hypothesize")
        verifications = self._source(observations, "network.protocol.verify")
        if hypotheses and not verifications:
            hypothesis = max(hypotheses, key=lambda item: (item.observed_at, str(item.id)))
            hypothesis_payload = payloads[hypothesis.id]
            model_blob_id = hypothesis_payload.get("model_blob_id")
            if not isinstance(model_blob_id, str):
                raise AssertionError("archived hypothesis omitted its model blob id")
            by_flow = {flow_id: reference for reference, flow_id in flows}
            holdout_id = _flow_id(41003)
            if holdout_id not in by_flow:
                raise AssertionError("archived capture reports omitted the holdout flow")
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(hypothesis.id)],
                    "selected_tools": ["network.protocol.verify"],
                    "selection": "verify the archived model on the separate holdout flow",
                }
            )
            return decision(
                "The model blob ID comes from the archived hypothesis observation; verify it "
                "against the distinct archived holdout flow.",
                tool_call=_call(
                    "network.protocol.verify",
                    {
                        "model_blob_id": model_blob_id,
                        "traces": [
                            {
                                "capture_ref": _capture_dict(by_flow[holdout_id]),
                                "flow_id": holdout_id,
                                "sample_sha256": None,
                                "report_id": None,
                                "holdout": True,
                            }
                        ],
                    },
                    "Verify the archived framing hypothesis against the separate holdout.",
                ),
            )

        if verifications and "network.protocol.render" not in names:
            verified = max(verifications, key=lambda item: (item.observed_at, str(item.id)))
            model_blob_id = payloads[verified.id].get("model_blob_id")
            if not isinstance(model_blob_id, str):
                raise AssertionError("archived protocol verification omitted its model blob id")
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(verified.id)],
                    "selected_tools": ["network.protocol.render"],
                    "selection": "render the archived verified protocol model",
                }
            )
            return decision(
                "Render only the versioned protocol model stored by the verification observation.",
                tool_call=_call(
                    "network.protocol.render",
                    {"model_blob_id": model_blob_id},
                    "Render observed, inferred, unresolved, and contradicted protocol fields.",
                ),
            )

        render_rows = self._source(observations, "network.protocol.render")
        if render_rows:
            vt_invocation_refs = {
                ref
                for invocation in invocations
                if invocation.tool_name == "vt.file_report"
                for ref in invocation.response_refs
            }
            shodan_invocation_refs = {
                ref
                for invocation in invocations
                if invocation.tool_name == "shodan.host"
                for ref in invocation.response_refs
            }
            static_invocation_refs = {
                ref
                for invocation in invocations
                if invocation.tool_name == "sample.analyze"
                for ref in invocation.response_refs
            }
            vt_rows = [item for item in observations if item.id in vt_invocation_refs]
            shodan_rows = [item for item in observations if item.id in shodan_invocation_refs]
            static_rows = [item for item in observations if item.id in static_invocation_refs]
            cited_rows = [
                item
                for item in observations
                if item.source
                in {
                    "network.capture.inspect",
                    "network.messages.segment",
                    "network.protocol.verify",
                    "sample_similarity",
                }
            ]
            cited_rows.extend((*shodan_rows, *vt_rows, *static_rows))
            refs = list(dict.fromkeys(str(item.id) for item in cited_rows))
            if not shodan_rows or not vt_rows or not static_rows:
                raise AssertionError("final finding lacks archived VT, Shodan, or static evidence")
            self.dynamic_choices.append(
                {
                    "basis_observation_ids": [str(item.id) for item in observations],
                    "selected_tools": [],
                    "selection": "all proposal citations were reread from archived observations",
                }
            )
            final_statement = (
                "Les observations établissent des flux TCP et un cadrage de messages candidat; "
                "elles ne démontrent ni exécution du sample ni exfiltration."
            )
            final_subject = next(
                item.object_id for item in observations if item.object_type == "ipv4"
            )
            final_scope = "Échantillon inerte, deux PCAP importés et observation passive datée."
            final_limitations = (
                "Les octets observés ne suffisent pas à établir la sémantique des messages.",
                "La charge TLS demeure opaque; aucun payload n'a été décodé.",
                "Les entrées synthétiques ne décrivent aucune campagne réelle.",
            )
            final_proposal = {
                "kind": FindingKind.CLAIM_VERIFICATION.value,
                "assessment": FindingAssessment.QUALIFIED.value,
                "statement": final_statement,
                "subject_ref": final_subject,
                "observation_refs": refs,
                "claim_refs": ["L1.1"],
                "hypothesis_refs": [],
                "novelty": NoveltyClass.NOT_CONFIRMED.value,
                "scope": final_scope,
                "limitations": list(final_limitations),
            }
            identity = FindingProposalV1(
                kind=FindingKind.CLAIM_VERIFICATION,
                assessment=FindingAssessment.QUALIFIED,
                statement=final_statement,
                subject_ref=final_subject,
                observation_refs=tuple(UUID(item) for item in refs),
                claim_refs=("L1.1",),
                hypothesis_refs=(),
                novelty=NoveltyClass.NOT_CONFIRMED,
                scope=final_scope,
                limitations=final_limitations,
            )
            finding_id = uuid5(
                _FINDING_NAMESPACE,
                f"{self._investigation_id}:"
                f"{hashlib.sha256(identity.persistent_identity_bytes()).hexdigest()}",
            )
            final = decision(
                "Finish with a sourced correction of L1.1: static analysis and passive protocol "
                "evidence do not establish execution or exfiltration.",
                next_action=AgentNextAction.FINISH.value,
                finding_proposals=[final_proposal],
            )
            final["opinion_delta"] = {
                "blocks": [
                    {
                        "block_type": OpinionBlockType.KEY_FINDINGS.value,
                        "text": (
                            "The passive observations support a qualified description of the "
                            "protocol framing; they do not establish the claimed execution or "
                            "exfiltration."
                        ),
                        "finding_refs": [str(finding_id)],
                        "evidence_refs": refs,
                        "qualification": OpinionClaimQualification.INFERENCE.value,
                        "certainty": 0.6,
                    }
                ],
                "unresolved_points": [
                    "Message semantics and the opaque TLS payload remain unverified."
                ],
            }
            return final

        raise AssertionError(
            "Pilot Bridge found no state-derived next action; inspect persisted observations "
            "and invocations"
        )

    async def submit(self, **kwargs: Any):
        prompt = str(kwargs["prompt"])
        self.received_prompts.append(prompt)
        observations, invocations, associations, payloads = await self._archive()
        self.observations_before_turn.append(tuple(item.id for item in observations))
        response = await self._choose(observations, invocations, associations, payloads)
        self.enqueue(response)
        return await super().submit(**kwargs)


@pytest.mark.asyncio
async def test_workflow_full_supervised_first_run_dynamic_pilot(
    workflow_driver: WorkflowDriver,
    uow_factory,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("workflow-full-supervised-first-run-pilot")
    async with driver:
        typst_binary = os.environ.get("TYPST_BINARY", "typst")
        d2_binary = os.environ.get("D2_BINARY", "d2")
        template_root, font_root, fonts_lock = typst_bundle_paths()

        async def preflight() -> tuple[str, ...]:
            required = {
                "Typst": typst_binary,
                "pdftotext": "pdftotext",
                "pdftoppm": "pdftoppm",
                "pdfinfo": "pdfinfo",
                "pdffonts": "pdffonts",
                "D2": d2_binary,
                "tshark": "tshark",
                "capinfos": "capinfos",
            }
            missing = [
                name for name, executable in required.items() if shutil.which(executable) is None
            ]
            if missing:
                raise AssertionError(
                    "workflow_full pilot requires local binaries: " + ", ".join(missing)
                )
            typst_result = await _run_process(
                [shutil.which(typst_binary) or typst_binary, "--version"],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
            typst_version = f"{typst_result.stdout} {typst_result.stderr}".strip()
            if "0.15.1" not in typst_version:
                raise AssertionError(
                    f"workflow_full pilot requires Typst 0.15.1; found {typst_version}"
                )
            if not template_root.is_dir() or not font_root.is_dir() or not fonts_lock.is_file():
                raise AssertionError(
                    "workflow_full pilot requires the pinned Typst template, fonts and font lock"
                )
            return (
                f"typst:{typst_version}",
                f"d2:{shutil.which(d2_binary)}",
                f"tshark:{shutil.which('tshark')}",
                f"poppler:{shutil.which('pdftotext')},{shutil.which('pdftoppm')}",
            )

        await driver._perform(
            "required_binary_preflight",
            preflight,
            via="service",
            action_name="check local Typst 0.15.1, Poppler, D2, TShark and capture tools",
            assertions=("missing-required-binary-fails-explicitly", "real-render-bundles-present"),
        )

        marker = f"PilotSharedMarker{uuid4().hex[:12]}"
        bytes_a = b"MZ\x00PILOT-SAMPLE-A:" + marker.encode() + b":inert-A"
        bytes_b = b"MZ\x00PILOT-SAMPLE-B:" + marker.encode() + b":inert-B"
        bytes_c = b"MZ\x00PILOT-SAMPLE-C:DistinctObservedFeature:" + marker.encode() + b":inert-C"
        sha_a, sha_b, sha_c = (
            hashlib.sha256(item).hexdigest() for item in (bytes_a, bytes_b, bytes_c)
        )
        material = replace(
            SyntheticSampleMaterial.unique(),
            seed_hash=sha_a,
            sample_bytes=bytes_a,
            sample_hash=sha_a,
            ip="203.0.113.10",
        )

        edition, artifact_store, subjects, batches = await driver._perform(
            "seed_independent_article_runs",
            lambda: _compose_three_runs(
                uow_factory,
                tmp_path,
                production_order=("A", "B", "C"),
                composition_order=(),
            ),
            via="fixture",
            action_name="seed PostgreSQL-backed independent synthetic article runs",
            assertions=("three-distinct-articles", "test-database-only"),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)

        runs: dict[str, Any] = {}
        publications: dict[str, ProductionArtifact] = {}
        publication_bytes: dict[str, bytes] = {}
        for label in ("A", "B", "C"):
            async with uow_factory() as uow:
                subject_runs = tuple(await uow.production_runs.list_for_subject(subjects[label].id))
            if len(subject_runs) != 1:
                raise AssertionError(f"pilot expects one independently seeded run for {label}")
            runs[label] = subject_runs[0]
            document = article_document_v5(subjects[label].id, label, version="supervised-pilot")
            if label == "A":
                document = _with_article_claim(document)
            encoded = ProductionArtifactStore.canonical_json_bytes(
                serialize_publication_document(document)
            )
            blob_id = await artifact_store.put_bytes(
                encoded,
                bucket="workflow-full-pilot-v5-publications",
                mime_type="application/json",
            )
            publication = ProductionArtifact(
                production_run_id=runs[label].id,
                subject_id=subjects[label].id,
                stage=ProductionArtifactStage.PUBLICATION,
                version=2,
                input_hash=hashlib.sha256(encoded).hexdigest(),
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=blob_id,
            )
            async with uow_factory() as uow:
                await uow.production_artifacts.append(publication)
                await uow.commit()
            publications[label] = publication
            publication_bytes[label] = encoded

        base_publication_bytes = publication_bytes["A"]
        base_publication_sha = hashlib.sha256(base_publication_bytes).hexdigest()
        await driver.compose(
            tuple(subjects[label].id for label in ("A", "B", "C")),
            provenance_batch_ids={subjects[label].id: batches[label] for label in ("A", "B", "C")},
        )
        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted_v5 = await driver.accept()
        release_v5 = await driver.assemble()
        composition_v5 = await read_composition(uow_factory, edition.id)
        manifest_v5 = await read_manifest(uow_factory, edition.id)
        assert composition_v5 is not None and manifest_v5 is not None

        blob_catalog = artifact_store._catalog
        filesystem_store = cast(FilesystemBlobStore, blob_catalog._store)
        media_store = MediaAssetStore(artifact_store, uow_factory)
        edition_renderer = EditionRenderService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            media_asset_store=media_store,
            renderer=EditionTypstRenderer(),
            compiler=TypstSubprocessCompiler(binary=typst_binary),
            chp_typst_root=template_root,
            font_bundle_root=font_root,
            typst_fonts_lock_path=fonts_lock,
        )
        output_root = (
            Path(os.environ.get("WORKFLOW_TRACE_DIR", str(tmp_path))) / "workflow-full-pilot"
        )
        output_root.mkdir(parents=True, exist_ok=True)

        async def persisted_release_snapshot(release_id: UUID, manifest_id: UUID, render_id: UUID):
            async with uow_factory() as uow:
                release = await uow.edition_releases.get(release_id)
                manifest = await uow.publication_manifests.get(manifest_id)
                manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest_id)
                render = await uow.edition_renders.get(render_id)
                manifest_blob = await uow.blobs.get(manifest_blob_id) if manifest_blob_id else None
            assert release is not None and manifest is not None and render is not None
            assert manifest_blob_id is not None and manifest_blob is not None
            assert render.output_blob_id is not None
            document_bytes = await artifact_store.read_bytes(release.edition_document_blob_id)
            manifest_bytes = await artifact_store.read_bytes(manifest_blob_id)
            pdf_bytes = await artifact_store.read_bytes(render.output_blob_id)
            assert hashlib.sha256(document_bytes).hexdigest() == release.edition_document_sha256
            assert hashlib.sha256(manifest_bytes).hexdigest() == manifest_blob.descriptor.sha256
            assert hashlib.sha256(pdf_bytes).hexdigest() == render.output_sha256
            return release, manifest, document_bytes, manifest_bytes, pdf_bytes

        async def render_v5_edition():
            rendered = await edition_renderer.render_pdf(release_v5.id)
            assert rendered.output_blob_id is not None
            assert rendered.output_sha256 is not None
            content = await artifact_store.read_bytes(rendered.output_blob_id)
            assert hashlib.sha256(content).hexdigest() == rendered.output_sha256
            return rendered, content

        rendered_v5, pdf_v5 = await driver._perform(
            "render_verified_v5_edition",
            render_v5_edition,
            via="service",
            action_name="EditionRenderService and real Typst compile the verified V5 edition",
            identities={"release": release_v5.id, "manifest": accepted_v5.manifest.id},
            assertions=("real-typst-pdf", "verified-v5-publication"),
        )
        v5_pdf_path = output_root / "edition-v5.pdf"
        v5_pdf_path.write_bytes(pdf_v5)
        async with uow_factory() as uow:
            manifest_blob_id_v5 = await uow.publication_manifests.get_blob_id(
                accepted_v5.manifest.id
            )
            manifest_blob_v5 = (
                await uow.blobs.get(manifest_blob_id_v5) if manifest_blob_id_v5 else None
            )
        assert manifest_blob_id_v5 is not None and manifest_blob_v5 is not None
        manifest_bytes_v5 = await artifact_store.read_bytes(manifest_blob_id_v5)
        manifest_sha_v5 = hashlib.sha256(manifest_bytes_v5).hexdigest()
        assert manifest_sha_v5 == manifest_blob_v5.descriptor.sha256
        snapshot_v5 = await persisted_release_snapshot(
            release_v5.id,
            accepted_v5.manifest.id,
            rendered_v5.id,
        )
        assert snapshot_v5[4] == pdf_v5

        article_claim = (
            "L1.1: Cet article affirme que l'échantillon a exécuté le protocole propriétaire "
            "et exfiltré des identifiants."
        )
        seeded = await seed_investigation_for_existing_article(
            uow_factory,
            artifact_store,
            edition=edition,
            subject=subjects["A"],
            run=runs["A"],
            publication=publications["A"],
            publication_bytes=base_publication_bytes,
            material=material,
            article_claim=article_claim,
        )
        assert seeded.synthesis.status is ProductionArtifactStatus.VERIFIED
        assert seeded.publication.input_hash == base_publication_sha
        async with uow_factory() as uow:
            stored_synthesis = await uow.production_artifacts.get(seeded.synthesis.id)
        assert stored_synthesis is not None
        assert stored_synthesis.stage is ProductionArtifactStage.SYNTHESIS
        assert stored_synthesis.status is ProductionArtifactStatus.VERIFIED
        assert stored_synthesis.input_hash == seeded.synthesis.input_hash

        association_a = await persist_sample_association_fixture(
            uow_factory,
            artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=subjects["A"].id,
            sample_bytes=bytes_a,
            origin=SampleOrigin.SEED,
            logical_name="pilot-a-inert.bin",
        )
        await persist_sample_association_fixture(
            uow_factory,
            artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=subjects["A"].id,
            sample_bytes=bytes_b,
            origin=SampleOrigin.MANUAL,
            logical_name="pilot-b-inert.bin",
        )
        await persist_sample_association_fixture(
            uow_factory,
            artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=subjects["A"].id,
            sample_bytes=bytes_c,
            origin=SampleOrigin.MANUAL,
            logical_name="pilot-c-inert.bin",
        )
        await _persist_observation(
            uow_factory,
            artifact_store,
            investigation_id=seeded.investigation.id,
            kind=EvidenceKind.OTHER,
            payload={"indicator": material.ip, "claim_ref": "L1.1"},
            source="synthetic-article-seed",
            object_type="ipv4",
            object_id=material.ip,
        )

        settings = investigation_settings(providers=True, local_analysis=True, rules=False)
        settings = settings.model_copy(
            update={
                "native_analysis_provider": "rea",
                "mwcp_parser_versions": {"pilot-unavailable-parser": "fixture-not-installed"},
            }
        )
        network_limits = NetworkExtractionLimitsV1()
        tshark = TsharkAdapter(
            runner=_PilotConfinedLocalCommandRunner(
                max_sample_bytes=network_limits.max_capture_bytes
            ),
            limits=network_limits,
        )
        vt = HashCheckedVirusTotal(material=material, include_candidate=False)
        shodan = FakeShodanPort()
        runtime = build_investigation_runtime_composition_factory(
            settings,
            uow_factory=uow_factory,
            blob_store=filesystem_store,
            blob_catalog=blob_catalog,
            artifact_store=artifact_store,
            model_conversations=SimpleNamespace(),  # type: ignore[arg-type]
            vt_adapter=vt,
            vt_capabilities=VirusTotalCapabilities(
                file_report=True,
                file_relationships=False,
                intelligence_search=False,
                file_download=False,
            ),
            vt_observations=VirusTotalObservationService(blob_catalog, uow_factory),
            shodan_adapter=shodan,
            ghidra_adapter=None,
            network_quarantine_factory=lambda investigation_id: ProductionQuarantinedCaptureSource(
                uow_factory=uow_factory,
                blob_catalog=blob_catalog,
                settings=settings,
                investigation_id=investigation_id,
            ),
            tshark=tshark,
        )
        inventory = describe_tool_inventory(runtime)
        inventory_path = output_root / "runtime-port-inventory.json"
        inventory_path.write_text(
            json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        families = inventory["families"]
        assert isinstance(families, Mapping)
        for family in ("native_analysis", "floss_extraction", "configuration_extraction"):
            item = families[family]
            assert isinstance(item, Mapping) and item["status"] == "unavailable"
        assert isinstance(families["network_capture"], Mapping)
        assert families["network_capture"]["status"] == "available"

        common = f"PilotSharedFeature{uuid4().hex[:10]}"
        similarity = _family_similarity(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            investigation_id=seeded.investigation.id,
            feature_packs={
                sha_a: _feature_pack(sha_a, strings=((common, 0x240),)),
                sha_b: _feature_pack(sha_b, strings=((common, 0x318),)),
                sha_c: _feature_pack(sha_c, strings=(("PilotDistinctFeature", 0x420),)),
            },
            baseline_id=uuid4(),
        )
        optional_ports = runtime.composition_factory.optional_tool_ports
        assert optional_ports is not None
        ports = replace(optional_ports, sample_similarity=similarity)
        transport = _PilotBridge(
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            artifact_store=artifact_store,
            material=material,
        )
        factory = replace(
            runtime.composition_factory,
            optional_tool_ports=ports,
            turn_transport=transport,
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            composition_factory=factory,
        )
        app.include_router(capture_import_router)
        reviewer_identity = "human:pilot-reviewer"
        app.state.identity_provider = LocalIdentityProvider(reviewer_identity)
        app.state.network_capture_import_service = NetworkCaptureImportService(
            uow_factory=uow_factory,
            blob_catalog=blob_catalog,
            settings=settings,
        )
        first_capture, second_capture = _unique_captures(uuid4().bytes)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://workflow-full-pilot"
        ) as client:
            started = await start_investigation_http(client, seeded)
            qualification = await qualify_sample_http(
                client,
                seeded.investigation.id,
                association_a.id,
                expected_version=association_a.version,
                qualification=SampleQualification.CONFIRMED,
                evidence_refs=(seeded.seed_observation.id,),
            )
            assert qualification.status_code == 200, qualification.text
            assert qualification.json()["association"]["qualification"] == "confirmed"
            imports = []
            for content in (first_capture, second_capture):
                content_sha = hashlib.sha256(content).hexdigest()
                imported = await client.post(
                    f"/api/v1/investigations/{seeded.investigation.id}/network-captures",
                    content=content,
                    headers={
                        "Content-Type": "application/vnd.tcpdump.pcap",
                        "X-Content-SHA256": content_sha,
                    },
                )
                assert imported.status_code == 201, imported.text
                assert imported.json()["sha256"] == content_sha
                imports.append(imported.json())
            assert len({item["sha256"] for item in imports}) == 2
            assert all(item["format"] == "pcap" for item in imports)

            approved = await approve_envelope_http(
                client,
                seeded.investigation.id,
                providers=("vt", "shodan"),
                budget_limits={
                    "bridge_turns": 12,
                    "tool_seconds": 3_600,
                    "vt_units": 8,
                    "shodan_credits": 4,
                    "downloads": 0,
                },
                allow_acquisition=False,
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["approved"] is True
            driver.edition_id = edition.id
            await driver._perform(
                "run_agent_through_investigation_service",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name=(
                    "InvestigationService and RegistryToolDispatcher execute dynamic Bridge tools"
                ),
                identities={"investigation": seeded.investigation.id},
                assertions=(
                    "archived-observations-fed-following-turns",
                    "production-runtime-ports",
                    "no-external-network",
                ),
            )

            async with uow_factory() as uow:
                observations = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                findings = tuple(
                    await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
                )
                state = await uow.investigation_states.get(seeded.investigation.id)
            invocation_by_name: dict[str, list[Any]] = {}
            for invocation in invocations:
                invocation_by_name.setdefault(invocation.tool_name, []).append(invocation)

            required_tools = {
                "native.inspect",
                "sample.extract_obfuscated_strings",
                "sample.extract_configuration",
                "vt.file_report",
                "shodan.host",
                "sample.analyze",
                "samples.compare",
                "network.capture.inspect",
                "network.stream.reassemble",
                "network.messages.segment",
                "network.protocol.compare",
                "network.protocol.hypothesize",
                "network.protocol.verify",
                "network.protocol.render",
            }
            assert required_tools <= set(invocation_by_name)
            for unavailable_name in (
                "native.inspect",
                "sample.extract_obfuscated_strings",
                "sample.extract_configuration",
            ):
                assert all(
                    item.status.value == "unavailable"
                    for item in invocation_by_name[unavailable_name]
                )
            unavailable_tools = {
                "native.inspect",
                "sample.extract_obfuscated_strings",
                "sample.extract_configuration",
            }
            partial_is_evidence = {"sample.analyze", "network.protocol.verify"}
            unexpected_statuses = {
                name: [item.status.value for item in invocation_by_name[name]]
                for name in required_tools - unavailable_tools - partial_is_evidence
                if any(
                    item.status is not ToolInvocationStatus.SUCCEEDED
                    for item in invocation_by_name[name]
                )
            }
            assert not unexpected_statuses, unexpected_statuses
            assert all(
                item.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
                for item in invocation_by_name["sample.analyze"]
            )
            assert all(
                item.status is ToolInvocationStatus.SUCCEEDED
                for item in invocation_by_name["network.protocol.verify"]
            ), "the separate holdout must corroborate the observed frame structure"
            assert not any(item.tool_name.startswith("rule.") for item in invocations)
            assert not any(item.tool_name.startswith("vt.retrohunt") for item in invocations)
            assert not any(item.tool_name == "vt.download_sample" for item in invocations)
            assert not any(item.kind is EvidenceKind.YARA_MATCH for item in observations)
            assert state is not None and state.stop_reason is not None
            assert state.stop_reason.value == "agent_finished"
            assert len(transport.dynamic_choices) >= 7
            observation_ids = {item.id for item in observations}
            assert all(
                {UUID(value) for value in item["basis_observation_ids"]} <= observation_ids
                for item in transport.dynamic_choices
            )
            assert len(transport.dynamic_choices) == len(transport.observations_before_turn)
            for turn_index, choice in enumerate(transport.dynamic_choices[:-1]):
                produced_refs = {
                    reference
                    for name in choice["selected_tools"]
                    for invocation in invocation_by_name.get(str(name), ())
                    for reference in invocation.response_refs
                }
                assert produced_refs <= set(transport.observations_before_turn[turn_index + 1]), (
                    "the next Bridge turn must reread each prior tool result from PostgreSQL"
                )
            assert all(
                str(item.id) in transport.received_prompts[0]
                for item in observations
                if item.source == "network.quarantine"
            )

            comparison_invocation = invocation_by_name["samples.compare"][0]
            compared_hashes = comparison_invocation.normalized_parameters["sample_sha256s"]
            assert set(compared_hashes) == {sha_a, sha_b, sha_c}
            assert len(compared_hashes) == 3
            assert comparison_invocation.response_refs
            assert (
                len([item for item in observations if item.source == "network.capture.inspect"])
                == 2
            )
            assert (
                len([item for item in observations if item.source == "network.protocol.render"])
                == 1
            )
            claim_findings = [
                item for item in findings if item.kind is FindingKind.CLAIM_VERIFICATION
            ]
            assert len(claim_findings) == 1
            pilot_finding = claim_findings[0]
            assert pilot_finding.claim_refs == ("L1.1",)
            assert pilot_finding.assessment is FindingAssessment.QUALIFIED
            assert pilot_finding.observation_refs
            assert set(pilot_finding.observation_refs) <= observation_ids

            tool_trace = {
                "schema_version": "workflow-full-pilot-tool-trace.v1",
                "investigation_id": str(seeded.investigation.id),
                "bridge_identity": transport.bridge_identity,
                "dynamic_choices": transport.dynamic_choices,
                "executed_tools": [
                    {
                        "tool_name": item.tool_name,
                        "status": item.status.value,
                        "parameters": dict(item.normalized_parameters),
                        "response_refs": [str(ref) for ref in item.response_refs],
                    }
                    for item in invocations
                ],
                "archived_observations": [
                    {
                        "id": str(item.id),
                        "source": item.source,
                        "sha256": item.sha256,
                        "object_type": item.object_type,
                        "object_id": item.object_id,
                    }
                    for item in observations
                ],
            }
            tool_trace_path = output_root / "tool-executions.json"
            tool_trace_path.write_text(
                json.dumps(
                    tool_trace,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    default=_json_default,
                )
                + "\n",
                encoding="utf-8",
            )

            network_refs = await transport._capture_refs(observations)
            assert len(network_refs) == 2
            verification_rows = [
                item for item in observations if item.source == "network.protocol.verify"
            ]
            assert verification_rows
            verification_payload = await artifact_store.read_json(verification_rows[-1].blob_id)
            model_id = UUID(verification_payload["model_blob_id"])
            model_payload = await artifact_store.read_json(model_id)
            from cti_app.domain.protocol_model import ProtocolModelV1, ProtocolValidationStatus

            model = ProtocolModelV1.from_dict(model_payload)
            assert model.validation_status is ProtocolValidationStatus.OBSERVED
            assert len(model.messages) >= 2
            assert model.protocol is None
            assert {item.name for item in model.messages} == {
                "observed_message_1",
                "observed_message_2",
            }
            assert all(
                item.trigger == "message_observed"
                and item.from_state.startswith("observed_state_")
                and item.to_state.startswith("observed_state_")
                for item in model.transitions
            )
            assert {item.from_state for item in model.transitions} == {
                "observed_state_0",
                "observed_state_1",
            }

            async with uow_factory() as uow:
                recorder = ProductionEvidenceRecorder(artifact_store, uow.evidence_observations)
                capture_workbench = NetworkWorkbench(
                    quarantine=ProductionQuarantinedCaptureSource(
                        uow_factory=uow_factory,
                        blob_catalog=blob_catalog,
                        settings=settings,
                        investigation_id=seeded.investigation.id,
                    ),
                    blobs=ProductionNetworkCaptureBlobStore(artifact_store),
                    tshark=tshark,
                    recorder=recorder,
                    limits=network_limits,
                )
                semantic_render = NetworkProtocolWorkbench(capture_workbench).render(model)
            render_observation = next(
                item for item in observations if item.source == "network.protocol.render"
            )
            stored_semantic_render = await artifact_store.read_json(render_observation.blob_id)
            assert stored_semantic_render == semantic_render.to_dict()
            from cti_app.application.protocol_diagram_projection import (
                compile_protocol_render_result,
                opinion_protocol_figures,
            )

            rendered_figures = await compile_protocol_render_result(
                semantic_render,
                compiler=D2DiagramCompiler(binary=d2_binary),
                media_asset_store=media_store,
                production_run_id=seeded.run.id,
            )
            figures = opinion_protocol_figures(rendered_figures)
            assert len(figures) == 3
            for rendered_figure in rendered_figures:
                figure_path = output_root / f"protocol-{rendered_figure.spec.kind.value}.png"
                figure_path.write_bytes(rendered_figure.preview.media_bytes)
                assert figure_path.stat().st_size > 1_024

            composed = await compose_opinion_http(client, seeded.investigation.id)
            assert composed.status_code == 200, composed.text
            draft_payload = composed.json()["draft"]
            opinion_service = AnalystOpinionService(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
            )
            draft = await opinion_service.get_draft(seeded.investigation.id)
            assert draft.opinion is not None
            assert str(draft.opinion.id) == draft_payload["id"]
            assert isinstance(draft.opinion, AnalystOpinionV1)

            evidence_by_id = {item.id: item for item in observations}
            available_finding_ids = {item.id for item in findings}
            static_observation_ids = tuple(
                ref
                for item in invocation_by_name["sample.analyze"]
                for ref in item.response_refs
                if ref in evidence_by_id
                and evidence_by_id[ref].source == "autowork.static_analysis"
            )
            shodan_observation_ids = tuple(invocation_by_name["shodan.host"][0].response_refs)
            similarity_observation_ids = tuple(comparison_invocation.response_refs)
            protocol_observation_ids = tuple(
                item.id
                for item in observations
                if item.source in {"network.protocol.verify", "network.protocol.render"}
            )
            required_citations = {
                pilot_finding.id,
                *static_observation_ids,
                *shodan_observation_ids,
                *similarity_observation_ids,
                *protocol_observation_ids,
            }
            assert required_citations <= (available_finding_ids | set(evidence_by_id))
            blocks = (
                OpinionBlock(
                    block_type=OpinionBlockType.KEY_FINDINGS,
                    text=(
                        "La phrase L1.1 doit être limitée : les éléments réunis ne démontrent ni "
                        "l'exécution de l'échantillon ni l'exfiltration d'identifiants."
                    ),
                    finding_refs=(pilot_finding.id,),
                    evidence_refs=(static_observation_ids[0], protocol_observation_ids[0]),
                    qualification=OpinionClaimQualification.INFERENCE,
                    certainty=0.78,
                ),
                OpinionBlock(
                    block_type=OpinionBlockType.NETWORK_AND_INFRASTRUCTURE,
                    text=(
                        "Shodan a indexé des services sur l'indicateur de l'article à une date "
                        "précise; cela ne démontre pas leur activité pendant l'incident."
                    ),
                    finding_refs=(pilot_finding.id,),
                    evidence_refs=(shodan_observation_ids[0],),
                    qualification=OpinionClaimQualification.INFERENCE,
                    certainty=0.94,
                ),
                OpinionBlock(
                    block_type=OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES,
                    text=(
                        "L'analyse statique locale a relevé un format PE et des chaînes; elle ne "
                        "prouve pas l'exécution. La comparaison de trois fichiers décrit des "
                        "caractéristiques mesurées, sans établir une appartenance de famille."
                    ),
                    finding_refs=(pilot_finding.id,),
                    evidence_refs=(static_observation_ids[0], similarity_observation_ids[0]),
                    qualification=OpinionClaimQualification.INFERENCE,
                    certainty=0.71,
                ),
                OpinionBlock(
                    block_type=OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
                    text=(
                        "Les captures établissent le cadrage de deux trames observées; le holdout "
                        "le reproduit. Les preuves sont insuffisantes pour identifier le protocole "
                        "applicatif ou attribuer un sens aux messages. La charge TLS demeure "
                        "opaque."
                    ),
                    finding_refs=(pilot_finding.id,),
                    evidence_refs=protocol_observation_ids,
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                    certainty=0.48,
                ),
            )
            for block in blocks:
                assert set(block.finding_refs) <= available_finding_ids
                assert set(block.evidence_refs) <= set(evidence_by_id)
                assert not any(value in block.text for value in (sha_a, sha_b, sha_c))
            revised = await opinion_service.revise(
                draft.opinion.id,
                draft.opinion.version,
                OpinionRevisionEdits(
                    blocks=tuple(blocks),
                    unresolved_points=(
                        "Les octets observés ne suffisent pas à établir "
                        "la sémantique des messages.",
                        "Les captures synthétiques ne démontrent ni exécution ni exfiltration.",
                    ),
                    protocol_figures=figures,
                ),
                actor=reviewer_identity,
            )
            assert revised.protocol_figures == figures
            assert all(block.finding_refs for block in revised.blocks if block.is_renderable)
            citation_check = await opinion_service.get_draft(seeded.investigation.id)
            assert citation_check.citation_violations == ()
            assert not any(item.tool_name.startswith("rule.") for item in invocations)
            approved_opinion = await approve_opinion_http(
                client,
                revised.id,
                version=revised.version,
                sha256=revised.content_sha256,
            )
            assert approved_opinion.status_code == 200, approved_opinion.text
            approved_payload = approved_opinion.json()["approved"]
            assert approved_payload["version"] == revised.version + 1
            assert reviewer_identity != transport.bridge_identity

            published = await publish_opinion_http(
                client,
                revised.id,
                version=approved_payload["version"],
                base_publication_sha256=approved_payload["provenance"]["base_publication_sha256"],
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
            assert stored_v6 is not None and stored_v6.canonical_blob_id is not None
            assert stored_v6.stage is ProductionArtifactStage.PUBLICATION
            assert stored_v6.version > publications["A"].version
            assert v6_blob is not None
            v6_document = publication_document_from_json(
                await artifact_store.read_json(stored_v6.canonical_blob_id)
            )
            assert isinstance(v6_document, PublicationDocumentV6)
            assert v6_document.rendered_opinion is not None
            assert v6_document.rendered_opinion.protocol_figures == figures
            assert await artifact_store.read_bytes(
                cast(UUID, publications["A"].canonical_blob_id)
            ) == (base_publication_bytes)

            async def repin_v6_explicitly():
                service = EditionCompositionService(uow_factory)
                current = await service.get(edition.id)
                assert current.composition is not None
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
                    for entry in current.composition.entries
                )
                return await service.replace_entries(
                    edition.id,
                    entries,
                    expected_version=current.composition.version,
                    idempotency_key=f"pilot-human-repin-{uuid4()}",
                    actor_id=reviewer_identity,
                    correlation_id=f"pilot-repin:{uuid4()}",
                )

            repinned = await driver._perform(
                "human_edition_composition_repin",
                repin_v6_explicitly,
                via="service",
                action_name="human reviewer explicitly repins V6 with composition compare-and-swap",
                identities={"edition": edition.id, "publication": v6_id},
                assertions=("explicit-v6-repin", "other-articles-unchanged"),
            )
            assert repinned.version == composition_v5.version + 1
            composition_after = await read_composition(uow_factory, edition.id)
            assert composition_after is not None
            old_entries = {item.subject_id: item for item in composition_v5.entries}
            new_entries = {item.subject_id: item for item in composition_after.entries}
            assert new_entries[subjects["A"].id].artifact_id == stored_v6.id
            assert all(
                new_entries[subjects[label].id] == old_entries[subjects[label].id]
                for label in ("B", "C")
            )

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
            rendered_v6 = await driver._perform(
                "render_real_typst_v6_preview",
                lambda: publication_renderer.render_pdf(v6_id),
                via="service",
                action_name="PublicationRenderService compiles the versioned V6 preview with Typst",
                identities={"publication": v6_id},
                assertions=("real-typst-0.15.1", "three-d2-protocol-figures"),
            )
            assert rendered_v6.output_blob_id is not None
            assert rendered_v6.output_sha256 is not None
            pdf_v6 = await artifact_store.read_bytes(rendered_v6.output_blob_id)
            assert hashlib.sha256(pdf_v6).hexdigest() == rendered_v6.output_sha256
            v6_pdf_path = output_root / "publication-v6-preview.pdf"
            v6_pdf_path.write_bytes(pdf_v6)

            text_result = await _run_process(
                [shutil.which("pdftotext") or "pdftotext", str(v6_pdf_path), "-"],
                capture_output=True,
                check=True,
                timeout=30,
                env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
            pdf_text = " ".join(
                text_result.stdout.decode("utf-8", errors="replace").casefold().split()
            )
            assert "l1.1" in pdf_text
            assert "exfiltration" in pdf_text or "exfiltré" in pdf_text
            assert "holdout" in pdf_text
            assert not any(value in pdf_text for value in (sha_a, sha_b, sha_c))
            info = await _run_process(
                [shutil.which("pdfinfo") or "pdfinfo", str(v6_pdf_path)],
                capture_output=True,
                check=True,
                text=True,
                timeout=15,
            )
            page_line = next(
                (line for line in info.stdout.splitlines() if line.startswith("Pages:")), None
            )
            if page_line is None or int(page_line.split(":", 1)[1].strip()) < 1:
                raise AssertionError("pdftotext output has no readable rendered PDF pages")
            page_prefix = output_root / "publication-v6-page"
            await _run_process(
                [
                    shutil.which("pdftoppm") or "pdftoppm",
                    "-png",
                    "-r",
                    "120",
                    str(v6_pdf_path),
                    str(page_prefix),
                ],
                capture_output=True,
                check=True,
                timeout=45,
            )
            rendered_pages = tuple(sorted(output_root.glob("publication-v6-page-*.png")))
            assert len(rendered_pages) >= 1
            assert all(item.stat().st_size > 1_024 for item in rendered_pages)
            figure_captions = tuple(item.caption.casefold() for item in figures)
            assert all(caption in pdf_text for caption in figure_captions)

            manifest_after = await read_manifest(uow_factory, edition.id)
            assert manifest_after is not None
            async with uow_factory() as uow:
                manifest_blob_id_after = await uow.publication_manifests.get_blob_id(
                    manifest_after.id
                )
                manifest_blob_after = (
                    await uow.blobs.get(manifest_blob_id_after)
                    if manifest_blob_id_after is not None
                    else None
                )
            assert manifest_blob_id_after is not None and manifest_blob_after is not None
            manifest_bytes_after = await artifact_store.read_bytes(manifest_blob_id_after)
            historical_after = await persisted_release_snapshot(
                release_v5.id,
                accepted_v5.manifest.id,
                rendered_v5.id,
            )
            assert manifest_after.id == manifest_v5.id
            assert hashlib.sha256(manifest_bytes_after).hexdigest() == manifest_sha_v5
            assert manifest_bytes_after == manifest_bytes_v5
            assert historical_after == snapshot_v5
            assert hashlib.sha256(historical_after[3]).hexdigest() == manifest_sha_v5
            assert (
                hashlib.sha256(historical_after[4]).hexdigest()
                == hashlib.sha256(pdf_v5).hexdigest()
            )
            assert await artifact_store.read_bytes(
                cast(UUID, publications["A"].canonical_blob_id)
            ) == (base_publication_bytes)

            final_trace = {
                "schema_version": "workflow-full-pilot-artifacts.v1",
                "investigation_id": str(seeded.investigation.id),
                "v5_publication_sha256_before_after": [base_publication_sha, base_publication_sha],
                "v5_manifest_sha256_before_after": [manifest_sha_v5, manifest_sha_v5],
                "v5_edition_pdf_sha256_before_after": [
                    hashlib.sha256(pdf_v5).hexdigest(),
                    hashlib.sha256(historical_after[4]).hexdigest(),
                ],
                "v6_publication_artifact_id": str(v6_id),
                "v6_preview_pdf_sha256": rendered_v6.output_sha256,
                "human_reviewer": reviewer_identity,
                "bridge_identity": transport.bridge_identity,
                "protocol_figures": [item.kind.value for item in figures],
                "no_external_release": True,
                "no_retrohunt_or_yara": True,
            }
            (output_root / "pilot-artifact-summary.json").write_text(
                json.dumps(final_trace, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

            invariants.require(
                len(rendered_pages) >= 1
                and len(figures) == 3
                and manifest_after.id == manifest_v5.id
                and hashlib.sha256(historical_after[4]).hexdigest()
                == hashlib.sha256(pdf_v5).hexdigest(),
                driver.scenario,
                "supervised_first_run_artifacts",
                "the V6 preview must render readably while the prior V5 manifest "
                "and PDF stay immutable",
            )
