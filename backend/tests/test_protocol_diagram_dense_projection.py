from __future__ import annotations

import re

from cti_app.application.protocol_diagram_projection import (
    PROTOCOL_DIAGRAM_PROJECTION_VERSION,
    project_protocol_diagram,
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
from cti_app.infrastructure.d2_diagram_compiler import encode_d2_source

_MAX_LABEL_CHARS = 44
_UUID_TEXT = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HASH_TEXT = re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)


def _node(node_id: str, label: str) -> ProtocolDiagramNodeV1:
    return ProtocolDiagramNodeV1(
        node_id=node_id,
        label=label,
        role="participant",
        validation_status=ProtocolValidationStatus.OBSERVED,
    )


def _edge(source_id: str, target_id: str, label: str, order: int) -> ProtocolDiagramEdgeV1:
    return ProtocolDiagramEdgeV1(
        source_id=source_id,
        target_id=target_id,
        label=label,
        order=order,
        frame_numbers=(),
        timing_ms=None,
        validation_status=ProtocolValidationStatus.OBSERVED,
        evidence=(),
    )


def _field(field_id: str, name: str, offset: int) -> ProtocolDiagramFieldV1:
    return ProtocolDiagramFieldV1(
        field_id=field_id,
        name=name,
        offset=offset,
        length=4,
        endianness=None,
        encoding=None,
        condition=None,
        encrypted_opaque=False,
        validation_status=ProtocolValidationStatus.OBSERVED,
        evidence=(),
    )


def _spec(
    kind: ProtocolDiagramKind,
    *,
    nodes: tuple[ProtocolDiagramNodeV1, ...] = (),
    edges: tuple[ProtocolDiagramEdgeV1, ...] = (),
    fields: tuple[ProtocolDiagramFieldV1, ...] = (),
) -> ProtocolDiagramSpecV1:
    return ProtocolDiagramSpecV1(
        schema_version="ProtocolDiagramSpecV1",
        kind=kind,
        status=ProtocolDiagramStatus.READY,
        title=f"Dense {kind.value} fixture",
        nodes=nodes,
        edges=edges,
        fields=fields,
        limitations=("Synthetic test fixture",),
    )


def _sequence_spec(*, long_labels: bool) -> ProtocolDiagramSpecV1:
    nodes = (_node("client", "Client"), _node("server", "Server"))
    labels = (
        "[PING] keepalive"
        if not long_labels
        else "Session established without transfer (heartbeats every 30 seconds) "
        "while the client waits for the server response and retains the connection; "
        "[PING] following heartbeat "
        "123e4567-e89b-12d3-a456-426614174000 "
        "abcdef0123456789abcdef0123456789"
    )
    edges = tuple(
        _edge(
            "client" if index % 2 else "server",
            "server" if index % 2 else "client",
            labels,
            index,
        )
        for index in range(1, 18)
    )
    return _spec(ProtocolDiagramKind.SEQUENCE, nodes=nodes, edges=edges)


def _state_machine_spec() -> ProtocolDiagramSpecV1:
    node_labels = tuple(f"State {index:02d}" for index in range(1, 10))
    nodes = tuple(_node(f"state-{index:02d}", label) for index, label in enumerate(node_labels, 1))
    transitions = (
        (1, 2),
        (2, 3),
        (3, 4),
        (4, 5),
        (5, 6),
        (6, 7),
        (7, 8),
        (8, 9),
        (9, 1),
        (1, 3),
        (2, 4),
        (3, 5),
        (4, 6),
        (5, 7),
        (6, 8),
        (7, 9),
        (8, 1),
    )
    edges = tuple(
        _edge(
            f"state-{source:02d}",
            f"state-{target:02d}",
            (
                f"[MSG{index:02d}] transition from the authenticated state to the next "
                "protocol state after validating the session metadata"
            ),
            index,
        )
        for index, (source, target) in enumerate(transitions, 1)
    )
    return _spec(ProtocolDiagramKind.STATE_MACHINE, nodes=nodes, edges=edges)


def _message_format_spec() -> ProtocolDiagramSpecV1:
    fields = tuple(
        _field(
            f"field-{index:02d}",
            (
                f"Capability bitmap for negotiated protocol extensions and "
                f"compatibility metadata, field {index:02d} "
                "123e4567-e89b-12d3-a456-426614174000 "
                "abcdef0123456789abcdef0123456789"
            ),
            (index - 1) * 4,
        )
        for index in range(1, 23)
    )
    return _spec(ProtocolDiagramKind.MESSAGE_FORMAT, fields=fields)


def _all_labels(diagram) -> tuple[str, ...]:
    return tuple(node.label for node in diagram.nodes) + tuple(
        edge.label or "" for edge in diagram.edges
    )


def _assert_safe_compact_labels(diagram) -> None:
    labels = _all_labels(diagram)
    assert all(len(label) <= _MAX_LABEL_CHARS for label in labels)
    assert all(_UUID_TEXT.search(label) is None for label in labels)
    assert all(_HASH_TEXT.search(label) is None for label in labels)
    assert len({node.node_id for node in diagram.nodes}) == len(diagram.nodes)


def test_dense_sequence_keeps_all_messages_as_short_numbered_arrows() -> None:
    short_spec = _sequence_spec(long_labels=False)
    short_diagram = project_protocol_diagram(short_spec)
    long_diagram = project_protocol_diagram(_sequence_spec(long_labels=True))

    assert len(short_diagram.edges) == len(short_spec.edges) == 17
    assert len(long_diagram.edges) == 17
    assert len(short_diagram.nodes) == len(short_spec.nodes) == 2
    assert all(edge.label and edge.label.startswith("#") for edge in short_diagram.edges)
    _assert_safe_compact_labels(short_diagram)
    _assert_safe_compact_labels(long_diagram)
    assert all("[PING]" in (edge.label or "") for edge in long_diagram.edges)
    assert short_diagram == project_protocol_diagram(short_spec)
    assert encode_d2_source(short_diagram) == encode_d2_source(project_protocol_diagram(short_spec))
    short_source = encode_d2_source(short_diagram).decode()
    assert "shape: sequence_diagram" in short_source
    assert short_source.count(" -> ") == 17


def test_dense_state_machine_keeps_seventeen_transitions_between_nine_states() -> None:
    spec = _state_machine_spec()

    diagram = project_protocol_diagram(spec)

    assert len(diagram.nodes) == 9
    assert len(diagram.edges) == len(spec.edges) == 17
    assert all(edge.label and edge.label.startswith("[MSG") for edge in diagram.edges)
    assert all(len(node.label) <= 24 for node in diagram.nodes)
    assert all(len(edge.label or "") <= 24 for edge in diagram.edges)
    _assert_safe_compact_labels(diagram)
    assert diagram == project_protocol_diagram(spec)
    assert encode_d2_source(diagram) == encode_d2_source(project_protocol_diagram(spec))
    assert encode_d2_source(diagram).decode().count(" -> ") == 17


def test_oversized_state_machine_keeps_the_transition_card_fallback() -> None:
    spec = _state_machine_spec()
    extra_edges = tuple(
        _edge(
            f"state-{((index - 1) % 9) + 1:02d}",
            f"state-{(index % 9) + 1:02d}",
            f"[MSG{index:02d}] overflow transition",
            index,
        )
        for index in range(18, 26)
    )
    oversized = _spec(
        ProtocolDiagramKind.STATE_MACHINE,
        nodes=spec.nodes,
        edges=(*spec.edges, *extra_edges),
    )

    diagram = project_protocol_diagram(oversized)

    assert len(diagram.nodes) == 25
    assert diagram.edges == ()


def test_dense_message_format_compacts_twenty_two_fields_for_a_two_column_grid() -> None:
    spec = _message_format_spec()

    diagram = project_protocol_diagram(spec)
    source = encode_d2_source(diagram).decode()

    assert len(diagram.nodes) == len(spec.fields) == 22
    assert diagram.edges == ()
    assert "grid-columns: 2" in source
    assert all(
        node.label.startswith(f"#{index:02d} offset ")
        for index, node in enumerate(diagram.nodes, 1)
    )
    _assert_safe_compact_labels(diagram)
    assert diagram == project_protocol_diagram(spec)
    assert source == encode_d2_source(project_protocol_diagram(spec)).decode()


def test_dense_projection_version_is_pinned_to_the_new_layout() -> None:
    assert (
        PROTOCOL_DIAGRAM_PROJECTION_VERSION
        == "protocol-diagram-projection-v9-m21-documented-states"
    )
