from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest

from cti_app.application.diagram_compilation import (
    DIAGRAM_COMPILATION_POLICY_VERSION,
    DiagramCompilerOutputInvalidError,
    DiagramCompilerOutputTooLargeError,
    DiagramCompilerProcessError,
    DiagramCompilerTimeoutError,
    DiagramCompilerUnavailableError,
    DiagramCompilerVersionError,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeRole,
    DiagramNodeV1,
    DiagramRelationType,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.infrastructure.d2_diagram_compiler import (
    D2_COMPILER_VERSION,
    AsyncioD2ProcessRunner,
    D2DiagramCompiler,
    D2ProcessResult,
    D2ProcessStatus,
    diagram_semantic_sha256,
    encode_d2_source,
    validate_d2_svg,
)

_EVIDENCE = ExtractionEvidenceRefV1(
    source_document_id=UUID("00000000-0000-0000-0000-000000000001"),
    kind=EvidenceKind.FACT,
    evidence_key="a" * 64,
)


def _node(
    node_id: str,
    label: str | None = None,
    role: DiagramNodeRole = DiagramNodeRole.UNKNOWN,
) -> DiagramNodeV1:
    return DiagramNodeV1(
        node_id=node_id, label=label or node_id, evidence_refs=(_EVIDENCE,), role=role
    )


def _edge(source: str, target: str, label: str | None = None) -> DiagramEdgeV1:
    return DiagramEdgeV1(
        source_node_id=source,
        target_node_id=target,
        label=label,
        evidence_refs=(_EVIDENCE,),
    )


def _diagram(**overrides: object) -> DiagramSpecV1:
    fields: dict[str, object] = {
        "key": "diagram-main",
        "kind": EnrichmentDiagramKind.CUSTOM,
        "title": "Ignored title",
        "caption": "Ignored caption",
        "direction": EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        "nodes": (_node("source"), _node("target")),
        "edges": (_edge("source", "target", "connects"),),
        "groups": (),
        "placement": EnrichmentPlacementV1(EnrichmentPlacementKind.END),
    }
    fields.update(overrides)
    return DiagramSpecV1(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("direction", "d2_direction"),
    (
        (EnrichmentDiagramDirection.LEFT_TO_RIGHT, "right"),
        (EnrichmentDiagramDirection.TOP_TO_BOTTOM, "down"),
    ),
)
def test_encodes_each_direction_and_ends_with_one_lf(
    direction: EnrichmentDiagramDirection, d2_direction: str
) -> None:
    source = encode_d2_source(_diagram(direction=direction))

    assert source.startswith(f"direction: {d2_direction}\n".encode())
    assert source.endswith(b"\n")
    assert not source.endswith(b"\n\n")


def test_preserves_tuple_order_and_qualifies_grouped_nodes() -> None:
    diagram = _diagram(
        nodes=(_node("third"), _node("first"), _node("second")),
        groups=(
            DiagramGroupV1("user-group-z", "Group Z", ("second",)),
            DiagramGroupV1("user-group-a", "Group A", ("third",)),
        ),
        edges=(
            _edge("third", "first", "first edge"),
            _edge("first", "second"),
            _edge("second", "third", "third edge"),
        ),
    )

    source = encode_d2_source(diagram).decode()
    assert source.startswith("direction: right\n")
    assert source.index('g001: "Group Z"') < source.index('g002: "Group A"')
    assert source.index('g002.n001: "third"') < source.index('n002: "first"')
    assert source.index('n002: "first"') < source.index('g001.n003: "second"')
    assert source.index('g002.n001 -> n002: "first edge"') < source.index("n002 -> g001.n003")
    assert source.index("n002 -> g001.n003") < source.index('g001.n003 -> g002.n001: "third edge"')


def test_encodes_two_directed_edges_in_tuple_order() -> None:
    diagram = _diagram(
        edges=(_edge("source", "target", "forward"), _edge("target", "source", "return")),
    )

    source = encode_d2_source(diagram).decode()
    assert source.index('n001 -> n002: "forward"') < source.index('n002 -> n001: "return"')


def test_escapes_hostile_labels_as_double_quoted_content() -> None:
    node_label = "Unicode café 雪; quotes \" and ' backslash \\\nnext\t{ } : # -> <- | ${D2_VAR}"
    group_label = '@import("https://example.test") ![icon](image.png)'
    edge_label = '<- $CONFIG; | -> ; link: https://example.test "end"'
    diagram = _diagram(
        nodes=(_node("model -> n001", node_label), _node("target")),
        edges=(_edge("model -> n001", "target", edge_label),),
        groups=(DiagramGroupV1("model-group", group_label, ("model -> n001",)),),
    )

    source = encode_d2_source(diagram).decode()
    assert source.startswith("direction: down\n")
    assert '@import(\\"https://example.test\\")' in source
    assert "\\" + "$" + "{D2_VAR}" in source
    assert "\\" + "$CONFIG" in source
    assert "shape: rectangle" in source


def test_omits_only_none_edge_labels() -> None:
    diagram = _diagram(edges=(_edge("source", "target"),))

    source = encode_d2_source(diagram).decode()
    assert "n001 -> n002" in source
    assert 'n001 -> n002: ""' not in source


@pytest.mark.parametrize(
    ("relation_type", "encoded_label"),
    (
        (DiagramRelationType.INFERENCE, '"may connect"'),
        (DiagramRelationType.COMPARISON, '"may connect"'),
    ),
)
def test_d2_labels_inference_and_comparison_without_changing_the_graph_authority(
    relation_type: DiagramRelationType, encoded_label: str
) -> None:
    edge = replace(
        _edge("source", "target", "may connect"),
        relation_type=relation_type,
    )
    source = encode_d2_source(_diagram(edges=(edge,))).decode()

    assert encoded_label in source
    if relation_type is DiagramRelationType.COMPARISON:
        assert 'n001 <-> n002: "may connect" {' in source
    else:
        assert 'n001 -> n002: "may connect" {' in source
    assert "stroke-dash: 5" in source


def test_comparison_relation_changes_rendering_identity() -> None:
    factual = _diagram()
    comparison = replace(
        factual,
        edges=(
            replace(
                factual.edges[0],
                relation_type=DiagramRelationType.COMPARISON,
                label="comparison of observations",
            ),
        ),
    )

    assert diagram_semantic_sha256(comparison) != diagram_semantic_sha256(factual)


def test_node_role_selects_deterministic_print_safe_shape_and_colour() -> None:
    diagram = _diagram(
        nodes=(
            _node("actor", "Threat actor", DiagramNodeRole.ACTOR),
            _node("wallet", "Bitcoin wallet", DiagramNodeRole.DATA_ARTIFACT),
        ),
        edges=(_edge("actor", "wallet"),),
    )

    source = encode_d2_source(diagram).decode()
    assert "shape: person" in source
    assert "shape: cylinder" in source
    assert 'fill: "#FBE3E6"' in source
    assert 'fill: "#DFF3E1"' in source
    assert "direction: right" in source
    assert diagram_semantic_sha256(
        replace(
            diagram,
            nodes=(replace(diagram.nodes[0], role=DiagramNodeRole.VICTIM), diagram.nodes[1]),
        )
    ) != diagram_semantic_sha256(diagram)


@pytest.mark.parametrize(
    ("node_count", "expected_direction"),
    ((3, "right"), (4, "down")),
)
def test_node_count_selects_vertical_layout_after_three_nodes(
    node_count: int, expected_direction: str
) -> None:
    nodes = tuple(_node(f"node-{index}") for index in range(node_count))
    edges = tuple(_edge(f"node-{index}", f"node-{index + 1}") for index in range(node_count - 1))

    source = encode_d2_source(
        _diagram(
            direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
            nodes=nodes,
            edges=edges,
        )
    ).decode()

    assert source.startswith(f"direction: {expected_direction}\n")


@pytest.mark.parametrize(
    ("label_length", "expected_direction"),
    ((30, "right"), (31, "down")),
)
def test_label_length_selects_vertical_layout_after_thirty_characters(
    label_length: int, expected_direction: str
) -> None:
    diagram = _diagram(
        nodes=(_node("source", "S"), _node("target", "x" * label_length)),
    )

    source = encode_d2_source(diagram).decode()

    assert source.startswith(f"direction: {expected_direction}\n")


@pytest.mark.parametrize("label_kind", ("relation", "group"))
def test_long_relation_and_group_labels_force_vertical_layout(label_kind: str) -> None:
    label = "x" * 31
    overrides: dict[str, object] = {}
    if label_kind == "relation":
        overrides["edges"] = (_edge("source", "target", label),)
    else:
        overrides["groups"] = (DiagramGroupV1("long-label", label, ("source",)),)

    source = encode_d2_source(_diagram(**overrides)).decode()

    assert source.startswith("direction: down\n")


def test_encoding_and_semantic_hash_are_stable_and_key_sensitive() -> None:
    diagram = _diagram()

    assert encode_d2_source(diagram) == encode_d2_source(diagram)
    assert diagram_semantic_sha256(diagram) == diagram_semantic_sha256(diagram)
    assert diagram_semantic_sha256(diagram) != diagram_semantic_sha256(
        replace(diagram, key="diagram-other")
    )


def test_d2_tool_lock_matches_compiler_version() -> None:
    lock_file = Path(__file__).parents[2] / "infra" / "d2.lock"
    match = re.search(r"^D2_VERSION=([0-9.]+)$", lock_file.read_text(), re.MULTILINE)

    assert match is not None
    assert match.group(1) == D2_COMPILER_VERSION


def test_diagram_compilation_policy_version_was_incremented() -> None:
    assert DIAGRAM_COMPILATION_POLICY_VERSION == "diagram-d2-svg-v4-role-styles-print-layout"


@dataclass(frozen=True, slots=True)
class _RecordedRun:
    argv: tuple[str, ...]
    stdin: bytes
    environment: dict[str, str]
    timeout_seconds: float
    stdout_limit: int
    stderr_limit: int


class _ControlledRunner:
    def __init__(self, *results: D2ProcessResult) -> None:
        self._results = list(results)
        self.calls: list[_RecordedRun] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        environment: Mapping[str, str],
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> D2ProcessResult:
        self.calls.append(
            _RecordedRun(
                tuple(argv), stdin, dict(environment), timeout_seconds, stdout_limit, stderr_limit
            )
        )
        assert self._results, "unexpected D2 process invocation"
        return self._results.pop(0)


def _version_result(reported: bytes = b"0.9.0\n") -> D2ProcessResult:
    return D2ProcessResult(D2ProcessStatus.SUCCEEDED, 0, reported, b"")


def _svg_result(stdout: bytes = b"<svg></svg>\n") -> D2ProcessResult:
    return D2ProcessResult(D2ProcessStatus.SUCCEEDED, 0, stdout, b"")


def _assert_minimal_environment(environment: Mapping[str, str]) -> None:
    assert sorted(environment) == ["LANG", "LC_ALL", "PATH"]
    assert environment["LANG"] == environment["LC_ALL"] == "C.UTF-8"
    assert environment["PATH"] == os.environ.get("PATH", os.defpath)


def _test_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


@pytest.mark.parametrize(
    "svg_bytes",
    (
        b"",
        b"<svg>",
        b"<html />",
        b"<svg><script /></svg>",
        b'<svg xmlns="urn:root"><x:script xmlns:x="urn:child" /></svg>',
        b'<svg xmlns="urn:root"><x:foreignObject xmlns:x="urn:child" /></svg>',
        b'<svg><image href="http://example.invalid/image.png" /></svg>',
        b'<svg><image href="https://example.invalid/image.png" /></svg>',
        b'<svg><image href="file:///tmp/image.png" /></svg>',
        b'<svg><image href="images/image.png" /></svg>',
        b'<svg><image href="/tmp/image.png" /></svg>',
        b'<svg><image src="data:image/png;base64,AAAA" /></svg>',
        b'<svg><rect style="fill: url(https://example.invalid/p.svg#x)" /></svg>',
        b'<svg><rect fill="url(#x)" filter="url(../filter.svg#x)" /></svg>',
        b"<svg><style>.shape { fill: url(file:///tmp/paint.svg#x) }</style></svg>",
        b"<svg><style>.s { fill: url(data:image/svg+xml;base64,AAAA) }</style></svg>",
        b'<svg><style>@import "https://example.invalid/x.css";</style></svg>',
        b"<svg><style>.s { fill: url/**/(https://example.invalid/x) }</style></svg>",
        b"<svg><style>.s { fill: url(https://example.invalid/x }</style></svg>",
        b"<svg><style>.s { fill: \\75rl(https://example.invalid/x) }</style></svg>",
        b'<svg><image href="data:font/woff;base64,AAAA" /></svg>',
        b'<!DOCTYPE svg [<!ENTITY label "unsafe">]><svg>&label;</svg>',
    ),
)
def test_validate_d2_svg_rejects_invalid_or_external_output(svg_bytes: bytes) -> None:
    with pytest.raises(DiagramCompilerOutputInvalidError):
        validate_d2_svg(svg_bytes)


def test_validate_d2_svg_accepts_fragment_references_and_url_like_text() -> None:
    svg_bytes = (
        b'<svg xmlns="urn:svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<use xlink:href="#local-id" />'
        b"<rect style=\"fill: url('#local-id')\" />"
        b"<style>@font-face { font-family: f; "
        b'src: url("data:application/font-woff;base64,d09GRgABAAA=") }</style>'
        b"<text>https://example.invalid file: /tmp/image.svg #local-id</text>"
        b"</svg>"
    )

    validate_d2_svg(svg_bytes)


def test_validate_d2_svg_rejects_output_over_two_mib() -> None:
    with pytest.raises(DiagramCompilerOutputTooLargeError):
        validate_d2_svg(b" " * (2 * 1024 * 1024 + 1))


async def test_compile_pins_version_render_argv_environment_and_limits() -> None:
    runner = _ControlledRunner(_version_result(), _svg_result())
    diagram = _diagram()

    compiled = await D2DiagramCompiler(runner=runner).compile(diagram)

    assert len(runner.calls) == 2
    version_call, render_call = runner.calls
    assert version_call.argv == ("d2", "--version")
    assert version_call.stdin == b""
    assert render_call.argv == (
        "d2",
        "--layout=dagre",
        "--timeout=10",
        "--omit-version",
        f"--salt={hashlib.sha256(encode_d2_source(diagram)).hexdigest()}",
        "--stdout-format=svg",
        "-",
        "-",
    )
    assert render_call.stdin == encode_d2_source(diagram)
    for call in (version_call, render_call):
        assert call.timeout_seconds <= 10
        assert call.stdout_limit == 2 * 1024 * 1024
        assert call.stderr_limit == 64 * 1024
        _assert_minimal_environment(call.environment)

    assert compiled.diagram_key == "diagram-main"
    assert compiled.source_format == "d2"
    assert compiled.source_bytes == encode_d2_source(diagram)
    assert compiled.source_sha256 == hashlib.sha256(encode_d2_source(diagram)).hexdigest()
    assert compiled.media_type == "image/svg+xml"
    assert compiled.media_bytes == b"<svg></svg>\n"
    assert compiled.media_sha256 == hashlib.sha256(b"<svg></svg>\n").hexdigest()
    assert compiled.compiler == "d2"
    assert compiled.compiler_version == "0.9.0"
    assert compiled.compiler_policy_version == DIAGRAM_COMPILATION_POLICY_VERSION


async def test_compile_is_deterministic_for_identical_diagrams() -> None:
    runner = _ControlledRunner(
        _version_result(),
        _svg_result(),
        _svg_result(),
    )
    compiler = D2DiagramCompiler(runner=runner)
    diagram = _diagram()

    first = await compiler.compile(diagram)
    second = await compiler.compile(diagram)

    assert first.source_bytes == second.source_bytes
    assert first.source_sha256 == second.source_sha256
    assert len(runner.calls) == 3
    assert runner.calls[0].argv == ("d2", "--version")
    assert runner.calls[1].argv == runner.calls[2].argv
    assert runner.calls[1].argv[4] == f"--salt={first.source_sha256}"
    assert first.media_bytes == second.media_bytes
    assert first.media_sha256 == second.media_sha256


async def test_compile_uses_configured_binary_for_version_and_render() -> None:
    runner = _ControlledRunner(_version_result(), _svg_result())

    await D2DiagramCompiler(runner=runner, binary="/opt/d2").compile(_diagram())

    assert runner.calls[0].argv == ("/opt/d2", "--version")
    assert runner.calls[1].argv[0] == "/opt/d2"


async def test_version_cache_is_process_wide_and_binary_specific() -> None:
    runner = _ControlledRunner(
        _version_result(),
        _svg_result(),
        _svg_result(),
        _version_result(),
        _svg_result(),
    )

    await D2DiagramCompiler(runner=runner).compile(_diagram())
    await D2DiagramCompiler(runner=runner).compile(_diagram())
    await D2DiagramCompiler(runner=runner, binary="/opt/other-d2").compile(_diagram())

    assert [call.argv for call in runner.calls if call.argv[-1] == "--version"] == [
        ("d2", "--version"),
        ("/opt/other-d2", "--version"),
    ]
    assert len(runner.calls) == 5


async def test_compile_rejects_invalid_svg_after_successful_process() -> None:
    runner = _ControlledRunner(
        _version_result(), _svg_result(b'<svg><image href="/tmp/x" /></svg>')
    )

    with pytest.raises(DiagramCompilerOutputInvalidError):
        await D2DiagramCompiler(runner=runner).compile(_diagram())


async def test_compile_ignores_d2_and_home_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("D2_LAYOUT", "elk")
    monkeypatch.setenv("D2_SALT", "attacker")
    monkeypatch.setenv("D2_THEME", "0")
    monkeypatch.setenv("HOME", "/home/attacker")
    runner = _ControlledRunner(_version_result(), _svg_result())

    await D2DiagramCompiler(runner=runner).compile(_diagram())

    assert len(runner.calls) == 2
    for call in runner.calls:
        _assert_minimal_environment(call.environment)
        assert not any(key.startswith("D2_") for key in call.environment)
        assert "HOME" not in call.environment


@pytest.mark.parametrize("reported", (b"0.9.0", b"0.9.0\n", b"v0.9.0\n"))
async def test_version_probe_accepts_optional_v_prefix(reported: bytes) -> None:
    runner = _ControlledRunner(_version_result(reported), _svg_result())

    compiled = await D2DiagramCompiler(runner=runner).compile(_diagram())

    assert compiled.compiler_version == "0.9.0"
    assert len(runner.calls) == 2


@pytest.mark.parametrize(
    "reported", (b"", b"0.8.0", b"v0.9.1", b"0.9.0\n0.9.0\n", b"d2 version 0.9.0")
)
async def test_version_probe_rejects_any_other_version(reported: bytes) -> None:
    runner = _ControlledRunner(_version_result(reported))

    with pytest.raises(DiagramCompilerVersionError):
        await D2DiagramCompiler(runner=runner).compile(_diagram())

    assert len(runner.calls) == 1


async def test_missing_binary_maps_to_compiler_unavailable() -> None:
    runner = _ControlledRunner(D2ProcessResult(D2ProcessStatus.FAILED_TO_START, None, b"", b""))

    with pytest.raises(DiagramCompilerUnavailableError):
        await D2DiagramCompiler(runner=runner).compile(_diagram())

    assert len(runner.calls) == 1


async def test_compilation_timeout_maps_to_typed_timeout() -> None:
    runner = _ControlledRunner(
        _version_result(),
        D2ProcessResult(D2ProcessStatus.TIMED_OUT, -9, b"<svg>partial", b"still running"),
    )

    with pytest.raises(DiagramCompilerTimeoutError) as error:
        await D2DiagramCompiler(runner=runner).compile(_diagram())

    assert "still running" in str(error.value)


async def test_non_zero_exit_never_yields_partial_stdout() -> None:
    runner = _ControlledRunner(
        _version_result(),
        D2ProcessResult(D2ProcessStatus.NON_ZERO_EXIT, 2, b"<svg>partial</svg>", b"render failed"),
    )

    with pytest.raises(DiagramCompilerProcessError) as error:
        await D2DiagramCompiler(runner=runner).compile(_diagram())

    assert "render failed" in str(error.value)


async def test_stdout_overflow_maps_to_output_too_large() -> None:
    runner = _ControlledRunner(
        _version_result(),
        D2ProcessResult(D2ProcessStatus.STDOUT_LIMIT, -9, b"x" * 16, b""),
    )

    with pytest.raises(DiagramCompilerOutputTooLargeError):
        await D2DiagramCompiler(runner=runner).compile(_diagram())


async def test_salt_uses_canonical_d2_source_not_semantic_metadata() -> None:
    runner = _ControlledRunner(_version_result(), _svg_result(), _svg_result())
    compiler = D2DiagramCompiler(runner=runner)
    first = _diagram()
    second = replace(first, key="diagram-other", title="Changed legend")

    await compiler.compile(first)
    await compiler.compile(second)

    first_salt, second_salt = runner.calls[1].argv[4], runner.calls[2].argv[4]
    expected_salt = f"--salt={hashlib.sha256(encode_d2_source(first)).hexdigest()}"
    assert encode_d2_source(first) == encode_d2_source(second)
    assert first_salt == second_salt == expected_salt


class _FakeStdin:
    def __init__(self) -> None:
        self.received = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.received.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


async def test_runner_creation_uses_direct_exec_with_pipes_and_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _CapturedProcess:
        returncode = 0

        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()

        def kill(self) -> None:
            raise AssertionError("a healthy process must not be killed")

        async def wait(self) -> int:
            return 0

    process = _CapturedProcess()
    process.stdout.feed_eof()
    process.stderr.feed_eof()

    async def _create_subprocess_exec(*argv: str, **kwargs: object) -> _CapturedProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)

    result = await AsyncioD2ProcessRunner().run(
        ("d2", "--version"),
        stdin=b"",
        environment=_test_environment(),
        timeout_seconds=1,
        stdout_limit=128,
        stderr_limit=64,
    )

    assert result.status is D2ProcessStatus.SUCCEEDED
    assert result.exit_code == 0
    assert captured["argv"] == ("d2", "--version")
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert set(kwargs) == {"stdin", "stdout", "stderr", "env"}
    assert kwargs["stdin"] is asyncio.subprocess.PIPE
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
    assert kwargs["env"] == _test_environment()
    assert process.stdin.received == b""
    assert process.stdin.closed is True


async def test_runner_streams_stdin_and_truncates_stderr() -> None:
    script = (
        "import sys; data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data[::-1]); "
        "sys.stderr.write('e' * 4096)"
    )

    result = await AsyncioD2ProcessRunner().run(
        (sys.executable, "-c", script),
        stdin=b"payload",
        environment=_test_environment(),
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=64,
    )

    assert result.status is D2ProcessStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.stdout == b"daolyap"
    assert len(result.stderr) == 64


async def test_runner_terminates_process_on_stdout_overflow() -> None:
    script = "import sys; sys.stdout.write('x' * (2 * 1024 * 1024))"

    result = await AsyncioD2ProcessRunner().run(
        (sys.executable, "-c", script),
        stdin=b"",
        environment=_test_environment(),
        timeout_seconds=10,
        stdout_limit=4096,
        stderr_limit=64,
    )

    assert result.status is D2ProcessStatus.STDOUT_LIMIT
    assert len(result.stdout) == 4096
    assert result.exit_code is not None


async def test_runner_terminates_process_on_timeout() -> None:
    script = "import sys, time; sys.stdin.buffer.read(); time.sleep(30)"

    result = await AsyncioD2ProcessRunner().run(
        (sys.executable, "-c", script),
        stdin=b"input",
        environment=_test_environment(),
        timeout_seconds=0.25,
        stdout_limit=1024,
        stderr_limit=64,
    )

    assert result.status is D2ProcessStatus.TIMED_OUT
    assert result.exit_code is not None


async def test_runner_reports_non_zero_exit_and_missing_executable() -> None:
    failed = await AsyncioD2ProcessRunner().run(
        (sys.executable, "-c", "raise SystemExit(3)"),
        stdin=b"",
        environment=_test_environment(),
        timeout_seconds=10,
        stdout_limit=64,
        stderr_limit=64,
    )
    assert failed.status is D2ProcessStatus.NON_ZERO_EXIT
    assert failed.exit_code == 3

    missing = await AsyncioD2ProcessRunner().run(
        ("d2-binary-that-does-not-exist", "--version"),
        stdin=b"",
        environment=_test_environment(),
        timeout_seconds=1,
        stdout_limit=64,
        stderr_limit=64,
    )
    assert missing.status is D2ProcessStatus.FAILED_TO_START
    assert missing.exit_code is None
