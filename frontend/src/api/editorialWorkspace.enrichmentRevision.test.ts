import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./editions";
import {
  prepareEnrichmentRevision,
  type EditorialEnrichmentRevisionInput,
  type EditorialProposalRead,
  type EditorialVisualRevisionProposalPatch,
  type EditorialVisualRevisionProposalRead,
} from "./editorialWorkspace";

const proposal: EditorialProposalRead = {
  id: "11111111-1111-4111-8111-111111111111",
  subject_id: "22222222-2222-4222-8222-222222222222",
  production_run_id: "33333333-3333-4333-8333-333333333333",
  occurrence_id: "44444444-4444-4444-8444-444444444444",
  thread_id: null,
  origin: "manual_patch",
  state: "proposed",
  state_version: 1,
  operation: "update_visual_caption",
  base_artifact_id: "55555555-5555-4555-8555-555555555555",
  base_artifact_version: 2,
  base_canonical_sha256: "a".repeat(64),
  base_text: null,
  patch_sha256: "b".repeat(64),
  patch: null,
  diagnostics: { preview: { operation: "update_visual_caption" } },
  created_at: "2026-11-22T10:00:00+00:00",
};

const captionInput: EditorialEnrichmentRevisionInput = {
  snapshot_id: "66666666-6666-4666-8666-666666666666",
  operation: "update_visual_caption",
  element_kind: "table",
  element_key: "table-a",
  caption: "Nouvelle légende",
};

interface FetchCall {
  input: RequestInfo | URL;
  init: RequestInit | undefined;
}

const calls: FetchCall[] = [];

function stubFetch(response: Response): void {
  vi.stubGlobal(
    "fetch",
    (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      calls.push({ input, init });
      return Promise.resolve(response);
    },
  );
}

afterEach(() => {
  calls.length = 0;
  vi.unstubAllGlobals();
});

function onlyCall(): FetchCall {
  const call = calls[0];
  if (call === undefined || calls.length !== 1) {
    throw new Error(
      `expected exactly one fetch call, received ${calls.length}`,
    );
  }
  return call;
}

function jsonBody(call: FetchCall): unknown {
  const body = call.init?.body;
  if (typeof body !== "string") {
    throw new Error("expected a JSON string request body");
  }
  return JSON.parse(body);
}

describe("prepareEnrichmentRevision", () => {
  it("posts the typed revision with the idempotency key and no cache", async () => {
    stubFetch(Response.json(proposal));

    const result = await prepareEnrichmentRevision(
      "subject 1",
      "occurrence/1",
      "idem-key-1",
      captionInput,
    );

    expect(result).toEqual(proposal);
    expect(calls).toHaveLength(1);
    const call = onlyCall();
    expect(call.input).toBe(
      "/api/subjects/subject%201/editor/blocks/occurrence%2F1/enrichment-revisions",
    );
    expect(call.init?.method).toBe("POST");
    expect(call.init?.cache).toBe("no-store");
    const headers = call.init?.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers["Idempotency-Key"]).toBe("idem-key-1");
    expect(jsonBody(call)).toEqual(captionInput);
  });

  it("keeps the discriminated reorder payload opaque", async () => {
    stubFetch(Response.json(proposal));
    const input: EditorialEnrichmentRevisionInput = {
      snapshot_id: "66666666-6666-4666-8666-666666666666",
      operation: "reorder_enrichment_elements",
      anchor_block_id: "77777777-7777-4777-8777-777777777777",
      order: [
        { kind: "table", key: "table-b" },
        { kind: "table", key: "table-a" },
      ],
    };

    await prepareEnrichmentRevision("subject", "anchor", "idem-key-2", input);

    expect(jsonBody(onlyCall())).toEqual({
      snapshot_id: "66666666-6666-4666-8666-666666666666",
      operation: "reorder_enrichment_elements",
      anchor_block_id: "77777777-7777-4777-8777-777777777777",
      order: [
        { kind: "table", key: "table-b" },
        { kind: "table", key: "table-a" },
      ],
    });
  });

  it("parses the stable French refusal and carries the closed code", async () => {
    stubFetch(
      Response.json(
        {
          detail: {
            code: "enrichment_revision_no_change",
            message: "server message is not trusted",
          },
        },
        { status: 422 },
      ),
    );

    const failure = prepareEnrichmentRevision(
      "subject",
      "block",
      "idem-key-3",
      captionInput,
    );

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "enrichment_revision_no_change",
      status: 422,
    });
    await expect(failure).rejects.toThrow(
      "La révision ne modifie rien. Aucune nouvelle version n’a été créée.",
    );
  });

  it("falls back to the server message for an unmapped refusal code", async () => {
    stubFetch(
      Response.json(
        {
          detail: {
            code: "enrichment_revision_unknown_code",
            message: "Message serveur de repli.",
          },
        },
        { status: 409 },
      ),
    );

    const error: unknown = await prepareEnrichmentRevision(
      "subject",
      "block",
      "idem-key-4",
      captionInput,
    ).catch((failure: unknown) => failure);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code: "enrichment_revision_unknown_code",
      status: 409,
    });
    expect((error as ApiError).message).toBe("Message serveur de repli.");
  });

  it("posts a revise_chart body with the opaque element identity", async () => {
    stubFetch(Response.json(proposal));
    const input: EditorialEnrichmentRevisionInput = {
      snapshot_id: "66666666-6666-4666-8666-666666666666",
      operation: "revise_chart",
      element_key: "chart-a",
      chart: {
        key: "chart-a",
        kind: "bar",
        title: "Documented counts",
        caption: null,
        category_axis_label: "Category",
        value_axis_label: "Count",
        unit: "endpoints",
        points: [
          {
            label: "Alpha",
            series: "Endpoints",
            value: "12",
            evidence_refs: [
              {
                source_document_id: "88888888-8888-4888-8888-888888888888",
                kind: "fact",
                evidence_key: "c".repeat(64),
              },
            ],
          },
        ],
        purpose: {
          question: "Which count is documented?",
          available_data: "One exact count is documented.",
          comprehension_gain: "The count is compared with its source.",
          scope: "Only the synthetic record.",
          evidence_refs: [
            {
              source_document_id: "88888888-8888-4888-8888-888888888888",
              kind: "fact",
              evidence_key: "c".repeat(64),
            },
          ],
          knowledge_limits: "No other count is inferred.",
          placement_reason: "Keep the chart after its paragraph.",
        },
      },
    };

    await prepareEnrichmentRevision("subject", "chart", "idem-key-5", input);

    expect(jsonBody(onlyCall())).toEqual(input);
  });
});

describe("visual revision proposal patch read typing", () => {
  const placement = {
    kind: "after_paragraph" as const,
    section_index: null,
    anchor_block_id: "99999999-9999-4999-8999-999999999999",
  };
  const purpose = {
    question: "Which count is documented?",
    available_data: "One exact count is documented.",
    comprehension_gain: "The count is compared with its source.",
    scope: "Only the synthetic record.",
    evidence_refs: [
      {
        source_document_id: "88888888-8888-4888-8888-888888888888",
        kind: "fact" as const,
        evidence_key: "c".repeat(64),
      },
    ],
    knowledge_limits: "No other count is inferred.",
    placement_reason: "Keep the chart after its paragraph.",
  };
  const proposalBase = {
    schema_version: "editorial-enrichment-visual-patch-v1" as const,
    target: { kind: "enrichment_element" },
    target_id: "d".repeat(64),
    occurrence_id: "44444444-4444-4444-8444-444444444444",
    before_hash: "e".repeat(64),
    rationale: "Révision visuelle manuelle validée par l’analyste.",
  };

  function readWith(
    patch: EditorialVisualRevisionProposalPatch,
  ): EditorialVisualRevisionProposalRead {
    return { ...proposal, operation: patch.operation, patch };
  }

  it("displays a revise_chart proposal card", () => {
    const read = readWith({
      ...proposalBase,
      operation: "rewrite_chart",
      visual_patch: {
        kind: "revise_chart",
        element_key: "chart-a",
        chart: {
          key: "chart-a",
          kind: "bar",
          title: "Documented counts",
          caption: null,
          placement,
          purpose,
          points: [
            {
              label: "Alpha",
              series: "Endpoints",
              value: "12",
              evidence_refs: purpose.evidence_refs,
            },
          ],
          category_axis_label: "Category",
          value_axis_label: "Count",
          unit: "endpoints",
        },
      },
    });

    const visualPatch = read.patch?.visual_patch;
    expect(visualPatch?.kind).toBe("revise_chart");
    if (visualPatch?.kind === "revise_chart") {
      expect(visualPatch.chart.key).toBe("chart-a");
      expect(visualPatch.chart.placement.kind).toBe("after_paragraph");
      expect(visualPatch.chart.points[0]?.value).toBe("12");
    }
  });

  it("displays a revise_diagram proposal card", () => {
    const read = readWith({
      ...proposalBase,
      operation: "rewrite_diagram",
      visual_patch: {
        kind: "revise_diagram",
        element_key: "diagram-a",
        diagram: {
          key: "diagram-a",
          kind: "infection_chain",
          title: "Documented flow",
          caption: null,
          direction: "left_to_right",
          profile: "flow",
          placement: { kind: "end", section_index: null },
          purpose,
          nodes: [
            { node_id: "loader", label: "Loader", evidence_refs: [] },
            { node_id: "payload", label: "Payload", evidence_refs: [] },
          ],
          edges: [
            {
              source_node_id: "loader",
              target_node_id: "payload",
              label: "loads",
              direction: "directed",
              relation_type: "factual",
              evidence_refs: [],
            },
          ],
          groups: [],
        },
      },
    });

    const visualPatch = read.patch?.visual_patch;
    expect(visualPatch?.kind).toBe("revise_diagram");
    if (visualPatch?.kind === "revise_diagram") {
      expect(visualPatch.diagram.edges[0]?.relation_type).toBe("factual");
      expect(visualPatch.diagram.placement.kind).toBe("end");
    }
  });

  it("displays replace_source_figure and remove_enrichment_element proposal cards", () => {
    const replaceRead = readWith({
      ...proposalBase,
      operation: "replace_source_figure",
      visual_patch: {
        kind: "replace_source_figure",
        element_key: "figure-a",
        figure_id: "77777777-7777-4777-8777-777777777777",
        caption: "Nouvelle légende de figure",
      },
    });
    const removeRead = readWith({
      ...proposalBase,
      operation: "delete_chart",
      visual_patch: {
        kind: "remove_enrichment_element",
        element_kind: "chart",
        element_key: "chart-a",
      },
    });

    const replacePatch = replaceRead.patch?.visual_patch;
    const removePatch = removeRead.patch?.visual_patch;
    expect(replacePatch?.kind).toBe("replace_source_figure");
    if (replacePatch?.kind === "replace_source_figure") {
      expect(replacePatch.figure_id).toBe(
        "77777777-7777-4777-8777-777777777777",
      );
    }
    expect(removePatch?.kind).toBe("remove_enrichment_element");
    if (removePatch?.kind === "remove_enrichment_element") {
      expect(removePatch.element_kind).toBe("chart");
      expect(removeRead.patch?.operation).toBe("delete_chart");
    }
  });
});
