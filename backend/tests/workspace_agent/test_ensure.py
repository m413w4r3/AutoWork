"""Workspace manifest, layout and idempotent ensure (design §5.1-5.2, ticket W2)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path

import pytest
from sidecars.workspace_agent.protocol import ErrorCode
from sidecars.workspace_agent.workspace import (
    MANIFEST_SCHEMA,
    EnsureWorkspaceRequest,
    InvalidWorkspaceRequestError,
    WorkspaceIdentityConflictError,
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspaceUnmanagedDirectoryError,
    compute_identity_sha256,
    workspace_id_for,
)

from tests.workspace_agent.conftest import RunningAgent

DELIVERY_CODE = "IR2609"
ARTICLE_SLUG = "bluemoon-cn"
SUBJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
INVESTIGATION_ID = "22222222-2222-4222-8222-222222222222"
SYNTHESIS_SHA256 = "a" * 64

MANIFEST_KEYS = {
    "schema",
    "workspace_id",
    "delivery_code",
    "article_slug",
    "subject_id",
    "investigation_id",
    "synthesis_sha256",
    "created_at",
    "created_by",
    "layout_version",
    "identity_sha256",
}

LAYOUT_DIRECTORIES = (
    ".autowork",
    ".autowork/incoming",
    "base_samples",
    "pcaps",
    "scripts",
    "regles",
    "discover_sample",
    "plans",
    "results",
    "notes",
    "notes/humaines",
    "notes/bridge",
    "notes/agents",
    "vt",
    "vt/files",
    "vt/ips",
    "vt/domains",
    "vt/urls",
    "vt/behaviours",
    "vt/relationships",
)


def ensure_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "delivery_code": DELIVERY_CODE,
        "article_slug": ARTICLE_SLUG,
        "subject_id": SUBJECT_ID,
        "investigation_id": INVESTIGATION_ID,
        "synthesis_sha256": SYNTHESIS_SHA256,
    }
    body.update(overrides)
    return body


def ensure_request(**overrides: object) -> EnsureWorkspaceRequest:
    return EnsureWorkspaceRequest.from_mapping(ensure_payload(**overrides))


def workspace_path(root: Path) -> Path:
    return root / DELIVERY_CODE / ARTICLE_SLUG


def manifest_path(root: Path) -> Path:
    return workspace_path(root) / ".autowork" / "manifest.json"


def expected_workspace_id() -> str:
    digest = hashlib.sha256(f"{DELIVERY_CODE}\x00{ARTICLE_SLUG}".encode()).hexdigest()
    return "ws-" + digest[:32]


def test_workspace_id_and_identity_are_deterministic() -> None:
    expected_id = expected_workspace_id()
    assert workspace_id_for(DELIVERY_CODE, ARTICLE_SLUG) == expected_id
    assert ensure_request().workspace_id == expected_id
    assert ensure_request().workspace_id == ensure_request().workspace_id

    fields = {
        "schema": MANIFEST_SCHEMA,
        "workspace_id": expected_id,
        "delivery_code": DELIVERY_CODE,
        "article_slug": ARTICLE_SLUG,
        "subject_id": SUBJECT_ID,
        "investigation_id": INVESTIGATION_ID,
        "synthesis_sha256": SYNTHESIS_SHA256,
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected_identity = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert ensure_request().identity_sha256 == expected_identity
    assert (
        compute_identity_sha256(
            workspace_id=expected_id,
            delivery_code=DELIVERY_CODE,
            article_slug=ARTICLE_SLUG,
            subject_id=SUBJECT_ID,
            investigation_id=INVESTIGATION_ID,
            synthesis_sha256=SYNTHESIS_SHA256,
        )
        == expected_identity
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delivery_code", ""),
        ("delivery_code", "ir2609"),
        ("delivery_code", "IR/2609"),
        ("delivery_code", ".IR2609"),
        ("delivery_code", "IR2609\n"),
        ("delivery_code", "A" * 65),
        ("article_slug", ""),
        ("article_slug", "BlueMoon"),
        ("article_slug", "-leading"),
        ("article_slug", "trailing-"),
        ("article_slug", "double--dash"),
        ("article_slug", "under_score"),
        ("article_slug", "with space"),
        ("article_slug", "a" * 81),
        ("subject_id", "not-a-uuid"),
        ("subject_id", 123),
        ("investigation_id", "1234"),
        ("synthesis_sha256", "A" * 64),
        ("synthesis_sha256", "a" * 63),
        ("synthesis_sha256", "a" * 65),
        ("synthesis_sha256", "g" * 64),
    ],
)
def test_invalid_ensure_fields_are_rejected(field: str, value: object) -> None:
    with pytest.raises(InvalidWorkspaceRequestError) as excinfo:
        EnsureWorkspaceRequest.from_mapping(ensure_payload(**{field: value}))
    error = excinfo.value
    assert error.code is ErrorCode.INVALID_REQUEST
    assert error.status == 400
    assert error.details == {"field": field, "reason": "invalid"}
    assert str(error) == "invalid workspace ensure request"


def test_missing_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(InvalidWorkspaceRequestError) as missing:
        EnsureWorkspaceRequest.from_mapping({})
    assert missing.value.details == {"field": "delivery_code", "reason": "missing"}

    with pytest.raises(InvalidWorkspaceRequestError) as unknown:
        EnsureWorkspaceRequest.from_mapping(ensure_payload(extra="value"))
    assert unknown.value.details == {"field": "extra", "reason": "unknown_field"}


def test_uuid_fields_are_canonicalized() -> None:
    request = ensure_request(
        subject_id=SUBJECT_ID.upper(), investigation_id="{" + INVESTIGATION_ID + "}"
    )
    assert request.subject_id == SUBJECT_ID
    assert request.investigation_id == INVESTIGATION_ID


def test_ensure_creates_the_layout_with_exact_modes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result = WorkspaceManager(root).ensure(ensure_request())
    workspace = workspace_path(root)

    assert result.created is True
    assert result.workspace_id == expected_workspace_id()
    assert result.host_path == str(workspace)
    assert stat.S_IMODE(os.lstat(workspace).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(workspace.parent).st_mode) == 0o700
    for relative in LAYOUT_DIRECTORIES:
        directory = workspace / relative
        assert directory.is_dir(), relative
        expected_mode = 0o750 if relative == "base_samples" else 0o700
        assert stat.S_IMODE(os.lstat(directory).st_mode) == expected_mode, relative

    manifest = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    assert set(manifest) == MANIFEST_KEYS
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["workspace_id"] == expected_workspace_id()
    assert manifest["delivery_code"] == DELIVERY_CODE
    assert manifest["article_slug"] == ARTICLE_SLUG
    assert manifest["subject_id"] == SUBJECT_ID
    assert manifest["investigation_id"] == INVESTIGATION_ID
    assert manifest["synthesis_sha256"] == SYNTHESIS_SHA256
    assert manifest["created_by"] == "autowork-workspace-agent"
    assert manifest["layout_version"] == 1
    assert manifest["identity_sha256"] == ensure_request().identity_sha256
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["created_at"])
    assert dict(result.manifest) == manifest
    assert stat.S_IMODE(os.lstat(manifest_path(root)).st_mode) & 0o022 == 0


def test_ensure_is_idempotent_and_never_rewrites_the_manifest(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = WorkspaceManager(root)
    first = manager.ensure(ensure_request())
    before = manifest_path(root).read_bytes()
    inode_before = os.lstat(manifest_path(root)).st_ino

    second = manager.ensure(ensure_request())

    assert first.created is True
    assert second.created is False
    assert second.manifest == first.manifest
    assert manifest_path(root).read_bytes() == before
    assert os.lstat(manifest_path(root)).st_ino == inode_before


def test_same_path_with_another_identity_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = WorkspaceManager(root)
    manager.ensure(ensure_request())
    before = manifest_path(root).read_bytes()

    with pytest.raises(WorkspaceIdentityConflictError) as excinfo:
        manager.ensure(ensure_request(synthesis_sha256="b" * 64))
    assert excinfo.value.code is ErrorCode.WORKSPACE_IDENTITY_CONFLICT
    assert excinfo.value.status == 409
    assert str(tmp_path) not in str(excinfo.value)

    with pytest.raises(WorkspaceIdentityConflictError):
        manager.ensure(ensure_request(subject_id="33333333-3333-4333-8333-333333333333"))
    assert manifest_path(root).read_bytes() == before


def test_non_empty_unmanaged_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    workspace = workspace_path(root)
    note = workspace / "notes" / "humaines" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("human note", encoding="utf-8")

    with pytest.raises(WorkspaceUnmanagedDirectoryError) as excinfo:
        WorkspaceManager(root).ensure(ensure_request())
    assert excinfo.value.code is ErrorCode.WORKSPACE_UNMANAGED_DIRECTORY
    assert excinfo.value.status == 409
    assert not (workspace / ".autowork").exists()
    assert note.read_text(encoding="utf-8") == "human note"
    assert str(tmp_path) not in str(excinfo.value)


def test_corrupt_manifest_is_refused_and_never_repaired(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = WorkspaceManager(root)
    manager.ensure(ensure_request())
    path = manifest_path(root)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(WorkspaceUnmanagedDirectoryError) as excinfo:
        manager.ensure(ensure_request())
    assert excinfo.value.code is ErrorCode.WORKSPACE_UNMANAGED_DIRECTORY
    assert excinfo.value.details.get("reason") == "manifest_corrupt"
    assert path.read_text(encoding="utf-8") == "{not json"


def test_tampered_manifest_identity_is_corrupt(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = WorkspaceManager(root)
    manager.ensure(ensure_request())
    path = manifest_path(root)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["synthesis_sha256"] = "b" * 64
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(WorkspaceUnmanagedDirectoryError) as excinfo:
        manager.ensure(ensure_request())
    assert excinfo.value.details.get("reason") == "manifest_corrupt"
    assert json.loads(path.read_text(encoding="utf-8"))["synthesis_sha256"] == "b" * 64


def test_concurrent_ensure_creates_exactly_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = WorkspaceManager(root)
    barrier = threading.Barrier(8)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(manager.ensure(ensure_request()))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 8
    created_flags = [result.created for result in results]
    assert created_flags.count(True) == 1
    assert {result.workspace_id for result in results} == {expected_workspace_id()}
    manifest = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    assert manifest["workspace_id"] == expected_workspace_id()


def test_refresh_recreates_missing_directories_and_keeps_human_content(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = WorkspaceManager(root)
    manager.ensure(ensure_request())
    workspace = workspace_path(root)
    note = workspace / "notes" / "humaines" / "note.md"
    note.write_text("human note", encoding="utf-8")
    note_before = os.lstat(note)
    manifest_before = os.lstat(manifest_path(root))

    (workspace / "vt" / "urls").rmdir()
    (workspace / "notes" / "bridge").rmdir()

    refreshed = manager.ensure(ensure_request())

    assert refreshed.created is False
    assert (workspace / "vt" / "urls").is_dir()
    assert (workspace / "notes" / "bridge").is_dir()
    assert note.read_text(encoding="utf-8") == "human note"
    note_after = os.lstat(note)
    assert (note_after.st_ino, note_after.st_mtime_ns) == (
        note_before.st_ino,
        note_before.st_mtime_ns,
    )
    manifest_after = os.lstat(manifest_path(root))
    assert manifest_after.st_ino == manifest_before.st_ino
    assert manifest_after.st_mtime_ns == manifest_before.st_mtime_ns


def test_reads_never_create_the_root(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    manager = WorkspaceManager(root)
    assert manager.list_workspaces() == []
    with pytest.raises(WorkspaceNotFoundError) as excinfo:
        manager.get_workspace(expected_workspace_id())
    assert excinfo.value.code is ErrorCode.WORKSPACE_NOT_FOUND
    assert not root.exists()


def test_http_ensure_list_and_get(uds_client, agent_server: RunningAgent) -> None:
    key = {"Idempotency-Key": "m24-w2-ensure-0001"}
    created = uds_client.post(
        "/v1/workspaces/ensure", headers={**agent_server.auth_headers, **key}, json=ensure_payload()
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"workspace_id", "host_path", "created", "manifest"}
    assert body["created"] is True
    assert body["workspace_id"] == expected_workspace_id()
    assert body["host_path"] == str(workspace_path(agent_server.root))

    replay = uds_client.post(
        "/v1/workspaces/ensure",
        headers={**agent_server.auth_headers, "Idempotency-Key": "m24-w2-ensure-0002"},
        json=ensure_payload(),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["created"] is False

    listing = uds_client.get("/v1/workspaces", headers=agent_server.auth_headers)
    assert listing.status_code == 200
    entries = listing.json()["workspaces"]
    assert len(entries) == 1
    assert set(entries[0]) == {
        "workspace_id",
        "delivery_code",
        "article_slug",
        "host_path",
        "generation",
    }
    assert entries[0]["workspace_id"] == expected_workspace_id()
    assert entries[0]["delivery_code"] == DELIVERY_CODE
    assert entries[0]["article_slug"] == ARTICLE_SLUG
    assert entries[0]["host_path"] == str(workspace_path(agent_server.root))
    assert entries[0]["generation"] == 0

    fetched = uds_client.get(
        f"/v1/workspaces/{expected_workspace_id()}", headers=agent_server.auth_headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["created"] is False
    assert fetched.json()["manifest"] == body["manifest"]

    missing = uds_client.get("/v1/workspaces/ws-" + "0" * 32, headers=agent_server.auth_headers)
    assert missing.status_code == 404
    assert missing.json()["error"] == "workspace_not_found"
    assert str(agent_server.root) not in missing.text


def test_http_rejects_invalid_ensure_payloads(uds_client, agent_server: RunningAgent) -> None:
    def post(**kwargs: object):
        headers = {
            **agent_server.auth_headers,
            "Idempotency-Key": f"m24-w2-ensure-{kwargs.pop('key')}",
        }
        return uds_client.post("/v1/workspaces/ensure", headers=headers, **kwargs)

    malformed = post(key="0100", content=b"{not json")
    assert malformed.status_code == 400
    assert malformed.json()["error"] == "invalid_request"

    unknown = post(key="0101", json=ensure_payload(extra="value"))
    assert unknown.status_code == 400
    assert unknown.json()["details"] == {"field": "extra", "reason": "unknown_field"}

    empty = post(key="0102", json={})
    assert empty.status_code == 400
    assert empty.json()["details"] == {"field": "delivery_code", "reason": "missing"}

    invalid = post(key="0103", json=ensure_payload(synthesis_sha256="A" * 64))
    assert invalid.status_code == 400
    assert invalid.json()["details"] == {"field": "synthesis_sha256", "reason": "invalid"}
    for response in (malformed, unknown, empty, invalid):
        assert str(agent_server.root) not in response.text


def test_http_identity_conflict_is_typed_and_path_free(
    uds_client, agent_server: RunningAgent
) -> None:
    first = uds_client.post(
        "/v1/workspaces/ensure",
        headers={**agent_server.auth_headers, "Idempotency-Key": "m24-w2-ensure-0200"},
        json=ensure_payload(),
    )
    assert first.status_code == 201
    conflict = uds_client.post(
        "/v1/workspaces/ensure",
        headers={**agent_server.auth_headers, "Idempotency-Key": "m24-w2-ensure-0201"},
        json=ensure_payload(synthesis_sha256="c" * 64),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "workspace_identity_conflict"
    assert str(agent_server.root) not in conflict.text
