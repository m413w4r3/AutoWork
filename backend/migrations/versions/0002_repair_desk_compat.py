"""Make the Repair Desk table available to databases stamped at 0001.

The current ``0001_baseline`` already creates this table on a fresh database.
Older databases can nevertheless be stamped at ``0001_baseline`` without it,
because the table was added to the baseline after those databases had been
created.  This revision only fills that compatibility gap.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from cti_app.infrastructure.database.models import (  # noqa: F401
    collection,
    core,
    discovery,
    edition_publication,
    editions,
    editorial,
    invariants,
    jobs,
    model_execution,
    production,
    publication_review,
)
from cti_app.infrastructure.database.models.base import Base

revision: str = "0002_repair_desk_compat"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPAIR_TABLE = "production_repair_decisions"
_REPAIR_TRIGGER = "trg_production_repair_decisions_append_only"


def _trigger_exists(bind: Connection) -> bool:
    result = bind.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_trigger AS trg
                JOIN pg_class AS tbl ON tbl.oid = trg.tgrelid
                JOIN pg_namespace AS nsp ON nsp.oid = tbl.relnamespace
                WHERE nsp.nspname = 'public'
                  AND tbl.relname = :table_name
                  AND trg.tgname = :trigger_name
                  AND NOT trg.tgisinternal
            )
            """
        ),
        {"table_name": _REPAIR_TABLE, "trigger_name": _REPAIR_TRIGGER},
    )
    return bool(result.scalar_one())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    repair_table = Base.metadata.tables[_REPAIR_TABLE]

    # The current baseline owns this table on a fresh install.  Only create
    # it when inspection proves that an older stamped database is missing it.
    if not inspector.has_table(_REPAIR_TABLE):
        repair_table.create(bind=bind, checkfirst=False)

    # PostgreSQL has no CREATE TRIGGER IF NOT EXISTS.  Never replace an
    # existing trigger: its definition is part of the existing audit policy.
    if not _trigger_exists(bind):
        op.execute(
            f"CREATE TRIGGER {_REPAIR_TRIGGER} "
            f"BEFORE UPDATE OR DELETE ON {_REPAIR_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
        )


def downgrade() -> None:
    # This compatibility revision must not destroy a table or audit rows that
    # belonged to the baseline on a fresh database.  The baseline downgrade
    # remains responsible for removing its complete fresh schema.
    pass
