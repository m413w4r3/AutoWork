from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.investigation_advisor_tools import InvestigationAdvisorTool
from cti_app.domain.investigation import ToolInvocationStatus

INVESTIGATION_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_unused_advisor_environment_is_absent_from_compose_and_source() -> None:
    root = Path(__file__).resolve().parents[2]
    unused_names = (
        "OPERATOR_ADVISOR_MAX_TURNS_PER_INVESTIGATION",
        "OPERATOR_ADVISOR_MIN_CONSULTATION_INTERVAL_SECONDS",
    )
    for name in unused_names:
        assert name not in (root / "compose.yaml").read_text(encoding="utf-8")
        assert name not in (root / ".env.example").read_text(encoding="utf-8")

    search_roots = (
        root / "backend/src",
        root / "frontend/src",
        root / "scripts",
        root / "workers",
        root / "Makefile",
    )
    for search_root in search_roots:
        if not search_root.exists():
            continue
        files = (search_root,) if search_root.is_file() else search_root.rglob("*")
        for path in files:
            if not path.is_file():
                continue
            try:
                contents = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for name in unused_names:
                assert name not in contents, f"unexpected consumer/reference in {path}"


@pytest.mark.asyncio
async def test_g2_bridge_turn_budget_remains_an_authorization_guard() -> None:
    tool = object.__new__(InvestigationAdvisorTool)
    tool._observations = SimpleNamespace()

    result = await tool._check_advisor_limits(
        INVESTIGATION_ID,
        g2_bridge_turns_remaining=0,
    )

    assert result is not None
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.error_code == "g2_bridge_budget_exhausted"
    assert result.output["advisor_turn_limit"] is None
    assert result.output["g2_bridge_turns_remaining"] == 0
