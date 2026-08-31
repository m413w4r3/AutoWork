"""Source-local evidence gate for Q2 IOC/rule proposals.

This module deliberately knows about one source text only.  It does not assign
provenance, call external services, or attempt to repair a model proposal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
)
from cti_app.domain.publication import ArtifactType

SOURCE_EVIDENCE_VERSION = "1"

_NBSP = "\u00a0"
_NARROW_NBSP = "\u202f"
_DOT = re.compile(r"\[\.\]|\(\.\)|\{\.\}", re.IGNORECASE)
_COLON = re.compile(r"\[:\]", re.IGNORECASE)
_AT = re.compile(r"\[(?:at|@)\]|\((?:at|@)\)", re.IGNORECASE)

# These are continuation characters of an indicator token.  Punctuation not
# listed here remains a delimiter; no punctuation is removed from the value.
_DOMAIN_CONTINUATION = ".-_"
_HASH_CONTINUATION = ""
_CVE_CONTINUATION = "-"
_IP_CONTINUATION = ".:"
_URL_CONTINUATION = "!#$%&'*+,-./:;=?@_~%"
_EMAIL_CONTINUATION = "!#$%&'*+-./=?^_`{|}~@"
_FILENAME_CONTINUATION = ".-_"
_FILEPATH_CONTINUATION = "._-/\\:"


@dataclass(frozen=True, slots=True)
class SourceEvidenceRejection:
    """One Q2 proposal removed because this source cannot prove it."""

    proposal_index: int
    proposal_kind: str
    reason_code: str
    value: str
    artifact_type: str | None = None

    @property
    def reason(self) -> str:
        """Compatibility alias for callers that use ``reason`` terminology."""
        return self.reason_code


@dataclass(frozen=True, slots=True)
class SourceEvidenceResult:
    """Filtered Q2 output plus deterministic local-gate diagnostics."""

    output: Q2SourceOutput
    warnings: tuple[str, ...]
    rejections: tuple[SourceEvidenceRejection, ...]

    @property
    def filtered_output(self) -> Q2SourceOutput:
        return self.output

    @property
    def rejected(self) -> tuple[SourceEvidenceRejection, ...]:
        return self.rejections


def verify_ioc_rules_output_against_source(
    output: Q2SourceOutput,
    source_text: str,
) -> SourceEvidenceResult:
    """Keep only IOC/rule proposals with literal proof in ``source_text``.

    Facts are outside the IOC_RULES contract and are always dropped.  Every
    surviving proposal has its narrative fields cleared; the model's value or
    rule body is otherwise left untouched.
    """
    source_view = _artifact_comparison_view(source_text)
    rule_source_view = _rule_comparison_view(source_text)
    artifacts: list[Q2ArtifactProposal] = []
    rules: list[Q2RuleProposal] = []
    warnings: list[str] = []
    rejections: list[SourceEvidenceRejection] = []

    if output.facts:
        warnings.append("fact_not_allowed")

    proposal_index = len(output.facts)
    for artifact in output.artifacts:
        proposal_index += 1
        if _artifact_is_proven(artifact, source_view):
            artifacts.append(artifact.model_copy(update={"context": "", "evidence_quote": ""}))
        else:
            rejections.append(
                SourceEvidenceRejection(
                    proposal_index=proposal_index,
                    proposal_kind="artifact",
                    reason_code="source_evidence_missing",
                    value=artifact.value,
                    artifact_type=artifact.artifact_type,
                )
            )

    for rule in output.rules:
        proposal_index += 1
        body = _rule_comparison_view(rule.body)
        if body.strip() and body in rule_source_view:
            rules.append(rule.model_copy(update={"context": "", "evidence_quote": ""}))
        else:
            rejections.append(
                SourceEvidenceRejection(
                    proposal_index=proposal_index,
                    proposal_kind="rule",
                    reason_code="source_rule_evidence_missing",
                    value=rule.body,
                    artifact_type=rule.rule_type.value,
                )
            )

    filtered = Q2SourceOutput(
        facts=[],
        artifacts=artifacts,
        rules=rules,
        uncertainties=list(output.uncertainties),
    )
    return SourceEvidenceResult(
        output=filtered,
        warnings=tuple(warnings),
        rejections=tuple(rejections),
    )


def _artifact_comparison_view(value: str) -> str:
    """Apply only the transport and CTI refanging allowed by this gate."""
    view = value.replace("\r\n", "\n").replace("\r", "\n")
    view = view.replace(_NBSP, " ").replace(_NARROW_NBSP, " ")
    view = view.replace(r"\:", ":")
    view = _DOT.sub(".", view)
    view = _COLON.sub(":", view)
    view = _AT.sub("@", view)
    if view[:8].casefold() == "hxxps://":
        view = "https://" + view[8:]
    elif view[:7].casefold() == "hxxp://":
        view = "http://" + view[7:]
    return view


def _rule_comparison_view(value: str) -> str:
    """Rules permit line-ending normalization and nothing else."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _artifact_is_proven(artifact: Q2ArtifactProposal, source: str) -> bool:
    try:
        artifact_type = ArtifactType(artifact.artifact_type)
    except ValueError:
        return False

    candidate = _artifact_comparison_view(artifact.value)
    if artifact_type is ArtifactType.DOMAIN:
        return _contains_domain(source, candidate.casefold())
    if artifact_type in {ArtifactType.HASH, ArtifactType.CVE}:
        return _contains_bounded(
            source,
            candidate.casefold(),
            _continuation_for(artifact_type),
            casefold_source=True,
        )
    if artifact_type is ArtifactType.IP:
        return _contains_ip(source, candidate)
    if artifact_type is ArtifactType.EMAIL:
        return _contains_email(source, candidate)
    if artifact_type is ArtifactType.URL:
        return _contains_url(source, candidate)
    return _contains_bounded(source, candidate, _continuation_for(artifact_type))


def _continuation_for(artifact_type: ArtifactType) -> str:
    if artifact_type is ArtifactType.DOMAIN:
        return _DOMAIN_CONTINUATION
    if artifact_type is ArtifactType.HASH:
        return _HASH_CONTINUATION
    if artifact_type is ArtifactType.CVE:
        return _CVE_CONTINUATION
    if artifact_type is ArtifactType.IP:
        return _IP_CONTINUATION
    if artifact_type is ArtifactType.URL:
        return _URL_CONTINUATION
    if artifact_type is ArtifactType.EMAIL:
        return _EMAIL_CONTINUATION
    if artifact_type is ArtifactType.FILENAME:
        return _FILENAME_CONTINUATION
    if artifact_type is ArtifactType.FILEPATH:
        return _FILEPATH_CONTINUATION
    return ""


def _contains_email(source: str, candidate: str) -> bool:
    local, separator, domain = candidate.rpartition("@")
    if not separator:
        return _contains_bounded(source, candidate, _EMAIL_CONTINUATION)

    prefix = f"{local}@"
    start = source.find(prefix)
    while start >= 0:
        end = start + len(candidate)
        source_domain = source[start + len(prefix) : end]
        if source_domain.casefold() == domain.casefold() and _has_email_boundaries(
            source, start, end
        ):
            return True
        start = source.find(prefix, start + 1)
    return False


def _contains_domain(source: str, candidate: str) -> bool:
    comparable_source = source.casefold()
    start = comparable_source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_domain_boundaries(comparable_source, start, end):
            return True
        start = comparable_source.find(candidate, start + 1)
    return False


def _contains_ip(source: str, candidate: str) -> bool:
    start = source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_ip_boundaries(source, candidate, start, end):
            return True
        start = source.find(candidate, start + 1)
    return False


def _contains_url(source: str, candidate: str) -> bool:
    start = source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_url_boundaries(source, start, end):
            return True
        start = source.find(candidate, start + 1)
    return False


def _contains_bounded(
    source: str,
    candidate: str,
    continuation: str,
    *,
    casefold_source: bool = False,
) -> bool:
    if not candidate:
        return False
    comparable_source = source.casefold() if casefold_source else source
    start = comparable_source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_token_boundaries(comparable_source, start, end, continuation):
            return True
        start = comparable_source.find(candidate, start + 1)
    return False


def _has_token_boundaries(source: str, start: int, end: int, continuation: str) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    return not _is_continuation(before, continuation) and not _is_continuation(after, continuation)


def _has_domain_boundaries(source: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if before and (before.isalnum() or before in _DOMAIN_CONTINUATION):
        return False
    if after and (after.isalnum() or after in "-_"):
        return False
    # A dot followed by another domain label extends the indicator. A final
    # dot followed by prose punctuation/whitespace is sentence punctuation.
    return not (
        after == "."
        and end + 1 < len(source)
        and (source[end + 1].isalnum() or source[end + 1] in "-_")
    )


def _has_ip_boundaries(source: str, candidate: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if ":" in candidate:
        return not _is_continuation(before, _IP_CONTINUATION) and not _is_continuation(
            after, _IP_CONTINUATION
        )
    if before and (before.isalnum() or before == "."):
        return False
    if after and (after.isalnum() or after == "."):
        if after != "." or (end + 1 < len(source) and source[end + 1].isdigit()):
            return False
    return True


def _has_email_boundaries(source: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if _is_continuation(before, _EMAIL_CONTINUATION):
        return False
    if after == ".":
        return not (end + 1 < len(source) and source[end + 1].isalnum())
    return not _is_continuation(after, _EMAIL_CONTINUATION)


def _has_url_boundaries(source: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if _is_continuation(before, _URL_CONTINUATION):
        return False
    if after in ".,;:!?":
        next_value = source[end + 1] if end + 1 < len(source) else ""
        return not next_value or next_value.isspace() or next_value in ")]}>"
    return not _is_continuation(after, _URL_CONTINUATION)


def _is_continuation(value: str, continuation: str) -> bool:
    return bool(value) and (value.isalnum() or value in continuation)
