from __future__ import annotations

import pytest

from tests.editorial.fixtures.model_adapters import forbid_network_egress


@pytest.fixture(autouse=True)
def editorial_tests_deny_network_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network_egress(monkeypatch)
