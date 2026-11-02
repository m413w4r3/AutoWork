# Mission 07 file relay

`relay.py` accepts request files from `inbox/`, executes GET and POST calls,
and writes results to `outbox/`. POSTs remain serialized through one worker;
GETs use the configured GET worker pool. The journal and status files add
observability fields without removing the older keys.

Each journal entry and request status record includes:

- `received_at`, `started_at`, and `completed_at` in UTC.
- `queue_wait_seconds`, measured from receipt to start, and `run_seconds`,
  measured from start to completion. While queued or running, status records
  show elapsed estimates; the journal records the final durations.
- `lane` (`get` or `post`) and `request_id`.
- `idempotency_key_sha256` for POSTs. The raw key is not written to the
  journal or status records; GETs have a null value.
- `queue_position` on queued status records, counted among requests in the
  same lane.

`relay-status.json` keeps the existing `last_completed_id` and adds a
`last_completed` record so the completed request's timing fields remain
available after its inflight file is removed.

`relayget.get()` and `relayget.post()` print a status line when a request
changes state. The vocabulary is:

- **received** — the relay has read the request and is preparing to dispatch it.
- **queued** — waiting for a GET or POST slot; POST lines include queue position.
- **running** — the executor is handling the request in its lane.
- **completed** — a response was received and saved to `outbox/`.
- **ambiguous** — transport ended without a trustworthy response. The relay
  does not resend it; reconcile the existing request before deciding what to do.

## POST lane decision

Keep a single POST lane. The API waits for `operator_tool_invocations.invoke()`
to finish. Its `advisor.consult` path saves a new working-state version before
the Bridge call, while strict local analytic writes also use working-state
compare-and-swap. A concurrent local write can advance canonical state while
the Bridge call is in flight, leaving the advisor response with an older
version and making the caller's next POST fail with `stale_working_state`.
The registry also contains external-provider tools, so treating every
non-advisor tool name as local is not a safe lane classifier.

Reconsider separate lanes only after a backend contract accepts an invocation
with a pending ID, allows polling that ID, exposes its completed result, and
supports idempotency-key lookup/replay. Relevant implementation paths are
`backend/src/cti_app/application/operator_tool_invocation.py`,
`backend/src/cti_app/application/investigation_tools.py`, and
`backend/src/cti_app/api/investigation.py`.
