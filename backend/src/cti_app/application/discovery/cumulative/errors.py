from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from .contracts import ReconcileDiscoveryParameters


class DiscoverySnapshotStaleError(RuntimeError):
    """The snapshot a merge was planned against is no longer the edition state.

    `replan` carries the parameters of the reconciliation that should take this
    plan's place. A reviewed plan names subjects by handles resolved against its
    parent snapshot, so once that parent is superseded the plan is unusable and
    the contribution has to be planned again from the current state.
    """

    def __init__(
        self, reason: str, *, replan: ReconcileDiscoveryParameters | None = None
    ) -> None:
        super().__init__(reason)
        self.replan = replan


class DiscoveryMergeNeedsReview(RuntimeError):
    def __init__(self, run_id: UUID, reasons: Sequence[str]) -> None:
        super().__init__(", ".join(reasons))
        self.run_id = run_id
        self.reasons = tuple(reasons)


class MergePlanInvalidError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        merge_model_run_id: UUID | None,
        raw_output_reference: str | None,
        normalized_output_reference: str | None,
    ) -> None:
        super().__init__(message)
        self.merge_model_run_id = merge_model_run_id
        self.raw_output_reference = raw_output_reference
        self.normalized_output_reference = normalized_output_reference


class MergeModelUnavailableError(RuntimeError):
    """The merge model never produced an answer — nothing was planned at all.

    Distinct from MergePlanInvalidError on purpose: a stalled bridge is a
    transient incident to retry, whereas a malformed plan is a real answer the
    reviewer can be shown. Conflating them persists an empty merge run that no
    human can resolve, and it silently blocks every later contribution.
    """

    def __init__(self, message: str, *, merge_model_run_id: UUID | None, code: str) -> None:
        super().__init__(message)
        self.merge_model_run_id = merge_model_run_id
        self.code = code
