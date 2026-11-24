import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OpinionVersionV1 } from "./types";
import { createTestQueryClient, wrapperFor } from "../api/testSupport";
import { OpinionPdfPreview } from "./OpinionPdfPreview";

const OPINION_ID = "11111111-1111-4111-8111-111111111111";
const INVESTIGATION_ID = "22222222-2222-4222-8222-222222222222";
const CONTENT_SHA = "a".repeat(64);
const BASE_SHA = "b".repeat(64);
const PDF_CONTENT = "%PDF-1.7\npreview bytes";
const PREVIEW_URL = "/api/opinions/" + OPINION_ID + "/preview.pdf";

function makeOpinion(): OpinionVersionV1 {
  return {
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
}

function pdfResponse(): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => "application/pdf" },
    blob: () =>
      Promise.resolve(new Blob([PDF_CONTENT], { type: "application/pdf" })),
  } as unknown as Response;
}

function renderPdfPreview() {
  const queryClient = createTestQueryClient();
  return render(
    <OpinionPdfPreview opinion={makeOpinion()} previewUrl={PREVIEW_URL} />,
    { wrapper: wrapperFor(queryClient) },
  );
}

let originalCreateObjectUrl: PropertyDescriptor | undefined;
let originalRevokeObjectUrl: PropertyDescriptor | undefined;
let originalPdfViewerEnabled: PropertyDescriptor | undefined;

beforeEach(() => {
  originalCreateObjectUrl = Object.getOwnPropertyDescriptor(
    URL,
    "createObjectURL",
  );
  originalRevokeObjectUrl = Object.getOwnPropertyDescriptor(
    URL,
    "revokeObjectURL",
  );
  originalPdfViewerEnabled = Object.getOwnPropertyDescriptor(
    navigator,
    "pdfViewerEnabled",
  );
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:opinion-preview"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
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
  if (originalPdfViewerEnabled === undefined) {
    Reflect.deleteProperty(navigator, "pdfViewerEnabled");
  } else {
    Object.defineProperty(
      navigator,
      "pdfViewerEnabled",
      originalPdfViewerEnabled,
    );
  }
});

describe("OpinionPdfPreview", () => {
  it("keeps version and generation status visible while the PDF request is pending", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => new Promise<Response>(() => undefined)),
    );
    renderPdfPreview();

    expect(
      screen.getByRole("heading", {
        name: "Aperçu PDF réel de l’avis v3",
      }),
    ).toBeVisible();
    expect(screen.getByText("Préparation du PDF")).toBeVisible();
    expect(screen.getByText("v3")).toBeVisible();
    expect(screen.getByText("En attente de génération")).toBeVisible();
    expect(screen.queryByTestId("opinion-pdf-viewer")).not.toBeInTheDocument();
  });

  it("offers styled open and download actions when headless Chromium cannot render PDF", async () => {
    Object.defineProperty(navigator, "pdfViewerEnabled", {
      configurable: true,
      value: false,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(pdfResponse()),
    );
    renderPdfPreview();

    expect(
      await screen.findByText(/Ce navigateur n’a pas affiché le PDF intégré/),
    ).toBeVisible();
    expect(screen.getByText("PDF prêt")).toBeVisible();
    expect(screen.getByText("22 octets")).toBeVisible();
    expect(screen.queryByTestId("opinion-pdf-viewer")).not.toBeInTheDocument();
    const open = screen.getByRole("link", { name: "Ouvrir le PDF" });
    const download = screen.getByRole("link", { name: "Télécharger" });
    expect(open).toHaveAttribute("href", PREVIEW_URL);
    expect(open).toHaveClass("button", "button--secondary");
    expect(download).toHaveAttribute("href", PREVIEW_URL);
    expect(download).toHaveAttribute("download", "avis-analyste.pdf");
  });
});
