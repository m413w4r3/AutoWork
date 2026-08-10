from dataclasses import dataclass
from typing import Literal, Protocol

DependencyState = Literal["ok", "unavailable"]


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    status: DependencyState
    detail: str | None = None


class ReadinessChecker(Protocol):
    async def check(self) -> dict[str, DependencyStatus]: ...


async def evaluate_readiness(checker: ReadinessChecker) -> dict[str, DependencyStatus]:
    """Run the typed readiness port without coupling the API to concrete services."""

    return await checker.check()
