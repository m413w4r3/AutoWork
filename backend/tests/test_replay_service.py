"""
Tests for replay service - creating alternative snapshot chains.

Incrément 4: Replay and lineage
"""

from uuid import uuid4

import pytest

from cti_app.application.replay_service import ReplayService
from cti_app.domain.discovery_cumulative import (
    ReplayIdentityResolution,
)


class TestReplayService:
    """Test replay edition discovery."""

    @pytest.mark.asyncio
    async def test_replay_service_initialization(self) -> None:
        """ReplayService can be instantiated."""
        service = ReplayService()
        assert service is not None

    def test_generate_comparison_report_all_same(self) -> None:
        """Generate report when all subjects are identical."""
        # This would need actual implementation
        # comparison = await service.generate_comparison_report(
        #     replay_snapshot, operational_snapshot, mappings
        # )
        # assert comparison.subjects_same_count == 3


class TestReplayIdentityMapping:
    """Test replay identity mapping constraints."""

    def test_same_requires_operational_subject(self) -> None:
        """SAME resolution requires operational_subject_id."""
        from cti_app.domain.discovery_cumulative import ReplayIdentityMapping

        with pytest.raises(ValueError, match="SAME resolution requires"):
            ReplayIdentityMapping(
                replay_run_id=uuid4(),
                replay_subject_id=uuid4(),
                operational_subject_id=None,  # Invalid for SAME
                resolution=ReplayIdentityResolution.SAME,
                actor_id="test",
            )

    def test_new_forbids_operational_subject(self) -> None:
        """NEW resolution must not have operational_subject_id."""
        from cti_app.domain.discovery_cumulative import ReplayIdentityMapping

        with pytest.raises(ValueError, match="NEW resolution must not have"):
            ReplayIdentityMapping(
                replay_run_id=uuid4(),
                replay_subject_id=uuid4(),
                operational_subject_id=uuid4(),  # Invalid for NEW
                resolution=ReplayIdentityResolution.NEW,
                actor_id="test",
            )

    def test_valid_mappings(self) -> None:
        """Valid mappings can be created."""
        from cti_app.domain.discovery_cumulative import ReplayIdentityMapping

        # SAME with operational_subject
        mapping1 = ReplayIdentityMapping(
            replay_run_id=uuid4(),
            replay_subject_id=uuid4(),
            operational_subject_id=uuid4(),
            resolution=ReplayIdentityResolution.SAME,
            actor_id="test",
        )
        assert mapping1.operational_subject_id is not None

        # NEW without operational_subject
        mapping2 = ReplayIdentityMapping(
            replay_run_id=uuid4(),
            replay_subject_id=uuid4(),
            operational_subject_id=None,
            resolution=ReplayIdentityResolution.NEW,
            actor_id="test",
        )
        assert mapping2.operational_subject_id is None
