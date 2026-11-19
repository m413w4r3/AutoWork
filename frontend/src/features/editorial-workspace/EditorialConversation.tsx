import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getEditorialThread,
  type EditorialThreadRead,
  type EditorialThreadTimelineEvent,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

interface EditorialConversationProps {
  subjectId: string;
  occurrenceId: string;
  snapshotId: string;
  targetLabel: string;
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
      aria-label="Historique de la discussion"
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

export function EditorialConversation({
  subjectId,
  occurrenceId,
  snapshotId,
  targetLabel,
}: EditorialConversationProps) {
  const queryClient = useQueryClient();
  const queryKey = editorialWorkspaceQueryKeys.thread(
    subjectId,
    occurrenceId,
    snapshotId,
  );
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

  return (
    <section
      className="editorial-conversation"
      aria-labelledby="editorial-conversation-heading"
      aria-busy={threadQuery.isPending}
    >
      <h3 id="editorial-conversation-heading">Discussion</h3>
      <p className="editorial-conversation__target">
        Discussion sur : {targetLabel}
      </p>
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
          <p role="status">{bridgeLabels[threadQuery.data.bridge_status]}</p>
          <ThreadTimeline events={threadQuery.data.timeline} />
        </>
      ) : null}
    </section>
  );
}
