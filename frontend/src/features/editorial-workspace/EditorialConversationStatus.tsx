import type { RefObject } from "react";

import type { EditorialThreadRead } from "../../api/editorialWorkspace";

interface ReconcileStatus {
  isPending: boolean;
  isSuccess: boolean;
  isError: boolean;
  message: string | null;
  errorMessage: string | null;
  belongsToCurrentTarget: boolean;
}

interface StartSessionStatus {
  isPending: boolean;
  isSuccess: boolean;
  isError: boolean;
  errorMessage: string | null;
  belongsToCurrentTarget: boolean;
}

interface EditorialConversationStatusProps {
  data: EditorialThreadRead | undefined;
  isPending: boolean;
  queryErrorMessage: string | null;
  reconcile: ReconcileStatus;
  startSession: StartSessionStatus;
  bridgeStatusRef: RefObject<HTMLParagraphElement | null>;
  onReconcile: () => void;
  onStartSession: () => void;
}

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
    "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre",
  reconciliation_required:
    "L’état de votre envoi est incertain. Vérifiez son état dans ce fil avant de continuer ; la demande ne sera pas renvoyée automatiquement.",
};

export function EditorialConversationStatus({
  data,
  isPending,
  queryErrorMessage,
  reconcile,
  startSession,
  bridgeStatusRef,
  onReconcile,
  onStartSession,
}: EditorialConversationStatusProps) {
  return (
    <>
      {isPending ? <p role="status">Chargement de la discussion…</p> : null}
      {queryErrorMessage ? <p role="alert">{queryErrorMessage}</p> : null}
      {data?.kind === "unopened" ? (
        <p>Aucune discussion n’a encore été ouverte pour ce bloc.</p>
      ) : null}
      {data?.kind === "stale" ? (
        <p role="status">
          {data.binding_state === "ambiguous"
            ? "L’identité de ce paragraphe historique est ambiguë."
            : "Ce paragraphe a changé. Rechargez l’atelier."}
        </p>
      ) : null}
      {data && data.kind !== "unopened" ? (
        <>
          {data.origin ? (
            <p className="editorial-conversation__origin">
              Version de référence du fil : v{data.origin.artifact_version}
            </p>
          ) : null}
          <p
            ref={bridgeStatusRef}
            role="status"
            aria-live="polite"
            aria-atomic="true"
            tabIndex={-1}
          >
            {bridgeLabels[data.bridge_status]}
          </p>
          {data.bridge_status === "reconciliation_required" ? (
            <button
              className="editorial-conversation__reconcile"
              type="button"
              disabled={reconcile.isPending}
              onClick={onReconcile}
            >
              {reconcile.isPending
                ? "Vérification en cours…"
                : "Vérifier l’état de l’envoi"}
            </button>
          ) : null}
          {reconcile.isSuccess && reconcile.belongsToCurrentTarget ? (
            <p
              className="editorial-conversation__announcement"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {reconcile.message}
            </p>
          ) : null}
          {reconcile.isError && reconcile.belongsToCurrentTarget ? (
            <p className="editorial-conversation__error" role="alert">
              {reconcile.errorMessage}
            </p>
          ) : null}
          {data.bridge_status === "session_lost" ? (
            <button
              className="editorial-conversation__new-session"
              type="button"
              disabled={startSession.isPending}
              onClick={onStartSession}
            >
              {startSession.isPending
                ? "Démarrage en cours…"
                : "Démarrer une nouvelle discussion"}
            </button>
          ) : null}
          {data.bridge_status === "session_lost" &&
          startSession.isError &&
          startSession.belongsToCurrentTarget ? (
            <p className="editorial-conversation__error" role="alert">
              {startSession.errorMessage}
            </p>
          ) : null}
          {startSession.isSuccess && startSession.belongsToCurrentTarget ? (
            <p role="status" aria-live="polite" aria-atomic="true">
              Nouvelle discussion démarrée.
            </p>
          ) : null}
          {data.kind === "open" && data.previous_sessions.length > 0 ? (
            <ul aria-label="Sessions précédentes">
              {data.previous_sessions.map((sessionNumber) => (
                <li key={sessionNumber}>
                  Session précédente · {sessionNumber}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </>
  );
}
