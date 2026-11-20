# Editorial workspace architecture

**Status:** proposed target for Mission 22. [`DECISIONS.md`](../../../.autowork-runs/m22/DECISIONS.md) D1–D7 remains authoritative until the orchestrator accepts proposed [ADR-0010](../../adr/ADR-0010-editorial-workshop-contracts.md) in M8 after implementation and reviews; D8/D8b govern DDL ticket scope. This document maps those decisions to current code and specifies contracts, seams and rollout. If the documents diverge, stop and report it. Facts here were checked at integrated base `887445f0`: M1–M3 through the T-M3-15 cutover, including migrations 0011–0015, are present. T-M3-16/17, drafts, gallery, and V9 placement/chart work remain targets. The direct legacy-writer/shared-effects deviation from the original D7.B1 wording is called out below.

## 1. Module map

The table separates integrated seams at the stated base from later target work.

| Layer | Before | After (target) |
|---|---|---|
| Backend API | `api/production.py` and `api/subject_content.py` retain production and `/content` APIs | `api/editorial_workspace.py` implements snapshot, occurrence-thread, proposal read/reject, and accept routes. The accept route is wired but returns 404 `accept_not_enabled` while `EDITORIAL_PROPOSAL_ACCEPT_ENABLED` is false (default); proposal activation and drafts/gallery remain target work |
| Backend application | Legacy production revision/enrichment services remain compatible direct writers | Integrated: `application/editorial_workspace/{service,registry}.py`; `editorial_threads.py` and extracted thread/context/turn modules; `editorial_synthesis/{candidate,targets,patch_validation}.py`; `editorial_enrichment_candidate.py`; `editorial_enrichment_revision.py`; `editorial_proposals.py`; `editorial_proposal_adoption.py`; `editorial_proposal_adoption_inputs.py`; `editorial_revision_effects.py`; `production_repair_factory.py`; proposal-impact persistence. Draft and gallery modules remain target work |
| Backend domain and persistence | Canonical production artifacts remain versioned blob references; model conversations/turns retain their transport role | Integrated: workspace/thread/job/slot plus proposal/model-run/event/impact persistence. Keep canonical synthesis blocks free of occurrence IDs; drafts, gallery and V9 placement/chart contracts remain target work |
| Frontend routing/API | `App.tsx` routes `/subjects/:id/editor` before subject detail; `api/editorialWorkspace.ts` provides the typed API | Snapshot/thread and proposal read/reject client paths are integrated; `Proposer` activation, user-visible `Accepter`, drafts and gallery remain target work |
| Frontend view | `ProductionArtifactView.tsx` is the historical read adapter; M1-03 extracted publication/read components under `features/publication/` | `features/editorial-workspace/` contains the snapshot page, layout, document, outline, inspector, conversation and proposal card/diff. Manual edit/table composer/figure gallery remain target work |

Code anchors for the integrated scope: `api/editorial_workspace.py`; `application/editorial_workspace/`; `application/editorial_threads.py`, `editorial_discussion_context.py`, `editorial_conversation_turns.py`, `editorial_thread_{job_completion,reconciliation,sessions}.py`, and `editorial_bridge_gate.py`; `application/editorial_synthesis/`; `editorial_enrichment_candidate.py`; `editorial_revision_effects.py`; `editorial_proposal_adoption_inputs.py`; `production_repair_factory.py`; `frontend/src/App.tsx`; and `frontend/src/features/editorial-workspace/`. Historical publication rendering is extracted under `frontend/src/features/publication/` and composed by `components/ProductionArtifactView.tsx`.

## 2. Contracts

### Workspace snapshot and block

`GET /editor` returns a server projection of exact pinned canonical artifacts. It is not a new evidence artifact. The registry holds identities and structure only; text and evidence references are projected from the pinned blob.

```json
{
  "snapshot_id": "uuid",
  "subject_id": "uuid",
  "production_run_id": "uuid",
  "publication_kind": "article | brief",
  "synthesis_artifact_id": "uuid",
  "synthesis_version": 2,
  "synthesis_canonical_sha256": "sha256",
  "enrichment_artifact_id": null,
  "enrichment_version": null,
  "publication_artifact_id": null,
  "publication_version": null,
  "pipeline_generation": 4,
  "blocks": [{
    "block_id":"uuid", "kind":"paragraph", "parent_block_id":"uuid|null",
    "position":2, "text":"projected text", "evidence_refs":[],
    "revision_target":{"block_id":"uuid", "legacy_target_id":"opaque|null"},
    "before_hash":"sha256", "allowed_operations":["rewrite","delete","insert_after"],
    "revision_status":"ready|proposal_pending|stale|needs_review|locked",
    "placement_anchor_id":null
  }],
  "enrichments": [],
  "document_outline": [],
  "capabilities": {},
  "preview_status":"IN_PROGRESS|READY|FAILED|STALE"
}
```

This is the flat wire DTO: `subject_id`, `production_run_id`, `publication_kind`, `synthesis_artifact_id`, `synthesis_version`, `synthesis_canonical_sha256`, nullable `enrichment_artifact_id`/`enrichment_version` and `publication_artifact_id`/`publication_version`, `pipeline_generation`, `blocks`, `enrichments`, `document_outline`, `capabilities`, `preview_status`, and opaque `snapshot_id`. Do not add parallel `pins` or `outline` fields.

The server mints distinct IDs per physical occurrence in a run. For a legacy pin, first-read UUIDv5 seeds include artifact ID, canonical SHA, kind and structural pointer; this makes duplicate paragraphs distinct and reload stable. Later versions inherit IDs only from recorded lineage or a unique exact correspondence. The client sends `snapshot_id` and `block_id`; it never derives target paths, hashes, or capabilities. `before_hash` is a server concurrency fence, not an identity.

Stable errors: `publication_kind_unresolved` (« Le type de publication est indéterminé. L’atelier reste en lecture seule. »), `editorial_snapshot_stale` (« Le document a changé. Rechargez l’atelier. »), `editorial_identity_ambiguous` (« L’identité de ce bloc historique est ambiguë. Il reste en lecture seule. »), `editorial_block_missing` (« Ce bloc n’existe plus dans cette version. »), `editorial_before_hash_mismatch` (« Ce bloc a changé. Rechargez l’atelier avant de réessayer. »), `editorial_anchor_stale` (« Le paragraphe d’ancrage a changé ou a été supprimé. Repositionnez l’élément. »), `editorial_registry_conflict` (« La continuité de ce bloc ne peut pas être prouvée. Une revue est nécessaire. »).

### Proposal aggregate and transitions

`EditorialProposal` stores: proposal ID; subject/run; origin (`bridge_draft` or `manual_patch`); target `block_id` and operation; exact base snapshot/artifact/version/input hash/canonical SHA/routing hash and `before_hash`; typed patch blob ref and SHA-256; admitted evidence refs and policy/version diagnostics; optional thread, `variant_group_id` and superseded IDs; exact ModelRun/turn refs (none for manual edits); state and state version; actor/idempotency identity; timestamps; decision and accepted artifact refs. `variant_group_id` keys `(base pin, target occurrence, operation, thread_id)`. Patch bytes are immutable in MinIO and re-hashed at every read/adoption.

| Current state | Command/result | Next state |
|---|---|---|
| `preparing` | validated immutable patch | `proposed` |
| `preparing` | deterministic refusal or invalid patch | `refused` |
| `preparing` | ambiguous execution/validation requiring a person | `needs_review` |
| `proposed` | human accept, exact fences pass | `accepted` |
| `proposed` | human reject | `rejected` |
| `proposed` | any base/target/routing/evidence/thread fence changed | `stale` |

Terminal states never reopen. Accept locks proposal and production run, verifies exact patch bytes/hash, revalidates policy and evidence, then performs the only adoption CAS. It never calls Bridge. Accepting a variant atomically marks its `variant_group_id` siblings stale. Any other proposal whose pins changed is projected stale on read and persisted stale before a decision can be offered. An impact failure leaves a visible, replayable `pending` impact; it cannot undo the accepted artifact.

### Thread, events and Bridge state

A thread session is identified by `(subject_id, occurrence_id, session_number)`. `session_number` distinguishes successive sessions for the same occurrence; the thread's origin subject/run/occurrence and artifact/version/canonical SHA are immutable. Its current reference advances only along proven registry lineage. Missing or ambiguous lineage makes the binding stale and blocks proposal work. Each session owns a private `ModelConversation` with purpose `EDITORIAL_DISCUSSION`; its turns and ModelRuns are not the UI timeline. A lost session is continued by creating the next session number for that occurrence.

The append-only event union is `user_message | bridge_reply | proposal_ref | refusal | proposal_decision | reconciliation`. Events carry sequence, actor, idempotency key/digest and typed refs; bodies live in immutable blobs with SHA-256. Proposal events reference D2 `proposal_id`; no thread-owned proposal payload/table duplicates D2. M2 is message-only: `message` can produce discussion text but never a proposal. API and UI refuse/hide `propose` until D2 and the unique adoption path are ready and the cutover gate passes; then `propose` accepts only `EditorialPatchV1 | EditorialRefusalV1`. All messages and model outputs remain non-evidence.

Bridge state is a projection: `ready | waiting_for_capacity | generating | reply_available | refused | session_lost | reconciliation_required`. A durable thread job/outbox permits one active job per thread. After possible external submission, inspect/reconcile the exact ModelRun/turn; never retry by resubmitting. `session_lost` is a typed reconciliation outcome recorded in the generic `reconciliation` event; it adds no separate table or column. A lost session starts the next numbered session for the same occurrence, never a reconstructed browser URL. Re-evaluate authorization and evidence admission on every generation.

### Typed patch union, charts, figures and placement

`EditorialPatchV1` is a closed discriminated union: `rewrite_block`, `insert_block`, `delete_block`, `patch_enrichment_element`, `insert_table(TableSpecV1)`, `insert_chart(TimelineChartSpecV1 | BarChartSpecV1)`, `insert_archived_figures(FigureSelectionPatchV1)`. Each variant names the stable target/parent and base fence. Evidence references are typed. It has no free HTML, SVG, Typst, JavaScript, Mermaid, URLs or client-selected blob keys. Manual and Bridge patches pass through the same validator/adoption writer.

`BarChartSpecV1` is semantic data only: key/title/caption, category axis label, numeric axis label and unit, an ordered list of `{category, finite_decimal_value, evidence_refs}`, analytic purpose and placement. Each numeric datum needs admissible evidence; reject missing, contradictory or invented values. Compile deterministically through the existing local chart/media path; pin compiler/policy versions and SHA. Never accept model-supplied SVG. Current charts are timeline-only; current table rows already carry evidence refs and width/column constraints (`domain/production_editorial_enrichment.py:141-265,296-335`).

`FigureSelectionPatchV1` contains an ordered list of server-resolvable accepted inventory figure IDs, optional editorial captions, and the selected `anchor_block_id`. Re-resolve every figure against the exact subject/run inventory and verify archived blob ID/SHA, MIME, byte size and dimensions. No model call. For BRIEF, retain the three-figure cap; it is distinct from the 128-candidate inventory limit (`application/source_figure_inventory.py:51-60,65-102,992-1057`).

Placement is a strict tagged union: existing `after_lead`, `after_section(section_index)`, `after_timeline`, `end`, plus `after_paragraph(anchor_block_id)`; exactly one payload is valid for each tag. Preserve legacy placements/readers. Resolve the paragraph against the exact pinned workspace registry during assembly. A moved occurrence carries its ID; a deleted/ambiguous anchor becomes stale and makes preview `STALE`. Never fall back to section/document end. Elements sharing an anchor follow canonical placement position then `element_key`. Publication V9 and ordered Typst render data record enough resolved anchor/pin data to reproduce the same location in the PDF. Current placement only has section indexes (`domain/production_editorial_enrichment.py:108-127`; render projection `application/typst_rendering.py:2539-2654`).

## 3. HTTP API

Base path: `/api/subjects/{subject_id}/editor`. Authenticated actor and subject/run authorization come from the server session; React never supplies policy, evidence context, provider, or ModelRun IDs.

FastAPI errors use the actual response envelope `{"detail":{"code":"…","message":"…"}}`; the frontend maps the stable `detail.code`. Editorial `Idempotency-Key` values are opaque tokens matching `^[A-Za-z0-9_-]{16,128}$`; an invalid value returns `idempotency_key_invalid`.

| Method and path | Request → response | Idempotency | Stable errors |
|---|---|---|---|
| `GET /` | optional `artifact_id` → `EditorialWorkspaceSnapshot` | Same exact pins return same snapshot; lazy registry bootstrap is unique | `publication_kind_unresolved`, `editorial_identity_ambiguous` |
| `POST /resolve` | `{snapshot_id, intent:{block_id,operation}}` → `ResolvedBlockIntent` | Read-only | `editorial_snapshot_stale`, `editorial_block_missing`, `editorial_identity_ambiguous`, `editorial_before_hash_mismatch` |
| `GET /threads/{occurrence_id}` | artifact/version/SHA + `after_sequence`; `If-None-Match` → `ThreadRead`/304 | Read-only; unopened is synthesized, no empty row | `thread_not_found`, `thread_binding_stale`, `occurrence_ambiguous` |
| `POST /threads/{occurrence_id}/entries` | Current: `message` → 202 `SendResult`; `propose` is refused until T-M3-16 | Required opaque `Idempotency-Key`; replay same digest returns existing event/job | `editorial_thread_busy`, `external_llm_blocked`, `bridge_unavailable`, `reconciliation_required`, `idempotency_conflict`, `idempotency_key_invalid`, `proposal_reference_invalid`, `propose_not_enabled` |
| `POST /threads/{occurrence_id}/reconcile` | exact pending entry/turn → typed `ReconciliationResult` | Required opaque `Idempotency-Key`; same turn/result is replay-safe; never resubmits | `thread_not_found`, `reconciliation_required`, `idempotency_key_required`, `idempotency_key_invalid` |
| `GET /threads/{occurrence_id}/proposals/{proposal_id}` | `ProposalView` | Reading may persist `stale` if current pins no longer match | `proposal_not_found`, `proposal_patch_hash_mismatch`, `proposal_patch_unavailable` |
| `POST /threads/{occurrence_id}/proposals/{proposal_id}/reject` | `{expected_state_version, patch_sha256}` → decision view | Required opaque `Idempotency-Key`; exact replay returns prior result | `proposal_not_found`, `proposal_state_conflict`, `proposal_patch_hash_mismatch`, `proposal_base_stale`, `proposal_idempotency_conflict`, `idempotency_key_invalid` |
| `POST /threads/{occurrence_id}/proposals/{proposal_id}/accept` | `{expected_state_version, patch_sha256}` → accepted artifact/snapshot/impact view | Route is wired but disabled by default; returns 404 `accept_not_enabled` while `EDITORIAL_PROPOSAL_ACCEPT_ENABLED=false` | `accept_not_enabled`, `proposal_not_found`, `proposal_state_conflict`, `proposal_patch_hash_mismatch`, `proposal_base_stale`, `proposal_idempotency_conflict` |
| `POST /proposals` | Future M4: manual typed patch → `ProposalView` | Not implemented at this base | Target only: `proposal_target_ambiguous`, `proposal_patch_invalid`, `proposal_evidence_not_admissible`, `proposal_idempotency_conflict` |
| `PUT /drafts/{block_id}` / `DELETE /drafts/{draft_id}` | Future M4: base snapshot + draft text/typed patch → expiring noncanonical draft / 204 | Target only; PUT uses draft version/If-Match; DELETE is idempotent | Target only: `editorial_snapshot_stale`, `editorial_block_missing`, `draft_expired` |
| `GET /figures` / `GET /figures/{figure_id}` | Future M6: filters/detail → `FigureCatalogPage` / `FigureDetail` | Target only, read-only | Target only: `figure_not_found`, `figure_not_eligible` |
| `GET /figures/{figure_id}/thumbnail` | Future M6: no caller-selected blob ID → bounded safe image bytes | Target only; private cache scoped to authorized identity | Target only: `figure_not_found`, `figure_blob_unavailable` |

All FastAPI errors use `{"detail":{"code":"…","message":"…"}}`; messages are server-owned French strings. Codes and messages: `thread_not_found` (« Discussion introuvable. »); `thread_binding_stale` (« Ce paragraphe a changé. Rechargez l’atelier. »); `occurrence_ambiguous` (« L’identité de ce paragraphe historique est ambiguë. »); `editorial_thread_busy` (« Une réponse est déjà en cours pour ce paragraphe. »); `external_llm_blocked` (« La politique interdit l’envoi externe de ce contenu. »); `bridge_unavailable` (« Le Bridge est indisponible. Actualisez avant de continuer. »); `reconciliation_required` (« L’envoi a peut-être déjà été reçu. Réconciliez-le avant de continuer. »); `idempotency_conflict` (« Cette clé est déjà associée à une autre demande. »); `proposal_reference_invalid` (« La proposition citée n’appartient pas à cette discussion. »); `propose_not_enabled` (« La proposition n’est pas encore activée. »); `accept_not_enabled` (« L’acceptation des propositions est désactivée. »); `proposal_not_found` (« Proposition introuvable. »); `proposal_state_conflict` (« Cette proposition a déjà reçu une décision. »); `proposal_patch_hash_mismatch` (« Le contenu vérifié de la proposition ne correspond plus. »); `proposal_base_stale` (« Le document a changé depuis cette proposition. »); `proposal_target_ambiguous` (« La cible est ambiguë. Aucune modification n’a été appliquée. »); `proposal_patch_invalid` (« Cette modification ne respecte pas le format éditorial autorisé. »); `proposal_evidence_not_admissible` (« Les preuves ne sont plus autorisées pour cette modification. »); `proposal_idempotency_conflict` (« Cette clé de proposition est déjà associée à une autre demande. »); `figure_not_eligible` (« Cette image ne peut pas être intégrée. Consultez son motif d’exclusion. »); `figure_blob_unavailable` (« Le fichier archivé de cette image est indisponible. »); `draft_expired` (« Ce brouillon a expiré. Rechargez le document. »).

At this base, only the message objective is accepted; API validation rejects `propose` with `propose_not_enabled`. T-M3-16 will enable `Proposer` only after the proposal aggregate and shared writer effects are ready. The accept route remains disabled and hidden until T-M3-17 has delivered the impact consumer; then set `EDITORIAL_PROPOSAL_ACCEPT_ENABLED=true` and expose the UI action `Accepter`.

### Frontend navigation and review gates

Link **« Ouvrir l’atelier éditorial »** from all five mission surfaces:

1. Subject detail.
2. Article tab publication section.
3. `SubjectProduction` near « Aperçu ».
4. `ReviewItemCard` / `ProductionConsole`.
5. Article and brief consultation screens.

The `/subjects/:id/editor` route precedes subject detail. It authorizes subject and run on entry and every API request; missing and forbidden subjects show a safe route state without leaking subject data. Returning from outline/inspector keeps the selected block.

Frontend implementation uses strict DTO/error unions and the API layer for network access; server state lives in TanStack Query. Use no `any`, no generic hook/component without demonstrated reuse, and keep backend business rules out of presentation components. The server resolves canonical targets; the client sends its returned IDs.

**Playwright visual review:** capture the real app at desktop-large and intermediate sizes, and these mission states: selected paragraph, editing, active conversation, proposal with diff, inserted table, figure gallery, and error/conflict. Review readability, hierarchy, alignment, spacing, overflow, button placement, density, primary action, document space and repeated controls; use synthetic fixtures only. No page horizontal scroll is allowed at widths of 1024 px and above.

```sh
cd frontend && pnpm exec playwright test --config playwright.editorial.config.ts e2e/editorial/visual-review.spec.ts
cd frontend && pnpm test --run
cd frontend && pnpm lint
cd frontend && pnpm typecheck
cd frontend && pnpm build
make test-backend PYTEST_ARGS="tests/editorial -q --tb=short"
make lint-backend
make typecheck-backend
make test-integration
```

## 4. Persistence, migration, backfill and drafts

At integrated base `887445f0`, the editorial migration chain runs through `0015_editorial_proposal_impacts`: `0011_editorial_workspace_snapshots` (`backend/migrations/versions/0011_editorial_workspace.py`) follows `0010_acquisition_provenance` (`backend/migrations/versions/0010_acquisition_provenance.py`); then `backend/migrations/versions/0012_editorial_threads.py` → `0013_bridge_generation_slots.py` → `0014_editorial_proposals.py` → `0015_editorial_proposal_impacts.py`. Never edit `0001_baseline`. Future DDL starts at 0016 and follows the integrated Alembic head, ADR-0007 preflight behavior, and D8/D8b: validate pre-existing objects; accept absent or exact canonical objects; fail before DDL on partial/nonconforming state. This document task did not upgrade or downgrade a database.

The integrated schema adds workspace snapshots in 0011; thread, event and job tables plus the editorial conversation purpose/objective extension in 0012; the editorial-only Bridge generation slot in 0013; proposal, model-run and event tables in 0014; and the impact outbox in 0015. Draft tables remain a future 0016+ target.

| Additive object | Required constraints and purpose |
|---|---|
| `editorial_workspace_snapshots` — integrated 0011 | UUID PK; subject/run and pinned artifact FKs `RESTRICT`; pinned versions/hashes and explicit `publication_kind`; registry/retired IDs JSONB; parent snapshot; unique `(production_run_id, workspace_pin_hash)`; append-only guards. Registry contains no prose/evidence |
| `editorial_threads`, `editorial_thread_events`, `editorial_thread_jobs` — integrated 0012 | `session_number` is unique per subject and occurrence; origin subject/run/occurrence/session and artifact/version/SHA are immutable; events are append-only and sequenced; jobs are keyed by send event with one active job per thread |
| Existing conversation rows — integrated 0012 | Add nullable `objective` (`editorial_message|editorial_proposal`) to `model_conversation_turns`; extend the closed `model_conversations.purpose` check with `EDITORIAL_DISCUSSION`. Old turns remain null |
| `bridge_generation_slots` — integrated 0013 | Singleton row with holder references to editorial thread/job/turn, fencing token and state; serializes editorial Bridge generations only. It does not gate other Bridge consumers |
| `editorial_proposals` — integrated 0014 | FKs to subject/run/thread/base artifact with `RESTRICT`; closed origin/operation/state checks; base fences, target ID, patch blob ref/SHA, evidence/diagnostics, state version, `variant_group_id`, actor and decision refs; unique `(subject_id, actor_id, idempotency_key)` plus request digest; at most one accepted artifact per proposal |
| `editorial_proposal_model_runs`, `editorial_proposal_events` — integrated 0014 | Exact ModelRun/turn refs, unique run refs; append-only ordered state/decision events and unique proposal/idempotency key; no model-run row for manual origin |
| `editorial_proposal_impacts` — integrated 0015 | Transactional outbox unique by accepted proposal; monotone sequence per run; mutable retry/status, not timeline truth; idempotent worker |
| `editorial_drafts` — future 0016+ target | Subject/run/actor/block/base snapshot, blob ref/SHA, draft version, `created_at`, `updated_at`, mandatory `expires_at`; subject ACL; never canonical evidence |

Keep snapshots, proposals and timeline events append-only. Store message bodies and patches in immutable MinIO blobs with SHA-256 references; Postgres rows carry typed identity/state. Keep outboxes mutable and replayable. There is no `editorial_thread_proposals` table: thread events point to `editorial_proposals`. The 0013 lease serializes editorial Bridge generations only; it does not constrain other Bridge consumers.

For adoption, upload candidate canonical bytes and patch bytes to MinIO and read them back to verify SHA-256 **before** opening the PostgreSQL UOW. The UOW contains only relational CAS, references, proposal decision, lineage, registry advance, accepted event and impact outbox. A pre-UOW blob orphan is harmless because it is content-addressed; an idempotent reconciler finds unreferenced objects/catalog rows and cleans them. Verify these crash boundaries: **before upload** (no write), **after upload/before UOW** (orphan only, safe retry/reconcile), and **after commit/before impact** (accepted state and outbox remain replayable).

Impact workers serialize per run: acquire the run lock and re-read current pins before producing. If an impact's base pins have been surpassed, mark it `superseded` as a no-op; the newer impact rebuilds from the latest state. A pending impact never blocks a later adoption. Never apply an older impact after a newer run state.

Bootstrap legacy pins lazily on first snapshot read under the production-run lock. Deterministic UUIDv5 seeds `(artifact_id, canonical_sha256, kind, structural_pointer)` produce distinct duplicate IDs. Link later versions only through existing revision lineage or a unique exact match; ambiguous duplicate/reordered/rewrite mappings stay read-only `needs_review`. `advance` runs in the adoption UOW, in legacy direct synthesis/enrichment write UOWs through `EditorialRevisionEffects`, and during run-guarded impact rebase, with explicit or legacy-resolved `rewrite|move|insert|delete` lineage. Proposal adoption creates the impact outbox row; legacy direct writes do not create synthetic adoption impacts. Insert gets a new ID, rewrite/move preserve it, delete retires it, and split/merge require explicit mapping. Never rewrite old blobs, fabricate missing proposal patches, or backfill conversations as editorial threads.

**Prose and retention contract:** drafts, thread bodies and turns can contain CTI-sensitive user prose. Stored editorial records use the subject ACL; the table defines copy-specific retention. Never copy prose or block text into logs, traces or error telemetry; keep identifiers and hashes only. Browser prose exists in React memory only, never localStorage or sessionStorage.

| Copy | Access and retention |
|---|---|
| Server draft blobs/rows | Subject ACL; configurable idle TTL, default 30 days since last modification; purge at adoption/rejection/cancellation and expiry; no persistence without configured policy |
| Thread event/body blobs | Subject ACL; retained as the editorial record; no automatic purge |
| `ModelConversation` turns | Subject ACL; no additional retention policy is introduced by this contract |
| Logs, traces, error telemetry | No user prose or block text; identifiers and hashes only |
| Browser draft/input | React memory only; no localStorage/sessionStorage persistence |

## 5. Data flow

1. **Click paragraph.** Client reads the snapshot, selects its `block_id`, calls `/resolve`, and opens the corresponding thread query. Server checks exact pins and registry lineage, then returns evidence refs, operations and current discussion state. The thread GET can return `unopened` without creating a row.
2. **Send message.** API appends a user event and durable job with the client idempotency key, returns 202, then a worker rebuilds minimum authorized context and rechecks G2/G3, TLP, revocations and `external_llm_allowed`. The exact private conversation/turn/ModelRun is persisted before transport. A reply becomes a non-evidence event; it cannot create a proposal.
3. **Propose (after T-M3-16).** At this base, M2 accepts `message` only; API refuses `propose`. After the D2 aggregate and shared run/registry/stale effects are ready for all legacy writers, T-M3-16 may enable `propose`. It then submits a strict objective and explicit same-thread proposal refs. Worker reconciles uncertain transport against the exact ModelRun, validates `EditorialPatchV1 | EditorialRefusalV1`, stores immutable patch bytes/SHA and transitions the D2 aggregate. No canonical artifact changes until human decision.
4. **Accept (T-M3-17 gate).** The route is wired, but configuration defaults `EDITORIAL_PROPOSAL_ACCEPT_ENABLED` to `false` and it returns 404 `accept_not_enabled`. Keep it disabled and hidden until the T-M3-17 impact consumer is delivered; then enable the flag and add UI action `Accepter`. Adoption locks proposal then run; upload and re-read candidate blobs by SHA before opening the relational UOW. The UOW CAS-appends canonical version, references, lineage, workspace `advance`, accepted event/state and an impact outbox with the next per-run sequence. No blob-store operation occurs in that UOW. A run-locked worker re-reads current pins: if superseded, it records a no-op; otherwise it rebuilds impact from current state, advances registry on rebase, and updates derived versions. Pending impacts never block adoption. No model call, rollback of accepted content, or automatic edition repin. Reject appends a decision only.
5. **Manual edit.** After the writer cutover, UI drafts remain per-block React memory and may be saved through `/drafts`; saving text is not canonical. Submit the user's typed patch as `ManualPatch` with zero provider calls. It uses the same validation, patch blob, explicit decision, CAS and impact path as Bridge output.
6. **Table or chart insertion → publication → Typst.** Select paragraph, send an insertion patch with `after_paragraph(anchor_block_id)`, preview typed cells/values/proofs, then adopt. Assembly resolves the anchor against the pinned registry, emits ordered publication data, and the deterministic Typst render-data path places it after that paragraph. A failed resolution leaves enrichment stale and preview `STALE`; there is no UI-only placement. Bar charts compile locally from documented numeric values.
7. **Figure integration.** Gallery queries accepted/rejected/pending archived inventory; authorized detail/thumbnail routes resolve candidate IDs server-side. Selection/caption/order/anchor forms a typed patch and makes no model call. Adoption validates inventory decision and original blob SHA/MIME/size/dimensions, then the same enrichment→publication→Typst flow serves a local archived asset and provenance. BRIEF enforces three integrated figures.

### Legacy writers and shared effects

The implemented cutover keeps legacy synthesis/enrichment `revise` endpoints as direct canonical writers. Within the same UOW they call `EditorialRevisionEffects`, shared with adoption: acquire the run guard, lock the run row, advance the workspace registry, and persist stale transitions for proposals whose pins changed. Restore and impact rebase use the same effects; production repair is wired through `application/production_repair_factory.py`. Legacy `revise` is not a prepare+accept facade: a synthetic proposal or ModelRun would misstate the origin. The shared guard/effects provide the common concurrency and registry guarantees; proposal adoption alone writes the accepted proposal and its impact outbox. Production Inputs recheck the subject’s current TLP plus current evidence and thread bindings. `publication_text_correction` and publication augmentation remain outside synthesis/enrichment writing, covered by zero-write oracles.

## 6. Module and test seams

| Layer/seam | Test without | Verify |
|---|---|---|
| Domain: registry reconciliation, `EditorialProposal` transitions, patch union and placement invariants | React, provider, Postgres | duplicate IDs, immutable terminal states, hashes, stale anchors, unsupported/free-form patch rejection |
| Application: workspace/proposal/thread services with in-memory artifact, registry, proposal, thread, blob, policy and outbox adapters | React, provider, Postgres | snapshot idempotence; no partial ambiguous bootstrap; exact patch rehash; CAS race; same-key replay; no gateway on accept/manual; no blind resubmit; impact retry |
| API DTO/error mapping with dependency overrides | React, provider, Postgres | closed unions, auth-to-subject scoping, stable codes/French messages, idempotency header handling, 202/304/409 shapes |
| PostgreSQL adapters and Alembic migration | React and providers (Postgres required) | FK/unique/check/append-only guarantees, partial active-job uniqueness, lock ordering, transaction rollback, lease fencing |
| Frontend API/error mapper and pure selectors | backend, provider, Postgres, mounted React | schema-version handling, query keys, block-to-server-ID mapping, French messages and cache invalidation |
| Frontend components and browser journeys | provider and Postgres with mocked API (React required) | context retention, compare/diff, desktop keyboard/focus, pending/error/conflict/gallery states |
| Publication projection/render-data builder | React, provider, Postgres | same-anchor deterministic order, stale anchor, exact artifact pins and V4–V9 parsing |
| Real Typst/PDF integration | provider and Postgres if render fixtures are local (Typst required) | output structure, figure provenance/caption and placement; visual review remains a separate human gate |

No tests were run for this document task.

## 7. Compatibility matrix

| Kind and source version | Read/display | Editing and enrichment contract |
|---|---|---|
| ARTICLE or BRIEF, publication V4–V6 | Preserve existing readers and exact canonical bytes. Resolve kind only from the pinned routing decision or ADR-0008 legacy resolver. | If kind remains unknown, workspace is read-only. Keep synthesis schema version 2 and artifact version pins; sidecar registry supplies occurrence IDs. Existing enrichment V8 and placements remain readable. |
| ARTICLE or BRIEF, publication V7 | V7 wraps exact V6 and carries explicit `publication_kind`, routing decision hash and policy. | Kind is authoritative. ARTICLE follows synthesis→enrichment→assembly. BRIEF behavior follows ADR-0009; figures remain archived and capped at three. |
| ARTICLE or BRIEF, publication V8 | V8 wraps V7; ARTICLE requires `article_format`, BRIEF requires `article_format=null`. | Keep V4–V8 readers. V9 adds the new typed chart/anchor projection without embedding block IDs in synthesis. ARTICLE/BRIEF both use the shared workspace; BRIEF enrichment may be absent and then means empty. |
| New publication V9 | Explicit kind plus typed after-paragraph placement and BarChart projection; exact source pins remain immutable. | Bump enrichment schema/policy/prompt/parser and publication/render policy where wire/render behavior changes. Preserve old readers and stored V4–V8 blobs. |

Current synthesis is `ProductionSynthesisV1` with `schema_version=2` and no occurrence ID field (`domain/production_synthesis.py:24,607-625`). Identity alone does not change that schema; accepted edits append the next artifact version. Current enrichment is schema V8 and placement is section-based; bar charts are absent (`domain/production_editorial_enrichment.py:30,108-127,141-265`). Publication V7 wraps V6 with explicit kind and V8 wraps V7 with article format (`domain/publication_document.py:2276-2306,3184-3207`). ADR-0009 keeps BRIEF's enrichment optional, uses no external web search, and caps archived source figures at three.

Keep old GET artifact, preview, revision-lineage and POST revision routes as compatible adapters. New editor clients use snapshot/proposal/thread APIs. Existing explicit POST `revise` remains a direct canonical write and invokes `EditorialRevisionEffects` in the same UOW; do not represent it as prepare+accept or fabricate proposal/ModelRun provenance. Preview stays read-only. Never make Bridge output auto-accept. `/content` gets an additive top-level nullable `publication_kind` so V7/V8 callers do not infer it from an inner V5 payload. Edition compositions keep exact artifact/run/generation pins; publication changes make pins stale, and only an explicit operator update repins them (`application/edition_composition.py:1011-1053`; repository latest-pin lookup `infrastructure/database/repositories/edition_composition.py:231-250`).

## 8. Expand–contract rollout

1. M1 is integrated at the stated base: migration 0011, snapshot/resolve and the read-only workspace route. Keep old artifact pages and readers live; ambiguous history remains visibly read-only.
2. M2 is integrated at the stated base: migration 0012, occurrence threads/events/jobs, typed conversation purpose/objective and migration 0013's editorial-only Bridge capacity slot. `message` is enabled; `propose` stays refused/hidden until T-M3-16.
3. M3 through T-M3-15d is integrated: 0014 proposals, 0015 impacts, no-model candidate cores and the shared revision-effects step are present. Legacy revise endpoints remain direct writers using the same run guard, registry advance and stale-proposal handling; the accept route is wired but disabled by default. This is the documented D7.B1 deviation, not a prepare+accept facade.
4. T-M3-16 enables only `Proposer`; accept remains disabled and hidden. T-M3-17 adds the run-locked impact consumer, then and only then enables accept with `EDITORIAL_PROPOSAL_ACCEPT_ENABLED=true` and exposes UI `Accepter`.
5. Add M4 server/local drafts and manual editing. Then add V9 enrichment/publication placement and chart contracts before exposing table/chart UI. Add gallery/thumbnail only after server authorization and image-safety review.
6. Integrate ARTICLE/BRIEF, PDF and composition journeys; monitor stale/ambiguous/bootstrap and pending-impact outcomes. Keep V4–V8 readers and composition pins exact. Retain revision endpoints as compatible facades; any later contraction requires a separate reviewed change.

The intended product order remains M1 snapshot/identity/kind → M2 threads/Bridge → M3 proposals/adoption → M4 manual editing/drafts → M5 table/chart/placement → M6 gallery → M7 ARTICLE/BRIEF/PDF integration → M8 architecture and visual review.

## 9. Security and provenance checklist

- Resolve actor, subject, run and artifact permissions server-side on every read/write; never accept a client-supplied context, evidence pack, provider, policy decision or blob key.
- Recheck G2/G3, TLP, provider routing, revocations and `external_llm_allowed` before every external generation. Treat TLP RED or denied external policy as a hard block. Apply `do_not_submit` only to actions whose contract uses it; it does not veto Bridge/advisor calls by itself.
- Send only minimum authorized block/context/evidence to Bridge. Label analyst text and model output as untrusted discussion. Neither enters canonical evidence or `primary_evidence`; every factual patch claim must validate against admitted evidence refs.
- Store canonical artifacts and accepted patches as immutable versioned blobs with SHA-256 refs; PostgreSQL owns pins, CAS, idempotency, lineage and decisions. Keep audit events free of source prose and credentials.
- Never put provider/service keys, tokens, passwords or signed URLs in prompts, browser state, traces, errors or logs. Keep user prose in React memory or authorized subject records only; never persist it in localStorage/sessionStorage. Server drafts follow the configured TTL and purge contract above.
- Figure list/detail/thumbnail routes reauthorize subject, run and figure on every request. Resolve only accepted archived inventory entries; never accept arbitrary IDs/URLs or expose MinIO keys. Generate bounded safe thumbnails server-side, sanitize or rasterize SVG, set `nosniff`, and serve private cache headers. The existing asset route only serves media already referenced by the current publication (`api/subject_content.py:482-531`); it is not the candidate gallery.
- Human acceptance is mandatory for canonical changes. Impact workers may rebase/rebuild only from the accepted pins; they never publish/release or repin an edition composition.

## 10. Open points and risks

| Point | Recommended resolution |
|---|---|
| Scope of the Bridge generation lease | Migration `0013_bridge_generation_slots.py` and `application/editorial_bridge_gate.py` scope the singleton holder to editorial thread/job/turn records. It serializes editorial Bridge generations; it does not throttle other Bridge consumers. This is an AutoWork editorial gate, not proof of a Bridge-side global rule. Never release the fenced slot while external outcome is unknown; reconcile the exact ModelRun first. |
| Legacy duplicate/reordered blocks | Leave ambiguous blocks read-only and require an explicit reviewed lineage mapping. Do not guess by ordinal, similarity or model. |
| Kind absent in legacy publication | Use only persisted routing/ADR-0008 resolver for V4–V6; block editing if unresolved. Remove the current ARTICLE fallback from the new workspace path. |
| Draft lifetime | Use the configurable 30-day-since-last-change default and defined purge triggers; fail closed for server persistence without configured retention. |
| Thumbnail safety and load | Bound decoded bytes, dimensions, pixel count and output size; rasterize SVG in a restricted local path; authorize every request and measure cache behavior without public URLs. |
| Anchor loss after edit | Keep the enrichment stale and preview visibly `STALE`; require a new anchor choice or explicit removal. Preserve original bytes and diagnostics. |
| Large registry JSONB | Keep the first implementation run-scoped and document-sized; validate parent closure/order in the adapter. Normalize behind the same interface only if measured registry size/query cost warrants it. |
| Outbox crash windows | Make accept state and impact row one transaction; make impact workers idempotent with bounded retry and visible `pending`/`failed` status. Composition pins stay unchanged. |

## Verified implementation at the documented base

- Thread `session_number` is unique per subject/occurrence, and origin fields are guarded immutable (`backend/migrations/versions/0012_editorial_threads.py`).
- GET, entries and reconciliation are routed by `occurrence_id`; HTTP errors serialize as `{detail:{code,message}}`; opaque idempotency keys must match `^[A-Za-z0-9_-]{16,128}$`, with invalid values reported as `idempotency_key_invalid` (`backend/src/cti_app/api/editorial_workspace.py`, `application/editorial_thread_common.py`).
- Proposal GET is routed by `occurrence_id` and persists `stale` when current pins differ. The accept route is wired but the `EDITORIAL_PROPOSAL_ACCEPT_ENABLED` setting defaults to false and returns 404 `accept_not_enabled` (`api/editorial_workspace.py`, `config.py`).
- `session_lost` is returned as a typed reconciliation outcome and persisted in a generic `reconciliation` event; no dedicated schema object is needed for that result (`domain/editorial_thread.py`, `application/editorial_thread_job_completion.py`).
- `EditorialRevisionEffects` is used by proposal adoption and legacy synthesis/enrichment revise, restore and rebase paths; production repair is wired by `production_repair_factory.py`. `ProductionEditorialProposalAdoptionInputs` rechecks current subject TLP, evidence and thread bindings.
- The integrated migration chain is 0011 workspace snapshots → 0012 threads/conversation fields → 0013 editorial-only Bridge generation slot → 0014 proposals → 0015 impacts.
