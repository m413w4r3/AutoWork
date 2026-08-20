# ADR-0004: Replay and Lineage (Incrément 4)

**Date**: 2026-08-19  
**Status**: ACCEPTED  
**Context**: Mission Codex AutoWork - Incrément 4  

## Problem

Discovery merge strategies evolve (prompt versions, policy versions, blocking strategies). When parameters change, operators need to:
1. Run discovery ingestion through new merge logic
2. Evaluate the result against current operational state
3. Decide to activate or stay with current state

Current limitations:
1. No way to explore alternative merge outcomes
2. Cannot compare different merge strategies
3. Activation would overwrite history
4. Published artifacts would break

## Solution

Implement parallel snapshot lineages with atomic activation and identity mapping.

### 1. Dual Lineage (D14, I24)

**DiscoverySnapshotLineage enum**:
```python
OPERATIONAL  # The active chain, driving editorial workflow
REPLAY       # Parallel chain, exploration-only
```

**Invariants** (I24, I25):
- Each edition has **one active OPERATIONAL snapshot**
- Multiple REPLAY lineage snapshots can coexist
- REPLAY never auto-activates; explicit operator action required (I25)
- Activation is atomic: switches from OPERATIONAL_v1 → OPERATIONAL_v2 (replay)

**Benefit**: Explore without risk. Replay chain is completely independent until activation.

### 2. Identity Mapping (D14, I26)

**Problem**: Replay uses same ingestion but different merge parameters → different identity assignments possible.

**Solution**: ReplayIdentityMapping bridges replay subjects to operational references.

**Four cases**:

| Resolution | Meaning | Mapping Requirement |
|---|---|---|
| SAME | Same origin_key → same subject_id (deterministic) | Automatic |
| SPLIT_OF | Replay split what was merged operationally | Manual operator mapping |
| MERGE_OF | Replay merged what was separate | Manual operator mapping |
| NEW | Subject only in replay | Optional (only if overlaps published) |

**Example**:
```
Operational: Y→X (merged)
Replay: X, Y separate (split)

Mappings:
  replay_X → operational_X (SAME)
  replay_Y → operational_Y (SPLIT_OF)
```

### 3. Activation Preconditions (I26, D14)

**Mandatory check before activation**:
```
For each subject with a published artifact:
  IF subject NOT in replay:
    FAIL "Cannot activate: published subject disappeared"
  IF published_subject NOT in mapping:
    FAIL "Cannot activate: published subject unmapped"
```

This ensures:
- Published articles won't reference non-existent subjects
- No broken FK relationships after activation
- Editorial continuity guaranteed

**Deferred to V2**: Batch publishing of amendments if subjects split.

### 4. Historical Binding Preservation (D25)

**Guarantee**: Published artifacts keep historical bindings.

```
Brief published in operational V1:
  subject_id = X1 (at time of publication)
  snapshot_id = snapshot_v1
  evidence_pack_id = pack_v1
  published_at = 2026-08-19

Replay happens → creates snapshot_v2
Activation: operational := replay

Brief STILL references:
  subject_id = X1 (unchanged!)
  snapshot_id = snapshot_v1 (unchanged!)
  evidence_pack_id = pack_v1 (unchanged!)
```

UI resolves X1 through current identity chain (resolve_canonical_subject):
- If X1 ACTIVE → direct link
- If X1 MERGED into X2 → follows to X2

**Benefits**:
- Audit trail preserved
- No rewriting of historical data
- Editorial timeline unaffected
- Replay improves future, not past

### 5. Stats and Frontend (Incrément 4)

**SnapshotStats** for edition dashboard:
- subject_count, intake_count, merge_run_count
- last_update_mode (BRIDGE_RESEARCH | MANUAL_IMPORT)
- pending_merge_count (NEEDS_REVIEW)
- update_available_count (signal to editors)

**Cached and invalidated** on snapshot activation.

## Trade-offs

### Accepted

1. **Separate Lineage Chains**
   - Storage overhead: REPLAY snapshots consume space
   - Tradeoff: Isolates changes, prevents accidental overwrites
   - Justifies by safety and auditability

2. **Manual Identity Mapping**
   - Operator cost: non-SAME cases need explicit mapping
   - Tradeoff: Prevents automation bugs, ensures intention
   - Justifies by published content protection

3. **Atomic Activation**
   - Transaction complexity
   - Tradeoff: Guarantees consistency, all-or-nothing
   - Justifies by data integrity

### Deferred (Post-V1)

1. **Automatic Mapping Generation**
   - V1: Operator provides SPLIT_OF / MERGE_OF mappings
   - Future: Heuristic auto-suggestion (high confidence only)
   - No schema change needed

2. **Batch Amendment Publishing**
   - V1: If subject splits during activation, manual action required
   - Future: Auto-generate amendments for split subjects
   - Requires LLM + workflow

3. **Concurrent Replay Chains**
   - V1: One REPLAY at a time per edition
   - Future: Multiple concurrent replays (each with unique run_id)
   - Needs UI work

## Implementation

### Domain Models (Phase 4.1)
```python
ReplayIdentityResolution  # enum: SAME | SPLIT_OF | MERGE_OF | NEW
ReplayIdentityMapping     # Maps replay → operational
ReplayComparison          # Summary report
```

### Application Services (Phase 4.2)
```python
ReplayService.replay_edition_discovery()
ReplayService.calculate_identity_mapping()
ReplayActivator.validate_activation_preconditions()
ReplayActivator.activate_replay()
SnapshotStatsCalculator.calculate_snapshot_stats()
```

### Database (Phase 4.3)
```sql
replay_identity_mappings
  (replay_run_id, replay_subject_id, operational_subject_id, resolution)

replay_comparisons
  (replay_run_id, edition_id, subjects_same/split/merged/created)
```

### API (Phase 4.4)
```
POST   /editions/{id}/replay
       → launches replay, returns job_id

GET    /editions/{id}/replays/{run_id}/comparison
       → ReplayComparison report

GET    /editions/{id}/replays/{run_id}/mappings
       → List of ReplayIdentityMapping (editor for manual mapping)

POST   /editions/{id}/replays/{run_id}/mappings
       → Operator provides mappings

POST   /editions/{id}/replays/{run_id}/activate
       → Atomic promotion (preconditions validated)

GET    /editions/{id}/stats
       → SnapshotStats for dashboard
```

### Tests (Phase 4.5)
- Replay creates separate lineage
- Identity mapping (SAME, SPLIT_OF, MERGE_OF, NEW)
- Activation precondition validation
- Published artifacts keep bindings
- Comparison report generation
- Stats calculation

## Consequences

**Benefits**:
1. Explore merge strategies without risk (D14)
2. Compare before commit (I25: never auto-activate)
3. Published content stays intact (D25: historical binding)
4. Atomic activation guarantees consistency (I18-ish for replay)
5. Operator explicitly approves identity decisions
6. Audit trail preserved

**Drawbacks**:
1. Storage overhead (multiple snapshot chains)
2. Operator cost (mapping for SPLIT/MERGE cases)
3. No automatic split handling (deferred to V2)

**Mitigations**:
1. Archive old replays after decisions made
2. Auto-suggest mappings (high-confidence SAME only in V2)
3. Amendments handle split subjects post-activation

## Related Decisions

- **D14**: Replay distinct from operational; no auto-activation
- **D18**: SubjectContribution immutable (preserved through merge)
- **D25**: Published artifacts keep historical bindings
- **I24**: Edition replayable from ordered intakes
- **I25**: Replay never auto-activates; explicit mapping required
- **I26**: Activation precondition: all published subjects mapped

## References

- current-task.md § 18 (Replay and history)
- current-task.md § 19-26 (Invariants I24-I26)
- ADR-0003 (Editorial preservation, prerequisite for i4)
