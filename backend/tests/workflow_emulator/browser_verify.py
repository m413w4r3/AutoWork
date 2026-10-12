"""Verify W10 browser publication state directly in the isolated PostgreSQL database."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TypeGuard

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

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


if __name__ == "__main__":
    raise SystemExit(main())
