"""Deterministic D2 source encoding and bounded D2 0.9.0 subprocess execution."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from xml.etree import ElementTree

from cti_app.application.diagram_compilation import (
    DIAGRAM_COMPILATION_POLICY_VERSION,
    CompiledDiagram,
    DiagramCompilerOutputInvalidError,
    DiagramCompilerOutputTooLargeError,
    DiagramCompilerProcessError,
    DiagramCompilerTimeoutError,
    DiagramCompilerUnavailableError,
    DiagramCompilerVersionError,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramNodeRole,
    DiagramRelationType,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    diagram_requires_vertical_layout,
)

D2_COMPILER = "d2"
D2_SOURCE_FORMAT = "d2"
D2_SOURCE_ENCODING = "utf-8"
D2_MEDIA_TYPE = "image/svg+xml"
D2_LAYOUT = "dagre"
D2_COMPILER_VERSION = "0.9.0"
D2_COMPILATION_TIMEOUT_SECONDS = 10
D2_MAX_STDOUT_BYTES = 2 * 1024 * 1024
D2_MAX_STDERR_BYTES = 64 * 1024
D2_PROCESS_LOCALE = "C.UTF-8"
D2_EXIT_POLL_SECONDS = 0.01
D2_EXIT_REAP_TIMEOUT_SECONDS = 5.0
_VERSION_CACHE_LOCK = threading.Lock()
_VERSION_CACHE_BINARY: str | None = None
_VERSION_CACHE_RUNNER: D2ProcessRunner | None = None
_VERSION_CACHE_FUTURE: concurrent.futures.Future[None] | None = None
_CSS_URL = re.compile(
    r"""url\s*\(\s*(?:(?P<quote>["'])(?P<quoted>(?:\\.|(?!(?P=quote)).)*)(?P=quote)|(?P<bare>[^)\s"'(]*))\s*\)""",
    re.IGNORECASE | re.DOTALL,
)
_CSS_URL_OPENING = re.compile(r"url\s*\(", re.IGNORECASE)
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)
# D2 embeds its fonts as base64 data URIs in the SVG stylesheet; nothing else may be inlined.
_EMBEDDED_FONT_PREFIXES = (
    "data:application/font-woff;base64,",
    "data:font/woff;base64,",
    "data:font/woff2;base64,",
)
_CSS_PRESENTATION_ATTRIBUTES = frozenset(
    {
        "clip-path",
        "color-profile",
        "cursor",
        "fill",
        "filter",
        "marker",
        "marker-end",
        "marker-mid",
        "marker-start",
        "mask",
        "shape-inside",
        "shape-subtract",
        "stroke",
    }
)

_D2_DIRECTION_BY_V1 = {
    EnrichmentDiagramDirection.LEFT_TO_RIGHT: "right",
    EnrichmentDiagramDirection.TOP_TO_BOTTOM: "down",
}
# One print-safe hue per role (light fill, dark stroke) so a reader tells actors, tooling,
# infrastructure and data apart at a glance; the shape is a second, colour-blind-safe cue.
_NODE_STYLE_BY_ROLE = {
    DiagramNodeRole.ACTOR: ("person", "#FBE3E6", "#B3243B"),
    DiagramNodeRole.VICTIM: ("rectangle", "#FFF1C9", "#9A6B00"),
    DiagramNodeRole.MALWARE_TOOL: ("hexagon", "#FFE4D1", "#C2410C"),
    DiagramNodeRole.INFRASTRUCTURE: ("cloud", "#DCEAF9", "#1D4E89"),
    DiagramNodeRole.DATA_ARTIFACT: ("cylinder", "#DFF3E1", "#2E7D32"),
    DiagramNodeRole.TECHNIQUE_STEP: ("rectangle", "#EAE3F6", "#5B3E96"),
    DiagramNodeRole.UNKNOWN: ("rectangle", "#F1F1EF", "#667085"),
}
_EDGE_COLOR_BY_RELATION = {
    DiagramRelationType.FACTUAL: "#2F3B3C",
    DiagramRelationType.INFERENCE: "#5B3E96",
    DiagramRelationType.COMPARISON: "#667085",
}
_NODE_FONT_SIZE = 30
_EDGE_FONT_SIZE = 24
# D2 double-quoted strings decode these escapes; ``$`` must be escaped so that
# ``${...}`` in canonical content is never resolved as a D2 variable.
_LABEL_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "$": "\\$",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _ref_projection(ref: Any) -> dict[str, str]:
    return {
        "source_document_id": str(ref.source_document_id),
        "kind": ref.kind.value,
        "evidence_key": ref.evidence_key,
    }


def _semantic_projection(diagram: DiagramSpecV1) -> dict[str, Any]:
    return {
        "key": diagram.key,
        "kind": diagram.kind.value,
        "title": diagram.title,
        "caption": diagram.caption,
        "direction": diagram.direction.value,
        "nodes": [
            {
                "node_id": node.node_id,
                "label": node.label,
                "role": node.role.value,
                "evidence_refs": [_ref_projection(ref) for ref in node.evidence_refs],
            }
            for node in diagram.nodes
        ],
        "edges": [
            {
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "label": edge.label,
                "relation_type": edge.relation_type.value,
                "evidence_refs": [_ref_projection(ref) for ref in edge.evidence_refs],
            }
            for edge in diagram.edges
        ],
        "groups": [
            {"group_id": group.group_id, "label": group.label, "node_ids": list(group.node_ids)}
            for group in diagram.groups
        ],
        "placement": {
            "kind": diagram.placement.kind.value,
            "section_index": diagram.placement.section_index,
        },
    }


def diagram_semantic_sha256(diagram: DiagramSpecV1) -> str:
    """Hash every canonical diagram field as deterministic JSON."""
    canonical_json = json.dumps(
        _semantic_projection(diagram), sort_keys=True, separators=(",", ":")
    ).encode(D2_SOURCE_ENCODING)
    return hashlib.sha256(canonical_json).hexdigest()


def _escape_d2_label(value: str) -> str:
    """Quote canonical text so D2 renders it verbatim and never parses it as syntax."""
    return '"' + "".join(_LABEL_ESCAPES.get(char, char) for char in value) + '"'


def _node_references(diagram: DiagramSpecV1) -> dict[str, str]:
    """Map canonical node IDs to synthetic D2 paths; group invariants hold in the domain."""
    memberships = {
        node_id: f"g{group_index:03d}"
        for group_index, group in enumerate(diagram.groups, 1)
        for node_id in group.node_ids
    }
    references: dict[str, str] = {}
    for node_index, node in enumerate(diagram.nodes, 1):
        node_reference = f"n{node_index:03d}"
        group_reference = memberships.get(node.node_id)
        references[node.node_id] = (
            f"{group_reference}.{node_reference}" if group_reference else node_reference
        )
    return references


def encode_d2_source(diagram: DiagramSpecV1) -> bytes:
    """Encode the canonical graph using only synthetic D2 identifiers."""
    node_references = _node_references(diagram)
    # Keep graphs vertical when node count or long labels make horizontal layout
    # hard to read. Otherwise preserve the model's direction.
    graph_labels = [node.label for node in diagram.nodes]
    graph_labels.extend(edge.label or "" for edge in diagram.edges)
    graph_labels.extend(group.label for group in diagram.groups)
    force_vertical = diagram_requires_vertical_layout(len(diagram.nodes), graph_labels)
    direction = "down" if force_vertical else _D2_DIRECTION_BY_V1[diagram.direction]
    lines = [f"direction: {direction}"]

    for group_index, group in enumerate(diagram.groups, 1):
        lines.extend(
            (
                f"g{group_index:03d}: {_escape_d2_label(_wrap_d2_label(group.label))} {{",
                "  style: {",
                '    fill: "#E8F0F0"',
                '    stroke: "#93AFB0"',
                "    stroke-width: 1",
                "  }",
                "}",
            )
        )

    for node in diagram.nodes:
        shape, fill, stroke = _NODE_STYLE_BY_ROLE[node.role]
        lines.extend(
            (
                f"{node_references[node.node_id]}: "
                f"{_escape_d2_label(_wrap_d2_label(node.label))} {{",
                f"  shape: {shape}",
                "  style: {",
                f'    fill: "{fill}"',
                f'    stroke: "{stroke}"',
                '    font-color: "#17202A"',
                "    stroke-width: 2",
                "    border-radius: 8",
                f"    font-size: {_NODE_FONT_SIZE}",
                "  }",
                "}",
            )
        )

    for edge in diagram.edges:
        connector = "<->" if edge.relation_type is DiagramRelationType.COMPARISON else "->"
        line = (
            f"{node_references[edge.source_node_id]} {connector} "
            f"{node_references[edge.target_node_id]}"
        )
        label = _wrap_d2_label(edge.label or "")
        if label:
            line += f": {_escape_d2_label(label)}"
        edge_lines = [
            line + " {",
            "  style: {",
            f'    stroke: "{_EDGE_COLOR_BY_RELATION[edge.relation_type]}"',
            "    stroke-width: 2",
            f"    font-size: {_EDGE_FONT_SIZE}",
        ]
        if edge.relation_type is not DiagramRelationType.FACTUAL:
            edge_lines.append("    stroke-dash: 5")
        edge_lines.extend(("  }", "}"))
        lines.extend(edge_lines)

    return ("\n".join(lines) + "\n").encode(D2_SOURCE_ENCODING)


def _wrap_d2_label(value: str, width: int = 24) -> str:
    """Wrap labels between words while leaving long technical literals intact."""
    words = value.split()
    if not words:
        return value
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


class D2ProcessStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_TO_START = "FAILED_TO_START"
    TIMED_OUT = "TIMED_OUT"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"
    STDOUT_LIMIT = "STDOUT_LIMIT"


@dataclass(frozen=True, slots=True)
class D2ProcessResult:
    status: D2ProcessStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes


class D2ProcessRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        environment: Mapping[str, str],
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> D2ProcessResult: ...


def d2_process_environment() -> dict[str, str]:
    """Return the minimal environment: PATH plus fixed locale values only."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": D2_PROCESS_LOCALE,
        "LC_ALL": D2_PROCESS_LOCALE,
    }


class AsyncioD2ProcessRunner:
    """Shell-free direct execution with bounded, concurrent stream capture."""

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
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(environment),
            )
        except OSError:
            return D2ProcessResult(D2ProcessStatus.FAILED_TO_START, None, b"", b"")

        stdin_stream = process.stdin
        stdout_stream = process.stdout
        stderr_stream = process.stderr
        assert stdin_stream is not None
        assert stdout_stream is not None
        assert stderr_stream is not None

        exit_code: int | None = None
        stdout_bytes = b""
        stderr_bytes = b""

        def close_transport() -> None:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()

        def settle(result: D2ProcessResult) -> D2ProcessResult:
            close_transport()
            return result

        def task_bytes(task: asyncio.Task[tuple[bytes, bool]]) -> bytes:
            if not task.done() or task.cancelled() or task.exception() is not None:
                return b""
            return task.result()[0]

        async def write_stdin() -> None:
            try:
                stdin_stream.write(stdin)
                await stdin_stream.drain()
            except OSError:
                pass
            finally:
                stdin_stream.close()

        async def read_stream(
            stream: asyncio.StreamReader, *, limit: int, stop_on_overflow: bool
        ) -> tuple[bytes, bool]:
            buffer = bytearray()
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return bytes(buffer), False
                remaining = limit - len(buffer)
                if len(chunk) > remaining:
                    buffer.extend(chunk[:remaining])
                    if stop_on_overflow:
                        return bytes(buffer), True
                else:
                    buffer.extend(chunk)

        async def drain_stream(stream: asyncio.StreamReader) -> None:
            while await stream.read(64 * 1024):
                pass

        async def wait_for_exit(reap_timeout_seconds: float | None = None) -> int | None:
            """Poll for the child exit so delivery never depends on watcher wakeups."""
            loop = asyncio.get_running_loop()
            deadline = None if reap_timeout_seconds is None else loop.time() + reap_timeout_seconds
            while process.returncode is None:
                if deadline is not None and loop.time() >= deadline:
                    break
                await asyncio.sleep(D2_EXIT_POLL_SECONDS)
            return process.returncode

        async def kill_and_wait(*tasks: asyncio.Future[Any]) -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                drain_stream(stdout_stream),
                drain_stream(stderr_stream),
                return_exceptions=True,
            )
            await wait_for_exit(D2_EXIT_REAP_TIMEOUT_SECONDS)

        async def collect() -> D2ProcessStatus:
            nonlocal exit_code, stdout_bytes, stderr_bytes
            stdin_task = asyncio.create_task(write_stdin())
            stdout_task = asyncio.create_task(
                read_stream(stdout_stream, limit=stdout_limit, stop_on_overflow=True)
            )
            stderr_task = asyncio.create_task(
                read_stream(stderr_stream, limit=stderr_limit, stop_on_overflow=False)
            )
            stream_tasks = {stdout_task, stderr_task}
            try:
                while stream_tasks:
                    done, stream_tasks = await asyncio.wait(
                        stream_tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    if any(task.result()[1] for task in done):
                        await kill_and_wait(stdin_task, stdout_task, stderr_task)
                        exit_code = process.returncode
                        stdout_bytes = stdout_task.result()[0]
                        stderr_bytes = stderr_task.result()[0]
                        return D2ProcessStatus.STDOUT_LIMIT
                exit_code = await wait_for_exit()
                stdout_bytes = stdout_task.result()[0]
                stderr_bytes = stderr_task.result()[0]
                if exit_code == 0:
                    return D2ProcessStatus.SUCCEEDED
                return D2ProcessStatus.NON_ZERO_EXIT
            except asyncio.CancelledError:
                await kill_and_wait(stdin_task, stdout_task, stderr_task)
                exit_code = process.returncode
                stdout_bytes = task_bytes(stdout_task)
                stderr_bytes = task_bytes(stderr_task)
                raise

        try:
            async with asyncio.timeout(timeout_seconds):
                status = await collect()
        except TimeoutError:
            return settle(
                D2ProcessResult(D2ProcessStatus.TIMED_OUT, exit_code, stdout_bytes, stderr_bytes)
            )
        return settle(D2ProcessResult(status, exit_code, stdout_bytes, stderr_bytes))


def _stderr_note(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    return f": {text[:512]}"


def _raise_process_failure(result: D2ProcessResult) -> None:
    if result.status is D2ProcessStatus.SUCCEEDED:
        return
    note = _stderr_note(result.stderr)
    if result.status is D2ProcessStatus.FAILED_TO_START:
        raise DiagramCompilerUnavailableError(f"d2 executable could not be started{note}")
    if result.status is D2ProcessStatus.TIMED_OUT:
        raise DiagramCompilerTimeoutError(
            f"d2 exceeded the {D2_COMPILATION_TIMEOUT_SECONDS} second budget{note}"
        )
    if result.status is D2ProcessStatus.STDOUT_LIMIT:
        raise DiagramCompilerOutputTooLargeError(
            f"d2 stdout exceeded {D2_MAX_STDOUT_BYTES} bytes and was terminated{note}"
        )
    raise DiagramCompilerProcessError(f"d2 exited with code {result.exit_code}{note}")


def _css_url_targets(css: str) -> Sequence[str]:
    """Return every CSS url() target, rejecting constructs that could hide one."""
    if "\\" in css:
        raise DiagramCompilerOutputInvalidError("D2 SVG contains an unsupported CSS escape")
    css = _CSS_COMMENT.sub(" ", css)
    if "@import" in css.lower():
        raise DiagramCompilerOutputInvalidError("D2 SVG contains a CSS @import")
    targets: list[str] = []
    for opening in _CSS_URL_OPENING.finditer(css):
        match = _CSS_URL.match(css, opening.start())
        if match is None:
            raise DiagramCompilerOutputInvalidError(
                "D2 SVG contains an invalid CSS url() reference"
            )
        targets.append((match.group("quoted") or match.group("bare") or "").strip())
    return targets


class _SvgTreeBuilder(ElementTree.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise DiagramCompilerOutputInvalidError("D2 SVG must not contain a document type")


def _xml_local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _validate_resource_target(target: str, *, allow_embedded_font: bool = False) -> None:
    target = target.strip()
    if target.startswith("#"):
        return
    if allow_embedded_font and target.lower().startswith(_EMBEDDED_FONT_PREFIXES):
        return
    raise DiagramCompilerOutputInvalidError("D2 SVG contains a non-fragment resource reference")


def validate_d2_svg(svg_bytes: bytes) -> None:
    """Reject invalid, unsafe, or unbounded D2 SVG output."""
    if not svg_bytes:
        raise DiagramCompilerOutputInvalidError("D2 SVG output is empty")
    if len(svg_bytes) > D2_MAX_STDOUT_BYTES:
        raise DiagramCompilerOutputTooLargeError(
            f"D2 SVG output exceeded {D2_MAX_STDOUT_BYTES} bytes"
        )
    try:
        root = ElementTree.fromstring(
            svg_bytes, parser=ElementTree.XMLParser(target=_SvgTreeBuilder())
        )
    except ElementTree.ParseError as exc:
        raise DiagramCompilerOutputInvalidError("D2 SVG output is not valid XML") from exc
    if _xml_local_name(root.tag) != "svg":
        raise DiagramCompilerOutputInvalidError("D2 SVG root element is not svg")

    for element in root.iter():
        name = _xml_local_name(element.tag)
        if name in {"script", "foreignObject"}:
            raise DiagramCompilerOutputInvalidError(f"D2 SVG contains a forbidden {name} element")
        for attribute, value in element.attrib.items():
            attribute_name = _xml_local_name(attribute)
            is_xml_base = attribute == "{http://www.w3.org/XML/1998/namespace}base"
            if attribute_name in {"href", "src"} or is_xml_base:
                _validate_resource_target(value)
            if attribute_name == "style" or attribute_name.lower() in _CSS_PRESENTATION_ATTRIBUTES:
                for target in _css_url_targets(value):
                    _validate_resource_target(target, allow_embedded_font=True)
        if name == "style":
            for target in _css_url_targets(element.text or ""):
                _validate_resource_target(target, allow_embedded_font=True)


class D2DiagramCompiler:
    """Compile canonical diagrams through a pinned, bounded D2 0.9.0 process."""

    def __init__(self, runner: D2ProcessRunner | None = None, binary: str = D2_COMPILER) -> None:
        self._uses_default_runner = runner is None
        self._runner: D2ProcessRunner = runner if runner is not None else AsyncioD2ProcessRunner()
        self._binary = binary

    async def compile(self, diagram: DiagramSpecV1) -> CompiledDiagram:
        source_bytes = encode_d2_source(diagram)
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        await self._verify_version()
        result = await self._runner.run(
            (
                self._binary,
                f"--layout={D2_LAYOUT}",
                f"--timeout={D2_COMPILATION_TIMEOUT_SECONDS}",
                "--omit-version",
                f"--salt={source_sha256}",
                "--stdout-format=svg",
                "-",
                "-",
            ),
            stdin=source_bytes,
            environment=d2_process_environment(),
            timeout_seconds=float(D2_COMPILATION_TIMEOUT_SECONDS),
            stdout_limit=D2_MAX_STDOUT_BYTES,
            stderr_limit=D2_MAX_STDERR_BYTES,
        )
        _raise_process_failure(result)
        validate_d2_svg(result.stdout)
        return CompiledDiagram(
            diagram_key=diagram.key,
            source_format=D2_SOURCE_FORMAT,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            media_type=D2_MEDIA_TYPE,
            media_bytes=result.stdout,
            media_sha256=hashlib.sha256(result.stdout).hexdigest(),
            compiler=D2_COMPILER,
            compiler_version=D2_COMPILER_VERSION,
            compiler_policy_version=DIAGRAM_COMPILATION_POLICY_VERSION,
        )

    async def _verify_version(self) -> None:
        global _VERSION_CACHE_BINARY, _VERSION_CACHE_RUNNER, _VERSION_CACHE_FUTURE

        cache_runner = None if self._uses_default_runner else self._runner
        with _VERSION_CACHE_LOCK:
            if self._binary != _VERSION_CACHE_BINARY or cache_runner is not _VERSION_CACHE_RUNNER:
                _VERSION_CACHE_BINARY = self._binary
                _VERSION_CACHE_RUNNER = cache_runner
                _VERSION_CACHE_FUTURE = None
            future = _VERSION_CACHE_FUTURE
            if future is None:
                future = concurrent.futures.Future()
                _VERSION_CACHE_FUTURE = future
                owns_check = True
            else:
                owns_check = False

        if not owns_check:
            await asyncio.wrap_future(future)
            return

        try:
            await self._run_version_check()
        except BaseException as exc:
            future.set_exception(exc)
            with _VERSION_CACHE_LOCK:
                if _VERSION_CACHE_FUTURE is future:
                    _VERSION_CACHE_FUTURE = None
            raise
        future.set_result(None)

    async def _run_version_check(self) -> None:
        result = await self._runner.run(
            (self._binary, "--version"),
            stdin=b"",
            environment=d2_process_environment(),
            timeout_seconds=float(D2_COMPILATION_TIMEOUT_SECONDS),
            stdout_limit=D2_MAX_STDOUT_BYTES,
            stderr_limit=D2_MAX_STDERR_BYTES,
        )
        _raise_process_failure(result)
        reported = result.stdout.decode("utf-8", errors="replace").strip().removeprefix("v")
        if reported != D2_COMPILER_VERSION:
            raise DiagramCompilerVersionError(
                f"required d2 version {D2_COMPILER_VERSION}, reported {reported[:64]!r}"
            )
