import type { FormEvent, KeyboardEvent, RefObject } from "react";

interface EditorialConversationComposerProps {
  draft: string;
  active: boolean;
  controlsLocked: boolean;
  hasQueryError: boolean;
  sendPending: boolean;
  sendSuccess: boolean;
  sendErrorMessage: string | null;
  submissionBelongsToCurrentTarget: boolean;
  composerRef: RefObject<HTMLTextAreaElement | null>;
  sendAnnouncementRef: RefObject<HTMLParagraphElement | null>;
  onDraftChange: (value: string) => void;
  onSuggestion: (value: string) => void;
  onSubmit: () => void;
}

const suggestions = [
  "Clarifier le contexte de ce passage",
  "Raccourcir ce passage sans perdre les faits établis",
  "Préciser le lien avec les preuves admises",
];

export function EditorialConversationComposer({
  draft,
  active,
  controlsLocked,
  hasQueryError,
  sendPending,
  sendSuccess,
  sendErrorMessage,
  submissionBelongsToCurrentTarget,
  composerRef,
  sendAnnouncementRef,
  onDraftChange,
  onSuggestion,
  onSubmit,
}: EditorialConversationComposerProps) {
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      onSubmit();
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
      <button
        className="editorial-conversation__send"
        type="submit"
        disabled={!draft.trim() || controlsLocked || hasQueryError}
      >
        {sendPending ? "Envoi en cours…" : "Envoyer un message"}
      </button>
      <p
        ref={sendAnnouncementRef}
        className="editorial-conversation__announcement"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        tabIndex={-1}
      >
        {sendPending && submissionBelongsToCurrentTarget
          ? "Envoi du message en cours."
          : sendSuccess && submissionBelongsToCurrentTarget
            ? "Message envoyé."
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
