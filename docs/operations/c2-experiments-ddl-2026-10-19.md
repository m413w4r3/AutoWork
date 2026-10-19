# C2 experiment storage DDL (2026-10-19)

This additive SQL is for a later, explicitly approved live database change. It
has **not** been applied. The repository target schema is updated in the
mutable `0001_baseline` migration.

```sql
CREATE TABLE investigation_experiment_heads (
    investigation_id uuid PRIMARY KEY
        REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
    version bigint NOT NULL CHECK (version >= 0),
    updated_at timestamptz NOT NULL
);

CREATE TABLE investigation_experiments (
    id uuid PRIMARY KEY,
    investigation_id uuid NOT NULL
        REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
    version bigint NOT NULL CHECK (version >= 1),
    hypothesis_id varchar(128) NOT NULL,
    claim_ids jsonb NOT NULL,
    schema_version varchar(64) NOT NULL
        CHECK (schema_version = 'investigation-experiment.v1'),
    payload jsonb NOT NULL,
    payload_sha256 varchar(64) NOT NULL
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

## Rollback SQL

Rollback discards the complete C2 experiment history. It must only be used
under an approved database change plan after preserving any required records.

```sql
DROP TRIGGER IF EXISTS trg_investigation_experiments_append_only
    ON investigation_experiments;
DROP TABLE IF EXISTS investigation_experiments;
DROP TABLE IF EXISTS investigation_experiment_heads;
DROP FUNCTION IF EXISTS reject_investigation_experiment_mutation();
```
