# ADR-0003: Editorial Preservation and Amendment (Incrément 3)

**Date**: 2026-08-19  
**Status**: ACCEPTED  
**Context**: Mission Codex AutoWork - Incrément 3  

## Problem

Published editorial content must remain immutable when underlying discovery data evolves. At the same time, new information about a subject should surface to editors as `UPDATE_AVAILABLE` signal without modifying the original publication.

Current system risks:
1. Updates to discovery snapshots could indirectly modify published briefs
2. No mechanism to track which evidence was incorporated into each artifact
3. No way to link new information to published content for amendment workflow
4. Subject merges mask contributions from absorbed identities

## Solution

Implement three key mechanisms:

### 1. Evidence Pack Extension (D12 Compliance)

**Existing Structure** (from increments 1-2):
- `BriefEvidencePack` captures the exact evidence used for a brief
- Immutable and versioned per brief version

**Extension** (Increment 3):
- Add `covered_contribution_ids: UUID[]` - the exact contributions incorporated
- Add `built_from_snapshot_id` - explicit snapshot reference (never "current active")
- Add `scope: FULL | DELTA` - full snapshot or delta-only
- Add `base_pack_id` - for DELTA packs, reference to parent

**Invariants**:
- FULL packs: `base_pack_id = None`, covers all contributions up to snapshot version
- DELTA packs: `base_pack_id != None`, covers only new contributions vs parent
- All evidence packs bound to explicit `snapshot_id`, never to "active snapshot"
- Prevents concurrent snapshot activation from contaminating pack construction

### 2. Contribution Closure (D20, I20)

**Problem**: When subject Y is merged into X, Y's `SubjectContribution` rows remain unchanged (I19). But naive queries for "X's contributions" would miss Y's contributions.

**Solution**: `contribution_closure(subject_id) → Set[contribution_ids]`
- Follows merge chain to canonical identity
- Recursively includes contributions from all identities merged into canonical
- Enables accurate `UPDATE_AVAILABLE` detection post-merge
- Essential for D15: amendments correctly reference all evidence

**Example**:
```
Y merged into X → X = {y1, y2} ∪ {x1, x2} = {x1, x2, y1, y2}
Amendment chains: amend_packs = [pack(y1, y2, x1)] 
New = closure - covered = {x2}
```

### 3. Amendment Model (D15, D17, Increment 3)

**BriefAmendment** (immutable, append-only):
- `parent_artifact_id` - the item being amended (brief or another amendment)
- `root_artifact_id` - the original brief (invariant across chain)
- `kind: UPDATE | CORRECTION | CLARIFICATION`
- `contribution_ids` - the contributions this addresses
- `status: DRAFT → APPROVED → PUBLISHED`
- `evidence_pack_id` - a DELTA pack (only new contributions)
- `revision_reason` - for redactional changes (CORRECTION)

**Guarantees**:
- Original brief never edited in-place
- Amendment explicitly references parent and root
- Amendment pack is DELTA, not full restatement
- Redactional revisions create new versions with `revision_reason`

### 4. UPDATE_AVAILABLE Signal (D16, I31)

**Distinction from STALE**:
- `STALE`: Pack input changed, non-published artifact recalculable (existing)
- `UPDATE_AVAILABLE`: Subject received new contributions not in artifact packs (new)
- Orthogonal signals; both can exist simultaneously
- `UPDATE_AVAILABLE` computed at read-time, never persisted as state
- Never applied to published content

**Calculation** (determ inistic, no LLM in V1):
```python
new_contributions = (
    contribution_closure(subject_id)
    - union(pack.covered_contribution_ids 
            for pack in [root_pack] + amendment_packs)
    - dismissed_contribution_ids
)
return "UPDATE_AVAILABLE" if new_contributions else "CURRENT"
```

### 5. Editorial Update Decision (D15, Incrément 3)

**EditorialUpdateDecision** (append-only log):
- `action: DISMISS | RESTORE`
- `contribution_ids` - which contributions to ignore
- Per `(artifact_id, contribution_id)` pair, last decision wins
- Enables "ignore for this edition" with audit trail
- `RESTORE` brings signal back without losing dismiss history

## Trade-offs

### Accepted

1. **DELTA Packs Smaller than FULL**
   - More efficient storage
   - Simpler amendment text (only new material)
   - Tradeoff: Query cost for full pack chain

2. **Explicit Snapshot Binding**
   - Higher upfront cost: every pack needs `snapshot_id`
   - Tradeoff: Prevents accidental cross-snapshot contamination
   - Justifies by correctness guarantee

3. **Coverage Closure Computation**
   - Requires merge event chain lookup at read-time
   - Tradeoff: Enables correct UPDATE_AVAILABLE detection post-merge
   - Cacheable for frequent queries

### Deferred (Post-V1)

1. **LLM-Based Materiality Classifier**
   - V1 uses: new contribution exists → NEW_EVIDENCE
   - Future: Replace `EditorialImpactEvaluator` with LLM without schema change
   - No migration required: wrapped as service

2. **Cross-Edition Lineage** (D17)
   - Amendments are intra-edition in V1
   - `cross_edition_lineage_id` reserved in `DiscoverySubjectIdentity`
   - Enables future feature without schema rework

3. **Automatic Amendment Drafting**
   - V1 creates amendment container only
   - Editors write amendment text manually
   - Future: LLM-generated delta summaries

## Implementation

### Domain Models (Phase 3.1)
- Extend `BriefEvidencePack`: add scope, coverage, snapshot binding
- New: `EvidencePackScope` enum
- New: `BriefAmendment` dataclass
- New: `EditorialUpdateDecision` append-only log
- Enum: `EditorialImpactLevel` (NO_CHANGE, NEW_EVIDENCE, MATERIAL_UPDATE)

### Application Services (Phase 3.2-3.3)
- `coverage_calculator.py`: resolve_canonical, contribution_closure, new_contributions
- `editorial_impact_evaluator.py`: EditorialImpactEvaluator service (V1: deterministic)
- `amendment_service.py`: create amendments, build DELTA packs, redactional revisions

### Database (Phase 3.3)
- Migration: Add columns to `brief_evidence_packs`
- Migration: Create `brief_amendments` table
- Migration: Create `editorial_update_decisions` table (append-only)

### API (Phase 3.3)
- `POST /editions/{id}/amendments` - create amendment draft
- `POST /editions/{id}/amendments/{amend_id}/approve` - approve amendment
- `POST /editions/{id}/amendments/{amend_id}/publish` - publish amendment
- `POST /editions/{id}/update-decisions` - dismiss or restore UPDATE_AVAILABLE

### Tests (Phase 3.4)
- Coverage calculator: closure, merge chains, new contributions
- Amendment service: create, chain, delta packs, revisions
- Impact evaluator: V1 logic
- Integration: published brief + new contributions → UPDATE_AVAILABLE → amendment

## Consequences

**Benefits**:
1. Published content never modified by discovery updates (D15)
2. New information surfaced via UPDATE_AVAILABLE signal (D16)
3. Editors can create amendments with explicit lineage (D17)
4. Merged subjects' contributions don't get hidden (I20)
5. Audit trail for dismissing updates (EditorialUpdateDecision)
6. No LLM-based materiality required for MVP (extensible later)

**Drawbacks**:
1. Storage overhead: coverage_ids, snapshot binding per pack
2. Query complexity: need to follow merge chains for closure
3. Editorial workflow more complex: manage amendment chains

**Mitigations**:
1. Coverage stored as UUID array (compact)
2. Closure computation cached for frequent subjects
3. Amendment workflow is opt-in; auto-generated signals only (no forced action)

## Related Decisions

- **D15**: Published content immutable
- **D16**: STALE vs UPDATE_AVAILABLE distinction
- **D17**: Amendments intra-edition in V1
- **D18**: SubjectContribution never mutated
- **D20**: Composition of closure (canonical + merged subjects)
- **I20**: Closure used for contribution reads
- **I31**: UPDATE_AVAILABLE computed, not stored

## References

- current-task.md § 15-17 (Editorial preservation)
- current-task.md § 16 (Coverage and new contributions)
- current-task.md § 17 (Amendments)
- current-task.md § 18 (Replay, references published content)
