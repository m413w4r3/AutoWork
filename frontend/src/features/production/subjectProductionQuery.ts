import { queryOptions } from "@tanstack/react-query";

import {
  getSubjectProduction,
  shouldPollProduction,
  type ProductionStatus,
} from "../../api/production";

/** Interval shared with the batch console: watch a live run, nothing more. */
const PRODUCTION_POLL_MS = 2_000;

/**
 * Single source of truth for the per-subject production read: every surface
 * that shows a `ProductionStatus` (workbench, dashboard) must share the cache
 * key and the polling rule, otherwise the dashboard refetches runs the
 * workbench already holds.
 *
 * `null` data is a real answer — no production started — not a miss.
 */
export function subjectProductionQuery(subjectId: string) {
  return queryOptions<ProductionStatus | null>({
    queryKey: ["production", subjectId],
    queryFn: () => getSubjectProduction(subjectId),
    refetchInterval: (query) =>
      shouldPollProduction(query.state.data?.status)
        ? PRODUCTION_POLL_MS
        : false,
  });
}
