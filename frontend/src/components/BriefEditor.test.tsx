import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BriefEditor } from "./BriefEditor";

const subjectId = "11111111-1111-4111-8111-111111111111";
const result = {
  subject_id: subjectId,
  pack: {
    id: "22222222-2222-4222-8222-222222222222",
    version: 1,
    content_hash: "a".repeat(64),
    object_hashes: ["b".repeat(64)],
    source_count: 1,
    claim_count: 1,
    indicator_count: 0,
    entity_count: 1,
    uncertainty_count: 1,
    created_by: "dev-analyst",
  },
  draft: {
    id: "33333333-3333-4333-8333-333333333333",
    version: 1,
    pack_id: "22222222-2222-4222-8222-222222222222",
    pack_hash: "a".repeat(64),
    title: "Iran : une campagne cible des administrations",
    provider: "qwen",
    stale: false,
  },
  blocks: [
    {
      id: "44444444-4444-4444-8444-444444444444",
      sentences: [
        {
          id: "55555555-5555-4555-8555-555555555555",
          text: "Une campagne a ciblé des administrations iraniennes.",
          factual: true,
          claim_ids: ["66666666-6666-4666-8666-666666666666"],
          indicator_ids: [],
          evidence: [
            {
              id: "66666666-6666-4666-8666-666666666666",
              kind: "fact",
              value: "ciblé des administrations iraniennes",
              source_id: "77777777-7777-4777-8777-777777777777",
              source_span: { start: 10, end: 48 },
            },
          ],
        },
      ],
    },
  ],
  limits: ["Attribution non évaluée."],
  references: [
    {
      id: "77777777-7777-4777-8777-777777777777",
      origin: "https://research.example/iran",
      sha256: "c".repeat(64),
    },
  ],
  versions: [],
  status: "draft",
  qa: {
    factual_sentences_covered: true,
    claim_references_in_pack: true,
    source_references_present: true,
    validated_indicators_only: true,
    current_evidence_pack: true,
  },
  qa_errors: [],
  diff: "",
};

function renderEditor() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <BriefEditor subjectId={subjectId} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("BriefEditor", () => {
  it("affiche la preuve à côté de la phrase et régénère seulement le bloc", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json(result));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderEditor();

    expect(
      await screen.findByRole("heading", {
        name: "Iran : une campagne cible des administrations",
      }),
    ).toBeVisible();
    expect(screen.getByLabelText("Preuves de la phrase 1")).toHaveTextContent(
      "ciblé des administrations iraniennes",
    );
    expect(screen.getByText("✓ current evidence pack")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Régénérer ce bloc" }));
    expect(fetchMock).toHaveBeenLastCalledWith(
      `/api/subjects/${subjectId}/brief/blocks/44444444-4444-4444-8444-444444444444/regenerate`,
      expect.objectContaining({ method: "POST" }),
    );
  });
});
