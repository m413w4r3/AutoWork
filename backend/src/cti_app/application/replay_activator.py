"""
Incrément 4: Replay activation - making a replay timeline operational.

This module handles:
- Validating replay activation preconditions
- Atomic promotion of replay snapshot to active
- Preserving historical editorial bindings
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID
from datetime import UTC, datetime

from cti_app.domain.discovery_cumulative import (
    DiscoverySnapshot,
    DiscoverySnapshotLineage,
    ReplayIdentityMapping,
    ReplayIdentityResolution,
)


class ReplayActivationError(Exception):
    """Raised when replay cannot be activated."""
    pass


class ReplayActivator:
    """
    Atomic promotion of replay snapshot to operational lineage.

    Key principles (D14, Incrément 4):
    - Preconditions validated before any state change
    - Activation is atomic: all-or-nothing
    - Published artifacts keep historical bindings
    - Identity mappings enable transition
    """

    async def validate_activation_preconditions(
        self,
        replay_snapshot: DiscoverySnapshot,
        mappings: dict[UUID, ReplayIdentityMapping],
        published_subject_ids: Sequence[UUID],
    ) -> None:
        """
        Validate that replay can be safely activated.

        Precondition (D14.2):
        - Every published subject must have a mapping
        - No mapping loss during transition
        - Canonical targets must exist in operational

        Args:
            replay_snapshot: The replay snapshot to activate
            mappings: Identity mappings (replay → operational)
            published_subject_ids: All subjects with published artifacts

        Raises:
            ReplayActivationError: If preconditions not met
        """
        mapped_operational = {
            m.operational_subject_id
            for m in mappings.values()
            if m.operational_subject_id is not None
        }

        missing_mappings = set(published_subject_ids) - mapped_operational

        if missing_mappings:
            raise ReplayActivationError(
                f"Published subjects without mapping: {missing_mappings}. "
                "Cannot activate replay without complete mapping coverage."
            )

    async def activate_replay(
        self,
        edition_id: UUID,
        replay_snapshot: DiscoverySnapshot,
        mappings: dict[UUID, ReplayIdentityMapping],
        actor_id: str,
    ) -> DiscoverySnapshot:
        """
        Atomically promote replay snapshot to operational lineage.

        Steps:
        1. Validate preconditions
        2. In transaction:
           a. Update replay_snapshot.is_active = True, lineage = OPERATIONAL
           b. Deactivate current operational snapshot
           c. Record ReplayActivationEvent
           d. Update identity resolution cache

        Key guarantee (D14, D25):
        - Published artifacts keep historical bindings
        - `subject_id`, `snapshot_id`, `evidence_pack_id` unchanged
        - Editorial timeline unaffected
        - Replay improves future knowledge, doesn't rewrite history

        Args:
            edition_id: The edition
            replay_snapshot: The replay snapshot to activate
            mappings: Identity mappings
            actor_id: Actor authorizing activation

        Returns:
            The activated snapshot (now operational, lineage=OPERATIONAL)

        Raises:
            ReplayActivationError: If preconditions fail
        """
        # Validate first
        await self.validate_activation_preconditions(
            replay_snapshot,
            mappings,
            set(),  # Would query published subjects
        )

        # In real implementation, would:
        # 1. Begin transaction
        # 2. Query current_active_snapshot
        # 3. Set current_active.is_active = False
        # 4. Set replay_snapshot.is_active = True
        # 5. Update replay_snapshot.lineage = OPERATIONAL
        # 6. Update DiscoverySubjectIdentity for merged subjects
        # 7. Record audit event
        # 8. Commit or rollback atomically

        raise NotImplementedError("Requires database integration and transactions")
