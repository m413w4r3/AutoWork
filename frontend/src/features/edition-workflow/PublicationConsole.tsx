import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  acceptEditionPublication,
  editionRulesUrl,
  getEditionRelease,
  releasePdfUrl,
  retryEditionRender,
  type EditionReleaseResponse,
} from "../../api/publication";
import { ApiError } from "../../api/editions";
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

function DownloadActions({
  editionId,
  pdfAvailable,
}: {
  editionId: string;
  pdfAvailable: boolean;
}) {
  return (
    <div className="publication-console__downloads">
      {pdfAvailable ? (
        <a
          className="button publication-console__download"
          href={releasePdfUrl(editionId)}
          download
        >
          Télécharger le PDF
        </a>
      ) : null}
      <a
        className="button publication-console__download publication-console__download--rules"
        href={editionRulesUrl(editionId)}
        download
      >
        Télécharger les règles (ZIP)
      </a>
    </div>
  );
}

function RenderStatus({ release }: { release: EditionReleaseResponse }) {
  if (release.pdf_available) {
    return (
      <>
        <h3 role="status">Bulletin publié</h3>
        {release.published_at ? (
          <p>Publié le {readableDate(release.published_at)}</p>
        ) : null}
      </>
    );
  }
  if (release.render_status === "failed") {
    return (
      <>
        <p role="status">Le rendu PDF a échoué.</p>
        {release.render_error_code ? (
          <p className="publication-console__diagnostic-code">
            {release.render_error_code}
          </p>
        ) : null}
        {release.render_error_message ? (
          <p className="error-message">{release.render_error_message}</p>
        ) : null}
      </>
    );
  }
  return (
    <p role="status">
      {release.render_status === "not_started"
        ? "Le rendu PDF n’a pas encore démarré."
        : "Génération du PDF en cours"}
    </p>
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
      {release?.json_available ? (
        <>
          <h3>Assemblage canonique</h3>
          <p>Release assemblé</p>
          <h3>Rendu PDF</h3>
          <RenderStatus release={release} />
          <DownloadActions
            editionId={editionId}
            pdfAvailable={release.pdf_available}
          />
        </>
      ) : null}
    </section>
  );
}

export function PublicationConsole({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const release = useQuery({
    queryKey: ["edition-release", editionId],
    queryFn: () => getEditionRelease(editionId),
    refetchInterval: (query) => publicationPollingInterval(query.state.data),
  });
  const accept = useMutation({
    mutationFn: () => acceptEditionPublication(editionId),
    retry: false,
    onSuccess: () => invalidatePublication(queryClient, editionId),
  });
  const retryRender = useMutation({
    mutationFn: () => retryEditionRender(editionId),
    retry: false,
    onSuccess: () => invalidatePublication(queryClient, editionId),
  });

  if (release.isPending) {
    return <p role="status">Chargement de la publication…</p>;
  }
  if (release.isError && (!readOnly || !isMissingRelease(release.error))) {
    return (
      <p className="error-message" role="alert">
        La publication est inaccessible : {String(release.error)}
      </p>
    );
  }
  if (readOnly) {
    return (
      <ArchivedPublication
        editionId={editionId}
        release={release.data ?? null}
      />
    );
  }
  if (!release.data) return null;

  const current = release.data;
  if (current.release_id) {
    return (
      <section
        className="workflow-placeholder publication-console"
        aria-live="polite"
      >
        <p className="eyebrow">Publication</p>
        <h2>Assemblage canonique</h2>
        <p>Release assemblé</p>
        <h2>Rendu PDF</h2>
        <RenderStatus release={current} />
        {!readOnly && current.can_retry_render && !current.pdf_available ? (
          <button
            className="button"
            type="button"
            disabled={retryRender.isPending}
            onClick={() => retryRender.mutate()}
          >
            {retryRender.isPending ? "Relancement…" : "Relancer le rendu"}
          </button>
        ) : null}
        {!readOnly && retryRender.error ? (
          <p className="error-message" role="alert">
            {retryRender.error instanceof Error
              ? retryRender.error.message
              : "Le rendu PDF n’a pas pu être relancé."}
          </p>
        ) : null}
        <DownloadActions
          editionId={editionId}
          pdfAvailable={current.pdf_available}
        />
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
          ? "Assemblage en cours"
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
      <h2>Assemblage canonique</h2>
      <p>Manifest figé</p>
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
