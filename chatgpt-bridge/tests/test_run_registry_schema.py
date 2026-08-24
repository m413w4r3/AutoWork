"""
Tests for RunRegistry's fresh-schema-only initialization (R51).

RunRegistry no longer carries historical SQLite migration shims: a new,
empty database path must produce the final canonical schema directly, with
no ALTER TABLE / rename / backfill path involved.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import RunRegistry


@pytest.fixture
def temp_db():
    """Path to a database file that does not exist yet."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "bridge-runs.sqlite3"


class TestFreshSchema:
    """A RunRegistry created on an empty path gets the final schema immediately."""

    def test_bridge_runs_columns_exist_immediately(self, temp_db):
        RunRegistry(temp_db)

        with sqlite3.connect(temp_db) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(bridge_runs)")}

        assert columns == {
            "idempotency_key",
            "request_hash",
            "bridge_run_id",
            "state",
            "created_at",
            "updated_at",
            "response_json",
            "error_json",
            "conversation_json",
            "preview_json",
        }

    def test_bridge_runs_state_check_includes_needs_review(self, temp_db):
        RunRegistry(temp_db)

        with sqlite3.connect(temp_db) as db:
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='bridge_runs'"
            ).fetchone()[0]

        assert "needs_review" in table_sql

    def test_bridge_conversations_table_exists(self, temp_db):
        RunRegistry(temp_db)

        with sqlite3.connect(temp_db) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(bridge_conversations)")}

        assert columns == {
            "id",
            "external_locator",
            "policy",
            "status",
            "release_outcome",
            "created_at",
            "updated_at",
            "released_at",
            "deleted_at",
            "cleanup_attempt_count",
            "last_cleanup_attempt_at",
            "last_cleanup_error_code",
            "version",
        }

    def test_no_legacy_table_created(self, temp_db):
        RunRegistry(temp_db)

        with sqlite3.connect(temp_db) as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

        assert "bridge_runs_legacy" not in names

    def test_reopening_fresh_schema_does_not_raise(self, temp_db):
        """Re-opening a DB already on the final schema is a no-op, not a migration."""
        RunRegistry(temp_db)
        RunRegistry(temp_db)  # must not raise

    def test_incompatible_existing_schema_fails_clearly(self, temp_db):
        """An old, incompatible bridge_runs table must fail loudly, not migrate."""
        temp_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(temp_db) as db:
            db.execute(
                """
                CREATE TABLE bridge_runs (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    bridge_run_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    response_json TEXT,
                    error_json TEXT
                )
                """
            )

        with pytest.raises(RuntimeError, match="bridge-runs.sqlite3"):
            RunRegistry(temp_db)
