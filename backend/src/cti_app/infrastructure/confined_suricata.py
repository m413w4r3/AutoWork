"""Offline Suricata jobs through the attested analysis-worker boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from uuid import UUID

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisResult,
    ConfinedAnalysisTool,
)

_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RULE_HEADER = re.compile(
    r"^alert\s+(?:ip|tcp|udp|icmp|icmp6|http|dns|tls|smb|dcerpc)\s+"
    r"\S+\s+\S+\s+->\s+\S+\s+\S+\s+\((.*)\)$",
    re.IGNORECASE,
)
_FORBIDDEN_RULE_DIRECTIVE = re.compile(r"\b(?:include|lua|filestore)\b", re.IGNORECASE)
_RESERVED_SID_MIN = 9_900_000
_RESERVED_SID_MAX = 9_999_999
_MAX_RULE_BYTES = 65_536


def validate_suricata_rule_source(
    source: str,
    *,
    candidate_id: str | None = None,
    candidate_version: str | None = None,
    max_bytes: int = _MAX_RULE_BYTES,
) -> str:
    """Validate bounded alert-only rules and their reserved, versioned SIDs."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("suricata_rule_source_empty")
    try:
        encoded = source.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError("suricata_rule_source_invalid_encoding") from exc
    if len(encoded) > min(max_bytes, _MAX_RULE_BYTES):
        raise ValueError("suricata_rule_source_too_large")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in source):
        raise ValueError("suricata_rule_source_control_character")
    if _FORBIDDEN_RULE_DIRECTIVE.search(source):
        raise ValueError("suricata_rule_source_forbidden_directive")
    if candidate_id is not None and _CANDIDATE_ID.fullmatch(candidate_id) is None:
        raise ValueError("suricata_candidate_id_invalid")
    if candidate_version is not None and (
        not candidate_version.isascii()
        or not candidate_version.isdecimal()
        or not 1 <= int(candidate_version) <= 100_000
    ):
        raise ValueError("suricata_candidate_version_invalid")

    parsed_rules: list[tuple[int, int]] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _RULE_HEADER.fullmatch(line)
        if match is None:
            raise ValueError("suricata_rule_syntax_rejected")
        options = _split_rule_options(match.group(1))
        sids = [value for key, value in options if key.lower() == "sid"]
        revisions = [value for key, value in options if key.lower() == "rev"]
        if len(sids) != 1 or len(revisions) != 1:
            raise ValueError("suricata_rule_requires_one_sid_and_revision")
        if not sids[0].isascii() or not sids[0].isdecimal():
            raise ValueError("suricata_rule_sid_invalid")
        if not revisions[0].isascii() or not revisions[0].isdecimal():
            raise ValueError("suricata_rule_revision_invalid")
        sid, revision = int(sids[0]), int(revisions[0])
        if not _RESERVED_SID_MIN <= sid <= _RESERVED_SID_MAX:
            raise ValueError("suricata_rule_sid_outside_reserved_range")
        if candidate_version is not None and revision != int(candidate_version):
            raise ValueError("suricata_rule_revision_mismatch")
        parsed_rules.append((sid, revision))
    if not parsed_rules or len(parsed_rules) > 64:
        raise ValueError("suricata_rule_count_invalid")
    if len({sid for sid, _ in parsed_rules}) != len(parsed_rules):
        raise ValueError("suricata_rule_sid_duplicate")
    return encoded.decode("utf-8")


def _split_rule_options(value: str) -> list[tuple[str, str]]:
    options: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif character == "\\" and quoted:
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == ";" and not quoted:
            options.append(value[start:index].strip())
            start = index + 1
    if quoted:
        raise ValueError("suricata_rule_string_unterminated")
    tail = value[start:].strip()
    if tail:
        options.append(tail)
    parsed: list[tuple[str, str]] = []
    for option in options:
        key, separator, option_value = option.partition(":")
        if separator:
            parsed.append((key.strip(), option_value.strip()))
        else:
            parsed.append((option.strip(), ""))
    return parsed


class ConfinedSuricataAdapter:
    """Submit rules validation and PCAP replay to the confined worker."""

    def __init__(
        self,
        execution: ConfinedAnalysisExecutionPort,
        *,
        timeout_seconds: float = 60,
        max_output_bytes: int = 512 * 1024,
    ) -> None:
        self.execution = execution
        self.timeout_seconds = min(timeout_seconds, 120)
        self.max_output_bytes = min(max_output_bytes, 1_000_000)

    @property
    def available(self) -> bool:
        family = self.execution.family_statuses.get("suricata")
        return bool(self.execution.available and family is not None and family.available)

    @property
    def available_reason(self) -> str | None:
        if self.available:
            return None
        family = self.execution.family_statuses.get("suricata")
        if family is not None and not family.available:
            return family.available_reason or "analysis_tool_unavailable"
        return self.execution.available_reason or "analysis_family_not_attested"

    async def validate_rules(
        self,
        *,
        artifact_id: UUID,
        investigation_id: UUID,
        capture_sha256: str,
        analysis_parameters: Mapping[str, str] | None = None,
    ) -> ConfinedAnalysisResult:
        return await self._execute(
            ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
            artifact_id=artifact_id,
            investigation_id=investigation_id,
            capture_sha256=capture_sha256,
            analysis_parameters=analysis_parameters,
        )

    async def replay(
        self,
        *,
        artifact_id: UUID,
        investigation_id: UUID,
        capture_sha256: str,
        analysis_parameters: Mapping[str, str] | None = None,
    ) -> ConfinedAnalysisResult:
        return await self._execute(
            ConfinedAnalysisTool.SURICATA_REPLAY,
            artifact_id=artifact_id,
            investigation_id=investigation_id,
            capture_sha256=capture_sha256,
            analysis_parameters=analysis_parameters,
        )

    async def _execute(
        self,
        tool: ConfinedAnalysisTool,
        *,
        artifact_id: UUID,
        investigation_id: UUID,
        capture_sha256: str,
        analysis_parameters: Mapping[str, str] | None,
    ) -> ConfinedAnalysisResult:
        parameters = dict(analysis_parameters or {})
        if parameters:
            if set(parameters) != {"candidate_id", "candidate_version", "rule_source"}:
                raise ValueError("suricata_candidate_parameters_invalid")
            validate_suricata_rule_source(
                parameters["rule_source"],
                candidate_id=parameters["candidate_id"],
                candidate_version=parameters["candidate_version"],
            )
        return await self.execution.execute(
            ConfinedAnalysisRequest(
                investigation_id=investigation_id,
                artifact_id=artifact_id,
                sha256=capture_sha256,
                tool=tool,
                timeout_seconds=self.timeout_seconds,
                max_output_bytes=self.max_output_bytes,
                artifact_kind=ConfinedAnalysisArtifactKind.CAPTURE,
                analysis_parameters=parameters,
            )
        )
