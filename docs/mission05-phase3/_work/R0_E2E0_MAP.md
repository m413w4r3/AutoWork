# M05-R0 E2E-0 map

## Verdict and provenance

- Checkout: `0db6a9048d5ca356de270be833698def8a35eb92`; requested base `55fba67d5882c1ae6c4abff4e74c53a24e57649f` is an ancestor.
- Scope: read-only reconnaissance; no provider, Bridge, CTI, or network calls were made. No sample was executed. Test fakes are local.
- E2E-0 contract: one no-sample investigation, disposable PostgreSQL, authorized G2, observations/finding versions, simulated Advisor and offline V4 parse/reparse, refused/partial/succeeded outcomes, bounded P10→P09, audit export, approved opinion and V5/V6 real PDF, SHA/lineage, restart/replay `NEEDS_REVIEW`, and zero real emission (`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md:84`).
- Existing vertical is an E2E-1-shaped sample journey, not E2E-0: `backend/tests/integration/test_investigation_vertical_postgres.py:115-447` acquires a synthetic candidate and publishes V6.

## Reusable seams

- Disposable PG: `backend/tests/integration/conftest.py:61-101`; `temporary_postgres_url`/`migrated_postgres_url` create a uniquely named DB and drop it in `finally`; `uow_factory` wraps `SqlAlchemyUnitOfWork`.
- Seed and app: `tests.workflow_emulator.investigation_support.seed_verified_article(uow_factory, artifact_store, *, suffix, material=None, include_seed=True, article_claim=None)` (`:377-385`); `composition_factory(*, uow_factory, artifact_store, settings, transport, virustotal=None, shodan=None, include_local_analysis=False, ...)` (`:747-759`); `build_investigation_app(*, uow_factory, artifact_store, composition_factory, include_detection=False)` (`:936-961`). Use `include_seed=False` for the no-sample scenario (`:392-397`).
- Blob/artifact storage: main vertical imports `BlobCatalogService`, `ProductionArtifactStore`, and `FilesystemBlobStore` (`test_investigation_vertical_postgres.py:13-19,37-38`); constructs `BlobCatalogService(FilesystemBlobStore(tmp_path / ...), uow_factory)` then `ProductionArtifactStore(blob_catalog)` (`:118-122`). This is isolated filesystem storage, not MinIO.
- Investigation Bridge fake: `ScriptedInvestigationTransport(responses, *, uow_factory, investigation_id, material, inject_analysis_at_turn=3, analyze_pending_samples=False, final_response_builder=None, include_external_pivot=False)` (`tests/workflow_emulator/investigation_support.py:219-245`); it counts submissions through its `ScriptedTurnTransport` base and can inject responses from persisted state (`:247-320`). `decision(...)` builds tool-call/finding/next-action wire values (`:689-706`).
- Existing main assertions: HTTP starts one seeded investigation and separately approves G2 (`test_investigation_vertical_postgres.py:243-273`); checks completed status, sample candidate/quarantine SHA, observations, finding assessments, tool invocations and fake provider counts (`:275-357`); re-dispatch is idempotent (`:353-357`); then qualifies sample, composes/approves/publishes opinion and asserts new V6 while preserving V5 and composition/manifest (`:359-445`). It does **not** cover no-sample, Advisor, V4 reparse, full/paged/cursor context parity, P10→P09, audit export, real V5/V6 PDF rendering, or restart/replay `NEEDS_REVIEW` in that same scenario.
- Advisor fake port: `tests.test_investigation_advisor_tools.Transport(submission)` records calls and exposes `real_network_calls=0` (`:65-74`); `make_tool(...)` constructs the real `InvestigationAdvisorTool(transport=..., settings=..., investigations=..., observations=..., findings=..., investigation_states=..., opinions=..., sample_associations=..., samples=..., blobs=..., article_snapshots=..., artifact_store=..., registry=...)` (`:236-260`). Local `DurableReservationTransport` checks DB invocation/envelope reservations before returning a scripted response (`:5501-5521`). Local policy refusal with zero calls is covered at `:4508-4535` and `:2727-2741`; partial result at `:788-810`; inject a successful wire reply as `TurnSubmission(SUCCEEDED, output_text=...)` (test helper `:65-74`).
- Advisor preparation and V4 reparse: pure `advisor_consultation.prepare(projection)` returns `PreparedConsultation | PreparationRefusal` (`backend/src/cti_app/application/advisor_consultation.py:144-159`); it renders a disclosure-filtered prompt and computes local dossier/preflight (`:159-230`). `ReparsePersistedAdvisorOutputTool` is built with observations, blob repository, artifact store, and recorder (`tests/test_advisor_output_reparse.py:27-75,138-214`). V4 test monkeypatches socket and subprocess to fail if used, then asserts successful parse, matching persisted SHA and zero Bridge turns (`:241-280`); bad SHA, missing parser selection/context and invalid V4 are refused offline (`:316-386`).
- Model conversation real service seam (still no egress): `ModelConversationService(uow_factory, gateway, blob_store, ...)` (`backend/src/cti_app/application/model_conversations.py:272-295`); `add_turn(conversation_id, *, message, mode, external_llm_allowed, ..., idempotency_key, correlation_id, ...)` (`:1086-1100`). `tests/test_model_conversations_gateway_integration.py:_ScriptedBridgeAdapter` returns fixed output and records requests (`:57-99`); `_build_service(adapter, tmp_path)` wires real `ModelGateway` + service and filesystem blob store (`:101-126`); `test_q1_fresh_conversation_reaches_the_adapter_exactly_once` proves one fake call and idempotent replay (`:129-179`).
- Context: PostgreSQL operator summary/section-page seam is `test_operator_context_summary_and_pages_preserve_postgres_cas` (`tests/integration/test_operator_context_summary_postgres.py:333-475`); full context and keyset page semantics are separately exercised in `tests/test_research_context_builder.py`. Contract fixes current finding versions, historical versions separately, canonical full-context SHA and stale-cursor 409 (`ARCHITECTURE_DECISIONS.md:72-76`).
- Audit: `cti_app.application.investigation_audit_export.build_audit_bundle(snapshot)` and `write_audit_bundle(...)` (`backend/src/cti_app/application/investigation_audit_export.py:2570-2657`); PostgreSQL projection/resume/scoping oracle is `test_postgres_audit_snapshot_projects_scoped_rows_and_resumes_without_reread` (`backend/tests/integration/test_investigation_audit_export_postgres.py:808-940`). It is not yet joined to the vertical journey.
- Opinion/PDF: `AnalystOpinionService(*, uow_factory, artifact_store, claim_knowledge_provider=None, author_id=...)` (`backend/src/cti_app/application/analyst_opinion_service.py:244-260`); `AnalystPublicationService(*, uow_factory, artifact_store, augmentation_service=None)` (`backend/src/cti_app/application/analyst_publication_service.py:38-53`). PG test `test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5` composes/approves, verifies immutable V5 plus V6 and SHA (`backend/tests/integration/test_analyst_opinion_publication_postgres.py:365-367,760-817`); `PublicationRenderService` receives UOW, artifact/media stores, `TypstRenderer`, `TypstSubprocessCompiler`, template/font roots, lock path and optional D2 compiler (`:819-835`; signature `backend/src/cti_app/application/publication_rendering.py:245-262`). It renders both artifacts and verifies reviewer/approver lineage (`:832-881`) and `pdftotext` content plus hidden actor IDs (`:882-896`).
- Real PDF prerequisites: `TYPST_BINARY` selects Typst; test is skipped if D2, Typst or `pdftotext` is missing (`test_analyst_opinion_publication_postgres.py:360-367`). Font bundle defaults to `chpTypst` unless `FONT_BUNDLE_ROOT` is set; lock is `infra/typst-fonts.lock` (`:824-830`). Preview helper `_TypstDraftPreviewRenderer(media_assets, root)` installs workspace and calls `_compile_pdf` (`:169-227`).
- Recovery: W18 provides a supervised restart journey with ambiguous operation ending in `NEEDS_REVIEW`, no replay of confirmed download, and worker restart/resume (`tests/workflow_emulator/test_w18_supervised_first_run_recovery.py:503-530,1081-1157`). Reuse its failure/recovery assertions on the no-sample M05-B driver; existing W18 uses sample/acquisition seams and is not the required E2E-0 proof.

## Observed offline test results

Environment: Python 3.12, Typst 0.15.1, Poppler 24.02.0 and D2 available. `TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst`. The publication test reaches real rendering but fails because the configured template font bundle lacks `CascadiaCode/ttf/CascadiaCode.ttf` (`chpTypst` default); status is `BLOCKED_PDF_FONT` / test `FAIL`, not a pass. First sandboxed PG attempt could not access the local DB socket (`PermissionError`); rerun with local socket access succeeded for the vertical test. No repository files changed before this report.

Common setup and exact commands used (run from repository root):

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=$HOME/.cache/autowork-tmp && mkdir -p "$TMPDIR"
cd backend
TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
  uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_vertical_postgres.py -q --tb=short
```

Result: **PASS — 13 passed**.

```sh
TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
  uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_analyst_opinion_publication_postgres.py -q --tb=short
```

Result: **FAIL — 1 failed**, missing pinned Cascadia Code font listed above. No test was skipped.

## E2E-0 gap matrix

| Required seam | Existing evidence | Remaining work; minimal addition |
|---|---|---|
| Disposable PG, isolated blob store | `integration/conftest.py:77-101`; vertical `:118-122` | Reuse `uow_factory` + `tmp_path`; use no-sample seed. |
| Single no-sample investigation + G2 | G2 is HTTP-driven in vertical `:246-273`; `seed_verified_article(include_seed=False)` support `investigation_support.py:377-397` | New driver: start one ID, approve G2 with no provider allowlist and explicit disclosure. |
| Observation + finding version chain | Vertical asserts persisted observation/finding rows `:307-344`; domain append-version oracle `tests/test_investigation_domain.py:290-303` | Append two versions and assert current/history in same driver. |
| Full, paged and cursor context parity | Operator PG summary/pages `test_operator_context_summary_postgres.py:333-475`; semantics `ARCHITECTURE_DECISIONS.md:74` | Driver builds and reads same investigation full + pages, follows cursor, checks canonical SHA/stale cursor. |
| Advisor refused/partial/succeeded through port | Fake tool and counters `test_investigation_advisor_tools.py:65-74,236-260,4508-4535,788-810` | Add three scripted outputs on same investigation; assert refusal before transport and call count. |
| Offline V4 parse/reparse | `test_advisor_output_reparse.py:241-280,316-386` | Persist raw output SHA, invoke explicit V4 reparse in driver; assert zero transport/network and append provenance. |
| Bounded P10→P09 | Unit seams `tests/test_invariant_proposals.py:1620-1687,1850-1887`; production gate `backend/src/cti_app/application/invariant_proposals.py:948-969` | One synthetic bounded proposal and local P09 gate; do not perform a real model call. |
| Audit bundle export/read | PG snapshot test `test_investigation_audit_export_postgres.py:808-940`; builder/writer module `investigation_audit_export.py:2570-2657` | Build/write/read bundle for the same investigation; verify source SHA and scope. |
| Approved opinion, V5/V6 and real PDF | PG opinion test `test_analyst_opinion_publication_postgres.py:760-896` | Join compose/approve/publish, render V5 then V6 and verify SHA/lineage/text. Current font bundle blocks this oracle. |
| Restart/replay `NEEDS_REVIEW` | W18 recovery `test_w18_supervised_first_run_recovery.py:1081-1157` | Inject ambiguous fake transport at one operation; reconstruct worker/service, assert no replay and `NEEDS_REVIEW`. |
| Zero real emission | Fake transport call counters and V4 network monkeypatch above | Add final assertion: only local fakes invoked; no provider/Bridge/DNS/socket activity. |

## Proposed single continuous M05-B driver

- Minimal scope: add `backend/tests/integration/test_m05_b_e2e0_postgres.py::test_m05_b_e2e0_single_investigation_postgres`; one `migrated_postgres_url`/`uow_factory`, one `investigation_id`, one filesystem blob root. Reuse `seed_verified_article(..., include_seed=False)`, `BlobCatalogService`, `ProductionArtifactStore`, `build_investigation_app`, context builder, Advisor tool, V4 reparse tool, invariant proposal service, audit bundle functions, opinion/publication services, and W18 restart pattern.
- Ordered journey: seed V5 and article with no sample → start operator-led investigation → approve G2 → append cited observations and two finding versions → assert full context and paged/cursor parity → run simulated Advisor refusal, partial and succeeded on the same investigation → persist raw wire SHA and offline V4 reparse → create one bounded P10 proposal and pass through local P09 gate → export/read audit bundle → compose and human-approve opinion → publish V6 while retaining V5 → render V5 then V6 with real Typst/Poppler and check SHA, reviewer/approver lineage and `pdftotext` → simulate ambiguous `NEEDS_REVIEW`, rebuild worker and replay safely → assert no duplicate call and zero real transport.
- Fakes: substitute only Advisor/Bridge and P10 model transport with deterministic in-process adapters; leave PostgreSQL, context, audit, opinion, publication and PDF renderer/compiler real. Publication PDF currently requires repairing/providing the locked Cascadia Code font bundle before that assertion can pass.
- Output SHA for reconnaissance: `0db6a9048d5ca356de270be833698def8a35eb92`.
