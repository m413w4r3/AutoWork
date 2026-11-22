import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import type { EditorialEnrichmentRevisionInput } from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";
import {
  prepareAndAcceptVisualRevision,
  type ManualVisualRevisionOutcome,
} from "./manualVisualRevision";

export interface ManualVisualRevisionInput {
  occurrenceId: string;
  revision: EditorialEnrichmentRevisionInput;
}

export type ManualVisualRevisionResult = ManualVisualRevisionOutcome;

/**
 * Manual visual changes are prepared as typed proposals and adopted
 * immediately by the explicit confirmation that produced them. No model or
 * Bridge call is involved: `prepare` runs the server planner and `accept`
 * adopts the same patch through the single adoption writer.
 *
 * The prepare and accept idempotency keys stay stable until the whole
 * sequence succeeds, so a failed acceptance (including a lost accept response)
 * retries against the same proposal and the same decision instead of preparing
 * a duplicate or reporting a false conflict.
 */
export function useManualVisualRevision(subjectId: string) {
  const queryClient = useQueryClient();
  const prepareKeys = useRef(new Map<string, string>());
  const acceptKeys = useRef(new Map<string, string>());
  const requestIdentity = (input: ManualVisualRevisionInput) =>
    JSON.stringify([input.occurrenceId, input.revision]);

  const stableKey = (keys: Map<string, string>, identity: string): string => {
    let key = keys.get(identity);
    if (!key) {
      key = crypto.randomUUID();
      keys.set(identity, key);
    }
    return key;
  };

  return useMutation<
    ManualVisualRevisionResult,
    Error,
    ManualVisualRevisionInput
  >({
    mutationFn: (input) => {
      const identity = requestIdentity(input);
      return prepareAndAcceptVisualRevision(
        subjectId,
        input.occurrenceId,
        input.revision,
        {
          prepareIdempotencyKey: stableKey(prepareKeys.current, identity),
          acceptIdempotencyKey: stableKey(acceptKeys.current, identity),
        },
      );
    },
    onSuccess: async (_result, input) => {
      const identity = requestIdentity(input);
      prepareKeys.current.delete(identity);
      acceptKeys.current.delete(identity);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.snapshot(subjectId),
        }),
        queryClient.invalidateQueries({
          queryKey: ["editorial-pdf-status", subjectId],
        }),
        queryClient.invalidateQueries({
          queryKey: ["editorial-composition-status", subjectId],
        }),
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.history(subjectId),
        }),
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.threadsForOccurrence(
            subjectId,
            input.occurrenceId,
          ),
        }),
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.proposalsForOccurrence(
            subjectId,
            input.occurrenceId,
          ),
        }),
      ]);
    },
  });
}
