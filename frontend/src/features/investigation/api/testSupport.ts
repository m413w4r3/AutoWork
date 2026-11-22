/**
 * Test-only helpers for the workshop API contract. No real network: every
 * helper installs a `fetch` stub and records the exact request it received.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactElement, type ReactNode } from "react";
import { vi } from "vitest";

export interface RecordedRequest {
  url: string;
  init: RequestInit | undefined;
}

export interface FetchRecorder {
  readonly calls: RecordedRequest[];
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

export function installFetch(
  handler: (request: RecordedRequest) => Response | Promise<Response>,
): FetchRecorder {
  const calls: RecordedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    (
      input: RequestInfo | URL,
      init?: RequestInit,
    ): Response | Promise<Response> => {
      const request: RecordedRequest = { url: urlOf(input), init };
      calls.push(request);
      return handler(request);
    },
  );
  return { calls };
}

export function lastRequest(recorder: FetchRecorder): RecordedRequest {
  const request = recorder.calls.at(-1);
  if (request === undefined) throw new Error("fetch was not called");
  return request;
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function errorResponse(
  status: number,
  code: string,
  message?: string,
): Response {
  return jsonResponse(
    {
      detail: {
        code,
        ...(message === undefined ? {} : { message }),
      },
    },
    status,
  );
}

export function headersOf(
  init: RequestInit | undefined,
): Record<string, string> {
  const headers = init?.headers;
  if (headers === undefined) return {};
  if (headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  if (Array.isArray(headers)) {
    return Object.fromEntries(headers);
  }
  return { ...headers };
}

/** Parsed JSON body of one recorded request; fails loudly on a missing body. */
export function bodyOf(init: RequestInit | undefined): unknown {
  if (typeof init?.body !== "string") {
    throw new Error("the recorded request carries no JSON body");
  }
  return JSON.parse(init.body) as unknown;
}

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

export function wrapperFor(client: QueryClient) {
  return function QueryWrapper({
    children,
  }: {
    children: ReactNode;
  }): ReactElement {
    return createElement(QueryClientProvider, { client }, children);
  };
}
