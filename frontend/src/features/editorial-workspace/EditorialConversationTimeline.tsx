import type { EditorialThreadTimelineEvent } from "../../api/editorialWorkspace";

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

interface EditorialConversationTimelineProps {
  events: EditorialThreadTimelineEvent[];
}

export function EditorialConversationTimeline({
  events,
}: EditorialConversationTimelineProps) {
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
