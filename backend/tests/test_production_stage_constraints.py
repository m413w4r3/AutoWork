"""The hard-coded stage CHECK lists must follow the domain enums."""

from __future__ import annotations

import re

from cti_app.domain.production import ProductionArtifactStage, ProductionStage
from cti_app.infrastructure.database.models import production as model


def _values(sql: str) -> set[str]:
    return set(re.findall(r"'([^']+)'", sql))


def test_run_stage_check_lists_every_production_stage() -> None:
    assert _values(model.PRODUCTION_STAGE_VALUES_SQL) == {stage.value for stage in ProductionStage}


def test_artifact_stage_check_lists_every_artifact_stage() -> None:
    assert _values(model.PRODUCTION_ARTIFACT_STAGE_VALUES_SQL) == {
        stage.value for stage in ProductionArtifactStage
    }


def test_reuse_stage_check_is_every_artifact_stage_but_publication() -> None:
    assert _values(model.PRODUCTION_REUSE_STAGE_VALUES_SQL) == {
        stage.value
        for stage in ProductionArtifactStage
        if stage is not ProductionArtifactStage.PUBLICATION
    }
