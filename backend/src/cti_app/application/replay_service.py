"""
Incrément 4: Replay edition discovery - creating alternative snapshot chains.

This module handles:
- Replaying discovery ingestion with different merge parameters
- Creating REPLAY lineage snapshots
- Computing identity mappings between replay and operational
- Generating comparison reports
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID
from datetime import UTC, datetime

from cti_app.domain.discovery_cumulative import (
    DiscoverySnapshot,
    DiscoverySnapshotLineage,
    DiscoveryPlannerKind,
    DiscoveryIntake,
    ReplayIdentityMapping,
    ReplayIdentityResolution,
    ReplayComparison,
)


class ReplayService:
    """
    Service for replaying discovery timeline with different merge strategies.

    Key principles (D14, Incrément 4):
    - Replay creates a separate REPLAY lineage snapshot chain
    - Replay never auto-activates; requires explicit operator action
    - Activation requires mapping for all published subject identities (D14.2)
    - Published artifacts keep historical bindings (snapshot_id, evidence_pack_id)
    - Replay improves future state, doesn't rewrite past
    """

    async def replay_edition_discovery(
        self,
        edition_id: UUID,
        prompt_version: str,
        policy_version: str,
        blocking_version: str,
        actor_id: str,
    ) -> tuple[DiscoverySnapshot, str]:
        """
        Replay an edition's discovery timeline with new merge parameters.

        Creates a new snapshot chain marked as REPLAY lineage, allowing
        parallel evolution without affecting the operational chain.

        Args:
            edition_id: The edition to replay
            prompt_version: Merge prompt version to use
            policy_version: Merge policy version
            blocking_version: Blocking strategy version
            actor_id: Actor initiating the replay

        Returns:
            Tuple of (final_replay_snapshot, replay_run_id)
            Snapshot is marked lineage=REPLAY and not activated

        Process:
        1. Fetch all intakes for edition (ordered by sequence)
        2. Replay merge from first intake through latest
        3. Create snapshots along the way, marked REPLAY
        4. Return final snapshot (not active yet)
        5. Operator reviews and activates via activate_replay()
        """
        # In real implementation, would:
        # 1. Query DiscoveryIntake.all_by_edition(edition_id) sorted by sequence
        # 2. Re-run merge pipeline with new prompt/policy/blocking versions
        # 3. Create DiscoverySnapshot with lineage=REPLAY
        # 4. Return final snapshot
        raise NotImplementedError("Requires database integration")

    async def calculate_identity_mapping(
        self,
        replay_snapshot: DiscoverySnapshot,
        operational_snapshot: DiscoverySnapshot,
        published_subject_ids: set[UUID],
    ) -> dict[UUID, ReplayIdentityMapping]:
        """
        Map replay subject identities to operational identities.

        Handles three cases:
        1. SAME: replay_origin_key == operational_origin_key
           → automatic, same subject_id
        2. SPLIT_OF / MERGE_OF: manual mapping required
           → operator provides mapping
        3. NEW: Subject only exists in replay
           → optional mapping if it overlaps published subjects

        Args:
            replay_snapshot: The replay snapshot
            operational_snapshot: The current operational snapshot
            published_subject_ids: Subjects with published artifacts

        Returns:
            Mapping of replay_subject_id → ReplayIdentityMapping

        Raises:
            ValueError: If published subject has no mapping
        """
        mappings: dict[UUID, ReplayIdentityMapping] = {}

        # In real implementation, would:
        # 1. Compare origin_keys between snapshots
        # 2. Auto-map SAME cases
        # 3. Query operator-provided mappings for complex cases
        # 4. Validate coverage for published subjects
        raise NotImplementedError("Requires database integration")

    async def generate_comparison_report(
        self,
        replay_snapshot: DiscoverySnapshot,
        operational_snapshot: DiscoverySnapshot,
        mappings: dict[UUID, ReplayIdentityMapping],
    ) -> ReplayComparison:
        """
        Generate a summary of changes between replay and operational.

        Args:
            replay_snapshot: Replay result
            operational_snapshot: Current operational snapshot
            mappings: Identity mappings

        Returns:
            ReplayComparison with counts and impact assessment
        """
        same_count = 0
        split_count = 0
        merged_count = 0
        new_count = 0
        impacting_editorial = 0

        for mapping in mappings.values():
            match mapping.resolution:
                case ReplayIdentityResolution.SAME:
                    same_count += 1
                case ReplayIdentityResolution.SPLIT_OF:
                    split_count += 1
                case ReplayIdentityResolution.MERGE_OF:
                    merged_count += 1
                case ReplayIdentityResolution.NEW:
                    new_count += 1

        # Count subjects affecting published artifacts
        # In real implementation, would query editorial artifacts
        # impacting_editorial = len(query_editorial_subjects(mappings))

        return ReplayComparison(
            replay_run_id=UUID("00000000-0000-0000-0000-000000000000"),  # Placeholder
            edition_id=operational_snapshot.edition_id,
            subjects_same_count=same_count,
            subjects_split_count=split_count,
            subjects_merged_count=merged_count,
            subjects_created_count=new_count,
            subjects_impacting_editorial=impacting_editorial,
        )
