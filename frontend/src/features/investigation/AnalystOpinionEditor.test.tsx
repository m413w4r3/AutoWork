import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  AnalystOpinionResponse,
  AnalystOpinionVersion,
} from "../../api/analystOpinion";
import type { InvestigationOpinionDraft } from "../../api/investigation";
import { AnalystOpinionEditor } from "./AnalystOpinionEditor";

const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const OPINION_ID = "22222222-2222-4222-8222-222222222222";
const CONTENT_HASH = "a".repeat(64);
const BASE_HASH = "b".repeat(64);
const FINDING_ID = "33333333-3333-4333-8333-333333333333";
const EVIDENCE_ID = "44444444-4444-4444-8444-444444444444";
const PROTOCOL_MODEL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function requestBody(init?: RequestInit): unknown {
  const body = init?.body;
  return typeof body === "string" ? (JSON.parse(body) as unknown) : body;
}

function makeOpinion(
  status: AnalystOpinionVersion["status"] = "draft",
): AnalystOpinionVersion {
  return {
    schema: "analyst-opinion.v1",
    id: OPINION_ID,
    version: 2,
    investigation_id: INVESTIGATION_ID,
    synthesis_artifact_id: "55555555-5555-4555-8555-555555555555",
    synthesis_sha256: "c".repeat(64),
    base_publication_artifact_id: "66666666-6666-4666-8666-666666666666",
    base_publication_sha256: BASE_HASH,
    blocks: [
      {
        type: "key_findings",
        text: "Une variante distincte est étayée.",
        finding_refs: [FINDING_ID],
        evidence_refs: [EVIDENCE_ID],
        qualification: "fact",
        certainty: 0.9,
      },
    ],
    unresolved_points: ["La date de première apparition reste inconnue."],
    author_id: "analyst:writer",
    reviewer_id: status === "approved" ? "analyst:reviewer" : null,
    status,
    created_at: "2026-10-13T09:00:00Z",
    content_sha256: CONTENT_HASH,
    renderable_blocks: [
      {
        type: "key_findings",
        text: "Une variante distincte est étayée.",
        finding_refs: [FINDING_ID],
        evidence_refs: [EVIDENCE_ID],
        qualification: "fact",
        certainty: 0.9,
      },
    ],
    provenance: {
      investigation_id: INVESTIGATION_ID,
      synthesis_artifact_id: "55555555-5555-4555-8555-555555555555",
      synthesis_sha256: "c".repeat(64),
      base_publication_artifact_id: "66666666-6666-4666-8666-666666666666",
      base_publication_sha256: BASE_HASH,
    },
  };
}

function draftSummary(
  opinion: AnalystOpinionVersion,
): InvestigationOpinionDraft {
  return {
    id: opinion.id,
    version: opinion.version,
    status: opinion.status,
    content_sha256: opinion.content_sha256,
    blocks: opinion.blocks,
    unresolved_points: opinion.unresolved_points,
    created_at: opinion.created_at,
  };
}

function responseFor(opinion: AnalystOpinionVersion): AnalystOpinionResponse {
  return {
    draft: opinion,
    history: [opinion],
    citation_violations: [],
    renderable_blocks: opinion.renderable_blocks,
    provenance: opinion.provenance,
  };
}

function renderEditor(
  opinion: AnalystOpinionVersion,
  currentDraft: InvestigationOpinionDraft | null = draftSummary(opinion),
  citationViolations: AnalystOpinionResponse["citation_violations"] = [],
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let currentOpinion = opinion;
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    calls.push({ url, init });
    if (url === `/api/investigations/${INVESTIGATION_ID}/protocol-models`) {
      return Promise.resolve(
        Response.json({
          investigation_id: INVESTIGATION_ID,
          models: [
            {
              blob_id: PROTOCOL_MODEL_ID,
              sha256: "9".repeat(64),
              schema_version: "ProtocolModelV1",
              model_version: "1",
              protocol: "test protocol",
              transport: "tcp",
              validation_status: "observed",
              family_assurance: "medium",
              sample_sha256s: [],
              message_count: 1,
              field_count: 1,
              transition_count: 0,
            },
          ],
          truncated: false,
        }),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/opinion`) {
      return Promise.resolve(
        Response.json({
          ...responseFor(currentOpinion),
          citation_violations: citationViolations,
        }),
      );
    }
    if (
      url === `/api/opinions/${OPINION_ID}/revise` &&
      init?.method === "POST"
    ) {
      const body = requestBody(init) as { expected_version: number };
      currentOpinion = {
        ...currentOpinion,
        version: body.expected_version + 1,
        content_sha256: "d".repeat(64),
      };
      return Promise.resolve(Response.json({ draft: currentOpinion }));
    }
    if (
      url === `/api/opinions/${OPINION_ID}/approve` &&
      init?.method === "POST"
    ) {
      currentOpinion = {
        ...currentOpinion,
        version: currentOpinion.version + 1,
        status: "approved",
      };
      return Promise.resolve(
        Response.json({
          approved: currentOpinion,
          reviewed_version: currentOpinion.version - 1,
          approved_version: currentOpinion.version,
        }),
      );
    }
    if (
      url === `/api/opinions/${OPINION_ID}/publish` &&
      init?.method === "POST"
    ) {
      return Promise.resolve(
        Response.json({
          publication: {
            artifact_id: "77777777-7777-4777-8777-777777777777",
            run_id: "88888888-8888-4888-8888-888888888888",
            subject_id: "99999999-9999-4999-8999-999999999999",
            version: 2,
            sha256: "e".repeat(64),
            previous_publication_artifact_id:
              "66666666-6666-4666-8666-666666666666",
          },
          composition_repin_required: true,
          message:
            "Nouvelle PUBLICATION créée. Le réépinglage dans une composition reste une action de l’éditeur.",
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
  vi.stubGlobal("fetch", fetchMock);
  const result = render(
    <QueryClientProvider client={client}>
      <AnalystOpinionEditor
        investigationId={INVESTIGATION_ID}
        currentDraft={currentDraft}
        compositionHref="/editions/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/composition"
      />
    </QueryClientProvider>,
  );
  return { ...result, calls };
}

afterEach(() => vi.unstubAllGlobals());

describe("AnalystOpinionEditor", () => {
  it("montre sans les masquer les violations de citation et bloque l’approbation", async () => {
    const opinion = makeOpinion();
    renderEditor(opinion, draftSummary(opinion), [
      {
        code: "fact_without_supported_finding",
        block_type: "key_findings",
        reference_id: null,
        message: "Un fait exige un constat étayé et une observation existante.",
      },
    ]);

    expect(
      await screen.findByText(/Violations de citation à corriger/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/fact_without_supported_finding/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approuver cette version précise" }),
    ).toBeDisabled();
  });

  it("désactive l’approbation si le hash affiché diffère du brouillon courant", async () => {
    const opinion = makeOpinion();
    const staleCurrentDraft = {
      ...draftSummary(opinion),
      content_sha256: "f".repeat(64),
    };
    renderEditor(opinion, staleCurrentDraft);

    expect(
      await screen.findByText(/ne correspond plus au brouillon courant/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approuver cette version précise" }),
    ).toBeDisabled();
  });

  it("sauvegarde une révision sous CAS avec la clé d’idempotence", async () => {
    const opinion = makeOpinion();
    const { calls } = renderEditor(opinion);
    const user = userEvent.setup();

    const text = await screen.findByLabelText("Texte du bloc");
    await user.clear(text);
    await user.type(text, "Texte éditorial révisé.");
    await user.click(
      screen.getByRole("button", {
        name: /Enregistrer la révision \(CAS v2\)/,
      }),
    );

    await waitFor(() =>
      expect(
        calls.some((call) => call.url === `/api/opinions/${OPINION_ID}/revise`),
      ).toBe(true),
    );
    const call = calls.find(
      (item) => item.url === `/api/opinions/${OPINION_ID}/revise`,
    );
    expect(requestBody(call?.init)).toMatchObject({
      expected_version: 2,
      blocks: [
        expect.objectContaining({
          block_type: "key_findings",
          text: "Texte éditorial révisé.",
        }),
      ],
    });
    expect(
      new Headers(call?.init?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
  });

  it("attache les vues choisies à une nouvelle version précise de l’avis", async () => {
    const opinion = makeOpinion();
    const { calls } = renderEditor(opinion);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", {
        name: "Attacher les figures à une nouvelle révision",
      }),
    );

    await waitFor(() => {
      const revision = calls.find(
        (call) =>
          call.url === `/api/opinions/${OPINION_ID}/revise` &&
          call.init?.method === "POST",
      );
      expect(requestBody(revision?.init)).toMatchObject({
        expected_version: opinion.version,
        protocol_model_blob_id: PROTOCOL_MODEL_ID,
        protocol_views: ["sequence", "state_machine", "message_format"],
      });
      expect(
        new Headers(revision?.init?.headers).get("Idempotency-Key"),
      ).toBeTruthy();
    });
  });

  it("approuve la version et le hash précis soumis", async () => {
    const opinion = makeOpinion();
    const { calls } = renderEditor(opinion);
    const user = userEvent.setup();

    await screen.findByText(/draft/);
    await user.type(
      screen.getByLabelText("Raison de la décision"),
      "Citations vérifiées.",
    );
    await user.click(
      screen.getByRole("button", { name: "Approuver cette version précise" }),
    );

    await waitFor(() =>
      expect(
        calls.some(
          (call) => call.url === `/api/opinions/${OPINION_ID}/approve`,
        ),
      ).toBe(true),
    );
    const call = calls.find(
      (item) => item.url === `/api/opinions/${OPINION_ID}/approve`,
    );
    expect(requestBody(call?.init)).toEqual({
      version: 2,
      expected_sha256: CONTENT_HASH,
      reason: "Citations vérifiées.",
    });
    expect(
      new Headers(call?.init?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
  });

  it("publie l’avis approuvé puis demande explicitement un repin humain", async () => {
    const opinion = makeOpinion("approved");
    const { calls } = renderEditor(opinion);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", {
        name: "Proposer la version augmentée",
      }),
    );
    expect(
      await screen.findByRole("heading", {
        name: "Version augmentée proposée",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Repin à faire dans la composition/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", {
        name: "Ouvrir la composition pour réépingler l’article",
      }),
    ).toHaveAttribute(
      "href",
      "/editions/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/composition",
    );
    const call = calls.find(
      (item) => item.url === `/api/opinions/${OPINION_ID}/publish`,
    );
    expect(requestBody(call?.init)).toEqual({
      version: 2,
      expected_base_publication_sha256: BASE_HASH,
    });
  });
});
