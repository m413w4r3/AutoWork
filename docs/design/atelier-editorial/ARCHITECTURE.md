# Editorial workspace architecture

**Status:** proposed target for Mission 22. [`DECISIONS.md`](../../../.autowork-runs/m22/DECISIONS.md) D1–D7 is authoritative until the orchestrator accepts proposed [ADR-0010](../../adr/ADR-0010-editorial-workshop-contracts.md) in M8 after implementation and reviews. This document maps those decisions to current code and specifies contracts, seams and rollout. If the documents diverge, stop and report it. Facts below were checked at base `067f1d9c33084cc370c59e6b51e885e668dfdfab`; proposed modules are not implemented.

## 1. Module map

Current size is measured in lines at the stated base. New paths have no measured size.

| Layer | Before | After (target) |
|---|---|---|
| Backend API | `api/production.py` (4,199): artifact/revision DTOs; `api/subject_content.py`: `/content`, PDF and selected-asset reads | `api/editorial_workspace.py` owns workspace/thread/proposal/draft/gallery DTO mapping; keep current routes as facades |
| Backend application | `production_synthesis_revision.py` (3,961) and `production_enrichment_revision.py` (3,092): target, model, validation, direct write, lineage and impact; `production_editorial_enrichment.py` (8,629): generation/projection | `application/editorial_workspace/` owns snapshot/resolve/advance; `editorial_proposals.py` is the proposal facade over target resolution, patch validation, preparation, sole adoption, lineage, impact and projection; `editorial_threads.py` owns thread ordering/context and queues jobs |
| Backend domain | `domain/production_synthesis.py` (current schema version 2; model `ProductionSynthesisV1`); `domain/production_editorial_enrichment.py` (1,961; schema V8); `domain/publication_document.py` (3,771; V4–V8) | Add pure `editorial_proposal.py`, `editorial_thread.py` and typed editorial patch contracts. Keep canonical synthesis blocks free of occurrence IDs; V9 enrichment/publication contracts carry typed placement/chart additions |
| Backend persistence | Production artifacts are immutable version rows over canonical blob refs; model conversations/turns are generic transport state | Add ORM/repository/UOW adapters for snapshots, proposals, threads, drafts and outboxes; keep blobs and SHA-256 canonical, PostgreSQL the state authority |
| Frontend routing/API | `App.tsx` (52); `api/production.ts` (2,183); no editorial-workspace feature | Add `/subjects/:id/editor` before the subject-detail matcher and `api/editorialWorkspace.ts` with closed DTO/error unions and feature-owned TanStack keys |
| Frontend view | `components/ProductionArtifactView.tsx` (2,361): historical artifact read/preview and enrichment display | Add `features/editorial-workspace/` for page/layout/outline/document/block toolbar/inspector/thread/proposal diff/manual edit/table composer/figure gallery/evidence/history/status. Keep the old view as a read adapter; extract real seams instead of moving its full body |

Code anchors: hash-derived target IDs and duplicate refusal are in `application/production_synthesis_revision.py:270-319,357-425`; current direct artifact write is `:2881-2959`. Current routes are `api/production.py:3153-3216,3565-3726`; the subject `/content` response has no top-level `publication_kind` (`api/subject_content.py:72-90,176-181`). Frontend route matching is `frontend/src/App.tsx:12-39`.

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

A thread is addressed by `(subject_id, occurrence_id)`. Its origin artifact/version/canonical SHA never changes; its current reference advances only along proven registry lineage. Missing or ambiguous lineage makes the thread stale and blocks proposal adoption. Each thread owns a private `ModelConversation` with purpose `EDITORIAL_DISCUSSION`; its turns and ModelRuns are not the UI timeline.

The append-only event union is `user_message | bridge_reply | proposal_ref | refusal | proposal_decision | reconciliation`. Events carry sequence, actor, idempotency key/digest and typed refs; bodies live in immutable blobs with SHA-256. Proposal events reference D2 `proposal_id`; no thread-owned proposal payload/table duplicates D2. M2 is message-only: `message` can produce discussion text but never a proposal. API and UI refuse/hide `propose` until D2 and the unique adoption path are ready and the cutover gate passes; then `propose` accepts only `EditorialPatchV1 | EditorialRefusalV1`. All messages and model outputs remain non-evidence.

Bridge state is a projection: `ready | waiting_for_capacity | generating | reply_available | refused | session_lost | reconciliation_required`. A durable thread job/outbox permits one active job per thread. After possible external submission, inspect/reconcile the exact ModelRun/turn; never retry by resubmitting. A lost session starts a new thread, never a reconstructed browser URL. Re-evaluate authorization and evidence admission on every generation.

### Typed patch union, charts, figures and placement

`EditorialPatchV1` is a closed discriminated union: `rewrite_block`, `insert_block`, `delete_block`, `patch_enrichment_element`, `insert_table(TableSpecV1)`, `insert_chart(TimelineChartSpecV1 | BarChartSpecV1)`, `insert_archived_figures(FigureSelectionPatchV1)`. Each variant names the stable target/parent and base fence. Evidence references are typed. It has no free HTML, SVG, Typst, JavaScript, Mermaid, URLs or client-selected blob keys. Manual and Bridge patches pass through the same validator/adoption writer.

`BarChartSpecV1` is semantic data only: key/title/caption, category axis label, numeric axis label and unit, an ordered list of `{category, finite_decimal_value, evidence_refs}`, analytic purpose and placement. Each numeric datum needs admissible evidence; reject missing, contradictory or invented values. Compile deterministically through the existing local chart/media path; pin compiler/policy versions and SHA. Never accept model-supplied SVG. Current charts are timeline-only; current table rows already carry evidence refs and width/column constraints (`domain/production_editorial_enrichment.py:141-265,296-335`).

`FigureSelectionPatchV1` contains an ordered list of server-resolvable accepted inventory figure IDs, optional editorial captions, and the selected `anchor_block_id`. Re-resolve every figure against the exact subject/run inventory and verify archived blob ID/SHA, MIME, byte size and dimensions. No model call. For BRIEF, retain the three-figure cap; it is distinct from the 128-candidate inventory limit (`application/source_figure_inventory.py:51-60,65-102,992-1057`).

Placement is a strict tagged union: existing `after_lead`, `after_section(section_index)`, `after_timeline`, `end`, plus `after_paragraph(anchor_block_id)`; exactly one payload is valid for each tag. Preserve legacy placements/readers. Resolve the paragraph against the exact pinned workspace registry during assembly. A moved occurrence carries its ID; a deleted/ambiguous anchor becomes stale and makes preview `STALE`. Never fall back to section/document end. Elements sharing an anchor follow canonical placement position then `element_key`. Publication V9 and ordered Typst render data record enough resolved anchor/pin data to reproduce the same location in the PDF. Current placement only has section indexes (`domain/production_editorial_enrichment.py:108-127`; render projection `application/typst_rendering.py:2539-2654`).

## 3. HTTP API

Base path: `/api/subjects/{subject_id}/editor`. Authenticated actor and subject/run authorization come from the server session; React never supplies policy, evidence context, provider, or ModelRun IDs.

| Method and path | Request → response | Idempotency | Stable errors |
|---|---|---|---|
| `GET /` | optional `artifact_id` → `EditorialWorkspaceSnapshot` | Same exact pins return same snapshot; lazy registry bootstrap is unique | `publication_kind_unresolved`, `editorial_identity_ambiguous` |
| `POST /resolve` | `{snapshot_id, intent:{block_id,operation}}` → `ResolvedBlockIntent` | Read-only | `editorial_snapshot_stale`, `editorial_block_missing`, `editorial_identity_ambiguous`, `editorial_before_hash_mismatch` |
| `GET /threads/{occurrence_id}` | artifact/version/SHA + `after_sequence`; `If-None-Match` → `ThreadRead`/304 | Read-only; unopened is synthesized, no empty row | `thread_not_found`, `thread_binding_stale`, `occurrence_ambiguous` |
| `POST /threads/{occurrence_id}/entries` | M2: `EditorialSend(message)` → 202 `SendResult`; `propose` is refused until the M3 activation gate | Required `Idempotency-Key`; replay same digest returns existing event/job | `editorial_thread_busy`, `external_llm_blocked`, `bridge_unavailable`, `reconciliation_required`, `idempotency_conflict`, `proposal_reference_invalid`, `propose_not_enabled` |
| `POST /threads/{thread_id}/reconcile` | exact pending entry/turn → `ReconciliationResult` | Same turn/result is replay-safe; never resubmits | `thread_not_found`, `reconciliation_required` |
| `POST /proposals` | M3 after D2: `PrepareManualProposal(base, block_id, typed_patch)` → `ProposalView`; decision route remains disabled until writer cutover | Required `Idempotency-Key` | `proposal_target_ambiguous`, `proposal_patch_invalid`, `proposal_evidence_not_admissible`, `proposal_idempotency_conflict` |
| `GET /proposals/{proposal_id}` | optional `thread_id` → `ProposalView` | Read-only | `proposal_not_found` |
| `POST /proposals/{proposal_id}/decision` | `{decision, expected_state_version, patch_sha256}` → `DecisionResult` | Required `Idempotency-Key`; exact replay returns prior result | `proposal_not_found`, `proposal_state_conflict`, `proposal_patch_hash_mismatch`, `proposal_base_stale`, `proposal_idempotency_conflict` |
| `PUT /drafts/{block_id}` / `DELETE /drafts/{draft_id}` | base snapshot + draft text/typed patch → expiring noncanonical draft / 204 | PUT uses draft version/If-Match; DELETE is idempotent | `editorial_snapshot_stale`, `editorial_block_missing`, `draft_expired` |
| `GET /figures` / `GET /figures/{figure_id}` | filters/detail → `FigureCatalogPage` / `FigureDetail` | Read-only | `figure_not_found`, `figure_not_eligible` |
| `GET /figures/{figure_id}/thumbnail` | no caller-selected blob ID → bounded safe image bytes | Private cache scoped to authorized identity | `figure_not_found`, `figure_blob_unavailable` |

The error envelope is `{error:{code,message}}`; messages are server-owned French strings. Codes and messages: `thread_not_found` (« Discussion introuvable. »); `thread_binding_stale` (« Ce paragraphe a changé. Rechargez l’atelier. »); `occurrence_ambiguous` (« L’identité de ce paragraphe historique est ambiguë. »); `editorial_thread_busy` (« Une réponse est déjà en cours pour ce paragraphe. »); `external_llm_blocked` (« La politique interdit l’envoi externe de ce contenu. »); `bridge_unavailable` (« Le Bridge est indisponible. Actualisez avant de continuer. »); `reconciliation_required` (« L’envoi a peut-être déjà été reçu. Réconciliez-le avant de continuer. »); `idempotency_conflict` (« Cette clé est déjà associée à une autre demande. »); `proposal_reference_invalid` (« La proposition citée n’appartient pas à cette discussion. »); `propose_not_enabled` (« La proposition n’est pas encore activée. »); `proposal_not_found` (« Proposition introuvable. »); `proposal_state_conflict` (« Cette proposition a déjà reçu une décision. »); `proposal_patch_hash_mismatch` (« Le contenu vérifié de la proposition ne correspond plus. »); `proposal_base_stale` (« Le document a changé depuis cette proposition. »); `proposal_target_ambiguous` (« La cible est ambiguë. Aucune modification n’a été appliquée. »); `proposal_patch_invalid` (« Cette modification ne respecte pas le format éditorial autorisé. »); `proposal_evidence_not_admissible` (« Les preuves ne sont plus autorisées pour cette modification. »); `proposal_idempotency_conflict` (« Cette clé de proposition est déjà associée à une autre demande. »); `figure_not_eligible` (« Cette image ne peut pas être intégrée. Consultez son motif d’exclusion. »); `figure_blob_unavailable` (« Le fichier archivé de cette image est indisponible. »); `draft_expired` (« Ce brouillon a expiré. Rechargez le document. »).

M2 has only the message objective. Before M3 activation, API validation rejects `propose` with `propose_not_enabled`, and the UI hides it. Enable both only after the D2 proposal aggregate, unique adoption and writer cutover are ready.

### Frontend navigation and review gates

Link **« Ouvrir l’atelier éditorial »** from all five mission surfaces:

1. Subject detail.
2. Article tab publication section.
3. `SubjectProduction` near « Aperçu ».
4. `ReviewItemCard` / `ProductionConsole`.
5. Article and brief consultation screens.

The `/subjects/:id/editor` route precedes subject detail. It authorizes subject and run on entry and every API request; missing and forbidden subjects show a safe route state without leaking subject data. Returning from outline/inspector keeps the selected block.

Frontend implementation uses strict DTO/error unions and the API layer for network access; server state lives in TanStack Query. Use no `any`, no generic hook/component without demonstrated reuse, and keep backend business rules out of presentation components. The server resolves canonical targets; the client sends its returned IDs.

**Playwright visual review:** capture the real app at desktop-large, intermediate and mobile sizes, and these mission states: selected paragraph, editing, active conversation, proposal with diff, inserted table, figure gallery, and error/conflict. Review readability, hierarchy, alignment, spacing, overflow, button placement, density, primary action, document space and repeated controls; use synthetic fixtures only.

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

The checked Alembic head is `0010_acquisition_provenance`, after `0009_acquisition_strategy_attempts` (`backend/migrations/versions/0010_acquisition_provenance.py:1-17`). Add one revision, `0011_editorial_workspace`, after that head; never edit `0001_baseline`. Follow ADR-0007 preflight behavior: validate all pre-existing names, columns, constraints, indexes, FKs and triggers before DDL; accept only absent or exact canonical objects; partial/nonconforming state fails before changes, not `IF NOT EXISTS`. Do not upgrade/downgrade a live database as part of this design. Downgrade refuses while editorial history exists.

| Additive object | Required constraints and purpose |
|---|---|
| `editorial_workspace_snapshots` | UUID PK; subject/run and pinned artifact FKs `RESTRICT`; versions, generation, input/canonical/routing/pin/registry SHA-256 checks; explicit `publication_kind`; registry/retired IDs JSONB; parent snapshot; unique `(production_run_id, workspace_pin_hash)`; append-only trigger. Registry validates unique IDs, parent closure, positions and tombstones in application; contains no prose/evidence |
| `editorial_proposals` | FKs to subject/run/thread/base artifact with `RESTRICT`; closed origin/operation/state checks; base fences, target ID, patch blob ref/SHA, evidence/diagnostics, state version, `variant_group_id`, actor and decision refs; unique `(subject_id, actor_id, idempotency_key)` plus request digest; at most one accepted artifact per proposal |
| `editorial_proposal_model_runs`, `editorial_proposal_events` | Exact ModelRun/turn refs, unique run refs; append-only ordered state/decision events and unique proposal/idempotency key; no model-run row for manual origin |
| `editorial_proposal_impacts` | Transactional outbox unique by accepted proposal; monotone sequence per run; mutable lease/retry/status (`pending|running|done|superseded|failed`), not timeline truth; idempotent worker |
| `editorial_threads`, `editorial_thread_events`, `editorial_thread_jobs` | Immutable origin pins; thread version/sequence CAS; events unique `(thread_id,sequence)` and `(thread_id,idempotency_key)`, append-only, bodies by blob ref/SHA; jobs keyed by send event and partial unique active job per thread |
| `editorial_drafts` | Subject/run/actor/block/base snapshot, blob ref/SHA, draft version, `created_at`, `updated_at`, mandatory `expires_at`; subject ACL; never canonical evidence |
| `bridge_generation_slots` | Singleton row with current turn, opaque lease/fencing token and state; acquired before external submission and released only after verified pre-submit failure or exact-turn reconciliation |
| Existing conversation rows | Add nullable `objective` (`editorial_message|editorial_proposal`) to `model_conversation_turns`; extend closed `model_conversations.purpose` check with `EDITORIAL_DISCUSSION`. Old turns remain null; application requires an objective for the new purpose |

Keep snapshots, proposals and timeline events append-only. Store message bodies and patches in immutable MinIO blobs with SHA-256 references; Postgres rows carry typed identity/state. Keep outboxes mutable and replayable. There is no `editorial_thread_proposals` table: thread events point to `editorial_proposals`. The global lease is needed unless a Bridge-side shared gate is verified; the currently inspected app code only locks a conversation/relay process.

For adoption, upload candidate canonical bytes and patch bytes to MinIO and read them back to verify SHA-256 **before** opening the PostgreSQL UOW. The UOW contains only relational CAS, references, proposal decision, lineage, registry advance, accepted event and impact outbox. A pre-UOW blob orphan is harmless because it is content-addressed; an idempotent reconciler finds unreferenced objects/catalog rows and cleans them. Verify these crash boundaries: **before upload** (no write), **after upload/before UOW** (orphan only, safe retry/reconcile), and **after commit/before impact** (accepted state and outbox remain replayable).

Impact workers serialize per run: acquire the run lock and re-read current pins before producing. If an impact's base pins have been surpassed, mark it `superseded` as a no-op; the newer impact rebuilds from the latest state. A pending impact never blocks a later adoption. Never apply an older impact after a newer run state.

Bootstrap legacy pins lazily on first snapshot read under the production-run lock. Deterministic UUIDv5 seeds `(artifact_id, canonical_sha256, kind, structural_pointer)` produce distinct duplicate IDs. Link later versions only through existing revision lineage or a unique exact match; ambiguous duplicate/reordered/rewrite mappings stay read-only `needs_review`. `advance` runs in the accepted-artifact UOW or during impact rebase under the same run guard, with explicit `rewrite|move|insert|delete` lineage and matching outbox; insert gets a new ID, rewrite/move preserve it, delete retires it, and split/merge require explicit mapping. Never rewrite old blobs, fabricate missing proposal patches, or backfill conversations as editorial threads.

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
3. **Propose (after the M3 gate).** M2 accepts `message` only; API and UI refuse/hide `propose`. After the D2 aggregate, adoption path and all-writer cutover are ready, M3 may enable `propose`. It then submits a strict objective and explicit same-thread proposal refs. Worker reconciles uncertain transport against the exact ModelRun, validates `EditorialPatchV1 | EditorialRefusalV1`, stores immutable patch bytes/SHA and transitions the D2 aggregate. No canonical artifact changes until human decision.
4. **Accept (after writer cutover).** Before the accept route is enabled, inventory every synthesis/enrichment writer and cut all of them over. Then lock proposal followed by run; upload and re-read candidate blobs by SHA before opening the relational UOW. The UOW CAS-appends canonical synthesis/enrichment version, references, lineage, workspace `advance`, accepted event/state and an impact outbox with the next per-run sequence. No blob-store operation occurs in that UOW. A run-locked worker re-reads current pins: if superseded, it records a no-op; otherwise it rebuilds impact from current state, advances registry on rebase, and updates derived versions. Pending impacts never block adoption. No model call, rollback of accepted content, or automatic edition repin. Reject appends a decision only.
5. **Manual edit.** After the writer cutover, UI drafts remain per-block React memory and may be saved through `/drafts`; saving text is not canonical. Submit the user's typed patch as `ManualPatch` with zero provider calls. It uses the same validation, patch blob, explicit decision, CAS and impact path as Bridge output.
6. **Table or chart insertion → publication → Typst.** Select paragraph, send an insertion patch with `after_paragraph(anchor_block_id)`, preview typed cells/values/proofs, then adopt. Assembly resolves the anchor against the pinned registry, emits ordered publication data, and the deterministic Typst render-data path places it after that paragraph. A failed resolution leaves enrichment stale and preview `STALE`; there is no UI-only placement. Bar charts compile locally from documented numeric values.
7. **Figure integration.** Gallery queries accepted/rejected/pending archived inventory; authorized detail/thumbnail routes resolve candidate IDs server-side. Selection/caption/order/anchor forms a typed patch and makes no model call. Adoption validates inventory decision and original blob SHA/MIME/size/dimensions, then the same enrichment→publication→Typst flow serves a local archived asset and provenance. BRIEF enforces three integrated figures.

## 6. Module and test seams

| Layer/seam | Test without | Verify |
|---|---|---|
| Domain: registry reconciliation, `EditorialProposal` transitions, patch union and placement invariants | React, provider, Postgres | duplicate IDs, immutable terminal states, hashes, stale anchors, unsupported/free-form patch rejection |
| Application: workspace/proposal/thread services with in-memory artifact, registry, proposal, thread, blob, policy and outbox adapters | React, provider, Postgres | snapshot idempotence; no partial ambiguous bootstrap; exact patch rehash; CAS race; same-key replay; no gateway on accept/manual; no blind resubmit; impact retry |
| API DTO/error mapping with dependency overrides | React, provider, Postgres | closed unions, auth-to-subject scoping, stable codes/French messages, idempotency header handling, 202/304/409 shapes |
| PostgreSQL adapters and Alembic migration | React and providers (Postgres required) | FK/unique/check/append-only guarantees, partial active-job uniqueness, lock ordering, transaction rollback, lease fencing |
| Frontend API/error mapper and pure selectors | backend, provider, Postgres, mounted React | schema-version handling, query keys, block-to-server-ID mapping, French messages and cache invalidation |
| Frontend components and browser journeys | provider and Postgres with mocked API (React required) | context retention, compare/diff, keyboard/mobile, pending/error/conflict/gallery states |
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

Keep old GET artifact, preview, revision-lineage and POST revision routes as adapters. New editor clients use snapshot/proposal/thread APIs. Existing explicit POST `revise` means user-requested apply: adapt it to prepare+accept with a deterministic legacy idempotency key; keep preview read-only. Never make Bridge output auto-accept. `/content` gets an additive top-level nullable `publication_kind` so V7/V8 callers do not infer it from an inner V5 payload. Edition compositions keep exact artifact/run/generation pins; publication changes make pins stale, and only an explicit operator update repins them (`application/edition_composition.py:1011-1053`; repository latest-pin lookup `infrastructure/database/repositories/edition_composition.py:231-250`).

## 8. Expand–contract rollout

1. Freeze DTOs, state/error unions, typed patch/version fences and migration preflight. Resolve publication-kind policy; require explicit draft-retention configuration before server-draft writes.
2. Add the single additive `0011_editorial_workspace` revision and ORM/repository/UOW adapters; verify it against empty, exact-current and partial-schema test databases. No production migration in this design phase.
3. Ship workspace registry/bootstrap, snapshot GET/resolve and read-only frontend route. Keep old artifact pages and readers live; ambiguous history remains visibly read-only.
4. Ship thread events/jobs, typed conversation purpose/objective and the shared capacity lease. M2 is message-only; refuse `propose` in API and hide it in UI.
5. In M3, add the D2 proposal aggregate and adoption service behind disabled decision routes. Inventory and cut every synthesis/enrichment writer—including revise synthesis/enrichment, restore stale blocks, impact rebase and publication text correction when applicable. Require the accept‖legacy-revise one-winner oracle and ensure impact rebase uses the run guard plus registry/lineage/outbox. Only then enable accept and `propose` routes. Keep impact sequencing per run.
6. Add M4 server/local drafts and manual editing after cutover. Then add V9 enrichment/publication placement and chart contracts before exposing table/chart UI. Add gallery/thumbnail only after server authorization and image-safety review.
7. Integrate ARTICLE/BRIEF, PDF and composition journeys; monitor stale/ambiguous/bootstrap and pending-impact outcomes. Keep V4–V8 readers and composition pins exact. Retain revision endpoints as facades; any later contraction requires a separate reviewed change.

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
| Bridge's documented global single-generation rule | Current app code proves only one busy turn per conversation and one active POST per relay process (`application/model_conversations.py:1118-1123`; `docs/mission07/tools/relay.py:328,438`). Cross-process/Bridge-wide enforcement is unverified. Keep the PostgreSQL singleton lease/fencing gate; verify with two concurrent relay processes and a Bridge-side oracle before deployment. Never expire/release a lease while external outcome is unknown; reconcile the exact ModelRun first. |
| Legacy duplicate/reordered blocks | Leave ambiguous blocks read-only and require an explicit reviewed lineage mapping. Do not guess by ordinal, similarity or model. |
| Kind absent in legacy publication | Use only persisted routing/ADR-0008 resolver for V4–V6; block editing if unresolved. Remove the current ARTICLE fallback from the new workspace path. |
| Draft lifetime | Use the configurable 30-day-since-last-change default and defined purge triggers; fail closed for server persistence without configured retention. |
| Thumbnail safety and load | Bound decoded bytes, dimensions, pixel count and output size; rasterize SVG in a restricted local path; authorize every request and measure cache behavior without public URLs. |
| Anchor loss after edit | Keep the enrichment stale and preview visibly `STALE`; require a new anchor choice or explicit removal. Preserve original bytes and diagnostics. |
| Large registry JSONB | Keep the first implementation run-scoped and document-sized; validate parent closure/order in the adapter. Normalize behind the same interface only if measured registry size/query cost warrants it. |
| Outbox crash windows | Make accept state and impact row one transaction; make impact workers idempotent with bounded retry and visible `pending`/`failed` status. Composition pins stay unchanged. |

## Verified discrepancies at the base

- Mission 22 requires explicit `publication_kind` and no silent ARTICLE default. `production_synthesis_revision.py:2204` and `:3194` currently default a missing routing decision to ARTICLE. `ContentResponse` has no top-level kind (`api/subject_content.py:72-90`), and the frontend reads the kind only for schema V7 while its publication union ends at V7 (`ProductionArtifactView.tsx:2078-2081`; `api/production.ts:1289-1299`); V8 is not represented at that boundary.
- M0 D1/D2/D3 describe target behavior not yet in code: IDs are hash-derived and duplicates can be omitted/refused; current revise writes artifacts directly without a proposal accept step; placement has no paragraph anchor (`production_synthesis_revision.py:270-319,357-425,2881-2959`; `production_editorial_enrichment.py:108-127`).
- The one-generation Bridge rule is documented, but reviewed executable code shows only per-conversation and per-relay-process exclusion. Global enforcement remains unverified; see the first open point.
