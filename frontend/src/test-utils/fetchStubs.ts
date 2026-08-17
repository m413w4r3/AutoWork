/**
 * Shared fetch stubs for component tests.
 */

type FetchHandler = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Response | Promise<Response>;

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

/**
 * Answers 404 on the production endpoints — the real "nothing started yet"
 * state — and delegates everything else to `fallback`.
 *
 * Components mounted alongside the one under test (SubjectProduction,
 * ProductionQueue) poll those endpoints, so a mock that returns the same
 * payload for every URL feeds them a shape they cannot render.
 */
export function withProductionNotStarted(fallback: FetchHandler): FetchHandler {
  return (input, init) => {
    if (urlOf(input).includes("/production")) {
      return new Response(null, { status: 404 });
    }
    return fallback(input, init);
  };
}
