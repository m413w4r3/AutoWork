"""
Incrément 3: Editorial impact evaluation - determining if new contributions warrant action.

This module handles:
- Distinguishing NEW_EVIDENCE from MATERIAL_UPDATE
- V1 uses deterministic criteria (no LLM)
- Extensible for future LLM-based materiality classification
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from cti_app.domain.editorial import EditorialImpactLevel
from cti_app.domain.discovery_cumulative import SubjectContribution


@dataclass
class ImpactEvaluationContext:
    """Context for evaluating editorial impact of new contributions."""
    subject_id: UUID
    new_contribution_ids: set[UUID]
    all_contributions: dict[UUID, SubjectContribution]


class EditorialImpactEvaluator:
    """
    Evaluates whether new contributions warrant editorial action.

    V1 (deterministic, no LLM):
    - Any new contribution to a subject with an artifact → NEW_EVIDENCE
    - Later, replace with LLM-based classification without changing domain model

    The interface is designed to support future LLM classification by wrapping it,
    without requiring migrations.
    """

    def evaluate(self, context: ImpactEvaluationContext) -> EditorialImpactLevel:
        """
        Determine impact level of new contributions.

        V1 strategy: If there are new contributions covering a subject with
        an artifact, it's NEW_EVIDENCE.

        Args:
            context: Evaluation context with new contributions

        Returns:
            EditorialImpactLevel: NO_CHANGE, NEW_EVIDENCE, or MATERIAL_UPDATE
        """
        if not context.new_contribution_ids:
            return EditorialImpactLevel.NO_CHANGE

        # V1: Any new contribution → NEW_EVIDENCE
        # Future: Replace with LLM materiality classification
        return EditorialImpactLevel.NEW_EVIDENCE

    def evaluate_batch(
        self,
        contexts: list[ImpactEvaluationContext],
    ) -> dict[UUID, EditorialImpactLevel]:
        """
        Evaluate impact for multiple subjects efficiently.

        Args:
            contexts: List of evaluation contexts

        Returns:
            Mapping of subject_id to impact level
        """
        return {ctx.subject_id: self.evaluate(ctx) for ctx in contexts}
