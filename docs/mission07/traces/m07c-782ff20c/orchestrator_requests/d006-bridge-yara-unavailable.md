# D006 — Bridge YARA consultation unavailable before transport

Invocation `1b54dc77-842a-4e5c-95bf-30ae11b7c6b2` (`advisor.consult`, `yara_draft`) returned `advisor_unavailable` / `bridge_unreachable`. The response records `preflight=READY`, `failure_stage=preflight`, `invocation_stage=pretransport_failure`, `confirmed_non_execution=true`, `prior_submission_made=false`, zero Bridge turns consumed, and zero response bytes. No content was submitted to Bridge.

One reconciliation was attempted once (invocation version 3). It returned `needs_review`, `non_execution_sealed=false`, `resubmission_allowed=false`, and no provider response ID. Current state v180 still lists it as ambiguous; Bridge budget shows 4 consumed / 5 reserved. No further Bridge request was sent. Please reconcile/seal the pre-transport non-execution and release or explain the retained reservation before another Bridge submission. Static YARA drafting and downstream rule testing remain pending.
