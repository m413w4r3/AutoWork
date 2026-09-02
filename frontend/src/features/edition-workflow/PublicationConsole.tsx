import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import {
  acceptEditionPublication,
  editionDocxUrl,
  getEditionRelease,
  type EditionReleaseResponse,
} from "../../api/publication";
import { ApiError, type EditionStatus } from "../../api/editions";
import { publicationPollingInterval } from "./publicationPolling";

function readableDate(value: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function isMissingRelease(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

function invalidatePublication(
  queryClient: ReturnType<typeof useQueryClient>,
  editionId: string,
) {
  void queryClient.invalidateQueries({
    queryKey: ["edition-release", editionId],
  });
  void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
  void queryClient.invalidateQueries({ queryKey: ["editions"] });
}

function DownloadAction({ editionId }: { editionId: string }) {
  return (
    <a
      className="button publication-console__download"
      href={editionDocxUrl(editionId)}
      download
    >
      Télécharger le bulletin DOCX
    </a>
  );
}

function ArchivedPublication({
  editionId,
  release,
}: {
  editionId: string;
  release: EditionReleaseResponse | null;
}) {
  return (
    <section
      className="workflow-placeholder publication-console"
      aria-live="polite"
    >
      <p className="eyebrow">Publication</p>
      <h2>Édition archivée</h2>
      <p>Cette édition est disponible en lecture seule.</p>
      {release?.docx_available ? (
        <DownloadAction editionId={editionId} />
      ) : null}
    </section>
  );
}

export function PublicationConsole({
  editionId,
  editionStatus,
  readOnly = false,
}: {
  editionId: string;
  editionStatus: EditionStatus;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const release = useQuery({
    queryKey: ["edition-release", editionId],
    queryFn: () => getEditionRelease(editionId),
    refetchInterval: (query) =>
      publicationPollingInterval(editionStatus, query.state.data),
  });
  const releaseTransitionInvalidated = useRef(false);
  const accept = useMutation({
    mutationFn: () => acceptEditionPublication(editionId),
    retry: false,
    onSuccess: () => invalidatePublication(queryClient, editionId),
  });

  useEffect(() => {
    if (
      !releaseTransitionInvalidated.current &&
      release.data?.edition_status === "published"
    ) {
      releaseTransitionInvalidated.current = true;
      void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
      void queryClient.invalidateQueries({ queryKey: ["editions"] });
    }
  }, [editionId, queryClient, release.data?.edition_status]);

  if (release.isPending) {
    return <p role="status">Chargement de la publication…</p>;
  }
  if (
    release.isError &&
    (editionStatus !== "archived" || !isMissingRelease(release.error))
  ) {
    return (
      <p className="error-message" role="alert">
        La publication est inaccessible : {String(release.error)}
      </p>
    );
  }
  if (editionStatus === "archived") {
    return (
      <ArchivedPublication
        editionId={editionId}
        release={release.data ?? null}
      />
    );
  }
  if (!release.data) return null;

  const current = release.data;
  if (editionStatus === "published") {
    return (
      <section
        className="workflow-placeholder publication-console"
        aria-live="polite"
      >
        <p className="eyebrow">Publication</p>
        <h2>Bulletin publié</h2>
        {current.published_at ? (
          <p>Publié le {readableDate(current.published_at)}</p>
        ) : null}
        {current.docx_available ? (
          <DownloadAction editionId={editionId} />
        ) : null}
      </section>
    );
  }

  const failed = current.assembly_status === "failed";
  const statusLabel =
    current.assembly_status === null
      ? "L'assemblage n'a pas pu être démarré."
      : current.assembly_status === "running"
        ? "Assemblage en cours"
        : current.assembly_status === "queued"
          ? "En attente"
          : current.assembly_status === "waiting_human"
            ? "Intervention requise pour poursuivre l'assemblage."
            : current.assembly_status === "cancelled"
              ? "L'assemblage a été annulé."
              : current.assembly_status === "succeeded"
                ? "Assemblage terminé"
                : "L'assemblage a échoué.";

  return (
    <section
      className="workflow-placeholder publication-console"
      aria-live="polite"
    >
      <p className="eyebrow">Publication</p>
      <h2>Manifest figé</h2>
      <p>Assemblage du bulletin</p>
      <p role="status">{failed ? "L'assemblage a échoué." : statusLabel}</p>
      {failed && current.assembly_error_message ? (
        <p className="error-message">{current.assembly_error_message}</p>
      ) : null}
      {!readOnly && current.can_retry_assembly ? (
        <button
          className="button"
          type="button"
          disabled={accept.isPending}
          onClick={() => accept.mutate()}
        >
          {accept.isPending ? "Relancement…" : "Relancer l'assemblage"}
        </button>
      ) : null}
      {!readOnly && accept.error ? (
        <p className="error-message" role="alert">
          {accept.error instanceof Error
            ? accept.error.message
            : "L’assemblage n’a pas pu être relancé."}
        </p>
      ) : null}
      {failed && current.assembly_error_code ? (
        <details className="publication-console__diagnostics">
          <summary>Diagnostics</summary>
          <p>{current.assembly_error_code}</p>
        </details>
      ) : null}
    </section>
  );
}
