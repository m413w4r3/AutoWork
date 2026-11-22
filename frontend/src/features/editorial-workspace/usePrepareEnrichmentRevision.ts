import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import {
  prepareEnrichmentRevision,
  type EditorialEnrichmentRevisionInput,
  type EditorialVisualRevisionProposalRead,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

export interface PrepareEnrichmentRevisionInput {
  occurrenceId: string;
  revision: EditorialEnrichmentRevisionInput;
}

/**
 * Prepare one typed visual revision (caption, move, reorder, ...) of an
 * existing enrichment element. The mutation returns the persisted proposal;
 * acceptance stays a separate explicit decision.
 */
export function usePrepareEnrichmentRevision(subjectId: string) {
  const queryClient = useQueryClient();
  const idempotencyKeys = useRef(new Map<string, string>());
  const requestIdentity = (input: PrepareEnrichmentRevisionInput) =>
    JSON.stringify([input.occurrenceId, input.revision]);

  const idempotencyKeyFor = (input: PrepareEnrichmentRevisionInput) => {
    const identity = requestIdentity(input);
    let key = idempotencyKeys.current.get(identity);
    if (!key) {
      key = crypto.randomUUID();
      idempotencyKeys.current.set(identity, key);
    }
    return key;
  };

  return useMutation<
    EditorialVisualRevisionProposalRead,
    Error,
    PrepareEnrichmentRevisionInput
  >({
    mutationFn: (input) =>
      prepareEnrichmentRevision(
        subjectId,
        input.occurrenceId,
        idempotencyKeyFor(input),
        input.revision,
      ),
    onSuccess: async (_result, input) => {
      idempotencyKeys.current.delete(requestIdentity(input));
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.snapshot(subjectId),
        }),
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.proposalsForOccurrence(
            subjectId,
            input.occurrenceId,
          ),
        }),
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.history(subjectId),
        }),
      ]);
    },
  });
}
