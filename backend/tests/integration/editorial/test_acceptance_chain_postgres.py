"""Chained end-to-end acceptance oracle for the M23 editorial workshop (F5).

The module drives the real FastAPI app wiring (manual routes, the generic typed
enrichment-revision route and the single accept writer) on PostgreSQL, with one
ARTICLE and one BRIEF parametrization, and checks after every step that:

* the re-fetched server snapshot exposes the expected element order/placement,
  exactly one enrichment version bump and no duplicate occurrence;
* the derived PDF preview is STALE and a regenerate re-assembles the publication
  from the current pins before the REAL Typst compiler renders it;
* the extracted PDF text places every element marker in snapshot order;
* archived blobs and older canonical artifact versions are untouched;
* no model run or gateway call happens during an editorial operation.

The suite is deliberately one chain per kind: the point is the interaction
between identity, order, adoption, assembly and rendering, not another set of
single-operation oracles (those live in the sibling modules referenced by
``docs/design/atelier-editorial/ACCEPTANCE.md``). The tail of the module adds
the E15/E16/E17/E21/E22 proofs that do not fit inside the chain.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    DiagramSpecV1,
    editorial_enrichment_from_json,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.database.models.model_execution import ModelRunRow
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from tests.integration.editorial.acceptance_support import (
    AcceptanceSeed,
    seed_acceptance_workspace,
)
from tests.integration.editorial.test_enrichment_chart_diagram_revision_postgres import (
    BASE_DIAGRAM_ASSET,
)
from tests.integration.editorial.test_enrichment_chart_diagram_revision_postgres import (
    _ready_artifact as _ready_chart_artifact,
)
from tests.integration.editorial.test_enrichment_revision_route_postgres import (
    _revise_chart_body,
)
from tests.integration.editorial.test_enrichment_visual_revision_postgres import (
    _enrichment_block,
    _read_enrichment,
)

pytestmark = pytest.mark.integration

_BASE_URL = "http://editorial.test"
_ELEMENT_KINDS = frozenset({"table", "chart", "figure", "diagram"})

TABLE_TWO_KEY = "acceptance_table"
TABLE_TWO_TITLE = "Table marker acceptance"
CHART_KEY = "synthetic_endpoint_counts"
CHART_CAPTION = "Chart marker endpoints"
CHART_REVISED_CAPTION = "Chart legend corrected marker"
FIGURE_ONE_CAPTION = "Figure marker alpha"
FIGURE_TWO_CAPTION = "Figure marker beta"
FIGURE_TWO_REVISED_CAPTION = "Second image caption amended"
FIGURE_THREE_CAPTION = "Figure marker gamma"
FIGURE_REPLACED_CAPTION = "Replacement image marker"
CONCURRENT_CAPTION = "Concurrent caption marker"


@dataclass(frozen=True)
class _Expected:
    """Expected snapshot projection after one accepted operation."""

    keys: tuple[str, ...]
    anchors: dict[str, str | None]
    markers: dict[str, str]
    absent_markers: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _snapshot(client: httpx.AsyncClient, subject_id: UUID) -> dict[str, Any]:
    response = await client.get(f"/api/subjects/{subject_id}/editor/snapshot")
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _post_revision(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    occurrence_id: UUID,
    body: dict[str, object],
) -> dict[str, Any]:
    response = await client.post(
        f"/api/subjects/{subject_id}/editor/blocks/{occurrence_id}/enrichment-revisions",
        headers={"Idempotency-Key": f"visual-revision-{uuid4().hex}"},
        json=body,
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _accept(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    occurrence_id: UUID,
    proposal: dict[str, Any],
) -> httpx.Response:
    return await client.post(
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/"
        f"proposals/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"accept-{uuid4().hex}"},
        json={
            "expected_state_version": proposal["state_version"],
            "patch_sha256": proposal["patch_sha256"],
        },
    )


async def _accept_ok(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    occurrence_id: UUID,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    response = await _accept(
        client,
        subject_id=subject_id,
        occurrence_id=occurrence_id,
        proposal=proposal,
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _regenerate_pdf(
    client: httpx.AsyncClient, subject_id: UUID, uow_factory: Any
) -> tuple[dict[str, Any], bytes]:
    stale = await client.get(f"/api/subjects/{subject_id}/editor/pdf")
    assert stale.status_code == 200, stale.text
    assert stale.json()["status"] == "STALE", stale.text
    regenerated = await client.post(f"/api/subjects/{subject_id}/editor/pdf/regenerate")
    assert regenerated.status_code == 200, regenerated.text
    ready = cast(dict[str, Any], regenerated.json())
    if ready["status"] != "READY":
        probe = await _snapshot(client, subject_id)
        render_probe: dict[str, object] = {}
        if probe.get("pdf_artifact_id"):
            async with uow_factory() as uow:
                render = await uow.publication_renders.get_latest_for_artifact(
                    UUID(str(probe["pdf_artifact_id"]))
                )
            if render is not None:
                render_probe = {
                    "status": render.status.value,
                    "error_code": render.error_code,
                    "error_message": render.error_message,
                }
        raise AssertionError(
            {
                "ready": ready,
                "render": render_probe,
                "snapshot": {
                    key: probe.get(key)
                    for key in (
                        "preview_status",
                        "pdf_status",
                        "pdf_artifact_id",
                        "pdf_artifact_version",
                        "publication_artifact_id",
                        "publication_version",
                        "enrichment_artifact_id",
                        "enrichment_version",
                        "impact_status",
                    )
                },
                "revisions": [
                    (item["element_key"], item["revision_status"]) for item in probe["enrichments"]
                ],
            }
        )
    assert ready["pdf_url"]
    pdf = await client.get(ready["pdf_url"])
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF-")
    return ready, pdf.content


def _pdf_text(pdf_bytes: bytes, tmp_path: Path, *, label: str) -> str:
    pdf_path = tmp_path / f"{label}.pdf"
    pdf_path.write_bytes(pdf_bytes)
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        raise AssertionError("Poppler pdftotext is required for the F5 acceptance oracle")
    result = subprocess.run(
        (pdftotext, "-layout", str(pdf_path), "-"),
        capture_output=True,
        check=True,
        timeout=30,
    )
    return " ".join(result.stdout.decode("utf-8", errors="replace").split()).casefold()


# ---------------------------------------------------------------------------
# Snapshot / PDF assertions
# ---------------------------------------------------------------------------


def _element_marker(block: dict[str, Any]) -> str:
    kind = str(block["kind"])
    if kind == "table":
        return str(block["table"]["title"])
    if kind in {"chart", "figure"}:
        return str(block["caption"])
    if kind == "diagram":
        return str(block["diagram"]["title"])
    raise AssertionError(f"unexpected enrichment kind {kind}")


def _element_anchor(block: dict[str, Any]) -> str | None:
    kind = str(block["kind"])
    payload = block.get(kind)
    if not isinstance(payload, dict):
        raise AssertionError(f"missing {kind} projection")
    anchor = payload.get("anchor_block_id")
    return str(anchor) if anchor is not None else None


def _element_keys(snapshot: dict[str, Any]) -> list[str]:
    return [str(block["element_key"]) for block in snapshot["enrichments"]]


def _element(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    matches = [block for block in snapshot["enrichments"] if block["element_key"] == key]
    assert len(matches) == 1, (key, [block["element_key"] for block in snapshot["enrichments"]])
    return cast(dict[str, Any], matches[0])


def _assert_snapshot_projection(snapshot: dict[str, Any], expected: _Expected) -> None:
    keys = _element_keys(snapshot)
    assert keys == list(expected.keys)
    assert len(keys) == len(set(keys))
    block_ids = [block["block_id"] for block in snapshot["enrichments"]]
    assert len(block_ids) == len(set(block_ids))
    ordered_blocks = [block for block in snapshot["blocks"] if block["kind"] in _ELEMENT_KINDS]
    assert [block["element_key"] for block in ordered_blocks] == list(expected.keys)
    for block in snapshot["enrichments"]:
        assert block["revision_status"] == "ready", block
    for key, anchor in expected.anchors.items():
        assert _element_anchor(_element(snapshot, key)) == anchor, key
    for key, marker in expected.markers.items():
        assert _element_marker(_element(snapshot, key)) == marker, key


def _assert_pdf_projection(
    text: str, snapshot: dict[str, Any], *, absent: tuple[str, ...] = ()
) -> None:
    markers = [_element_marker(block) for block in snapshot["enrichments"]]
    for marker in markers:
        count = text.count(marker.casefold())
        assert count == 1, (marker, count)
    positions = [text.index(marker.casefold()) for marker in markers]
    assert positions == sorted(positions), (markers, positions)
    for marker in absent:
        assert marker.casefold() not in text, marker


# ---------------------------------------------------------------------------
# Persistence assertions
# ---------------------------------------------------------------------------


async def _model_run_count(migrated_postgres_url: str) -> int:
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            return int((await session.scalar(select(func.count()).select_from(ModelRunRow))) or 0)
    finally:
        await engine.dispose()


async def _enrichment_versions(uow_factory: Any, run_id: UUID) -> list[int]:
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(run_id)
    return sorted(
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    )


async def _verify_enrichment_history(
    app: Any,
    uow_factory: Any,
    run_id: UUID,
    recorded: dict[int, str],
) -> dict[int, str]:
    """Every old canonical version stays byte-identical and parseable."""
    store = cast(ProductionArtifactStore, app.state.production_artifact_store)
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(run_id)
    seen = dict(recorded)
    for artifact in artifacts:
        if artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT:
            continue
        assert artifact.canonical_blob_id is not None
        payload_bytes = await store.read_bytes(artifact.canonical_blob_id)
        sha256 = hashlib.sha256(payload_bytes).hexdigest()
        if artifact.version in seen:
            assert seen[artifact.version] == sha256, artifact.version
        else:
            seen[artifact.version] = sha256
        editorial_enrichment_from_json(await store.read_json(artifact.canonical_blob_id))
    return seen


async def _archived_blobs(
    uow_factory: Any, archived_blob_shas: tuple[tuple[UUID, str], ...]
) -> None:
    async with uow_factory() as uow:
        for blob_id, expected_sha in archived_blob_shas:
            blob = await uow.blobs.get(blob_id)
            assert blob is not None
            assert blob.descriptor.sha256 == expected_sha


async def _publication_artifact(uow_factory: Any, artifact_id: UUID) -> Any:
    async with uow_factory() as uow:
        artifact = await uow.production_artifacts.get(artifact_id)
    assert artifact is not None
    return artifact


async def _assert_publication_pins(
    seed: AcceptanceSeed,
    uow_factory: Any,
    *,
    ready: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    artifact = await _publication_artifact(uow_factory, UUID(ready["artifact_id"]))
    store = cast(ProductionArtifactStore, seed.app.state.production_artifact_store)
    assert artifact.canonical_blob_id is not None
    canonical = await store.read_bytes(artifact.canonical_blob_id)
    assert ready["artifact_canonical_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert ready["artifact_input_hash"] == artifact.input_hash
    input_artifacts = artifact.metadata.get("input_artifacts")
    assert isinstance(input_artifacts, dict)
    assert input_artifacts["synthesis_artifact_id"] == snapshot["synthesis_artifact_id"]
    assert input_artifacts["editorial_enrichment_artifact_id"] == snapshot["enrichment_artifact_id"]


# ---------------------------------------------------------------------------
# Manual-route operation helpers
# ---------------------------------------------------------------------------


async def _create_table(
    client: httpx.AsyncClient,
    seed: AcceptanceSeed,
    *,
    anchor_id: UUID,
    key: str,
    title: str,
    caption: str | None,
    evidence_ref: dict[str, str],
    snapshot: dict[str, Any],
) -> None:
    response = await client.post(
        f"/api/subjects/{seed.seeded.subject.id}/editor/blocks/{anchor_id}/table-proposals",
        headers={"Idempotency-Key": f"manual-table-{uuid4().hex}"},
        json={
            "snapshot_id": snapshot["snapshot_id"],
            "anchor_block_id": str(anchor_id),
            "table": {
                "key": key,
                "kind": "custom",
                "title": title,
                "caption": caption,
                "columns": [
                    {"key": "finding", "label": "Finding"},
                    {"key": "source", "label": "Source detail"},
                ],
                "rows": [
                    {
                        "cells": ["Synthetic finding", "Synthetic test context."],
                        "evidence_refs": [evidence_ref],
                    },
                    {
                        "cells": [
                            "Synthetic finding",
                            "The synthetic report records this finding.",
                        ],
                        "evidence_refs": [evidence_ref],
                    },
                ],
                "purpose": {
                    "question": "Which acceptance finding is documented?",
                    "available_data": "The synthetic finding and its cited context.",
                    "comprehension_gain": "Compare the finding with its source context.",
                    "scope": "Only the synthetic finding and its source context.",
                    "evidence_refs": [evidence_ref],
                    "knowledge_limits": "No other source detail is represented.",
                    "placement_reason": "Keep the table after its anchor paragraph.",
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    await _accept_ok(
        client,
        subject_id=seed.seeded.subject.id,
        occurrence_id=anchor_id,
        proposal=response.json(),
    )


def _chart_request(
    evidence_pack: Any,
    *,
    title: str,
    caption: str,
) -> dict[str, Any]:
    handles = {
        str(record["value"]): str(record["handle"])
        for record in evidence_pack.narrative_evidence
        if record.get("value") in {"Alpha: 12 endpoints", "Beta: 18 endpoints"}
    }
    alpha = evidence_pack.resolve_handle(handles["Alpha: 12 endpoints"])
    beta = evidence_pack.resolve_handle(handles["Beta: 18 endpoints"])

    def ref(reference: Any) -> dict[str, str]:
        return {
            "source_document_id": str(reference.source_document_id),
            "kind": reference.kind.value,
            "evidence_key": reference.evidence_key,
        }

    return {
        "key": CHART_KEY,
        "title": title,
        "caption": caption,
        "category_axis_label": "Catégorie",
        "value_axis_label": "Nombre rapporté",
        "unit": "endpoints",
        "points": [
            {
                "label": "Alpha",
                "series": "endpoints",
                "value": 12,
                "evidence_refs": [ref(alpha)],
            },
            {
                "label": "Beta",
                "series": "endpoints",
                "value": 18,
                "evidence_refs": [ref(beta)],
            },
        ],
        "purpose": {
            "question": "Quels nombres d'endpoints sont rapportés pour Alpha et Beta ?",
            "available_data": "Deux valeurs exactes sont citées pour ces catégories.",
            "comprehension_gain": "La comparaison rend visible l'écart entre les valeurs.",
            "scope": "Uniquement Alpha et Beta dans la fixture de test.",
            "evidence_refs": [ref(alpha), ref(beta)],
            "knowledge_limits": "Aucune autre catégorie n'est extrapolée.",
            "placement_reason": "Placer le graphique après le paragraphe choisi.",
        },
    }


async def _create_chart(
    client: httpx.AsyncClient,
    seed: AcceptanceSeed,
    *,
    anchor_id: UUID,
    snapshot: dict[str, Any],
) -> None:
    response = await client.post(
        f"/api/subjects/{seed.seeded.subject.id}/editor/blocks/{anchor_id}/bar-chart-proposals",
        headers={"Idempotency-Key": f"manual-bar-chart-{uuid4().hex}"},
        json={
            "snapshot_id": snapshot["snapshot_id"],
            "anchor_block_id": str(anchor_id),
            "chart": _chart_request(
                seed.enrichment_content.evidence_pack,
                title="Comptes synthétiques rapportés",
                caption=CHART_CAPTION,
            ),
        },
    )
    assert response.status_code == 200, response.text
    await _accept_ok(
        client,
        subject_id=seed.seeded.subject.id,
        occurrence_id=anchor_id,
        proposal=response.json(),
    )


async def _catalog_items(client: httpx.AsyncClient, subject_id: UUID) -> list[dict[str, Any]]:
    response = await client.get(f"/api/subjects/{subject_id}/editor/figures?status=available")
    assert response.status_code == 200, response.text
    return cast(list[dict[str, Any]], response.json()["items"])


async def _figure_proposal(
    client: httpx.AsyncClient,
    seed: AcceptanceSeed,
    *,
    anchor_id: UUID,
    snapshot: dict[str, Any],
    figure_id: str,
    caption: str,
) -> httpx.Response:
    return await client.post(
        f"/api/subjects/{seed.seeded.subject.id}/editor/blocks/{anchor_id}/figure-proposals",
        headers={"Idempotency-Key": f"manual-figure-{uuid4().hex}"},
        json={
            "snapshot_id": snapshot["snapshot_id"],
            "anchor_block_id": str(anchor_id),
            "figures": [
                {
                    "figure_id": figure_id,
                    "caption": caption,
                    "order": 0,
                }
            ],
        },
    )


async def _insert_figure(
    client: httpx.AsyncClient,
    seed: AcceptanceSeed,
    *,
    anchor_id: UUID,
    snapshot: dict[str, Any],
    caption: str,
) -> None:
    items = await _catalog_items(client, seed.seeded.subject.id)
    assert items, "no selectable archived figure"
    response = await _figure_proposal(
        client,
        seed,
        anchor_id=anchor_id,
        snapshot=snapshot,
        figure_id=str(items[0]["figure_id"]),
        caption=caption,
    )
    assert response.status_code == 200, response.text
    await _accept_ok(
        client,
        subject_id=seed.seeded.subject.id,
        occurrence_id=anchor_id,
        proposal=response.json(),
    )


# ---------------------------------------------------------------------------
# One verified chain step
# ---------------------------------------------------------------------------


async def _verify_step(
    client: httpx.AsyncClient,
    seed: AcceptanceSeed,
    uow_factory: Any,
    *,
    migrated_postgres_url: str,
    baseline_model_runs: int,
    tmp_path: Path,
    label: str,
    expected: _Expected,
    enrichment_version_before: int,
    publication_version_before: int,
    artifact_shas: dict[int, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[int, str]]:
    subject_id = seed.seeded.subject.id
    snapshot = await _snapshot(client, subject_id)
    assert snapshot["enrichment_version"] == enrichment_version_before + 1
    _assert_snapshot_projection(snapshot, expected)

    versions = await _enrichment_versions(uow_factory, seed.seeded.run.id)
    assert versions == list(range(1, enrichment_version_before + 2))
    artifact_shas = await _verify_enrichment_history(
        seed.app, uow_factory, seed.seeded.run.id, artifact_shas
    )

    ready, pdf_bytes = await _regenerate_pdf(client, subject_id, uow_factory)
    assert ready["artifact_version"] == publication_version_before + 1
    # Regeneration assembles a new publication, so the current workspace pin
    # changes too. Re-fetch the server snapshot: it is both the document the
    # reviewer sees after reload and the ``snapshot_id`` the next route call
    # must carry.
    snapshot = await _snapshot(client, subject_id)
    _assert_snapshot_projection(snapshot, expected)
    assert int(snapshot["publication_version"]) == int(ready["artifact_version"])
    await _assert_publication_pins(seed, uow_factory, ready=ready, snapshot=snapshot)

    text = _pdf_text(pdf_bytes, tmp_path, label=label)
    _assert_pdf_projection(text, snapshot, absent=expected.absent_markers)

    await _archived_blobs(uow_factory, seed.archived_blob_shas)
    assert await _model_run_count(migrated_postgres_url) == baseline_model_runs
    assert seed.model_gateway.calls == []
    return snapshot, ready, artifact_shas


# ---------------------------------------------------------------------------
# The chained acceptance test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication_kind", "expected_kind"),
    ((PublicationKind.ARTICLE, "article"), (PublicationKind.BRIEF, "brief")),
    ids=("article", "brief"),
)
async def test_acceptance_chain_postgres(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    publication_kind: PublicationKind,
    expected_kind: str,
) -> None:
    seed = await seed_acceptance_workspace(
        uow_factory, monkeypatch, publication_kind=publication_kind
    )
    # Discovery-lineage seeding may legitimately persist upstream model runs;
    # the editorial operations below must not add any.
    baseline_model_runs = await _model_run_count(migrated_postgres_url)
    subject_id = seed.seeded.subject.id
    evidence = seed.enrichment_content.base_enrichment.tables[0].rows[0].evidence_refs[0]
    evidence_ref = {
        "source_document_id": str(evidence.source_document_id),
        "kind": evidence.kind.value,
        "evidence_key": evidence.evidence_key,
    }
    artifact_shas: dict[int, str] = {}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=seed.app), base_url=_BASE_URL
    ) as client:
        snapshot = await _snapshot(client, subject_id)
        assert snapshot["publication_kind"] == expected_kind
        paragraphs = [
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        ]
        assert len(paragraphs) == 8
        paragraph_ids = [str(block["block_id"]) for block in paragraphs]
        assert _element_keys(snapshot) == ["synthetic_table"]
        assert snapshot["enrichment_version"] == 2
        assert snapshot["publication_version"] == 2
        assert snapshot["capabilities"]["read_only_reason"] is None
        assert await _model_run_count(migrated_postgres_url) == baseline_model_runs
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(snapshot["publication_version"])

        # 1. Create a second table after paragraph 1 (1-based numbering).
        await _create_table(
            client,
            seed,
            anchor_id=UUID(paragraph_ids[0]),
            key=TABLE_TWO_KEY,
            title=TABLE_TWO_TITLE,
            caption=None,
            evidence_ref=evidence_ref,
            snapshot=snapshot,
        )
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-table",
            expected=_Expected(
                keys=("acceptance_table", "synthetic_table"),
                anchors={TABLE_TWO_KEY: paragraph_ids[0], "synthetic_table": None},
                markers={TABLE_TWO_KEY: TABLE_TWO_TITLE, "synthetic_table": "Synthetic findings"},
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        # 2. Create the bar chart after paragraph 3.
        await _create_chart(
            client,
            seed,
            anchor_id=UUID(paragraph_ids[2]),
            snapshot=snapshot,
        )
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-chart",
            expected=_Expected(
                keys=("acceptance_table", "synthetic_table", CHART_KEY),
                anchors={
                    TABLE_TWO_KEY: paragraph_ids[0],
                    "synthetic_table": None,
                    CHART_KEY: paragraph_ids[2],
                },
                markers={
                    TABLE_TWO_KEY: TABLE_TWO_TITLE,
                    "synthetic_table": "Synthetic findings",
                    CHART_KEY: CHART_CAPTION,
                },
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        # 3. Insert the first figure after paragraph 2. It shares the
        #    after-lead boundary with the seeded table; the shared order rule
        #    breaks that tie on the element key.
        await _insert_figure(
            client,
            seed,
            anchor_id=UUID(paragraph_ids[1]),
            snapshot=snapshot,
            caption=FIGURE_ONE_CAPTION,
        )
        snapshot = await _snapshot(client, subject_id)
        figure_one_key = next(
            block["element_key"] for block in snapshot["enrichments"] if block["kind"] == "figure"
        )
        tied = sorted(["synthetic_table", str(figure_one_key)])
        expected_keys = (TABLE_TWO_KEY, *tied, CHART_KEY)
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-figure-one",
            expected=_Expected(
                keys=expected_keys,
                anchors={
                    TABLE_TWO_KEY: paragraph_ids[0],
                    "synthetic_table": None,
                    str(figure_one_key): paragraph_ids[1],
                    CHART_KEY: paragraph_ids[2],
                },
                markers={
                    TABLE_TWO_KEY: TABLE_TWO_TITLE,
                    "synthetic_table": "Synthetic findings",
                    str(figure_one_key): FIGURE_ONE_CAPTION,
                    CHART_KEY: CHART_CAPTION,
                },
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        # 4. Insert the second figure after paragraph 4.
        await _insert_figure(
            client,
            seed,
            anchor_id=UUID(paragraph_ids[3]),
            snapshot=snapshot,
            caption=FIGURE_TWO_CAPTION,
        )
        snapshot = await _snapshot(client, subject_id)
        figure_keys = [
            block["element_key"] for block in snapshot["enrichments"] if block["kind"] == "figure"
        ]
        figure_two_key = next(key for key in figure_keys if key != figure_one_key)
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-figure-two",
            expected=_Expected(
                keys=(*expected_keys, str(figure_two_key)),
                anchors={
                    TABLE_TWO_KEY: paragraph_ids[0],
                    "synthetic_table": None,
                    str(figure_one_key): paragraph_ids[1],
                    CHART_KEY: paragraph_ids[2],
                    str(figure_two_key): paragraph_ids[3],
                },
                markers={
                    TABLE_TWO_KEY: TABLE_TWO_TITLE,
                    "synthetic_table": "Synthetic findings",
                    str(figure_one_key): FIGURE_ONE_CAPTION,
                    CHART_KEY: CHART_CAPTION,
                    str(figure_two_key): FIGURE_TWO_CAPTION,
                },
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        # 5. Insert the third figure on the same anchor as the second one.
        if publication_kind is PublicationKind.BRIEF:
            # E15 at prepare: one request asking for two more figures while two
            # are already integrated crosses the ceiling by name, before any
            # version or blob write.
            available_before_cap = await _catalog_items(client, subject_id)
            assert len(available_before_cap) == 2
            refused = await client.post(
                f"/api/subjects/{subject_id}/editor/blocks/{paragraph_ids[3]}/figure-proposals",
                headers={"Idempotency-Key": f"manual-figure-cap-{uuid4().hex}"},
                json={
                    "snapshot_id": snapshot["snapshot_id"],
                    "anchor_block_id": paragraph_ids[3],
                    "figures": [
                        {
                            "figure_id": str(item["figure_id"]),
                            "caption": f"Figure marker refused {index}",
                            "order": index,
                        }
                        for index, item in enumerate(available_before_cap)
                    ],
                },
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["detail"]["code"] == "brief_figure_limit_exceeded"
            unchanged = await _snapshot(client, subject_id)
            assert unchanged["enrichment_version"] == enrichment_version
        await _insert_figure(
            client,
            seed,
            anchor_id=UUID(paragraph_ids[3]),
            snapshot=snapshot,
            caption=FIGURE_THREE_CAPTION,
        )
        snapshot = await _snapshot(client, subject_id)
        figure_keys = [
            block["element_key"] for block in snapshot["enrichments"] if block["kind"] == "figure"
        ]
        figure_three_key = next(
            key for key in figure_keys if key not in {figure_one_key, figure_two_key}
        )
        anchor_map = {
            TABLE_TWO_KEY: paragraph_ids[0],
            "synthetic_table": None,
            str(figure_one_key): paragraph_ids[1],
            CHART_KEY: paragraph_ids[2],
            str(figure_two_key): paragraph_ids[3],
            str(figure_three_key): paragraph_ids[3],
        }
        marker_map = {
            TABLE_TWO_KEY: TABLE_TWO_TITLE,
            "synthetic_table": "Synthetic findings",
            str(figure_one_key): FIGURE_ONE_CAPTION,
            CHART_KEY: CHART_CAPTION,
            str(figure_two_key): FIGURE_TWO_CAPTION,
            str(figure_three_key): FIGURE_THREE_CAPTION,
        }
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-figure-three",
            expected=_Expected(
                keys=(*expected_keys, str(figure_two_key), str(figure_three_key)),
                anchors=anchor_map,
                markers=marker_map,
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        if publication_kind is PublicationKind.BRIEF:
            # E15 at the cap: the anchor no longer advertises figure insertion
            # and a forged attempt is refused without a canonical write.
            capped_anchor = next(
                block for block in snapshot["blocks"] if block["block_id"] == paragraph_ids[3]
            )
            assert "create_source_figure" not in capped_anchor["allowed_operations"]
            available = await _catalog_items(client, subject_id)
            assert len(available) == 1
            refused = await _figure_proposal(
                client,
                seed,
                anchor_id=UUID(paragraph_ids[3]),
                snapshot=snapshot,
                figure_id=str(available[0]["figure_id"]),
                caption="Figure marker refused",
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["detail"]["code"] in {
                "brief_figure_limit_exceeded",
                "proposal_operation_unsupported",
            }
            after_refusal = await _snapshot(client, subject_id)
            assert after_refusal["enrichment_version"] == enrichment_version

        # 6. Revise the chart labels and caption in place (one chart only).
        _artifact, payload = await _read_enrichment(seed.app, uow_factory, seed.seeded.run.id)
        base_chart = editorial_enrichment_from_json(payload).charts[0]
        corrected_labels = tuple(f"{point.label} (corrigé)" for point in base_chart.points)
        chart_block = _enrichment_block(snapshot, kind="chart")
        revised_chart_body = _revise_chart_body(
            snapshot["snapshot_id"], base_chart, labels=corrected_labels
        )
        revised_chart_request = cast(dict[str, Any], revised_chart_body["chart"])
        revised_chart_request["caption"] = CHART_REVISED_CAPTION
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(chart_block["block_id"]),
            body=revised_chart_body,
        )
        await _accept_ok(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(chart_block["block_id"]),
            proposal=proposal,
        )
        marker_map = {
            TABLE_TWO_KEY: TABLE_TWO_TITLE,
            "synthetic_table": "Synthetic findings",
            str(figure_one_key): FIGURE_ONE_CAPTION,
            CHART_KEY: CHART_REVISED_CAPTION,
            str(figure_two_key): FIGURE_TWO_CAPTION,
            str(figure_three_key): FIGURE_THREE_CAPTION,
        }
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-chart-revised",
            expected=_Expected(
                keys=(*expected_keys, str(figure_two_key), str(figure_three_key)),
                anchors=anchor_map,
                markers=marker_map,
                absent_markers=(CHART_CAPTION,),
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])
        _artifact, payload = await _read_enrichment(seed.app, uow_factory, seed.seeded.run.id)
        revised_chart = editorial_enrichment_from_json(payload).charts[0]
        assert len(editorial_enrichment_from_json(payload).charts) == 1
        assert [point.label for point in revised_chart.points] == list(corrected_labels)
        assert revised_chart.compiled_asset_id != base_chart.compiled_asset_id

        # 7. Update only the second figure caption.
        figure_two_block = _enrichment_block(snapshot, kind="figure", key=str(figure_two_key))
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_two_block["block_id"]),
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "update_visual_caption",
                "element_kind": "figure",
                "element_key": str(figure_two_key),
                "caption": FIGURE_TWO_REVISED_CAPTION,
            },
        )
        await _accept_ok(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_two_block["block_id"]),
            proposal=proposal,
        )
        marker_map = {
            **marker_map,
            str(figure_two_key): FIGURE_TWO_REVISED_CAPTION,
        }
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-caption",
            expected=_Expected(
                keys=(*expected_keys, str(figure_two_key), str(figure_three_key)),
                anchors=anchor_map,
                markers=marker_map,
                absent_markers=(FIGURE_TWO_CAPTION,),
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        # 8. Move the first figure from after paragraph 2 to after paragraph 5.
        figure_one_block = _enrichment_block(snapshot, kind="figure", key=str(figure_one_key))
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_one_block["block_id"]),
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "move_enrichment_element",
                "element_kind": "figure",
                "element_key": str(figure_one_key),
                "anchor_block_id": paragraph_ids[4],
            },
        )
        await _accept_ok(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_one_block["block_id"]),
            proposal=proposal,
        )
        anchor_map = {**anchor_map, str(figure_one_key): paragraph_ids[4]}
        snapshot = await _snapshot(client, subject_id)
        moved_keys = [
            str(block["element_key"])
            for block in snapshot["enrichments"]
            if block["element_key"] in {figure_one_key, figure_two_key, figure_three_key}
        ]
        expected_after_move = (
            TABLE_TWO_KEY,
            "synthetic_table",
            CHART_KEY,
            str(figure_two_key),
            str(figure_three_key),
            str(figure_one_key),
        )
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-move",
            expected=_Expected(
                keys=expected_after_move,
                anchors=anchor_map,
                markers=marker_map,
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        assert moved_keys == [str(figure_two_key), str(figure_three_key), str(figure_one_key)]
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        # 9. Reorder the two figures sharing paragraph 4 (invert their order).
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(paragraph_ids[3]),
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "reorder_enrichment_elements",
                "anchor_block_id": paragraph_ids[3],
                "order": [
                    {"kind": "figure", "key": str(figure_three_key)},
                    {"kind": "figure", "key": str(figure_two_key)},
                ],
            },
        )
        await _accept_ok(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(paragraph_ids[3]),
            proposal=proposal,
        )
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-reorder",
            expected=_Expected(
                keys=(
                    TABLE_TWO_KEY,
                    "synthetic_table",
                    CHART_KEY,
                    str(figure_three_key),
                    str(figure_two_key),
                    str(figure_one_key),
                ),
                anchors=anchor_map,
                markers=marker_map,
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])

        # 10. Replace the second figure with the remaining archived figure.
        figure_two_block = _enrichment_block(snapshot, kind="figure", key=str(figure_two_key))
        available = await _catalog_items(client, subject_id)
        spare = next(item for item in available if item.get("selectable"))
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_two_block["block_id"]),
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "replace_source_figure",
                "element_key": str(figure_two_key),
                "figure_id": spare["figure_id"],
                "caption": FIGURE_REPLACED_CAPTION,
            },
        )
        await _accept_ok(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_two_block["block_id"]),
            proposal=proposal,
        )
        marker_map = {**marker_map, str(figure_two_key): FIGURE_REPLACED_CAPTION}
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-replace",
            expected=_Expected(
                keys=(
                    TABLE_TWO_KEY,
                    "synthetic_table",
                    CHART_KEY,
                    str(figure_three_key),
                    str(figure_two_key),
                    str(figure_one_key),
                ),
                anchors=anchor_map,
                markers=marker_map,
                absent_markers=(FIGURE_TWO_REVISED_CAPTION,),
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        enrichment_version = int(snapshot["enrichment_version"])
        publication_version = int(ready["artifact_version"])
        _artifact, payload = await _read_enrichment(seed.app, uow_factory, seed.seeded.run.id)
        assert len(editorial_enrichment_from_json(payload).source_figures) == 3

        # 11. Remove the third figure; its caption disappears from the PDF.
        figure_three_block = _enrichment_block(snapshot, kind="figure", key=str(figure_three_key))
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_three_block["block_id"]),
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "remove_enrichment_element",
                "element_kind": "figure",
                "element_key": str(figure_three_key),
            },
        )
        await _accept_ok(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(figure_three_block["block_id"]),
            proposal=proposal,
        )
        marker_map = {key: marker for key, marker in marker_map.items() if key != figure_three_key}
        anchor_map = {key: value for key, value in anchor_map.items() if key != figure_three_key}
        snapshot, ready, artifact_shas = await _verify_step(
            client,
            seed,
            uow_factory,
            migrated_postgres_url=migrated_postgres_url,
            baseline_model_runs=baseline_model_runs,
            tmp_path=tmp_path,
            label=f"chain-{expected_kind}-remove",
            expected=_Expected(
                keys=(
                    TABLE_TWO_KEY,
                    "synthetic_table",
                    CHART_KEY,
                    str(figure_two_key),
                    str(figure_one_key),
                ),
                anchors=anchor_map,
                markers=marker_map,
                absent_markers=(FIGURE_THREE_CAPTION,),
            ),
            enrichment_version_before=enrichment_version,
            publication_version_before=publication_version,
            artifact_shas=artifact_shas,
        )
        assert _element_keys(snapshot).count(str(figure_three_key)) == 0
        # Removal and replacement free their previous archive entries for reuse;
        # the archived blobs themselves are asserted unchanged above.
        assert len(await _catalog_items(client, subject_id)) == 2

    # The final artifact history is contiguous and every old blob is intact.
    final_versions = await _enrichment_versions(uow_factory, seed.seeded.run.id)
    assert final_versions == list(range(1, final_versions[-1] + 1))
    assert await _model_run_count(migrated_postgres_url) == baseline_model_runs
    assert seed.model_gateway.calls == []


# ---------------------------------------------------------------------------
# E15 — ARTICLE has no figure ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_article_integrates_four_figures_postgres(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_acceptance_workspace(
        uow_factory, monkeypatch, publication_kind=PublicationKind.ARTICLE
    )
    baseline_model_runs = await _model_run_count(migrated_postgres_url)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=seed.app), base_url=_BASE_URL
    ) as client:
        snapshot = await _snapshot(client, seed.seeded.subject.id)
        paragraphs = [
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        ]
        anchors = [UUID(block["block_id"]) for block in paragraphs]
        for index, caption in enumerate(
            (
                FIGURE_ONE_CAPTION,
                FIGURE_TWO_CAPTION,
                FIGURE_THREE_CAPTION,
                FIGURE_REPLACED_CAPTION,
            )
        ):
            await _insert_figure(
                client,
                seed,
                anchor_id=anchors[1 + index],
                snapshot=snapshot,
                caption=caption,
            )
            snapshot = await _snapshot(client, seed.seeded.subject.id)
        figures = [block for block in snapshot["enrichments"] if block["kind"] == "figure"]
        assert len(figures) == 4
        assert len({block["element_key"] for block in figures}) == 4
        assert int(snapshot["enrichment_version"]) == 6
    assert await _enrichment_versions(uow_factory, seed.seeded.run.id) == [1, 2, 3, 4, 5, 6]
    assert await _model_run_count(migrated_postgres_url) == baseline_model_runs
    assert seed.model_gateway.calls == []


# ---------------------------------------------------------------------------
# E16 — concurrent accept on the same enrichment version has one winner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication_kind", "expected_kind"),
    ((PublicationKind.ARTICLE, "article"), (PublicationKind.BRIEF, "brief")),
    ids=("article", "brief"),
)
async def test_concurrent_accept_has_one_winner_postgres(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    publication_kind: PublicationKind,
    expected_kind: str,
) -> None:
    from cti_app.domain.editorial_proposal import EditorialProposalState

    seed = await seed_acceptance_workspace(
        uow_factory, monkeypatch, publication_kind=publication_kind
    )
    baseline_model_runs = await _model_run_count(migrated_postgres_url)
    subject_id = seed.seeded.subject.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=seed.app), base_url=_BASE_URL
    ) as client:
        snapshot = await _snapshot(client, subject_id)
        assert snapshot["publication_kind"] == expected_kind
        paragraphs = [
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        ]
        base_table = _enrichment_block(snapshot, kind="table")
        base_caption = base_table["caption"]
        base_anchor = _element_anchor(base_table)
        occurrence_id = UUID(base_table["block_id"])

        move = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "move_enrichment_element",
                "element_kind": "table",
                "element_key": base_table["element_key"],
                "anchor_block_id": str(paragraphs[0]["block_id"]),
            },
        )
        caption = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "update_visual_caption",
                "element_kind": "table",
                "element_key": base_table["element_key"],
                "caption": CONCURRENT_CAPTION,
            },
        )
        responses = await asyncio.gather(
            _accept(
                client,
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                proposal=move,
            ),
            _accept(
                client,
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                proposal=caption,
            ),
        )
        statuses = sorted(response.status_code for response in responses)
        assert statuses == [200, 409], [response.text for response in responses]
        loser = next(response for response in responses if response.status_code == 409)
        # The thread accept route exposes the adoption writer's closed conflict
        # code verbatim: the losing sibling was staled inside the winner's
        # transaction (``proposal_state_conflict``), or its base pins moved.
        assert loser.json()["detail"]["code"] in {
            "proposal_state_conflict",
            "proposal_base_stale",
            "proposal_stale",
        }
        after = await _snapshot(client, subject_id)

    assert int(after["enrichment_version"]) == 3
    table = _element(after, base_table["element_key"])
    moved_won = _element_anchor(table) == str(paragraphs[0]["block_id"])
    if moved_won:
        assert table["caption"] == base_caption
    else:
        assert _element_anchor(table) == base_anchor
        assert table["caption"] == CONCURRENT_CAPTION
    assert len(after["enrichments"]) == 1
    assert await _enrichment_versions(uow_factory, seed.seeded.run.id) == [1, 2, 3]
    async with uow_factory() as uow:
        stored = [
            await uow.editorial_proposals.get_for_occurrence(
                subject_id, occurrence_id, UUID(proposal["id"])
            )
            for proposal in (move, caption)
        ]
    states = [item.state for item in stored if item is not None]
    assert states.count(EditorialProposalState.ACCEPTED) == 1
    assert states.count(EditorialProposalState.PROPOSED) == 0
    assert await _model_run_count(migrated_postgres_url) == baseline_model_runs
    assert seed.model_gateway.calls == []


# ---------------------------------------------------------------------------
# E17 — rejecting a visual proposal leaves the canonical chart unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication_kind", "expected_kind"),
    ((PublicationKind.ARTICLE, "article"), (PublicationKind.BRIEF, "brief")),
    ids=("article", "brief"),
)
async def test_rejected_visual_proposal_leaves_canonical_unchanged_postgres(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    publication_kind: PublicationKind,
    expected_kind: str,
) -> None:
    from cti_app.domain.editorial_proposal import EditorialProposalState

    seed = await seed_acceptance_workspace(
        uow_factory, monkeypatch, publication_kind=publication_kind
    )
    baseline_model_runs = await _model_run_count(migrated_postgres_url)
    subject_id = seed.seeded.subject.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=seed.app), base_url=_BASE_URL
    ) as client:
        snapshot = await _snapshot(client, subject_id)
        assert snapshot["publication_kind"] == expected_kind
        paragraphs = [
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        ]
        await _create_chart(
            client, seed, anchor_id=UUID(paragraphs[2]["block_id"]), snapshot=snapshot
        )
        snapshot = await _snapshot(client, subject_id)
        chart_block = _enrichment_block(snapshot, kind="chart")
        version_before = int(snapshot["enrichment_version"])
        publication_before = int(snapshot["publication_version"])
        _artifact, payload_before = await _read_enrichment(
            seed.app, uow_factory, seed.seeded.run.id
        )
        payload_sha_before = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(payload_before)
        ).hexdigest()
        base_chart = editorial_enrichment_from_json(payload_before).charts[0]
        revised_labels = tuple(f"{point.label} proposé" for point in base_chart.points)
        body = _revise_chart_body(snapshot["snapshot_id"], base_chart, labels=revised_labels)
        cast(dict[str, Any], body["chart"])["caption"] = CHART_REVISED_CAPTION
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(chart_block["block_id"]),
            body=body,
        )
        rejected = await client.post(
            f"/api/subjects/{subject_id}/editor/threads/{chart_block['block_id']}/"
            f"proposals/{proposal['id']}/reject",
            headers={"Idempotency-Key": f"reject-{uuid4().hex}"},
            json={
                "expected_state_version": proposal["state_version"],
                "patch_sha256": proposal["patch_sha256"],
            },
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["state"] == "rejected"
        after = await _snapshot(client, subject_id)

    assert int(after["enrichment_version"]) == version_before
    assert int(after["publication_version"]) == publication_before
    _artifact, payload_after = await _read_enrichment(seed.app, uow_factory, seed.seeded.run.id)
    assert (
        hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(payload_after)).hexdigest()
        == payload_sha_before
    )
    chart = editorial_enrichment_from_json(payload_after).charts[0]
    assert chart.caption == CHART_CAPTION
    assert [point.label for point in chart.points] == [point.label for point in base_chart.points]
    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            subject_id, UUID(chart_block["block_id"]), UUID(proposal["id"])
        )
        proposals = await uow.editorial_proposals.list_for_subject(
            subject_id, occurrence_id=None, before=None, limit=100
        )
    assert stored is not None and stored.state is EditorialProposalState.REJECTED
    assert all(
        item.state is not EditorialProposalState.ACCEPTED
        for item in proposals
        if item.id == UUID(proposal["id"])
    )
    assert await _model_run_count(migrated_postgres_url) == baseline_model_runs
    assert seed.model_gateway.calls == []


# ---------------------------------------------------------------------------
# E21 — a historical publication version is read-only and non-mutating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication_kind", "expected_kind"),
    ((PublicationKind.ARTICLE, "article"), (PublicationKind.BRIEF, "brief")),
    ids=("article", "brief"),
)
async def test_historical_publication_version_is_read_only_postgres(
    uow_factory: Any,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    publication_kind: PublicationKind,
    expected_kind: str,
) -> None:
    seed = await seed_acceptance_workspace(
        uow_factory, monkeypatch, publication_kind=publication_kind
    )
    baseline_model_runs = await _model_run_count(migrated_postgres_url)
    subject_id = seed.seeded.subject.id
    store = cast(ProductionArtifactStore, seed.app.state.production_artifact_store)
    evidence = seed.enrichment_content.base_enrichment.tables[0].rows[0].evidence_refs[0]
    evidence_ref = {
        "source_document_id": str(evidence.source_document_id),
        "kind": evidence.kind.value,
        "evidence_key": evidence.evidence_key,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=seed.app), base_url=_BASE_URL
    ) as client:
        snapshot = await _snapshot(client, subject_id)
        assert snapshot["publication_kind"] == expected_kind
        paragraphs = [
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        ]
        await _create_table(
            client,
            seed,
            anchor_id=UUID(paragraphs[0]["block_id"]),
            key=TABLE_TWO_KEY,
            title=TABLE_TWO_TITLE,
            caption=None,
            evidence_ref=evidence_ref,
            snapshot=snapshot,
        )
        ready, _pdf = await _regenerate_pdf(client, subject_id, uow_factory)
        current = await _snapshot(client, subject_id)
        assert int(current["publication_version"]) == int(ready["artifact_version"])

        async with uow_factory() as uow:
            publications = [
                artifact
                for artifact in await uow.production_artifacts.list_for_run(seed.seeded.run.id)
                if artifact.stage is ProductionArtifactStage.PUBLICATION
            ]
            snapshots = await uow.editorial_workspace_snapshots.list_for_run(seed.seeded.run.id)
        historical = next(artifact for artifact in publications if artifact.version == 2)
        assert historical.canonical_blob_id is not None
        historical_bytes = await store.read_bytes(historical.canonical_blob_id)
        historical_sha = hashlib.sha256(historical_bytes).hexdigest()
        historical_snapshot = next(
            record for record in snapshots if record.publication_artifact_id == historical.id
        )

        before_artifacts = sorted(
            (artifact.id, artifact.version, artifact.canonical_blob_id)
            for artifact in await _all_artifacts(uow_factory, seed.seeded.run.id)
        )
        before_blobs = await _blob_shas(uow_factory, seed.seeded.run.id)

        detail = await client.get(
            f"/api/subjects/{subject_id}/production/artifacts/publication/{historical.id}"
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["version"] == historical.version == 2
        assert body["read_only_reason"] == "historical_version"
        assert body["canonical_sha256"] == historical_sha
        assert isinstance(body["canonical_content"], dict)
        assert body["artifact_id"] == str(historical.id)

        preview = await client.get(
            f"/api/subjects/{subject_id}/publication/preview",
            params={"artifact_id": str(historical.id)},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["status"] == "STALE"
        assert preview.json()["current_artifact_id"] == current["publication_artifact_id"]

        title = next(block for block in current["blocks"] if block["kind"] == "title")
        forged = await client.post(
            f"/api/subjects/{subject_id}/editor/blocks/{title['block_id']}/manual-edit",
            headers={"Idempotency-Key": f"historical-write-{uuid4().hex}"},
            json={
                "snapshot_id": str(historical_snapshot.snapshot_id),
                "operation": "rewrite_title",
                "text": "Synthetic forged historical title",
            },
        )
        assert forged.status_code == 409, forged.text
        # A historical snapshot id can never be the base of an editorial
        # write. The manual-edit route answers the shared stale-context code;
        # the policy guard uses ``editorial_read_only`` for a read-only current
        # context (proven by the legacy read-only PG oracle).
        assert forged.json()["detail"]["code"] in {
            "editorial_read_only",
            "editorial_snapshot_stale",
            "manual_edit_stale",
        }

    after_artifacts = sorted(
        (artifact.id, artifact.version, artifact.canonical_blob_id)
        for artifact in await _all_artifacts(uow_factory, seed.seeded.run.id)
    )
    after_blobs = await _blob_shas(uow_factory, seed.seeded.run.id)
    assert after_artifacts == before_artifacts
    assert after_blobs == before_blobs
    assert current["capabilities"]["read_only_reason"] is None
    assert await _model_run_count(migrated_postgres_url) == baseline_model_runs
    assert seed.model_gateway.calls == []


async def _all_artifacts(uow_factory: Any, run_id: UUID) -> list[Any]:
    async with uow_factory() as uow:
        return list(await uow.production_artifacts.list_for_run(run_id))


async def _blob_shas(uow_factory: Any, run_id: UUID) -> dict[str, str]:
    async with uow_factory() as uow:
        shas: dict[str, str] = {}
        for artifact in await uow.production_artifacts.list_for_run(run_id):
            if artifact.canonical_blob_id is None:
                continue
            blob = await uow.blobs.get(artifact.canonical_blob_id)
            assert blob is not None
            shas[str(artifact.id)] = blob.descriptor.sha256
        return shas


# ---------------------------------------------------------------------------
# E22 gap — manual revise_diagram through the generic typed revision route
# ---------------------------------------------------------------------------


def _evidence_ref_json(reference: Any) -> dict[str, str]:
    return {
        "source_document_id": str(reference.source_document_id),
        "kind": reference.kind.value,
        "evidence_key": reference.evidence_key,
    }


def _diagram_body(snapshot_id: str, diagram: DiagramSpecV1) -> dict[str, object]:
    assert diagram.purpose is not None
    purpose = diagram.purpose
    return {
        "snapshot_id": snapshot_id,
        "operation": "revise_diagram",
        "element_key": diagram.key,
        "diagram": {
            "key": diagram.key,
            "kind": diagram.kind.value,
            "title": f"{diagram.title} (revu)",
            "caption": diagram.caption,
            "direction": diagram.direction.value,
            "profile": diagram.profile.value if diagram.profile is not None else None,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "label": (f"{node.label} (revu)" if index == 0 else node.label),
                    "role": node.role.value,
                    "evidence_refs": [
                        _evidence_ref_json(reference) for reference in node.evidence_refs
                    ],
                }
                for index, node in enumerate(diagram.nodes)
            ],
            "edges": [
                {
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "label": edge.label,
                    "evidence_refs": [
                        _evidence_ref_json(reference) for reference in edge.evidence_refs
                    ],
                }
                for edge in diagram.edges
            ],
            "groups": [],
            "purpose": {
                "question": purpose.question,
                "available_data": purpose.available_data,
                "comprehension_gain": purpose.comprehension_gain,
                "scope": purpose.scope,
                "evidence_refs": [
                    _evidence_ref_json(reference) for reference in purpose.evidence_refs
                ],
                "knowledge_limits": purpose.knowledge_limits,
                "placement_reason": purpose.placement_reason,
            },
        },
    }


@pytest.mark.asyncio
async def test_manual_diagram_revision_route_postgres(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        _proposal,
        _adoption,
    ) = await _ready_chart_artifact(uow_factory, monkeypatch)
    subject_id = seeded.subject.id
    snapshot = await workspace_service.snapshot(subject_id)
    diagram_block = _enrichment_block(snapshot, kind="diagram")
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    base_diagram = editorial_enrichment_from_json(payload).diagrams[0]
    assert base_diagram.compiled_asset_id == BASE_DIAGRAM_ASSET
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_BASE_URL
    ) as client:
        proposal = await _post_revision(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(diagram_block["block_id"]),
            body=_diagram_body(snapshot["snapshot_id"], base_diagram),
        )
        assert proposal["operation"] == "rewrite_diagram"
        await _accept_ok(
            client,
            subject_id=subject_id,
            occurrence_id=UUID(diagram_block["block_id"]),
            proposal=proposal,
        )
        after = await _snapshot(client, subject_id)
    assert len([block for block in after["enrichments"] if block["kind"] == "diagram"]) == 1
    assert len([block for block in after["enrichments"] if block["kind"] == "chart"]) == 1
    artifact, revised_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    revised = editorial_enrichment_from_json(revised_payload)
    assert len(revised.diagrams) == 1
    assert revised.diagrams[0].key == base_diagram.key
    assert revised.diagrams[0].title == f"{base_diagram.title} (revu)"
    assert revised.diagrams[0].nodes[0].label == f"{base_diagram.nodes[0].label} (revu)"
    assert revised.diagrams[0].compiled_asset_id not in {
        None,
        BASE_DIAGRAM_ASSET,
    }
    assert artifact.version == 3
    previous = next(
        item
        for item in await _all_artifacts(uow_factory, seeded.run.id)
        if item.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT and item.version == 2
    )
    assert previous.canonical_blob_id is not None
    previous_enrichment = editorial_enrichment_from_json(
        await app.state.production_artifact_store.read_json(previous.canonical_blob_id)
    )
    assert previous_enrichment.diagrams[0].compiled_asset_id == BASE_DIAGRAM_ASSET
    assert model_gateway.calls == []
