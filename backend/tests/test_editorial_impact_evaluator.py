"""
Tests for editorial impact evaluation.

Incrément 3: Préservation éditoriale
"""

import pytest
from uuid import uuid4

from cti_app.application.editorial_impact_evaluator import (
    EditorialImpactEvaluator,
    ImpactEvaluationContext,
)
from cti_app.domain.editorial import EditorialImpactLevel
from cti_app.domain.discovery_cumulative import SubjectContribution


class TestEditorialImpactEvaluator:
    """Test editorial impact evaluation."""

    def test_no_changes_when_no_new_contributions(self):
        """No new contributions means NO_CHANGE."""
        evaluator = EditorialImpactEvaluator()
        context = ImpactEvaluationContext(
            subject_id=uuid4(),
            new_contribution_ids=set(),
            all_contributions={},
        )

        result = evaluator.evaluate(context)

        assert result == EditorialImpactLevel.NO_CHANGE

    def test_new_evidence_with_contributions(self):
        """V1: Any new contribution is NEW_EVIDENCE."""
        evaluator = EditorialImpactEvaluator()
        context = ImpactEvaluationContext(
            subject_id=uuid4(),
            new_contribution_ids={uuid4(), uuid4()},
            all_contributions={},
        )

        result = evaluator.evaluate(context)

        assert result == EditorialImpactLevel.NEW_EVIDENCE

    def test_batch_evaluation(self):
        """Evaluate multiple subjects efficiently."""
        evaluator = EditorialImpactEvaluator()
        contexts = [
            ImpactEvaluationContext(
                subject_id=uuid4(),
                new_contribution_ids=set(),
                all_contributions={},
            ),
            ImpactEvaluationContext(
                subject_id=uuid4(),
                new_contribution_ids={uuid4()},
                all_contributions={},
            ),
            ImpactEvaluationContext(
                subject_id=uuid4(),
                new_contribution_ids={uuid4(), uuid4(), uuid4()},
                all_contributions={},
            ),
        ]

        results = evaluator.evaluate_batch(contexts)

        assert len(results) == 3
        assert results[contexts[0].subject_id] == EditorialImpactLevel.NO_CHANGE
        assert results[contexts[1].subject_id] == EditorialImpactLevel.NEW_EVIDENCE
        assert results[contexts[2].subject_id] == EditorialImpactLevel.NEW_EVIDENCE
