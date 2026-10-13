from __future__ import annotations

import os
import shutil

import pytest

from cti_app.application.protocol_diagram_projection import (
    project_protocol_diagram,
    protocol_diagram_pin_sha256,
)
from cti_app.domain.protocol_model import (
    ProtocolDiagramEdgeV1,
    ProtocolDiagramFieldV1,
    ProtocolDiagramKind,
    ProtocolDiagramNodeV1,
    ProtocolDiagramSpecV1,
    ProtocolDiagramStatus,
    ProtocolValidationStatus,
)
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler, encode_d2_source


def _node(
    node_id: str,
    label: str,
    status: ProtocolValidationStatus = ProtocolValidationStatus.OBSERVED,
) -> ProtocolDiagramNodeV1:
    return ProtocolDiagramNodeV1(
        node_id=node_id,
        label=label,
        role="participant",
        validation_status=status,
    )


def _edge(
    source_id: str,
    target_id: str,
    label: str,
    order: int,
    frames: tuple[int, ...],
    status: ProtocolValidationStatus = ProtocolValidationStatus.OBSERVED,
    timing_ms: int | None = None,
) -> ProtocolDiagramEdgeV1:
    return ProtocolDiagramEdgeV1(
        source_id=source_id,
        target_id=target_id,
        label=label,
        order=order,
        frame_numbers=frames,
        timing_ms=timing_ms,
        validation_status=status,
        evidence=(),
    )


def _field(
    field_id: str,
    name: str,
    offset: int | None,
    length: int | None,
    *,
    status: ProtocolValidationStatus = ProtocolValidationStatus.OBSERVED,
    encrypted_opaque: bool = False,
    endianness: str | None = None,
) -> ProtocolDiagramFieldV1:
    return ProtocolDiagramFieldV1(
        field_id=field_id,
        name=name,
        offset=offset,
        length=length,
        endianness=endianness,
        encoding=None,
        condition=None,
        encrypted_opaque=encrypted_opaque,
        validation_status=status,
        evidence=(),
    )


def _spec(
    kind: ProtocolDiagramKind,
    *,
    status: ProtocolDiagramStatus = ProtocolDiagramStatus.READY,
    nodes: tuple[ProtocolDiagramNodeV1, ...] = (),
    edges: tuple[ProtocolDiagramEdgeV1, ...] = (),
    fields: tuple[ProtocolDiagramFieldV1, ...] = (),
) -> ProtocolDiagramSpecV1:
    return ProtocolDiagramSpecV1(
        schema_version="ProtocolDiagramSpecV1",
        kind=kind,
        status=status,
        title=f"Synthetic {kind.value} protocol view",
        nodes=nodes,
        edges=edges,
        fields=fields,
        limitations=("Synthetic test evidence only",),
    )


def _three_views() -> tuple[ProtocolDiagramSpecV1, ...]:
    return (
        _spec(
            ProtocolDiagramKind.SEQUENCE,
            nodes=(
                _node("client", "Client"),
                _node("dns", "DNS resolver"),
                _node("c2", "Endpoint / C2"),
                _node("payload", "Payload endpoint", ProtocolValidationStatus.INFERRED),
            ),
            edges=(
                _edge("client", "c2", "beacon", 2, (12,), timing_ms=400),
                _edge("client", "dns", "query A", 1, (9,)),
                _edge(
                    "c2",
                    "payload",
                    "payload reference",
                    3,
                    (14,),
                    ProtocolValidationStatus.INFERRED,
                ),
            ),
        ),
        _spec(
            ProtocolDiagramKind.STATE_MACHINE,
            nodes=(
                _node("connected", "Connected"),
                _node("beacon", "Beacon observed"),
                _node(
                    "command", "Command execution unresolved", ProtocolValidationStatus.UNRESOLVED
                ),
                _node("old", "Old transition contradicted", ProtocolValidationStatus.CONTRADICTED),
            ),
            edges=(
                _edge("connected", "beacon", "periodic request", 1, (20,)),
                _edge(
                    "beacon",
                    "command",
                    "command received",
                    2,
                    (),
                    ProtocolValidationStatus.INFERRED,
                ),
                _edge(
                    "command",
                    "old",
                    "old command sequence",
                    3,
                    (28,),
                    ProtocolValidationStatus.CONTRADICTED,
                ),
            ),
        ),
        _spec(
            ProtocolDiagramKind.MESSAGE_FORMAT,
            fields=(
                _field("length", "Frame length", 0, 2, endianness="little"),
                _field("opcode", "Opcode", 2, 1),
                _field(
                    "payload",
                    "sensitive plaintext must not appear",
                    3,
                    None,
                    status=ProtocolValidationStatus.UNRESOLVED,
                    encrypted_opaque=True,
                ),
            ),
        ),
    )


def test_protocol_projection_builds_three_distinct_views_and_keeps_statuses() -> None:
    sequence, states, message = (project_protocol_diagram(item) for item in _three_views())

    sequence_source = encode_d2_source(sequence).decode("utf-8")
    state_source = encode_d2_source(states).decode("utf-8")
    message_source = encode_d2_source(message).decode("utf-8")

    assert "shape: sequence_diagram" in sequence_source
    assert sequence_source.index("order 1") < sequence_source.index("order 2")
    assert "frames 9" in sequence_source
    assert "timing 400 ms" in sequence_source
    assert "INFERRED" in sequence_source
    assert 'stroke: "#B54708"' in sequence_source
    assert 'fill: "#FFF3E0"' in sequence_source
    assert all(
        status in state_source for status in ("OBSERVED", "INFERRED", "UNRESOLVED", "CONTRADICTED")
    )
    assert "little" in message_source
    assert "Opaque" in message_source
    assert "encrypted" in message_source
    assert "payload" in message_source
    assert "sensitive plaintext must not appear" not in message_source
    assert "bytes omitted" in message_source


def test_protocol_projection_escapes_model_text_before_d2_encoding() -> None:
    hostile = 'Client"\nclient -> attacker: ${payload} \\ end'
    spec = _spec(
        ProtocolDiagramKind.SEQUENCE,
        nodes=(_node("client", hostile), _node("dns", "DNS")),
        edges=(_edge("client", "dns", 'query "\n${inject}', 1, (5,)),),
    )

    source = encode_d2_source(project_protocol_diagram(spec)).decode("utf-8")

    assert 'Client\\"\\nclient -> attacker: \\$' in source
    assert 'query \\"\\n\\${inject}' in source
    assert "\nclient -> attacker:" not in source
    assert "\n${inject}" not in source


def test_insufficient_evidence_renders_only_an_explicit_gap_card() -> None:
    spec = _spec(
        ProtocolDiagramKind.SEQUENCE,
        status=ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE,
    )

    diagram = project_protocol_diagram(spec)
    source = encode_d2_source(diagram).decode("utf-8")

    assert len(diagram.nodes) == 1
    assert diagram.edges == ()
    assert "INSUFFICIENT EVIDENCE" in source
    assert "No protocol scenario established" in source
    assert "beacon" not in source.casefold()
    assert "generic sequence" in (diagram.caption or "")


def test_protocol_pin_hash_covers_semantics_and_compiler_identity() -> None:
    spec = _three_views()[0]
    pin = protocol_diagram_pin_sha256(spec, compiler="d2", compiler_version="0.9.0")

    assert pin == protocol_diagram_pin_sha256(spec, compiler="d2", compiler_version="0.9.0")
    assert pin != protocol_diagram_pin_sha256(spec, compiler="d2", compiler_version="0.9.1")
    changed = _spec(
        ProtocolDiagramKind.SEQUENCE,
        nodes=spec.nodes,
        edges=(spec.edges[1], spec.edges[0], spec.edges[2]),
    )
    assert pin != protocol_diagram_pin_sha256(changed, compiler="d2", compiler_version="0.9.0")


_D2_BINARY = os.environ.get("D2_BINARY") or shutil.which("d2")


@pytest.mark.asyncio
@pytest.mark.skipif(_D2_BINARY is None, reason="D2 compiler capability is unavailable")
async def test_real_d2_compiles_each_protocol_view_to_svg_and_png() -> None:
    compiler = D2DiagramCompiler(binary=_D2_BINARY or "d2")

    for spec in _three_views():
        diagram = project_protocol_diagram(spec)
        svg = await compiler.compile(diagram)
        preview = await compiler.compile_preview(diagram)
        assert svg.media_type == "image/svg+xml"
        assert svg.media_bytes.lstrip().startswith(b"<svg") or b"<svg" in svg.media_bytes[:256]
        assert preview.media_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert svg.source_sha256 == preview.source_sha256


@pytest.mark.asyncio
@pytest.mark.skipif(_D2_BINARY is None, reason="D2 compiler capability is unavailable")
async def test_real_d2_renders_insufficient_evidence_without_a_scenario() -> None:
    compiler = D2DiagramCompiler(binary=_D2_BINARY or "d2")
    diagram = project_protocol_diagram(
        _spec(
            ProtocolDiagramKind.STATE_MACHINE,
            status=ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE,
        )
    )

    result = await compiler.compile(diagram)

    assert b"INSUFFICIENT EVIDENCE" in result.source_bytes
    assert b"No protocol scenario" in result.source_bytes
    assert b"established" in result.source_bytes
