#!/usr/bin/env python3
"""Bootstrap the Blinder Tunnel subject on a fresh stack using source exports."""

from __future__ import annotations

import argparse
import asyncio
import calendar
import gzip
import hashlib
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

FORMAT = "autowork.m14-source-manifest"
MAIN_ARTICLE = "https://unit42.paloaltonetworks.com/blinder-tunnel-targets-critical-infrastructure"
DEFAULT_TITLE = (
    "Blinder Tunnel — campagne d\u2019espionnage contre les infrastructures critiques "
    "irakiennes via de faux recrutements"
)
TLP_RANK = {"CLEAR": 0, "GREEN": 1, "AMBER": 2, "AMBER+STRICT": 3, "RED": 4}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARCHIVED_SOURCE_STATES = {"archived", "extracted", "completed"}


class BootstrapError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApiError(BootstrapError):
    def __init__(self, method: str, path: str, status_code: int, code: str) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.api_code = code
        if status_code == 401:
            message = (
                f"{method} {path} returned 401 ({code}); OPERATOR_API_TOKEN is missing "
                "or was rejected."
            )
        elif status_code == 403 and code == "operator_client_scope_denied":
            message = (
                f"{method} {path} returned 403 ({code}); this workflow requires a human "
                "operator identity, not an operator-client token."
            )
        elif status_code == 403 and code == "human_operator_identity_required":
            message = (
                f"{method} {path} returned 403 ({code}); configure OPERATOR_API_TOKEN "
                "for a human operator identity."
            )
        else:
            message = f"{method} {path} returned HTTP {status_code} ({code})."
        super().__init__(f"http_{status_code}_{code}", message)


@dataclass(frozen=True, slots=True)
class SourceInput:
    name: str
    url: str
    tlp: str
    sha256: str
    size: int
    mime_type: str
    path: Path
    # Exported raw bytes may be gzip (stored as received); the archive route
    # sniffs the media type, so the decoded bytes are what gets uploaded.
    upload_sha256: str = ""
    upload_size: int = 0

    def upload_bytes(self) -> bytes:
        content = self.path.read_bytes()
        return gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content


@dataclass(frozen=True, slots=True)
class BootstrapInputs:
    manifest_path: Path
    manifest_sha256: str
    sources: tuple[SourceInput, ...]
    report_markdown: str
    report_sha256: str
    edition_tlp: str
    external_llm_allowed: bool
    period_start: str
    period_end: str
    period: str


SourceInitializer = Callable[[str], Awaitable[None]]
Sleeper = Callable[[float], Awaitable[None]]


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source URL must not contain embedded credentials")
    query_keys = {key.casefold() for key, _item in parse_qsl(parsed.query, keep_blank_values=True)}
    signed_query_keys = {
        "access_token",
        "token",
        "signature",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
    }
    if query_keys.intersection(signed_query_keys) or (
        "sig" in query_keys and query_keys.intersection({"se", "sp", "sv", "sr"})
    ):
        raise ValueError("signed source URLs are not accepted")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def _normalize_tlp(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("source TLP must be a string")
    normalized = value.strip().upper().replace("TLP:", "").replace("TLP ", "").strip()
    normalized = normalized.replace("AMBER_STRICT", "AMBER+STRICT")
    if normalized not in TLP_RANK:
        raise ValueError("source TLP is not a supported TLP 2.0 label")
    return normalized


def _safe_field(value: str, *, limit: int = 1000) -> str:
    printable = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(printable.split())[:limit]


def _source_bytes_path(source_dir: Path, digest: str) -> Path:
    path = source_dir / digest
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"source bytes file for {digest} is missing or not a regular file")
    return path


def _build_report(sources: tuple[SourceInput, ...]) -> str:
    lines = [
        "## SUBJECT S1",
        f"title: {DEFAULT_TITLE}",
        "presentation: Le dossier regroupe les publications fournies dans "
        "le manifeste des sources. Les affirmations et leurs liens restent "
        "à vérifier à partir des contenus archivés.",
        "actor_or_campaign: Blinder Tunnel",
        "campaigns: Blinder Tunnel",
        "countries: Iraq",
        "technical_potential: 0",
        "technical_potential_reason: À déterminer après examen des contenus archivés.",
        "artifacts: unknown",
        "uncertainties: Les faits et les relations entre publications restent à vérifier.",
    ]
    main_url = canonicalize_url(MAIN_ARTICLE)
    for ordinal, source in enumerate(sources, start=1):
        role = "primary" if canonicalize_url(source.url) == main_url else "unknown"
        lines.extend(
            [
                "",
                f"PUBLICATION P{ordinal}",
                f"title: {_safe_field(source.name)}",
                f"url: {source.url}",
                "publisher: unknown",
                f"source_role: {role}",
                "period_relation: unknown",
                "ioc_presence: unknown",
            ]
        )
    return "\n".join(lines) + "\n"


def load_inputs(
    manifest_path: Path,
    *,
    source_dir: Path | None = None,
    period: str | None = None,
) -> BootstrapInputs:
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("source manifest is missing or not a regular file")
    manifest_path = manifest_path.resolve()
    input_dir = (source_dir or manifest_path.parent).resolve()
    raw_manifest = manifest_path.read_bytes()
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict):
        raise ValueError("source manifest must be a JSON object")
    if manifest.get("format") != FORMAT or manifest.get("schema_version") != 1:
        raise ValueError("unsupported M14 source manifest format")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) != 7:
        raise ValueError("source manifest must contain exactly seven documents")

    sources: list[SourceInput] = []
    seen_urls: set[str] = set()
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("source manifest document entry is invalid")
        name = document.get("original_name", document.get("name"))
        url = document.get("url", document.get("origin"))
        digest = document.get("sha256")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("source manifest document name is missing")
        if not isinstance(url, str) or not url.strip() or any(ch.isspace() for ch in url):
            raise ValueError("source manifest document URL is missing or invalid")
        canonical_url = canonicalize_url(url)
        if canonical_url in seen_urls:
            raise ValueError("source manifest contains duplicate canonical source URLs")
        seen_urls.add(canonical_url)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValueError("source manifest SHA-256 is invalid")
        digest = digest.lower()
        tlp = _normalize_tlp(document.get("tlp"))
        path = _source_bytes_path(input_dir, digest)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"source bytes do not match manifest SHA-256 {digest}")
        size = document.get("size", len(content))
        if type(size) is not int or size < 0 or size != len(content):
            raise ValueError(f"source bytes do not match manifest size for {digest}")
        upload = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
        mime_type = (
            document.get("mime") or document.get("stored_mime") or "application/octet-stream"
        )
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise ValueError("source manifest MIME type is invalid")
        sources.append(
            SourceInput(
                name=_safe_field(name, limit=500),
                url=url.strip(),
                tlp=tlp,
                sha256=digest,
                size=size,
                mime_type=mime_type.strip(),
                path=path,
                upload_sha256=hashlib.sha256(upload).hexdigest(),
                upload_size=len(upload),
            )
        )

    if canonicalize_url(MAIN_ARTICLE) not in seen_urls:
        raise ValueError("source manifest does not contain the required Unit 42 article")
    strictest_tlp = max((source.tlp for source in sources), key=TLP_RANK.__getitem__)
    if period is None:
        period = datetime.now(UTC).strftime("%Y-%m")
    match = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if not match:
        raise ValueError("edition period must use YYYY-MM")
    year, month = map(int, match.groups())
    if month < 1 or month > 12:
        raise ValueError("edition period month must be between 01 and 12")
    report = _build_report(tuple(sources))
    return BootstrapInputs(
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        sources=tuple(sources),
        report_markdown=report,
        report_sha256=hashlib.sha256(report.encode("utf-8")).hexdigest(),
        edition_tlp=strictest_tlp,
        external_llm_allowed=strictest_tlp not in {"AMBER+STRICT", "RED"},
        period_start=f"{year:04d}-{month:02d}-01",
        period_end=f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}",
        period=period,
    )


def build_plan(inputs: BootstrapInputs, *, create_investigation: bool = False) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "manifest": str(inputs.manifest_path),
        "manifest_sha256": inputs.manifest_sha256,
        "source_count": len(inputs.sources),
        "source_sha256": [source.sha256 for source in inputs.sources],
        "edition": {
            "lookup": "GET /api/editions?country_code=IQ&period={period}",
            "create": "POST /api/editions",
            "period": inputs.period,
            "period_start": inputs.period_start,
            "period_end": inputs.period_end,
            "country": "Iraq",
            "country_code": "IQ",
            "tlp": inputs.edition_tlp,
            "languages": ["en"],
        },
        "discovery": {
            "preview": "POST /api/editions/{edition_id}/discovery/import/preview",
            "confirm": "POST /api/editions/{edition_id}/discovery/import/confirm",
            "source_mode": "manual_import",
            "report_sha256": inputs.report_sha256,
            "subject_title": DEFAULT_TITLE,
            "subject_count": 1,
            "publication_count": len(inputs.sources),
        },
        "selection": (
            "GET /api/editions/{edition_id}/selection; "
            "POST /api/editions/{edition_id}/selection/decisions"
        ),
        "source_collection_initialization": {
            "http_route": None,
            "in_container_service": "SubjectCollectionService.initialize(subject_id)",
            "network_collection_started": False,
        },
        "source_archival": {
            "route": "POST /api/subjects/{subject_id}/sources/{collection_id}/content",
            "content_origin": "verified local manifest files only",
            "source_count": len(inputs.sources),
        },
        "production": {
            "start": "POST /api/editions/{edition_id}/production/batches",
            "poll": "GET /api/subjects/{subject_id}/production",
            "synthesis": "GET /api/subjects/{subject_id}/production/artifacts/synthesis",
            "external_llm_allowed": inputs.external_llm_allowed,
        },
        "create_investigation": create_investigation,
    }


def _detail_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "api_error"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"][:100]
    return "api_error"


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    expected: tuple[int, ...] = (200,),
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> Any:
    response = await client.request(
        method,
        path,
        json=json_body if files is None and data is None else None,
        headers=headers,
        data=data,
        files=files,
    )
    if response.status_code not in expected:
        raise ApiError(method, path, response.status_code, _detail_code(response))
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise BootstrapError("api_invalid_json", f"{method} {path} returned invalid JSON.") from exc


async def _get(client: httpx.AsyncClient, path: str) -> Any:
    return await _request_json(client, "GET", path)


def _stable_key(stage: str, *values: str) -> str:
    material = "\0".join(values).encode("utf-8")
    return f"m14-bootstrap:{stage}:{hashlib.sha256(material).hexdigest()}"


async def _ensure_edition(client: httpx.AsyncClient, inputs: BootstrapInputs) -> dict[str, Any]:
    path = f"/api/editions?country_code=IQ&period={inputs.period}&page_size=100"
    page = await _get(client, path)
    items = page.get("items", []) if isinstance(page, dict) else []
    matching = [
        item
        for item in items
        if item.get("country_code") == "IQ"
        and item.get("period_start") == inputs.period_start
        and item.get("period_end") == inputs.period_end
    ]
    if len(matching) > 1:
        raise BootstrapError(
            "edition_duplicate", "More than one edition matches the requested period."
        )
    if matching:
        edition = matching[0]
        if edition.get("state") != "open":
            raise BootstrapError(
                "edition_archived", "The matching edition is archived and cannot be reused."
            )
        existing_tlp = _normalize_tlp(edition.get("tlp"))
        if TLP_RANK[existing_tlp] < TLP_RANK[inputs.edition_tlp]:
            raise BootstrapError(
                "edition_tlp_insufficient",
                "The matching edition has a lower TLP than the source manifest; "
                "refusing to import.",
            )
        return edition
    body = {
        "country": "Iraq",
        "country_code": "IQ",
        "period_start": inputs.period_start,
        "period_end": inputs.period_end,
        "tlp": inputs.edition_tlp,
        "languages": ["en"],
    }
    try:
        return await _request_json(client, "POST", "/api/editions", expected=(201,), json_body=body)
    except ApiError as exc:
        if exc.status_code != 409:
            raise
        # The edition's business key is the idempotency boundary for this API.
        refreshed = await _get(client, path)
        candidates = refreshed.get("items", []) if isinstance(refreshed, dict) else []
        exact = [
            item
            for item in candidates
            if item.get("country_code") == "IQ"
            and item.get("period_start") == inputs.period_start
            and item.get("period_end") == inputs.period_end
        ]
        if len(exact) == 1 and exact[0].get("state") == "open":
            existing_tlp = _normalize_tlp(exact[0].get("tlp"))
            if TLP_RANK[existing_tlp] < TLP_RANK[inputs.edition_tlp]:
                raise BootstrapError(
                    "edition_tlp_insufficient",
                    "The matching edition has a lower TLP than the source manifest; "
                    "refusing to import.",
                ) from None
            return exact[0]
        raise


async def _poll_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    poll_interval: float,
    timeout_seconds: float,
    sleep: Sleeper,
) -> dict[str, Any]:
    max_polls = max(1, int(timeout_seconds / max(poll_interval, 1.0)) + 1)
    for attempt in range(max_polls):
        job = await _get(client, f"/api/jobs/{job_id}")
        status = job.get("status")
        if status == "succeeded":
            return job
        if status in {"failed", "cancelled", "waiting_human"}:
            raise BootstrapError(
                f"discovery_reconciliation_{status}",
                f"Discovery reconciliation job {job_id} stopped in state {status}.",
            )
        if attempt + 1 < max_polls:
            await sleep(poll_interval)
    raise BootstrapError("discovery_reconciliation_timeout", f"Timed out waiting for job {job_id}.")


async def _selection_item(
    client: httpx.AsyncClient,
    edition_id: str,
    *,
    poll_interval: float,
    timeout_seconds: float,
    sleep: Sleeper,
) -> tuple[dict[str, Any], dict[str, Any]]:
    max_polls = max(1, int(timeout_seconds / max(poll_interval, 1.0)) + 1)
    for attempt in range(max_polls):
        board = await _get(client, f"/api/editions/{edition_id}/selection")
        items = board.get("items", []) if isinstance(board, dict) else []
        matches = [item for item in items if item.get("title") == DEFAULT_TITLE]
        if len(matches) > 1:
            raise BootstrapError(
                "selection_ambiguous",
                "The imported subject title is ambiguous on the selection board.",
            )
        if matches and board.get("snapshot_version"):
            item = matches[0]
            if item.get("selectable") is not True:
                blocking_reason = item.get("blocking_reason") or "unknown reason"
                raise BootstrapError(
                    "selection_not_selectable",
                    f"Imported subject is not selectable ({blocking_reason}).",
                )
            return board, item
        if attempt + 1 < max_polls:
            await sleep(poll_interval)
    raise BootstrapError(
        "selection_snapshot_timeout", "No reconciled selection snapshot became available."
    )


def _source_filename(name: str) -> str:
    filename = Path(name).name
    filename = "".join(ch for ch in filename if ch.isprintable() and ch not in {'"', "'"})
    return filename[:180] or "source.bin"


def _source_map(workbench: Any) -> dict[str, dict[str, Any]]:
    entries = workbench.get("sources", []) if isinstance(workbench, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        url = entry.get("requested_url")
        if not isinstance(url, str):
            continue
        result[canonicalize_url(url)] = entry
    return result


def _receipt_matches(entry: dict[str, Any], source: SourceInput) -> bool:
    receipt = entry.get("archive_receipt")
    return (
        isinstance(receipt, dict)
        and receipt.get("encoded_sha256") == (source.upload_sha256 or source.sha256)
        and receipt.get("bytes") == (source.upload_size or source.size)
    )


async def _archive_sources(
    client: httpx.AsyncClient,
    subject_id: str,
    sources: tuple[SourceInput, ...],
    *,
    source_initializer: SourceInitializer | None,
) -> None:
    workbench = await _get(client, f"/api/subjects/{subject_id}/workbench")
    by_url = _source_map(workbench)
    wanted = {canonicalize_url(source.url) for source in sources}
    if not wanted.issubset(by_url):
        if source_initializer is None:
            raise BootstrapError(
                "source_collection_init_route_missing",
                "The fresh subject has no collection rows. "
                "POST /api/subjects/{subject_id}/collection also starts network collection, "
                "so host mode stopped. Run inside the backend container to call "
                "SubjectCollectionService.initialize(subject_id), which creates rows "
                "without fetching.",
            )
        await source_initializer(subject_id)
        workbench = await _get(client, f"/api/subjects/{subject_id}/workbench")
        by_url = _source_map(workbench)
    missing = wanted.difference(by_url)
    if missing:
        raise BootstrapError(
            "source_collection_missing",
            f"SourceCollectionService.initialize left {len(missing)} manifest URL(s) unattached.",
        )

    for source in sources:
        entry = by_url[canonicalize_url(source.url)]
        state = entry.get("state")
        if state in ARCHIVED_SOURCE_STATES:
            if not _receipt_matches(entry, source):
                raise BootstrapError(
                    "source_archive_hash_mismatch",
                    f"Existing archived source for {source.url} does not match its exported bytes.",
                )
            continue
        content = source.upload_bytes()
        if (
            len(content) != (source.upload_size or source.size)
            or hashlib.sha256(content).hexdigest() != (source.upload_sha256 or source.sha256)
        ):
            raise BootstrapError(
                "source_bytes_changed", f"Source file changed after preflight: {source.sha256}."
            )
        collection_id = entry.get("id")
        if not isinstance(collection_id, str) or not collection_id:
            raise BootstrapError(
                "source_collection_id_missing", "A source collection row has no ID."
            )
        path = f"/api/subjects/{subject_id}/sources/{collection_id}/content"
        form_data = {
            "declared_mime_type": source.mime_type,
            "final_url": source.url,
        }
        try:
            archived = await _request_json(
                client,
                "POST",
                path,
                data=form_data,
                files={"file": (_source_filename(source.name), content, source.mime_type)},
            )
        except ApiError as exc:
            if exc.status_code == 409:
                refreshed = await _get(client, f"/api/subjects/{subject_id}/workbench")
                current = _source_map(refreshed).get(canonicalize_url(source.url), {})
                if current.get("state") in ARCHIVED_SOURCE_STATES and _receipt_matches(
                    current, source
                ):
                    continue
            raise
        if not _receipt_matches(archived, source):
            raise BootstrapError(
                "source_archive_receipt_mismatch",
                f"Manual archive receipt for {source.url} does not verify the exported "
                "SHA-256 and size.",
            )


async def _find_run_id(
    client: httpx.AsyncClient, edition_id: str, batch: dict[str, Any], subject_id: str
) -> str:
    details = batch.get("item_details", []) if isinstance(batch, dict) else []
    matches = [item for item in details if item.get("subject_id") == subject_id]
    if len(matches) == 1 and isinstance(matches[0].get("run_id"), str):
        return matches[0]["run_id"]
    board = await _get(client, f"/api/editions/{edition_id}/production")
    candidates = [board.get("active_batch"), *(board.get("recent_batches") or [])]
    for item in candidates:
        if not isinstance(item, dict) or item.get("batch_id") != batch.get("batch_id"):
            continue
        run_matches = [
            entry for entry in item.get("item_details", []) if entry.get("subject_id") == subject_id
        ]
        if len(run_matches) == 1 and isinstance(run_matches[0].get("run_id"), str):
            return run_matches[0]["run_id"]
    raise BootstrapError(
        "production_run_id_missing", "Production batch did not expose its subject run ID."
    )


async def _wait_for_production(
    client: httpx.AsyncClient,
    *,
    edition_id: str,
    subject_id: str,
    batch_id: str,
    poll_interval: float,
    timeout_seconds: float,
    sleep: Sleeper,
    on_status: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    max_polls = max(1, int(timeout_seconds / max(poll_interval, 1.0)) + 1)
    resumed_cancelled_batch = False
    for attempt in range(max_polls):
        status_view = await _get(client, f"/api/subjects/{subject_id}/production")
        on_status(status_view)
        status = status_view.get("status")
        if status == "ready":
            return status_view
        if status == "cancelled" and not resumed_cancelled_batch and status_view.get("resume_plan"):
            await _request_json(
                client,
                "POST",
                f"/api/editions/{edition_id}/production/{batch_id}/resume",
            )
            resumed_cancelled_batch = True
        elif status in {"needs_review", "failed"} or status_view.get("reconciliation"):
            return status_view
        elif status == "cancelled":
            return status_view
        if attempt + 1 < max_polls:
            await sleep(poll_interval)
    raise BootstrapError(
        "production_timeout", f"Timed out waiting for production run for subject {subject_id}."
    )


async def _create_investigation(
    client: httpx.AsyncClient,
    *,
    subject_id: str,
    run_id: str,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = artifact.get("artifact_id")
    synthesis_sha256 = artifact.get("canonical_sha256")
    if not isinstance(artifact_id, str) or not isinstance(synthesis_sha256, str):
        raise BootstrapError(
            "synthesis_identity_missing",
            "Verified synthesis is missing its ID or canonical SHA-256.",
        )
    result = await _request_json(
        client,
        "POST",
        f"/api/subjects/{subject_id}/investigations",
        json_body={
            "production_run_id": run_id,
            "synthesis_artifact_id": artifact_id,
            "synthesis_sha256": synthesis_sha256,
            "execution_mode": "operator_led",
        },
    )
    investigation = result.get("investigation") if isinstance(result, dict) else None
    investigation_id = investigation.get("id") if isinstance(investigation, dict) else None
    if not isinstance(investigation_id, str):
        raise BootstrapError(
            "investigation_id_missing", "Investigation creation response did not include an ID."
        )
    envelope = await _get(client, f"/api/investigations/{investigation_id}/envelope")
    return {
        "investigation": investigation,
        "job_id": result.get("job_id"),
        "idempotent_replay": result.get("idempotent_replay"),
        "g2_envelope": envelope,
    }


async def bootstrap_subject(
    client: httpx.AsyncClient,
    inputs: BootstrapInputs,
    *,
    source_initializer: SourceInitializer | None = None,
    create_investigation: bool = False,
    poll_interval: float = 5.0,
    timeout_seconds: float = 7200.0,
    sleep: Sleeper = asyncio.sleep,
    on_status: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    emit_status = on_status or (lambda _status: None)
    edition = await _ensure_edition(client, inputs)
    edition_id = str(edition["id"])
    effective_edition_tlp = _normalize_tlp(edition.get("tlp"))
    external_llm_allowed = inputs.external_llm_allowed and effective_edition_tlp not in {
        "AMBER+STRICT",
        "RED",
    }
    preview = await _request_json(
        client,
        "POST",
        f"/api/editions/{edition_id}/discovery/import/preview",
        json_body={
            "source_profile": "manual-import",
            "markdown": inputs.report_markdown,
            "complementary_axis": "M14 source-manifest import",
            "sensitivity": "internal",
            "external_llm_allowed": external_llm_allowed,
        },
    )
    if (
        preview.get("sha256") != inputs.report_sha256
        or preview.get("subject_count") != 1
        or preview.get("publication_count") != len(inputs.sources)
    ):
        raise BootstrapError(
            "discovery_preview_mismatch",
            "Manual discovery preview did not match the one-subject/seven-source plan.",
        )
    discovery_key = _stable_key(
        "discovery", edition_id, inputs.manifest_sha256, inputs.report_sha256
    )
    imported = await _request_json(
        client,
        "POST",
        f"/api/editions/{edition_id}/discovery/import/confirm",
        headers={"Idempotency-Key": discovery_key},
        json_body={
            "source_profile": "manual-import",
            "markdown": inputs.report_markdown,
            "complementary_axis": "M14 source-manifest import",
            "sensitivity": "internal",
            "external_llm_allowed": external_llm_allowed,
            "expected_sha256": preview["sha256"],
        },
    )
    if imported.get("source_mode") != "manual_import" or imported.get("subject_count") != 1:
        raise BootstrapError(
            "discovery_import_unexpected",
            "Discovery confirmation did not create one manual_import subject.",
        )
    reconciliation_job_id = imported.get("reconciliation_job_id")
    if reconciliation_job_id:
        await _poll_job(
            client,
            str(reconciliation_job_id),
            poll_interval=poll_interval,
            timeout_seconds=min(timeout_seconds, 1800.0),
            sleep=sleep,
        )

    board, item = await _selection_item(
        client,
        edition_id,
        poll_interval=poll_interval,
        timeout_seconds=min(timeout_seconds, 1800.0),
        sleep=sleep,
    )
    discovery_subject_id = item.get("discovery_subject_id")
    if not isinstance(discovery_subject_id, str):
        raise BootstrapError(
            "discovery_subject_id_missing", "Selection board entry is missing discovery_subject_id."
        )
    selection_key = _stable_key(
        "selection",
        edition_id,
        inputs.manifest_sha256,
        discovery_subject_id,
        str(board["snapshot_version"]),
    )
    selected = await _request_json(
        client,
        "POST",
        f"/api/editions/{edition_id}/selection/decisions",
        headers={"Idempotency-Key": selection_key},
        json_body={
            "snapshot_version": board["snapshot_version"],
            "decisions": [{"discovery_subject_id": discovery_subject_id, "action": "select"}],
        },
    )
    selected_items = [
        entry
        for entry in selected.get("items", [])
        if entry.get("discovery_subject_id") == discovery_subject_id
    ]
    if len(selected_items) != 1 or not isinstance(selected_items[0].get("subject_id"), str):
        raise BootstrapError(
            "subject_materialization_missing", "Selection did not materialize the subject."
        )
    subject_id = selected_items[0]["subject_id"]

    await _archive_sources(
        client,
        subject_id,
        inputs.sources,
        source_initializer=source_initializer,
    )
    batch = await _request_json(
        client,
        "POST",
        f"/api/editions/{edition_id}/production/batches",
        headers={
            "Idempotency-Key": _stable_key(
                "production", edition_id, subject_id, inputs.manifest_sha256
            )
        },
        json_body={"subject_ids": [subject_id]},
    )
    batch_id = batch.get("batch_id") if isinstance(batch, dict) else None
    if not isinstance(batch_id, str):
        raise BootstrapError(
            "production_batch_id_missing", "Production start response did not include batch_id."
        )
    run_id = await _find_run_id(client, edition_id, batch, subject_id)
    status_view = await _wait_for_production(
        client,
        edition_id=edition_id,
        subject_id=subject_id,
        batch_id=batch_id,
        poll_interval=poll_interval,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        on_status=emit_status,
    )
    state = status_view.get("status")
    if state != "ready":
        return {
            "status": state,
            "edition_id": edition_id,
            "subject_id": subject_id,
            "production_run_id": status_view.get("run_id", run_id),
            "production_status": {
                "status": state,
                "current_stage": status_view.get("current_stage"),
                "error_code": status_view.get("error_code"),
                "reconciliation": status_view.get("reconciliation"),
            },
            "reconciliation": status_view.get("reconciliation"),
        }

    artifact = await _get(client, f"/api/subjects/{subject_id}/production/artifacts/synthesis")
    if artifact.get("status") != "verified":
        raise BootstrapError(
            "synthesis_not_verified",
            "Production is ready but the synthesis artifact is not verified.",
        )
    result: dict[str, Any] = {
        "status": "complete",
        "edition_id": edition_id,
        "subject_id": subject_id,
        "production_run_id": artifact.get("production_run_id", run_id),
        "synthesis_artifact_id": artifact.get("artifact_id"),
        "synthesis_sha256": artifact.get("canonical_sha256"),
    }
    if not isinstance(result["synthesis_artifact_id"], str) or not isinstance(
        result["synthesis_sha256"], str
    ):
        raise BootstrapError(
            "synthesis_identity_missing",
            "Verified synthesis is missing its ID or canonical SHA-256.",
        )
    if create_investigation:
        result["investigation_result"] = await _create_investigation(
            client,
            subject_id=subject_id,
            run_id=str(result["production_run_id"]),
            artifact=artifact,
        )
    return result


async def initialize_source_collections_in_container(subject_id: str) -> None:
    """Call the source-row initializer without dispatching the network collector."""
    from cti_app.application.collection import SubjectCollectionService
    from cti_app.application.http_collection import (
        CollectionPolicy,
        SafeHttpCollector,
        SystemDnsResolver,
        parse_domain_policy,
    )
    from cti_app.config import Settings
    from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
    from cti_app.infrastructure.database.session import (
        create_postgres_engine,
        create_session_factory,
    )
    from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from cti_app.infrastructure.http import AsyncioPinnedHttpTransport
    from minio import Minio

    settings = Settings(_env_file=None)
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> Any:
        return SqlAlchemyUnitOfWork(session_factory)

    collector = SafeHttpCollector(
        AsyncioPinnedHttpTransport(),
        SystemDnsResolver(),
        CollectionPolicy(
            max_redirects=settings.collection_max_redirects,
            timeout_seconds=settings.collection_timeout_seconds,
            max_download_bytes=settings.collection_max_download_bytes,
            max_expanded_bytes=settings.collection_max_expanded_bytes,
            max_decompression_ratio=settings.collection_max_decompression_ratio,
            allowed_domains=parse_domain_policy(settings.collection_allowed_domains),
            blocked_domains=parse_domain_policy(settings.collection_blocked_domains),
        ),
    )
    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    try:
        service = SubjectCollectionService(uow_factory, collector, blob_store)
        await service.initialize(UUID(subject_id))
    finally:
        await engine.dispose()


def _default_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="sources_manifest.json")
    parser.add_argument(
        "--source-dir", type=Path, help="directory containing SHA-256-named source files"
    )
    parser.add_argument(
        "--period",
        default=_default_period(),
        help="edition month as YYYY-MM; defaults to current UTC month",
    )
    parser.add_argument(
        "--base-url",
        help=(
            "API base URL for host mode; fresh-stack source initialization requires "
            "in-container mode"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print the API plan without sending requests",
    )
    parser.add_argument(
        "--create-investigation",
        action="store_true",
        help=(
            "create the operator_led investigation and display its G2 envelope without approving it"
        ),
    )
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    inputs = load_inputs(args.manifest, source_dir=args.source_dir, period=args.period)
    if args.poll_interval < 0 or args.timeout_seconds <= 0:
        raise ValueError("poll interval must be non-negative and timeout must be positive")
    if args.dry_run:
        print(
            json.dumps(
                build_plan(inputs, create_investigation=args.create_investigation),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    token = os.environ.get("OPERATOR_API_TOKEN", "").strip()
    if not token:
        raise BootstrapError(
            "operator_token_missing", "OPERATOR_API_TOKEN is not set in the process environment."
        )
    base_url = (args.base_url or "http://127.0.0.1:8000").rstrip("/")
    parsed_base_url = urlsplit(base_url)
    if (
        parsed_base_url.scheme not in {"http", "https"}
        or not parsed_base_url.hostname
        or parsed_base_url.username
        or parsed_base_url.password
        or parsed_base_url.query
        or parsed_base_url.fragment
    ):
        raise ValueError("base URL must be an HTTP(S) URL without embedded credentials")
    initializer = None if args.base_url else initialize_source_collections_in_container

    def report_status(status: dict[str, Any]) -> None:
        print(
            json.dumps(
                {
                    "event": "production_status",
                    "status": status.get("status"),
                    "run_id": status.get("run_id"),
                    "current_stage": status.get("current_stage"),
                    "error_code": status.get("error_code"),
                    "reconciliation": status.get("reconciliation"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(60.0, connect=10.0),
        trust_env=False,
    ) as client:
        result = await bootstrap_subject(
            client,
            inputs,
            source_initializer=initializer,
            create_investigation=args.create_investigation,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
            on_status=report_status,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "complete" else 2


def main() -> int:
    args = _parser().parse_args()
    try:
        return asyncio.run(_main_async(args))
    except ApiError as exc:
        print(
            json.dumps(
                {"status": "error", "code": exc.code, "message": str(exc)}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 1
    except BootstrapError as exc:
        print(
            json.dumps(
                {"status": "error", "code": exc.code, "message": str(exc)}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # Keep exception text out of logs: settings and URLs may be sensitive.
        print(
            json.dumps(
                {"status": "error", "code": "bootstrap_error", "error_type": type(exc).__name__}
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
