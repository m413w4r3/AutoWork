from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from cti_app.domain.errors import TlpDowngradeError


class TLP(StrEnum):
    """TLP 2.0 labels ordered from least to most restrictive."""

    CLEAR = "CLEAR"
    GREEN = "GREEN"
    AMBER = "AMBER"
    AMBER_STRICT = "AMBER+STRICT"
    RED = "RED"


@dataclass(frozen=True, slots=True)
class DerivedPolicy:
    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool


_TLP_RANK = {
    TLP.CLEAR: 0,
    TLP.GREEN: 1,
    TLP.AMBER: 2,
    TLP.AMBER_STRICT: 3,
    TLP.RED: 4,
}


def ensure_tlp_not_downgraded(current: TLP, requested: TLP) -> None:
    if _TLP_RANK[requested] < _TLP_RANK[current]:
        raise TlpDowngradeError(f"TLP downgrade from {current} to {requested} is forbidden")


def derived_policy(members: Iterable[object]) -> DerivedPolicy:
    """Derive the aggregate diffusion policy from members using the canonical fields."""
    members = tuple(members)
    if not members:
        raise ValueError("At least one member is required to derive a policy")
    return DerivedPolicy(
        tlp=max((member.tlp for member in members), key=_TLP_RANK.__getitem__),  # type: ignore[attr-defined]
        do_not_submit=any(member.do_not_submit for member in members),  # type: ignore[attr-defined]
        external_llm_allowed=all(member.external_llm_allowed for member in members),  # type: ignore[attr-defined]
    )
