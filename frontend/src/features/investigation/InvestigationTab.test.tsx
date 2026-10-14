import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationView } from "../../api/investigation";
import { InvestigationTab } from "./InvestigationTab";

const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const EDITION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const RUN_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const SYNTHESIS_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const INVESTIGATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const SHA256 = "a".repeat(64);
const OBSERVATION_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff";

function makeInvestigation(
  overrides: Partial<InvestigationView> = {},
): InvestigationView {
  const budget: InvestigationView["budget"] = {
    bridge_turns: { limit: 8, consumed: 2, remaining: 6 },
    vt_units: { limit: 10, consumed: 1, remaining: 9 },
    shodan_credits: { limit: 5, consumed: 0, remaining: 5 },
    downloads: { limit: 2, consumed: 0, remaining: 2 },
    tool_seconds: { limit: 120, consumed: 12, remaining: 108 },
    context_tokens: { limit: 20_000, consumed: 1_000, remaining: 19_000 },
    context_bytes: { limit: 100_000, consumed: 5_000, remaining: 95_000 },
  };
  return {
    id: INVESTIGATION_ID,
    subject_id: SUBJECT_ID,
    production_run_id: RUN_ID,
    synthesis_artifact_id: SYNTHESIS_ID,
    synthesis_sha256: SHA256,
    status: "queued",
    version: 1,
    stop_reason: null,
    agenda: [],
    hypotheses: [],
    findings: [],
    observations: [],
    sample_associations: [],
    opinion_draft: null,
    budget,
    blocked_decisions: [],
    available_tools: ["article.get_claims"],
    unavailable_tools: [],
    envelope_approved: false,
    allowed_providers: [],
    acquire_hits_allowed: false,
    ...overrides,
  };
}

function summary(view: InvestigationView) {
  return {
    id: view.id,
    subject_id: view.subject_id,
    production_run_id: view.production_run_id,
    synthesis_artifact_id: view.synthesis_artifact_id,
    synthesis_sha256: view.synthesis_sha256,
    status: view.status,
    version: view.version,
    stop_reason: view.stop_reason,
    updated_at: "2026-10-13T09:00:00Z",
  };
}

function requestBody(init?: RequestInit): unknown {
  const body = init?.body;
  return typeof body === "string" ? (JSON.parse(body) as unknown) : body;
}

function createFetch(view: InvestigationView | null) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let currentView = view;
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    calls.push({ url, init });
    if (url === `/api/subjects/${SUBJECT_ID}/production/artifacts/synthesis`) {
      return Promise.resolve(
        Response.json({
          artifact_id: SYNTHESIS_ID,
          production_run_id: RUN_ID,
          stage: "synthesis",
          version: 3,
          input_hash: "b".repeat(64),
          canonical_sha256: SHA256,
          status: "verified",
          metadata: {},
          canonical_content: { schema_version: 2 },
          caption: "Synthèse vérifiée",
          provenance: "production",
        }),
      );
    }
    if (url === `/api/subjects/${SUBJECT_ID}/investigations`) {
      if (init?.method === "POST") {
        currentView = currentView ?? makeInvestigation();
        return Promise.resolve(
          Response.json({
            investigation: currentView,
            job_id: null,
            idempotent_replay: false,
          }),
        );
      }
      return Promise.resolve(
        Response.json(currentView ? [summary(currentView)] : []),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}` && currentView) {
      return Promise.resolve(Response.json(currentView));
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/envelope`) {
      return Promise.resolve(
        Response.json({
          investigation_id: INVESTIGATION_ID,
          approved: false,
          version: 0,
          allowed_providers: [],
          available_providers: ["vt"],
          acquire_hits_allowed: false,
          max_acquire_volume: 0,
          budget: {
            bridge_turns: { limit: 8, consumed: 2, remaining: 6 },
            vt_units: { limit: 10, consumed: 1, remaining: 9 },
            shodan_credits: { limit: 5, consumed: 0, remaining: 5 },
            downloads: { limit: 0, consumed: 0, remaining: 0 },
            tool_seconds: { limit: 120, consumed: 12, remaining: 108 },
            context_tokens: {
              limit: 20_000,
              consumed: 1_000,
              remaining: 19_000,
            },
            context_bytes: {
              limit: 100_000,
              consumed: 5_000,
              remaining: 95_000,
            },
          },
          disclosure: {
            tlp: "amber",
            do_not_submit: true,
            external_llm_allowed: false,
          },
          approval_decision_id: null,
          approval_actor_id: null,
          approved_at: null,
        }),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/samples`) {
      return Promise.resolve(
        Response.json(
          currentView?.sample_associations.map((sample) => ({
            ...sample,
            changes: [],
          })) ?? [],
        ),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/sample-comparisons`) {
      return Promise.resolve(
        Response.json({
          investigation_id: INVESTIGATION_ID,
          comparisons: [],
          truncated: false,
        }),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/hypotheses`) {
      return Promise.resolve(
        Response.json({ investigation_id: INVESTIGATION_ID, hypotheses: [] }),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/protocol-models`) {
      return Promise.resolve(
        Response.json({
          investigation_id: INVESTIGATION_ID,
          models: [],
          truncated: false,
        }),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/retrohunt`) {
      return Promise.resolve(
        Response.json({
          investigation_id: INVESTIGATION_ID,
          subject_id: SUBJECT_ID,
          maximum_slots: 2,
          consumed_slots: 0,
          remaining_slots: 2,
          jobs: [],
        }),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/detections`) {
      return Promise.resolve(
        Response.json({ investigation_id: INVESTIGATION_ID, versions: [] }),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/opinion`) {
      return Promise.resolve(
        Response.json({
          draft: null,
          history: [],
          citation_violations: [],
          renderable_blocks: [],
          provenance: null,
        }),
      );
    }
    return Promise.resolve(
      Response.json(
        { detail: { code: "unexpected_request" } },
        { status: 500 },
      ),
    );
  });
  return { fetchMock, calls };
}

function renderInvestigation(view: InvestigationView | null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  const fetchState = createFetch(view);
  vi.stubGlobal("fetch", fetchState.fetchMock);
  const result = render(
    <QueryClientProvider client={client}>
      <InvestigationTab subjectId={SUBJECT_ID} editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
  return { ...result, ...fetchState };
}

afterEach(() => vi.unstubAllGlobals());

describe("InvestigationTab", () => {
  it("affiche l’état vide et lie le lancement aux identifiants exacts", async () => {
    const { user } = { user: userEvent.setup() };
    const { calls } = renderInvestigation(null);

    expect(
      await screen.findByText("Aucune enquête n’a encore été lancée."),
    ).toBeInTheDocument();
    expect(screen.getByText(RUN_ID)).toBeInTheDocument();
    expect(
      await screen.findByText(new RegExp(SYNTHESIS_ID)),
    ).toBeInTheDocument();
    expect(screen.getByText(SHA256)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Investiguer cette version exacte" }),
    );
    expect(
      await screen.findByRole("heading", {
        name: `Enquête ${INVESTIGATION_ID}`,
      }),
    ).toBeInTheDocument();
    const post = calls.find((call) => call.init?.method === "POST");
    expect(post?.url).toBe(`/api/subjects/${SUBJECT_ID}/investigations`);
    expect(requestBody(post?.init)).toEqual({
      production_run_id: RUN_ID,
      synthesis_artifact_id: SYNTHESIS_ID,
      synthesis_sha256: SHA256,
    });
    expect(
      new Headers(post?.init?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
  });

  it("montre l’enquête en cours et permet de l’interrompre", async () => {
    renderInvestigation(makeInvestigation({ status: "running" }));

    expect(
      await screen.findByRole("heading", {
        name: `Enquête ${INVESTIGATION_ID}`,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/En cours · version CAS 1/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Interrompre l’enquête" }),
    ).toBeInTheDocument();
  });

  it("affiche l’état awaiting_review", async () => {
    renderInvestigation(
      makeInvestigation({
        status: "awaiting_review",
        stop_reason: "submission_needs_review",
      }),
    );

    expect(await screen.findByText(/À réviser/)).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Reprendre l’enquête" }),
    ).toBeInTheDocument();
  });

  it("relie une permission G2 requise à la décision d’enveloppe", async () => {
    renderInvestigation(
      makeInvestigation({
        status: "exhausted",
        stop_reason: "permission_required",
        blocked_decisions: [
          { code: "permission_required", tool_name: "virustotal.search" },
        ],
      }),
    );

    expect(
      await screen.findByText(/Consultez et décidez l’enveloppe G2 ici/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Décision requise : enveloppe G2 non approuvée/),
    ).toBeInTheDocument();
    expect(screen.getByText(/outil : virustotal.search/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Approuver l’enveloppe" }),
    ).toHaveAttribute("href", "#investigation-envelope-approve");
    expect(
      screen.queryByRole("button", { name: "Reprendre l’enquête" }),
    ).not.toBeInTheDocument();
  });

  it("traite model_unavailable comme une limite locale reprenable", async () => {
    renderInvestigation(
      makeInvestigation({
        status: "awaiting_review",
        stop_reason: "model_unavailable",
      }),
    );

    expect(
      await screen.findByText(/Le modèle est indisponible/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/limite locale de cette piste/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reprendre l’enquête" }),
    ).toBeInTheDocument();
  });

  it("présente nouveauté, verdicts, preuves datées, budget par dimension et limites locales sans données brutes", async () => {
    const view = makeInvestigation({
      status: "running",
      findings: [
        {
          id: "finding-new-sample",
          version: 1,
          kind: "new_sample",
          assessment: "supported",
          novelty: "new_to_article",
          statement:
            "Une variante absente du snapshot de l’article a été trouvée.",
          scope: "Article étudié",
          limitations: ["La relation reste à confirmer."],
          observation_refs: [OBSERVATION_ID],
          claim_refs: [],
          created_at: "2026-10-13T08:00:00Z",
        },
        {
          id: "finding-claim",
          version: 1,
          kind: "claim_verification",
          assessment: "refuted",
          novelty: "claim_correction",
          statement:
            "L’affirmation de l’article est contredite par l’observation.",
          scope: "Affirmation C1",
          limitations: [],
          observation_refs: [OBSERVATION_ID],
          claim_refs: ["claim-1"],
          created_at: "2026-10-13T08:05:00Z",
        },
      ],
      observations: [
        {
          id: OBSERVATION_ID,
          kind: "vt_relationship",
          sha256: "c".repeat(64),
          source: "VirusTotal",
          source_version: "2026-10-13",
          observed_at: "2026-10-13T08:00:00Z",
          object_type: "sample",
          object_id: "private-object-id-must-not-render",
          limitations: ["Ancienneté de la donnée inconnue."],
          invocation_ref: null,
          blob_id: "private-blob-id-must-not-render",
          api_key: "private-api-key-must-not-render",
        } as InvestigationView["observations"][number],
      ],
      sample_associations: [
        {
          id: "sample-association-1",
          sample_sha256: "d".repeat(64),
          origin: "vt_pivot",
          qualification: "candidate",
          related_sample_sha256: ["e".repeat(64)],
          version: 1,
        },
      ],
      unavailable_tools: ["virustotal.search"],
      blocked_decisions: [{ code: "policy_denied", tool_name: "vt.download" }],
    });
    const { container } = renderInvestigation(view);

    expect(
      await screen.findByText(/Nouveau dans cet article/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/La relation reste à confirmer/),
    ).toBeInTheDocument();
    expect(screen.getByText("contredit", { exact: true })).toBeInTheDocument();
    expect(screen.getByText(/Éléments contradictoires/)).toBeInTheDocument();
    expect(
      await screen.findByText(/Acquisition ≠ confirmation/),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Tours Bridge/).length).toBeGreaterThan(1);
    expect(screen.getAllByText(/Unités VirusTotal/).length).toBeGreaterThan(1);
    expect(screen.getAllByText(/Crédits Shodan/).length).toBeGreaterThan(1);
    expect(screen.getByText("Limites")).toBeInTheDocument();
    expect(
      screen.getByText(/Capacités indisponibles, limites locales/),
    ).toBeInTheDocument();
    expect(container.textContent).not.toContain(
      "private-object-id-must-not-render",
    );
    expect(container.textContent).not.toContain(
      "private-blob-id-must-not-render",
    );
    expect(container.textContent).not.toContain(
      "private-api-key-must-not-render",
    );
  });
});
