/**
 * One error-text mapper for the whole M24 cockpit.
 *
 * `WorkshopApiError` keeps its typed `code`; any other failure falls back to
 * its message. Views never print a raw response body.
 */

import { isWorkshopApiError } from "../api/client";

export function workshopErrorText(error: unknown, fallback: string): string {
  if (isWorkshopApiError(error)) return `${fallback} (${error.code})`;
  if (error instanceof Error && error.message !== "") {
    return `${fallback} ${error.message}`;
  }
  return fallback;
}
