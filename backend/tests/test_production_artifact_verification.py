from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_verification import (
    ProposalStatus,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_evidence_pack import EvidenceChunk, ProductionEvidencePack
from cti_app.application.production_parsers import Q2ChunkOutput, SemanticType
from cti_app.domain.collection import SourceOriginKind


def _pack(
    text: str, *, source_document_id: str | None = None, title: str | None = None
) -> tuple[ProductionEvidencePack, str]:
    source = source_document_id or str(uuid4())
    chunk = EvidenceChunk(
        source_document_id=UUID(source),
        parent_source_ids=(),
        source_ids=("S1",),
        title=title,
        origin_kind=SourceOriginKind.DISCOVERY,
        chunk_id="chunk-1",
        text=text,
        sha256="a" * 64,
    )
    return (
        ProductionEvidencePack(
            "ready", "pack", (chunk,), {}, original_derived_texts={source: text}
        ),
        source,
    )


def _submission(
    source: str,
    artifacts: list[dict[str, Any]],
    *,
    text_facts: list[dict[str, Any]] | None = None,
) -> Q2ProposalSubmission:
    return Q2ProposalSubmission(
        output=Q2ChunkOutput.model_validate(
            {
                "facts": text_facts or [],
                "artifacts": artifacts,
                "uncertainties": [],
            }
        ),
        source_document_id=source,
        chunk_id="chunk-1",
        source_ids=("S1",),
        model_run_id="run-1",
    )


def _artifact(
    value: str, kind: str, *, quote: str | None = None, status: str = "contextual"
) -> dict[str, str]:
    return {
        "value": value,
        "artifact_type": kind,
        "indicator_status": status,
        "context": "archived evidence",
        "evidence_quote": quote or value,
    }


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("nbn.org.il", "domain"),
        ("members.nefeshhope.com", "domain"),
        ("yahelisrael.com", "domain"),
        ("46.30.190.173", "ip"),
        ("c2[.]example.org", "domain"),
        ("hxxp://198[.]51[.]100[.]7/path", "url"),
        ("2001:db8::7", "ip"),
        ("d41d8cd98f00b204e9800998ecf8427e", "hash"),
        ("a" * 40, "hash"),
        ("b" * 64, "hash"),
        ("c" * 128, "hash"),
    ],
)
def test_accepts_valid_artifacts(value: str, kind: str) -> None:
    pack, source = _pack(value)
    result = verify_q2_proposals([_submission(source, [_artifact(value, kind)])], pack)

    assert len(result.canonical.items) == 1
    assert result.diagnostics[0].status is ProposalStatus.VERIFIED


@pytest.mark.parametrize(
    "value",
    [
        "3.coordinatingand",
        "pdf.php",
        "a.exe",
        "directory-list-lowercase-2.3-medium.txt",
        "frameworkearlierthisyear.themitre",
        "this.is.a.sentence.fragment",
    ],
)
def test_rejects_false_domains(value: str) -> None:
    pack, source = _pack(value)
    result = verify_q2_proposals([_submission(source, [_artifact(value, "domain")])], pack)

    assert not result.canonical.items
    assert result.diagnostics[0].reason_code == "invalid_value"


@pytest.mark.parametrize(
    ("artifact", "text", "reason"),
    [
        (_artifact("203.0.113.9", "ip"), "other", "evidence_quote_not_found"),
        (
            _artifact("203.0.113.9", "ip", quote="reported address"),
            "reported address",
            "value_not_in_quote",
        ),
        (_artifact("not-an-ip", "ip"), "not-an-ip", "invalid_value"),
    ],
)
def test_rejection_proof_and_value_reason_codes(
    artifact: dict[str, str], text: str, reason: str
) -> None:
    pack, source = _pack(text)
    result = verify_q2_proposals([_submission(source, [artifact])], pack)

    assert result.diagnostics[0].status is ProposalStatus.REJECTED
    assert result.diagnostics[0].reason_code == reason


def test_rejects_unattached_source_and_missing_chunk() -> None:
    pack, source = _pack("203.0.113.9")
    source_missing = _submission(str(uuid4()), [_artifact("203.0.113.9", "ip")])
    chunk_missing = Q2ProposalSubmission(
        output=source_missing.output,
        source_document_id=source,
        chunk_id="missing",
        source_ids=("S1",),
    )
    result = verify_q2_proposals([source_missing, chunk_missing], pack)

    assert [item.reason_code for item in result.rejected] == [
        "source_not_found",
        "chunk_not_found",
    ]


def test_rejects_value_absent_from_original_derived_text() -> None:
    pack, source = _pack("203.0.113.9")
    result = verify_q2_proposals(
        [_submission(source, [_artifact("203.0.113.9", "ip")])],
        pack,
        original_derived_texts={source: "archived document without artifact"},
    )

    assert result.diagnostics[0].reason_code == "literal_not_found"


def test_merges_verified_artifact_provenance_and_statuses() -> None:
    text = "c2[.]example.org c2.example.org is C2 infrastructure"
    pack, source = _pack(text)
    first = _submission(source, [_artifact("c2[.]example.org", "domain", status="excluded")])
    second = _submission(
        source,
        [
            _artifact(
                "c2.example.org",
                "domain",
                quote="c2.example.org is C2 infrastructure",
                status="confirmed_ioc",
            )
        ],
    )
    result = verify_q2_proposals([first, second], pack)

    assert len(result.canonical.items) == 1
    item = result.canonical.items[0]
    assert item.normalized_value == "c2.example.org"
    assert item.indicator_status.value == "contextual"
    assert "semantic_status_conflict" in result.warnings


def test_rule_is_not_applicable_and_fact_needs_archived_literal() -> None:
    text = "rule suspicious_rule { condition: true } APT Z"
    pack, source = _pack(text)
    result = verify_q2_proposals(
        [
            _submission(
                source,
                [_artifact("suspicious_rule", "yara_rule")],
                text_facts=[
                    {
                        "category": "actors",
                        "value": "APT Z",
                        "context": "named",
                        "evidence_quote": "APT Z",
                    }
                ],
            )
        ],
        pack,
    )

    assert len(result.canonical.items) == 2
    rule = next(item for item in result.canonical.items if item.artifact_type is not None)
    assert rule.indicator_status.value == "not_applicable"


def test_fact_can_be_paraphrased_when_its_quote_and_attack_id_are_literal() -> None:
    text = "APT Z uses T1566.001 to deliver phishing emails."
    pack, source = _pack(text)
    result = verify_q2_proposals(
        [
            _submission(
                source,
                [],
                text_facts=[
                    {
                        "category": "ttps",
                        "value": "Hameçonnage",
                        "attack_id": "T1566.001",
                        "context": "Vecteur initial",
                        "evidence_quote": text,
                    }
                ],
            )
        ],
        pack,
    )

    fact = result.canonical.items[0]
    assert fact.value == "Hameçonnage"
    assert fact.attack_id == "T1566.001"


def test_ioc_titled_document_can_confirm_a_bare_literal_list() -> None:
    pack, source = _pack("203.0.113.9", title="IOC list")
    result = verify_q2_proposals(
        [_submission(source, [_artifact("203.0.113.9", "ip", status="confirmed_ioc")])],
        pack,
    )

    assert result.canonical.items[0].display_policy.value == "ioc_section"


def _fact(category: str, value: str) -> dict[str, str]:
    return {
        "category": category,
        "value": value,
        "context": "archived evidence",
        "evidence_quote": value,
    }


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("actors", SemanticType.ACTOR),
        ("campaigns", SemanticType.CAMPAIGN),
        ("malware", SemanticType.MALWARE),
        ("tools", SemanticType.TOOL),
        ("ttps", SemanticType.TECHNIQUE),
        ("protocols", SemanticType.PROTOCOL),
        ("infrastructure", SemanticType.INFRASTRUCTURE),
        ("files", SemanticType.FILE),
    ],
)
def test_fact_category_maps_to_deterministic_semantic_type(
    category: str, expected: SemanticType
) -> None:
    value = f"literal {category}"
    pack, source = _pack(value)
    result = verify_q2_proposals(
        [_submission(source, [], text_facts=[_fact(category, value)])], pack
    )

    assert result.canonical.items[0].semantic_type is expected


@pytest.mark.parametrize(
    "category",
    ["infection_chain", "victimology", "commands", "persistence", "detections", "other_technical"],
)
def test_fact_category_without_a_dedicated_type_falls_back_to_other(category: str) -> None:
    value = f"literal {category}"
    pack, source = _pack(value)
    result = verify_q2_proposals(
        [_submission(source, [], text_facts=[_fact(category, value)])], pack
    )

    assert result.canonical.items[0].semantic_type is SemanticType.OTHER
