"""
Incrément 4: Snapshot statistics for frontend display and edition state tracking.

This module calculates aggregate stats about discovery state at snapshot time.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID
from datetime import UTC, datetime

from cti_app.domain.discovery_cumulative import DiscoveryInputMode


@dataclass(frozen=True)
class SnapshotStats:
    """
    Aggregate statistics about a discovery snapshot at a point in time.

    Used for:
    - Edition dashboard (subject count, freshness)
    - Merge run view (impact assessment)
    - Frontend state display
    """
    snapshot_version: int
    subject_count: int
    intake_count: int
    merge_run_count: int
    last_update_mode: DiscoveryInputMode | None
    pending_merge_count: int
    needs_review_count: int
    update_available_count: int


class SnapshotStatsCalculator:
    """
    Calculate statistics for a snapshot or edition.

    Stats are expensive to compute (many joins), so they're cached
    and invalidated on snapshot activation.

    Incrément 4: These stats drive the frontend edition view.
    """

    async def calculate_snapshot_stats(
        self,
        edition_id: UUID,
        snapshot_id: UUID,
    ) -> SnapshotStats:
        """
        Calculate stats for a specific snapshot.

        In real implementation, would:
        1. Query snapshot by id
        2. Count subjects with status=ACTIVE
        3. Count intakes leading to this snapshot
        4. Count merge runs in this snapshot
        5. Query last intake for input_mode
        6. Count merge runs with status=NEEDS_REVIEW not yet applied
        7. Count artifacts with UPDATE_AVAILABLE signal

        Args:
            edition_id: The edition
            snapshot_id: The snapshot ID

        Returns:
            SnapshotStats with computed values

        Raises:
            ValueError: If snapshot not found
        """
        raise NotImplementedError("Requires database integration")

    async def calculate_edition_stats(
        self,
        edition_id: UUID,
    ) -> SnapshotStats:
        """
        Calculate stats for the active snapshot of an edition.

        Args:
            edition_id: The edition

        Returns:
            Stats for the currently active snapshot
        """
        raise NotImplementedError("Requires database integration")

    def invalidate_cache(self, edition_id: UUID, snapshot_id: UUID) -> None:
        """
        Invalidate cached stats for a snapshot.

        Called when a snapshot is activated or when a merge run is created.

        In real implementation, would:
        - Delete cache entry from Redis or memory cache
        - Trigger UI update if live

        Args:
            edition_id: The edition
            snapshot_id: The snapshot
        """
        # In real implementation: cache.delete(f"stats:{edition_id}:{snapshot_id}")
        pass
