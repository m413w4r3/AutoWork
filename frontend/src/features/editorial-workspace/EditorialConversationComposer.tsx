import type { FormEvent, KeyboardEvent, RefObject } from "react";

interface EditorialConversationComposerProps {
  draft: string;
  active: boolean;
  canPropose: boolean;
  controlsLocked: boolean;
  hasQueryError: boolean;
  sendPending: boolean;
  sendSuccess: boolean;
  sendKind: "message" | "propose" | undefined;
  sendErrorMessage: string | null;
  proposalLabel?: string;
  submissionBelongsToCurrentTarget: boolean;
  composerRef: RefObject<HTMLTextAreaElement | null>;
  sendAnnouncementRef: RefObject<HTMLParagraphElement | null>;
  onDraftChange: (value: string) => void;
  onSuggestion: (value: string) => void;
  onSubmit: (kind: "message" | "propose") => void;
}

const suggestions = [
  "Clarifier le contexte de ce passage",
  "Raccourcir ce passage sans perdre les faits établis",
  "Préciser le lien avec les preuves admises",
];

export function EditorialConversationComposer({
  draft,
  active,
  canPropose,
  controlsLocked,
  hasQueryError,
  sendPending,
  sendSuccess,
  sendKind,
  sendErrorMessage,
  proposalLabel = "Générer une proposition",
  submissionBelongsToCurrentTarget,
  composerRef,
  sendAnnouncementRef,
  onDraftChange,
  onSuggestion,
  onSubmit,
}: EditorialConversationComposerProps) {
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit("message");
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      onSubmit("message");
    }
  }

  return (
    <form className="editorial-conversation__composer" onSubmit={handleSubmit}>
      <label htmlFor="editorial-message-composer">Votre message à Bridge</label>
      <textarea
        ref={composerRef}
        id="editorial-message-composer"
        rows={3}
        value={draft}
        placeholder="Que souhaitez-vous modifier ou clarifier ?"
        disabled={active || sendPending}
        onChange={(event) => onDraftChange(event.currentTarget.value)}
        onKeyDown={handleKeyDown}
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
            onClick={() => onSuggestion(suggestion)}
          >
            {suggestion}
          </button>
        ))}
      </div>
      <div className="editorial-conversation__actions">
        <button
          className="editorial-conversation__send"
          type="submit"
          disabled={!draft.trim() || controlsLocked || hasQueryError}
        >
          {sendPending && sendKind === "message"
            ? "Envoi en cours…"
            : "Envoyer un message"}
        </button>
        {canPropose ? (
          <button
            className="editorial-conversation__propose"
            type="button"
            disabled={!draft.trim() || controlsLocked || hasQueryError}
            onClick={() => onSubmit("propose")}
          >
            {sendPending && sendKind === "propose"
              ? "Génération en cours…"
              : proposalLabel}
          </button>
        ) : null}
      </div>
      <p
        ref={sendAnnouncementRef}
        className="editorial-conversation__announcement"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        tabIndex={-1}
      >
        {sendPending && submissionBelongsToCurrentTarget
          ? sendKind === "propose"
            ? "Génération de la proposition en cours."
            : "Envoi du message en cours."
          : sendSuccess && submissionBelongsToCurrentTarget
            ? sendKind === "propose"
              ? "Demande de proposition envoyée à Bridge."
              : "Message envoyé."
            : ""}
      </p>
      {sendErrorMessage && submissionBelongsToCurrentTarget ? (
        <p className="editorial-conversation__error" role="alert">
          {sendErrorMessage}
        </p>
      ) : null}
    </form>
  );
}
