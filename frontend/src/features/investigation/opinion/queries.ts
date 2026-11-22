import { useQuery } from "@tanstack/react-query";

import { analystOpinionQueryKeys } from "../../../api/analystOpinion";
import {
  getInvestigationActivity,
  investigationOperatorQueryKeys,
} from "../../../api/investigationOperator";
import { getJson, investigationPath } from "../api/client";
import type { OpinionReadPayloadV1 } from "./types";

/**
 * Nested under the official opinion key so the approval/publication hooks
 * (which invalidate `analystOpinionQueryKeys.detail`) also refresh this
 * richer projection.
 */
export const opinionNotesKeys = {
  detail: (investigationId: string) =>
    [...analystOpinionQueryKeys.detail(investigationId), "notes"] as const,
};

export function getOpinionRead(
  investigationId: string,
  signal?: AbortSignal,
): Promise<OpinionReadPayloadV1> {
  return getJson<OpinionReadPayloadV1>(
    investigationPath(investigationId, "/opinion"),
    signal,
  );
}

export function useOpinionRead(investigationId: string) {
  return useQuery({
    queryKey: opinionNotesKeys.detail(investigationId),
    queryFn: ({ signal }) => getOpinionRead(investigationId, signal),
    enabled: investigationId !== "",
  });
}

/**
 * The drafting request pins the investigation working-state version. The
 * activity projection is the cockpit read that exposes it; no polling is
 * added by this view (the shell owns live activity polling).
 */
export function useInvestigationWorkingState(investigationId: string) {
  return useQuery({
    queryKey: investigationOperatorQueryKeys.activity(investigationId),
    queryFn: () => getInvestigationActivity(investigationId, null),
    enabled: investigationId !== "",
    refetchInterval: false,
  });
}
