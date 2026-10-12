import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  excludeEditionCompositionSubject,
  getEditionComposition,
  listSubjects,
  putEditionComposition,
  type EditionComposition,
  type EditionCompositionRead,
  type Subject,
} from "../../api/editions";
import {
  acceptEditionComposition,
  getEditionCompositionValidation,
  getEditionReview,
  type EditionReview,
} from "../../api/publication";
import {
  getEditionProduction,
  getPublicationArtifact,
  getSubjectProductionRuns,
  type ArtifactResponse,
  type ProductionBoard,
  type ProductionRunSummary,
} from "../../api/production";
import { CompositionConsole } from "./CompositionConsole";

vi.mock("../../api/editions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/editions")>();
  return {
    ...actual,
    getEditionComposition: vi.fn(),
    listSubjects: vi.fn(),
    putEditionComposition: vi.fn(),
    excludeEditionCompositionSubject: vi.fn(),
  };
});

vi.mock("../../api/publication", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/publication")>();
  return {
    ...actual,
    acceptEditionComposition: vi.fn(),
    getEditionCompositionValidation: vi.fn(),
    getEditionReview: vi.fn(),
  };
});

vi.mock("../../api/production", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/production")>();
  return {
    ...actual,
    getEditionProduction: vi.fn(),
    getPublicationArtifact: vi.fn(),
    getSubjectProductionRuns: vi.fn(),
  };
});

vi.mock("./EditionPreviewPanel", () => ({
  EditionPreviewPanel: () => <div>Preview test double</div>,
}));

const EDITION_ID = "edition-1";
const SUBJECT_A = "subject-a";
const SUBJECT_B = "subject-b";
const RUN_A = "run-a";
const RUN_B = "run-b";
const ARTIFACT_A = "artifact-a";
const ARTIFACT_B = "artifact-b";
const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);
const DOCUMENT_SHA = "c".repeat(64);

const subjects: Subject[] = [
  {
    id: SUBJECT_A,
    edition_id: EDITION_ID,
    title: "Sujet A",
    slug: "sujet-a",
    tlp: "AMBER",
    version: 1,
    created_at: "2026-10-12T09:00:00Z",
    updated_at: "2026-10-12T09:00:00Z",
  },
  {
    id: SUBJECT_B,
    edition_id: EDITION_ID,
    title: "Sujet B",
    slug: "sujet-b",
    tlp: "AMBER",
    version: 1,
    created_at: "2026-10-12T09:00:00Z",
    updated_at: "2026-10-12T09:00:00Z",
  },
];

function compositionEntry(
  key: "a" | "b",
  position: number,
): EditionComposition["entries"][number] {
  const subjectId = key === "a" ? SUBJECT_A : SUBJECT_B;
  return {
    id: `entry-${key}`,
    subject_id: subjectId,
    disposition: "included",
    position,
    production_run_id: key === "a" ? RUN_A : RUN_B,
    pipeline_generation: 4,
    artifact_id: key === "a" ? ARTIFACT_A : ARTIFACT_B,
    artifact_version: key === "a" ? 2 : 1,
    artifact_input_hash: key === "a" ? HASH_A : HASH_B,
    provenance_batch_id: `batch-${key}`,
    review_decision_id: null,
    document_sha256: DOCUMENT_SHA,
    preparation_status: "ready",
  };
}

function composition(
  version: number,
  entries: EditionComposition["entries"],
): EditionComposition {
  return {
    id: "composition-1",
    edition_id: EDITION_ID,
    version,
    actor_id: "analyst",
    preparation_status: "ready",
    created_at: "2026-10-12T09:00:00Z",
    updated_at: "2026-10-12T09:00:00Z",
    entries,
  };
}

function compositionRead(
  value: EditionComposition | null,
): EditionCompositionRead {
  return {
    edition_id: EDITION_ID,
    composition: value,
    reasons: [],
    axes: {
      production_validity: true,
      discovery_update_available: true,
      composition_eligibility: true,
    },
  };
}

function artifact(key: "a" | "b"): ArtifactResponse {
  return {
    artifact_id: key === "a" ? ARTIFACT_A : ARTIFACT_B,
    production_run_id: key === "a" ? RUN_A : RUN_B,
    stage: "publication",
    version: key === "a" ? 2 : 1,
    input_hash: key === "a" ? HASH_A : HASH_B,
    canonical_sha256: DOCUMENT_SHA,
    status: "verified",
    metadata: {},
    canonical_content: null,
  };
}

function productionBoard(): ProductionBoard {
  return {
    edition_id: EDITION_ID,
    subjects: [
      {
        subject_id: SUBJECT_A,
        title: "Sujet A",
        tlp: "AMBER",
        latest_run_id: RUN_A,
        latest_run_number: 1,
        latest_status: "ready",
        latest_stage: "assembly",
        active_run_id: null,
        can_start: true,
        blocking_reason: null,
      },
      {
        subject_id: SUBJECT_B,
        title: "Sujet B",
        tlp: "AMBER",
        latest_run_id: RUN_B,
        latest_run_number: 1,
        latest_status: "ready",
        latest_stage: "assembly",
        active_run_id: null,
        can_start: true,
        blocking_reason: null,
      },
    ],
    active_batch: null,
    recent_batches: [],
  };
}

function run(key: "a" | "b"): ProductionRunSummary {
  return {
    run_id: key === "a" ? RUN_A : RUN_B,
    edition_id: EDITION_ID,
    subject_id: key === "a" ? SUBJECT_A : SUBJECT_B,
    run_number: 1,
    status: "ready",
    current_stage: "assembly",
    pipeline_generation: 4,
    research_date: "2026-10-12",
    created_at: "2026-10-12T09:00:00Z",
    started_at: "2026-10-12T09:01:00Z",
    finished_at: "2026-10-12T09:05:00Z",
    error_code: null,
    error_message: null,
  };
}

const validation: EditionReview = {
  edition_id: EDITION_ID,
  items: [],
  can_accept: true,
  composition_version: 2,
  diagnostics: [],
};

function renderComposition() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CompositionConsole editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("crypto", { randomUUID: () => "idempotency-1" });
  vi.mocked(listSubjects).mockResolvedValue(subjects);
  vi.mocked(getEditionComposition).mockResolvedValue(
    compositionRead(
      composition(2, [compositionEntry("a", 1), compositionEntry("b", 2)]),
    ),
  );
  vi.mocked(getEditionCompositionValidation).mockResolvedValue(validation);
  vi.mocked(getEditionReview).mockResolvedValue({
    ...validation,
    exclusions: [],
  });
  vi.mocked(getEditionProduction).mockResolvedValue(productionBoard());
  vi.mocked(getSubjectProductionRuns).mockImplementation((subjectId) =>
    Promise.resolve([run(subjectId === SUBJECT_A ? "a" : "b")]),
  );
  vi.mocked(getPublicationArtifact).mockImplementation((subjectId) =>
    Promise.resolve(artifact(subjectId === SUBJECT_A ? "a" : "b")),
  );
  vi.mocked(putEditionComposition).mockResolvedValue(
    composition(3, [compositionEntry("a", 1), compositionEntry("b", 2)]),
  );
  vi.mocked(excludeEditionCompositionSubject).mockResolvedValue({
    composition: composition(3, [
      compositionEntry("a", 1),
      compositionEntry("b", 2),
    ]),
    decision_id: "decision-b",
  });
  vi.mocked(acceptEditionComposition).mockResolvedValue({
    edition_id: EDITION_ID,
    edition_state: "open",
    manifest_id: "manifest-1",
    manifest_sha256: DOCUMENT_SHA,
    manifest_schema_version: "2",
    edition_version: 3,
    composition_id: "composition-1",
    composition_version: 2,
    job_id: "job-1",
    job_dispatched: true,
  });
});

afterEach(() => vi.unstubAllGlobals());

describe("CompositionConsole", () => {
  it("affiche les versions exactes épinglées, la progression et l’avertissement Discovery", async () => {
    renderComposition();

    expect(
      await screen.findByText(/Une mise à jour Discovery est disponible/),
    ).toBeInTheDocument();
    const subjectA = screen
      .getByRole("heading", { name: "Sujet A" })
      .closest("article");
    expect(subjectA).not.toBeNull();
    expect(
      within(subjectA!).getByText("Run épinglé").parentElement,
    ).toHaveTextContent(RUN_A);
    expect(
      within(subjectA!).getByText("Génération").parentElement,
    ).toHaveTextContent("4");
    expect(
      within(subjectA!).getByText("Hash d’entrée").parentElement,
    ).toHaveTextContent("aaaaaaaaaaaa…");
    expect(subjectA).toHaveTextContent("Production : Prête");
  });

  it("traduit le diagnostic d’un sujet reporté et maintient l’acceptation bloquée", async () => {
    vi.mocked(getEditionCompositionValidation).mockResolvedValueOnce({
      ...validation,
      can_accept: false,
      diagnostics: [
        {
          code: "subject_deferred",
          subject_id: SUBJECT_B,
          disposition: "deferred",
          run_id: null,
          details: null,
          active_repair_count: 0,
          unresolved_repair_count: 0,
          pending_rebuild_count: 0,
        },
      ],
    });
    renderComposition();

    expect(
      await screen.findAllByText(
        "Sujet reporté : une décision explicite est requise avant acceptation.",
      ),
    ).toHaveLength(1);
    expect(
      screen.getByText(
        "L’API signale des blocages. L’acceptation finale reste désactivée.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la composition" }),
    ).toBeDisabled();
  });

  it("confirme explicitement la version avant le POST composition/accept", async () => {
    renderComposition();
    const accept = await screen.findByRole("button", {
      name: "Accepter la composition",
    });
    expect(accept).toBeDisabled();

    await userEvent.setup().click(
      screen.getByRole("checkbox", {
        name: /Je confirme l’acceptation finale de la composition version 2/,
      }),
    );
    expect(accept).toBeEnabled();
    await userEvent.setup().click(accept);

    await waitFor(() =>
      expect(acceptEditionComposition).toHaveBeenCalledWith(EDITION_ID, 2),
    );
  });

  it("recharge une composition périmée sans rejouer le PUT", async () => {
    vi.mocked(getEditionComposition)
      .mockResolvedValueOnce(
        compositionRead(
          composition(2, [compositionEntry("a", 1), compositionEntry("b", 2)]),
        ),
      )
      .mockResolvedValue(
        compositionRead(
          composition(4, [compositionEntry("a", 1), compositionEntry("b", 2)]),
        ),
      );
    vi.mocked(putEditionComposition).mockRejectedValue(
      new ApiError(
        "La composition a changé.",
        "composition_version_conflict",
        409,
        {
          expected_version: 2,
          actual_version: 4,
        },
      ),
    );
    renderComposition();

    await userEvent
      .setup()
      .click(await screen.findByRole("button", { name: "Monter Sujet B" }));
    expect(
      await screen.findByText(/votre modification n’a pas été réappliquée/),
    ).toBeInTheDocument();
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Recharger la composition" }));
    await waitFor(() => expect(getEditionComposition).toHaveBeenCalledTimes(3));
    expect(screen.getByText(/Version 4/)).toBeInTheDocument();
    expect(putEditionComposition).toHaveBeenCalledTimes(1);
    expect(putEditionComposition).toHaveBeenCalledWith(
      EDITION_ID,
      expect.objectContaining({ expected_version: 2 }),
      "idempotency-1",
    );
  });

  it("exclut un sujet sans artifact vérifié en un appel et affiche son audit", async () => {
    const excludedB = {
      ...compositionEntry("b", 2),
      disposition: "excluded" as const,
      position: null,
      review_decision_id: "decision-b",
    };
    vi.mocked(getEditionComposition)
      .mockResolvedValueOnce(
        compositionRead(
          composition(2, [compositionEntry("a", 1), compositionEntry("b", 2)]),
        ),
      )
      .mockResolvedValue(
        compositionRead(composition(3, [compositionEntry("a", 1), excludedB])),
      );
    vi.mocked(getPublicationArtifact).mockImplementation((subjectId) =>
      subjectId === SUBJECT_A
        ? Promise.resolve(artifact("a"))
        : Promise.reject(new Error("publication artifact unavailable")),
    );
    vi.mocked(getEditionReview).mockResolvedValue({
      ...validation,
      exclusions: [
        {
          subject_id: SUBJECT_B,
          title: "Sujet B",
          decision_id: "decision-b",
          applicable: true,
          actor_id: "analyst",
          reason: "Hors périmètre du bulletin.",
          occurred_at: "2026-10-12T10:00:00Z",
          run_id: null,
          document_artifact_id: null,
          document_artifact_version: null,
          document_input_hash: null,
          pipeline_generation: null,
        },
      ],
    });
    renderComposition();
    const user = userEvent.setup();
    const headingB = await screen.findByRole("heading", { name: "Sujet B" });
    const subjectB = headingB.closest("article");
    expect(subjectB).not.toBeNull();
    await user.click(
      within(subjectB!).getByRole("button", { name: "Exclure…" }),
    );
    await user.type(
      within(subjectB!).getByLabelText("Motif obligatoire de l’exclusion"),
      "Hors périmètre du bulletin.",
    );
    await user.click(
      within(subjectB!).getByRole("button", {
        name: "Confirmer l’exclusion motivée",
      }),
    );

    await waitFor(() =>
      expect(excludeEditionCompositionSubject).toHaveBeenCalledWith(
        EDITION_ID,
        SUBJECT_B,
        { reason: "Hors périmètre du bulletin.", expected_version: 2 },
        "idempotency-1",
      ),
    );
    expect(excludeEditionCompositionSubject).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByText(
        /par analyst · motif : Hors périmètre du bulletin/,
      ),
    ).toBeInTheDocument();
  });

  it("recharge et signale un conflit de version typé pendant l’exclusion", async () => {
    vi.mocked(excludeEditionCompositionSubject).mockRejectedValue(
      new ApiError(
        "La composition a changé.",
        "composition_version_conflict",
        409,
        { expected_version: 2, actual_version: 3 },
      ),
    );
    renderComposition();
    const user = userEvent.setup();
    const headingB = await screen.findByRole("heading", { name: "Sujet B" });
    const subjectB = headingB.closest("article");
    expect(subjectB).not.toBeNull();
    await user.click(
      within(subjectB!).getByRole("button", { name: "Exclure…" }),
    );
    await user.type(
      within(subjectB!).getByLabelText("Motif obligatoire de l’exclusion"),
      "Hors périmètre.",
    );
    await user.click(
      within(subjectB!).getByRole("button", {
        name: "Confirmer l’exclusion motivée",
      }),
    );

    expect(
      await screen.findByText(/composition a changé pendant votre action/),
    ).toBeInTheDocument();
    await waitFor(() => expect(getEditionComposition).toHaveBeenCalledTimes(2));
    expect(excludeEditionCompositionSubject).toHaveBeenCalledTimes(1);
  });
});
