import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import {
  acceptEditorialProposal,
  prepareEnrichmentRevision,
  type EditorialEnrichmentRevisionInput,
  type EditorialProposalAcceptResult,
  type EditorialVisualRevisionProposalRead,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

export interface ManualVisualRevisionInput {
  occurrenceId: string;
  revision: EditorialEnrichmentRevisionInput;
}

export interface ManualVisualRevisionResult {
  proposal: EditorialVisualRevisionProposalRead;
  acceptance: EditorialProposalAcceptResult;
}

/**
 * Manual visual changes are prepared as typed proposals and adopted
 * immediately by the explicit confirmation that produced them. No model or
 * Bridge call is involved: `prepare` runs the server planner and `accept`
 * adopts the same patch through the single adoption writer.
 *
 * The prepare idempotency key stays stable until the whole prepare+accept
 * sequence succeeds, so a failed acceptance retries against the same proposal
 * instead of preparing a duplicate.
 */
export function useManualVisualRevision(subjectId: string) {
  const queryClient = useQueryClient();
  const prepareKeys = useRef(new Map<string, string>());
  const requestIdentity = (input: ManualVisualRevisionInput) =>
    JSON.stringify([input.occurrenceId, input.revision]);

  const prepareKeyFor = (input: ManualVisualRevisionInput) => {
    const identity = requestIdentity(input);
    let key = prepareKeys.current.get(identity);
    if (!key) {
      key = crypto.randomUUID();
      prepareKeys.current.set(identity, key);
    }
    return key;
  };

  return useMutation<
    ManualVisualRevisionResult,
    Error,
    ManualVisualRevisionInput
  >({
    mutationFn: async (input) => {
      const proposal = await prepareEnrichmentRevision(
        subjectId,
        input.occurrenceId,
        prepareKeyFor(input),
        input.revision,
      );
      if (!proposal.patch_sha256) {
        throw new Error("L’empreinte du patch est indisponible.");
      }
      const acceptance = await acceptEditorialProposal(
        subjectId,
        input.occurrenceId,
        proposal.id,
        {
          expected_state_version: proposal.state_version,
          patch_sha256: proposal.patch_sha256,
        },
      );
      return { proposal, acceptance };
    },
    onSuccess: async (_result, input) => {
      prepareKeys.current.delete(requestIdentity(input));
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
