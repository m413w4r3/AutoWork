"""Tests for conversation lifecycle management in the bridge.

Tests the RunRegistry methods and API endpoints for conversation lifecycle.
"""

import sqlite3
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest


def test_bridge_conversations_table_created(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_conversations (
            id TEXT PRIMARY KEY,
            external_locator TEXT,
            policy TEXT NOT NULL CHECK(policy IN ('keep', 'delete_on_success')),
            status TEXT NOT NULL CHECK(status IN (
                'active', 'released', 'delete_pending', 'deleting',
                'deleted', 'cleanup_failed', 'retained'
            )),
            release_outcome TEXT CHECK(release_outcome IN (
                'success', 'failure', 'needs_review', 'cancelled', NULL
            )),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            deleted_at REAL,
            cleanup_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_cleanup_attempt_at REAL,
            last_cleanup_error_code TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bridge_conversations'"
    ).fetchone()
    assert tables is not None
    db.close()


def test_create_conversation_stores_in_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE bridge_conversations (
            id TEXT PRIMARY KEY,
            external_locator TEXT,
            policy TEXT NOT NULL,
            status TEXT NOT NULL,
            release_outcome TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            deleted_at REAL,
            cleanup_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_cleanup_attempt_at REAL,
            last_cleanup_error_code TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conversation_id = str(uuid4())
    external_locator = "https://chatgpt.com/c/abc123"
    policy = "delete_on_success"
    now = time.time()

    db.execute(
        """
        INSERT INTO bridge_conversations
        (id, external_locator, policy, status, created_at, updated_at, version)
        VALUES (?, ?, ?, 'active', ?, ?, 1)
        """,
        (conversation_id, external_locator, policy, now, now),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    assert row is not None
    assert row["id"] == conversation_id
    assert row["policy"] == "delete_on_success"
    assert row["status"] == "active"
    assert row["release_outcome"] is None
    db.close()


def test_release_conversation_success_with_delete_on_success(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE bridge_conversations (
            id TEXT PRIMARY KEY,
            external_locator TEXT,
            policy TEXT NOT NULL,
            status TEXT NOT NULL,
            release_outcome TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            deleted_at REAL,
            cleanup_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_cleanup_attempt_at REAL,
            last_cleanup_error_code TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conversation_id = str(uuid4())
    now = time.time()

    db.execute(
        """
        INSERT INTO bridge_conversations
        (id, external_locator, policy, status, created_at, updated_at, version)
        VALUES (?, ?, ?, 'active', ?, ?, 1)
        """,
        (conversation_id, "https://chatgpt.com/c/abc", "delete_on_success", now, now),
    )

    db.execute(
        """
        UPDATE bridge_conversations
        SET status=?, release_outcome=?, released_at=?, updated_at=?, version=version+1
        WHERE id=?
        """,
        ("delete_pending", "success", now, now, conversation_id),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    assert row["status"] == "delete_pending"
    assert row["release_outcome"] == "success"
    assert row["released_at"] == now
    assert row["version"] == 2
    db.close()


def test_release_conversation_success_with_keep_policy(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE bridge_conversations (
            id TEXT PRIMARY KEY,
            external_locator TEXT,
            policy TEXT NOT NULL,
            status TEXT NOT NULL,
            release_outcome TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            deleted_at REAL,
            cleanup_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_cleanup_attempt_at REAL,
            last_cleanup_error_code TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conversation_id = str(uuid4())
    now = time.time()

    db.execute(
        """
        INSERT INTO bridge_conversations
        (id, external_locator, policy, status, created_at, updated_at, version)
        VALUES (?, ?, ?, 'active', ?, ?, 1)
        """,
        (conversation_id, "https://chatgpt.com/c/abc", "keep", now, now),
    )

    db.execute(
        """
        UPDATE bridge_conversations
        SET status=?, release_outcome=?, released_at=?, updated_at=?, version=version+1
        WHERE id=?
        """,
        ("retained", "success", now, now, conversation_id),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    assert row["status"] == "retained"
    assert row["release_outcome"] == "success"
    assert row["policy"] == "keep"
    db.close()


def test_release_conversation_failure_preserves(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE bridge_conversations (
            id TEXT PRIMARY KEY,
            external_locator TEXT,
            policy TEXT NOT NULL,
            status TEXT NOT NULL,
            release_outcome TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            deleted_at REAL,
            cleanup_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_cleanup_attempt_at REAL,
            last_cleanup_error_code TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conversation_id = str(uuid4())
    now = time.time()

    db.execute(
        """
        INSERT INTO bridge_conversations
        (id, external_locator, policy, status, created_at, updated_at, version)
        VALUES (?, ?, ?, 'active', ?, ?, 1)
        """,
        (conversation_id, None, "delete_on_success", now, now),
    )

    db.execute(
        """
        UPDATE bridge_conversations
        SET status=?, release_outcome=?, released_at=?, updated_at=?, version=version+1
        WHERE id=?
        """,
        ("retained", "failure", now, now, conversation_id),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    # DELETE_ON_SUCCESS policy does not apply on FAILURE: conversation is preserved.
    assert row["status"] == "retained"
    assert row["release_outcome"] == "failure"
    db.close()


def test_cleanup_failure_increments_attempt_count(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE bridge_conversations (
            id TEXT PRIMARY KEY,
            external_locator TEXT,
            policy TEXT NOT NULL,
            status TEXT NOT NULL,
            release_outcome TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            deleted_at REAL,
            cleanup_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_cleanup_attempt_at REAL,
            last_cleanup_error_code TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conversation_id = str(uuid4())
    now = time.time()

    db.execute(
        """
        INSERT INTO bridge_conversations
        (id, external_locator, policy, status, created_at, updated_at, version)
        VALUES (?, ?, ?, 'delete_pending', ?, ?, 1)
        """,
        (conversation_id, "https://chatgpt.com/c/abc", "delete_on_success", now, now),
    )

    db.execute(
        """
        UPDATE bridge_conversations
        SET status=?, cleanup_attempt_count=cleanup_attempt_count+1,
            last_cleanup_attempt_at=?, last_cleanup_error_code=?, updated_at=?, version=version+1
        WHERE id=?
        """,
        ("cleanup_failed", now, "network_timeout", now, conversation_id),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    assert row["status"] == "cleanup_failed"
    assert row["cleanup_attempt_count"] == 1
    assert row["last_cleanup_error_code"] == "network_timeout"
    db.close()


def test_mark_deleted_stores_timestamp(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE bridge_conversations (
            id TEXT PRIMARY KEY,
            external_locator TEXT,
            policy TEXT NOT NULL,
            status TEXT NOT NULL,
            release_outcome TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            deleted_at REAL,
            cleanup_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_cleanup_attempt_at REAL,
            last_cleanup_error_code TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conversation_id = str(uuid4())
    created_at = time.time()
    deleted_at = created_at + 100.0

    db.execute(
        """
        INSERT INTO bridge_conversations
        (id, external_locator, policy, status, created_at, updated_at, version)
        VALUES (?, ?, ?, 'delete_pending', ?, ?, 1)
        """,
        (conversation_id, "https://chatgpt.com/c/abc", "delete_on_success", created_at, created_at),
    )

    db.execute(
        """
        UPDATE bridge_conversations
        SET status=?, deleted_at=?, cleanup_attempt_count=cleanup_attempt_count+1,
            last_cleanup_attempt_at=?, updated_at=?, version=version+1
        WHERE id=?
        """,
        ("deleted", deleted_at, deleted_at, deleted_at, conversation_id),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    assert row["status"] == "deleted"
    assert row["deleted_at"] == deleted_at
    assert row["cleanup_attempt_count"] == 1
    db.close()
