from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from cti_app.application import production_workflow
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import get_settings

from .support import ProductionScenario


@pytest.fixture(autouse=True)
def configure_production_evidence_gate_for_integration_suite(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Keep business-pipeline integration fixtures focused on their contract.

    These suites use minimal scripted extractions while exercising collection,
    retries, restart safety, editorial enrichment and pipeline persistence.
    Their assertions are not about evidence sufficiency; the dedicated
    PostgreSQL gate suite opts into the production threshold and covers that
    contract directly.
    """
    threshold = 4 if request.node.get_closest_marker("production_evidence_gate") else 0
    runtime_settings = get_settings().model_copy(
        update={"production_min_direct_evidence_items": threshold}
    )
    monkeypatch.setattr(production_workflow, "get_settings", lambda: runtime_settings)


@pytest.fixture
def production_scenario_factory(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]:
    def factory(sources: Mapping[str, Mapping[str, object]]) -> ProductionScenario:
        return ProductionScenario(uow_factory, tmp_path / "blobs", sources)

    return factory
