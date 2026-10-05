from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from cti_app.application.persistence import UnitOfWorkFactory

from .support import ProductionScenario


@pytest.fixture
def production_scenario_factory(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]:
    def factory(sources: Mapping[str, Mapping[str, object]]) -> ProductionScenario:
        return ProductionScenario(uow_factory, tmp_path / "blobs", sources)

    return factory
