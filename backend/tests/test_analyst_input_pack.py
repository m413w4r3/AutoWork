from __future__ import annotations

import hashlib
from datetime import date
from uuid import uuid4

from cti_app.application.analyst_input_pack import (
    ANALYST_INPUT_PACK_BUCKET,
    build_analyst_input_pack_v1,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionProfile,
    SubjectProductionRun,
)


def test_pack_is_canonical_and_uses_only_accepted_structured_file_indicators() -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(), edition_id=uuid4(), profile=ProductionProfile.MAJOR_ASSISTED
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="a" * 64,
    )
    extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="b" * 64,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis, budget=LoopBudget()
    )
    items = [
        {"value": "A" * 64, "indicator_status": "accepted", "source_ids": ["S2", "S1"]},
        {"value": "b" * 64, "indicator_status": "excluded", "source_ids": ["S3"]},
        # A hash-looking sentence is not an accepted structured indicator.
        {"value": "prose " + "c" * 64, "indicator_status": "accepted", "source_ids": ["S4"]},
    ]
    pack = build_analyst_input_pack_v1(
        run=run,
        investigation=investigation,
        synthesis=synthesis,
        extraction_artifacts=(extraction,),
        extraction_items=items,
        tlp="amber",
        do_not_submit=True,
        external_llm_allowed=False,
        research_date=date(2026, 8, 26),
    )
    again = build_analyst_input_pack_v1(
        run=run,
        investigation=investigation,
        synthesis=synthesis,
        extraction_artifacts=(extraction,),
        extraction_items=reversed(items),
        tlp="amber",
        do_not_submit=True,
        external_llm_allowed=False,
        research_date=date(2026, 8, 26),
    )

    assert ANALYST_INPUT_PACK_BUCKET == "analyst-input-packs"
    assert pack.canonical_bytes == again.canonical_bytes
    assert pack.sha256 == hashlib.sha256(pack.canonical_bytes).hexdigest()
    assert pack.payload["file_indicators"] == [
        {"sha256": "a" * 64, "provenance": {"source_ids": ["S1", "S2"], "extraction_item_id": None}}
    ]
    assert pack.payload["policy"] == {
        "tlp": "amber",
        "do_not_submit": True,
        "external_llm_allowed": False,
    }
