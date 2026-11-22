/**
 * Transport seam of the M24 workshop API.
 *
 * All workshop modules go through the existing `investigationRequest` helper
 * (idempotency headers, JSON parsing, 204 handling). This module only adds the
 * browser-side typed error: `WorkshopApiError` exposes the HTTP status, the
 * backend `detail.code` and the bounded `detail.details` map, and it never
 * swallows a network failure.
 */

import {
  InvestigationApiError,
  investigationRequest,
} from "../../../api/investigation";

export { createIdempotencyKey } from "../../../api/investigation";

const NETWORK_ERROR_MESSAGE =
  "La requête de l’atelier a échoué (network_error).";

/** Typed refusal surfaced to the cockpit. `status` is 0 for a transport error. */
export class WorkshopApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | null;

  constructor(
    message: string,
    status: number,
    code: string,
    details: Record<string, unknown> | null = null,
  ) {
    super(message);
    this.name = "WorkshopApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * `investigationRequest` carries the whole `detail` object; the workshop
 * contract exposes the bounded `detail.details` map instead.
 */
function nestedDetails(
  detail: Record<string, unknown> | null,
): Record<string, unknown> | null {
  if (detail === null) return null;
  const nested = detail["details"];
  return isRecord(nested) ? nested : null;
}

/** Normalise anything thrown by the shared request helper into one type. */
export function toWorkshopApiError(error: unknown): WorkshopApiError {
  if (error instanceof WorkshopApiError) return error;
  if (error instanceof InvestigationApiError) {
    return new WorkshopApiError(
      error.message,
      error.status,
      error.code,
      nestedDetails(error.details),
    );
  }
  return new WorkshopApiError(NETWORK_ERROR_MESSAGE, 0, "network_error");
}

export function isWorkshopApiError(error: unknown): error is WorkshopApiError {
  return error instanceof WorkshopApiError;
}

async function call<T>(url: string, init: RequestInit): Promise<T> {
  try {
    return await investigationRequest<T>(url, init);
  } catch (error) {
    throw toWorkshopApiError(error);
  }
}

function buildInit(
  method: string,
  body: unknown,
  idempotencyKey: string | undefined,
  signal: AbortSignal | undefined,
): RequestInit {
  const init: RequestInit = { method };
  const headers: Record<string, string> = {};
  if (signal !== undefined) init.signal = signal;
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  if (idempotencyKey !== undefined) headers["Idempotency-Key"] = idempotencyKey;
  if (Object.keys(headers).length > 0) init.headers = headers;
  return init;
}

export function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  return call<T>(url, buildInit("GET", undefined, undefined, signal));
}

/**
 * `idempotencyKey` is mandatory for every route that declares the header and
 * for the session instruction enqueue route, which reads it directly.
 */
export function postJson<T>(
  url: string,
  body: unknown,
  idempotencyKey?: string,
  signal?: AbortSignal,
): Promise<T> {
  return call<T>(url, buildInit("POST", body, idempotencyKey, signal));
}

export function postEmpty<T>(
  url: string,
  idempotencyKey?: string,
  signal?: AbortSignal,
): Promise<T> {
  return call<T>(url, buildInit("POST", undefined, idempotencyKey, signal));
}

export function putJson<T>(
  url: string,
  body: unknown,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<T> {
  return call<T>(url, buildInit("PUT", body, idempotencyKey, signal));
}

export function encodeSegment(value: string): string {
  return encodeURIComponent(value);
}

export function investigationPath(
  investigationId: string,
  suffix = "",
): string {
  return `/api/investigations/${encodeSegment(investigationId)}${suffix}`;
}

export function queryString(
  params: Record<string, string | number | boolean | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered === "" ? "" : `?${rendered}`;
}
