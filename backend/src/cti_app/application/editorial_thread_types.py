"""Small result types shared by the editorial thread application seams."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from cti_app.domain.editorial_thread import (
    EditorialThreadJobState,
    EditorialThreadReconciliationOutcome,
)


class EditorialThreadApplicationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EditorialThreadSendResult:
    kind: Literal["accepted", "already_known"]
    thread_id: UUID
    entry_id: UUID
    generation: EditorialThreadJobState


@dataclass(frozen=True, slots=True)
class EditorialThreadSessionStartResult:
    thread_id: UUID
    session_number: int


@dataclass(frozen=True, slots=True)
class EditorialThreadReconciliationResult:
    thread_id: UUID
    outcome: EditorialThreadReconciliationOutcome
    message: str
