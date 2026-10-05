from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    SynthesisUncertaintyV1,
    is_valid_editorial_title,
    production_synthesis_from_json,
    production_synthesis_to_json,
    validate_synthesis_lineage,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _snapshot() -> ProductionInputSnapshot:
    subject_id = uuid4()
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        edition_id=uuid4(),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Example subject",
        subject_tlp=next(iter(TLP)),
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="A bounded summary.",
        actor_or_campaign="Example campaign",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        publication_language="fr",
        research_date=date(2026, 2, 1),
    )


def _ref(document_id: UUID | None = None, *, key: str = _HASH_A) -> ExtractionEvidenceRefV1:
    return ExtractionEvidenceRefV1(
        source_document_id=document_id or uuid4(),
        kind=EvidenceKind.FACT,
        evidence_key=key,
    )


def _synthesis(snapshot: ProductionInputSnapshot | None = None) -> ProductionSynthesisV1:
    snapshot = snapshot or _snapshot()
    document_id = uuid4()
    ref = _ref(document_id)
    return ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=_HASH_B,
        publication_language=snapshot.publication_language,
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=snapshot.subject_title,
        lead=(SynthesisParagraphV1("The campaign was observed.", (ref,)),),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Overview",
                (SynthesisParagraphV1("The activity affected several systems.", (ref,)),),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(
                date(2026, 1, 3), "3 January", "Activity was observed.", (ref,)
            ),
        ),
        uncertainties=(SynthesisUncertaintyV1("The operator is unknown.", (document_id,)),),
        warnings=("Limited source coverage.",),
    )


def test_production_synthesis_round_trip_and_deterministic_collections() -> None:
    synthesis = _synthesis()
    payload = production_synthesis_to_json(synthesis)

    assert production_synthesis_from_json(payload) == synthesis
    assert production_synthesis_to_json(production_synthesis_from_json(payload)) == payload
    assert production_synthesis_to_json(synthesis) == production_synthesis_to_json(synthesis)


def test_evidence_refs_and_timeline_are_normalized_deterministically() -> None:
    first = _ref(key=_HASH_A)
    second = _ref(key=_HASH_B)
    paragraph = SynthesisParagraphV1("A sourced claim.", (second, first))
    entry_later = SynthesisTimelineEntryV1(date(2026, 1, 2), None, "Later event.", (first,))
    entry_earlier = SynthesisTimelineEntryV1(date(2026, 1, 1), None, "Earlier event.", (second,))
    synthesis = replace(
        _synthesis(),
        lead=(paragraph,),
        timeline=(entry_later, entry_earlier),
        warnings=("z", "a", "z"),
    )

    assert paragraph.evidence_refs == tuple(
        sorted(
            (first, second),
            key=lambda item: (
                str(item.source_document_id),
                item.kind.value,
                item.evidence_key,
            ),
        )
    )
    assert [entry.event_date for entry in synthesis.timeline] == [
        date(2026, 1, 1),
        date(2026, 1, 2),
    ]
    assert synthesis.warnings == ("a", "z")


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_decoder_rejects_extra_or_missing_fields(mutation: str) -> None:
    payload = production_synthesis_to_json(_synthesis())
    if mutation == "extra":
        payload["unexpected"] = "value"
    else:
        del payload["title"]

    with pytest.raises(ValueError):
        production_synthesis_from_json(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_id", "not-a-uuid"),
        ("production_input_hash", "A" * 64),
        ("extraction_hash", "invalid"),
    ],
)
def test_decoder_rejects_malformed_uuid_and_hashes(field: str, value: str) -> None:
    payload = production_synthesis_to_json(_synthesis())
    payload[field] = value

    with pytest.raises(ValueError):
        production_synthesis_from_json(payload)


def test_decoder_rejects_unknown_section_kind_and_nested_extra_fields() -> None:
    payload = production_synthesis_to_json(_synthesis())
    payload["sections"][0]["kind"] = "h2"
    with pytest.raises(ValueError):
        production_synthesis_from_json(payload)

    payload = production_synthesis_to_json(_synthesis())
    payload["lead"][0]["unexpected"] = "value"
    with pytest.raises(ValueError):
        production_synthesis_from_json(payload)


def test_sourced_paragraph_rejects_empty_or_duplicate_evidence() -> None:
    with pytest.raises(ValueError):
        SynthesisParagraphV1("An unsupported claim.", ())

    ref = _ref()
    with pytest.raises(ValueError):
        SynthesisParagraphV1("A claim.", (ref, ref))


def test_evidence_reference_rejects_invalid_key() -> None:
    with pytest.raises(ValueError):
        _ref(key="g" * 64)


def test_editorial_title_contract_accepts_plain_titles_and_rejects_invalid_forms() -> None:
    assert is_valid_editorial_title("[Seedworm / MuddyWater] Adoption de ChainShell et CastleRAT")
    assert is_valid_editorial_title("[Publication] Analyse d\u2019un implant de commande")
    for value in (
        "Frozen subject title",
        "[] Titre sans groupe",
        "[Groupe] Une campagne documentée.",
        "[Groupe] **Une campagne documentée**",
        "[Groupe] *Une campagne documentée*",
        "[Groupe] <em>Campagne</em>",
        "[Groupe] Une campagne E001 documentée",
        "[Groupe] " + "x" * 105,
    ):
        assert not is_valid_editorial_title(value)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": PRODUCTION_SYNTHESIS_SCHEMA_VERSION + 1},
        {"synthesis_policy_version": "future-policy"},
    ],
)
def test_contract_rejects_unsupported_schema_and_policy(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(_synthesis(), **change)


def test_lineage_matches_frozen_snapshot_and_canonical_extraction_hash() -> None:
    snapshot = _snapshot()
    synthesis = _synthesis(snapshot)

    validate_synthesis_lineage(synthesis, snapshot, _HASH_B)

    with pytest.raises(ValueError, match="subject_id"):
        validate_synthesis_lineage(replace(synthesis, subject_id=uuid4()), snapshot, _HASH_B)
    with pytest.raises(ValueError, match="production_input_hash"):
        validate_synthesis_lineage(
            replace(synthesis, production_input_hash=_HASH_A), snapshot, _HASH_B
        )
    with pytest.raises(ValueError, match="publication_language"):
        validate_synthesis_lineage(replace(synthesis, publication_language="en"), snapshot, _HASH_B)
    validate_synthesis_lineage(
        replace(synthesis, title="[Core publisher] Editorial title"), snapshot, _HASH_B
    )
    with pytest.raises(ValueError, match="extraction_hash"):
        validate_synthesis_lineage(synthesis, snapshot, _HASH_A)
