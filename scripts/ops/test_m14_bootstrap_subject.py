from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from m14_bootstrap_subject import (
    DEFAULT_TITLE,
    MAIN_ARTICLE,
    BootstrapError,
    _archive_sources,
    _wait_for_production,
    bootstrap_subject,
    load_inputs,
)


def _inputs(tmp_path: Path):
    sources = []
    for index in range(7):
        name = f"report-{index}.html"
        url = (
            "https://unit42.paloaltonetworks.com/blinder-tunnel-targets-critical-infrastructure"
            if index == 0
            else f"https://vendor.example/report-{index}"
        )
        content = f"source bytes {index}".encode()
        digest = hashlib.sha256(content).hexdigest()
        (tmp_path / digest).write_bytes(content)
        sources.append(
            {
                "original_name": name,
                "url": url,
                "tlp": "AMBER",
                "sha256": digest,
                "size": len(content),
                "mime": "text/html",
            }
        )
    manifest = {
        "format": "autowork.m14-source-manifest",
        "schema_version": 1,
        "documents": sources,
    }
    manifest_path = tmp_path / "sources_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return load_inputs(manifest_path, period="2026-11"), sources


async def _no_sleep(_seconds: float) -> None:
    return None


def test_inputs_use_most_restrictive_manifest_tlp(tmp_path: Path) -> None:
    _inputs_result, documents = _inputs(tmp_path)
    manifest_path = tmp_path / "sources_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"][-1]["tlp"] = "RED"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = load_inputs(manifest_path, period="2026-11")

    assert result.edition_tlp == "RED"
    assert result.external_llm_allowed is False
    assert len(documents) == 7


@pytest.mark.asyncio
async def test_bootstrap_runs_manual_http_path_and_preserves_source_bytes(tmp_path: Path) -> None:
    inputs, source_docs = _inputs(tmp_path)
    ids = {
        "edition": str(uuid4()),
        "discovery_subject": str(uuid4()),
        "subject": str(uuid4()),
        "run": str(uuid4()),
        "batch": str(uuid4()),
        "job": str(uuid4()),
        "artifact": str(uuid4()),
        "investigation": str(uuid4()),
    }
    requests: list[httpx.Request] = []
    initialized: list[str] = []
    workbench_initialized = False
    production_polls = 0
    source_by_collection: dict[str, dict] = {}
    for index, document in enumerate(source_docs):
        source_by_collection[f"collection-{index}"] = document

    def workbench_sources() -> list[dict]:
        if not workbench_initialized:
            return []
        return [
            {
                "id": collection_id,
                "requested_url": document["url"],
                "state": "pending",
                "archive_receipt": None,
            }
            for collection_id, document in source_by_collection.items()
        ]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal workbench_initialized, production_polls
        requests.append(request)
        assert request.headers.get("authorization") == "Bearer test-token"
        path = request.url.path
        if request.method == "GET" and path == "/api/editions":
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 100})
        if request.method == "POST" and path == "/api/editions":
            body = json.loads(request.content)
            assert body["tlp"] == "AMBER"
            return httpx.Response(
                201,
                json={"id": ids["edition"], "state": "open", "tlp": "AMBER"},
            )
        if path.endswith("/discovery/import/preview"):
            body = json.loads(request.content)
            assert body["source_profile"] == "manual-import"
            assert MAIN_ARTICLE in body["markdown"]
            return httpx.Response(
                200,
                json={
                    "sha256": hashlib.sha256(body["markdown"].encode()).hexdigest(),
                    "subject_count": 1,
                    "publication_count": 7,
                },
            )
        if path.endswith("/discovery/import/confirm"):
            assert request.headers["idempotency-key"].startswith("m14-bootstrap:discovery:")
            return httpx.Response(
                200,
                json={
                    "run_id": str(uuid4()),
                    "batch_id": str(uuid4()),
                    "reused": False,
                    "source_mode": "manual_import",
                    "subject_count": 1,
                    "publication_count": 7,
                    "reconciliation_job_id": ids["job"],
                },
            )
        if request.method == "GET" and path == f"/api/jobs/{ids['job']}":
            return httpx.Response(200, json={"id": ids["job"], "status": "succeeded"})
        if path.endswith("/selection") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "snapshot_version": 1,
                    "items": [
                        {
                            "discovery_subject_id": ids["discovery_subject"],
                            "title": DEFAULT_TITLE,
                            "selectable": True,
                            "subject_id": None,
                        }
                    ],
                },
            )
        if path.endswith("/selection/decisions"):
            body = json.loads(request.content)
            assert request.headers["idempotency-key"].startswith("m14-bootstrap:selection:")
            assert body["decisions"][0]["action"] == "select"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "discovery_subject_id": ids["discovery_subject"],
                            "subject_id": ids["subject"],
                        }
                    ]
                },
            )
        if path == f"/api/subjects/{ids['subject']}/workbench":
            return httpx.Response(200, json={"sources": workbench_sources()})
        if "/sources/collection-" in path and path.endswith("/content"):
            collection_id = path.split("/")[-2]
            document = source_by_collection[collection_id]
            source = inputs.sources[int(collection_id.rsplit("-", 1)[1])]
            assert source.path.read_bytes() in request.content
            assert request.headers["content-type"].startswith("multipart/form-data;")
            return httpx.Response(
                200,
                json={
                    "id": collection_id,
                    "archive_receipt": {
                        "encoded_sha256": document["sha256"],
                        "bytes": document["size"],
                    },
                },
            )
        if path.endswith("/production/batches"):
            assert request.headers["idempotency-key"].startswith("m14-bootstrap:production:")
            return httpx.Response(
                200,
                json={
                    "batch_id": ids["batch"],
                    "item_details": [{"subject_id": ids["subject"], "run_id": ids["run"]}],
                },
            )
        if path == f"/api/subjects/{ids['subject']}/production":
            production_polls += 1
            status = "running" if production_polls == 1 else "ready"
            return httpx.Response(
                200,
                json={
                    "status": status,
                    "run_id": ids["run"],
                    "current_stage": "synthesis" if status == "running" else "assembly",
                    "reconciliation": None,
                },
            )
        if path.endswith("/production/artifacts/synthesis"):
            return httpx.Response(
                200,
                json={
                    "artifact_id": ids["artifact"],
                    "production_run_id": ids["run"],
                    "canonical_sha256": "a" * 64,
                    "status": "verified",
                },
            )
        if path == f"/api/subjects/{ids['subject']}/investigations":
            body = json.loads(request.content)
            assert body["execution_mode"] == "operator_led"
            return httpx.Response(
                200,
                json={
                    "investigation": {"id": ids["investigation"], "execution_mode": "operator_led"},
                    "job_id": str(uuid4()),
                    "idempotent_replay": False,
                },
            )
        if path == f"/api/investigations/{ids['investigation']}/envelope":
            return httpx.Response(200, json={"approved": False, "version": 0})
        return httpx.Response(404, json={"detail": {"code": "unexpected_mock_route"}})

    async def initialize(_subject_id: str) -> None:
        nonlocal workbench_initialized
        initialized.append(_subject_id)
        workbench_initialized = True

    client = httpx.AsyncClient(
        base_url="http://mock",
        headers={"Authorization": "Bearer test-token"},
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await bootstrap_subject(
            client,
            inputs,
            source_initializer=initialize,
            create_investigation=True,
            poll_interval=0,
            timeout_seconds=30,
            sleep=_no_sleep,
        )
    finally:
        await client.aclose()

    assert result["status"] == "complete"
    assert result["edition_id"] == ids["edition"]
    assert result["subject_id"] == ids["subject"]
    assert result["production_run_id"] == ids["run"]
    assert result["synthesis_artifact_id"] == ids["artifact"]
    assert result["synthesis_sha256"] == "a" * 64
    assert result["investigation_result"]["g2_envelope"]["approved"] is False
    assert initialized == [ids["subject"]]
    assert sum(request.url.path.endswith("/content") for request in requests) == 7
    assert not any(request.url.path.endswith("/envelope/approve") for request in requests)


@pytest.mark.asyncio
async def test_host_mode_reports_missing_initialization_route_without_collection_call(
    tmp_path: Path,
) -> None:
    inputs, _ = _inputs(tmp_path)
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"sources": []})

    client = httpx.AsyncClient(base_url="http://mock", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(
            BootstrapError, match=r"POST /api/subjects/\{subject_id\}/collection"
        ) as exc:
            await _archive_sources(client, str(uuid4()), inputs.sources, source_initializer=None)
    finally:
        await client.aclose()

    assert exc.value.code == "source_collection_init_route_missing"
    assert len(calls) == 1
    assert calls[0].endswith("/workbench")
    assert not any(path.endswith("/collection") for path in calls)


@pytest.mark.asyncio
async def test_needs_review_is_displayed_and_not_resumed() -> None:
    calls: list[str] = []
    events: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "status": "needs_review",
                "run_id": "run-1",
                "current_stage": "synthesis",
                "error_code": "provider_result_ambiguous",
                "reconciliation": {"submission_state": "needs_review"},
            },
        )

    client = httpx.AsyncClient(base_url="http://mock", transport=httpx.MockTransport(handler))
    try:
        status = await _wait_for_production(
            client,
            edition_id="edition-1",
            subject_id="subject-1",
            batch_id="batch-1",
            poll_interval=0,
            timeout_seconds=10,
            sleep=_no_sleep,
            on_status=events.append,
        )
    finally:
        await client.aclose()

    assert status["status"] == "needs_review"
    assert events[0]["reconciliation"]["submission_state"] == "needs_review"
    assert calls == ["/api/subjects/subject-1/production"]
