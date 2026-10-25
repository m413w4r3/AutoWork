# Phase 3 — Architecture preflight (lecture seule)

Date: 2026-10-25. Repository: m413w4r3/AutoWork.
Code inspected at `main@2f9c7454d1f980cb09543e93fc0e469c15eaa668`. CI associated with this SHA: run 40183232036, completed success. This document is an **initial static review**, not a completed upstream HTML deepening audit and not a runtime execution or approval of S3.

## Observed facts and candidate seams

### A1 — advisor preparation still depends on the legacy tool (priority HIGH, candidate)

Observed: `backend/src/cti_app/application/advisor_consultation.py:144-220` defines a pure `prepare` function but imports `InvestigationAdvisorTool` within `_prepare_projection` and calls `InvestigationAdvisorTool._prompt` (around lines 197-202). Its new `SourceRefCatalog` and byte/hash rendering work, but the preparation module is not independent of the old application tool.

Observed: `backend/src/cti_app/application/investigation_advisor_tools.py` still holds `InvestigationAdvisorTool` (around line 136) and legacy response parsing (`_parse_advisor_output` around line 5607, `_parse_advisor_wire_legacy` around line 5942). The v4 parser lives in `advisor_semantic_parser.py`; `docs/mission04-closure/L22_ADOPTION_PLAN.md` says production remains v3-authoritative and L22 PREP_ONLY.

Candidate deepening: make prompt rendering a pure internal implementation belonging to a single prepared-consultation seam; retire unnecessary duplicated aggregation only after a caller-level deletion test. No production switch or send until S3 consumption authority is implemented and approved. **Do not call current code a completed advisor cutover.**

First proposed oracle: offline fake caller prepares Unicode/CRLF and conflicting source references, archives exactly the same UTF-8 bytes and SHA that its downstream adapter receives; explicit refusal prevents dispatch. Demonstrate that the legacy prompt renderer is unreachable after an approved migration (future, not done).

### A2 — investigation execution/gateway spans multiple large modules (priority HIGH, design only)

Observed: `investigation_service.py` is approximately 3,636 lines at this SHA, with `execute_tool_call` near line 1184 and `reconcile_invocation` near line 2019. `investigation_tools.py` has its dispatcher path near line 630 and registry definitions. Owner, budget, stop, gateway and effects touch different application/infrastructure modules.

The Mission 04 independent S3 review retains `S3_BLOCKED_DESIGN`: incomplete last-mile send coverage, model gateway retry, network poll under lock, late-response settlement, and normalizer continuity. These are unimplemented invariants, not cosmetic cleanup.

Candidate: map actual last-mile physical sends, owners, writers and transaction lock ranks. Evaluate a **deep** prepared/consume seam with typed transport adapters and PostgreSQL concurrency oracles. Do not implement a route, new migration or live egress just because the module is large.

First proposed oracle: two independent disposable PostgreSQL sessions and a fake transport reproduce `RUNNING -> NEEDS_REVIEW` before consume with zero sends; gateway retry/redirect/poll coverage requires an explicit permit per physical send. **Not executed in this preflight**.

### A3 — current context projection and pagination (priority MEDIUM, measure before changing)

Observed: `context_pagination.py`, `operator_tool_invocation.py`, `research_context_builder.py`, `repositories/investigation.py` were modified in Mission 04. User contracts D1-D4 are fixed: current finding versions, `observed_at, UUID` ascending, canonical full-context SHA, and stale cursor refusal. GitHub CI is green.

Candidate: a single internal projection seam could reduce duplication between full and paged reads **only if** call-graph and deletion test reveal actual leakage. Avoid a broad rewrite of working pagination. First oracle: multi-page disposable PostgreSQL with tie timestamps, new versions and stale cursor; check full/page semantic/hash parity.

### A4 — canonical PDF and audit output (priority LOW)

Observed: PDF lineage reviewer/approver and PostgreSQL + Typst oracles were integrated, and the GitHub CI passed. Candidate only if the real Phase 3 E2E reveals repeated projection logic or a difficult-to-observe omission; retain V5/V6 identity and immutable source hashes.

## What has not been done

No code refactor, tests, GitHub Actions trigger, provider call, Bridge transport, live datastore access, or S3 migration was performed during this preflight. No sample-containing end-to-end investigation in a **single complete scenario** is proved by the cross-suite 338-pass replay. The architecture HTML candidate report, choice/grilling, implementation and independent reviews belong to the next Paseo mission.

## Phase 3 gate suggestions

1. Construct separate full offline vertical journeys: one without sample, one with authorized synthetic sample and isolated object storage. Verify provenance, admitted/refused outcomes, P09/advisor, audit and V5/V6 PDF, restart/recovery and zero network egress.
2. Rank A1-A4 by measured interface complexity, locality, leverage and deletion test; do not use file length alone as a refactor justification.
3. Keep independent verdicts: `PHASE3_OFFLINE_E2E`, `ARCHITECTURE_REVIEW`, `S3_BLOCKED_DESIGN`, `ADVISOR_CUTOVER_BLOCKED_BY_S3`.
4. Re-run full CI on actual branch SHA; no automatic merge/push to main without explicit approval of Phase 3 integration.
