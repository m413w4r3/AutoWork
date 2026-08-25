"""Deterministic gate between Q2 proposals and canonical extraction."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit

from cti_app.application.production_evidence_pack import EvidenceChunk, ProductionEvidencePack
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    Q2ArtifactProposal,
    Q2ChunkOutput,
    Q2FactProposal,
    SemanticType,
    TechnicalExtraction,
)
from cti_app.domain.publication import ArtifactType


class ProposalStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Q2ProposalSubmission:
    """One Q2 call, bound by orchestration to exactly one archived chunk."""

    output: Q2ChunkOutput
    source_document_id: str
    chunk_id: str
    source_ids: tuple[str, ...]
    model_run_id: str | None = None


@dataclass(frozen=True)
class ProposalDiagnostic:
    status: ProposalStatus
    proposal_index: int
    source_document_id: str
    chunk_id: str
    reason_code: str | None = None


@dataclass(frozen=True)
class ArtifactVerificationResult:
    canonical: TechnicalExtraction
    diagnostics: tuple[ProposalDiagnostic, ...]
    warnings: tuple[str, ...]

    @property
    def rejected(self) -> tuple[ProposalDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.status is ProposalStatus.REJECTED)


_REFANG_DOT = re.compile(r"\[\.\]|\(\.\)|\{\.\}", re.IGNORECASE)
_REFANG_AT = re.compile(r"\[(?:at|@)\]|\((?:at|@)\)", re.IGNORECASE)
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

# IANA TLD snapshot 2026-08-25, deliberately local and versioned.  This
# compact allow-list covers operational CTI sources and blocks prose/file suffixes.
IANA_TLD_SNAPSHOT_VERSION = "iana-tlds-2026-08-25"
_IANA_TLDS = frozenset(
    (
        "ac ad ae af ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn "
        "bo bq br bs bt bw by bz ca cc cd cf cg ch ci ck cl cm cn co com coop cr cu cv cw cx "
        "cy cz de dev dj dk dm do dz ec edu ee eg er es et eu fi fj fk fm fo fr ga gd ge gf gg "
        "gh gi gl gm gn gov gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in info int io "
        "iq ir is it je jm jo jobs jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt "
        "lu lv ly ma mc md me mg mh mil mk ml mm mn mo mobi mp mq mr ms mt mu museum mv mw mx "
        "my mz na name nc ne net nf ng ni nl no np nr nu nz om org pa pe pf pg ph pk pl pm pn pr "
        "pro ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st su "
        "sv sx sy sz tc td tel tf tg th tj tk tl tm tn to top tr travel tt tv tw tz ua ug uk us uy "
        "uz va vc ve vg vi vn vu wf ws xyz ye yt za zm zw"
    ).split()
)
_MULTI_LABEL_SUFFIXES = frozenset({"org.il", "co.uk", "org.uk", "com.au", "net.au", "org.au"})


def verify_q2_proposals(
    submissions: Sequence[Q2ProposalSubmission],
    evidence_pack: ProductionEvidencePack,
    original_derived_texts: Mapping[str, str] | None = None,
) -> ArtifactVerificationResult:
    """Accept only literals demonstrated in archived text; never Q2 context."""
    original_texts = original_derived_texts or evidence_pack.original_derived_texts
    chunks = {chunk.chunk_id: chunk for chunk in evidence_pack.chunks}
    verified: list[ExtractionItem] = []
    diagnostics: list[ProposalDiagnostic] = []
    for submission in submissions:
        chunk = chunks.get(submission.chunk_id)
        proposals: list[Q2FactProposal | Q2ArtifactProposal] = [
            *submission.output.facts,
            *submission.output.artifacts,
        ]
        for index, proposal in enumerate(proposals, start=1):
            rejection = _rejection_reason(proposal, submission, chunk, original_texts)
            if rejection is not None:
                diagnostics.append(
                    ProposalDiagnostic(
                        ProposalStatus.REJECTED,
                        index,
                        submission.source_document_id,
                        submission.chunk_id,
                        rejection,
                    )
                )
                continue
            try:
                verified.append(_to_item(proposal, index, submission))
            except ValueError:
                diagnostics.append(
                    ProposalDiagnostic(
                        ProposalStatus.REJECTED,
                        index,
                        submission.source_document_id,
                        submission.chunk_id,
                        "normalization_error",
                    )
                )
                continue
            diagnostics.append(
                ProposalDiagnostic(
                    ProposalStatus.VERIFIED,
                    index,
                    submission.source_document_id,
                    submission.chunk_id,
                )
            )
    merged, warnings = _merge_verified(verified)
    uncertainties = tuple(
        dict.fromkeys(
            uncertainty
            for submission in submissions
            for uncertainty in submission.output.uncertainties
        )
    )
    return ArtifactVerificationResult(
        TechnicalExtraction(tuple(merged), uncertainties), tuple(diagnostics), tuple(warnings)
    )


def _rejection_reason(
    proposal: Q2FactProposal | Q2ArtifactProposal,
    submission: Q2ProposalSubmission,
    chunk: EvidenceChunk | None,
    original_texts: Mapping[str, str],
) -> str | None:
    if submission.source_document_id not in original_texts:
        return "source_not_found"
    if chunk is None or str(chunk.source_document_id) != submission.source_document_id:
        return "chunk_not_found"
    if proposal.evidence_quote not in chunk.text:
        return "evidence_quote_not_found"
    if proposal.value not in proposal.evidence_quote:
        return "value_not_in_quote"
    if proposal.value not in original_texts[submission.source_document_id]:
        return "literal_not_found"
    if isinstance(proposal, Q2ArtifactProposal):
        try:
            artifact_type = ArtifactType(proposal.artifact_type)
        except ValueError:
            return "invalid_artifact_type"
        try:
            _validate_value(proposal.value, artifact_type)
        except ValueError:
            return "invalid_value"
        try:
            normalize_indicator_value(proposal.value, artifact_type)
        except ValueError:
            return "normalization_error"
    return None


def _to_item(
    proposal: Q2FactProposal | Q2ArtifactProposal,
    index: int,
    submission: Q2ProposalSubmission,
) -> ExtractionItem:
    if isinstance(proposal, Q2FactProposal):
        return ExtractionItem(
            local_id=f"Q2F{index}",
            category=proposal.category,
            value=proposal.value,
            context=proposal.context,
            artifact_type=None,
            attack_id=None,
            reference_ids=(),
            source_ids=submission.source_ids,
            supported=bool(submission.source_ids),
            evidence_quote=proposal.evidence_quote,
            source_document_ids=(submission.source_document_id,),
            chunk_ids=(submission.chunk_id,),
            model_run_ids=(submission.model_run_id,) if submission.model_run_id else (),
        )
    artifact_type = ArtifactType(proposal.artifact_type)
    status = IndicatorStatus(proposal.indicator_status)
    if artifact_type in {
        ArtifactType.YARA_RULE,
        ArtifactType.SIGMA_RULE,
        ArtifactType.SURICATA_RULE,
    }:
        status = IndicatorStatus.NOT_APPLICABLE
    return ExtractionItem(
        local_id=f"Q2A{index}",
        category="network_artifacts",
        value=proposal.value,
        context=proposal.context,
        artifact_type=artifact_type,
        semantic_type=SemanticType.INDICATOR,
        indicator_status=status,
        display_policy=DisplayPolicy.IOC_SECTION
        if status is IndicatorStatus.CONFIRMED_IOC
        else DisplayPolicy.BODY_ONLY,
        normalized_value=normalize_indicator_value(proposal.value, artifact_type),
        attack_id=None,
        reference_ids=(),
        source_ids=submission.source_ids,
        supported=bool(submission.source_ids),
        evidence_quote=proposal.evidence_quote,
        source_document_ids=(submission.source_document_id,),
        chunk_ids=(submission.chunk_id,),
        model_run_ids=(submission.model_run_id,) if submission.model_run_id else (),
    )


def _validate_value(raw: str, artifact_type: ArtifactType) -> None:
    value = _refang(raw)
    if artifact_type is ArtifactType.IP:
        ipaddress.ip_address(value)
    elif artifact_type is ArtifactType.DOMAIN:
        _validate_hostname(value)
    elif artifact_type is ArtifactType.URL:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("invalid URL")
        _validate_hostname(parts.hostname)
        _ = parts.port
    elif artifact_type is ArtifactType.HASH:
        if len(value) not in {32, 40, 64, 128} or not _HEX.fullmatch(value):
            raise ValueError("invalid hash")
    elif artifact_type is ArtifactType.EMAIL:
        local, marker, domain = value.rpartition("@")
        if not marker or not local or len(local) > 64 or any(c.isspace() for c in local):
            raise ValueError("invalid email")
        _validate_hostname(domain)
    elif artifact_type is ArtifactType.CVE:
        if not _CVE.fullmatch(value):
            raise ValueError("invalid CVE")
    elif artifact_type in {ArtifactType.FILENAME, ArtifactType.FILEPATH}:
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid file value")
    # Rule identifiers need literal proof only; evidence checks already did it.


def _validate_hostname(raw: str) -> None:
    hostname = raw.rstrip(".").lower()
    if len(hostname) > 253 or "/" in hostname or "\\" in hostname or hostname.count(".") < 1:
        raise ValueError("invalid hostname")
    labels = hostname.split(".")
    if any(not _LABEL.fullmatch(label) for label in labels):
        raise ValueError("invalid hostname")
    suffix = ".".join(labels[-2:]) if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES else labels[-1]
    if suffix not in _IANA_TLDS and suffix not in _MULTI_LABEL_SUFFIXES:
        raise ValueError("unknown public suffix")
    # File extensions and glued prose commonly pass label syntax; require a
    # plausible registrable label, never an extension-looking final label.
    if labels[-1] in {"exe", "php", "txt", "pdf"} or any(len(label) > 50 for label in labels):
        raise ValueError("file or prose fragment")


def _refang(value: str) -> str:
    return _REFANG_AT.sub("@", _REFANG_DOT.sub(".", value.strip()))


def _merge_verified(items: Sequence[ExtractionItem]) -> tuple[list[ExtractionItem], list[str]]:
    merged: dict[tuple[object, ...], ExtractionItem] = {}
    warnings: list[str] = []
    for item in items:
        key = (
            (item.artifact_type, item.normalized_value)
            if item.artifact_type
            else (item.category, item.value.casefold())
        )
        previous = merged.get(key)
        if previous is None:
            merged[key] = item
            continue
        statuses = {previous.indicator_status, item.indicator_status}
        if len(statuses) > 1:
            warnings.append("q2_indicator_status_divergence")
        status = _merged_status(statuses)
        context = " | ".join(
            dict.fromkeys(part for part in (previous.context, item.context) if part)
        )
        merged[key] = replace(
            previous,
            context=context,
            indicator_status=status,
            display_policy=DisplayPolicy.IOC_SECTION
            if status is IndicatorStatus.CONFIRMED_IOC
            else DisplayPolicy.BODY_ONLY,
            source_ids=tuple(sorted(set(previous.source_ids + item.source_ids))),
            source_document_ids=tuple(
                sorted(set(previous.source_document_ids + item.source_document_ids))
            ),
            chunk_ids=tuple(sorted(set(previous.chunk_ids + item.chunk_ids))),
            model_run_ids=tuple(sorted(set(previous.model_run_ids + item.model_run_ids))),
            supported=previous.supported or item.supported,
        )
    return [merged[key] for key in sorted(merged, key=str)], list(dict.fromkeys(warnings))


def _merged_status(statuses: set[IndicatorStatus]) -> IndicatorStatus:
    if IndicatorStatus.CONFIRMED_IOC in statuses:
        return IndicatorStatus.CONFIRMED_IOC
    if IndicatorStatus.CONTEXTUAL in statuses:
        return IndicatorStatus.CONTEXTUAL
    if statuses == {IndicatorStatus.EXCLUDED}:
        return IndicatorStatus.EXCLUDED
    return IndicatorStatus.NOT_APPLICABLE
