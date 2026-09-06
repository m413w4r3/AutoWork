"""Give the analyst upload path its own acquisition token.

``source_collections.fetch_job_id`` and ``collection_attempts.job_id`` are
foreign keys into ``jobs``.  An analyst upload is not a collector job, so it
now carries ``manual_lease_id`` instead — a lease token, never a foreign key —
and ``collection_attempts.job_id`` becomes nullable for that path only.

The current baseline already creates these columns on a fresh database, so
every step here is applied only when inspection proves it is missing.  No
existing row is rewritten: the collector path keeps its job id, and every
attempt already stored keeps the one it was recorded with.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision: str = "0003_manual_source_archival"
down_revision: str | None = "0002_repair_desk_compat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLLECTIONS = "source_collections"
_ATTEMPTS = "collection_attempts"
_COLLECTIONS_LEASE_CHECK = "ck_source_collections_single_lease"
_ATTEMPTS_ACQUISITION_CHECK = "ck_collection_attempts_acquisition"


def _has_column(bind: Connection, table: str, column: str) -> bool:
    return any(item["name"] == column for item in inspect(bind).get_columns(table))


def _has_check(bind: Connection, table: str, name: str) -> bool:
    return any(item["name"] == name for item in inspect(bind).get_check_constraints(table))


def _job_id_is_nullable(bind: Connection, table: str) -> bool:
    return any(
        item["name"] == "job_id" and item["nullable"] for item in inspect(bind).get_columns(table)
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, _COLLECTIONS, "manual_lease_id"):
        op.execute(f"ALTER TABLE {_COLLECTIONS} ADD COLUMN manual_lease_id UUID")
    if not _has_column(bind, _ATTEMPTS, "manual_lease_id"):
        op.execute(f"ALTER TABLE {_ATTEMPTS} ADD COLUMN manual_lease_id UUID")

    if not _job_id_is_nullable(bind, _ATTEMPTS):
        op.execute(f"ALTER TABLE {_ATTEMPTS} ALTER COLUMN job_id DROP NOT NULL")

    # The foreign key stays: a non-NULL job_id must still name a real job.
    if not _has_check(bind, _COLLECTIONS, _COLLECTIONS_LEASE_CHECK):
        op.execute(
            f"ALTER TABLE {_COLLECTIONS} ADD CONSTRAINT {_COLLECTIONS_LEASE_CHECK} "
            "CHECK (fetch_job_id IS NULL OR manual_lease_id IS NULL)"
        )
    if not _has_check(bind, _ATTEMPTS, _ATTEMPTS_ACQUISITION_CHECK):
        op.execute(
            f"ALTER TABLE {_ATTEMPTS} ADD CONSTRAINT {_ATTEMPTS_ACQUISITION_CHECK} "
            "CHECK ((job_id IS NULL) <> (manual_lease_id IS NULL))"
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Restoring NOT NULL would require inventing a job id for every analyst
    # upload or deleting its attempt. Both destroy audit rows, so the
    # downgrade refuses instead.
    manual_attempts = bind.execute(
        text(f"SELECT count(*) FROM {_ATTEMPTS} WHERE job_id IS NULL")
    ).scalar_one()
    if manual_attempts:
        raise RuntimeError(
            f"{manual_attempts} manual collection attempts have no job id; "
            "downgrading would destroy their audit"
        )

    if _has_check(bind, _ATTEMPTS, _ATTEMPTS_ACQUISITION_CHECK):
        op.execute(f"ALTER TABLE {_ATTEMPTS} DROP CONSTRAINT {_ATTEMPTS_ACQUISITION_CHECK}")
    if _has_check(bind, _COLLECTIONS, _COLLECTIONS_LEASE_CHECK):
        op.execute(f"ALTER TABLE {_COLLECTIONS} DROP CONSTRAINT {_COLLECTIONS_LEASE_CHECK}")
    if _job_id_is_nullable(bind, _ATTEMPTS):
        op.execute(f"ALTER TABLE {_ATTEMPTS} ALTER COLUMN job_id SET NOT NULL")
    if _has_column(bind, _ATTEMPTS, "manual_lease_id"):
        op.execute(f"ALTER TABLE {_ATTEMPTS} DROP COLUMN manual_lease_id")
    if _has_column(bind, _COLLECTIONS, "manual_lease_id"):
        op.execute(f"ALTER TABLE {_COLLECTIONS} DROP COLUMN manual_lease_id")
