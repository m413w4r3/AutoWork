"""Repair runs left READY without a current publication artifact.

Until the repair paths transitioned the run themselves, staling SYNTHESIS and
PUBLICATION left the run claiming READY with nothing to publish.  The review
read model only ever joins non-STALE artifacts, so those articles surfaced as
"à corriger" with no reason, no error and a retry aimed at ASSEMBLY -- whose
prerequisite the same repair had just invalidated.

This revision restates the invariant on the rows that already broke it: a
terminal run with no VERIFIED publication artifact owes a rebuild, and says so.
The code that produced the state is fixed separately; this only repairs the
data, and it is idempotent -- a second run matches nothing.

Deliberately narrow:

* Only READY runs are touched.  NEEDS_REVIEW and FAILED already carry their own
  diagnosis, which is more specific than the rebuild debt; CANCELLED owns its
  resume use case; QUEUED and RUNNING are expected to have no outputs yet.
* Only runs whose edition is still open are touched.  A frozen or published
  edition is immutable evidence, and rewriting a run underneath a manifest
  would contradict it.
* No artifact is created, deleted or unstaled.  The rebuild itself stays an
  explicit operator gesture.
"""

import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0005_publication_rebuild"
down_revision: str | None = "0004_repair_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ERROR_CODE = "publication_rebuild_required"
_ERROR_MESSAGE = (
    "La publication a été invalidée par une réparation en amont. "
    "Reconstruction requise avant publication."
)

#: READY runs with no current PUBLICATION artifact, in an edition still open to
#: production.  ``status <> 'stale'`` mirrors the read model's own join, so the
#: migration selects exactly the rows the review reports as document-less.
#: Aliased ``broken`` rather than ``r``: this is nested inside an UPDATE whose
#: target is also aliased ``r``, and a shadowed alias in a correlated position
#: is exactly the kind of thing that silently changes meaning on a later edit.
_BROKEN_RUNS = """
    SELECT broken.id
    FROM subject_production_runs AS broken
    JOIN editions AS e ON e.id = broken.edition_id
    WHERE broken.status = 'ready'
      AND e.status IN ('production', 'review')
      AND NOT EXISTS (
          SELECT 1
          FROM production_artifacts AS a
          WHERE a.production_run_id = broken.id
            AND a.stage = 'publication'
            AND a.status = 'verified'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM publication_manifests AS m
          WHERE m.edition_id = broken.edition_id
      )
"""


def upgrade() -> None:
    bind = op.get_bind()
    result = bind.execute(
        text(
            f"""
            UPDATE subject_production_runs AS r
            SET status = 'needs_review',
                error_code = :code,
                error_message = :message,
                -- jsonb_build_object takes "any", so the driver has no column
                -- to infer the parameter's type from: asyncpg refuses it
                -- outright. The cast is what makes the bind resolvable.
                error_details = jsonb_build_object(
                    'migration', CAST(:revision AS text)
                ),
                finished_at = now(),
                updated_at = now(),
                version = r.version + 1
            WHERE r.id IN ({_BROKEN_RUNS})
            """
        ),
        {"code": _ERROR_CODE, "message": _ERROR_MESSAGE, "revision": revision},
    )
    if result.rowcount:
        # The operator has to know how many articles this repaired; a silent
        # data migration is indistinguishable from one that matched nothing.
        logging.getLogger("alembic.runtime.migration").warning(
            "%s: %s run(s) moved READY -> NEEDS_REVIEW (%s)",
            revision,
            result.rowcount,
            _ERROR_CODE,
        )


def downgrade() -> None:
    """Put back exactly the runs this revision moved, and nothing else.

    ``error_details`` carries the revision that wrote the row, so a run that
    reached NEEDS_REVIEW for any other reason -- including one this migration
    already skipped -- is left untouched.
    """
    op.execute(
        text(
            """
            UPDATE subject_production_runs
            SET status = 'ready',
                error_code = NULL,
                error_message = NULL,
                error_details = NULL,
                updated_at = now(),
                version = version + 1
            WHERE error_code = :code
              AND error_details ->> 'migration' = CAST(:revision AS text)
            """
        ).bindparams(code=_ERROR_CODE, revision=revision)
    )
