"""File content reads, CAS writes and generated publication (design §4, §5.3, §7, ticket W4).

The wire tests drive a real ``ThreadingHTTPServer`` over an AF_UNIX socket in a
pytest tmpdir; the atomicity tests call :mod:`sidecars.workspace_agent.files`
directly against a real workspace tree. Everything is offline and rootless.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx
import pytest
from sidecars.workspace_agent import events, files, policies, protocol
from sidecars.workspace_agent.scan import WorkspaceScanService

from tests.workspace_agent.conftest import raw_http_request, response_json, response_status
from tests.workspace_agent.w3_support import (
    ARTICLE_SLUG,
    DELIVERY_CODE,
    INVESTIGATION_ID,
    SUBJECT_ID,
    SYNTHESIS_SHA256,
    ScanEnvironment,
    scan_via_http,
)

_BASE_URL = "http://workspace-agent"


class AgentHandle(Protocol):
    """Common surface of the ``agent_server`` and ``scanning_agent`` fixtures."""

    @property
    def root(self) -> Path: ...

    @property
    def socket_path(self) -> Path: ...

    @property
    def token(self) -> str: ...

    @property
    def auth_headers(self) -> dict[str, str]: ...


def ensure_workspace(
    client: httpx.Client, running: AgentHandle, *, key: str = "w4-ensure-0001"
) -> str:
    response = client.post(
        "/v1/workspaces/ensure",
        json={
            "delivery_code": DELIVERY_CODE,
            "article_slug": ARTICLE_SLUG,
            "subject_id": SUBJECT_ID,
            "investigation_id": INVESTIGATION_ID,
            "synthesis_sha256": SYNTHESIS_SHA256,
        },
        headers={**running.auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    workspace_id = response.json()["workspace_id"]
    assert isinstance(workspace_id, str)
    return workspace_id


def workspace_tree(running: AgentHandle) -> Path:
    return running.root / DELIVERY_CODE / ARTICLE_SLUG


def file_url(workspace_id: str, suffix: str) -> str:
    return f"/v1/workspaces/{workspace_id}/{suffix}"


def put_file(
    client: httpx.Client,
    running: AgentHandle,
    workspace_id: str,
    path: str,
    content: bytes,
    *,
    key: str,
    expected: str = "new",
    origin: str = "ui",
    task_id: str | None = None,
    generated_by: str | None = None,
    source_sha256: str | None = None,
    generated: bool = False,
) -> httpx.Response:
    headers = {
        **running.auth_headers,
        "Idempotency-Key": key,
        "X-Autowork-Expected-Sha256": expected,
        "X-Autowork-Origin": origin,
    }
    if task_id is not None:
        headers["X-Autowork-Task-Id"] = task_id
    if generated_by is not None:
        headers["X-Autowork-Generated-By"] = generated_by
    if source_sha256 is not None:
        headers["X-Autowork-Source-Sha256"] = source_sha256
    target = "generated" if generated else "files"
    return client.put(
        file_url(workspace_id, target),
        params={"path": path},
        headers=headers,
        content=content,
    )


def get_content(
    client: httpx.Client, running: AgentHandle, workspace_id: str, path: str
) -> httpx.Response:
    return client.get(
        file_url(workspace_id, "files/content"),
        params={"path": path},
        headers=running.auth_headers,
    )


def get_revisions(
    client: httpx.Client, running: AgentHandle, workspace_id: str, path: str
) -> httpx.Response:
    return client.get(
        file_url(workspace_id, "files/revisions"),
        params={"path": path},
        headers=running.auth_headers,
    )


def fetch_events(
    client: httpx.Client, running: AgentHandle, workspace_id: str
) -> list[dict[str, object]]:
    response = client.get(
        file_url(workspace_id, "events"),
        params={"after": 0, "limit": 500},
        headers=running.auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    entries = payload["events"]
    assert isinstance(entries, list)
    return [entry for entry in entries if isinstance(entry, dict)]


def events_for(
    client: httpx.Client, running: AgentHandle, workspace_id: str, path: str
) -> list[dict[str, object]]:
    return [event for event in fetch_events(client, running, workspace_id) if event["path"] == path]


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def expected_revision(workspace_id: str, path: str, content: bytes) -> str:
    # ``sidecars.*`` is outside the backend mypy package (see ignore_missing_imports).
    revision: str = events.revision_id_for(workspace_id, path, sha256_of(content))
    return revision


def read_index(running: AgentHandle) -> dict[str, object]:
    payload = json.loads((workspace_tree(running) / ".autowork" / "file-index.json").read_text())
    assert isinstance(payload, dict)
    return payload


def test_put_creates_an_editable_file_and_serves_its_raw_bytes(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    content = b"# premier constat\n\nsynthetic note\n"
    path = "notes/humaines/premier.md"

    created = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        content,
        key="m24-w4-put-0001",
        origin="ui",
    )

    assert created.status_code == 201, created.text
    payload = created.json()
    revision = payload["revision"]
    assert isinstance(revision, dict)
    assert payload["created"] is True
    assert payload["already_present"] is False
    assert payload["conflict"] is False
    assert revision["revision_id"] == expected_revision(workspace_id, path, content)
    assert revision["path"] == path
    assert revision["sha256"] == sha256_of(content)
    assert revision["size"] == len(content)
    assert revision["origin"] == "ui"
    assert revision["task_id"] is None
    assert revision["supersedes_revision_id"] is None

    live = workspace_tree(scanning_agent) / path
    assert live.read_bytes() == content
    assert stat.S_IMODE(os.lstat(live).st_mode) == 0o640

    read = get_content(scanning_client, scanning_agent, workspace_id, path)
    assert read.status_code == 200, read.text
    assert read.content == content
    assert read.headers["etag"] == f'"sha256:{sha256_of(content)}"'
    assert read.headers["x-autowork-sha256"] == sha256_of(content)
    assert read.headers["x-autowork-size"] == str(len(content))
    assert int(read.headers["x-autowork-mtime-ns"]) == os.lstat(live).st_mtime_ns

    revisions = get_revisions(scanning_client, scanning_agent, workspace_id, path)
    assert revisions.status_code == 200, revisions.text
    assert revisions.json()["revisions"] == [revision]


def test_origin_comes_from_the_header_not_the_folder(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    agent_path = "notes/humaines/par-agent.md"
    ui_path = "notes/humaines/par-humain.md"

    agent_write = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        agent_path,
        b"agent",
        key="m24-w4-put-0100",
        origin="agent",
    )
    assert agent_write.status_code == 201, agent_write.text
    ui_write = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        ui_path,
        b"ui",
        key="m24-w4-put-0101",
        origin="ui",
    )
    assert ui_write.status_code == 201, ui_write.text

    listing = scanning_client.get(
        file_url(workspace_id, "files"),
        params={"prefix": "notes/humaines/"},
        headers=scanning_agent.auth_headers,
    )
    assert listing.status_code == 200, listing.text
    states = {state["path"]: state for state in listing.json()["files"]}
    assert states[agent_path]["origin"] == "agent"
    assert states[ui_path]["origin"] == "ui"


def test_route_prefix_mismatches_are_policy_denied(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    tree = workspace_tree(scanning_agent)
    denied_files = (
        "plans/plan.md",
        "vt/files/sample.json",
        "results/export.csv",
        "pcaps/capture.pcap",
        "notes/bridge/draft.md",
        "base_samples/" + "a" * 64,
    )
    for path in denied_files:
        response = put_file(
            scanning_client,
            scanning_agent,
            workspace_id,
            path,
            b"denied",
            key="m24-w4-put-0200",
        )
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"] == "policy_denied"
        assert not (tree / path).exists()

    for path in (
        "notes/humaines/ok.md",
        "scripts/ok.sh",
        "regles/ok.yar",
        "discover_sample/ok.txt",
    ):
        response = put_file(
            scanning_client,
            scanning_agent,
            workspace_id,
            path,
            b"allowed",
            key="m24-w4-put-0201",
        )
        assert response.status_code == 201, (path, response.text)


def test_generated_route_refuses_editable_prefixes(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    for path in ("notes/humaines/note.md", "scripts/tool.sh", "base_samples/" + "b" * 64):
        response = put_file(
            scanning_client,
            scanning_agent,
            workspace_id,
            path,
            b"denied",
            key="m24-w4-put-0300",
            origin="system",
            generated=True,
            generated_by="bridge",
            source_sha256="d" * 64,
        )
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"] == "policy_denied"


def test_agent_owned_write_requires_the_matching_task_header(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    task_directory = workspace_tree(scanning_agent) / "notes" / "agents" / "taskA"
    path = "notes/agents/taskA/rapport.md"

    mismatch = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        b"x",
        key="m24-w4-put-0400",
        task_id="taskB",
    )
    assert mismatch.status_code == 403, mismatch.text
    assert mismatch.json()["error"] == "policy_denied"
    assert not (workspace_tree(scanning_agent) / path).exists()

    missing = put_file(
        scanning_client, scanning_agent, workspace_id, path, b"x", key="m24-w4-put-0401"
    )
    assert missing.status_code == 403
    assert missing.json()["error"] == "policy_denied"

    accepted = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        b"x",
        key="m24-w4-put-0402",
        task_id="taskA",
        origin="agent",
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["revision"]["task_id"] == "taskA"
    assert task_directory.is_dir()
    assert stat.S_IMODE(task_directory.stat().st_mode) == 0o700
    events_here = events_for(scanning_client, scanning_agent, workspace_id, path)
    assert [event["task_id"] for event in events_here] == ["taskA"]


def test_generated_publication_requires_system_origin_and_provenance(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    path = "plans/plan-001.md"
    url = file_url(workspace_id, "generated")
    base = {
        **scanning_agent.auth_headers,
        "Idempotency-Key": "m24-w4-put-0500",
        "X-Autowork-Expected-Sha256": "new",
        "X-Autowork-Generated-By": "bridge",
        "X-Autowork-Source-Sha256": "e" * 64,
    }

    wrong_origin = scanning_client.put(
        url,
        params={"path": path},
        headers={**base, "X-Autowork-Origin": "ui"},
        content=b"plan",
    )
    assert wrong_origin.status_code == 400, wrong_origin.text
    assert wrong_origin.json()["error"] == "invalid_request"

    for missing in ("X-Autowork-Generated-By", "X-Autowork-Source-Sha256"):
        headers = {**base, "X-Autowork-Origin": "system"}
        del headers[missing]
        response = scanning_client.put(url, params={"path": path}, headers=headers, content=b"plan")
        assert response.status_code == 400, (missing, response.text)
        assert response.json()["error"] == "invalid_request"

    accepted = scanning_client.put(
        url,
        params={"path": path},
        headers={**base, "X-Autowork-Origin": "system"},
        content=b"plan",
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["revision"]["origin"] == "system"
    live = workspace_tree(scanning_agent) / path
    assert live.read_bytes() == b"plan"
    assert stat.S_IMODE(os.lstat(live).st_mode) == 0o440


def test_generated_overwrite_requires_the_matching_expected_sha(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    path = "results/export-001.csv"
    first = b"a,b\n1,2\n"
    second = b"a,b\n3,4\n"
    third = b"a,b\n5,6\n"

    created = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        first,
        key="m24-w4-put-0600",
        origin="system",
        generated=True,
        generated_by="vt-export",
        source_sha256="f" * 64,
    )
    assert created.status_code == 201, created.text

    overwritten = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        second,
        key="m24-w4-put-0601",
        expected=sha256_of(first),
        origin="system",
        generated=True,
        generated_by="vt-export",
        source_sha256="f" * 64,
    )
    assert overwritten.status_code == 200, overwritten.text
    revision = overwritten.json()["revision"]
    assert revision["supersedes_revision_id"] == expected_revision(workspace_id, path, first)
    live = workspace_tree(scanning_agent) / path
    assert live.read_bytes() == second
    assert stat.S_IMODE(os.lstat(live).st_mode) == 0o440

    stale = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        third,
        key="m24-w4-put-0602",
        expected=sha256_of(first),
        origin="system",
        generated=True,
        generated_by="vt-export",
        source_sha256="f" * 64,
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["error"] == "revision_conflict"
    assert body["details"]["current"] == sha256_of(second)
    assert body["details"]["proposed_sha256"] == sha256_of(third)
    assert live.read_bytes() == second
    assert stat.S_IMODE(os.lstat(live).st_mode) == 0o440


def test_expected_sha_header_is_required_and_strict(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    url = file_url(workspace_id, "files")
    path = "notes/humaines/strict.md"
    base = {
        **scanning_agent.auth_headers,
        "Idempotency-Key": "m24-w4-put-0700",
        "X-Autowork-Origin": "ui",
    }

    missing = scanning_client.put(url, params={"path": path}, headers=base, content=b"strict")
    assert missing.status_code == 400
    assert missing.json()["error"] == "invalid_request"

    for value in ("deadbeef", "A" * 64, "sha256:" + "a" * 64):
        response = scanning_client.put(
            url,
            params={"path": path},
            headers={**base, "X-Autowork-Expected-Sha256": value},
            content=b"strict",
        )
        assert response.status_code == 400, (value, response.text)
        assert response.json()["error"] == "invalid_request"
    assert not (workspace_tree(scanning_agent) / path).exists()


def test_missing_or_invalid_origin_is_rejected(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    url = file_url(workspace_id, "files")
    headers = {
        **scanning_agent.auth_headers,
        "Idempotency-Key": "m24-w4-put-0800",
        "X-Autowork-Expected-Sha256": "new",
    }
    missing = scanning_client.put(
        url, params={"path": "notes/humaines/x.md"}, headers=headers, content=b"x"
    )
    assert missing.status_code == 400
    for origin in ("system", "filesystem", "other"):
        response = scanning_client.put(
            url,
            params={"path": "notes/humaines/x.md"},
            headers={**headers, "X-Autowork-Origin": origin},
            content=b"x",
        )
        assert response.status_code == 400, (origin, response.text)


def test_a_body_equal_to_the_current_content_is_already_present(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    path = "notes/humaines/idempotent.md"
    content = b"same bytes"
    created = put_file(
        scanning_client, scanning_agent, workspace_id, path, content, key="m24-w4-put-0900"
    )
    assert created.status_code == 201
    live = workspace_tree(scanning_agent) / path
    before = os.lstat(live)

    repeated = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        content,
        key="m24-w4-put-0901",
        expected=sha256_of(content),
    )

    assert repeated.status_code == 200, repeated.text
    payload = repeated.json()
    assert payload["already_present"] is True
    assert payload["created"] is False
    assert payload["revision"]["revision_id"] == expected_revision(workspace_id, path, content)
    after = os.lstat(live)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert len(events_for(scanning_client, scanning_agent, workspace_id, path)) == 1


def test_replaying_an_idempotency_key_returns_the_stored_revision(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    path = "notes/humaines/rejoue.md"
    content = b"replayed body"
    first = put_file(
        scanning_client, scanning_agent, workspace_id, path, content, key="m24-w4-put-1000"
    )
    assert first.status_code == 201
    live = workspace_tree(scanning_agent) / path
    before = os.lstat(live)

    replay = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        b"different body ignored on replay",
        key="m24-w4-put-1000",
    )

    assert replay.status_code == first.status_code
    assert replay.json() == first.json()
    after = os.lstat(live)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert live.read_bytes() == content
    assert len(events_for(scanning_client, scanning_agent, workspace_id, path)) == 1


def test_get_content_rejects_traversal_absolute_and_agent_internal_paths(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    bad_paths = (
        "../outside.txt",
        "/etc/passwd",
        "notes/../plans/x.md",
        "notes/humaines/../../etc/passwd",
        ".autowork/file-index.json",
        "unknown/thing.md",
        "notes/agents/../humaines/x.md",
        "",
    )
    for path in bad_paths:
        response = get_content(scanning_client, scanning_agent, workspace_id, path)
        assert response.status_code == 400, (path, response.text)
        assert response.json()["error"] == "invalid_path", path


def test_symlink_target_and_intermediate_component_are_refused(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    tree = workspace_tree(scanning_agent)
    outside = tree.parent / "outside.txt"
    outside.write_bytes(b"secret")
    link = tree / "notes" / "humaines" / "lien.md"
    link.symlink_to(outside)
    directory_link = tree / "notes" / "humaines" / "repertoire"
    directory_link.symlink_to(tree.parent)

    final = get_content(scanning_client, scanning_agent, workspace_id, "notes/humaines/lien.md")
    assert final.status_code == 409, final.text
    assert final.json()["error"] == "concurrent_change"

    intermediate = get_content(
        scanning_client, scanning_agent, workspace_id, "notes/humaines/repertoire/outside.txt"
    )
    assert intermediate.status_code == 400, intermediate.text
    assert intermediate.json()["error"] == "invalid_path"

    write = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        "notes/humaines/lien.md",
        b"overwrite attempt",
        key="m24-w4-put-1100",
    )
    assert write.status_code == 409, write.text
    assert write.json()["error"] == "concurrent_change"
    assert outside.read_bytes() == b"secret"
    assert link.is_symlink()


def test_content_reads_are_capped_and_missing_files_are_404(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    tree = workspace_tree(scanning_agent)
    oversized = tree / "notes" / "humaines" / "large.bin"
    oversized.touch()
    os.truncate(oversized, protocol.CONTENT_READ_LIMIT + 1)

    too_large = get_content(
        scanning_client, scanning_agent, workspace_id, "notes/humaines/large.bin"
    )
    assert too_large.status_code == 413, too_large.text
    assert too_large.json()["error"] == "size_limit"

    missing = get_content(scanning_client, scanning_agent, workspace_id, "notes/humaines/absent.md")
    assert missing.status_code == 404
    assert missing.json()["error"] == "invalid_request"


def test_declared_editable_body_above_the_limit_is_refused_with_413(
    uds_client: httpx.Client, agent_server: AgentHandle
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    target = f"/v1/workspaces/{workspace_id}/files?path=notes/humaines/big.md"
    request = (
        f"PUT {target} HTTP/1.1\r\nHost: workspace-agent\r\n".encode()
        + f"Authorization: Bearer {agent_server.token}\r\n".encode()
        + b"Idempotency-Key: m24-w4-raw-0001\r\n"
        + b"X-Autowork-Expected-Sha256: new\r\n"
        + b"X-Autowork-Origin: ui\r\n"
        + f"Content-Length: {protocol.EDITABLE_FILE_LIMIT + 1}\r\n".encode()
        + b"\r\n"
    )

    raw = raw_http_request(agent_server.socket_path, request, timeout=5.0)

    assert response_status(raw) == 413
    assert response_json(raw)["error"] == "size_limit"


def test_revisions_are_newest_first_and_bounded_to_the_known_history(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    path = "notes/humaines/historique.md"
    versions = (b"version one", b"version two", b"version three")
    expected: list[str] = []
    current: str = "new"
    for index, content in enumerate(versions):
        response = put_file(
            scanning_client,
            scanning_agent,
            workspace_id,
            path,
            content,
            key=f"m24-w4-put-12{index:02d}",
            expected=current,
        )
        assert response.status_code in (200, 201), response.text
        expected.append(expected_revision(workspace_id, path, content))
        current = sha256_of(content)

    revisions = get_revisions(scanning_client, scanning_agent, workspace_id, path)
    assert revisions.status_code == 200
    listed = revisions.json()["revisions"]
    assert [entry["revision_id"] for entry in listed] == list(reversed(expected))
    assert listed[0]["supersedes_revision_id"] == expected[1]
    assert listed[1]["supersedes_revision_id"] == expected[0]
    assert listed[2]["supersedes_revision_id"] is None

    unknown = get_revisions(
        scanning_client, scanning_agent, workspace_id, "notes/humaines/inconnu.md"
    )
    assert unknown.status_code == 200
    assert unknown.json() == {"revisions": []}


def test_the_projection_is_persisted_before_the_write_response(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    path = "notes/humaines/projete.md"
    content = b"projected"

    response = put_file(
        scanning_client, scanning_agent, workspace_id, path, content, key="m24-w4-put-1300"
    )

    assert response.status_code == 201
    stored = read_index(scanning_agent)["files"]
    assert isinstance(stored, dict)
    entry = stored[path]
    assert isinstance(entry, dict)
    assert entry["revision_id"] == expected_revision(workspace_id, path, content)
    assert entry["sha256"] == sha256_of(content)
    assert entry["mode"] == "0640"
    assert entry["origin"] == "ui"


def test_a_human_edit_of_a_generated_file_is_preserved_and_flagged(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    path = "plans/plan-002.md"
    created = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        path,
        b"generated plan",
        key="m24-w4-put-1400",
        origin="system",
        generated=True,
        generated_by="bridge",
        source_sha256="1" * 64,
    )
    assert created.status_code == 201
    live = workspace_tree(scanning_agent) / path
    os.chmod(live, 0o640)
    live.write_bytes(b"human edited plan")

    # ``scanning_agent`` is a ``RunningScanAgent``; the protocol covers it.
    scan_via_http(scanning_client, scanning_agent, workspace_id)  # type: ignore[arg-type]

    assert live.read_bytes() == b"human edited plan"
    assert stat.S_IMODE(os.lstat(live).st_mode) == 0o640
    flagged = [
        event
        for event in events_for(scanning_client, scanning_agent, workspace_id, path)
        if event["kind"] == "policy_violation"
    ]
    assert flagged, events_for(scanning_client, scanning_agent, workspace_id, path)


# -- direct service tests ----------------------------------------------------


class ProbeStream:
    """A byte source that runs a probe before the first read."""

    def __init__(self, payload: bytes, probe: Callable[[], None]) -> None:
        self._payload = payload
        self._probe = probe
        self._probed = False

    def read(self, size: int) -> bytes:
        if not self._probed:
            self._probed = True
            self._probe()
        chunk = self._payload[:size]
        self._payload = self._payload[size:]
        return chunk


def direct_write(
    environment: ScanEnvironment,
    service: WorkspaceScanService,
    *,
    path: str,
    content: bytes,
    key: str,
    expected: str = "new",
    route: policies.WriteRoute = policies.WriteRoute.FILES,
    origin: str = "ui",
    task_id: str | None = None,
    generated_by: str | None = None,
    source_sha256: str | None = None,
    source: ProbeStream | None = None,
) -> files.WriteOutcome:
    stream: object = io.BytesIO(content) if source is None else source
    return files.write_file(
        root=environment.root,
        descriptor=environment.descriptor,
        scans=service,
        route=route,
        path=path,
        expected_sha256=expected,
        origin=origin,
        task_id=task_id,
        generated_by=generated_by,
        source_sha256=source_sha256,
        source=stream,  # type: ignore[arg-type]
        declared_length=len(content),
        idempotency_key=key,
    )


def test_mid_write_only_a_tmp_exists_and_the_live_file_stays_visible(
    scan_environment: ScanEnvironment,
) -> None:
    service = scan_environment.service(stability_window_ms=0)
    path = "notes/humaines/atomique.md"
    first = b"live version"
    direct_write(scan_environment, service, path=path, content=first, key="m24-w4-dir-0001")
    live = scan_environment.host_path / path
    before = os.lstat(live)
    observed: list[list[str]] = []

    def probe() -> None:
        observed.append(sorted(os.listdir(live.parent)))
        assert live.read_bytes() == first

    outcome = direct_write(
        scan_environment,
        service,
        path=path,
        content=b"next version",
        key="m24-w4-dir-0002",
        expected=sha256_of(first),
        source=ProbeStream(b"next version", probe),
    )

    assert outcome.status == 200
    after = os.lstat(live)
    assert after.st_ino != before.st_ino
    assert live.read_bytes() == b"next version"
    assert observed, "the probe never ran"
    during = observed[0]
    assert [name for name in during if name.endswith(".tmp")], during
    assert not [name for name in os.listdir(live.parent) if name.endswith(".tmp")]


def test_a_failed_publish_leaves_the_live_file_untouched_and_no_tmp(
    scan_environment: ScanEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = scan_environment.service(stability_window_ms=0)
    path = "notes/humaines/echec.md"
    first = b"original bytes"
    direct_write(scan_environment, service, path=path, content=first, key="m24-w4-dir-0100")
    live = scan_environment.host_path / path
    before = os.lstat(live)
    real_rename = os.rename
    live_name = live.name

    def failing_rename(
        source: object,
        destination: object,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if str(source).startswith(f".{live_name}.") and str(destination) == live_name:
            raise OSError(5, "simulated crash")
        real_rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", failing_rename)

    with pytest.raises(files.FileStorageError):
        direct_write(
            scan_environment,
            service,
            path=path,
            content=b"never published",
            key="m24-w4-dir-0101",
            expected=sha256_of(first),
        )

    after = os.lstat(live)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert live.read_bytes() == first
    assert not [name for name in os.listdir(live.parent) if name.endswith(".tmp")]
