# Discovery — Module Map for Agents

## Quick Navigation

**If the task concerns...** → **Start with:**

| Task | Module |
|------|--------|
| Merge plan validation, review policy, duplicate guards | `cumulative/validation.py` |
| Merge application, source folding, IOC remap | `cumulative/apply.py` |
| External model prompt, ChatGPT request, repair logic | `cumulative/chatgpt_planner.py` |
| Stale/rebase detection, human resolution, editorial linking | `cumulative/service.py` |
| DiscoveryBatch persistence, source verification, manual import | `service.py` |
| Request idempotency, hash computation | `contracts.py` |
| Discovery search prompt | `prompts.py` |
| Background polling, recovery flow, conversation identity | `recovery.py` |
| Analyst URL attachment | `manual_source_edits.py` |
| Job adapter lifecycle | `jobs.py` (nominal) or `cumulative/jobs.py` (cumulative) |

---

## Ownership & Entry Points

### Discovery Nominal (`service.py` ↔ `contracts.py`)

- **service.py**: Main Discovery orchestration, DiscoveryBatch persistence, standalone manual report import, source verification/listing
- **contracts.py**: `DiscoverEditionParameters`, request hash, idempotency key
- **prompts.py**: Discovery search prompt template
- **ports.py**: Archive/bridge protocol abstractions
- **recovery.py**: Background polling, visible/manual/completion recovery, fail-closed conversation identity
- **jobs.py**: Discovery Job adapter
- **manual_source_edits.py**: Analyst URL attachment to candidates

### Cumulative Discovery (`cumulative/service.py` ↔ `cumulative/apply.py`)

- **service.py**: Unit-of-work orchestration, stale/rebase detection, cache/review/apply persistence, human resolution, editorial linking
- **validation.py**: Merge-plan validation, review policy, editorial duplicate guard
- **apply.py**: Pure deterministic merge application, source folding, IOC remap, snapshot hash, non-loss guard ← **Start here for data-loss bugs**
- **chatgpt_planner.py**: External merge prompt, model request, one-shot repair attempt, conversation cleanup
- **planners.py**: Local heuristic/human/targeted planners
- **merge_runs.py**: Deterministic `DiscoveryMergeRun` identity & audit trail
- **context.py**: Delta representation, canonical candidate state, blocking, handles, model projection
- **types.py**: Delta/handles/planner protocol types, result types
- **errors.py**: Stale, review, model-unavailable, invalid-plan errors
- **contracts.py**: `ReconcileDiscoveryParameters`
- **jobs.py**: Cumulative reconciliation Job adapter

---

## Import Rule

Import **directly** from the module owner; do not re-export via `__init__.py`, `service.py`, or create `_shared.py`.

---

## Core Invariants

- ✓ All hashes/UUIDs deterministic
- ✓ No incoming handle lost or duplicated
- ✓ Existing handles used at most once
- ✓ Multiple existing subjects → review before HUMAN resolution
- ✓ No-loss on sources & member refs (audit `apply.py`)
- ✓ `same_publication` is the source-folding rule
- ✓ Stale reviewed plan never applied to new parent
- ✓ Double-click human resolution is idempotent
- ✓ Model unavailable = transient job error, not human review
- ✓ Prompt repair attempted at most once
- ✓ External conversation identity fail-closed

---

## Key Test Files

- `tests/test_discovery_cumulative.py` — Merge logic, source folding
- `tests/test_discovery_merge_planning.py` — Planner behavior, plan validation
- `tests/test_discovery_recovery.py` — Background polling, recovery flow
- `tests/integration/test_merge_review_resolution.py` — Human resolution, stale detection
- `tests/integration/test_cumulative_discovery_repository.py` — Persistence & UoW

---

## When to Question Defaults

1. **DB persistence** → Check cumulative repository (not application service, except orchestration)
2. **Pure merge logic** → Start in `validation.py`/`apply.py`, never `service.py`
3. **Incoming handles** → Audit `apply.py` for no-loss guarantee
4. **Model unavailable** → Transient error; never escalate to human review
5. **Prompt repair** → Allowed exactly once in `chatgpt_planner.py`
