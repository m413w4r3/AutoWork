import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Edition, Subject } from "../../api/editions";
import type { ProductionStatus } from "../../api/production";
import { EditionDashboard } from "./EditionDashboard";

const { listSubjectsMock, getSubjectProductionMock } = vi.hoisted(() => ({
  listSubjectsMock: vi.fn(),
  getSubjectProductionMock: vi.fn(),
}));

vi.mock("../../api/editions", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/editions")>(
      "../../api/editions",
    );
  return { ...actual, listSubjects: listSubjectsMock };
});

vi.mock("../../api/production", async () => {
  const actual = await vi.importActual<typeof import("../../api/production")>(
    "../../api/production",
  );
  return { ...actual, getSubjectProduction: getSubjectProductionMock };
});

const edition: Edition = {
  id: "edition-1",
  country: "France",
  country_code: "FR",
  period_start: "2026-08-01",
  period_end: "2026-08-31",
  tlp: "GREEN",
  languages: ["fr"],
  state: "open",
  version: 1,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
};

const subjects: Subject[] = [
  {
    id: "subject-null",
    edition_id: edition.id,
    title: "Sujet non démarré",
    slug: "sujet-non-demarre",
    tlp: "GREEN",
    version: 1,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
  },
  {
    id: "subject-running",
    edition_id: edition.id,
    title: "Sujet en cours",
    slug: "sujet-en-cours",
    tlp: "AMBER",
    version: 1,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
  },
  {
    id: "subject-review",
    edition_id: edition.id,
    title: "Sujet à vérifier",
    slug: "sujet-a-verifier",
    tlp: "RED",
    version: 1,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
  },
  {
    id: "subject-ready",
    edition_id: edition.id,
    title: "Sujet prêt",
    slug: "sujet-pret",
    tlp: "CLEAR",
    version: 1,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
  },
];

function makeProduction(
  subjectId: string,
  overrides: Partial<ProductionStatus> = {},
): ProductionStatus {
  return {
    subject_id: subjectId,
    edition_id: edition.id,
    title: "Sujet",
    status: "ready",
    current_stage: "assembly",
    progress_current: 1,
    progress_total: 1,
    references_conversation_id: null,
    synthesis_conversation_id: null,
    run_id: `run-${subjectId}`,
    pipeline_generation: 1,
    created_at: "2026-08-01T10:00:00Z",
    started_at: "2026-08-01T10:00:00Z",
    finished_at: "2026-08-01T10:00:00Z",
    error_code: null,
    error_message: null,
    error_details: null,
    recovery_disposition: "auto",
    warnings: [],
    stages: {
      references: {
        status: "succeeded",
        version: 1,
        error_code: null,
        error_message: null,
      },
      extraction: {
        status: "succeeded",
        version: 1,
        error_code: null,
        error_message: null,
      },
      synthesis: {
        status: "succeeded",
        version: 1,
        error_code: null,
        error_message: null,
      },
      assembly: {
        status: "succeeded",
        version: 1,
        error_code: null,
        error_message: null,
      },
    },
    ...overrides,
  };
}

function renderDashboard(currentEdition = edition) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditionDashboard edition={currentEdition} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  listSubjectsMock.mockReset();
  getSubjectProductionMock.mockReset();
  window.history.replaceState({}, "", "/");
});

describe("EditionDashboard", () => {
  it("projette les compteurs, étapes, erreurs et TLP des sujets", async () => {
    listSubjectsMock.mockResolvedValue(subjects);
    getSubjectProductionMock.mockImplementation((subjectId: string) => {
      if (subjectId === "subject-null") return Promise.resolve(null);
      if (subjectId === "subject-running") {
        return Promise.resolve(
          makeProduction(subjectId, {
            status: "running",
            current_stage: "extraction",
            stages: {
              ...makeProduction(subjectId).stages,
              references: {
                status: "succeeded",
                version: 1,
                error_code: null,
                error_message: null,
              },
              extraction: {
                status: "running",
                version: 1,
                error_code: null,
                error_message: null,
              },
              synthesis: {
                status: "pending",
                version: null,
                error_code: null,
                error_message: null,
              },
              assembly: {
                status: "pending",
                version: null,
                error_code: null,
                error_message: null,
              },
            },
          }),
        );
      }
      if (subjectId === "subject-review") {
        return Promise.resolve(
          makeProduction(subjectId, {
            status: "failed",
            current_stage: "references",
            error_code: "REF_TIMEOUT",
            error_message: "Les références n'ont pas répondu.",
            stages: {
              ...makeProduction(subjectId).stages,
              references: {
                status: "failed",
                version: 1,
                error_code: "STAGE_TIMEOUT",
                error_message: "Délai de référence dépassé.",
              },
            },
          }),
        );
      }
      return Promise.resolve(makeProduction(subjectId));
    });

    renderDashboard();

    expect(await screen.findByText("Sujet prêt")).toBeInTheDocument();
    expect(screen.getByText("Subjects").parentElement).toHaveTextContent("4");
    expect(
      screen.getByText("Production non démarrée").parentElement,
    ).toHaveTextContent("1");
    expect(
      screen.getByText("En cours", { selector: "dt" }).parentElement,
    ).toHaveTextContent("1");
    expect(
      screen.getByText("Attention requise").parentElement,
    ).toHaveTextContent("1");
    expect(screen.getByText("Prêts").parentElement).toHaveTextContent("1");
    expect(screen.getByText("TLP:RED")).toBeInTheDocument();
    expect(
      screen.getByText("Extraction", { selector: "td" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("REF_TIMEOUT — Les références n'ont pas répondu."),
    ).toBeInTheDocument();
    expect(screen.getByText("TLP:AMBER")).toBeInTheDocument();
    expect(
      screen.getByText("En cours", { selector: "td" }),
    ).toBeInTheDocument();
  });

  it("affiche l'état vide ouvert avec les liens Discovery et Selection", async () => {
    listSubjectsMock.mockResolvedValue([]);

    renderDashboard();

    expect(
      await screen.findByText(
        "Aucun sujet n'a encore été sélectionné pour cette édition.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Découvrir des sujets" }),
    ).toHaveAttribute("href", "/editions/edition-1/discovery");
    expect(
      screen.getByRole("link", { name: "Sélectionner les sujets" }),
    ).toHaveAttribute("href", "/editions/edition-1/selection");
  });

  it("ouvre le sujet depuis son lien clavier-accessible", async () => {
    listSubjectsMock.mockResolvedValue([subjects[0]]);
    getSubjectProductionMock.mockResolvedValue(null);

    renderDashboard();
    const link = await screen.findByRole("link", { name: "Ouvrir le sujet" });

    await userEvent.setup().click(link);

    expect(window.location.pathname).toBe("/subjects/subject-null");
  });
});
