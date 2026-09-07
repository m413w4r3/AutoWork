"""Add immutable value corrections to the Repair Desk.

The baseline already contains the latest schema on a fresh database.  This
revision is deliberately additive for databases stamped at an earlier
baseline: it creates the correction table, adds the nullable decision
reference, and then installs the database-level REPLACE invariant.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, ForeignKey, Uuid, inspect, text
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

revision: str = "0004_repair_corrections"
down_revision: str | None = "0003_manual_source_archival"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CORRECTIONS = "production_repair_corrections"
_DECISIONS = "production_repair_decisions"
_CORRECTION_TRIGGER = "trg_production_repair_corrections_append_only"
_REPLACE_CHECK = "ck_production_repair_replace_correction"
_ACTION_CHECK = "ck_production_repair_action"
_COMPATIBILITY_CHECK = "ck_production_repair_action_compatibility"


def _has_column(bind: Connection, table: str, column: str) -> bool:
    return any(item["name"] == column for item in inspect(bind).get_columns(table))


def _has_check(bind: Connection, table: str, name: str) -> bool:
    return any(item.get("name") == name for item in inspect(bind).get_check_constraints(table))


def _trigger_exists(bind: Connection, table: str, trigger: str) -> bool:
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
        {"table_name": table, "trigger_name": trigger},
    )
    return bool(result.scalar_one())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table(_CORRECTIONS):
        Base.metadata.tables[_CORRECTIONS].create(bind=bind, checkfirst=False)

    if not _has_column(bind, _DECISIONS, "correction_id"):
        op.add_column(
            _DECISIONS,
            Column(
                "correction_id",
                Uuid(as_uuid=True),
                ForeignKey("production_repair_corrections.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )

    # Existing databases still have the pre-LOT-36 action checks.  Replace
    # both constraints explicitly so REPLACE is accepted only for Q2
    # indicator/rule issues.
    if _has_check(bind, _DECISIONS, _ACTION_CHECK):
        op.drop_constraint(_ACTION_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _ACTION_CHECK,
        _DECISIONS,
        "action IN ('include', 'exclude', 'replace', 'continue_without_source')",
    )
    if _has_check(bind, _DECISIONS, _COMPATIBILITY_CHECK):
        op.drop_constraint(_COMPATIBILITY_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _COMPATIBILITY_CHECK,
        _DECISIONS,
        "((issue_kind IN ('rejected_indicator', 'rejected_rule') "
        "AND action IN ('include', 'exclude', 'replace')) OR "
        "(issue_kind = 'supplemental_source_unarchived' "
        "AND action = 'continue_without_source'))",
    )

    if not _has_check(bind, _DECISIONS, _REPLACE_CHECK):
        op.create_check_constraint(
            _REPLACE_CHECK,
            _DECISIONS,
            "(action = 'replace') = (correction_id IS NOT NULL)",
        )

    if not _trigger_exists(bind, _CORRECTIONS, _CORRECTION_TRIGGER):
        op.execute(
            f"CREATE TRIGGER {_CORRECTION_TRIGGER} "
            f"BEFORE UPDATE OR DELETE ON {_CORRECTIONS} "
            "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    correction_count = bind.execute(text(f"SELECT count(*) FROM {_CORRECTIONS}")).scalar_one()
    replace_count = bind.execute(
        text(f"SELECT count(*) FROM {_DECISIONS} WHERE action = 'replace'")
    ).scalar_one()
    if correction_count or replace_count:
        raise RuntimeError("cannot downgrade while repair corrections are part of the audit")

    if _trigger_exists(bind, _CORRECTIONS, _CORRECTION_TRIGGER):
        op.execute(f"DROP TRIGGER {_CORRECTION_TRIGGER} ON {_CORRECTIONS}")
    if _has_check(bind, _DECISIONS, _REPLACE_CHECK):
        op.drop_constraint(_REPLACE_CHECK, _DECISIONS, type_="check")
    if _has_check(bind, _DECISIONS, _ACTION_CHECK):
        op.drop_constraint(_ACTION_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _ACTION_CHECK,
        _DECISIONS,
        "action IN ('include', 'exclude', 'continue_without_source')",
    )
    if _has_check(bind, _DECISIONS, _COMPATIBILITY_CHECK):
        op.drop_constraint(_COMPATIBILITY_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _COMPATIBILITY_CHECK,
        _DECISIONS,
        "((issue_kind IN ('rejected_indicator', 'rejected_rule') "
        "AND action IN ('include', 'exclude')) OR "
        "(issue_kind = 'supplemental_source_unarchived' "
        "AND action = 'continue_without_source'))",
    )
    if _has_column(bind, _DECISIONS, "correction_id"):
        op.drop_column(_DECISIONS, "correction_id")
    if inspect(bind).has_table(_CORRECTIONS):
        op.drop_table(_CORRECTIONS)
