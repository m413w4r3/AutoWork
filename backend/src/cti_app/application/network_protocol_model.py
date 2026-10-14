"""Deterministic protocol comparison, hypothesis replay, verification, and semantic render.

Composition integration call contract (deliberately not wired here):
register_protocol_tools(
    registry,
    service=NetworkProtocolWorkbench(network_workbench),
    recorder=evidence_recorder,
    model_store=ProductionProtocolModelStore(artifact_store),
)
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from cti_app.application.network_workbench import NetworkWorkbench
from cti_app.domain.network_protocol import (
    EvidenceStatus,
    ExtractorVersionV1,
    MessageBoundaryV1,
    NetworkCaptureRefV1,
    NetworkExtractionLimitsV1,
    NetworkTransport,
    ReassembledStreamV1,
    StreamDirection,
)
from cti_app.domain.protocol_model import (
    AssuranceLevel,
    FieldDefinitionV1,
    FieldStability,
    MessageDefinitionV1,
    ProtocolConversationV1,
    ProtocolDiagramEdgeV1,
    ProtocolDiagramFieldV1,
    ProtocolDiagramKind,
    ProtocolDiagramNodeV1,
    ProtocolDiagramSpecV1,
    ProtocolDiagramStatus,
    ProtocolEndpointV1,
    ProtocolEvidenceRefV1,
    ProtocolHypothesisProposalV1,
    ProtocolModelV1,
    ProtocolRenderResultV1,
    ProtocolTransitionV1,
    ProtocolValidationStatus,
    ProtocolVerificationFindingV1,
)
from cti_app.infrastructure.pcap_reader import CapturedPacket

_SOURCE = "network.messages.segment"
_MAX_TRACE_COUNT = 32
_MAX_OBSERVED_MESSAGES = 512
_MAX_MODEL_FIELDS = 256
_MAX_BYTE_RUNS_PER_MESSAGE = 64
_MAX_FINDING_TEXT = 64


class ProtocolTraceError(ValueError):
    """A trace cannot be loaded or does not meet the protocol service contract."""


class HypothesisNotReplayableError(ValueError):
    """The structured schema hypothesis did not replay on its training traces."""


@dataclass(frozen=True, slots=True)
class ProtocolTraceRequestV1:
    capture: NetworkCaptureRefV1
    flow_id: str
    sample_sha256: str | None = None
    report_id: UUID | None = None
    holdout: bool = False

    def __post_init__(self) -> None:
        if not self.flow_id:
            raise ValueError("flow_id is required")
        if self.sample_sha256 is not None and not _is_sha256(self.sample_sha256):
            raise ValueError("sample_sha256 must be lowercase hexadecimal")


@dataclass(frozen=True, slots=True)
class ProtocolComparisonOutcomeV1:
    model: ProtocolModelV1
    compatible: bool
    stable_field_ids: tuple[str, ...]
    variant_field_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ObservedMessage:
    ordinal: int
    boundary: MessageBoundaryV1
    stream: ReassembledStreamV1
    content: bytes | None
    evidence: ProtocolEvidenceRefV1

    @property
    def length(self) -> int:
        return self.boundary.end_offset - self.boundary.start_offset


@dataclass(frozen=True, slots=True)
class _ObservedTrace:
    request: ProtocolTraceRequestV1
    transport: NetworkTransport
    source: ProtocolEndpointV1
    destination: ProtocolEndpointV1
    direction: StreamDirection
    orientation_basis: str
    frame_numbers: tuple[int, ...]
    messages: tuple[_ObservedMessage, ...]
    segmentation_fields: tuple[tuple[object, ProtocolEvidenceRefV1], ...]
    evidence: ProtocolEvidenceRefV1
    extractor_versions: tuple[ExtractorVersionV1, ...]
    limits: NetworkExtractionLimitsV1
    limitations: tuple[str, ...]


class NetworkProtocolWorkbench:
    """Build protocol models from bounded L5 traces without returning raw payloads."""

    def __init__(self, workbench: NetworkWorkbench) -> None:
        self._workbench = workbench

    async def compare(
        self,
        *,
        subject_id: str,
        investigation_id: UUID,
        traces: Sequence[ProtocolTraceRequestV1],
    ) -> ProtocolComparisonOutcomeV1:
        requests = _bounded_traces(traces, require_holdout=False)
        observed = await self._load_traces(requests, investigation_id=investigation_id)
        if not observed:
            raise ProtocolTraceError("no trace material is available")
        transports = {trace.transport for trace in observed}
        if len(transports) != 1:
            raise ProtocolTraceError("one protocol model cannot mix transport protocols")

        ordinal_groups: dict[tuple[StreamDirection, int], list[_ObservedMessage]] = defaultdict(
            list
        )
        if sum(len(trace.messages) for trace in observed) > _MAX_OBSERVED_MESSAGES:
            raise ProtocolTraceError("protocol comparison exceeds the observed-message limit")
        for trace in observed:
            for message in trace.messages:
                ordinal_groups[(trace.direction, message.ordinal)].append(message)

        messages: list[MessageDefinitionV1] = []
        fields: list[FieldDefinitionV1] = []
        stable_ids: list[str] = []
        variant_ids: list[str] = []
        message_ids_by_trace: dict[tuple[str, str, StreamDirection], list[str]] = defaultdict(list)
        field_ids_by_message: dict[str, list[str]] = defaultdict(list)

        for (direction, ordinal), items in sorted(
            ordinal_groups.items(), key=lambda pair: (pair[0][0].value, pair[0][1])
        ):
            message_id = _message_id(direction, ordinal)
            evidence = _unique_evidence(item.evidence for item in items)
            opaque = any(item.stream.encrypted_opaque for item in items)
            lengths = [item.length for item in items]
            messages.append(
                MessageDefinitionV1(
                    message_id=message_id,
                    name=(
                        f"opaque_encrypted_message_{ordinal}"
                        if opaque
                        else f"observed_message_{ordinal}"
                    ),
                    ordinal=ordinal,
                    directions=(direction,),
                    min_length=min(lengths),
                    max_length=max(lengths),
                    field_ids=(),
                    validation_status=(
                        ProtocolValidationStatus.UNRESOLVED
                        if opaque
                        else _status_from_boundary(items)
                    ),
                    evidence=evidence,
                    encrypted_opaque=opaque,
                )
            )
            for trace in observed:
                if trace.direction is direction and any(
                    item.ordinal == ordinal for item in trace.messages
                ):
                    message_ids_by_trace[
                        (trace.request.capture.sha256, trace.request.flow_id, direction)
                    ].append(message_id)

            candidates: dict[
                tuple[str, int | None, int | None, str | None],
                list[tuple[object, _ObservedMessage]],
            ] = defaultdict(list)
            for trace in observed:
                for item in trace.messages:
                    if item.ordinal != ordinal or item.stream.direction is not direction:
                        continue
                    for candidate, _proof in trace.segmentation_fields:
                        kind = getattr(getattr(candidate, "kind", None), "value", "field")
                        offset = getattr(candidate, "offset", None)
                        length = getattr(candidate, "length", None)
                        endian = getattr(candidate, "endianness", None)
                        candidates[(str(kind), offset, length, endian)].append((candidate, item))
            for (kind, raw_offset, length, endian), candidate_items in sorted(
                candidates.items(),
                key=lambda pair: (
                    pair[0][1] is None,
                    pair[0][1] or -1,
                    pair[0][0],
                    pair[0][3] or "",
                ),
            ):
                candidate, _ = candidate_items[0]
                candidate_name = str(getattr(candidate, "name", kind))
                relative_offset = _relative_field_offset(raw_offset, candidate_items, ordinal)
                field_id = _field_id(message_id, candidate_name, relative_offset, length, endian)
                proof_items = _unique_evidence(item.evidence for _, item in candidate_items)
                digests, independent_observations = _candidate_value_digests(
                    candidate_items, relative_offset, length
                )
                state = _candidate_status(getattr(candidate, "status", EvidenceStatus.UNRESOLVED))
                encrypted = opaque and kind == "payload"
                stability = FieldStability.UNKNOWN
                if independent_observations >= 2:
                    stability = (
                        FieldStability.VARIANT if len(digests) > 1 else FieldStability.STABLE
                    )
                if encrypted:
                    state = ProtocolValidationStatus.UNRESOLVED
                    relative_offset = None
                    length = None
                    endian = None
                field = FieldDefinitionV1(
                    field_id=field_id,
                    message_id=message_id,
                    name=candidate_name,
                    offset=relative_offset,
                    length=length,
                    endianness=endian,
                    encoding=None,
                    condition=None,
                    source=_SOURCE,
                    validation_status=state,
                    stability=stability,
                    evidence=proof_items,
                    value_sha256s=digests,
                    encrypted_opaque=encrypted,
                )
                if field.field_id not in {existing.field_id for existing in fields}:
                    if len(fields) >= _MAX_MODEL_FIELDS:
                        raise ProtocolTraceError("protocol model exceeds the field limit")
                    fields.append(field)
                    field_ids_by_message[message_id].append(field_id)
                    if field.stability is FieldStability.STABLE:
                        stable_ids.append(field_id)
                    elif field.stability is FieldStability.VARIANT:
                        variant_ids.append(field_id)

            raw_observations_by_key = {
                _message_observation_key(item): (item, item.content)
                for item in items
                if item.content is not None and not item.stream.encrypted_opaque
            }
            raw_observations = list(raw_observations_by_key.values())
            if len(raw_observations) >= 2:
                for start, end, stable in _byte_runs(raw_observations):
                    name = f"{'stable' if stable else 'variant'}_bytes_{start}_{end - start}"
                    field_id = _field_id(message_id, name, start, end - start, None)
                    proof_items = _unique_evidence(
                        _range_evidence(item, start, end) for item, _ in raw_observations
                    )
                    value_hashes = tuple(
                        sorted(
                            {
                                hashlib.sha256(content[start:end]).hexdigest()
                                for _, content in raw_observations
                                if content is not None
                            }
                        )
                    )
                    stability = FieldStability.STABLE if stable else FieldStability.VARIANT
                    if len(fields) >= _MAX_MODEL_FIELDS:
                        raise ProtocolTraceError("protocol model exceeds the field limit")
                    fields.append(
                        FieldDefinitionV1(
                            field_id=field_id,
                            message_id=message_id,
                            name=name,
                            offset=start,
                            length=end - start,
                            endianness=None,
                            encoding=None,
                            condition=None,
                            source="network.protocol.compare",
                            validation_status=(
                                ProtocolValidationStatus.OBSERVED
                                if stable
                                else ProtocolValidationStatus.INFERRED
                            ),
                            stability=stability,
                            evidence=proof_items,
                            value_sha256s=value_hashes,
                        )
                    )
                    field_ids_by_message[message_id].append(field_id)
                    (stable_ids if stable else variant_ids).append(field_id)

        messages = [
            replace(
                message, field_ids=tuple(dict.fromkeys(field_ids_by_message[message.message_id]))
            )
            for message in messages
        ]
        conversations = self._conversations(observed, message_ids_by_trace)
        endpoints = _unique_endpoints(
            endpoint for trace in observed for endpoint in (trace.source, trace.destination)
        )
        validation = (
            ProtocolValidationStatus.INFERRED
            if any(trace.messages for trace in observed)
            else ProtocolValidationStatus.UNRESOLVED
        )
        limitations = list(limitation for trace in observed for limitation in trace.limitations)
        limitations.append(
            "Les endpoints et ports partagés ne prouvent pas le lien au binaire ni à une famille."
        )
        if _trace_views(observed):
            limitations.append("Les charges marquées encrypted_opaque restent opaques.")
        model = ProtocolModelV1(
            schema_version="ProtocolModelV1",
            subject_id=subject_id,
            investigation_id=investigation_id,
            family=None,
            cluster=None,
            family_assurance=AssuranceLevel.UNCONFIRMED,
            sample_sha256s=tuple(sorted({r.sample_sha256 for r in requests if r.sample_sha256})),
            capture_sha256s=tuple(sorted({r.capture.sha256 for r in requests})),
            report_ids=tuple(sorted({r.report_id for r in requests if r.report_id}, key=str)),
            model_version="1.0.0",
            protocol=None,
            transport=next(iter(transports)),
            endpoints=endpoints,
            client_server_orientation=(
                "unresolved: L5 records observed direction but does not prove client/server role"
            ),
            infection_state=None,
            conversations=conversations,
            messages=tuple(messages),
            fields=tuple(fields),
            transitions=(),
            function_provenance=(),
            validation_status=validation,
            verification_findings=(),
            test_plan=(),
            limits=self._workbench.limits,
            limitations=tuple(dict.fromkeys(limitations)),
            extractor_versions=_unique_versions(
                version for trace in observed for version in trace.extractor_versions
            ),
        )
        return ProtocolComparisonOutcomeV1(
            model=model,
            compatible=bool(messages)
            and not any(message.min_length != message.max_length for message in messages),
            stable_field_ids=tuple(dict.fromkeys(stable_ids)),
            variant_field_ids=tuple(dict.fromkeys(variant_ids)),
        )

    async def hypothesize(
        self,
        *,
        subject_id: str,
        investigation_id: UUID,
        proposal: ProtocolHypothesisProposalV1,
        traces: Sequence[ProtocolTraceRequestV1],
    ) -> ProtocolModelV1:
        requests = _bounded_traces(traces, require_holdout=False)
        if any(item.holdout for item in requests):
            raise ProtocolTraceError("hypothesis replay only accepts training traces")
        observed = await self._load_traces(requests, investigation_id=investigation_id)
        return _replay_hypothesis(
            subject_id=subject_id,
            investigation_id=investigation_id,
            proposal=proposal,
            traces=observed,
        )

    async def verify(
        self,
        *,
        model: ProtocolModelV1,
        traces: Sequence[ProtocolTraceRequestV1],
        investigation_id: UUID | None = None,
    ) -> ProtocolModelV1:
        requests = _bounded_traces(traces, require_holdout=True)
        observed = await self._load_traces(
            requests, investigation_id=investigation_id or model.investigation_id
        )
        return _verify_model(model, observed)

    def render(self, model: ProtocolModelV1) -> ProtocolRenderResultV1:
        return render_protocol_model(model)

    async def _load_traces(
        self,
        requests: Sequence[ProtocolTraceRequestV1],
        *,
        investigation_id: UUID,
    ) -> tuple[_ObservedTrace, ...]:
        # The L5 API intentionally returns only compact contracts. For this local
        # validator, use its immutable blob and shared deterministic reconstruction
        # functions to obtain byte ranges; raw bytes never leave this service.
        from cti_app.application.network_workbench import (
            _flows,
            _reassemble_flow,
            _segment_material,
        )

        result: list[_ObservedTrace] = []
        for request in requests:
            _, parsed = await self._workbench._load(
                request.capture, investigation_id=investigation_id
            )
            flow = next(
                (item for item in _flows(parsed.packets) if item.flow_id == request.flow_id), None
            )
            if flow is None:
                raise ProtocolTraceError(f"flow not found: {request.flow_id}")
            materials = _reassemble_flow(flow, request.capture.sha256, self._workbench.limits)
            for material in materials:
                contract = material.contract
                segmentation = _segment_material(material, self._workbench.limits)
                stream_id = _stream_id(contract)
                stream_timestamps = _timestamp_bounds(parsed.packets, contract.frame_numbers)
                stream_evidence = ProtocolEvidenceRefV1(
                    capture_sha256=contract.capture_sha256,
                    flow_id=contract.flow_id,
                    stream_id=stream_id,
                    direction=contract.direction,
                    frame_numbers=contract.frame_numbers,
                    start_offset=contract.stream_start_offset
                    if contract.stream_end_offset > contract.stream_start_offset
                    else None,
                    end_offset=contract.stream_end_offset
                    if contract.stream_end_offset > contract.stream_start_offset
                    else None,
                    source="network.stream.reassemble",
                    timestamp_start_ns=stream_timestamps[0],
                    timestamp_end_ns=stream_timestamps[1],
                )
                source = ProtocolEndpointV1(
                    contract.source.address,
                    contract.source.port,
                    None,
                    (stream_evidence,),
                )
                destination = ProtocolEndpointV1(
                    contract.destination.address,
                    contract.destination.port,
                    None,
                    (stream_evidence,),
                )
                observed_messages: list[_ObservedMessage] = []
                boundaries = segmentation.boundaries
                if (
                    not boundaries
                    and contract.encrypted_opaque
                    and contract.stream_end_offset > contract.stream_start_offset
                ):
                    boundaries = (
                        MessageBoundaryV1(
                            message_index=1,
                            start_offset=contract.stream_start_offset,
                            end_offset=contract.stream_end_offset,
                            frame_numbers=contract.frame_numbers,
                            support_messages=0,
                            status=EvidenceStatus.UNRESOLVED,
                            frame_numbers_truncated=contract.frame_numbers_truncated,
                        ),
                    )
                for boundary in boundaries:
                    start, end = boundary.start_offset, boundary.end_offset
                    message_timestamps = _timestamp_bounds(parsed.packets, boundary.frame_numbers)
                    frame_evidence = ProtocolEvidenceRefV1(
                        capture_sha256=contract.capture_sha256,
                        flow_id=contract.flow_id,
                        stream_id=stream_id,
                        direction=contract.direction,
                        frame_numbers=boundary.frame_numbers,
                        start_offset=start,
                        end_offset=end,
                        source=_SOURCE,
                        timestamp_start_ns=message_timestamps[0],
                        timestamp_end_ns=message_timestamps[1],
                    )
                    complete = (
                        end <= len(material.data)
                        and start >= 0
                        and all(material.coverage[start:end])
                        and not any(
                            item.ambiguous and item.start_offset < end and item.end_offset > start
                            for item in contract.overlaps
                        )
                    )
                    content = (
                        bytes(material.data[start:end])
                        if complete and not contract.encrypted_opaque
                        else None
                    )
                    observed_messages.append(
                        _ObservedMessage(
                            boundary.message_index,
                            boundary,
                            contract,
                            content,
                            frame_evidence,
                        )
                    )
                candidate_evidence = tuple(
                    (
                        candidate,
                        ProtocolEvidenceRefV1(
                            capture_sha256=contract.capture_sha256,
                            flow_id=contract.flow_id,
                            stream_id=stream_id,
                            direction=contract.direction,
                            frame_numbers=candidate.frame_numbers or contract.frame_numbers,
                            start_offset=(
                                candidate.offset
                                if candidate.offset is not None and candidate.length
                                else None
                            ),
                            end_offset=(
                                candidate.offset + candidate.length
                                if candidate.offset is not None and candidate.length
                                else None
                            ),
                            source=_SOURCE,
                        ),
                    )
                    for candidate in segmentation.fields
                )
                result.append(
                    _ObservedTrace(
                        request=request,
                        transport=contract.transport,
                        source=source,
                        destination=destination,
                        direction=contract.direction,
                        orientation_basis=contract.direction_basis,
                        frame_numbers=contract.frame_numbers,
                        messages=tuple(observed_messages),
                        segmentation_fields=candidate_evidence,
                        evidence=stream_evidence,
                        extractor_versions=contract.extractor_versions
                        + segmentation.extractor_versions,
                        limits=contract.limits,
                        limitations=contract.limitations + segmentation.limitations,
                    )
                )
        return tuple(result)

    @staticmethod
    def _conversations(
        traces: Sequence[_ObservedTrace],
        message_ids_by_trace: dict[tuple[str, str, StreamDirection], list[str]],
    ) -> tuple[ProtocolConversationV1, ...]:
        result: list[ProtocolConversationV1] = []
        for trace in traces:
            ids = tuple(
                dict.fromkeys(
                    message_ids_by_trace.get(
                        (
                            trace.request.capture.sha256,
                            trace.request.flow_id,
                            trace.direction,
                        ),
                        [],
                    )
                )
            )
            result.append(
                ProtocolConversationV1(
                    capture_sha256=trace.request.capture.sha256,
                    report_id=trace.request.report_id,
                    flow_id=trace.request.flow_id,
                    direction=trace.direction,
                    source=trace.source,
                    destination=trace.destination,
                    message_ids=ids,
                    frame_numbers=trace.frame_numbers,
                    orientation_basis=trace.orientation_basis,
                    evidence=(trace.evidence,),
                )
            )
        return tuple(result)


def _replay_hypothesis(
    *,
    subject_id: str,
    investigation_id: UUID,
    proposal: ProtocolHypothesisProposalV1,
    traces: Sequence[_ObservedTrace],
) -> ProtocolModelV1:
    if not traces:
        raise HypothesisNotReplayableError("hypothesis has no training trace")
    fields_by_message: dict[str, list[FieldDefinitionV1]] = defaultdict(list)
    messages: list[MessageDefinitionV1] = []
    transitions: list[ProtocolTransitionV1] = []
    message_lookup = {item.message_id: item for item in proposal.messages}
    trace_message_ids: dict[tuple[str, str, StreamDirection], list[str]] = defaultdict(list)
    for hypothesis_message in proposal.messages:
        message_matches = [
            (trace, item)
            for trace in traces
            if trace.direction is hypothesis_message.direction
            for item in trace.messages
            if item.ordinal == hypothesis_message.ordinal
        ]
        if not message_matches:
            raise HypothesisNotReplayableError(
                f"message {hypothesis_message.message_id} has no observed training example"
            )
        for trace, item in message_matches:
            trace_message_ids[
                (trace.request.capture.sha256, trace.request.flow_id, trace.direction)
            ].append(hypothesis_message.message_id)
            if not hypothesis_message.min_length <= item.length <= hypothesis_message.max_length:
                raise HypothesisNotReplayableError(
                    f"message length does not replay for {hypothesis_message.message_id}"
                )
            if item.stream.encrypted_opaque and not hypothesis_message.encrypted_opaque:
                raise HypothesisNotReplayableError(
                    "encrypted_opaque traffic cannot be modeled as decoded message content"
                )
            if not item.stream.encrypted_opaque and item.content is None:
                raise HypothesisNotReplayableError(
                    f"message {hypothesis_message.message_id} crosses an incomplete stream range"
                )
        messages.append(
            MessageDefinitionV1(
                message_id=hypothesis_message.message_id,
                name=hypothesis_message.name,
                ordinal=hypothesis_message.ordinal,
                directions=(hypothesis_message.direction,),
                min_length=hypothesis_message.min_length,
                max_length=hypothesis_message.max_length,
                field_ids=tuple(
                    field.field_id
                    for field in proposal.fields
                    if field.message_id == hypothesis_message.message_id
                ),
                validation_status=(
                    ProtocolValidationStatus.UNRESOLVED
                    if hypothesis_message.encrypted_opaque
                    else ProtocolValidationStatus.INFERRED
                ),
                evidence=_unique_evidence(item.evidence for _, item in message_matches),
                encrypted_opaque=hypothesis_message.encrypted_opaque
                or any(item.stream.encrypted_opaque for _, item in message_matches),
            )
        )

    for hypothesis_field in proposal.fields:
        parent = message_lookup.get(hypothesis_field.message_id)
        if parent is None:
            raise HypothesisNotReplayableError(
                f"field {hypothesis_field.field_id} references a missing message"
            )
        field_matches = [
            (trace, item)
            for trace in traces
            if trace.direction is parent.direction
            for item in trace.messages
            if item.ordinal == parent.ordinal
        ]
        selected: list[tuple[_ObservedTrace, _ObservedMessage, bytes | None]] = []
        for trace, item in field_matches:
            if item.stream.encrypted_opaque or item.content is None:
                if not hypothesis_field.encrypted_opaque:
                    raise HypothesisNotReplayableError(
                        "hypothesis attempts to decode an opaque or incomplete message"
                    )
                selected.append((trace, item, None))
                continue
            start = hypothesis_field.offset
            end = start + hypothesis_field.length
            if end > len(item.content):
                raise HypothesisNotReplayableError(
                    f"field {hypothesis_field.field_id} exceeds an observed message"
                )
            value = item.content[start:end]
            if (
                hypothesis_field.expected_hex is not None
                and value.hex() != hypothesis_field.expected_hex
            ):
                raise HypothesisNotReplayableError(
                    f"field {hypothesis_field.field_id} did not replay on training data"
                )
            selected.append((trace, item, value))
        if not selected:
            raise HypothesisNotReplayableError(
                f"field {hypothesis_field.field_id} has no training observations"
            )
        values = {value for _, _, value in selected if value is not None}
        opaque = hypothesis_field.encrypted_opaque or any(
            item.stream.encrypted_opaque for _, item, _ in selected
        )
        fields_by_message[hypothesis_field.message_id].append(
            FieldDefinitionV1(
                field_id=hypothesis_field.field_id,
                message_id=hypothesis_field.message_id,
                name=hypothesis_field.name,
                offset=None if opaque else hypothesis_field.offset,
                length=None if opaque else hypothesis_field.length,
                endianness=None if opaque else hypothesis_field.endianness,
                encoding=None if opaque else hypothesis_field.encoding,
                condition=hypothesis_field.condition,
                source="network.protocol.hypothesize",
                validation_status=(
                    ProtocolValidationStatus.UNRESOLVED
                    if opaque
                    else ProtocolValidationStatus.INFERRED
                ),
                stability=(FieldStability.VARIANT if len(values) > 1 else FieldStability.STABLE),
                evidence=_unique_evidence(item.evidence for _, item, _ in selected),
                value_sha256s=tuple(sorted(hashlib.sha256(value).hexdigest() for value in values)),
                expected_hex=None if opaque else hypothesis_field.expected_hex,
                encrypted_opaque=opaque,
            )
        )
    for hypothesis_transition in proposal.transitions:
        parent = message_lookup.get(hypothesis_transition.message_id)
        if parent is None:
            raise HypothesisNotReplayableError(
                f"transition {hypothesis_transition.transition_id} references a missing message"
            )
        transition_matches = [
            (trace, item)
            for trace in traces
            if trace.direction is parent.direction
            for item in trace.messages
            if item.ordinal == parent.ordinal
        ]
        if not transition_matches:
            raise HypothesisNotReplayableError(
                f"transition {hypothesis_transition.transition_id} has no observed message"
            )
        transitions.append(
            ProtocolTransitionV1(
                transition_id=hypothesis_transition.transition_id,
                from_state=hypothesis_transition.from_state,
                to_state=hypothesis_transition.to_state,
                message_id=hypothesis_transition.message_id,
                trigger=hypothesis_transition.trigger,
                hypotheses=(f"hypothesis:{hypothesis_transition.transition_id}",),
                validation_status=ProtocolValidationStatus.INFERRED,
                evidence=_unique_evidence(item.evidence for _, item in transition_matches),
            )
        )

    transition_by_message = {item.message_id: item for item in transitions}
    proposal_message_by_key = {
        (item.direction, item.ordinal): item.message_id for item in proposal.messages
    }
    for trace in traces:
        current_state: str | None = None
        for observed in sorted(trace.messages, key=lambda item: item.ordinal):
            message_id = proposal_message_by_key.get((trace.direction, observed.ordinal))
            model_transition = transition_by_message.get(message_id or "")
            if model_transition is None:
                continue
            if current_state is None:
                current_state = model_transition.from_state
            if current_state != model_transition.from_state:
                raise HypothesisNotReplayableError(
                    f"state transition {model_transition.transition_id} does not replay in order"
                )
            current_state = model_transition.to_state

    conversations = NetworkProtocolWorkbench._conversations(traces, trace_message_ids)
    endpoints = _unique_endpoints(
        endpoint for trace in traces for endpoint in (trace.source, trace.destination)
    )
    opaque_seen = any(message.encrypted_opaque for message in messages)
    limitations = [
        "Famille et cluster non confirmés: aucun lien prouvé entre binaire et conversation."
    ]
    if opaque_seen:
        limitations.append("Le trafic encrypted_opaque reste opaque et n'a pas été décodé.")
    return ProtocolModelV1(
        schema_version="ProtocolModelV1",
        subject_id=subject_id,
        investigation_id=investigation_id,
        family=None,
        cluster=None,
        family_assurance=AssuranceLevel.UNCONFIRMED,
        sample_sha256s=tuple(
            sorted({trace.request.sample_sha256 for trace in traces if trace.request.sample_sha256})
        ),
        capture_sha256s=tuple(sorted({trace.request.capture.sha256 for trace in traces})),
        report_ids=tuple(
            sorted(
                {trace.request.report_id for trace in traces if trace.request.report_id}, key=str
            )
        ),
        model_version="1.0.0",
        protocol=None,
        transport=traces[0].transport,
        endpoints=endpoints,
        client_server_orientation="unresolved: observed direction only",
        infection_state=None,
        conversations=conversations,
        messages=tuple(
            replace(
                message,
                field_ids=tuple(field.field_id for field in fields_by_message[message.message_id]),
            )
            for message in messages
        ),
        fields=tuple(field for group in fields_by_message.values() for field in group),
        transitions=tuple(transitions),
        function_provenance=(),
        validation_status=(
            ProtocolValidationStatus.UNRESOLVED
            if opaque_seen
            else ProtocolValidationStatus.INFERRED
        ),
        verification_findings=(),
        test_plan=proposal.test_plan,
        limits=traces[0].limits,
        limitations=tuple(limitations),
        extractor_versions=_unique_versions(
            version for trace in traces for version in trace.extractor_versions
        ),
    )


def _verify_model(model: ProtocolModelV1, traces: Sequence[_ObservedTrace]) -> ProtocolModelV1:
    if not traces:
        raise ProtocolTraceError("verification requires at least one holdout trace")
    message_by_key = {
        (direction, message.ordinal): message
        for message in model.messages
        for direction in message.directions
    }
    fields_by_message: dict[str, list[FieldDefinitionV1]] = defaultdict(list)
    for item in model.fields:
        fields_by_message[item.message_id].append(item)
    new_findings: dict[str, ProtocolVerificationFindingV1] = {}
    message_status: dict[str, ProtocolValidationStatus] = {}
    field_status: dict[str, ProtocolValidationStatus] = {}
    transition_status: dict[str, ProtocolValidationStatus] = {}

    for trace in traces:
        by_ordinal = {item.ordinal: item for item in trace.messages}
        for (direction, ordinal), expected_definition in message_by_key.items():
            if direction is not trace.direction:
                continue
            actual = by_ordinal.get(ordinal)
            if actual is None:
                proof = trace.evidence
                finding = _finding(
                    expected_definition.message_id,
                    "message_missing",
                    ProtocolValidationStatus.UNRESOLVED,
                    expected_definition.name,
                    None,
                    "Expected message was absent from this holdout stream.",
                    (proof,),
                )
                new_findings[finding.finding_id] = finding
                message_status[expected_definition.message_id] = _merge_status(
                    message_status.get(
                        expected_definition.message_id,
                        expected_definition.validation_status,
                    ),
                    ProtocolValidationStatus.UNRESOLVED,
                )
                for expected_field in fields_by_message[expected_definition.message_id]:
                    field_finding = _finding(
                        expected_field.field_id,
                        "field_missing",
                        ProtocolValidationStatus.UNRESOLVED,
                        expected_field.name,
                        None,
                        "The field cannot be checked because its parent message was absent.",
                        (proof,),
                    )
                    new_findings[field_finding.finding_id] = field_finding
                    field_status[expected_field.field_id] = _merge_status(
                        field_status.get(
                            expected_field.field_id,
                            expected_field.validation_status,
                        ),
                        ProtocolValidationStatus.UNRESOLVED,
                    )
                continue
            if (
                not expected_definition.min_length
                <= actual.length
                <= expected_definition.max_length
            ):
                finding = _finding(
                    expected_definition.message_id,
                    "message_length",
                    ProtocolValidationStatus.CONTRADICTED,
                    f"{expected_definition.min_length}..{expected_definition.max_length}",
                    str(actual.length),
                    "Holdout message length falls outside the proposed range.",
                    (actual.evidence,),
                )
                new_findings[finding.finding_id] = finding
                message_status[expected_definition.message_id] = _merge_status(
                    message_status.get(
                        expected_definition.message_id,
                        expected_definition.validation_status,
                    ),
                    ProtocolValidationStatus.CONTRADICTED,
                )
            else:
                message_status[expected_definition.message_id] = _merge_status(
                    message_status.get(
                        expected_definition.message_id,
                        expected_definition.validation_status,
                    ),
                    ProtocolValidationStatus.OBSERVED,
                )
            for field in fields_by_message[expected_definition.message_id]:
                if field.expected_hex is None:
                    continue
                if actual.stream.encrypted_opaque or actual.content is None:
                    finding = _finding(
                        field.field_id,
                        "opaque_field",
                        ProtocolValidationStatus.UNRESOLVED,
                        field.expected_hex,
                        None,
                        "The field cannot be checked because the holdout bytes are opaque "
                        "or incomplete.",
                        (actual.evidence,),
                    )
                    new_findings[finding.finding_id] = finding
                    field_status[field.field_id] = _merge_status(
                        field_status.get(field.field_id, field.validation_status),
                        ProtocolValidationStatus.UNRESOLVED,
                    )
                    continue
                start = field.offset
                end = None if start is None or field.length is None else start + field.length
                actual_hex = (
                    actual.content[start:end].hex()
                    if start is not None and end is not None and end <= len(actual.content)
                    else None
                )
                if actual_hex is None:
                    status = ProtocolValidationStatus.CONTRADICTED
                    detail = "The expected field range exceeds the holdout message."
                elif actual_hex != field.expected_hex:
                    status = ProtocolValidationStatus.CONTRADICTED
                    detail = "The holdout field value contradicts the proposed schema."
                else:
                    field_status[field.field_id] = _merge_status(
                        field_status.get(field.field_id, field.validation_status),
                        ProtocolValidationStatus.OBSERVED,
                    )
                    continue
                finding = _finding(
                    field.field_id,
                    "field_value",
                    status,
                    field.expected_hex,
                    actual_hex[:_MAX_FINDING_TEXT] if actual_hex else None,
                    detail,
                    (_range_evidence(actual, start or 0, end or actual.length),),
                )
                new_findings[finding.finding_id] = finding
                field_status[field.field_id] = _merge_status(
                    field_status.get(field.field_id, field.validation_status), status
                )

        transitions_by_message = {item.message_id: item for item in model.transitions}
        seen_transitions: set[str] = set()
        current_state: str | None = None
        for observed in sorted(trace.messages, key=lambda item: item.ordinal):
            observed_definition = message_by_key.get((trace.direction, observed.ordinal))
            if observed_definition is None:
                continue
            model_transition = transitions_by_message.get(observed_definition.message_id)
            if model_transition is None:
                continue
            seen_transitions.add(model_transition.transition_id)
            if current_state is None:
                current_state = model_transition.from_state
            if current_state != model_transition.from_state:
                finding = _finding(
                    model_transition.transition_id,
                    "state_transition",
                    ProtocolValidationStatus.CONTRADICTED,
                    model_transition.from_state,
                    current_state,
                    "Holdout message order contradicts the proposed state path.",
                    (observed.evidence,),
                )
                new_findings[finding.finding_id] = finding
                transition_status[model_transition.transition_id] = _merge_status(
                    transition_status.get(
                        model_transition.transition_id,
                        model_transition.validation_status,
                    ),
                    ProtocolValidationStatus.CONTRADICTED,
                )
            else:
                transition_status[model_transition.transition_id] = _merge_status(
                    transition_status.get(
                        model_transition.transition_id,
                        model_transition.validation_status,
                    ),
                    ProtocolValidationStatus.OBSERVED,
                )
                current_state = model_transition.to_state
        for transition in model.transitions:
            message_directions = next(
                (
                    item.directions
                    for item in model.messages
                    if item.message_id == transition.message_id
                ),
                (),
            )
            if (
                trace.direction not in message_directions
                or transition.transition_id in seen_transitions
            ):
                continue
            finding = _finding(
                transition.transition_id,
                "transition_missing",
                ProtocolValidationStatus.UNRESOLVED,
                transition.trigger,
                None,
                "Expected state transition was not observed in this holdout stream.",
                (trace.evidence,),
            )
            new_findings[finding.finding_id] = finding
            transition_status[transition.transition_id] = _merge_status(
                transition_status.get(
                    transition.transition_id,
                    transition.validation_status,
                ),
                ProtocolValidationStatus.UNRESOLVED,
            )

    findings_by_id = {item.finding_id: item for item in model.verification_findings}
    findings_by_id.update(new_findings)
    messages = tuple(
        replace(
            item,
            validation_status=_merge_status(
                item.validation_status,
                message_status.get(item.message_id, item.validation_status),
            ),
        )
        for item in model.messages
    )
    fields = tuple(
        replace(
            item,
            validation_status=_merge_status(
                item.validation_status,
                field_status.get(item.field_id, item.validation_status),
            ),
        )
        for item in model.fields
    )
    transitions = tuple(
        replace(
            item,
            validation_status=_merge_status(
                item.validation_status,
                transition_status.get(item.transition_id, item.validation_status),
            ),
        )
        for item in model.transitions
    )
    findings = tuple(findings_by_id[key] for key in sorted(findings_by_id))
    status = model.validation_status
    for finding in findings:
        status = _merge_status(status, finding.validation_status)
    established_observations = any(
        status is ProtocolValidationStatus.OBSERVED
        for status in (
            *message_status.values(),
            *field_status.values(),
            *transition_status.values(),
        )
    )
    if not new_findings and established_observations:
        status = _merge_status(status, ProtocolValidationStatus.OBSERVED)
    return replace(
        model,
        messages=messages,
        fields=fields,
        transitions=transitions,
        verification_findings=findings,
        validation_status=status,
    )


def _render_model(model: ProtocolModelV1) -> ProtocolRenderResultV1:
    sequence_nodes: dict[str, ProtocolDiagramNodeV1] = {}
    sequence_edges: list[ProtocolDiagramEdgeV1] = []
    message_by_id = {item.message_id: item for item in model.messages}
    for conversation in model.conversations:
        source_id = _endpoint_id(conversation.source)
        target_id = _endpoint_id(conversation.destination)
        sequence_nodes.setdefault(
            source_id,
            ProtocolDiagramNodeV1(
                source_id,
                _endpoint_label(conversation.source),
                "observed_endpoint",
                ProtocolValidationStatus.OBSERVED,
                conversation.source.evidence,
            ),
        )
        sequence_nodes.setdefault(
            target_id,
            ProtocolDiagramNodeV1(
                target_id,
                _endpoint_label(conversation.destination),
                "observed_endpoint",
                ProtocolValidationStatus.OBSERVED,
                conversation.destination.evidence,
            ),
        )
        previous_timestamp_ns: int | None = None
        for message_id in conversation.message_ids:
            message = message_by_id.get(message_id)
            if message is None:
                continue
            message_proofs = tuple(
                proof
                for proof in message.evidence
                if proof.capture_sha256 == conversation.capture_sha256
                and proof.flow_id == conversation.flow_id
            )
            observed_timestamp_ns = next(
                (
                    proof.timestamp_start_ns
                    for proof in message_proofs
                    if proof.timestamp_start_ns is not None
                ),
                None,
            )
            timing_ms = (
                max(0, (observed_timestamp_ns - previous_timestamp_ns) // 1_000_000)
                if observed_timestamp_ns is not None and previous_timestamp_ns is not None
                else None
            )
            if observed_timestamp_ns is not None:
                previous_timestamp_ns = observed_timestamp_ns
            sequence_edges.append(
                ProtocolDiagramEdgeV1(
                    source_id=source_id,
                    target_id=target_id,
                    label=message.name,
                    order=message.ordinal,
                    frame_numbers=tuple(
                        sorted(
                            {
                                frame
                                for proof in message.evidence
                                if proof.capture_sha256 == conversation.capture_sha256
                                and proof.flow_id == conversation.flow_id
                                for frame in proof.frame_numbers
                            }
                        )
                    ),
                    timing_ms=timing_ms,
                    validation_status=message.validation_status,
                    evidence=message_proofs or message.evidence,
                )
            )
    sequence_ready = bool(sequence_edges) and all(edge.evidence for edge in sequence_edges)
    sequence = ProtocolDiagramSpecV1(
        schema_version="ProtocolDiagramSpecV1",
        kind=ProtocolDiagramKind.SEQUENCE,
        status=ProtocolDiagramStatus.READY
        if sequence_ready
        else ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE,
        title="Observed message sequence",
        nodes=tuple(sequence_nodes.values()) if sequence_ready else (),
        edges=tuple(sorted(sequence_edges, key=lambda item: (item.order or 0, item.frame_numbers)))
        if sequence_ready
        else (),
        fields=(),
        limitations=()
        if sequence_ready
        else ("No message sequence has sufficient trace evidence.",),
    )

    state_nodes: dict[str, ProtocolDiagramNodeV1] = {}
    state_edges: list[ProtocolDiagramEdgeV1] = []
    for transition in model.transitions:
        if not transition.evidence:
            continue
        state_nodes.setdefault(
            transition.from_state,
            ProtocolDiagramNodeV1(
                transition.from_state,
                transition.from_state,
                "protocol_state",
                transition.validation_status,
                transition.evidence,
            ),
        )
        state_nodes.setdefault(
            transition.to_state,
            ProtocolDiagramNodeV1(
                transition.to_state,
                transition.to_state,
                "protocol_state",
                transition.validation_status,
                transition.evidence,
            ),
        )
        state_edges.append(
            ProtocolDiagramEdgeV1(
                source_id=transition.from_state,
                target_id=transition.to_state,
                label=f"{transition.trigger}: {message_by_id[transition.message_id].name}",
                order=None,
                frame_numbers=tuple(
                    sorted(
                        {frame for proof in transition.evidence for frame in proof.frame_numbers}
                    )
                ),
                timing_ms=None,
                validation_status=transition.validation_status,
                evidence=transition.evidence,
            )
        )
    state_ready = bool(state_edges)
    state_diagram = ProtocolDiagramSpecV1(
        schema_version="ProtocolDiagramSpecV1",
        kind=ProtocolDiagramKind.STATE_MACHINE,
        status=ProtocolDiagramStatus.READY
        if state_ready
        else ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE,
        title="Protocol state transitions",
        nodes=tuple(state_nodes.values()) if state_ready else (),
        edges=tuple(state_edges) if state_ready else (),
        fields=(),
        limitations=() if state_ready else ("No state transition has sufficient trace evidence.",),
    )

    format_fields = tuple(
        ProtocolDiagramFieldV1(
            field_id=item.field_id,
            name="opaque encrypted payload" if item.encrypted_opaque else item.name,
            offset=None if item.encrypted_opaque else item.offset,
            length=None if item.encrypted_opaque else item.length,
            endianness=None if item.encrypted_opaque else item.endianness,
            encoding=None if item.encrypted_opaque else item.encoding,
            condition=item.condition,
            encrypted_opaque=item.encrypted_opaque,
            validation_status=item.validation_status,
            evidence=item.evidence,
        )
        for item in model.fields
        if item.evidence
    )
    format_ready = bool(format_fields)
    format_diagram = ProtocolDiagramSpecV1(
        schema_version="ProtocolDiagramSpecV1",
        kind=ProtocolDiagramKind.MESSAGE_FORMAT,
        status=ProtocolDiagramStatus.READY
        if format_ready
        else ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE,
        title="Observed message field layout",
        nodes=(),
        edges=(),
        fields=format_fields if format_ready else (),
        limitations=() if format_ready else ("No field layout has sufficient trace evidence.",),
    )
    statements: list[str] = []
    for message in model.messages:
        proof = message.evidence[0]
        frames = ",".join(str(frame) for frame in proof.frame_numbers)
        statements.append(
            f"{message.name} is {message.validation_status.value}; capture {proof.capture_sha256}, "
            f"flow {proof.flow_id}, frames {frames or 'not listed'}."
        )
    for finding in model.verification_findings:
        proof = finding.evidence[0]
        statements.append(
            f"{finding.kind} for {finding.item_id} is {finding.validation_status.value} "
            f"in capture {proof.capture_sha256}, frames "
            f"{','.join(str(frame) for frame in proof.frame_numbers) or 'not listed'}."
        )
    if any(item.encrypted_opaque for item in model.messages):
        statements.append("Encrypted opaque message content remains undecoded.")
    return ProtocolRenderResultV1(
        schema_version="ProtocolRenderResultV1",
        model_sha256=model.canonical_sha256,
        diagrams=(sequence, state_diagram, format_diagram),
        factual_statements=tuple(statements),
    )


def render_protocol_model(model: ProtocolModelV1) -> ProtocolRenderResultV1:
    """Project a stored semantic model into its three evidence-bound diagram specs."""
    if not isinstance(model, ProtocolModelV1):
        raise TypeError("Protocol rendering requires ProtocolModelV1")
    return _render_model(model)


def _bounded_traces(
    traces: Sequence[ProtocolTraceRequestV1], *, require_holdout: bool
) -> tuple[ProtocolTraceRequestV1, ...]:
    items = tuple(traces)
    if not items or len(items) > _MAX_TRACE_COUNT:
        raise ProtocolTraceError(f"trace count must be between 1 and {_MAX_TRACE_COUNT}")
    if require_holdout and any(not item.holdout for item in items):
        raise ProtocolTraceError("verification accepts holdout traces only")
    if not require_holdout and any(item.holdout for item in items):
        raise ProtocolTraceError("this operation does not accept holdout traces")
    return items


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _message_id(direction: StreamDirection, ordinal: int) -> str:
    return f"message_{direction.value}_{ordinal}"


def _field_id(
    message_id: str, name: str, offset: int | None, length: int | None, endian: str | None
) -> str:
    token = hashlib.sha256(f"{message_id}|{name}|{offset}|{length}|{endian}".encode()).hexdigest()[
        :12
    ]
    return f"field_{token}"


def _stream_id(stream: ReassembledStreamV1) -> str:
    return f"{stream.capture_sha256}:{stream.flow_id}:{stream.direction.value}"


def _timestamp_bounds(
    packets: Sequence[CapturedPacket], frame_numbers: Sequence[int]
) -> tuple[int | None, int | None]:
    frame_set = set(frame_numbers)
    timestamps = [
        packet.timestamp_ns
        for packet in packets
        if packet.frame_number in frame_set and packet.timestamp_ns is not None
    ]
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


def _status_from_boundary(items: Sequence[_ObservedMessage]) -> ProtocolValidationStatus:
    if all(item.boundary.status is EvidenceStatus.OBSERVED for item in items):
        return ProtocolValidationStatus.OBSERVED
    return ProtocolValidationStatus.INFERRED


def _candidate_status(value: object) -> ProtocolValidationStatus:
    if value is EvidenceStatus.OBSERVED:
        return ProtocolValidationStatus.OBSERVED
    if value is EvidenceStatus.INFERRED:
        return ProtocolValidationStatus.INFERRED
    return ProtocolValidationStatus.UNRESOLVED


def _unique_evidence(
    evidence: Iterable[ProtocolEvidenceRefV1],
) -> tuple[ProtocolEvidenceRefV1, ...]:
    by_key: dict[tuple[str, str, str, int | None, int | None], ProtocolEvidenceRefV1] = {}
    for item in evidence:
        key = (
            item.capture_sha256,
            item.stream_id,
            item.source,
            item.start_offset,
            item.end_offset,
        )
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
        else:
            by_key[key] = replace(
                existing,
                frame_numbers=tuple(sorted(set(existing.frame_numbers + item.frame_numbers))),
            )
    return tuple(
        by_key[key]
        for key in sorted(
            by_key,
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                -1 if item[3] is None else item[3],
                -1 if item[4] is None else item[4],
            ),
        )
    )


def _relative_field_offset(
    raw_offset: int | None,
    items: Sequence[tuple[object, _ObservedMessage]],
    ordinal: int,
) -> int | None:
    if raw_offset is None:
        return None
    starts = [item.boundary.start_offset for _, item in items if item.ordinal == ordinal]
    first = min(starts) if starts else 0
    return raw_offset - first if raw_offset >= first else raw_offset


def _candidate_value_digests(
    items: Sequence[tuple[object, _ObservedMessage]],
    relative_offset: int | None,
    length: int | None,
) -> tuple[tuple[str, ...], int]:
    if relative_offset is None or length is None:
        return (), 0
    values: set[str] = set()
    observations: set[tuple[str, str, str, int, int]] = set()
    for _, message in items:
        if message.content is not None and relative_offset + length <= len(message.content):
            observations.add(_message_observation_key(message))
            values.add(
                hashlib.sha256(
                    message.content[relative_offset : relative_offset + length]
                ).hexdigest()
            )
    return tuple(sorted(values)), len(observations)


def _message_observation_key(
    message: _ObservedMessage,
) -> tuple[str, str, str, int, int]:
    evidence = message.evidence
    return (
        evidence.capture_sha256,
        evidence.flow_id,
        evidence.stream_id,
        message.boundary.start_offset,
        message.boundary.end_offset,
    )


def _byte_runs(
    observations: Sequence[tuple[_ObservedMessage, bytes | None]],
) -> tuple[tuple[int, int, bool], ...]:
    lengths = {len(value) for _, value in observations if value is not None}
    if len(lengths) != 1:
        return ()
    length = next(iter(lengths))
    runs: list[tuple[int, int, bool]] = []
    start = 0
    current: bool | None = None
    for offset in range(length):
        values = {value[offset] for _, value in observations if value is not None}
        stable = len(values) <= 1
        if current is None:
            current = stable
            start = offset
        elif stable != current:
            runs.append((start, offset, current))
            if len(runs) > _MAX_BYTE_RUNS_PER_MESSAGE:
                raise ProtocolTraceError("byte comparison exceeds the field-run limit")
            start = offset
            current = stable
    if current is not None:
        runs.append((start, length, current))
    if len(runs) > _MAX_BYTE_RUNS_PER_MESSAGE:
        raise ProtocolTraceError("byte comparison exceeds the field-run limit")
    return tuple((start, end, stable) for start, end, stable in runs if end > start)


def _range_evidence(message: _ObservedMessage, start: int, end: int) -> ProtocolEvidenceRefV1:
    stream_start = message.boundary.start_offset
    relative_start = max(0, start)
    relative_end = min(message.length, end)
    return replace(
        message.evidence,
        start_offset=stream_start + relative_start,
        end_offset=stream_start + max(relative_start + 1, relative_end),
    )


def _unique_endpoints(
    endpoints: Iterable[ProtocolEndpointV1],
) -> tuple[ProtocolEndpointV1, ...]:
    by_key: dict[tuple[str, int | None, str | None], ProtocolEndpointV1] = {}
    for endpoint in endpoints:
        key = (endpoint.address, endpoint.port, endpoint.channel)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = endpoint
        else:
            by_key[key] = replace(
                existing,
                evidence=_unique_evidence(existing.evidence + endpoint.evidence),
            )
    return tuple(
        by_key[key]
        for key in sorted(by_key, key=lambda item: (item[0], item[1] or -1, item[2] or ""))
    )


def _unique_versions(
    versions: Iterable[ExtractorVersionV1],
) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({(item.name, item.version) for item in versions}))


def _candidate_placeholder() -> None:
    """Keep a named seam for mypy when L5 adds new field candidate kinds."""


def _finding(
    item_id: str,
    kind: str,
    status: ProtocolValidationStatus,
    expected: str | None,
    observed: str | None,
    detail: str,
    evidence: tuple[ProtocolEvidenceRefV1, ...],
) -> ProtocolVerificationFindingV1:
    identity = hashlib.sha256(
        "|".join(
            (
                item_id,
                kind,
                status.value,
                expected or "",
                observed or "",
                evidence[0].capture_sha256,
                evidence[0].flow_id,
                ",".join(map(str, evidence[0].frame_numbers)),
            )
        ).encode("utf-8")
    ).hexdigest()[:20]
    return ProtocolVerificationFindingV1(
        finding_id=f"verification_{identity}",
        item_id=item_id,
        kind=kind,
        validation_status=status,
        expected=expected,
        observed=observed,
        detail=detail,
        evidence=evidence,
    )


def _merge_status(
    current: ProtocolValidationStatus, new: ProtocolValidationStatus
) -> ProtocolValidationStatus:
    if (
        current is ProtocolValidationStatus.CONTRADICTED
        or new is ProtocolValidationStatus.CONTRADICTED
    ):
        return ProtocolValidationStatus.CONTRADICTED
    if current is ProtocolValidationStatus.UNRESOLVED or new is ProtocolValidationStatus.UNRESOLVED:
        return ProtocolValidationStatus.UNRESOLVED
    if current is ProtocolValidationStatus.OBSERVED or new is ProtocolValidationStatus.OBSERVED:
        return ProtocolValidationStatus.OBSERVED
    return ProtocolValidationStatus.INFERRED


def _endpoint_id(endpoint: ProtocolEndpointV1) -> str:
    return hashlib.sha256(f"{endpoint.address}:{endpoint.port}".encode()).hexdigest()[:12]


def _endpoint_label(endpoint: ProtocolEndpointV1) -> str:
    return endpoint.address if endpoint.port is None else f"{endpoint.address}:{endpoint.port}"


def _trace_views(traces: Sequence[_ObservedTrace]) -> tuple[_ObservedTrace, ...]:
    return tuple(
        trace for trace in traces if any(item.stream.encrypted_opaque for item in trace.messages)
    )
