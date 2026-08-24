"""RunRegistry : journal SQLite du bridge (MOVE-ONLY depuis server.py)."""

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from bridge.config import RUN_CLEANUP_LIMIT, RUN_RETENTION_SECONDS

logger = logging.getLogger("chatgpt_bridge")


class RunRegistry:
    """Petit journal SQLite atomique ; aucun prompt/résultat n'est journalisé.

    SQLite est volontairement local au bridge : la contrainte UNIQUE porte la
    garantie de déduplication même lorsque deux handlers HTTP entrent ensemble.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bridge_runs (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    bridge_run_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','needs_review')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    response_json TEXT,
                    error_json TEXT,
                    conversation_json TEXT,
                    preview_json TEXT
                )
                """
            )
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='bridge_runs'"
            ).fetchone()[0]
            columns = {row[1] for row in db.execute("PRAGMA table_info(bridge_runs)")}
            required_columns = {
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
            if "needs_review" not in table_sql or not required_columns.issubset(columns):
                raise RuntimeError(
                    "bridge_runs table schema is incompatible with the current "
                    "code. This bridge no longer migrates historical SQLite "
                    "schemas. Reset the run registry by deleting only "
                    "bridge-runs.sqlite3, bridge-runs.sqlite3-wal and "
                    "bridge-runs.sqlite3-shm inside bridge_data (never delete "
                    "other bridge_data contents, in particular the ChatGPT "
                    "browser/auth state), then restart the bridge."
                )

            # Table for tracking conversation lifecycle and cleanup
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

    def recover_interrupted(self) -> None:
        # Après un arrêt, il est impossible de prouver si le clic UI a eu lieu.
        # Ne jamais resoumettre est la seule reprise sûre. Cette transition se
        # fait au démarrage réel, pas au simple import du module par les tests.
        interrupted = json.dumps(
            {
                "status_code": 503,
                "body": {
                    "error": {
                        "code": "bridge_server_error",
                        "message": "Le bridge a redémarré pendant cette exécution.",
                        "retryable": True,
                    }
                },
            },
            separators=(",", ":"),
        )
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE bridge_runs SET state='failed', error_json=?, updated_at=? "
                "WHERE state IN ('queued','running')",
                (interrupted, time.time()),
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, key: str, request_hash: str) -> tuple[dict[str, Any], bool]:
        now = time.time()
        run_id = f"resp_{uuid.uuid4().hex[:24]}"
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM bridge_runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO bridge_runs "
                    "(idempotency_key,request_hash,bridge_run_id,state,created_at,updated_at,"
                    "response_json,error_json,conversation_json,preview_json) "
                    "VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
                    (key, request_hash, run_id, "queued", now, now),
                )
                row = db.execute(
                    "SELECT * FROM bridge_runs WHERE idempotency_key=?", (key,)
                ).fetchone()
                created = True
            else:
                created = False
            db.execute("COMMIT")
        assert row is not None
        return dict(row), created

    def set_state(self, key: str, state: str, value: dict[str, Any] | None = None) -> None:
        column = "response_json" if state == "completed" else "error_json"
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value else None
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE bridge_runs SET state=?, updated_at=?, {column}=? WHERE idempotency_key=?",
                (state, time.time(), encoded, key),
            )

    def bind_conversation(self, run_id: str, value: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT idempotency_key FROM bridge_runs WHERE bridge_run_id=?", (run_id,)
            ).fetchone()
            bound = {
                **value,
                "bridge_run_id": run_id,
                "model_run_id": row["idempotency_key"] if row else None,
            }
            value.update(bound)
            encoded = json.dumps(bound, ensure_ascii=False, separators=(",", ":"))
            db.execute(
                "UPDATE bridge_runs SET conversation_json=?, updated_at=? "
                "WHERE bridge_run_id=?",
                (encoded, time.time(), run_id),
            )

    def store_preview(self, run_id: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE bridge_runs SET preview_json=?, updated_at=? WHERE bridge_run_id=?",
                (encoded, time.time(), run_id),
            )

    def get_by_run_id(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_runs WHERE bridge_run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def cleanup(self) -> int:
        cutoff = time.time() - RUN_RETENTION_SECONDS
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "DELETE FROM bridge_runs WHERE idempotency_key IN ("
                "SELECT idempotency_key FROM bridge_runs "
                "WHERE state IN ('completed','failed') AND updated_at < ? "
                "ORDER BY updated_at LIMIT ?)",
                (cutoff, RUN_CLEANUP_LIMIT),
            )
        return cursor.rowcount

    def accessible(self) -> bool:
        """Vérifie le registre sans exposer son chemin ni son contenu."""
        try:
            with self._lock, self._connect() as db:
                db.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            logger.exception("bridge_registry_unavailable")
            return False

    def create_conversation(
        self,
        conversation_id: str,
        external_locator: Optional[str],
        policy: str,
    ) -> None:
        """Register a new conversation with its lifecycle policy."""
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO bridge_conversations
                (id, external_locator, policy, status, created_at, updated_at, version)
                VALUES (?, ?, ?, 'active', ?, ?, 1)
                """,
                (conversation_id, external_locator, policy, now, now),
            )

    def release_conversation(self, conversation_id: str, outcome: str) -> dict[str, Any]:
        """Release a conversation with an explicit outcome.

        Returns the updated lifecycle state.
        Only SUCCESS may trigger cleanup based on policy.
        """
        now = time.time()
        with self._lock, self._connect() as db:
            # Get current state
            row = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Conversation {conversation_id} not found")

            current = dict(row)

            # Idempotence: if already released, return current state
            if current["status"] != "active":
                return current

            # Determine next state based on outcome and policy
            if outcome == "success":
                next_status = (
                    "delete_pending" if current["policy"] == "delete_on_success" else "retained"
                )
            else:
                # FAILURE, NEEDS_REVIEW, CANCELLED all preserve conversation
                next_status = "retained"

            # Update state
            db.execute(
                """
                UPDATE bridge_conversations
                SET status=?, release_outcome=?, released_at=?, updated_at=?, version=version+1
                WHERE id=?
                """,
                (next_status, outcome, now, now, conversation_id),
            )

            # Return updated state
            result = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return dict(result)

    def get_conversation_lifecycle(self, conversation_id: str) -> dict[str, Any] | None:
        """Retrieve the current lifecycle state of a conversation."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def start_cleanup(self, conversation_id: str) -> dict[str, Any]:
        """Transition a DELETE_PENDING or CLEANUP_FAILED conversation to DELETING state.

        Allows starting cleanup from DELETE_PENDING (initial attempt) or
        CLEANUP_FAILED (retry). Idempotent on DELETING/DELETED.
        Returns the updated conversation state.
        """
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Conversation not found: {conversation_id}")

            conversation = dict(row)
            current_status = conversation["status"]

            # Transition from DELETE_PENDING or CLEANUP_FAILED
            if current_status in ("delete_pending", "cleanup_failed"):
                db.execute(
                    """
                    UPDATE bridge_conversations
                    SET status=?, updated_at=?, version=version+1
                    WHERE id=?
                    """,
                    ("deleting", now, conversation_id),
                )
            elif current_status not in ("deleting", "deleted"):
                raise ValueError(
                    f"Cannot start cleanup from status {current_status}"
                )

            # Return updated state
            result = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return dict(result)

    def mark_conversation_deleted(self, conversation_id: str) -> dict[str, Any]:
        """Mark a conversation as successfully deleted.

        Transitions from DELETING to DELETED. Idempotent on DELETED.
        """
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Conversation not found: {conversation_id}")

            conversation = dict(row)
            current_status = conversation["status"]

            # Only transition from DELETING; idempotent for DELETED
            if current_status == "deleting":
                db.execute(
                    """
                    UPDATE bridge_conversations
                    SET status=?, deleted_at=?, cleanup_attempt_count=cleanup_attempt_count+1,
                        last_cleanup_attempt_at=?, updated_at=?, version=version+1
                    WHERE id=?
                    """,
                    ("deleted", now, now, now, conversation_id),
                )
            elif current_status == "deleted":
                # Idempotent: already deleted, no-op
                pass
            else:
                # Cannot mark deleted from other states
                raise ValueError(
                    f"Cannot mark deleted from status {current_status}"
                )

            # Return updated state
            result = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return dict(result)

    def mark_cleanup_failed(
        self,
        conversation_id: str,
        error_code: str,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Mark cleanup attempt as failed (retryable).

        Transitions from DELETE_PENDING or DELETING to CLEANUP_FAILED.
        Idempotent: calling again increments attempt count.
        """
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Conversation not found: {conversation_id}")

            conversation = dict(row)
            current_status = conversation["status"]

            # Transition from cleanup states or ignore if already in cleanup_failed
            if current_status in ("delete_pending", "deleting", "cleanup_failed"):
                db.execute(
                    """
                    UPDATE bridge_conversations
                    SET status=?, cleanup_attempt_count=cleanup_attempt_count+1,
                        last_cleanup_attempt_at=?, last_cleanup_error_code=?, updated_at=?, version=version+1
                    WHERE id=?
                    """,
                    ("cleanup_failed", now, error_code[:64], now, conversation_id),
                )
            else:
                raise ValueError(
                    f"Cannot mark cleanup failed from status {current_status}"
                )

            # Return updated state
            result = db.execute(
                "SELECT * FROM bridge_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return dict(result)

    def get_all_delete_pending(self) -> list[str]:
        """Récupère tous les IDs de conversations en DELETE_PENDING."""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT id FROM bridge_conversations WHERE status=?", ("delete_pending",)
            ).fetchall()
            return [row[0] for row in rows]

    def get_all_cleanup_failed(self) -> list[str]:
        """Récupère tous les IDs de conversations en CLEANUP_FAILED."""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT id FROM bridge_conversations WHERE status=?", ("cleanup_failed",)
            ).fetchall()
            return [row[0] for row in rows]

    def checkpoint_and_close(self) -> None:
        """Force le checkpoint WAL ; les connexions sont déjà ouvertes à l'appel."""
        with self._lock, self._connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
