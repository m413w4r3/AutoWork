import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./editions";
import {
  prepareEnrichmentRevision,
  type EditorialEnrichmentRevisionInput,
  type EditorialProposalRead,
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
});
