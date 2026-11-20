import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import {
  saveManualEditorialEdit,
  type EditorialManualEditOperation,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

export interface ManualEditorialEditInput {
  occurrenceId: string;
  snapshotId: string;
  operation: EditorialManualEditOperation;
  text: string;
}

export function useManualEditorialEdit(subjectId: string) {
  const queryClient = useQueryClient();
  const idempotencyKeys = useRef(new Map<string, string>());
  const requestIdentity = (input: ManualEditorialEditInput) =>
    JSON.stringify([
      input.occurrenceId,
      input.snapshotId,
      input.operation,
      input.text,
    ]);

  const idempotencyKeyFor = (input: ManualEditorialEditInput) => {
    const identity = requestIdentity(input);
    let key = idempotencyKeys.current.get(identity);
    if (!key) {
      key = crypto.randomUUID();
      idempotencyKeys.current.set(identity, key);
    }
    return key;
  };

  return useMutation({
    mutationFn: (input: ManualEditorialEditInput) =>
      saveManualEditorialEdit(
        subjectId,
        input.occurrenceId,
        idempotencyKeyFor(input),
        {
          snapshot_id: input.snapshotId,
          operation: input.operation,
          text: input.text,
        },
      ),
    onSuccess: async (_result, input) => {
      idempotencyKeys.current.delete(requestIdentity(input));
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.snapshot(subjectId),
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
        queryClient.invalidateQueries({
          queryKey: editorialWorkspaceQueryKeys.serverDraft(
            subjectId,
            input.occurrenceId,
          ),
        }),
      ]);
    },
  });
}
