import { afterEach, describe, expect, it, vi } from "vitest";

import {
  WorkshopApiError,
  encodeSegment,
  getJson,
  investigationPath,
  postEmpty,
  postJson,
  putJson,
  queryString,
} from "./client";
import {
  bodyOf,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  lastRequest,
} from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

async function rejection(promise: Promise<unknown>): Promise<WorkshopApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof WorkshopApiError) return error;
    throw error;
  }
  throw new Error("the request unexpectedly resolved");
}

describe("client transport", () => {
  it("sends a GET without body or extra headers", async () => {
    const recorder = installFetch(() => jsonResponse({ ok: true }));

    await expect(getJson<{ ok: boolean }>("/api/probe")).resolves.toEqual({
      ok: true,
    });

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/probe");
    expect(request.init?.method).toBe("GET");
    expect(request.init?.body).toBeUndefined();
    expect(headersOf(request.init)).toEqual({});
  });

  it("sends JSON and the Idempotency-Key on a POST", async () => {
    const recorder = installFetch(() => jsonResponse({ id: "x" }));

    await postJson("/api/things", { value: 3 }, "key-123");

    const request = lastRequest(recorder);
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)).toEqual({
      "Content-Type": "application/json",
      "Idempotency-Key": "key-123",
    });
    expect(bodyOf(request.init)).toEqual({ value: 3 });
  });

  it("sends a bodyless POST carrying only the idempotency header", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await postEmpty("/api/run", "key-abcdefgh");

    const request = lastRequest(recorder);
    expect(request.init?.method).toBe("POST");
    expect(request.init?.body).toBeUndefined();
    expect(headersOf(request.init)).toEqual({
      "Idempotency-Key": "key-abcdefgh",
    });
  });

  it("sends a PUT with a mandatory Idempotency-Key", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await putJson("/api/file", { path: "notes/x.md" }, "key-12345678");

    const request = lastRequest(recorder);
    expect(request.init?.method).toBe("PUT");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe("key-12345678");
    expect(bodyOf(request.init)).toEqual({ path: "notes/x.md" });
  });

  it("maps detail.code/status/message into WorkshopApiError", async () => {
    installFetch(() =>
      errorResponse(409, "stale_task_version", "Version périmée"),
    );

    const error = await rejection(getJson("/api/things"));

    expect(error.status).toBe(409);
    expect(error.code).toBe("stale_task_version");
    expect(error.message).toBe("Version périmée");
    expect(error.details).toBeNull();
  });

  it("keeps the bounded detail.details map", async () => {
    installFetch(() =>
      jsonResponse(
        {
          detail: {
            code: "stale_plan_version",
            message: "stale",
            details: { current_version: 4 },
          },
        },
        409,
      ),
    );

    const error = await rejection(getJson("/api/plans"));

    expect(error.status).toBe(409);
    expect(error.code).toBe("stale_plan_version");
    expect(error.details).toEqual({ current_version: 4 });
  });

  it("falls back to the helper code for a FastAPI validation body", async () => {
    installFetch(() =>
      jsonResponse(
        { detail: [{ loc: ["body"], msg: "field required", type: "missing" }] },
        422,
      ),
    );

    const error = await rejection(getJson("/api/things"));

    expect(error.status).toBe(422);
    expect(error.code).toBe("investigation_api_error");
  });

  it("reports a transport failure as status 0 / network_error", async () => {
    installFetch(() => {
      throw new TypeError("fetch failed");
    });

    const error = await rejection(getJson("/api/things"));

    expect(error.status).toBe(0);
    expect(error.code).toBe("network_error");
    expect(error.details).toBeNull();
  });

  it("builds and encodes investigation paths", () => {
    expect(investigationPath("inv 1")).toBe("/api/investigations/inv%201");
    expect(investigationPath("inv-1", "/tasks")).toBe(
      "/api/investigations/inv-1/tasks",
    );
    expect(encodeSegment("rev-abc/def")).toBe("rev-abc%2Fdef");
  });

  it("renders only defined query parameters", () => {
    expect(queryString({ a: "x y", b: 2, c: undefined })).toBe("?a=x+y&b=2");
    expect(queryString({})).toBe("");
  });
});
