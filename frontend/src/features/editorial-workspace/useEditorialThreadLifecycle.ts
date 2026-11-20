import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { ApiError } from "../../api/editions";
import {
  getEditorialThread,
  reconcileEditorialThread,
  sendEditorialThreadMessage,
  startNewEditorialThreadSession,
  type EditorialThreadBridgeStatus,
  type EditorialThreadRead,
  type EditorialThreadSendResult,
  type EditorialThreadTimelineEvent,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

interface ThreadTarget {
  subjectId: string;
  occurrenceId: string;
  snapshotId: string;
}

interface SendMessageVariables extends ThreadTarget {
  text: string;
  kind: "message" | "propose";
}

type ReconcileVariables = ThreadTarget;

interface StartSessionVariables extends ThreadTarget {
  idempotencyKey: string;
}

interface UseEditorialThreadLifecycleOptions extends ThreadTarget {
  onMessageSent: (occurrenceId: string) => void;
}

function threadQueryKey(target: ThreadTarget) {
  return editorialWorkspaceQueryKeys.thread(
    target.subjectId,
    target.occurrenceId,
    target.snapshotId,
  );
}

function mergeThreadRead(
  previous: EditorialThreadRead | undefined,
  next: EditorialThreadRead,
): EditorialThreadRead {
  if (!previous || next.kind === "unopened") return next;
  const nextThreadId = next.thread_id;
  const previousThreadId =
    previous.kind === "unopened" ? null : previous.thread_id;
  if (!nextThreadId || nextThreadId !== previousThreadId) return next;
  const events = new Map<number, EditorialThreadTimelineEvent>();
  for (const event of previous.timeline) events.set(event.sequence, event);
  for (const event of next.timeline) events.set(event.sequence, event);
  return {
    ...next,
    timeline: [...events.values()].sort(
      (left, right) => left.sequence - right.sequence,
    ),
  };
}

function acceptedBridgeStatus(
  result: EditorialThreadSendResult,
): EditorialThreadBridgeStatus | null {
  if (result.generation === "queued" || result.generation === "running")
    return "generating";
  if (result.generation === "waiting_for_capacity")
    return "waiting_for_capacity";
  if (result.generation === "reconciliation_required")
    return "reconciliation_required";
  return null;
}

function isUncertainSubmissionError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === "reconciliation_required" ||
      error.code === "editorial_thread_transport_uncertain")
  );
}

function isIndeterminateStartError(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  if (error instanceof ApiError && error.status === 0) return true;
  return (
    typeof DOMException !== "undefined" &&
    error instanceof DOMException &&
    (error.name === "AbortError" || error.name === "TimeoutError")
  );
}

function sessionStartIdentity(target: ThreadTarget): string {
  return JSON.stringify([target.subjectId, target.occurrenceId]);
}

export function useEditorialThreadLifecycle({
  subjectId,
  occurrenceId,
  snapshotId,
  onMessageSent,
}: UseEditorialThreadLifecycleOptions) {
  const queryClient = useQueryClient();
  const queryKey = threadQueryKey({ subjectId, occurrenceId, snapshotId });
  const sessionStartKeysRef = useRef<Map<string, string>>(new Map());
  const threadQuery = useQuery({
    queryKey,
    enabled: Boolean(subjectId && occurrenceId),
    retry: false,
    queryFn: async () => {
      const previous = queryClient.getQueryData<EditorialThreadRead>(queryKey);
      const response = await getEditorialThread(subjectId, occurrenceId, {
        after_sequence: previous?.event_sequence ?? 0,
        etag: previous?.etag,
      });
      if (response === null) return previous;
      return mergeThreadRead(previous, response);
    },
    refetchInterval: (query) =>
      query.state.data?.bridge_status === "generating" ||
      query.state.data?.bridge_status === "waiting_for_capacity"
        ? 1_500
        : false,
  });
  const sendMutation = useMutation({
    mutationFn: ({
      subjectId: targetSubjectId,
      occurrenceId: targetOccurrenceId,
      text,
      kind,
    }: SendMessageVariables) =>
      sendEditorialThreadMessage(
        targetSubjectId,
        targetOccurrenceId,
        text,
        kind,
      ),
    onSuccess: (result, variables) => {
      onMessageSent(variables.occurrenceId);
      const targetQueryKey = threadQueryKey(variables);
      queryClient.setQueryData<EditorialThreadRead>(
        targetQueryKey,
        (previous) => {
          if (!previous || previous.kind === "stale") return previous;
          const bridgeStatus = acceptedBridgeStatus(result);
          const sequence = previous.event_sequence + 1;
          const event: EditorialThreadTimelineEvent = {
            id: result.entry_id,
            sequence,
            kind: "user_message",
            actor_id: null,
            proposal_id: null,
            created_at: new Date().toISOString(),
            body: variables.text,
          };
          const timeline = previous.timeline.some(
            (item) => item.id === event.id,
          )
            ? previous.timeline
            : [...previous.timeline, event];
          if (previous.kind === "unopened") {
            return {
              kind: "open",
              thread_id: result.thread_id,
              origin: previous.binding,
              current: previous.current,
              binding_state: "valid",
              bridge_status: bridgeStatus ?? previous.bridge_status,
              event_sequence: Math.max(previous.event_sequence, sequence),
              timeline,
              etag: "",
              previous_sessions: [],
            };
          }
          return {
            ...previous,
            bridge_status: bridgeStatus ?? previous.bridge_status,
            event_sequence: Math.max(previous.event_sequence, sequence),
            timeline,
            etag: "",
          };
        },
      );
      void queryClient.invalidateQueries({ queryKey: targetQueryKey });
    },
    onError: (_error, variables) => {
      void queryClient.invalidateQueries({
        queryKey: threadQueryKey(variables),
      });
    },
  });
  const reconcileMutation = useMutation({
    mutationFn: ({
      subjectId: targetSubjectId,
      occurrenceId: targetOccurrenceId,
    }: ReconcileVariables) =>
      reconcileEditorialThread(targetSubjectId, targetOccurrenceId),
    onSuccess: (_result, variables) => {
      sendMutation.reset();
      void queryClient.invalidateQueries({
        queryKey: threadQueryKey(variables),
      });
    },
  });
  const startSessionMutation = useMutation({
    mutationFn: ({
      subjectId: targetSubjectId,
      occurrenceId: targetOccurrenceId,
      idempotencyKey,
    }: StartSessionVariables) =>
      startNewEditorialThreadSession(
        targetSubjectId,
        targetOccurrenceId,
        idempotencyKey,
      ),
    onSuccess: async (_result, variables) => {
      const identity = sessionStartIdentity(variables);
      if (
        sessionStartKeysRef.current.get(identity) === variables.idempotencyKey
      ) {
        sessionStartKeysRef.current.delete(identity);
      }
      sendMutation.reset();
      reconcileMutation.reset();
      await queryClient.invalidateQueries({
        queryKey: threadQueryKey(variables),
      });
    },
    onError: async (error, variables) => {
      const identity = sessionStartIdentity(variables);
      if (!isIndeterminateStartError(error)) {
        if (
          sessionStartKeysRef.current.get(identity) === variables.idempotencyKey
        ) {
          sessionStartKeysRef.current.delete(identity);
        }
        return;
      }
      const targetQueryKey = threadQueryKey(variables);
      await queryClient.invalidateQueries({ queryKey: targetQueryKey });
      const refreshed =
        queryClient.getQueryData<EditorialThreadRead>(targetQueryKey);
      if (
        refreshed?.kind === "open" &&
        refreshed.bridge_status !== "session_lost" &&
        sessionStartKeysRef.current.get(identity) === variables.idempotencyKey
      ) {
        sessionStartKeysRef.current.delete(identity);
      }
    },
  });

  const matchesCurrentTarget = (target: ThreadTarget | undefined) =>
    target?.subjectId === subjectId &&
    target.occurrenceId === occurrenceId &&
    target.snapshotId === snapshotId;
  const submissionBelongsToCurrentTarget = matchesCurrentTarget(
    sendMutation.variables,
  );
  const submissionUncertain =
    submissionBelongsToCurrentTarget &&
    isUncertainSubmissionError(sendMutation.error);
  const active =
    threadQuery.data?.bridge_status === "generating" ||
    threadQuery.data?.bridge_status === "waiting_for_capacity" ||
    threadQuery.data?.bridge_status === "reconciliation_required" ||
    threadQuery.data?.bridge_status === "session_lost" ||
    reconcileMutation.isPending ||
    startSessionMutation.isPending ||
    submissionUncertain;
  const controlsLocked = active || sendMutation.isPending;
  const canCompose =
    threadQuery.data !== undefined &&
    threadQuery.data.kind !== "stale" &&
    !threadQuery.isError;

  function submitMessage(
    text: string,
    kind: "message" | "propose" = "message",
  ): boolean {
    const message = text.trim();
    if (!message || sendMutation.isPending || active) return false;
    reconcileMutation.reset();
    startSessionMutation.reset();
    sendMutation.mutate({
      subjectId,
      occurrenceId,
      snapshotId,
      text: message,
      kind,
    });
    return true;
  }

  function clearSendResult() {
    if (sendMutation.isError || sendMutation.isSuccess) sendMutation.reset();
  }

  function reconcile() {
    reconcileMutation.mutate({ subjectId, occurrenceId, snapshotId });
  }

  function startNewSession() {
    if (startSessionMutation.isPending) return;
    const identity = sessionStartIdentity({
      subjectId,
      occurrenceId,
      snapshotId,
    });
    let idempotencyKey = sessionStartKeysRef.current.get(identity);
    if (!idempotencyKey) {
      idempotencyKey = crypto.randomUUID();
      sessionStartKeysRef.current.set(identity, idempotencyKey);
    }
    startSessionMutation.mutate({
      subjectId,
      occurrenceId,
      snapshotId,
      idempotencyKey,
    });
  }

  return {
    threadQuery,
    active,
    controlsLocked,
    canCompose,
    submissionBelongsToCurrentTarget,
    submissionKind: sendMutation.variables?.kind,
    submissionUncertain,
    sendState: {
      isPending: sendMutation.isPending,
      isSuccess: sendMutation.isSuccess,
      isError: sendMutation.isError,
      errorMessage: sendMutation.error?.message ?? null,
    },
    reconcileState: {
      isPending: reconcileMutation.isPending,
      isSuccess: reconcileMutation.isSuccess,
      isError: reconcileMutation.isError,
      message: reconcileMutation.data?.message ?? null,
      errorMessage: reconcileMutation.error?.message ?? null,
      belongsToCurrentTarget: matchesCurrentTarget(reconcileMutation.variables),
    },
    startSessionState: {
      isPending: startSessionMutation.isPending,
      isSuccess: startSessionMutation.isSuccess,
      isError: startSessionMutation.isError,
      errorMessage: startSessionMutation.error
        ? isIndeterminateStartError(startSessionMutation.error)
          ? "La réponse n’a pas pu être confirmée. Vérifiez l’état du fil avant de réessayer."
          : startSessionMutation.error.message
        : null,
      belongsToCurrentTarget: matchesCurrentTarget(
        startSessionMutation.variables,
      ),
    },
    submitMessage,
    clearSendResult,
    reconcile,
    startNewSession,
  };
}
