"""Canonical PostgreSQL seed helpers for the workflow emulator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from uuid import uuid4

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import DiscoveryBatch, DiscoverySourceMode
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from tests.discovery_support import make_discovery_run_for_edition

from .providers import CaptureBundle, discovery_candidate


async def persist_discovery_fixture(
    uow_factory: UnitOfWorkFactory,
    edition: Edition,
    *,
    batch_label: str,
    subject_labels: Sequence[str],
    bundles: Mapping[str, CaptureBundle],
) -> DiscoveryBatch:
    """Persist one deterministic Discovery output using canonical repositories."""
    discovery_run = await make_discovery_run_for_edition(
        uow_factory,
        edition,
        complementary_axis=f"workflow-{batch_label}",
        actor_id="workflow-analyst",
    )
    model_run_id = uuid4()
    fixture_identity = f"workflow-discovery-v1:{edition.id}:{batch_label}:{uuid4()}"
    model_run = ModelRun(
        id=model_run_id,
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="workflow-discovery-fixture-v1",
        actual_model_version="workflow-discovery-fixture-v1",
        prompt_template_id="workflow-discovery-fixture-v1",
        prompt_template_version="1",
        authorized_input_hash=sha256(f"input:{fixture_identity}".encode()).hexdigest(),
        evidence_pack_hash=sha256(f"evidence:{fixture_identity}".encode()).hexdigest(),
        parameters={"fixture_version": "workflow-discovery-v1"},
    )
    batch = DiscoveryBatch(
        edition_id=edition.id,
        request_hash=sha256(f"{fixture_identity}:request".encode()).hexdigest(),
        complementary_axis=f"workflow-{batch_label}",
        queries=(f"synthetic {', '.join(subject_labels)}",),
        citations=(),
        discovery_run_id=discovery_run.id,
        discovery_model_run_id=model_run_id,
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
        parser_version="workflow-discovery-v1",
        candidates=[discovery_candidate(label, bundles[label]) for label in subject_labels],
        source_mode=DiscoverySourceMode.NATIVE_COMPLETE,
        source_coverage_complete=True,
        source_coverage_incomplete_reason=None,
    )
    async with uow_factory() as uow:
        await uow.model_runs.add(model_run)
        assert await uow.discovery_batches.add_if_absent(batch)
        await uow.commit()
    return batch
