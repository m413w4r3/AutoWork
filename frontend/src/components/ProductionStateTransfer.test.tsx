import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  ProductionStatus,
  ProductionStateSnapshotV2,
  ProductionStateSnapshotV1,
} from "../api/production";
import { ProductionStateTransfer } from "./ProductionStateTransfer";

const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const snapshot: ProductionStateSnapshotV1 = {
  format: "autowork.production-state",
  schema_version: 1,
  exported_at: "2026-08-20T12:34:56Z",
  origin: {
    subject_title: "Campagne d’Iran",
    editorial_type: "brief",
    profile: "brief_auto",
    research_date: null,
  },
  artifacts: {
    references: { input_hash: "a", canonical_content: {} },
    extraction: {
      input_hash: "b",
      canonical_content: {
        schema_version: "2",
        parser_version: "test",
        items: [],
        uncertainties: [],
      },
    },
    synthesis: { input_hash: "c", rendered_content: "Synthèse" },
  },
  content_sha256: "d",
};

const snapshotV2: ProductionStateSnapshotV2 = {
  ...snapshot,
  schema_version: 2,
  origin: {
    subject_title: "Campagne d’Iran",
    research_date: null,
  },
};

function productionStatus(
  status: ProductionStatus["status"],
  title = "Campagne d’Iran",
): ProductionStatus {
  return {
    subject_id: SUBJECT_ID,
    edition_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    title,
    status,
    current_stage: "assembly",
    progress_current: 5,
    progress_total: 5,
    references_conversation_id: null,
    synthesis_conversation_id: null,
    run_id: "run-1",
    pipeline_generation: 1,
    created_at: "2026-08-20T12:00:00Z",
    started_at: "2026-08-20T12:00:00Z",
    finished_at: "2026-08-20T12:30:00Z",
    error_code: null,
    error_message: null,
    error_details: null,
    recovery_disposition: "manual_only",
    warnings: [],
    stages: {},
  };
}

function renderTransfer(status: ProductionStatus | null) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProductionStateTransfer
        subjectId={SUBJECT_ID}
        productionStatus={status}
      />
    </QueryClientProvider>,
  );
}

function responseFor(input: RequestInfo | URL, init?: RequestInit): Response {
  const url =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;
  if (url.endsWith("/state/export")) return Response.json(snapshotV2);
  if (url.endsWith("/state/import") && init?.method === "POST") {
    return Response.json({
      run_id: "run-imported",
      status: "needs_review",
      current_stage: "assembly",
      imported_stages: ["references", "extraction", "synthesis"],
      schema_version: 2,
      content_sha256: "d",
    });
  }
  return Response.json({});
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ProductionStateTransfer", () => {
  it("désactive l’export sans production et active l’import", () => {
    renderTransfer(null);
    expect(
      screen.getByRole("button", { name: "Exporter l’état" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("Importer un état")).toBeEnabled();
  });

  it("désactive les deux contrôles pendant une production en cours", () => {
    renderTransfer(productionStatus("running"));
    expect(
      screen.getByRole("button", { name: "Exporter l’état" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("Importer un état")).toBeDisabled();
    expect(
      screen.getByText(
        "L’import est indisponible pendant une production en cours.",
      ),
    ).toBeInTheDocument();
  });

  it("autorise les deux contrôles pour un run terminal", () => {
    renderTransfer(productionStatus("ready"));
    expect(
      screen.getByRole("button", { name: "Exporter l’état" }),
    ).toBeEnabled();
    expect(screen.getByLabelText("Importer un état")).toBeEnabled();
  });

  it("affiche un aperçu sans poster avant la confirmation", async () => {
    const fetchMock = vi.fn(responseFor);
    vi.stubGlobal("fetch", fetchMock);
    renderTransfer(productionStatus("ready"));
    const file = new File([JSON.stringify(snapshot)], "state.json", {
      type: "application/json",
    });
    await userEvent.upload(screen.getByLabelText("Importer un état"), file);

    expect(await screen.findByText("État prêt à importer")).toBeInTheDocument();
    expect(
      screen.getByText("Sujet d’origine : Campagne d’Iran"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Exporté le :/)).toBeInTheDocument();
    expect(screen.getAllByText(/✓/)).toHaveLength(3);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("recharge immédiatement le V2 produit par l’export et l’importe", async () => {
    const fetchMock = vi.fn(responseFor);
    vi.stubGlobal("fetch", fetchMock);
    let exportedBlob: Blob | undefined;
    const createObjectURL = vi.fn((blob: Blob) => {
      exportedBlob = blob;
      return "blob:round-trip";
    });
    Object.defineProperty(URL, "createObjectURL", {
      value: createObjectURL,
      configurable: true,
    });
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "revokeObjectURL", {
      value: revokeObjectURL,
      configurable: true,
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );
    renderTransfer(productionStatus("ready"));

    await userEvent.click(
      screen.getByRole("button", { name: "Exporter l’état" }),
    );
    await waitFor(() => expect(exportedBlob).toBeDefined());
    const blob = exportedBlob;
    if (!blob) throw new Error("Export did not create a Blob");
    const exportedJson = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        if (typeof reader.result === "string") resolve(reader.result);
        else reject(new Error("Exported Blob was not text"));
      };
      reader.onerror = () =>
        reject(reader.error ?? new Error("Unable to read Blob"));
      reader.readAsText(blob);
    });
    const file = new File([exportedJson], "round-trip.json", {
      type: "application/json",
    });
    await userEvent.upload(screen.getByLabelText("Importer un état"), file);

    expect(
      await screen.findByText("Format : AutoWork production-state v2"),
    ).toBeInTheDocument();
    await userEvent.click(
      await screen.findByRole("button", { name: "Importer" }),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = fetchMock.mock.calls[1]?.[1]?.body;
    if (typeof body !== "string")
      throw new Error("Import request has no JSON body");
    expect(JSON.parse(body)).toMatchObject({
      schema_version: 2,
      origin: { subject_title: "Campagne d’Iran", research_date: null },
    });
  });

  it("signale un autre sujet et permet d’annuler sans importer", async () => {
    const fetchMock = vi.fn(responseFor);
    vi.stubGlobal("fetch", fetchMock);
    renderTransfer(productionStatus("ready", "Sujet ouvert"));
    const file = new File([JSON.stringify(snapshot)], "state.json", {
      type: "application/json",
    });
    await userEvent.upload(screen.getByLabelText("Importer un état"), file);
    expect(
      await screen.findByText(/Le fichier provient d’un autre sujet/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Annuler" }));
    expect(screen.queryByText("État prêt à importer")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("importe après confirmation, affiche le succès et invalide les deux préfixes", async () => {
    const fetchMock = vi.fn(responseFor);
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    render(
      <QueryClientProvider client={queryClient}>
        <ProductionStateTransfer
          subjectId={SUBJECT_ID}
          productionStatus={productionStatus("ready")}
        />
      </QueryClientProvider>,
    );
    const file = new File([JSON.stringify(snapshot)], "state.json", {
      type: "application/json",
    });
    await userEvent.upload(screen.getByLabelText("Importer un état"), file);
    await userEvent.click(
      await screen.findByRole("button", { name: "Importer" }),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/subjects/${SUBJECT_ID}/production/state/import`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "État importé.",
    );
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["production", SUBJECT_ID],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["production-artifact", SUBJECT_ID],
    });
  });

  it("exporte un Blob avec un nom sûr et révoque l’URL", async () => {
    const fetchMock = vi.fn(responseFor);
    vi.stubGlobal("fetch", fetchMock);
    const createObjectURL = vi.fn(() => "blob:test");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      value: createObjectURL,
      configurable: true,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      value: revokeObjectURL,
      configurable: true,
    });
    let downloadedName = "";
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloadedName = this.download;
      });
    renderTransfer(productionStatus("ready"));
    await userEvent.click(
      screen.getByRole("button", { name: "Exporter l’état" }),
    );
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(downloadedName).toContain("production-state-v2");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });

  it("rejette un JSON invalide sans appel backend", async () => {
    const fetchMock = vi.fn(responseFor);
    vi.stubGlobal("fetch", fetchMock);
    renderTransfer(productionStatus("ready"));
    const file = new File(["not-json"], "state.json", {
      type: "application/json",
    });
    await userEvent.upload(screen.getByLabelText("Importer un état"), file);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Ce fichier n’est pas un état de production AutoWork valide.",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
