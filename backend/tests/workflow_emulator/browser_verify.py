"""Verify W10 browser publication state directly in the isolated PostgreSQL database."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, TypeGuard
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.edition_publication import EditionDocumentV2, PublicationManifestV2
from cti_app.domain.investigation import SampleQualification
from cti_app.domain.publication_document import PublicationDocumentV6
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

EXPECTED_ACTOR = "workflow-analyst"


def _is_string_mapping(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssertionError(f"state field {name!r} must be a non-empty string")
    return value


def _require(condition: bool, step: str, message: str) -> None:
    if not condition:
        raise AssertionError(f"W10-browser-exclusion / {step}: {message}")


def _extract_pdf_text(pdf_path: Path) -> str:
    executable = shutil.which("pdftotext")
    if executable is None:
        raise AssertionError("J2-browser-investigation / Poppler pdftotext is required")
    result = subprocess.run(
        [executable, str(pdf_path), "-"],
        check=True,
        capture_output=True,
        timeout=30,
        env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    return result.stdout.decode("utf-8", errors="replace")


async def _verify_investigation(state: dict[str, object], admin_dsn: str) -> None:
    def text_value(value: object, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise AssertionError(f"J2-browser-investigation / state {name!r} is missing")
        return value

    def integer_value(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise AssertionError(f"J2-browser-investigation / state {name!r} is not an integer")
        return value

    edition_id = text_value(state.get("edition_id"), "edition_id")
    database_name = text_value(state.get("database_name"), "database_name")
    blob_root = Path(text_value(state.get("blob_root"), "blob_root"))
    investigation_id = text_value(state.get("investigation_id"), "investigation_id")
    base_publication_id = text_value(
        state.get("base_publication_artifact_id"), "base_publication_artifact_id"
    )
    augmented_publication_id = text_value(
        state.get("browser_result", {}).get("augmented_publication_artifact_id")
        if isinstance(state.get("browser_result"), dict)
        else None,
        "browser_result.augmented_publication_artifact_id",
    )
    expected_candidate_hash = text_value(state.get("candidate_sha256"), "candidate_sha256")
    historical_value = state.get("historic_release")
    browser_value = state.get("browser_result")
    _require(isinstance(historical_value, dict), "state", "historic_release must be an object")
    _require(isinstance(browser_value, dict), "state", "browser_result must be an object")
    history = historical_value
    browser_result = browser_value
    subjects_value = state.get("subjects")
    _require(isinstance(subjects_value, dict), "state", "subjects must be an object")
    subjects = subjects_value
    subject_ids = {
        label: text_value(subjects.get(label), f"subjects.{label}") for label in ("A", "B", "C")
    }

    initial_composition_version = integer_value(
        state.get("initial_composition_version"), "initial_composition_version"
    )
    before_repin_version = integer_value(
        browser_result.get("composition_before_repin_version"),
        "browser_result.composition_before_repin_version",
    )
    after_repin_version = integer_value(
        browser_result.get("composition_after_repin_version"),
        "browser_result.composition_after_repin_version",
    )
    _require(
        before_repin_version == initial_composition_version,
        "composition before repin",
        "investigation and opinion publication changed the pinned composition",
    )
    _require(
        after_repin_version == before_repin_version + 1,
        "composition repin",
        f"expected one CAS version increment, got v{before_repin_version} → v{after_repin_version}",
    )
    _require(
        browser_result.get("composition_before_repin_a_artifact_id") == base_publication_id,
        "composition before repin",
        "Article A did not remain pinned to its original V5 publication before the human action",
    )
    _require(
        browser_result.get("composition_after_repin_a_artifact_id") == augmented_publication_id,
        "composition after repin",
        "Article A does not pin the augmented PUBLICATION returned by the UI action",
    )
    _require(
        text_value(browser_result.get("synthesis_sha256_ui"), "synthesis_sha256_ui")
        == text_value(state.get("synthesis_sha256"), "synthesis_sha256"),
        "exact synthesis",
        "the SHA-256 shown in the subject UI differs from the seeded synthesis artifact",
    )
    _require(
        browser_result.get("g2_status") == 200,
        "G2",
        "UI G2 approval did not return HTTP 200",
    )
    _require(browser_result.get("g2_version") == 1, "G2", "expected approved envelope version 1")
    _require(
        browser_result.get("g3_status") == 200,
        "G3",
        "UI G3 qualification did not return HTTP 200",
    )
    _require(
        browser_result.get("accept_status") == 202,
        "accept",
        "UI acceptance did not return HTTP 202",
    )
    _require(
        integer_value(browser_result.get("opinion_approved_version"), "opinion_approved_version")
        == 3,
        "opinion version",
        # compose=v1, human revision=v2, approval appends the immutable approved v3.
        "expected the human-revised draft v2 to be approved as appended version 3",
    )
    _require(
        text_value(browser_result.get("review_marker"), "review_marker")
        == "Relecture humaine J2 après qualification de S2.",
        "opinion revision",
        "the expected human review marker was not recorded",
    )
    for key in ("investigation_job_statuses", "publication_job_statuses"):
        statuses = browser_result.get(key)
        _require(
            isinstance(statuses, list)
            and statuses
            and all(item == "succeeded" for item in statuses),
            "JobExecutor",
            f"{key} must contain only succeeded jobs, observed {statuses!r}",
        )
    fixtures = browser_result.get("investigation_fixture_stats")
    _require(isinstance(fixtures, dict), "fixture transport", "fixture counters are missing")
    _require(
        fixtures.get("bridge_submissions") == 6
        and fixtures.get("vt_file_reports") == 1
        and fixtures.get("vt_searches") == 1
        and fixtures.get("vt_download_calls") == 1
        and fixtures.get("vt_downloaded_sha256s") == [expected_candidate_hash]
        and fixtures.get("shodan_calls") == 1,
        "fixture transport",
        f"scripted Bridge/VT/Shodan calls did not match the deterministic scenario: {fixtures!r}",
    )

    downloaded_pdf_path = Path(
        text_value(browser_result.get("downloaded_pdf_path"), "downloaded_pdf_path")
    )
    try:
        downloaded_pdf = await asyncio.to_thread(downloaded_pdf_path.read_bytes)
    except OSError as exc:
        raise AssertionError(
            f"J2-browser-investigation / browser PDF download is unreadable: {exc}"
        ) from exc
    downloaded_pdf_sha256 = hashlib.sha256(downloaded_pdf).hexdigest()
    _require(
        downloaded_pdf_sha256
        == text_value(browser_result.get("downloaded_pdf_sha256"), "downloaded_pdf_sha256"),
        "downloaded PDF",
        "downloaded bytes differ from the SHA-256 recorded by Playwright",
    )

    database_dsn = (
        make_url(admin_dsn).set(database=database_name).render_as_string(hide_password=False)
    )
    engine = create_postgres_engine(database_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(blob_root), uow_factory)
    )
    try:
        history_manifest_id = text_value(history.get("manifest_id"), "historic_release.manifest_id")
        history_release_id = text_value(history.get("release_id"), "historic_release.release_id")
        history_render_id = text_value(history.get("render_id"), "historic_release.render_id")
        final_manifest_id = text_value(browser_result.get("manifest_id"), "manifest_id")

        async with uow_factory() as uow:
            history_manifest = await uow.publication_manifests.get(UUID(history_manifest_id))
            history_manifest_blob_id = await uow.publication_manifests.get_blob_id(
                UUID(history_manifest_id)
            )
            history_release = await uow.edition_releases.get(UUID(history_release_id))
            history_render = await uow.edition_renders.get(UUID(history_render_id))
            final_manifest = await uow.publication_manifests.get(UUID(final_manifest_id))
            final_manifest_blob_id = await uow.publication_manifests.get_blob_id(
                UUID(final_manifest_id)
            )
            final_release = await uow.edition_releases.get_by_manifest(UUID(final_manifest_id))
            final_render = (
                await uow.edition_renders.get_latest_succeeded_for_release(final_release.id)
                if final_release is not None
                else None
            )
            associations = await uow.sample_associations.list_for_investigation(
                UUID(investigation_id)
            )

        _require(
            history_manifest is not None
            and history_release is not None
            and history_render is not None,
            "historical release",
            "the original manifest, release, or render is no longer readable in PostgreSQL",
        )
        _require(
            history_manifest_blob_id is not None,
            "historical manifest",
            "the original manifest blob reference is missing",
        )
        _require(
            final_manifest is not None
            and final_release is not None
            and final_render is not None
            and final_manifest_blob_id is not None,
            "final release",
            "the repinned manifest, release, or successful render is missing",
        )
        _require(
            final_manifest.id == UUID(final_manifest_id)
            and final_manifest.edition_id == UUID(edition_id)
            and final_manifest.composition_version == after_repin_version,
            "final manifest",
            "the new manifest does not freeze the repinned composition version",
        )
        _require(
            final_release.manifest_id == final_manifest.id
            and final_release.edition_id == UUID(edition_id),
            "final release",
            "the release does not point to the UI-accepted manifest",
        )

        history_manifest_bytes = await artifact_store.read_bytes(history_manifest_blob_id)
        history_document_bytes = await artifact_store.read_bytes(
            history_release.edition_document_blob_id
        )
        history_pdf_bytes = await artifact_store.read_bytes(history_render.output_blob_id)
        _require(
            hashlib.sha256(history_manifest_bytes).hexdigest()
            == text_value(history.get("manifest_sha256"), "historic_release.manifest_sha256")
            and history_manifest.content_sha256
            == text_value(
                history.get("manifest_content_sha256"),
                "historic_release.manifest_content_sha256",
            ),
            "historical manifest",
            "the original manifest bytes or canonical hash changed after repin",
        )
        _require(
            hashlib.sha256(history_document_bytes).hexdigest()
            == text_value(
                history.get("edition_document_blob_sha256"),
                "historic_release.edition_document_blob_sha256",
            )
            and history_release.edition_document_sha256
            == text_value(
                history.get("edition_document_sha256"),
                "historic_release.edition_document_sha256",
            ),
            "historical release",
            "the original assembled edition document changed after repin",
        )
        _require(
            hashlib.sha256(history_pdf_bytes).hexdigest()
            == text_value(history.get("pdf_sha256"), "historic_release.pdf_sha256")
            and history_render.output_sha256 == history.get("pdf_sha256"),
            "historical PDF",
            "the original release PDF bytes or render hash changed after repin",
        )
        final_manifest_bytes = await artifact_store.read_bytes(final_manifest_blob_id)
        _require(
            final_manifest.content_sha256
            == text_value(browser_result.get("manifest_sha256"), "manifest_sha256"),
            "final manifest",
            "the UI response canonical hash does not match the persisted manifest",
        )
        old_manifest = PublicationManifestV2.from_json(json.loads(history_manifest_bytes))
        new_manifest = PublicationManifestV2.from_json(json.loads(final_manifest_bytes))
        old_entries = {entry.subject_id: entry for entry in old_manifest.entries}
        new_entries = {entry.subject_id: entry for entry in new_manifest.entries}
        manifest_differences = {
            label: sorted(
                item.name
                for item in dataclasses.fields(old_entries[UUID(subject_ids[label])])
                if getattr(old_entries[UUID(subject_ids[label])], item.name)
                != getattr(new_entries[UUID(subject_ids[label])], item.name)
            )
            for label in ("B", "C")
            if _without_position(old_entries[UUID(subject_ids[label])])
            != _without_position(new_entries[UUID(subject_ids[label])])
        }
        _require(
            set(old_entries) == set(new_entries)
            and new_entries[UUID(subject_ids["A"])].document_artifact_id
            == UUID(augmented_publication_id)
            and all(
                # The editor's repin re-submits the ordered composition; only the display
                # position may be renumbered, never the pinned run/artifact/hash identity.
                _without_position(old_entries[UUID(subject_ids[label])])
                == _without_position(new_entries[UUID(subject_ids[label])])
                for label in ("B", "C")
            )
            and old_manifest.exclusions == new_manifest.exclusions,
            "manifest entries",
            "the new manifest must change A only and preserve B, C and exclusions "
            f"(differences: {manifest_differences})",
        )
        _require(
            associations
            and any(
                item.sample_sha256 == expected_candidate_hash
                and item.qualification is SampleQualification.CONFIRMED
                for item in associations
            ),
            "G3 persisted state",
            "the candidate sample lacks the human confirmed qualification in PostgreSQL",
        )

        final_document_bytes = await artifact_store.read_bytes(
            final_release.edition_document_blob_id
        )
        _require(
            hashlib.sha256(final_document_bytes).hexdigest()
            == final_release.edition_document_sha256,
            "final edition document",
            "the assembled document SHA-256 does not match its release record",
        )
        edition_document = EditionDocumentV2.from_json(json.loads(final_document_bytes))
        opinion_subjects = {
            item.subject_id
            for item in edition_document.publications
            if isinstance(item.document, PublicationDocumentV6)
            and item.document.rendered_opinion is not None
        }
        _require(
            opinion_subjects == {UUID(subject_ids["A"])},
            "final edition document",
            f"expected an opinion on A only, observed {sorted(map(str, opinion_subjects))}",
        )

        final_pdf_bytes = await artifact_store.read_bytes(final_render.output_blob_id)
        persisted_pdf_sha256 = hashlib.sha256(final_pdf_bytes).hexdigest()
        _require(
            final_render.output_sha256 == persisted_pdf_sha256 == downloaded_pdf_sha256,
            "downloaded PDF",
            "Playwright's release download does not match the successful PostgreSQL render",
        )
        _require(downloaded_pdf.startswith(b"%PDF-"), "downloaded PDF", "download is not a PDF")

        with tempfile.TemporaryDirectory(prefix="workflow-browser-j2-poppler-") as directory:
            downloaded_copy = Path(directory) / "downloaded-release.pdf"
            downloaded_copy.write_bytes(downloaded_pdf)
            pdf_text = await asyncio.to_thread(_extract_pdf_text, downloaded_copy)
        # Typst renders a typographic apostrophe (U+2019) in small caps.
        folded_text = pdf_text.casefold().replace("\u2019", "'")
        opinion_heading = "avis de l'analyste"
        _require(
            folded_text.count(opinion_heading) == 1,
            "PDF text",
            "expected exactly one extracted analyst-opinion heading, observed "
            f"{folded_text.count(opinion_heading)}",
        )
        heading_position = folded_text.index(opinion_heading)
        article_a_position = folded_text.rfind("article a", 0, heading_position)
        next_article_b_position = folded_text.find("article b", heading_position)
        _require(
            article_a_position >= 0
            and (next_article_b_position < 0 or heading_position < next_article_b_position),
            "PDF article ownership",
            "the extracted analyst opinion is not located in Article A's section",
        )
        _require(
            text_value(browser_result.get("review_marker"), "review_marker").casefold()
            in folded_text,
            "PDF text",
            "the human revision marker is absent from the release PDF",
        )

        print(
            "J2-browser-investigation / PostgreSQL, historical hashes and "
            "Poppler verification passed"
        )
        print(
            f"  composition=v{before_repin_version} unchanged before repin → v{after_repin_version}"
        )
        print(
            "  historical manifest="
            f"{history_manifest.id} sha256={hashlib.sha256(history_manifest_bytes).hexdigest()}"
        )
        historical_pdf_sha256 = hashlib.sha256(history_pdf_bytes).hexdigest()
        print(f"  historical release={history_release.id} PDF sha256={historical_pdf_sha256}")
        final_manifest_sha256 = hashlib.sha256(final_manifest_bytes).hexdigest()
        print(f"  final manifest={final_manifest.id} sha256={final_manifest_sha256}")
        print(
            f"  downloaded/final PDF sha256={downloaded_pdf_sha256}; "
            "analyst-opinion heading count=1"
        )
    finally:
        await engine.dispose()


async def _verify() -> None:
    state_value = os.environ.get("WORKFLOW_BROWSER_STATE")
    admin_dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    _require(bool(state_value), "state", "WORKFLOW_BROWSER_STATE is required")
    _require(bool(admin_dsn), "state", "TEST_POSTGRES_ADMIN_DSN is required")
    assert state_value is not None
    assert admin_dsn is not None

    state_path = Path(state_value)
    try:
        raw_state: object = json.loads(
            await asyncio.to_thread(state_path.read_text, encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(
            f"W10-browser-exclusion / state: cannot read {state_path}: {exc}"
        ) from exc
    _require(_is_string_mapping(raw_state), "state", "state JSON must be an object")
    state = raw_state

    if state.get("scenario") == "investigation-opinion-repin":
        await _verify_investigation(state, admin_dsn)
        return

    edition_id = _required_text(state.get("edition_id"), "edition_id")
    database_name = _required_text(state.get("database_name"), "database_name")
    expected_reason = _required_text(state.get("exclusion_reason"), "exclusion_reason")
    subjects_value = state.get("subjects")
    _require(_is_string_mapping(subjects_value), "state", "subjects must be an object")
    subjects = subjects_value
    subject_ids = {
        label: _required_text(subjects.get(label), f"subjects.{label}") for label in ("A", "B", "C")
    }
    result_value = state.get("browser_result")
    _require(
        _is_string_mapping(result_value),
        "browser response",
        "Playwright did not record the observed exclusion response",
    )
    browser_result = result_value
    status = browser_result.get("exclusion_status")
    initial_version = browser_result.get("initial_composition_version")
    composition_version = browser_result.get("composition_version")
    decision_id = _required_text(browser_result.get("decision_id"), "browser_result.decision_id")
    _require(status == 200, "browser response", f"expected HTTP 200, observed {status!r}")
    _require(
        isinstance(initial_version, int) and isinstance(composition_version, int),
        "browser response",
        "composition versions must be integers",
    )
    _require(
        composition_version == initial_version + 1,
        "browser response",
        f"expected composition v{initial_version}+1, observed v{composition_version}",
    )

    database_dsn = (
        make_url(admin_dsn).set(database=database_name).render_as_string(hide_password=False)
    )
    engine = create_async_engine(database_dsn)
    try:
        async with engine.connect() as connection:
            manifest_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, schema_version, composition_version "
                            "FROM publication_manifests WHERE edition_id = :edition_id"
                        ),
                        {"edition_id": edition_id},
                    )
                )
                .mappings()
                .all()
            )
            _require(
                len(manifest_rows) == 1,
                "manifest",
                "expected one persisted manifest for edition "
                f"{edition_id}, found {len(manifest_rows)}",
            )
            manifest = manifest_rows[0]
            manifest_id = str(manifest["id"])
            _require(
                manifest["schema_version"] == "2",
                "manifest",
                f"expected schema v2, observed {manifest['schema_version']!r}",
            )
            _require(
                manifest["composition_version"] == composition_version,
                "manifest",
                "manifest composition version differs from the observed exclusion response",
            )

            entry_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT subject_id FROM publication_manifest_entries "
                            "WHERE manifest_id = :manifest_id"
                        ),
                        {"manifest_id": manifest_id},
                    )
                )
                .mappings()
                .all()
            )
            entry_subject_ids = {str(row["subject_id"]) for row in entry_rows}
            _require(
                entry_subject_ids == {subject_ids["A"], subject_ids["C"]},
                "manifest entries",
                f"expected A and C only, observed subject ids {sorted(entry_subject_ids)}",
            )

            exclusion_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT subject_id, review_decision_id "
                            "FROM publication_manifest_exclusions WHERE manifest_id = :manifest_id"
                        ),
                        {"manifest_id": manifest_id},
                    )
                )
                .mappings()
                .all()
            )
            _require(
                len(exclusion_rows) == 1,
                "manifest exclusions",
                f"expected one persisted exclusion, found {len(exclusion_rows)}",
            )
            exclusion = exclusion_rows[0]
            _require(
                str(exclusion["subject_id"]) == subject_ids["B"],
                "manifest exclusions",
                f"expected subject B {subject_ids['B']}, observed {exclusion['subject_id']}",
            )
            _require(
                str(exclusion["review_decision_id"]) == decision_id,
                "manifest exclusions",
                "manifest exclusion decision id differs from the HTTP response decision id",
            )

            decision_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, edition_id, subject_id, decision, actor_id, reason "
                            "FROM publication_review_decisions WHERE id = :decision_id"
                        ),
                        {"decision_id": decision_id},
                    )
                )
                .mappings()
                .all()
            )
            _require(
                len(decision_rows) == 1,
                "review decision",
                f"decision {decision_id} is absent from publication_review_decisions",
            )
            decision = decision_rows[0]
            _require(
                str(decision["edition_id"]) == edition_id
                and str(decision["subject_id"]) == subject_ids["B"]
                and decision["decision"] == "exclude",
                "review decision",
                "decision row does not describe exclusion of subject B in this edition",
            )
            _require(
                decision["actor_id"] == EXPECTED_ACTOR,
                "review decision",
                f"expected actor {EXPECTED_ACTOR!r}, observed {decision['actor_id']!r}",
            )
            _require(
                decision["reason"] == expected_reason,
                "review decision",
                f"expected exact reason {expected_reason!r}, observed {decision['reason']!r}",
            )
    finally:
        await engine.dispose()

    print("W10-browser-exclusion / SQL verification passed")
    print(f"  manifest={manifest_id} schema=v2 composition=v{composition_version}")
    print(f"  entries=A:{subject_ids['A']}, C:{subject_ids['C']}")
    print(f"  exclusion=B:{subject_ids['B']} decision={decision_id}")
    print(f"  actor={EXPECTED_ACTOR} reason={expected_reason}")


def main() -> int:
    try:
        asyncio.run(_verify())
    except Exception as exc:
        print(f"W10-browser-exclusion / SQL verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _without_position(entry: Any) -> Any:
    return dataclasses.replace(entry, position=1)


if __name__ == "__main__":
    raise SystemExit(main())
