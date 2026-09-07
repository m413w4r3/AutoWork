"""LOT 37: deterministic Q4 revision context and prompt contract."""

from uuid import uuid4

from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    TechnicalExtraction,
)
from cti_app.application.production_prompts import ProductionPromptTemplates
from cti_app.application.production_synthesis_revision import (
    build_synthesis_revision_context,
    narrative_repair_keys,
    synthesis_content_hash,
    synthesis_semantic_source_ids,
)
from cti_app.application.production_workflow import _synthesis_input_hash


def test_revision_delta_is_sorted_and_content_based() -> None:
    previous_id = uuid4()
    context = build_synthesis_revision_context(
        previous_artifact_id=previous_id,
        previous_input_hash="a" * 64,
        previous_text="Ancien draft [S1].",
        previous_semantic_hash="b" * 64,
        current_semantic_hash="c" * 64,
        previous_source_ids=("S2", "S1"),
        current_source_ids=("S3", "S1"),
        previous_repair_keys=("2" * 64,),
        current_repair_keys=("3" * 64,),
    )

    assert context.previous_artifact_id == previous_id
    assert context.added_source_ids == ("S3",)
    assert context.removed_source_ids == ("S2",)
    assert context.added_repair_keys == ("3" * 64,)
    assert context.removed_repair_keys == ("2" * 64,)


def test_publication_only_ioc_and_rules_never_enter_narrative_repair_delta() -> None:
    publication_key = "1" * 64
    narrative_key = "2" * 64
    rule_key = "3" * 64
    extraction = TechnicalExtraction(
        items=(
            ExtractionItem(
                local_id=f"RPA-{publication_key[:16]}",
                category="network_artifacts",
                value="198.51.100.10",
                context="",
                artifact_type=None,
                attack_id=None,
                reference_ids=(),
                source_ids=("S1",),
                supported=True,
                indicator_status=IndicatorStatus.CONFIRMED_IOC,
                provenance=IndicatorProvenance.ANALYST,
                display_policy=DisplayPolicy.IOC_SECTION,
            ),
            ExtractionItem(
                local_id=f"RPA-{narrative_key[:16]}",
                category="tools",
                value="tool.exe",
                context="execution",
                artifact_type=None,
                attack_id=None,
                reference_ids=(),
                source_ids=("S1",),
                supported=True,
                display_policy=DisplayPolicy.BODY_ONLY,
            ),
        )
    )

    keys = narrative_repair_keys(
        extraction,
        {
            "repair_projection": {
                "included_repair_keys": [publication_key, narrative_key, rule_key]
            }
        },
    )

    assert keys == (narrative_key,)


def test_semantic_source_ids_ignore_unreferenced_publication_only_source() -> None:
    pack = {
        "reference_report": {
            "sources": [{"id": "S1"}, {"id": "S2"}],
            "events": [{"source_ids": ["S1"]}],
        },
        "technical_extraction": {
            "items": [{"source_ids": ["S1"]}],
        },
    }

    assert synthesis_semantic_source_ids(pack) == ("S1",)


def test_revision_prompt_keeps_previous_draft_non_authoritative() -> None:
    context = build_synthesis_revision_context(
        previous_artifact_id=uuid4(),
        previous_input_hash="a" * 64,
        previous_text="Ancien fait devenu unsupported [S2].",
        previous_semantic_hash="b" * 64,
        current_semantic_hash="c" * 64,
        previous_source_ids=("S2",),
        current_source_ids=("S1",),
    )
    prompt = ProductionPromptTemplates.get_synthesis_prompt(
        "Sujet",
        '{"reference_report": {"sources": [{"id": "S1"}]}}',
        revision_context=context,
    )

    assert "PREVIOUS_DRAFT_NON_AUTHORITATIVE" in prompt
    assert "Produce a complete synthesis, never a patch" in prompt
    assert "Current evidence is the sole authority" in prompt
    assert "Remove every old fact" in prompt
    assert "IOC/rules publication-only material must not be forced into the prose" in prompt
    assert "Ancien fait devenu unsupported [S2]." in prompt


def test_revision_hash_uses_previous_content_not_artifact_identity() -> None:
    common = {
        "subject_id": uuid4(),
        "references_hash": "a" * 64,
        "reference_report_hash": "b" * 64,
        "extraction_hash": "c" * 64,
        "technical_extraction_hash": "c" * 64,
        "synthesis_evidence_pack_hash": "d" * 64,
        "previous_synthesis_content_hash": synthesis_content_hash("same draft"),
        "current_synthesis_semantic_hash": "e" * 64,
    }

    assert _synthesis_input_hash(**common) == _synthesis_input_hash(**common)
    assert _synthesis_input_hash(
        **{**common, "previous_synthesis_content_hash": synthesis_content_hash("other draft")}
    ) != _synthesis_input_hash(**common)
