import { ApiError } from "../../api/editions";
import {
  useDeleteEditorialServerDraft,
  useEditorialServerDraft,
  useSaveEditorialServerDraft,
} from "./editorialServerDrafts";

interface EditorialServerDraftStatusProps {
  subjectId: string;
  occurrenceId: string | null;
  snapshotId: string;
  localDraftText: string;
  onRecover: (text: string) => void;
}

function formatExpiry(value: string | null): string | null {
  if (value === null) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function saveFailureText(error: Error | null): string | null {
  if (!error) return null;
  if (error instanceof ApiError && error.code === "draft_base_stale") {
    return "Le document a changé. Rechargez l’atelier avant d’enregistrer.";
  }
  if (error instanceof ApiError && error.code === "draft_text_too_large") {
    return "Le brouillon dépasse la taille autorisée.";
  }
  return "Le brouillon serveur n’a pas pu être enregistré.";
}

export function EditorialServerDraftStatus({
  subjectId,
  occurrenceId,
  snapshotId,
  localDraftText,
  onRecover,
}: EditorialServerDraftStatusProps) {
  const draftQuery = useEditorialServerDraft(subjectId, occurrenceId);
  const saveMutation = useSaveEditorialServerDraft(
    subjectId,
    occurrenceId ?? "unselected",
  );
  const deleteMutation = useDeleteEditorialServerDraft(
    subjectId,
    occurrenceId ?? "unselected",
  );

  if (occurrenceId === null) return null;
  if (draftQuery.isPending || !draftQuery.data) {
    if (draftQuery.isError) {
      return (
        <div className="editorial-server-draft-status" role="status">
          <p>Le brouillon serveur est temporairement indisponible.</p>
          <button
            className="button button--secondary"
            type="button"
            onClick={() => void draftQuery.refetch()}
          >
            Réessayer
          </button>
        </div>
      );
    }
    return null;
  }

  const state = draftQuery.data;
  const draft = state.kind === "available" ? state.draft : null;
  const expiry = formatExpiry(
    state.kind === "available"
      ? state.draft.expires_at
      : state.kind === "expired"
        ? state.expiresAt
        : null,
  );
  const recover = () => {
    if (!draft) return;
    if (
      localDraftText.length > 0 &&
      !window.confirm(
        "Remplacer le brouillon local non vide par le brouillon serveur ?",
      )
    ) {
      return;
    }
    onRecover(draft.text);
  };
  const save = () => {
    if (occurrenceId === null || localDraftText.length === 0) return;
    void saveMutation.mutate({
      snapshot_id: snapshotId,
      text: localDraftText,
      expected_version: draft?.version ?? 0,
    });
  };
  const remove = () => {
    if (
      !draft ||
      !window.confirm(
        "Supprimer définitivement le brouillon enregistré sur le serveur ?",
      )
    ) {
      return;
    }
    deleteMutation.mutate(draft.version);
  };

  return (
    <div
      className="editorial-server-draft-status"
      role="status"
      data-stale={draft?.is_stale ?? false}
      data-expired={state.kind === "expired"}
    >
      {state.kind === "available" ? (
        <p>
          Brouillon enregistré sur le serveur · expire le{" "}
          {expiry ?? "date inconnue"}
          {state.draft.is_stale ? " · base périmée, rechargez l’atelier" : ""}
        </p>
      ) : state.kind === "expired" ? (
        <p>
          {expiry
            ? `Le brouillon serveur a expiré le ${expiry}. Son texte n’est plus disponible.`
            : "Le brouillon serveur a expiré. Son texte n’est plus disponible."}
        </p>
      ) : localDraftText.length > 0 ? (
        <p>Aucun brouillon serveur n’est enregistré pour ce bloc.</p>
      ) : null}

      {draft ? (
        <>
          <button
            className="button button--secondary"
            type="button"
            onClick={recover}
          >
            Reprendre depuis le serveur
          </button>
          <button
            className="button button--secondary"
            type="button"
            disabled={deleteMutation.isPending}
            onClick={remove}
          >
            Supprimer le brouillon serveur
          </button>
        </>
      ) : null}
      {localDraftText.length > 0 ? (
        <button
          className="button button--secondary"
          type="button"
          disabled={saveMutation.isPending || Boolean(draft?.is_stale)}
          onClick={save}
        >
          Enregistrer le brouillon serveur
        </button>
      ) : null}
      {saveMutation.isPending ? (
        <p>Enregistrement du brouillon serveur…</p>
      ) : null}
      {saveFailureText(saveMutation.error) ? (
        <p role="alert">{saveFailureText(saveMutation.error)}</p>
      ) : null}
      {deleteMutation.isError ? (
        <p role="alert">Le brouillon serveur n’a pas pu être supprimé.</p>
      ) : null}
    </div>
  );
}
