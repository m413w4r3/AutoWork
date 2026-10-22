import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationView } from "../../api/investigation";
import { InvestigationGovernancePanel } from "./InvestigationGovernancePanel";
import { InvestigationSamples } from "./InvestigationPanels";

const INVESTIGATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const ASSOCIATION_ID = "55555555-5555-4555-8555-555555555555";
const EVIDENCE_ID = "22222222-2222-4222-8222-222222222222";
const SAMPLE_HASH = "d".repeat(64);
type QualificationState =
  "candidate" | "related" | "confirmed" | "rejected" | "uncertain";

function makeInvestigation(
  overrides: Partial<InvestigationView> = {},
): InvestigationView {
  return {
    id: INVESTIGATION_ID,
    subject_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    production_run_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    synthesis_artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    synthesis_sha256: "a".repeat(64),
    status: "running",
    version: 3,
    stop_reason: null,
    agenda: [],
    hypotheses: [],
    findings: [],
    observations: [
      {
        id: EVIDENCE_ID,
        kind: "vt_relationship",
        sha256: "c".repeat(64),
        source: "VirusTotal",
        source_version: "2026-10-13",
        observed_at: "2026-10-13T08:00:00Z",
        object_type: "sample",
        object_id: "sample-evidence",
        limitations: [],
        invocation_ref: null,
      },
    ],
    sample_associations: [],
    opinion_draft: null,
    budget: {
      bridge_turns: { limit: 8, consumed: 2, reserved: 0, remaining: 6 },
      vt_units: { limit: 10, consumed: 1, reserved: 0, remaining: 9 },
      shodan_credits: { limit: 5, consumed: 0, reserved: 0, remaining: 5 },
      downloads: { limit: 2, consumed: 0, reserved: 0, remaining: 2 },
      tool_seconds: { limit: 120, consumed: 12, reserved: 0, remaining: 108 },
      context_tokens: {
        limit: 20_000,
        consumed: 1_000,
        reserved: 0,
        remaining: 19_000,
      },
      context_bytes: {
        limit: 100_000,
        consumed: 5_000,
        reserved: 0,
        remaining: 95_000,
      },
    },
    blocked_decisions: [],
    available_tools: ["article.get_claims"],
    unavailable_tools: [],
    unavailable_tool_reasons: {},
    envelope_approved: false,
    allowed_providers: [],
    acquire_hits_allowed: false,
    ...overrides,
  };
}

function makeEnvelope(approved: boolean) {
  return {
    investigation_id: INVESTIGATION_ID,
    approved,
    version: approved ? 1 : 0,
    allowed_providers: approved ? ["vt"] : [],
    available_providers: ["vt", "shodan"],
    acquire_hits_allowed: approved,
    max_acquire_volume: approved ? 2 : 0,
    budget: {
      bridge_turns: { limit: 8, consumed: 2, reserved: 0, remaining: 6 },
      vt_units: { limit: 10, consumed: 1, reserved: 0, remaining: 9 },
      shodan_credits: { limit: 5, consumed: 0, reserved: 0, remaining: 5 },
      downloads: {
        limit: approved ? 2 : 0,
        consumed: 0,
        reserved: 0,
        remaining: approved ? 2 : 0,
      },
      tool_seconds: { limit: 120, consumed: 12, reserved: 0, remaining: 108 },
      context_tokens: {
        limit: 20_000,
        consumed: 1_000,
        reserved: 0,
        remaining: 19_000,
      },
      context_bytes: {
        limit: 100_000,
        consumed: 5_000,
        reserved: 0,
        remaining: 95_000,
      },
    },
    disclosure: {
      tlp: "amber" as "amber" | null,
      do_not_submit: true,
      external_llm_allowed: false,
    },
    approval_decision_id: approved
      ? "66666666-6666-4666-8666-666666666666"
      : null,
    approval_actor_id: approved ? "analyst:test" : null,
    approved_at: approved ? "2026-10-13T08:00:00Z" : null,
  };
}

function createHarness(options?: {
  approved?: boolean;
  paused?: boolean;
  failPath?: string;
  withSample?: boolean;
}) {
  const requests: Array<{
    url: string;
    method: string;
    body: unknown;
    idempotencyKey: string | null;
  }> = [];
  let envelope = makeEnvelope(options?.approved ?? false);
  let sample: {
    id: string;
    sample_sha256: string;
    origin: "vt_pivot";
    qualification: QualificationState;
    related_sample_sha256: string[];
    version: number;
    changes: Array<{
      from_qualification: QualificationState;
      to_qualification: QualificationState;
      actor_kind: "human";
      actor_id: string;
      reason: string;
      changed_at: string;
    }>;
  } = {
    id: ASSOCIATION_ID,
    sample_sha256: SAMPLE_HASH,
    origin: "vt_pivot" as const,
    qualification: "candidate",
    related_sample_sha256: ["a".repeat(64)],
    version: 1,
    changes: [] as Array<{
      from_qualification: QualificationState;
      to_qualification: QualificationState;
      actor_kind: "human";
      actor_id: string;
      reason: string;
      changed_at: string;
    }>,
  };
  const investigation = makeInvestigation({
    stop_reason: options?.paused ? "user_interrupt" : null,
    status: options?.paused ? "exhausted" : "running",
  });
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const method = init?.method ?? "GET";
    let body: unknown = null;
    if (typeof init?.body === "string") body = JSON.parse(init.body) as unknown;
    const idempotencyKey = new Headers(init?.headers).get("Idempotency-Key");
    requests.push({ url, method, body, idempotencyKey });

    if (
      options?.failPath &&
      url.endsWith(options.failPath) &&
      method === "POST"
    ) {
      return Response.json(
        {
          detail: {
            code: "envelope_conflict",
            message: "La version a changé.",
          },
        },
        { status: 409 },
      );
    }
    if (url.endsWith("/envelope") && method === "GET") {
      return Response.json(envelope);
    }
    if (url.endsWith("/samples") && method === "GET") {
      return Response.json(options?.withSample ? [sample] : []);
    }
    if (url.endsWith("/envelope/approve") && method === "POST") {
      const input = body as {
        allowed_providers: string[];
        acquire_hits_allowed: boolean;
        max_acquire_volume: number;
      };
      envelope = {
        ...envelope,
        approved: true,
        version: 1,
        allowed_providers: input.allowed_providers,
        acquire_hits_allowed: input.acquire_hits_allowed,
        max_acquire_volume: input.max_acquire_volume,
        approval_decision_id: "66666666-6666-4666-8666-666666666666",
        approval_actor_id: "analyst:test",
        approved_at: "2026-10-13T08:00:00Z",
      };
      return Response.json(envelope);
    }
    if (url.endsWith("/envelope/extend") && method === "POST") {
      envelope = { ...envelope, version: envelope.version + 1 };
      return Response.json(envelope);
    }
    if (url.endsWith("/envelope/revoke") && method === "POST") {
      envelope = {
        ...envelope,
        version: envelope.version + 1,
        allowed_providers: [],
        acquire_hits_allowed: false,
        max_acquire_volume: 0,
      };
      return Response.json(envelope);
    }
    if (url.endsWith("/qualify") && method === "POST") {
      const input = body as {
        qualification: typeof sample.qualification;
        reason: string;
      };
      sample = {
        ...sample,
        qualification: input.qualification,
        version: sample.version + 1,
        changes: [
          ...sample.changes,
          {
            from_qualification: sample.qualification,
            to_qualification: input.qualification,
            actor_kind: "human",
            actor_id: "analyst:test",
            reason: input.reason,
            changed_at: "2026-10-13T08:00:00Z",
          },
        ],
      };
      return Response.json({ association: sample, idempotent_replay: false });
    }
    if (url.endsWith("/requests") && method === "POST") {
      return Response.json({
        investigation,
        request_id: "77777777-7777-4777-8777-777777777777",
        job_id: "88888888-8888-4888-8888-888888888888",
        idempotent_replay: false,
      });
    }
    return Response.json(
      { detail: { code: "unexpected_request" } },
      { status: 500 },
    );
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.stubGlobal("fetch", fetchMock);
  const result = render(
    <QueryClientProvider client={client}>
      {options?.withSample ? (
        <InvestigationSamples
          investigationId={INVESTIGATION_ID}
          observations={investigation.observations}
        />
      ) : (
        <InvestigationGovernancePanel investigation={investigation} />
      )}
    </QueryClientProvider>,
  );
  return { ...result, requests, fetchMock };
}

afterEach(() => vi.unstubAllGlobals());

describe("InvestigationGovernancePanel", () => {
  it("approuve uniquement après raison et confirmation explicites", async () => {
    const user = userEvent.setup();
    const { requests } = createHarness();

    expect(
      await screen.findByText(
        "Proposition locale non approuvée · aucun droit externe accordé",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Les pivots passifs dans cette enveloppe ne demandent plus de clic/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/la qualification reste humaine/),
    ).toBeInTheDocument();

    const approveButton = screen.getByRole("button", {
      name: "Confirmer l’approbation",
    });
    expect(approveButton).toBeDisabled();
    await user.click(approveButton);
    expect(
      requests.some((request) => request.url.endsWith("/envelope/approve")),
    ).toBe(false);

    await user.click(screen.getByLabelText("vt"));
    await user.click(
      screen.getByLabelText("Autoriser l’acquisition automatique de hits"),
    );
    await user.clear(screen.getByLabelText("Téléchargements"));
    await user.type(screen.getByLabelText("Téléchargements"), "2");
    await user.type(
      screen.getByLabelText("Raison obligatoire"),
      "Les pivots autorisés répondent aux questions de l’article.",
    );
    await user.click(screen.getByLabelText(/Je confirme cette décision G2/));
    await user.click(approveButton);

    await waitFor(() =>
      expect(
        requests.some((request) => request.url.endsWith("/envelope/approve")),
      ).toBe(true),
    );
    const request = requests.find((item) =>
      item.url.endsWith("/envelope/approve"),
    );
    expect(request?.body).toMatchObject({
      expected_version: null,
      allowed_providers: ["vt"],
      acquire_hits_allowed: true,
      max_acquire_volume: 2,
      reason: "Les pivots autorisés répondent aux questions de l’article.",
    });
    expect(request?.idempotencyKey).toBeTruthy();
    expect(request?.body).not.toHaveProperty("actor_id");
  });

  it("étend puis révoque par décisions G2 distinctes et versionnées", async () => {
    const user = userEvent.setup();
    const { requests } = createHarness({ approved: true });

    await user.click(
      await screen.findByRole("button", { name: "Étendre l’enveloppe" }),
    );
    await user.type(
      screen.getByLabelText("Raison obligatoire"),
      "Un budget supplémentaire est nécessaire pour cette piste.",
    );
    await user.click(screen.getByLabelText(/Je confirme cette décision G2/));
    await user.click(
      screen.getByRole("button", { name: "Confirmer l’extension" }),
    );

    await waitFor(() =>
      expect(
        requests.some((request) => request.url.endsWith("/envelope/extend")),
      ).toBe(true),
    );
    await user.click(screen.getByRole("button", { name: "Révoquer" }));
    await user.type(
      screen.getByLabelText("Raison obligatoire de révocation"),
      "Les recherches externes sont terminées.",
    );
    await user.click(screen.getByLabelText(/Je confirme la révocation/));
    await user.click(
      screen.getByRole("button", { name: "Confirmer la révocation" }),
    );

    const extension = requests.find((request) =>
      request.url.endsWith("/envelope/extend"),
    );
    const revocation = requests.find((request) =>
      request.url.endsWith("/envelope/revoke"),
    );
    expect(extension?.body).toMatchObject({ expected_version: 1 });
    expect(extension?.idempotencyKey).toBeTruthy();
    expect(revocation?.body).toMatchObject({
      expected_version: 2,
      reason: "Les recherches externes sont terminées.",
    });
    expect(revocation?.idempotencyKey).toBeTruthy();
  });

  it("affiche les conflits CAS typés renvoyés par l’API", async () => {
    const user = userEvent.setup();
    createHarness({ failPath: "/envelope/approve" });

    await screen.findByText(
      "Proposition locale non approuvée · aucun droit externe accordé",
    );
    await user.type(
      screen.getByLabelText("Raison obligatoire"),
      "La décision doit être persistée avec CAS.",
    );
    await user.click(screen.getByLabelText(/Je confirme cette décision G2/));
    await user.click(
      screen.getByRole("button", { name: "Confirmer l’approbation" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /envelope_conflict, HTTP 409/,
    );
  });

  it("valide le texte ciblé et affiche la reprise automatique d’une enquête en pause", async () => {
    const user = userEvent.setup();
    const { requests } = createHarness({ paused: true });

    await screen.findByRole("heading", { name: "Demander une analyse ciblée" });
    const submit = screen.getByRole("button", {
      name: "Demander cette analyse",
    });
    expect(submit).toBeDisabled();
    await user.click(submit);
    expect(requests.some((request) => request.url.endsWith("/requests"))).toBe(
      false,
    );

    await user.type(
      screen.getByLabelText("Demande (2 000 caractères maximum)"),
      "Vérifier cette affirmation.",
    );
    await user.selectOptions(
      screen.getByLabelText("Type de cible (facultatif)"),
      "claim_id",
    );
    await user.type(
      screen.getByLabelText("Cible (256 caractères maximum)"),
      "C1",
    );
    await user.click(submit);

    expect(
      await screen.findByText(
        /L’enquête en pause a été relancée automatiquement/,
      ),
    ).toBeInTheDocument();
    const request = requests.find((item) => item.url.endsWith("/requests"));
    expect(request?.body).toMatchObject({
      text: "Vérifier cette affirmation.",
      target: { kind: "claim_id", value: "C1" },
      expected_version: 3,
    });
    expect(request?.idempotencyKey).toBeTruthy();
  });
});

describe("InvestigationSamples", () => {
  it("exige raison et preuve avant confirmation, puis conserve chaque changement humain", async () => {
    const user = userEvent.setup();
    const { requests } = createHarness({ withSample: true });

    expect(
      await screen.findByText(/Acquisition ≠ confirmation/),
    ).toBeInTheDocument();
    const stateInput = screen.getByLabelText("Décision G3 pour dddddddddddd");
    await user.selectOptions(stateInput, "confirmed");
    const confirm = screen.getByRole("button", { name: "Confirmer" });
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(requests.some((request) => request.url.endsWith("/qualify"))).toBe(
      false,
    );

    await user.type(
      screen.getByLabelText("Raison obligatoire pour dddddddddddd"),
      "La preuve corroborante confirme l’appartenance au cluster.",
    );
    await user.click(
      screen.getByLabelText(
        /Je confirme cette décision de qualification humaine/,
      ),
    );
    expect(confirm).toBeDisabled();
    await user.selectOptions(
      screen.getByLabelText("Preuves pour dddddddddddd"),
      EVIDENCE_ID,
    );
    await user.click(confirm);

    await waitFor(() =>
      expect(
        requests.filter((request) => request.url.endsWith("/qualify")),
      ).toHaveLength(1),
    );
    const confirmation = requests.find((request) =>
      request.url.endsWith("/qualify"),
    );
    expect(confirmation?.body).toMatchObject({
      expected_version: 1,
      qualification: "confirmed",
      reason: "La preuve corroborante confirme l’appartenance au cluster.",
      evidence_refs: [EVIDENCE_ID],
    });
    expect(confirmation?.body).not.toHaveProperty("actor_id");
    expect(confirmation?.idempotencyKey).toBeTruthy();

    await waitFor(() =>
      expect(
        screen.getByText(
          (text, element) =>
            element?.tagName === "STRONG" && text === "Confirmé",
        ),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/human analyst:test/)).toBeInTheDocument();
  });

  it("permet de rejeter avec preuve et de marquer lié, incertain ou candidat avec une raison", async () => {
    const user = userEvent.setup();
    const { requests } = createHarness({ withSample: true });
    await screen.findByText(/Acquisition ≠ confirmation/);
    const stateInput = screen.getByLabelText("Décision G3 pour dddddddddddd");
    const reasonInput = screen.getByLabelText(
      "Raison obligatoire pour dddddddddddd",
    );
    const confirmInput = screen.getByLabelText(
      /Je confirme cette décision de qualification humaine/,
    );

    for (const state of ["related", "uncertain", "candidate"] as const) {
      await user.selectOptions(stateInput, state);
      await user.type(reasonInput, `Justification ${state}.`);
      await user.click(confirmInput);
      const buttonName =
        state === "related"
          ? "Marquer lié"
          : state === "uncertain"
            ? "Marquer incertain"
            : "Marquer candidat";
      await user.click(screen.getByRole("button", { name: buttonName }));
      const label =
        state === "related"
          ? "Associé"
          : state === "uncertain"
            ? "Incertain"
            : "Candidat";
      await waitFor(() =>
        expect(
          screen.getByText(
            (text, element) => element?.tagName === "STRONG" && text === label,
          ),
        ).toBeInTheDocument(),
      );
    }

    await user.selectOptions(stateInput, "rejected");
    await user.type(reasonInput, "Les preuves contredisent l’appartenance.");
    await user.click(confirmInput);
    expect(screen.getByRole("button", { name: "Rejeter" })).toBeDisabled();
    await user.selectOptions(
      screen.getByLabelText("Preuves pour dddddddddddd"),
      EVIDENCE_ID,
    );
    await user.click(screen.getByRole("button", { name: "Rejeter" }));

    await waitFor(() =>
      expect(
        screen.getByText(
          (text, element) => element?.tagName === "STRONG" && text === "Rejeté",
        ),
      ).toBeInTheDocument(),
    );
    expect(
      requests.filter((request) => request.url.endsWith("/qualify")),
    ).toHaveLength(4);
    expect(
      requests
        .filter((request) => request.url.endsWith("/qualify"))
        .every((request) => request.idempotencyKey),
    ).toBe(true);
    expect(screen.getByText(/Justification related/)).toBeInTheDocument();
    expect(screen.getByText(/Justification uncertain/)).toBeInTheDocument();
    expect(
      screen.getByText(/Les preuves contredisent l’appartenance/),
    ).toBeInTheDocument();
  });
});
