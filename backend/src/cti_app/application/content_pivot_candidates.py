"""Build traceable VirusTotal content pivots from typed persisted measurements."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from cti_app.application.evidence_recorder import sanitize_evidence_text
from cti_app.application.virustotal import validate_search_query
from cti_app.domain.investigation import EvidenceObservation

MIN_CONTENT_PATTERN_BYTES = 12
MAX_CONTENT_PATTERN_BYTES = 512
MAX_HEX_PATTERN_BYTES = 256
RAW_PATTERN_WINDOW_BYTES = 48
RAW_PATTERN_WINDOW_STRIDE = 16
MAX_BASE64_PATTERN_BYTES = 384
MAX_CONTENT_PATTERN_CANDIDATES = 512
MAX_CONTENT_PATTERN_MEASUREMENTS = 64
MAX_SHARED_PREFIX_SAMPLES = 32
MAX_CONTENT_FRONTIER_PAGE = 128
_UNLINKED_CANDIDATE_SCHEMAS = frozenset(
    {
        "content-measurements.v1",
        "dotnet-analysis-observation.v1",
        "local-command-execution.v1",
        "sample-obfuscated-strings.v1",
        "sample-strings.v1",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_HOST_PATH = re.compile(
    r"(?i)(?:^|[\s=:'\"])(?:/home/[^/\s]+|/users/[^/\s]+|/root(?:/|$)|"
    r"/tmp/|/var/tmp/|/mnt/|/workspace/|[a-z]:\\(?:users|temp|windows\\temp)\\|"
    r"\\\\[^\\/\s]+\\(?:users|temp|windows\\temp)\\)"
)
_DUMP_LINE = re.compile(r"^\s*(?:0x)?([0-9a-fA-F]{4,16}):?\s+(.*)$")
_GENERIC_EXACT = frozenset(
    {
        "system",
        "system.object",
        "system.string",
        "system.int32",
        "system.boolean",
        "system.void",
        "system.runtime",
        "system.private.corelib",
        "mscorlib",
        "netstandard",
        "microsoft.csharp",
        "system.core",
        "system.dll",
        "system.xml",
        "system.net.http",
        "system.collections",
        "tostring",
        "gethashcode",
        "equals",
        "dispose",
        ".ctor",
    }
)
_GENERIC_PREFIXES = (
    "system.collections.",
    "system.diagnostics.",
    "system.io.",
    "system.linq.",
    "system.net.",
    "system.reflection.",
    "system.runtime.",
    "system.security.",
    "system.text.",
    "system.threading.",
    "microsoft.win32.",
)
_MEASUREMENT_CHILD_KEYS = frozenset(
    {"items", "strings", "values", "members", "methods", "types", "result"}
)


@dataclass(frozen=True, slots=True)
class ContentPatternMeasurement:
    sample_sha256: str
    observation_id: str
    observation_sha256: str
    source: str
    source_version: str
    source_file: str
    offset: int
    length: int
    encoding: str
    bridge_kind: str
    bridge_value: str
    goodware_occurrences: int | None


@dataclass(frozen=True, slots=True)
class ContentPatternCandidate:
    id: str
    query: str
    hex_value: str
    byte_length: int
    evidence_refs: tuple[str, ...]
    measurements: tuple[ContentPatternMeasurement, ...]
    justification: str

    @property
    def rank_key(self) -> tuple[int, int, int, int, str]:
        sample_count = len({item.sample_sha256 for item in self.measurements})
        measured_goodware = any(item.goodware_occurrences is not None for item in self.measurements)
        clean_goodware = measured_goodware and all(
            item.goodware_occurrences == 0
            for item in self.measurements
            if item.goodware_occurrences is not None
        )
        return (
            -sample_count,
            -int(clean_goodware),
            -self.byte_length,
            -len(self.measurements),
            self.id,
        )


@dataclass(slots=True)
class _PatternAggregate:
    value: bytes
    measurements: list[ContentPatternMeasurement] = field(default_factory=list)


def generate_content_pivot_candidates(
    observations: Sequence[EvidenceObservation],
    observation_payloads: Mapping[str, Mapping[str, Any] | None],
    *,
    minimum_length: int = MIN_CONTENT_PATTERN_BYTES,
    maximum_candidates: int = MAX_CONTENT_PATTERN_CANDIDATES,
) -> tuple[ContentPatternCandidate, ...]:
    """Return bounded, deduplicated content queries from explicitly typed evidence.

    Text encodings are accepted only when the persisted extraction declares them.
    Raw hex dumps require a recognized xxd/hexdump command and an explicit range.
    """
    if minimum_length < 1 or minimum_length > MAX_CONTENT_PATTERN_BYTES:
        raise ValueError("minimum content-pattern length is outside its supported range")
    if maximum_candidates < 1 or maximum_candidates > MAX_CONTENT_PATTERN_CANDIDATES:
        raise ValueError("content-pattern candidate limit is outside its supported range")

    aggregates: dict[bytes, _PatternAggregate] = {}
    goodware_collisions: set[bytes] = set()
    raw_ranges: list[tuple[EvidenceObservation, str, int, bytes]] = []
    ordered = sorted(observations, key=lambda item: (item.observed_at, item.id.hex))
    for observation in ordered:
        payload = observation_payloads.get(str(observation.id))
        if not isinstance(payload, Mapping):
            continue
        payload = content_pivot_payload_for_observation(observation, payload)
        if payload is None:
            continue
        if payload.get("schema") == "local-command-execution.v1" and _is_local_measurement_source(
            observation.source
        ):
            sample_sha256 = _observation_sample_sha256(observation, payload)
            bounded_range = _bounded_hexdump_range(payload)
            if sample_sha256 is not None and bounded_range is not None:
                raw_ranges.append((observation, sample_sha256, *bounded_range))
        for measurement, value in _typed_measurements(observation, payload):
            if (
                measurement.goodware_occurrences is not None
                and measurement.goodware_occurrences > 0
            ):
                goodware_collisions.add(value)
                continue
            if (
                len(value) < minimum_length
                or _looks_sensitive(value, measurement.encoding)
                or _looks_low_complexity(value)
            ):
                continue
            aggregate = aggregates.setdefault(value, _PatternAggregate(value))
            identity = (
                measurement.observation_id,
                measurement.offset,
                measurement.encoding,
                measurement.bridge_value,
            )
            if all(
                (
                    item.observation_id,
                    item.offset,
                    item.encoding,
                    item.bridge_value,
                )
                != identity
                for item in aggregate.measurements
            ):
                aggregate.measurements.append(measurement)

    _add_shared_raw_prefix_candidates(
        raw_ranges,
        aggregates,
        goodware_collisions,
        minimum_length=minimum_length,
    )

    candidates: list[ContentPatternCandidate] = []
    for value, aggregate in aggregates.items():
        if value in goodware_collisions:
            continue
        measurements = tuple(
            sorted(
                aggregate.measurements,
                key=lambda item: (
                    item.sample_sha256,
                    item.observation_id,
                    item.offset,
                    item.encoding,
                ),
            )[:MAX_CONTENT_PATTERN_MEASUREMENTS]
        )
        if not measurements:
            continue
        hex_value = value.hex()
        query = validate_search_query(f"content:{{{hex_value}}}")
        sample_count = len({item.sample_sha256 for item in measurements})
        measured_goodware = any(item.goodware_occurrences is not None for item in measurements)
        reason = f"Motif mesuré de {len(value)} octets; observé dans {sample_count} échantillon(s)"
        if measured_goodware:
            reason += "; aucune occurrence dans le goodware mesuré"
        elif sample_count > 1:
            reason += "; présence recoupée entre échantillons"
        locations = sorted({(item.sample_sha256, item.offset) for item in aggregate.measurements})
        candidate_identity = {
            "schema": "content-pattern-candidate.v2",
            "octets_hex": hex_value,
            "locations": [[sample_sha256, offset] for sample_sha256, offset in locations],
        }
        candidate_id = hashlib.sha256(
            json.dumps(candidate_identity, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        candidates.append(
            ContentPatternCandidate(
                id=candidate_id,
                query=query,
                hex_value=hex_value,
                byte_length=len(value),
                evidence_refs=tuple(sorted({item.observation_id for item in measurements})[:16]),
                measurements=measurements,
                justification=reason,
            )
        )

    return tuple(sorted(candidates, key=lambda item: item.rank_key)[:maximum_candidates])


def verify_content_pattern_candidates(
    observations: Sequence[EvidenceObservation],
    observation_payloads: Mapping[str, Mapping[str, Any] | None],
    *,
    candidate_ids: Sequence[str] | None = None,
) -> tuple[ContentPatternCandidate, ...]:
    """Recompute the pivot set and optionally retain only requested stable IDs.

    Both the pivot frontier and advisor input validation use this function, so
    candidate identity and every payload/chunk/offset check have one code path.
    """
    candidates = generate_content_pivot_candidates(observations, observation_payloads)
    if candidate_ids is None:
        return candidates
    requested = set(candidate_ids)
    return tuple(candidate for candidate in candidates if candidate.id in requested)


def candidate_frontier_limit(limit: int, *, has_content_candidates: bool) -> int:
    """Bound each rebuild-heavy page while avoiding dozens of repeated full scans."""
    return min(limit, MAX_CONTENT_FRONTIER_PAGE) if has_content_candidates else limit


def content_pivot_payload_for_observation(
    observation: EvidenceObservation, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Normalize typed unlinked payloads through the same admission check as /pivots."""
    schema = payload.get("schema")
    is_typed_schema = (
        isinstance(schema, str) and schema in _UNLINKED_CANDIDATE_SCHEMAS
    ) or payload.get("schema_version") == "dotnet-analysis-observation.v1"
    if observation.invocation_ref is not None or not is_typed_schema:
        return dict(payload)
    return validated_unlinked_content_pivot_payload(observation, payload)


def validated_unlinked_content_pivot_payload(
    observation: EvidenceObservation, value: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Admit only source, object, sample identity, and schema combinations used by pivots."""
    source = observation.source.strip().casefold()
    kind = getattr(observation.kind, "value", observation.kind)
    object_type = observation.object_type
    schema = value.get("schema")
    sample_sha256 = value.get("sample_sha256")
    object_id = observation.object_id
    if (
        not isinstance(sample_sha256, str)
        or _SHA256.fullmatch(sample_sha256) is None
        or not isinstance(object_id, str)
    ):
        return None

    normalized_sample = sample_sha256.casefold()
    normalized_object_id = object_id.casefold()
    if source.startswith("dotnet.") and object_type == "dotnet_analysis":
        expected_schema = "dotnet-analysis-observation.v1"
        schema_version = value.get("schema_version")
        if (
            kind != "binary_analysis"
            or schema not in (None, expected_schema)
            or schema_version not in (None, expected_schema)
            or expected_schema not in (schema, schema_version)
            or not normalized_object_id.startswith(f"{normalized_sample}:")
            or not isinstance(value.get("result"), Mapping)
        ):
            return None
        return {**value, "schema": expected_schema}

    if normalized_object_id != normalized_sample:
        return None
    if (
        source == "autowork.floss"
        and kind == "binary_string"
        and object_type == "sample"
        and schema in ("sample-obfuscated-strings.v1", "sample-strings.v1")
        and isinstance(value.get("strings"), list)
    ):
        return dict(value)
    if (
        (source == "autowork local analysis" or source.startswith("local."))
        and kind == "binary_analysis"
        and object_type in {"file", "command"}
        and schema == "local-command-execution.v1"
        and value.get("command_id") == "xxd"
        and isinstance(value.get("command"), list)
    ):
        return dict(value)
    return None


def _typed_measurements(
    observation: EvidenceObservation, payload: Mapping[str, Any]
) -> tuple[tuple[ContentPatternMeasurement, bytes], ...]:
    schema = payload.get("schema")
    if schema == "sample-obfuscated-strings.v1" and "floss" in observation.source.casefold():
        sample_sha256 = _observation_sample_sha256(observation, payload)
        if sample_sha256 is None:
            return ()
        records = payload.get("strings")
        return _string_records(observation, records, sample_sha256, require_source_file=False)

    if schema == "sample-strings.v1" and any(
        marker in observation.source.casefold() for marker in ("strings", "floss")
    ):
        sample_sha256 = _observation_sample_sha256(observation, payload)
        if sample_sha256 is None:
            return ()
        records = payload.get("strings")
        return _string_records(observation, records, sample_sha256, require_source_file=False)

    if schema == "dotnet-analysis-observation.v1" and observation.source.casefold().startswith(
        "dotnet."
    ):
        sample_sha256 = _observation_sample_sha256(observation, payload)
        if sample_sha256 is None or payload.get("result_truncated_by_tool_boundary") is True:
            return ()
        result = payload.get("result")
        return _dotnet_records(observation, result, sample_sha256)

    if schema == "local-command-execution.v1" and _is_local_measurement_source(observation.source):
        sample_sha256 = _observation_sample_sha256(observation, payload)
        if sample_sha256 is None:
            return ()
        return _hexdump_records(observation, payload, sample_sha256)

    if schema == "content-measurements.v1":
        sample_sha256 = _observation_sample_sha256(observation, payload)
        if sample_sha256 is None:
            return ()
        return _string_records(
            observation,
            payload.get("measurements"),
            sample_sha256,
            require_source_file=True,
        )
    return ()


def _string_records(
    observation: EvidenceObservation,
    records: Any,
    sample_sha256: str,
    *,
    require_source_file: bool,
) -> tuple[tuple[ContentPatternMeasurement, bytes], ...]:
    if not isinstance(records, Sequence) or isinstance(records, str | bytes):
        return ()
    result: list[tuple[ContentPatternMeasurement, bytes]] = []
    for record in records:
        if not isinstance(record, Mapping) or record.get("truncated_fields"):
            continue
        source_file_value = _first_text(record, "source_file", "file_name", "file", "evidence_path")
        if require_source_file and source_file_value is None:
            continue
        source_file = source_file_value or f"sample:{sample_sha256}"
        if _HOST_PATH.search(source_file):
            continue
        value = record.get("value")
        offset = record.get("offset")
        if not isinstance(value, str) or type(offset) is not int or offset < 0:
            continue
        kind = str(record.get("kind", record.get("type", "string"))).casefold()
        encoding = str(record.get("encoding", "")).casefold().replace("-", "")
        if kind in {"import", "assembly_import", "namespace"}:
            continue
        goodware = _nonnegative_int(record.get("goodware_occurrences"))
        decoded = _value_bytes(value, encoding, record.get("hex"))
        if decoded is None:
            continue
        raw_value, normalized_encoding, bridge_kind, _bridge_value = decoded
        if _is_generic(value):
            continue
        if normalized_encoding in {"ascii", "utf16le"}:
            source_text = value
            if normalized_encoding == "ascii" and len(raw_value) > MAX_CONTENT_PATTERN_BYTES:
                source_text = value[:MAX_CONTENT_PATTERN_BYTES]
            elif normalized_encoding == "utf16le" and len(raw_value) > MAX_CONTENT_PATTERN_BYTES:
                source_text = value[: MAX_CONTENT_PATTERN_BYTES // 2]
            chunks = _split_text_value(source_text, normalized_encoding, bridge_kind)
        elif normalized_encoding == "utf8":
            chunks = _split_utf8_value(value)
        elif normalized_encoding == "base64":
            chunks = _split_base64_value(raw_value)
        else:
            chunks = _split_raw_value(raw_value)
        for chunk_offset, chunk, chunk_bridge_kind, chunk_bridge_value in chunks:
            measurement = _measurement(
                observation,
                sample_sha256,
                source_file,
                offset + chunk_offset,
                chunk,
                normalized_encoding,
                chunk_bridge_kind,
                chunk_bridge_value,
                goodware,
            )
            result.append((measurement, chunk))
    return tuple(result)


def _dotnet_records(
    observation: EvidenceObservation, result_payload: Any, sample_sha256: str
) -> tuple[tuple[ContentPatternMeasurement, bytes], ...]:
    if not isinstance(result_payload, Mapping):
        return ()
    output: list[tuple[ContentPatternMeasurement, bytes]] = []
    for record in _walk_measurement_records(result_payload):
        if record.get("offset_kind") not in {"file", "file_offset"}:
            continue
        converted = _string_records(
            observation,
            (record,),
            sample_sha256,
            require_source_file=True,
        )
        output.extend(converted)
    return tuple(output)


def _walk_measurement_records(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    found: list[Mapping[str, Any]] = []
    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        if not isinstance(item, Mapping):
            continue
        if all(key in item for key in ("value", "offset", "encoding")):
            found.append(item)
        for key in _MEASUREMENT_CHILD_KEYS:
            child = item.get(key)
            if isinstance(child, Mapping):
                pending.append(child)
            elif isinstance(child, Sequence) and not isinstance(child, str | bytes):
                pending.extend(child[:4096])
    return tuple(found)


def _hexdump_records(
    observation: EvidenceObservation, payload: Mapping[str, Any], sample_sha256: str
) -> tuple[tuple[ContentPatternMeasurement, bytes], ...]:
    bounded_range = _bounded_hexdump_range(payload)
    if bounded_range is None:
        return ()
    start, raw = bounded_range
    chunks = list(_split_raw_value(raw))
    if len(raw) >= RAW_PATTERN_WINDOW_BYTES:
        last_start = len(raw) - RAW_PATTERN_WINDOW_BYTES
        starts = list(range(0, last_start + 1, RAW_PATTERN_WINDOW_STRIDE))
        if not starts or starts[-1] != last_start:
            starts.append(last_start)
        chunks.extend(
            (
                offset,
                raw[offset : offset + RAW_PATTERN_WINDOW_BYTES],
                "hex",
                raw[offset : offset + RAW_PATTERN_WINDOW_BYTES].hex(),
            )
            for offset in starts
        )
    unique_chunks = {
        (offset, chunk): (offset, chunk, bridge_kind, bridge_value)
        for offset, chunk, bridge_kind, bridge_value in chunks
    }
    return tuple(
        (
            _measurement(
                observation,
                sample_sha256,
                f"sample:{sample_sha256}",
                start + offset,
                chunk,
                "raw",
                "hex",
                chunk.hex(),
                None,
            ),
            chunk,
        )
        for offset, chunk, _, _ in unique_chunks.values()
    )


def _bounded_hexdump_range(payload: Mapping[str, Any]) -> tuple[int, bytes] | None:
    if (
        payload.get("output_truncated") is True
        or payload.get("output_truncated_by_evidence_limit") is True
        or payload.get("output_truncated_by_response") is True
        or payload.get("command_id") not in {"xxd", "hexdump"}
    ):
        return None
    command = payload.get("command")
    command_id = payload.get("command_id")
    if not _is_bounded_hex_dump_command(command, command_id):
        return None
    range_value = payload.get("range")
    if isinstance(range_value, Mapping):
        start = range_value.get("offset")
        length = range_value.get("length")
    elif command_id == "xxd":
        start, length = _xxd_command_range(command)
    else:
        return None
    output = _decode_local_output(payload)
    if (
        type(start) is not int
        or start < 0
        or type(length) is not int
        or not 1 <= length <= 16_384
        or output is None
        or len(output) > 128 * 1024
    ):
        return None
    raw = _parse_bounded_hexdump(
        output,
        start,
        length,
        allow_partial_tail=payload.get("output_encoding") != "base64-utf8",
    )
    return (start, raw) if raw is not None else None


def _decode_local_output(payload: Mapping[str, Any]) -> str | None:
    if payload.get("output_encoding") != "base64-utf8":
        output = payload.get("output")
        if (
            not isinstance(output, str)
            or payload.get("output_truncated") is not False
            or payload.get("output_truncated_by_evidence_limit") is True
            or payload.get("output_truncated_by_response") is True
            or not output
            or len(output.encode("utf-8")) > 128 * 1024
        ):
            return None
        return output
    chunks = payload.get("output_chunks")
    expected_size = payload.get("output_byte_length")
    expected_sha256 = payload.get("output_sha256")
    if (
        not isinstance(chunks, Sequence)
        or isinstance(chunks, str | bytes)
        or not chunks
        or any(not isinstance(chunk, str) for chunk in chunks)
        or type(expected_size) is not int
        or not 1 <= expected_size <= 128 * 1024
        or not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        return None
    try:
        encoded = "".join(cast(str, chunk) for chunk in chunks)
        output_bytes = base64.b64decode(encoded, validate=True)
        if (
            len(output_bytes) != expected_size
            or hashlib.sha256(output_bytes).hexdigest() != expected_sha256.casefold()
        ):
            return None
        return output_bytes.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError, binascii.Error):
        return None


def _is_local_measurement_source(source: str) -> bool:
    normalized = source.strip().casefold()
    return normalized.startswith("local.") or normalized == "autowork local analysis"


def _add_shared_raw_prefix_candidates(
    raw_ranges: Sequence[tuple[EvidenceObservation, str, int, bytes]],
    aggregates: dict[bytes, _PatternAggregate],
    goodware_collisions: set[bytes],
    *,
    minimum_length: int,
) -> None:
    by_offset: dict[int, dict[str, tuple[EvidenceObservation, str, bytes]]] = {}
    for observation, sample_sha256, offset, raw in raw_ranges:
        samples = by_offset.setdefault(offset, {})
        previous = samples.get(sample_sha256)
        if previous is None or len(raw) > len(previous[2]):
            samples[sample_sha256] = (observation, sample_sha256, raw)

    for offset, samples in sorted(by_offset.items()):
        distinct_samples = sorted(samples.values(), key=lambda item: item[1])[
            :MAX_SHARED_PREFIX_SAMPLES
        ]
        for left_index, left in enumerate(distinct_samples):
            left_raw = left[2]
            for right in distinct_samples[left_index + 1 :]:
                right_raw = right[2]
                common_length = 0
                max_common = min(len(left_raw), len(right_raw))
                while (
                    common_length < max_common
                    and left_raw[common_length] == right_raw[common_length]
                ):
                    common_length += 1
                if common_length < minimum_length:
                    continue
                for relative_offset in range(0, common_length, MAX_HEX_PATTERN_BYTES):
                    value = left_raw[
                        relative_offset : min(
                            relative_offset + MAX_HEX_PATTERN_BYTES,
                            common_length,
                        )
                    ]
                    if (
                        len(value) < minimum_length
                        or _looks_sensitive(value, "raw")
                        or _looks_low_complexity(value)
                    ):
                        continue
                    if value in goodware_collisions:
                        continue
                    aggregate = aggregates.setdefault(value, _PatternAggregate(value))
                    for measurement_source in (left, right):
                        observation, sample_sha256, _ = measurement_source
                        measurement = _measurement(
                            observation,
                            sample_sha256,
                            f"sample:{sample_sha256}",
                            offset + relative_offset,
                            value,
                            "raw",
                            "hex",
                            value.hex(),
                            None,
                        )
                        identity = (
                            measurement.observation_id,
                            measurement.offset,
                            measurement.encoding,
                            measurement.bridge_value,
                        )
                        if all(
                            (
                                item.observation_id,
                                item.offset,
                                item.encoding,
                                item.bridge_value,
                            )
                            != identity
                            for item in aggregate.measurements
                        ):
                            aggregate.measurements.append(measurement)


def _parse_bounded_hexdump(
    output: str, start: int, length: int, *, allow_partial_tail: bool = False
) -> bytes | None:
    data = bytearray()
    expected_offset = start
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    for line_index, line in enumerate(lines):
        match = _DUMP_LINE.fullmatch(line)
        if match is None:
            partial_address = re.match(r"^\s*(?:0x)?([0-9a-fA-F]{4,16}):", line)
            if (
                allow_partial_tail
                and line_index == len(lines) - 1
                and partial_address is not None
                and int(partial_address.group(1), 16) == expected_offset
            ):
                break
            return None
        line_offset = int(match.group(1), 16)
        if line_offset != expected_offset or len(data) >= length:
            return None
        expected_count = min(16, length - len(data))
        line_data = bytearray()
        for segment in re.split(r"\s{2,}", match.group(2).strip()):
            for token in segment.split():
                if len(line_data) >= expected_count:
                    break
                if not token or len(token) % 2 or re.fullmatch(r"[0-9a-fA-F]+", token) is None:
                    break
                for byte_index in range(0, len(token), 2):
                    line_data.append(int(token[byte_index : byte_index + 2], 16))
                    if len(line_data) == expected_count:
                        break
            if len(line_data) >= expected_count:
                break
        if len(line_data) != expected_count:
            if allow_partial_tail and line_index == len(lines) - 1:
                break
            return None
        data.extend(line_data)
        expected_offset += len(line_data)
    return bytes(data) if data else None


def _is_bounded_hex_dump_command(command: Any, command_id: Any) -> bool:
    if isinstance(command, str):
        parts = command.strip().split()
    elif isinstance(command, Sequence) and not isinstance(command, str | bytes):
        if any(not isinstance(part, str) for part in command):
            return False
        parts = [cast(str, part) for part in command]
    else:
        return False
    if not parts or any(marker in part for marker in (";", "|", "&", "$", "`") for part in parts):
        return False
    executable = parts[0].rsplit("/", 1)[-1].casefold()
    return executable in {"xxd", "hexdump"} and command_id == executable


def _xxd_command_range(command: Any) -> tuple[int, int] | tuple[None, None]:
    if isinstance(command, str):
        parts = command.strip().split()
    elif isinstance(command, Sequence) and not isinstance(command, str | bytes):
        if any(not isinstance(part, str) for part in command):
            return None, None
        parts = [cast(str, part) for part in command]
    else:
        return None, None
    offsets = [index for index, part in enumerate(parts[:-1]) if part == "-s"]
    lengths = [index for index, part in enumerate(parts[:-1]) if part == "-l"]
    if len(offsets) != 1 or len(lengths) != 1:
        return None, None
    offset_text = parts[offsets[0] + 1]
    length_text = parts[lengths[0] + 1]
    if not offset_text.isdecimal() or not length_text.isdecimal():
        return None, None
    return int(offset_text), int(length_text)


def _value_bytes(value: str, encoding: str, raw_hex: Any) -> tuple[bytes, str, str, str] | None:
    if encoding in {"ascii", "utf8"}:
        try:
            raw = value.encode("ascii" if encoding == "ascii" else "utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        normalized = "ascii" if encoding == "ascii" else "utf8"
        return raw, normalized, "string", value
    if encoding in {"utf16le", "wide"}:
        try:
            raw = value.encode("utf-16le", errors="strict")
        except UnicodeEncodeError:
            return None
        return raw, "utf16le", "wide", value
    if encoding == "raw":
        hex_value = raw_hex if isinstance(raw_hex, str) else value
        decoded_raw = _strict_hex_value(hex_value)
        if decoded_raw is None:
            return None
        return decoded_raw, "raw", "hex", decoded_raw.hex()
    if encoding == "base64":
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            return None
        return raw, "base64", "base64", value
    return None


def _strict_hex_value(value: str) -> bytes | None:
    compact = "".join(value.split())
    if not compact or len(compact) % 2 or re.fullmatch(r"[0-9a-fA-F]+", compact) is None:
        return None
    return bytes.fromhex(compact)


def _split_text_value(
    value: str, encoding: str, bridge_kind: str
) -> tuple[tuple[int, bytes, str, str], ...]:
    if encoding in {"ascii", "utf8"}:
        chunk_text = value[:MAX_CONTENT_PATTERN_BYTES]
        raw = chunk_text.encode("ascii")
        return ((0, raw, bridge_kind, chunk_text),)
    chunk_text = value[: MAX_CONTENT_PATTERN_BYTES // 2]
    raw = chunk_text.encode("utf-16le")
    return ((0, raw, bridge_kind, chunk_text),)


def _split_utf8_value(value: str) -> tuple[tuple[int, bytes, str, str], ...]:
    chunks: list[tuple[int, bytes, str, str]] = []
    start = 0
    current: list[str] = []
    current_size = 0
    for character in value:
        encoded = character.encode("utf-8")
        if current and current_size + len(encoded) > MAX_CONTENT_PATTERN_BYTES:
            chunk_text = "".join(current)
            chunks.append((start, chunk_text.encode("utf-8"), "string", chunk_text))
            start += current_size
            current = []
            current_size = 0
        current.append(character)
        current_size += len(encoded)
    if current:
        chunk_text = "".join(current)
        chunks.append((start, chunk_text.encode("utf-8"), "string", chunk_text))
    return tuple(chunks)


def _split_raw_value(value: bytes) -> tuple[tuple[int, bytes, str, str], ...]:
    return tuple(
        (
            start,
            chunk,
            "hex",
            chunk.hex(),
        )
        for start in range(0, len(value), MAX_HEX_PATTERN_BYTES)
        if (chunk := value[start : start + MAX_HEX_PATTERN_BYTES])
    )


def _split_base64_value(value: bytes) -> tuple[tuple[int, bytes, str, str], ...]:
    return tuple(
        (
            start,
            chunk,
            "base64",
            base64.b64encode(chunk).decode("ascii"),
        )
        for start in range(0, len(value), MAX_BASE64_PATTERN_BYTES)
        if (chunk := value[start : start + MAX_BASE64_PATTERN_BYTES])
    )


def _measurement(
    observation: EvidenceObservation,
    sample_sha256: str,
    source_file: str,
    offset: int,
    value: bytes,
    encoding: str,
    bridge_kind: str,
    bridge_value: str,
    goodware_occurrences: int | None,
) -> ContentPatternMeasurement:
    return ContentPatternMeasurement(
        sample_sha256=sample_sha256,
        observation_id=str(observation.id),
        observation_sha256=observation.sha256,
        source=observation.source,
        source_version=observation.source_version,
        source_file=source_file,
        offset=offset,
        length=len(value),
        encoding=encoding,
        bridge_kind=bridge_kind,
        bridge_value=bridge_value,
        goodware_occurrences=goodware_occurrences,
    )


def _looks_sensitive(value: bytes, encoding: str) -> bool:
    from cti_app.application.investigation_vt_tools import (
        _NO_EXTERNAL_SECRET,
        _SIGNED_QUERY_KEYS,
    )

    if encoding == "raw":
        text = value.decode("ascii", errors="ignore")
        text_runs = re.findall(r"[\x20-\x7e]{4,}", text)
        return any(
            _NO_EXTERNAL_SECRET.search(run)
            or _SIGNED_QUERY_KEYS.search(run)
            or _HOST_PATH.search(run)
            or sanitize_evidence_text(run) != run
            for run in text_runs
        )
    if encoding == "utf16le":
        try:
            text = value.decode("utf-16le", errors="strict")
        except UnicodeDecodeError:
            return True
    elif encoding == "utf8":
        try:
            text = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return True
    else:
        text = value.decode("ascii", errors="ignore")
    if not text:
        return False
    return bool(
        _NO_EXTERNAL_SECRET.search(text)
        or _SIGNED_QUERY_KEYS.search(text)
        or _HOST_PATH.search(text)
        or sanitize_evidence_text(text) != text
    )


def _is_generic(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    return normalized in _GENERIC_EXACT or normalized.startswith(_GENERIC_PREFIXES)


def _looks_low_complexity(value: bytes) -> bool:
    if len(value) < 16:
        return len(set(value)) < 4
    frequencies = Counter(value)
    return len(frequencies) < 4 or max(frequencies.values()) / len(value) >= 0.8


def _sample_sha256(value: Any) -> str | None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        return None
    return value.casefold()


def _observation_sample_sha256(
    observation: EvidenceObservation, payload: Mapping[str, Any]
) -> str | None:
    sample_sha256 = _sample_sha256(payload.get("sample_sha256"))
    object_id = observation.object_id
    if sample_sha256 is None or not isinstance(object_id, str):
        return None
    normalized_object_id = object_id.casefold()
    matches_identity = normalized_object_id == sample_sha256
    matches_dotnet_identity = (
        observation.object_type == "dotnet_analysis"
        and normalized_object_id.startswith(f"{sample_sha256}:")
    )
    return sample_sha256 if matches_identity or matches_dotnet_identity else None


def _first_text(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None
