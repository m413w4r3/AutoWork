"""Allow an explicitly scoped destructive edition purge.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _replace_guard(
        "reject_provenance_mutation",
        "provenance_events is append-only",
    )
    _replace_guard("reject_audit_mutation", "audit journals are append-only")
    _replace_guard(
        "reject_human_decision_mutation",
        "human_decisions is append-only",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND
               current_setting('cti.allow_destructive_edition_delete', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    _restore_guard(
        "reject_provenance_mutation",
        "provenance_events is append-only",
    )
    _restore_guard("reject_audit_mutation", "audit journals are append-only")
    _restore_guard(
        "reject_human_decision_mutation",
        "human_decisions is append-only",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _replace_guard(function_name: str, message: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND
               current_setting('cti.allow_destructive_edition_delete', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '{message}' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _restore_guard(function_name: str, message: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '{message}' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
