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
  onDraftChange: (value: string) => void;
  onSuggestion: (value: string) => void;
  onSubmit: (kind: "message" | "propose") => void;
}

const suggestions = [
  {
    label: "Raccourcir",
    instruction: "Raccourcis ce passage sans perdre les faits établis.",
  },
  {
    label: "Clarifier",
    instruction: "Clarifie ce passage sans ajouter de faits non étayés.",
  },
  {
    label: "Détailler",
    instruction:
      "Détaille davantage ce passage en t’appuyant sur les preuves admises.",
  },
  {
    label: "Reformuler",
    instruction: "Reformule ce passage sans en changer le sens ni les faits.",
  },
  {
    label: "Corriger les imprécisions",
    instruction:
      "Corrige les imprécisions de ce passage sans introduire de faits non étayés.",
  },
  {
    label: "Améliorer la structure",
    instruction: "Améliore la structure de ce passage sans modifier les faits.",
  },
  {
    label: "Proposer une autre formulation",
    instruction: "Propose une autre formulation de ce passage.",
  },
] as const;

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
  onDraftChange,
  onSuggestion,
  onSubmit,
}: EditorialConversationComposerProps) {
  const locked = controlsLocked || active || sendPending;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked) return;
    onSubmit("message");
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.altKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      if (!locked) onSubmit("message");
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
        readOnly={locked}
        aria-describedby="editorial-message-composer-shortcuts"
        onChange={(event) => onDraftChange(event.currentTarget.value)}
        onKeyDown={handleKeyDown}
      />
      <p
        id="editorial-message-composer-shortcuts"
        className="editorial-conversation__shortcut-help"
      >
        Entrée envoie le message, Maj+Entrée ajoute une ligne et Ctrl/Cmd+Entrée
        envoie le message.
      </p>
      <div
        className="editorial-conversation__suggestions"
        aria-label="Suggestions de message"
        role="group"
      >
        {suggestions.map((suggestion) => (
          <button
            key={suggestion.label}
            type="button"
            disabled={locked}
            onClick={() => onSuggestion(suggestion.instruction)}
          >
            {suggestion.label}
          </button>
        ))}
      </div>
      <p className="editorial-conversation__suggestion-help">
        Une suggestion préremplit une consigne modifiable ; elle n’envoie rien.
      </p>
      <div className="editorial-conversation__actions">
        <button
          className="editorial-conversation__send"
          type="submit"
          disabled={!draft.trim() || locked || hasQueryError}
        >
          {sendPending && sendKind === "message"
            ? "Envoi en cours…"
            : "Envoyer un message"}
        </button>
        {canPropose ? (
          <button
            className="editorial-conversation__propose"
            type="button"
            disabled={!draft.trim() || locked || hasQueryError}
            onClick={() => onSubmit("propose")}
          >
            {sendPending && sendKind === "propose"
              ? "Génération en cours…"
              : proposalLabel}
          </button>
        ) : null}
      </div>
      <p
        className="editorial-conversation__announcement"
        role="status"
        aria-live="polite"
        aria-atomic="true"
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
