import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationEnvelopeView } from "../../../api/investigationGovernance";
import { EnvelopeLifecycleSection } from "./EnvelopeLifecycleSection";

const INVESTIGATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const DECISION_ID = "66666666-6666-4666-8666-666666666666";
const POLICY_ID = "77777777-7777-4777-8777-777777777777";
const POLICY_EVENT_ID = "88888888-8888-4888-8888-888888888888";
const POLICY_SUBJECT_ID = "99999999-9999-4999-8999-999999999999";
const CORRELATION_ID = "11111111-1111-4111-8111-111111111111";

type LifecycleEnvelope = InvestigationEnvelopeView & {
  policy_activated_by: string | null;
  authority_kind: string | null;
  inherited_policy_id: string | null;
  inherited_policy_version: number | null;
  inherited_policy_event_id: string | null;
  inherited_subject_id: string | null;
  policy_status: string | null;
  currently_authorized: boolean;
};

function makeEnvelope(
  overrides: Partial<LifecycleEnvelope> = {},
): LifecycleEnvelope {
  return {
    investigation_id: INVESTIGATION_ID,
    approved: true,
    version: 1,
    allowed_providers: ["vt", "bridge"],
    available_providers: ["vt", "bridge", "shodan"],
    acquire_hits_allowed: true,
    max_acquire_volume: 2,
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
    disclosure: {
      tlp: "amber",
      do_not_submit: true,
      external_llm_allowed: false,
    },
    approval_decision_id: DECISION_ID,
    approval_actor_id: "analyst:test",
    approved_at: "2026-11-22T10:00:00Z",
    policy_activated_by: null,
    authority_kind: "human_g2",
    inherited_policy_id: null,
    inherited_policy_version: null,
    inherited_policy_event_id: null,
    inherited_subject_id: null,
    policy_status: null,
    currently_authorized: true,
    ...overrides,
  };
}

interface HarnessOptions {
  initial?: LifecycleEnvelope;
  extendError?: { status: number; code: string };
  revokeError?: { status: number; code: string };
  deferExtend?: boolean;
}

function errorResponse(status: number, code: string) {
  return Response.json(
    { detail: { code, message: `Refus simulé ${code}` } },
    { status },
  );
}

function applyExtension(
  envelope: LifecycleEnvelope,
  payload: Record<string, unknown>,
): LifecycleEnvelope {
  const budget = { ...envelope.budget };
  const limits = payload.budget_limits;
  if (typeof limits === "object" && limits !== null) {
    for (const [dimension, limit] of Object.entries(limits)) {
      if (
        dimension in budget &&
        typeof limit === "number" &&
        Number.isFinite(limit)
      ) {
        const key = dimension as keyof typeof budget;
        const current = budget[key];
        budget[key] = {
          ...current,
          limit,
          remaining: Math.max(limit - current.consumed - current.reserved, 0),
        };
      }
    }
  }
  return {
    ...envelope,
    version: envelope.version + 1,
    allowed_providers:
      (payload.allowed_providers as string[] | undefined) ??
      envelope.allowed_providers,
    acquire_hits_allowed:
      typeof payload.acquire_hits_allowed === "boolean"
        ? payload.acquire_hits_allowed
        : envelope.acquire_hits_allowed,
    max_acquire_volume: budget.downloads.limit ?? 0,
    currently_authorized: true,
  };
}

function createHarness(options: HarnessOptions = {}) {
  const requests: Array<{
    url: string;
    method: string;
    body: unknown;
    idempotencyKey: string | null;
  }> = [];
  let envelope = options.initial ?? makeEnvelope();
  let extendCount = 0;
  let pendingExtend: (() => void) | null = null;

  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const method = init?.method ?? "GET";
    let body: unknown = null;
    if (typeof init?.body === "string") {
      body = JSON.parse(init.body) as unknown;
    }
    requests.push({
      url,
      method,
      body,
      idempotencyKey: new Headers(init?.headers).get("Idempotency-Key"),
    });

    if (url.endsWith("/envelope") && method === "GET") {
      return Promise.resolve(Response.json(envelope));
    }
    if (url.endsWith("/envelope/extend") && method === "POST") {
      extendCount += 1;
      if (options.extendError) {
        return Promise.resolve(
          errorResponse(options.extendError.status, options.extendError.code),
        );
      }
      envelope = applyExtension(
        envelope,
        (body ?? {}) as Record<string, unknown>,
      );
      if (options.deferExtend) {
        return new Promise<Response>((resolve) => {
          pendingExtend = () => resolve(Response.json(envelope));
        });
      }
      return Promise.resolve(Response.json(envelope));
    }
    if (url.endsWith("/envelope/revoke") && method === "POST") {
      if (options.revokeError) {
        return Promise.resolve(
          errorResponse(options.revokeError.status, options.revokeError.code),
        );
      }
      envelope = {
        ...envelope,
        version: envelope.version + 1,
        allowed_providers: [],
        acquire_hits_allowed: false,
        max_acquire_volume: 0,
        approval_decision_id: DECISION_ID,
        approved_at: "2026-11-22T11:00:00Z",
        currently_authorized: false,
      };
      return Promise.resolve(Response.json(envelope));
    }
    return Promise.resolve(errorResponse(500, "unexpected_request"));
  });

  return {
    fetchMock,
    requests,
    extendCount: () => extendCount,
    resolveExtend: () => pendingExtend?.(),
  };
}

function renderSection(options: HarnessOptions = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  vi.stubGlobal("crypto", { randomUUID: () => CORRELATION_ID });
  const harness = createHarness(options);
  vi.stubGlobal("fetch", harness.fetchMock);
  const result = render(
    <QueryClientProvider client={client}>
      <EnvelopeLifecycleSection investigationId={INVESTIGATION_ID} />
    </QueryClientProvider>,
  );
  return { ...result, ...harness };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EnvelopeLifecycleSection", () => {
  it("ne monte aucune action tant que l’enveloppe n’est pas approuvée", async () => {
    const { requests } = renderSection({
      initial: makeEnvelope({
        approved: false,
        version: 0,
        allowed_providers: [],
        acquire_hits_allowed: false,
        max_acquire_volume: 0,
        approval_decision_id: null,
        approval_actor_id: null,
        approved_at: null,
        authority_kind: null,
        currently_authorized: false,
      }),
    });

    await waitFor(() =>
      expect(
        requests.some(
          (request) =>
            request.method === "GET" && request.url.endsWith("/envelope"),
        ),
      ).toBe(true),
    );
    expect(
      screen.queryByRole("heading", {
        name: "Cycle de vie de l’enveloppe G2",
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Étendre l’enveloppe" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Révoquer l’enveloppe" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /Approuver/ })).toBeNull();
  });

  it("affiche les droits réels, le budget et la politique héritée", async () => {
    renderSection({
      initial: makeEnvelope({
        authority_kind: "osint_policy",
        policy_status: "active",
        inherited_policy_id: POLICY_ID,
        inherited_policy_version: 3,
        inherited_policy_event_id: POLICY_EVENT_ID,
        inherited_subject_id: POLICY_SUBJECT_ID,
        policy_activated_by: "analyst:policy",
      }),
    });

    expect(
      await screen.findByRole("heading", {
        name: "Cycle de vie de l’enveloppe G2",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Enveloppe G2 active — version 1"),
    ).toBeInTheDocument();
    expect(screen.getByText("Droits autorisés")).toBeInTheDocument();
    expect(screen.getAllByText("autorisées")).toHaveLength(2);
    expect(
      screen.getByText("autorisée · 2 fichier(s) maximum"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/soumission du contenu : interdite \(do_not_submit\)/),
    ).toBeInTheDocument();
    expect(screen.getByText("Unités VirusTotal")).toBeInTheDocument();
    expect(
      screen.getByText(/1 consommé · 0 réservé · plafond 10 · restant 9/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Les invocations d’outils des opérateurs locaux/),
    ).toBeInTheDocument();

    expect(
      screen.getByRole("heading", { name: "Politique héritée" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Politique OSINT héritée")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText(POLICY_ID)).toBeInTheDocument();
    expect(screen.getByText(/version 3/)).toBeInTheDocument();
    expect(screen.getByText(POLICY_EVENT_ID)).toBeInTheDocument();
    expect(screen.getByText(POLICY_SUBJECT_ID)).toBeInTheDocument();
    expect(screen.getByText("Événement de politique")).toBeInTheDocument();
    expect(screen.getByText("Activée par")).toBeInTheDocument();
    expect(screen.getByText("analyst:policy")).toBeInTheDocument();
  });

  it("envoie l’extension avec le CAS, les droits complets et une clé d’idempotence", async () => {
    const user = userEvent.setup();
    const { requests } = renderSection();

    await user.click(
      await screen.findByRole("button", { name: "Étendre l’enveloppe" }),
    );
    await user.type(
      screen.getByLabelText("Motif de l’extension"),
      "Un budget VT supplémentaire est nécessaire pour cette piste.",
    );
    await user.click(screen.getByLabelText(/Je confirme cette extension/));
    const submit = screen.getByRole("button", {
      name: "Confirmer l’extension",
    });
    expect(submit).toBeEnabled();
    await user.click(submit);

    expect(
      await screen.findByText("Enveloppe étendue — version 2"),
    ).toBeInTheDocument();
    const post = requests.find(
      (request) =>
        request.method === "POST" && request.url.endsWith("/envelope/extend"),
    );
    if (!post) throw new Error("extension request missing");
    expect(post.idempotencyKey).toBe(CORRELATION_ID);
    const body = post.body as Record<string, unknown>;
    expect(body).toMatchObject({
      expected_version: 1,
      allowed_providers: ["vt", "bridge"],
      acquire_hits_allowed: true,
      max_acquire_volume: 2,
      disclosure: {
        tlp: null,
        do_not_submit: true,
        external_llm_allowed: false,
      },
      reason: "Un budget VT supplémentaire est nécessaire pour cette piste.",
      correlation_id: CORRELATION_ID,
    });
    expect(body.budget_limits).toMatchObject({
      bridge_turns: 8,
      vt_units: 10,
      downloads: 2,
      tool_seconds: 120,
      context_tokens: 20_000,
      context_bytes: 100_000,
    });
    expect(body).not.toHaveProperty("investigation_id");
  });

  it("exige une confirmation explicite, gère le focus et Échap, puis n’offre aucune ré-approbation", async () => {
    const user = userEvent.setup();
    const { requests } = renderSection();

    const trigger = await screen.findByRole("button", {
      name: "Révoquer l’enveloppe",
    });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", {
      name: "Révoquer l’enveloppe G2 ?",
    });
    expect(dialog).toBeInTheDocument();
    const reasonInput = screen.getByLabelText(
      "Motif obligatoire de la révocation",
    );
    expect(reasonInput).toHaveFocus();

    const confirm = screen.getByRole("button", {
      name: "Confirmer la révocation",
    });
    expect(confirm).toBeDisabled();
    await user.type(reasonInput, "Les recherches externes sont terminées.");
    expect(confirm).toBeDisabled();
    await user.click(
      screen.getByLabelText(/Je confirme la révocation de l’enveloppe/),
    );
    expect(confirm).toBeEnabled();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
    expect(
      requests.some((request) => request.url.endsWith("/envelope/revoke")),
    ).toBe(false);

    await user.click(trigger);
    await user.type(
      screen.getByLabelText("Motif obligatoire de la révocation"),
      "Les recherches externes sont terminées.",
    );
    await user.click(
      screen.getByLabelText(/Je confirme la révocation de l’enveloppe/),
    );
    await user.click(
      screen.getByRole("button", { name: "Confirmer la révocation" }),
    );

    expect(
      await screen.findByText("Enveloppe révoquée — droits externes coupés"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("button", { name: /Approuver/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Étendre l’enveloppe" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Révoquer l’enveloppe" }),
    ).toBeNull();

    const revocation = requests.find(
      (request) =>
        request.method === "POST" && request.url.endsWith("/envelope/revoke"),
    );
    if (!revocation) throw new Error("revocation request missing");
    expect(revocation.idempotencyKey).toBe(CORRELATION_ID);
    expect(revocation.body).toMatchObject({
      expected_version: 1,
      reason: "Les recherches externes sont terminées.",
      correlation_id: CORRELATION_ID,
    });
    expect(revocation.body).not.toHaveProperty("investigation_id");
  });

  it("affiche l’état révoqué renvoyé par le serveur sans action de ré-approbation", async () => {
    renderSection({
      initial: makeEnvelope({
        version: 2,
        allowed_providers: [],
        acquire_hits_allowed: false,
        max_acquire_volume: 0,
        currently_authorized: false,
      }),
    });

    expect(
      await screen.findByText("Enveloppe révoquée — droits externes coupés"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Droits externes actifs : non/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approuver/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Étendre l’enveloppe" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Révoquer l’enveloppe" }),
    ).toBeNull();
  });

  it("affiche le conflit CAS et recharge l’enveloppe", async () => {
    const user = userEvent.setup();
    const { requests } = renderSection({
      extendError: { status: 409, code: "envelope_conflict" },
    });

    await user.click(
      await screen.findByRole("button", { name: "Étendre l’enveloppe" }),
    );
    await user.type(
      screen.getByLabelText("Motif de l’extension"),
      "Extension demandée avec une version périmée.",
    );
    await user.click(screen.getByLabelText(/Je confirme cette extension/));
    await user.click(
      screen.getByRole("button", { name: "Confirmer l’extension" }),
    );

    expect(await screen.findByText("Conflit de version")).toBeInTheDocument();
    expect(
      screen.getByText(/Refus simulé envelope_conflict/),
    ).toBeInTheDocument();
    const getsBefore = requests.filter(
      (request) =>
        request.method === "GET" && request.url.endsWith("/envelope"),
    ).length;
    await user.click(
      screen.getByRole("button", { name: "Recharger l’enveloppe" }),
    );
    await waitFor(() =>
      expect(
        requests.filter(
          (request) =>
            request.method === "GET" && request.url.endsWith("/envelope"),
        ).length,
      ).toBeGreaterThan(getsBefore),
    );
  });

  it("affiche un refus typé non conflictuel avec le code backend", async () => {
    const user = userEvent.setup();
    renderSection({
      extendError: { status: 422, code: "invalid_envelope" },
    });

    await user.click(
      await screen.findByRole("button", { name: "Étendre l’enveloppe" }),
    );
    await user.type(
      screen.getByLabelText("Motif de l’extension"),
      "Extension invalide pour vérifier le refus.",
    );
    await user.click(screen.getByLabelText(/Je confirme cette extension/));
    await user.click(
      screen.getByRole("button", { name: "Confirmer l’extension" }),
    );

    expect(await screen.findByText("Extension refusée")).toBeInTheDocument();
    expect(screen.getByText("invalid_envelope")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Recharger l’enveloppe" }),
    ).toBeInTheDocument();
  });

  it("ne soumet qu’une seule requête sur un double-clic", async () => {
    const user = userEvent.setup();
    const harness = renderSection({ deferExtend: true });

    await user.click(
      await screen.findByRole("button", { name: "Étendre l’enveloppe" }),
    );
    await user.type(
      screen.getByLabelText("Motif de l’extension"),
      "Une seule soumission attendue.",
    );
    await user.click(screen.getByLabelText(/Je confirme cette extension/));
    const submit = screen.getByRole("button", {
      name: "Confirmer l’extension",
    });
    expect(submit).toBeEnabled();
    const form = submit.closest("form");
    if (!form) throw new Error("extension form missing");
    fireEvent.submit(form);
    fireEvent.submit(form);

    await waitFor(() => expect(harness.extendCount()).toBe(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(harness.extendCount()).toBe(1);
    harness.resolveExtend();
    expect(
      await screen.findByText("Enveloppe étendue — version 2"),
    ).toBeInTheDocument();
  });
});
