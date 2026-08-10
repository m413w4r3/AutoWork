from enum import StrEnum

from cti_app.domain.errors import TlpDowngradeError


class TLP(StrEnum):
    """TLP 2.0 labels ordered from least to most restrictive."""

    CLEAR = "CLEAR"
    GREEN = "GREEN"
    AMBER = "AMBER"
    AMBER_STRICT = "AMBER+STRICT"
    RED = "RED"


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
