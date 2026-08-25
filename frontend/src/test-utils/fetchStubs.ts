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
 * Answers the endpoints polled by components mounted *alongside* the one under
 * test with their real "nothing here yet" state, and delegates everything else
 * to `fallback`.
 *
 * SubjectProduction and ProductionQueue poll `/production`; DiscoveryMergeReview
 * polls `/merge-runs` and, to know whether the ChatGPT bridge is busy planning
 * a merge, `/api/jobs?...`. A mock that returns the same payload for every URL
 * feeds them a shape they cannot render, so each needs its own empty answer.
 */
export function withProductionNotStarted(fallback: FetchHandler): FetchHandler {
  return (input, init) => {
    const url = urlOf(input);
    if (url.includes("/production")) {
      return new Response(null, { status: 404 });
    }
    if (url.includes("/merge-runs")) {
      return Response.json([]);
    }
    if (url.includes("/api/jobs?")) {
      return Response.json([]);
    }
    return fallback(input, init);
  };
}
