# C2 experiment storage DDL (2026-10-19)

The additive DDL below was applied to the live PostgreSQL database on
2026-10-19 under the already-recorded authorization. At that time, the
repository still used a mutable `0001_baseline` and had no `0002` revision.
The decision recorded in ADR-0007 freezes `0001_baseline` at the pre-C2 shape
and restores an additive `0002_c2_experiments` revision. The live database
still holds these objects and remains stamped `0001_baseline` until an
operator runs the documented backup, preflight and upgrade procedure.

```sql
CREATE TABLE investigation_experiment_heads (
    investigation_id uuid PRIMARY KEY
        REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
    version bigint NOT NULL
        CONSTRAINT ck_investigation_experiment_heads_version CHECK (version >= 0),
    updated_at timestamptz NOT NULL
);

CREATE TABLE investigation_experiments (
    id uuid PRIMARY KEY,
    investigation_id uuid NOT NULL
        REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
    version bigint NOT NULL
        CONSTRAINT ck_investigation_experiments_version CHECK (version >= 1),
    hypothesis_id varchar(128) NOT NULL,
    claim_ids jsonb NOT NULL,
    schema_version varchar(64) NOT NULL
        CONSTRAINT ck_investigation_experiments_schema_version
            CHECK (schema_version = 'investigation-experiment.v1'),
    payload jsonb NOT NULL,
    payload_sha256 varchar(64) NOT NULL
        CONSTRAINT ck_investigation_experiments_payload_sha256
            CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    CONSTRAINT uq_investigation_experiments_version
        UNIQUE (investigation_id, version),
    CONSTRAINT ck_investigation_experiments_payload_identity CHECK (
        payload->>'schema_version' = schema_version
        AND payload->>'id' = id::text
        AND payload->>'investigation_id' = investigation_id::text
        AND payload->>'version' = version::text
    ),
    CONSTRAINT ck_investigation_experiments_payload_shape CHECK (
        jsonb_typeof(payload) = 'object'
        AND jsonb_typeof(claim_ids) = 'array'
    )
);

CREATE INDEX ix_investigation_experiments_claim_timeline
    ON investigation_experiments (investigation_id, hypothesis_id, version);

CREATE FUNCTION reject_investigation_experiment_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'investigation_experiments is append-only'
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_investigation_experiments_append_only
    BEFORE UPDATE OR DELETE ON investigation_experiments
    FOR EACH ROW EXECUTE FUNCTION reject_investigation_experiment_mutation();
```

## Live execution record (2026-10-19)

- Before the change, the live database was PostgreSQL 16.14 at
  `0001_baseline`; both tables, the timeline index, mutation function and
  append-only trigger were absent. The authenticated GET returned HTTP 500.
- The idempotent equivalent of the DDL above was applied once with `psql` from
  the PostgreSQL container. The live Alembic marker remains `0001_baseline`,
  which matches the baseline now that the objects exist.
- Exact idempotent SQL artifact:
  `/home/nill/backups/autowork/run12-corr-ddl-c2-20261019T163923Z.sql`
  SHA-256: `5a9ae1daa1ca8ad54f8625962ea034499f5b0dae46a84b80d0ddb1b8aed2bec7`.
  It wraps the table/index creation in `IF NOT EXISTS` and checks the function
  and trigger catalogs before creating those objects.
- Pre-change jobs: 0 `RUNNING`, 0 `QUEUED`. Row counts immediately before and
  after were identical: investigations 12; finding versions 139; finding
  observations 340; finding claims 184; evidence observations 1290; tool
  invocations 995; job events 997. Both experiment tables contain 0 rows.
- After the change, all expected named CHECK/UNIQUE/FK constraints, the index,
  function and trigger are present. The live GET for investigation
  `5f66ebfb-1653-45e6-83cd-a29af6f87868` returned HTTP 200 with
  `stream_version: 0`, `experiments: []`, and `claims: []`.
- The same SQL artifact was applied twice on a disposable PostgreSQL test
  database. CHECK and UNIQUE violations were rejected, UPDATE and DELETE were
  rejected with SQLSTATE `55000`, and the transaction ended with 0 experiment
  rows. The scratch database was then removed.
- Pre-change backup:
  `/home/nill/backups/autowork/autowork-live-cti_app-pre-run12-corr-ddl-c2-20261019T163923Z.dump`
  (3,574,179 bytes, mode `0600`, timestamp 2026-10-19 18:39:32 +0200).
  `pg_restore --list` succeeded and produced 791 TOC entries. The dump is a
  custom-format backup of the complete database, including schema and data.
- No live rollback DDL was executed. Recovery keeps this backup and validates any
  restore in an isolated database; removing these objects from live would need
  a separate explicit change decision.

The application computes `payload_sha256` from canonical UTF-8 JSON and checks
it again when reading. The head row is the mutable CAS cursor; experiment rows
are immutable history. Each append locks the head, compares the caller's
`expected_version`, writes exactly `expected_version + 1`, and advances the
head in the same transaction.

## API and operator contract for C7

`GET /api/investigations/{investigation_id}/experiments` returns
`investigation-experiment-timeline.v1`:

- `investigation_id`, `stream_version`, and `experiments`, ordered by the
  append version. Every experiment remains visible, including incompatible
  verdicts and each experiment's `scope` and `limitations`.
- `claims`, one projection per article claim with `status` (`supported`,
  `refuted`, `qualified`, or
  `inconclusive`), distinct observed `verdicts`, ordered `experiment_ids`,
  per-experiment scopes and observation IDs, and `contradictory`.
- Each `investigation-experiment.v1` item contains prediction, falsification
  criterion, repeatable method and `method_mode: "passive"`, input refs and hashes, preconditions,
  availability, tool/version/schema/secret-free arguments, step and persisted
  invocation refs, persisted observation IDs and SHA-256 values, expected and
  actual results, observation coverage, dates, scope, limitations, verdict,
  implication, and follow-up `next_pivots`.
- `next_pivots` contains C1 pivot-frontier node IDs (64 lowercase hexadecimal
  characters). The timeline does not need to resolve those IDs because C1
  supplies the frontier read endpoint.

Operator writes use the existing authenticated
`POST /api/investigations/{investigation_id}/operator/tools/experiment.record`
route with tool schema `operator-experiment.v1` and the operator's current
working-state version and idempotency key. The tool requires `operator.write`,
a current approved G2 envelope, a hypothesis and article claim in the same
investigation, and observation refs whose UUID, investigation UUID, and SHA-256
match persisted observations. A limited-coverage `not_observed` result cannot
be recorded as `refutes`. `experiment.list` uses schema
`investigation-experiment-timeline.v1` and returns the same timeline shape.

The existing investigation response also adds finding revision identity:
`id` and `current_version_id` identify the current immutable revision,
`logical_id` is the root revision UUID, and `revision_chain` lists every
`{id, version, created_at}` from root through current. Frozen references to an
older `id` remain valid.

The API schema enums are versioned with the corresponding schema fields;
`ExperimentVerdict` is `supports | refutes | qualifies | inconclusive |
blocked`, `actual_outcome` is `observed | not_observed | unknown`, and
`observation_coverage` is `complete | limited | none`. Consolidation retains
all experiments; any `supports` plus `refutes` pair produces `qualified` with
`contradictory: true`.

## Destructive rollback SQL (not executed)

This SQL discards the complete C2 experiment history. It was not run and is
not part of the additive migration. The `0002` downgrade refuses to proceed
when experiment or head rows exist. Downgrade is for disposable fixtures only;
never run it against the live database, even when the current row counts are
zero.

```sql
DROP TRIGGER IF EXISTS trg_investigation_experiments_append_only
    ON investigation_experiments;
DROP TABLE IF EXISTS investigation_experiments;
DROP TABLE IF EXISTS investigation_experiment_heads;
DROP FUNCTION IF EXISTS reject_investigation_experiment_mutation();
```
