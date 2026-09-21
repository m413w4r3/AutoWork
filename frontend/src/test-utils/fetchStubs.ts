type FetchHandler = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Response | Promise<Response>;

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

const EDITION_PRODUCTION_BOARD =
  /\/api\/editions\/([^/?]+)\/production(?:\?|$)/;

/**
 * Answers the endpoints polled by components mounted *alongside* the one under
 * test with their real "nothing here yet" state, and delegates everything else
 * to `fallback`.
 *
 * The edition production board always answers 200, with an empty board when
 * nothing was produced; a Subject without any run answers 404 on its latest-run
 * shortcut. DiscoveryPanel polls `/api/jobs?...` to know whether the ChatGPT
 * bridge is busy planning a merge. A mock that returns the same payload for
 * every URL feeds them a shape they cannot render, so each needs its own empty
 * answer.
 */
export function withProductionNotStarted(fallback: FetchHandler): FetchHandler {
  return (input, init) => {
    const url = urlOf(input);
    const board = EDITION_PRODUCTION_BOARD.exec(url);
    if (board && (init?.method ?? "GET") === "GET") {
      return Response.json({
        edition_id: decodeURIComponent(board[1] ?? ""),
        active_batch: null,
        subjects: [],
        recent_batches: [],
      });
    }
    if (url.includes("/production")) {
      return new Response(null, { status: 404 });
    }
    if (url.includes("/api/jobs?")) {
      return Response.json([]);
    }
    return fallback(input, init);
  };
}
