from __future__ import annotations

from uuid import uuid4

from cti_app.application.discovery_manual_source_edits import (
    MANUAL_SOURCE_EDIT_VERSION,
    _build_manual_edit_batch,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import CandidateTopic


def _candidate() -> CandidateTopic:
    return CandidateTopic(
        title="Candidate",
        summary="Summary.",
        novelty="Novel.",
        technical_potential=1,
        uncertainties=(),
        relevance_reasons=(),
        actors=(),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


def test_build_manual_edit_batch_uses_manual_source_edit_version() -> None:
    batch, _digest = _build_manual_edit_batch(
        edition_id=uuid4(),
        subject_id=uuid4(),
        incomplete_source_id=uuid4(),
        url="https://example.com/report",
        candidate=_candidate(),
    )

    assert MANUAL_SOURCE_EDIT_VERSION == "manual-url-attach-v1"
    assert batch.parser_version == "manual-url-attach-v1"
