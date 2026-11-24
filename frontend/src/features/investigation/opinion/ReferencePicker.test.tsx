import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import {
  FindingReferencePicker,
  ObservationReferencePicker,
} from "./ReferencePicker";

const FIRST_OBSERVATION_ID = "11111111-1111-4111-8111-111111111111";
const SECOND_OBSERVATION_ID = "22222222-2222-4222-8222-222222222222";
const FINDING_ID = "33333333-3333-4333-8333-333333333333";
const OBJECT_ID = "a".repeat(64);
const FIRST_SHA = "b".repeat(64);
const SECOND_SHA = "c".repeat(64);

function makeObservation(
  overrides: Partial<InvestigationObservation> = {},
): InvestigationObservation {
  return {
    id: FIRST_OBSERVATION_ID,
    kind: "vt_file_report",
    sha256: FIRST_SHA,
    source: "VirusTotal",
    source_version: "3",
    observed_at: "2026-11-24T09:00:00Z",
    object_type: "file",
    object_id: OBJECT_ID,
    limitations: [],
    invocation_ref: null,
    ...overrides,
  };
}

function makeFinding(): InvestigationFinding {
  return {
    id: FINDING_ID,
    logical_id: "44444444-4444-4444-8444-444444444444",
    current_version_id: "55555555-5555-4555-8555-555555555555",
    version: 2,
    revision_chain: [],
    kind: "new_sample",
    assessment: "supported",
    novelty: "new_to_article",
    statement: "Un échantillon distinct est étayé par les observations.",
    scope: "sample",
    limitations: [],
    observation_refs: [FIRST_OBSERVATION_ID],
    claim_refs: [],
    created_at: "2026-11-24T09:00:00Z",
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("reference pickers", () => {
  it("regroupe les observations similaires et réserve les identifiants au détail", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ObservationReferencePicker
        observations={[
          makeObservation(),
          makeObservation({
            id: SECOND_OBSERVATION_ID,
            sha256: SECOND_SHA,
            observed_at: "2026-11-24T10:00:00Z",
          }),
        ]}
        selectedIds={[]}
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("heading", { name: "VirusTotal" })).toBeVisible();
    expect(
      screen.getByText("Rapport VT fichier · file/aaaaaaaaaa…"),
    ).toBeVisible();
    expect(screen.getByText("2 références regroupées")).toBeVisible();
    expect(
      screen
        .getAllByText(OBJECT_ID)
        .every((element) => element.closest("details")?.open === false),
    ).toBe(true);
    expect(screen.getByText(FIRST_SHA).closest("details")?.open).toBe(false);
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);

    await user.click(
      screen.getByText("Provenance technique", { selector: "summary" }),
    );
    const details = screen.getByText("Provenance technique", {
      selector: "summary",
    }).parentElement;
    expect(details).not.toBeNull();
    expect(within(details as HTMLElement).getAllByText(OBJECT_ID).length).toBe(
      2,
    );
    expect(within(details as HTMLElement).getByText(FIRST_SHA)).toBeVisible();
    expect(
      within(details as HTMLElement).getByText(SECOND_OBSERVATION_ID),
    ).toBeVisible();

    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    await user.click(
      within(details as HTMLElement).getAllByRole("button", {
        name: "Copier SHA-256 de l’observation",
      })[0] as HTMLElement,
    );
    expect(writeText).toHaveBeenCalledWith(FIRST_SHA);
  });

  it("garde les identifiants finding repliés et permet la recherche", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <FindingReferencePicker
        findings={[makeFinding()]}
        selectedIds={[]}
        onChange={onChange}
      />,
    );

    expect(screen.getByText(/Un échantillon distinct/)).toBeVisible();
    expect(screen.getByText(FINDING_ID).closest("details")?.open).toBe(false);
    await user.type(
      screen.getByRole("searchbox", { name: "Rechercher un finding" }),
      "distinct",
    );
    expect(screen.getByText(/Un échantillon distinct/)).toBeVisible();
    await user.click(
      screen.getByText("Provenance technique", { selector: "summary" }),
    );
    expect(screen.getByText(FINDING_ID)).toBeVisible();
  });
});
