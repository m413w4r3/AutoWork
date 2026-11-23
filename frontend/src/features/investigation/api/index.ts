/**
 * M24 workshop API — public TypeScript contract of the cockpit.
 *
 * Source of truth: the FastAPI routers mounted in
 * `backend/src/cti_app/api/main.py` and their Pydantic models. Every module
 * mirrors exact route paths, request bodies, response payloads, CAS fields,
 * `Idempotency-Key` requirements and typed error codes.
 *
 * NON EXPOSÉES À L’UI — routes réservées aux opérateurs / sessions agent,
 * volontairement absentes de ce contrat :
 *
 * - `POST /api/investigations/{id}/agent-sessions/{sid}/tool-calls` — D7
 *   HMAC-signed host tool shim; the per-session secret is delivered to the
 *   host agent and must never reach the browser.
 * - `POST …/agent-sessions/{sid}/instructions` with `action: "ack"` — the
 *   owning operator acknowledges its own queue with the session signature;
 *   only the human `enqueue` half is exposed in `agentSessions.ts`.
 * - `POST …/agent-sessions/{sid}/reconcile` — operator-signed reconciliation;
 *   it never auto-relaunches and is a host concern.
 * - `POST …/agent-sessions/{sid}/collect` — operator-signed bounded result
 *   collection; results are written by the host, not by the cockpit.
 * - `POST …/tasks/{tid}/commands` when signed as an operator — the same path
 *   is exposed for HUMAN commands only (`tasks.ts`); the signed variant is
 *   authenticated by the host shim, not by a browser.
 * - `POST …/handoffs` when signed as an operator — exposed for the human
 *   author only (`handoffs.ts`); an owning operator creates handoffs from the
 *   signed shim.
 * - `/api/investigations/{id}/operator/**` (legacy operator context, tools,
 *   invocations, status) — operator-token surface outside the M24 cockpit
 *   contract; GET reads are allowlisted server-side, writes are not part of
 *   this UI.
 *
 * Invariants: no live fetch in tests, synthetic payloads only, no secret or
 * evidence content in this layer.
 */

export * from "./types";
export * from "./client";
export * from "./workspaceAgent";
export * from "./bootstrap";
export * from "./plans";
export * from "./tasks";
export * from "./handoffs";
export * from "./transfers";
export * from "./agentSessions";
export * from "./contributions";
export * from "./opinionProposals";
export * from "./observationExcerpts";
export * from "./workspace";
export * from "./workspaceNotes";
