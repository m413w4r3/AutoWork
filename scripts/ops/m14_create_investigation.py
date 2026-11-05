#!/usr/bin/env python3
"""OBSOLETE for M14 fresh-stack bootstrap; use --create-investigation instead.

This historical direct-database helper is retained for reference only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from typing import Any
from uuid import UUID

from cti_app.application.analyst_handoff import (
    AnalystHandoffPolicy,
    AnalystPostSynthesisService,
    analyst_handoff_policy_from_sources,
    current_verified_extraction_inputs,
)
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_article_snapshot import (
    ProductionArticleSnapshotAdapter,
)
from cti_app.application.investigation_service import _agenda_from_snapshot
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.investigation import InvestigationExecutionMode, TLPLevel
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from minio import Minio
from sqlalchemy import inspect, text

ACTIVITY_TABLE_TERMS = (
    "invocation",
    "observation",
    "finding",
    "sample",
    "rule",
    "detection_candidate",
)


def make_g2_proposal(
    *, investigation_id: UUID, tlp: str, do_not_submit: bool, external_llm_allowed: bool
) -> dict[str, Any]:
    normalized_tlp = tlp.strip().upper().removeprefix("TLP:")
    allowed = external_llm_allowed and normalized_tlp != TLPLevel.RED.value.upper()
    providers = ["vt", "bridge"] if allowed else []
    return {
        "approved": False,
        "investigation_id": str(investigation_id),
        "allowed_providers": providers,
        "budget_limits": {
            "bridge_turns": 200 if "bridge" in providers else 0,
            "vt_units": 10_000 if "vt" in providers else 0,
            "downloads": 1_000 if "vt" in providers else 0,
        },
        "acquire_hits_allowed": "vt" in providers,
        "disclosure": {
            "tlp": normalized_tlp,
            "do_not_submit": do_not_submit,
            "external_llm_allowed": allowed,
        },
        "approval_route": f"POST /api/investigations/{investigation_id}/envelope/approve",
    }


async def _zero_activity_counts(engine: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.connect() as connection, connection.begin():
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        tables = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names(
                schema="public"
            )
        )
        preparer = connection.dialect.identifier_preparer
        for name in sorted(tables):
            lowered = name.casefold()
            if not any(term in lowered for term in ACTIVITY_TABLE_TERMS):
                continue
            quoted = preparer.quote(name)
            counts[name] = int(
                await connection.scalar(text(f"SELECT count(*) FROM public.{quoted}"))
                or 0
            )
    return counts


async def create_operator_investigation(
    *,
    subject_id: UUID,
    production_run_id: UUID,
    synthesis_artifact_id: UUID,
    synthesis_sha256: str,
    settings: Settings,
) -> dict[str, Any]:
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(blob_store, uow_factory)
    )
    try:
        activity_counts_before = await _zero_activity_counts(engine)
        if any(activity_counts_before.values()):
            raise ValueError("investigation activity exists before handoff creation")
        async with uow_factory() as uow:
            run = await uow.production_runs.get(production_run_id)
            artifact = await uow.production_artifacts.get(synthesis_artifact_id)
            if (
                run is None
                or run.subject_id != subject_id
                or run.id != production_run_id
            ):
                raise ValueError("production run does not belong to subject")
            if (
                artifact is None
                or artifact.subject_id != subject_id
                or artifact.production_run_id != run.id
                or artifact.stage.value != "synthesis"
                or artifact.status.value != "verified"
                or artifact.canonical_blob_id is None
            ):
                raise ValueError("synthesis artifact is not verified for this run")
            payload = await artifact_store.read_json(artifact.canonical_blob_id)
            actual_sha256 = hashlib.sha256(
                artifact_store.canonical_json_bytes(payload)
            ).hexdigest()
            if actual_sha256 != synthesis_sha256:
                raise ValueError("synthesis SHA-256 does not match canonical content")
            input_snapshot = await uow.production_input_snapshots.get_by_run(run.id)
            if input_snapshot is None:
                raise ValueError("production input snapshot is missing")
            (
                extraction_artifacts,
                extraction_items,
            ) = await current_verified_extraction_inputs(
                uow=uow, run=run, artifact_store=artifact_store
            )
            source_policy = analyst_handoff_policy_from_sources(
                input_snapshot.core_sources
            )
            is_red = input_snapshot.subject_tlp.value.casefold() == TLPLevel.RED.value
            policy = AnalystHandoffPolicy(
                tlp=input_snapshot.subject_tlp.value,
                do_not_submit=True if is_red else source_policy.do_not_submit,
                external_llm_allowed=(
                    not is_red
                    and bool(input_snapshot.core_sources)
                    and source_policy.external_llm_allowed
                ),
            )
            existing_investigation = await uow.analyst_investigations.get_for_run(
                run.id
            )
            if (
                existing_investigation is not None
                and await uow.investigation_envelopes.get(existing_investigation.id)
                is not None
            ):
                raise ValueError(
                    "an envelope already exists; refusing to replace G2 policy"
                )

        handoff = await AnalystPostSynthesisService(
            uow_factory,
            artifact_store,
        ).ensure_for_verified_synthesis(
            run=run,
            synthesis=artifact,
            extraction_artifacts=extraction_artifacts,
            extraction_items=extraction_items,
            policy=policy,
        )
        if handoff is None:
            raise ValueError("verified synthesis handoff was not created")

        article_snapshot = await ProductionArticleSnapshotAdapter(
            uow_factory, artifact_store
        ).resolve(synthesis_artifact_id)
        async with uow_factory() as uow:
            existing_state = await uow.investigation_states.get(
                handoff.investigation_id
            )
            if existing_state is not None:
                if (
                    existing_state.execution_mode
                    is not InvestigationExecutionMode.OPERATOR_LED
                ):
                    raise ValueError(
                        "existing investigation execution mode is not operator_led"
                    )
                state = existing_state
            else:
                state = InvestigationWorkingState(
                    investigation_id=handoff.investigation_id,
                    agenda=_agenda_from_snapshot(article_snapshot),
                    base_publication_artifact_id=article_snapshot.base_publication_artifact_id,
                    base_publication_sha256=article_snapshot.base_publication_sha256,
                    execution_mode=InvestigationExecutionMode.OPERATOR_LED,
                )
                await uow.investigation_states.save_if_version(
                    state, expected_version=None
                )
            if (
                await uow.investigation_envelopes.get(handoff.investigation_id)
                is not None
            ):
                raise ValueError(
                    "an envelope already exists; refusing to replace G2 policy"
                )
            await uow.commit()

        activity_counts = await _zero_activity_counts(engine)
        populated = {name: count for name, count in activity_counts.items() if count}
        if populated:
            raise ValueError("unexpected investigation activity rows exist")
        external_allowed = (
            article_snapshot.external_llm_allowed
            and article_snapshot.tlp.casefold() != TLPLevel.RED.value
        )
        return {
            "subject_id": str(subject_id),
            "production_run_id": str(production_run_id),
            "synthesis_artifact_id": str(synthesis_artifact_id),
            "synthesis_sha256": actual_sha256,
            "investigation_id": str(handoff.investigation_id),
            "execution_mode": state.execution_mode.value,
            "g2_proposal": make_g2_proposal(
                investigation_id=handoff.investigation_id,
                tlp=article_snapshot.tlp,
                do_not_submit=article_snapshot.do_not_submit,
                external_llm_allowed=external_allowed,
            ),
            "activity_table_counts": activity_counts,
            "activity_table_counts_before": activity_counts_before,
            "start_request_after_approval": {
                "method": "POST",
                "path": f"/api/subjects/{subject_id}/investigations",
                "body": {
                    "production_run_id": str(production_run_id),
                    "synthesis_artifact_id": str(synthesis_artifact_id),
                    "synthesis_sha256": actual_sha256,
                    "execution_mode": "operator_led",
                },
            },
        }
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject-id", type=UUID, required=True)
    parser.add_argument("--production-run-id", type=UUID, required=True)
    parser.add_argument("--synthesis-artifact-id", type=UUID, required=True)
    parser.add_argument("--synthesis-sha256", required=True)
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    try:
        result = await create_operator_investigation(
            subject_id=args.subject_id,
            production_run_id=args.production_run_id,
            synthesis_artifact_id=args.synthesis_artifact_id,
            synthesis_sha256=args.synthesis_sha256,
            settings=Settings(_env_file=None),
        )
    except Exception as exc:  # noqa: BLE001 - output is intentionally limited to error type.
        print(
            json.dumps({"status": "error", "error": type(exc).__name__}, sort_keys=True)
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
