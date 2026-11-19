import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/editions";
import {
  getEditorialThread,
  sendEditorialThreadMessage,
  type EditorialThreadBridgeStatus,
  type EditorialThreadRead,
  type EditorialThreadSendResult,
  type EditorialThreadTimelineEvent,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

interface EditorialConversationProps {
  subjectId: string;
  occurrenceId: string;
  snapshotId: string;
  targetLabel: string;
  onChangeTarget: () => void;
}

interface SendMessageVariables {
  subjectId: string;
  occurrenceId: string;
  snapshotId: string;
  text: string;
}

const eventLabels: Record<EditorialThreadTimelineEvent["kind"], string> = {
  user_message: "Message",
  bridge_reply: "Réponse Bridge",
  proposal: "Proposition",
  accepted: "Proposition acceptée",
  rejected: "Proposition rejetée",
  refused: "Refus",
  reconciliation: "Réconciliation",
  generation: "État de génération",
};

const bridgeLabels: Record<EditorialThreadRead["bridge_status"], string> = {
  ready: "La discussion est prête.",
  waiting_for_capacity:
    "Votre demande attend qu’une capacité Bridge soit disponible. Son état sera suivi dans ce fil.",
  generating:
    "Bridge traite votre demande. L’état sera mis à jour dans ce fil.",
  reply_available:
    "Une réponse est disponible dans ce fil. Consultez-la avant toute décision.",
  refused:
    "Bridge a refusé la demande. Aucune modification canonique n’a été appliquée.",
  session_lost:
    "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre.",
  reconciliation_required:
    "L’état de votre envoi est incertain. Vérifiez son état dans ce fil avant de continuer ; la demande ne sera pas renvoyée automatiquement.",
};

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

function ThreadTimeline({
  events,
}: {
  events: EditorialThreadTimelineEvent[];
}) {
  if (events.length === 0)
    return <p>Aucun échange n’a encore eu lieu pour ce bloc.</p>;
  return (
    <ol
      className="editorial-conversation__timeline"
      role="log"
      aria-label="Historique de la discussion"
      aria-live="polite"
      aria-relevant="additions"
      aria-atomic="false"
    >
      {events.map((event) => (
        <li key={event.id}>
          <article className="editorial-conversation__event">
            <h4>
              {eventLabels[event.kind]} <span>· {event.sequence}</span>
            </h4>
            {event.body ? <p>{event.body}</p> : null}
          </article>
        </li>
      ))}
    </ol>
  );
}

const suggestions = [
  "Clarifier le contexte de ce passage",
  "Raccourcir ce passage sans perdre les faits établis",
  "Préciser le lien avec les preuves admises",
];

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

export function EditorialConversation({
  subjectId,
  occurrenceId,
  snapshotId,
  targetLabel,
  onChangeTarget,
}: EditorialConversationProps) {
  const queryClient = useQueryClient();
  const queryKey = editorialWorkspaceQueryKeys.thread(
    subjectId,
    occurrenceId,
    snapshotId,
  );
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draft = drafts[occurrenceId] ?? "";
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const bridgeStatusRef = useRef<HTMLParagraphElement>(null);
  const sendAnnouncementRef = useRef<HTMLParagraphElement>(null);
  const focusAfterSubmitRef = useRef<string | null>(null);
  const restoreComposerFocusRef = useRef<string | null>(null);
  const controlsWereLockedRef = useRef(false);
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
    }: SendMessageVariables) =>
      sendEditorialThreadMessage(targetSubjectId, targetOccurrenceId, text),
    onSuccess: (result, variables) => {
      setDrafts((current) => {
        const next = { ...current };
        delete next[variables.occurrenceId];
        return next;
      });
      const targetQueryKey = editorialWorkspaceQueryKeys.thread(
        variables.subjectId,
        variables.occurrenceId,
        variables.snapshotId,
      );
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
      const targetQueryKey = editorialWorkspaceQueryKeys.thread(
        variables.subjectId,
        variables.occurrenceId,
        variables.snapshotId,
      );
      void queryClient.invalidateQueries({ queryKey: targetQueryKey });
    },
  });
  const submissionBelongsToCurrentTarget =
    sendMutation.variables?.subjectId === subjectId &&
    sendMutation.variables?.occurrenceId === occurrenceId &&
    sendMutation.variables?.snapshotId === snapshotId;
  const submissionUncertain =
    submissionBelongsToCurrentTarget &&
    isUncertainSubmissionError(sendMutation.error);
  const active =
    threadQuery.data?.bridge_status === "generating" ||
    threadQuery.data?.bridge_status === "waiting_for_capacity" ||
    threadQuery.data?.bridge_status === "reconciliation_required" ||
    submissionUncertain;
  const controlsLocked = active || sendMutation.isPending;
  const canCompose =
    threadQuery.data !== undefined &&
    threadQuery.data.kind !== "stale" &&
    !threadQuery.isError;

  useEffect(() => {
    if (submissionUncertain && controlsLocked) {
      bridgeStatusRef.current?.focus({ preventScroll: true });
      return;
    }

    if (focusAfterSubmitRef.current === occurrenceId) {
      focusAfterSubmitRef.current = null;
      restoreComposerFocusRef.current = occurrenceId;
      if (controlsLocked) {
        sendAnnouncementRef.current?.focus({ preventScroll: true });
        controlsWereLockedRef.current = true;
      } else {
        restoreComposerFocusRef.current = null;
        composerRef.current?.focus({ preventScroll: true });
      }
      return;
    }

    if (controlsWereLockedRef.current && !controlsLocked) {
      controlsWereLockedRef.current = false;
      const restoreOccurrenceId = restoreComposerFocusRef.current;
      restoreComposerFocusRef.current = null;
      if (restoreOccurrenceId === occurrenceId) {
        composerRef.current?.focus({ preventScroll: true });
      }
      return;
    }

    if (controlsLocked) controlsWereLockedRef.current = true;
  }, [controlsLocked, occurrenceId, submissionUncertain]);

  function submitMessage() {
    const message = draft.trim();
    if (!message || sendMutation.isPending || active) return;
    focusAfterSubmitRef.current = occurrenceId;
    sendMutation.mutate({ subjectId, occurrenceId, snapshotId, text: message });
  }

  return (
    <section
      className="editorial-conversation"
      aria-labelledby="editorial-conversation-heading"
      aria-busy={threadQuery.isPending}
    >
      <h3 id="editorial-conversation-heading">Discussion</h3>
      <div className="editorial-conversation__target">
        <p className="editorial-conversation__target-label">
          Discussion sur : {targetLabel}
        </p>
        <button
          className="editorial-conversation__change-target"
          type="button"
          onClick={onChangeTarget}
        >
          Changer de cible
        </button>
      </div>
      {threadQuery.isPending ? (
        <p role="status">Chargement de la discussion…</p>
      ) : null}
      {threadQuery.isError ? (
        <p role="alert">{threadQuery.error.message}</p>
      ) : null}
      {threadQuery.data?.kind === "unopened" ? (
        <p>Aucune discussion n’a encore été ouverte pour ce bloc.</p>
      ) : null}
      {threadQuery.data?.kind === "stale" ? (
        <p role="status">
          {threadQuery.data.binding_state === "ambiguous"
            ? "L’identité de ce paragraphe historique est ambiguë."
            : "Ce paragraphe a changé. Rechargez l’atelier."}
        </p>
      ) : null}
      {threadQuery.data && threadQuery.data.kind !== "unopened" ? (
        <>
          {threadQuery.data.origin ? (
            <p className="editorial-conversation__origin">
              Version de référence du fil : v
              {threadQuery.data.origin.artifact_version}
            </p>
          ) : null}
          <p
            ref={bridgeStatusRef}
            role="status"
            aria-live="polite"
            aria-atomic="true"
            tabIndex={-1}
          >
            {bridgeLabels[threadQuery.data.bridge_status]}
          </p>
          <ThreadTimeline events={threadQuery.data.timeline} />
        </>
      ) : null}
      {canCompose ? (
        <form
          className="editorial-conversation__composer"
          onSubmit={(event) => {
            event.preventDefault();
            submitMessage();
          }}
        >
          <label htmlFor="editorial-message-composer">
            Votre message à Bridge
          </label>
          <textarea
            ref={composerRef}
            id="editorial-message-composer"
            rows={3}
            value={draft}
            placeholder="Que souhaitez-vous modifier ou clarifier ?"
            disabled={active || sendMutation.isPending}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setDrafts((current) => ({
                ...current,
                [occurrenceId]: value,
              }));
              if (sendMutation.isError || sendMutation.isSuccess)
                sendMutation.reset();
            }}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                submitMessage();
              }
            }}
          />
          <div
            className="editorial-conversation__suggestions"
            aria-label="Suggestions de message"
          >
            {suggestions.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                disabled={controlsLocked}
                onClick={() => {
                  setDrafts((current) => ({
                    ...current,
                    [occurrenceId]: suggestion,
                  }));
                  composerRef.current?.focus();
                }}
              >
                {suggestion}
              </button>
            ))}
          </div>
          <button
            className="editorial-conversation__send"
            type="submit"
            disabled={!draft.trim() || controlsLocked || threadQuery.isError}
          >
            {sendMutation.isPending ? "Envoi en cours…" : "Envoyer un message"}
          </button>
          <p
            ref={sendAnnouncementRef}
            className="editorial-conversation__announcement"
            role="status"
            aria-live="polite"
            aria-atomic="true"
            tabIndex={-1}
          >
            {sendMutation.isPending && submissionBelongsToCurrentTarget
              ? "Envoi du message en cours."
              : sendMutation.isSuccess && submissionBelongsToCurrentTarget
                ? "Message envoyé."
                : ""}
          </p>
          {sendMutation.isError && submissionBelongsToCurrentTarget ? (
            <p className="editorial-conversation__error" role="alert">
              {sendMutation.error.message}
            </p>
          ) : null}
        </form>
      ) : null}
    </section>
  );
}
