import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../../api/editions";
import {
  deleteEditorialServerDraft,
  getEditorialServerDraft,
  putEditorialServerDraft,
  type EditorialServerDraft,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

export type EditorialServerDraftState =
  | { kind: "available"; draft: EditorialServerDraft }
  | { kind: "expired"; expiresAt: string | null }
  | { kind: "missing" };

export function useEditorialServerDraft(
  subjectId: string,
  occurrenceId: string | null,
) {
  return useQuery({
    queryKey: editorialWorkspaceQueryKeys.serverDraft(
      subjectId,
      occurrenceId ?? "unselected",
    ),
    queryFn: async (): Promise<EditorialServerDraftState> => {
      if (occurrenceId === null) return { kind: "missing" };
      try {
        return {
          kind: "available",
          draft: await getEditorialServerDraft(subjectId, occurrenceId),
        };
      } catch (error) {
        if (error instanceof ApiError && error.code === "draft_not_found") {
          return { kind: "missing" };
        }
        if (error instanceof ApiError && error.code === "draft_expired") {
          const expiresAt = error.details?.expires_at;
          return {
            kind: "expired",
            expiresAt: typeof expiresAt === "string" ? expiresAt : null,
          };
        }
        throw error;
      }
    },
    enabled: occurrenceId !== null,
    retry: false,
  });
}

export function useSaveEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
) {
  const queryClient = useQueryClient();
  const queryKey = editorialWorkspaceQueryKeys.serverDraft(
    subjectId,
    occurrenceId,
  );
  return useMutation({
    mutationFn: (input: {
      snapshot_id: string;
      text: string;
      expected_version: number;
    }) => putEditorialServerDraft(subjectId, occurrenceId, input),
    onSuccess: (draft) =>
      queryClient.setQueryData(queryKey, { kind: "available", draft }),
    onError: (error) => {
      if (error instanceof ApiError && error.code === "draft_expired") {
        const expiresAt = error.details?.expires_at;
        queryClient.setQueryData(queryKey, {
          kind: "expired",
          expiresAt: typeof expiresAt === "string" ? expiresAt : null,
        });
      }
    },
  });
}

export function useDeleteEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
) {
  const queryClient = useQueryClient();
  const queryKey = editorialWorkspaceQueryKeys.serverDraft(
    subjectId,
    occurrenceId,
  );
  return useMutation({
    mutationFn: (expectedVersion: number) =>
      deleteEditorialServerDraft(subjectId, occurrenceId, expectedVersion),
    onSuccess: () => queryClient.setQueryData(queryKey, { kind: "missing" }),
  });
}
