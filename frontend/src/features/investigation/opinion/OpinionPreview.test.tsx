import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OpinionVersionV1 } from "./types";
import { OpinionPreview } from "./OpinionPreview";

const OPINION_ID = "22222222-2222-4222-8222-222222222222";
const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const CONTENT_SHA = "a".repeat(64);
const BASE_SHA = "b".repeat(64);
const PDF_CONTENT = "%PDF-1.7\npreview bytes";

function makeOpinion(
  overrides: Partial<OpinionVersionV1> = {},
): OpinionVersionV1 {
  const opinion: OpinionVersionV1 = {
    schema: "analyst-opinion.v1",
    id: OPINION_ID,
    version: 3,
    investigation_id: INVESTIGATION_ID,
    synthesis_artifact_id: "33333333-3333-4333-8333-333333333333",
    synthesis_sha256: "c".repeat(64),
    base_publication_artifact_id: "44444444-4444-4444-8444-444444444444",
    base_publication_sha256: BASE_SHA,
    blocks: [],
    unresolved_points: [],
    author_id: "system:opinion-writer",
    reviewer_id: null,
    status: "draft",
    created_at: "2026-11-23T00:00:00Z",
    content_sha256: CONTENT_SHA,
    renderable_blocks: [],
    provenance: {
      investigation_id: INVESTIGATION_ID,
      synthesis_artifact_id: "33333333-3333-4333-8333-333333333333",
      synthesis_sha256: "c".repeat(64),
      base_publication_artifact_id: "44444444-4444-4444-8444-444444444444",
      base_publication_sha256: BASE_SHA,
    },
  };
  return {
    ...opinion,
    ...overrides,
    provenance: { ...opinion.provenance, ...overrides.provenance },
  };
}

function response(body: string, contentType: string, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) =>
        name.toLowerCase() === "content-type" ? contentType : null,
    },
    blob: () => Promise.resolve(new Blob([body], { type: contentType })),
    json: () => Promise.resolve(JSON.parse(body) as unknown),
  } as Response;
}

function renderPreview(opinion = makeOpinion()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <OpinionPreview opinion={opinion} findings={[]} observations={[]} />
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

async function expectStatus(text: string) {
  await waitFor(() => {
    expect(screen.getByText(text, { exact: true })).toBeVisible();
  });
}

let originalCreateObjectUrl: PropertyDescriptor | undefined;
let originalRevokeObjectUrl: PropertyDescriptor | undefined;
let createObjectUrl: ReturnType<typeof vi.fn>;
let revokeObjectUrl: ReturnType<typeof vi.fn>;

beforeEach(() => {
  originalCreateObjectUrl = Object.getOwnPropertyDescriptor(
    URL,
    "createObjectURL",
  );
  originalRevokeObjectUrl = Object.getOwnPropertyDescriptor(
    URL,
    "revokeObjectURL",
  );
  createObjectUrl = vi.fn(() => "blob:opinion-preview");
  revokeObjectUrl = vi.fn();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: createObjectUrl,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectUrl,
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  if (originalCreateObjectUrl === undefined) {
    Reflect.deleteProperty(URL, "createObjectURL");
  } else {
    Object.defineProperty(URL, "createObjectURL", originalCreateObjectUrl);
  }
  if (originalRevokeObjectUrl === undefined) {
    Reflect.deleteProperty(URL, "revokeObjectURL");
  } else {
    Object.defineProperty(URL, "revokeObjectURL", originalRevokeObjectUrl);
  }
});

describe("OpinionPreview PDF", () => {
  it("announces generation, validates %PDF-, shows the PDF and revokes its URL", async () => {
    let resolvePdf: (value: Response) => void = () => undefined;
    const pendingResponse = new Promise<Response>((resolve) => {
      resolvePdf = resolve;
    });
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementation(() => pendingResponse);
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = renderPreview();

    expect(screen.getByRole("status")).toHaveTextContent("Génération en cours");
    expect(
      screen.getByText("Chargement de l’aperçu…", { exact: false }),
    ).toBeVisible();

    resolvePdf(response(PDF_CONTENT, "application/pdf; charset=binary"));

    await expectStatus("PDF prêt");
    expect(screen.getByText("PDF disponible · 22 octets")).toBeVisible();
    expect(screen.getByRole("link", { name: "Télécharger" })).toHaveAttribute(
      "download",
      "avis-v3.pdf",
    );
    const viewer = screen.getByTestId("opinion-pdf-viewer");
    expect(
      within(viewer).getByRole("link", { name: "Ouvrir le PDF" }),
    ).toBeInTheDocument();
    const requestInput = fetchMock.mock.calls[0]?.[0];
    if (typeof requestInput !== "string") {
      throw new Error("Expected the opinion PDF URL to be a string.");
    }
    const requestUrl = requestInput;
    expect(requestUrl).toContain("version=3");
    expect(requestUrl).toContain(`opinion_sha256=${CONTENT_SHA}`);
    expect(requestUrl).toContain(`base_publication_sha256=${BASE_SHA}`);

    unmount();
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:opinion-preview");
  });

  it("fails clearly when a successful response is not a PDF", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(response("service unavailable", "text/html")),
    );

    renderPreview();

    await expectStatus("Échec de la génération");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "La réponse reçue n’est pas un PDF",
    );
    expect(screen.getByRole("button", { name: "Réessayer" })).toBeVisible();
    expect(screen.queryByTestId("opinion-pdf-viewer")).not.toBeInTheDocument();
    expect(createObjectUrl).not.toHaveBeenCalled();
  });

  it("falls back with a failed status when createObjectURL is unavailable", async () => {
    Reflect.deleteProperty(URL, "createObjectURL");
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(response(PDF_CONTENT, "application/pdf")),
    );

    renderPreview();

    await expectStatus("Échec de la génération");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Le lecteur PDF intégré n’est pas disponible",
    );
    expect(screen.getByRole("link", { name: "Ouvrir le PDF" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Télécharger" })).toBeVisible();
  });

  it("falls back with a failed status when createObjectURL throws", async () => {
    createObjectUrl.mockImplementation(() => {
      throw new Error("URL API unavailable");
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(response(PDF_CONTENT, "application/pdf")),
    );

    renderPreview();

    await expectStatus("Échec de la génération");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Le lecteur PDF intégré n’est pas disponible",
    );
    expect(screen.getByRole("link", { name: "Ouvrir le PDF" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Télécharger" })).toBeVisible();
  });

  it("rejects an application/pdf response without the %PDF- signature", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(response("not a PDF", "application/pdf")),
    );

    renderPreview();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "ne contient pas la signature d’un PDF valide",
    );
    await expectStatus("Échec de la génération");
    expect(screen.queryByTestId("opinion-pdf-viewer")).not.toBeInTheDocument();
  });

  it("marks a previously loaded PDF stale when the opinion version changes", async () => {
    let resolveNextPdf: (value: Response) => void = () => undefined;
    const nextPdf = new Promise<Response>((resolve) => {
      resolveNextPdf = resolve;
    });
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(response(PDF_CONTENT, "application/pdf"))
      .mockImplementationOnce(() => nextPdf);
    vi.stubGlobal("fetch", fetchMock);

    const { rerender, queryClient } = renderPreview();
    await expectStatus("PDF prêt");

    rerender(
      <QueryClientProvider client={queryClient}>
        <OpinionPreview
          opinion={makeOpinion({ version: 4, content_sha256: "d".repeat(64) })}
          findings={[]}
          observations={[]}
        />
      </QueryClientProvider>,
    );

    await expectStatus("Obsolète");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Le PDF chargé correspond à l’avis v3",
    );
    expect(screen.queryByTestId("opinion-pdf-viewer")).not.toBeInTheDocument();
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:opinion-preview");

    resolveNextPdf(response("%PDF-1.7\nnew version", "application/pdf"));
  });

  it("retries a failed request and displays the new PDF", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response(
          '{"detail":{"message":"Typst indisponible"}}',
          "application/json",
          503,
        ),
      )
      .mockResolvedValueOnce(response(PDF_CONTENT, "application/pdf"));
    vi.stubGlobal("fetch", fetchMock);

    renderPreview();

    await expectStatus("Échec de la génération");
    expect(screen.getByRole("alert")).toHaveTextContent("Typst indisponible");

    await userEvent.click(screen.getByRole("button", { name: "Réessayer" }));

    await expectStatus("PDF prêt");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
