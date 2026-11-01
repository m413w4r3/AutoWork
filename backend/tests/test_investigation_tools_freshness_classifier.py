"""Exhaustive classification of tools allowed to use non-blocking local freshness."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cti_app.application.investigation_tools import ToolEffect, uses_nonblocking_local_freshness

_STRICT_NAMES = (
    "hypothesis.declare",
    "experiment.record",
    "finding.propose",
    "finding.revise",
    "opinion.compose",
    "opinion.revise",
    "opinion.note",
    "rule.propose",
    "rule.propose_bridge",
    "rule.operator_syntax_fix",
    "rule.revise",
    "rule.propose_from_candidates",
    "sample.set_qualification",
    "archive.extract_member",
)
_NONBLOCKING_NAMES = (
    "assembly.inspect",
    "types.list",
    "methods.list",
    "method.decompile",
    "references.imports",
    "strings.list",
    "resources.list",
    "local.run_command",
    "ghidra.decompile_function",
    "sample.analyze",
    "experiment.list",
    "rule.test",
    "evidence.get",
)


def _definition(effect: ToolEffect, capability_key: str = "investigation.local") -> SimpleNamespace:
    return SimpleNamespace(effect=effect, capability_key=capability_key)


@pytest.mark.parametrize("effect", (ToolEffect.LOCAL_READ, ToolEffect.LOCAL_COMPUTE))
@pytest.mark.parametrize("tool_name", _STRICT_NAMES)
def test_analytic_writers_never_use_nonblocking_freshness(
    tool_name: str, effect: ToolEffect
) -> None:
    assert uses_nonblocking_local_freshness(tool_name, _definition(effect)) is False


@pytest.mark.parametrize("effect", (ToolEffect.LOCAL_READ, ToolEffect.LOCAL_COMPUTE))
@pytest.mark.parametrize("tool_name", _NONBLOCKING_NAMES)
def test_independent_local_tools_use_nonblocking_freshness(
    tool_name: str, effect: ToolEffect
) -> None:
    assert uses_nonblocking_local_freshness(tool_name, _definition(effect)) is True


@pytest.mark.parametrize(
    "effect",
    (
        ToolEffect.EXTERNAL_READ,
        ToolEffect.ACQUIRE,
        ToolEffect.EXTERNAL_ACTIVE,
        ToolEffect.EXTERNAL_SUBMIT,
    ),
)
def test_non_local_effects_and_write_capability_stay_strict(effect: ToolEffect) -> None:
    assert uses_nonblocking_local_freshness("strings.list", _definition(effect)) is False
    assert (
        uses_nonblocking_local_freshness(
            "strings.list", _definition(ToolEffect.LOCAL_COMPUTE, "operator.write")
        )
        is False
    )
    assert uses_nonblocking_local_freshness("strings.list", None) is False
