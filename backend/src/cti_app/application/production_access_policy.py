"""Shared conservative access-policy merge for production model inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cti_app.domain.classification import TLP


@dataclass(frozen=True, slots=True)
class SourceAccessPolicy:
    """The diffusion policy governing one exact archived document."""

    tlp: TLP
    sensitivity: str
    external_llm_allowed: bool
    do_not_submit: bool

    @property
    def submission_allowed(self) -> bool:
        return self.external_llm_allowed and not self.do_not_submit


def resolve_source_policy(document: Any, collection: Any | None) -> SourceAccessPolicy:
    """Combine document and owning-collection policy without loosening either."""

    tlp = document.tlp
    external_llm_allowed = document.external_llm_allowed
    do_not_submit = document.do_not_submit
    sensitivity = "internal"
    if collection is not None:
        tlp = max((document.tlp, collection.source_tlp), key=lambda item: tuple(TLP).index(item))
        external_llm_allowed = external_llm_allowed and collection.external_llm_allowed
        do_not_submit = do_not_submit or collection.do_not_submit
        sensitivity = collection.sensitivity or sensitivity
    return SourceAccessPolicy(
        tlp=tlp,
        sensitivity=sensitivity,
        external_llm_allowed=external_llm_allowed,
        do_not_submit=do_not_submit,
    )
