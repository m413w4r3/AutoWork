"""Add the note publication anchor identity (M24.1 F4-D B4/B5a).

Revision ID: 0029_note_anchor_identity
Revises: 0028_task_context_consultations

A note proposal now pins the exact article publication it was drafted
against (artifact identity, version, SHA-256) and the SHA-256 of the anchored
paragraph text, so acceptance can refuse a moved base (``stale_base``) or a
changed paragraph (``note_anchor_text_changed``) instead of silently moving
the note.  Older proposals carry NULL in every new identity column and stay
readable and acceptable.

The same revision adds ``content_blocks`` (an empty JSON array by default) for
the next B5a lot; no content-block behaviour is implemented here.

The user policy restarts from an empty database, so this revision is additive
and deliberately simple: no elaborate preflight.  The downgrade only refuses
while a row actually carries a new identity value.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029_note_anchor_identity"
down_revision = "0028_task_context_consultations"
branch_labels = None
depends_on = None

_TABLE = "investigation_note_proposals"
_ARTIFACTS = "production_artifacts"

_BASE_FK = "fk_investigation_note_proposals_base_publication"
_BASE_PAIR_CHECK = "ck_investigation_note_proposals_base_publication_pair"
_BASE_VERSION_CHECK = "ck_investigation_note_proposals_base_publication_version"
_BASE_SHA_CHECK = "ck_investigation_note_proposals_base_publication_sha256"
_ANCHOR_SHA_CHECK = "ck_investigation_note_proposals_anchor_before_sha256"
_CONTENT_BLOCKS_CHECK = "ck_investigation_note_proposals_content_blocks_shape"
_BASE_INDEX = "ix_investigation_note_proposals_base_publication"

_DROPPED_COLUMNS = (
    "content_blocks",
    "anchor_before_sha256",
    "base_publication_sha256",
    "base_publication_version",
    "base_publication_artifact_id",
)


def _sha256_sql(column: str) -> str:
    return f"char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("base_publication_artifact_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("base_publication_version", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("base_publication_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("anchor_before_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "content_blocks",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_foreign_key(
        _BASE_FK,
        _TABLE,
        _ARTIFACTS,
        ["base_publication_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        _BASE_PAIR_CHECK,
        _TABLE,
        "(base_publication_artifact_id IS NULL) = (base_publication_version IS NULL) AND "
        "(base_publication_artifact_id IS NULL) = (base_publication_sha256 IS NULL) AND "
        "(base_publication_artifact_id IS NULL) = (anchor_before_sha256 IS NULL)",
    )
    op.create_check_constraint(
        _BASE_VERSION_CHECK,
        _TABLE,
        "base_publication_version IS NULL OR base_publication_version >= 1",
    )
    op.create_check_constraint(
        _BASE_SHA_CHECK,
        _TABLE,
        f"base_publication_sha256 IS NULL OR ({_sha256_sql('base_publication_sha256')})",
    )
    op.create_check_constraint(
        _ANCHOR_SHA_CHECK,
        _TABLE,
        f"anchor_before_sha256 IS NULL OR ({_sha256_sql('anchor_before_sha256')})",
    )
    op.create_check_constraint(
        _CONTENT_BLOCKS_CHECK,
        _TABLE,
        "jsonb_typeof(content_blocks) = 'array'",
    )
    op.create_index(_BASE_INDEX, _TABLE, ["base_publication_artifact_id"])


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    used = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {_TABLE} "
            "WHERE base_publication_artifact_id IS NOT NULL "
            "OR anchor_before_sha256 IS NOT NULL "
            "OR content_blocks <> '[]'::jsonb"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(f"refusing to downgrade {_TABLE} while a row carries a note identity")
    op.drop_index(_BASE_INDEX, table_name=_TABLE)
    op.drop_constraint(_CONTENT_BLOCKS_CHECK, _TABLE, type_="check")
    op.drop_constraint(_ANCHOR_SHA_CHECK, _TABLE, type_="check")
    op.drop_constraint(_BASE_SHA_CHECK, _TABLE, type_="check")
    op.drop_constraint(_BASE_VERSION_CHECK, _TABLE, type_="check")
    op.drop_constraint(_BASE_PAIR_CHECK, _TABLE, type_="check")
    op.drop_constraint(_BASE_FK, _TABLE, type_="foreignkey")
    for column in _DROPPED_COLUMNS:
        op.drop_column(_TABLE, column)
