"""Align sample hash validation with MD5, SHA-1 and SHA-256."""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_investigation_integration_fix"
down_revision: str | None = "0005_analyst_input_packs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HASH_CONSTRAINT = (
    "expected_hash IS NULL OR (char_length(expected_hash) IN (32, 40, 64) "
    "AND expected_hash ~ '^[0-9a-f]+$')"
)
_SHA256_CONSTRAINT = (
    "expected_hash IS NULL OR (char_length(expected_hash) = 64 "
    "AND expected_hash ~ '^[0-9a-f]{64}$')"
)


def upgrade() -> None:
    op.drop_constraint("ck_samples_expected_hash", "samples", type_="check")
    op.create_check_constraint(
        "ck_samples_expected_hash",
        "samples",
        _HASH_CONSTRAINT,
    )


def downgrade() -> None:
    op.drop_constraint("ck_samples_expected_hash", "samples", type_="check")
    op.create_check_constraint(
        "ck_samples_expected_hash",
        "samples",
        _SHA256_CONSTRAINT,
    )
