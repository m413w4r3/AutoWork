import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../api/editions";
import { getEditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { Link } from "../../routing";
import { EditorialWorkspaceLayout } from "./EditorialWorkspaceLayout";
import { EditorialWorkspaceStatus } from "./EditorialWorkspaceStatus";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";
import "./editorialWorkspace.css";

interface EditorialWorkspacePageProps {
  subjectId: string;
}

export function EditorialWorkspacePage({
  subjectId,
}: EditorialWorkspacePageProps) {
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [localDrafts, setLocalDrafts] = useState<Record<string, string>>({});
  const snapshotQuery = useQuery({
    queryKey: editorialWorkspaceQueryKeys.snapshot(subjectId),
    queryFn: () => getEditorialWorkspaceSnapshot(subjectId),
    retry: false,
    refetchInterval: ({ state }) =>
      state.data?.impact_status === "pending" ||
      state.data?.impact_status === "running"
        ? 2_500
        : false,
  });

  if (snapshotQuery.isPending) {
    return (
      <section className="editorial-route-state" aria-busy="true">
        <p role="status">Chargement de l’atelier éditorial…</p>
      </section>
    );
  }

  if (snapshotQuery.isError) {
    const error = snapshotQuery.error;
    const accessUnavailable =
      error instanceof ApiError &&
      (error.status === 403 || error.status === 404);
    const staleSnapshot =
      error instanceof ApiError && error.code === "editorial_snapshot_stale";

    if (accessUnavailable) {
      return (
        <section
          className="editorial-route-state"
          aria-labelledby="editorial-route-error"
        >
          <h1 id="editorial-route-error">Atelier éditorial indisponible</h1>
          <p role="alert" aria-label="Atelier éditorial indisponible">
            Cet atelier n’est pas disponible.
          </p>
          <Link to="/editions">Retour aux éditions</Link>
        </section>
      );
    }

    return (
      <section
        className="editorial-route-state"
        aria-labelledby="editorial-route-error"
      >
        <h1 id="editorial-route-error">Lecture de l’atelier impossible</h1>
        <p role="alert">
          {staleSnapshot
            ? "Le document a changé. Rechargez l’atelier."
            : "L’atelier éditorial n’a pas pu être chargé."}
        </p>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => void snapshotQuery.refetch()}
        >
          Réessayer la lecture
        </button>
        <Link to="/editions">Retour aux éditions</Link>
      </section>
    );
  }

  const snapshot = snapshotQuery.data;
  const selectedLocalDraft = selectedBlockId
    ? (localDrafts[selectedBlockId] ?? "")
    : "";
  const updateLocalDraft = (blockId: string, text: string) => {
    setLocalDrafts((current) => {
      const next = { ...current };
      if (text.length === 0) delete next[blockId];
      else next[blockId] = text;
      return next;
    });
  };
  if (snapshot.blocks.length === 0 && snapshot.enrichments.length === 0) {
    return (
      <section
        className="editorial-route-state"
        aria-labelledby="editorial-empty-heading"
      >
        <Link to={`/subjects/${subjectId}`}>← Retour au sujet</Link>
        <h1 id="editorial-empty-heading">Atelier éditorial</h1>
        <EditorialWorkspaceStatus
          impactStatus={snapshot.impact_status}
          pdfStatus={snapshot.pdf_status}
        />
        <h2>Aucun contenu produit</h2>
        <p>Aucun contenu n’est disponible pour cette version.</p>
        {snapshot.publication_kind === null ? (
          <p role="note">
            Le type de publication est indéterminé. L’atelier reste en lecture
            seule.
          </p>
        ) : null}
      </section>
    );
  }

  return (
    <>
      <EditorialWorkspaceStatus
        impactStatus={snapshot.impact_status}
        pdfStatus={snapshot.pdf_status}
        showPdfStatus={snapshot.preview_status !== "STALE"}
        showImpactDetails={snapshot.preview_status !== "STALE"}
      />
      <EditorialWorkspaceLayout
        subjectId={subjectId}
        snapshot={snapshot}
        selectedBlockId={selectedBlockId}
        localDraftText={selectedLocalDraft}
        onSelectBlock={setSelectedBlockId}
        onLocalDraftChange={updateLocalDraft}
        onRefresh={() => void snapshotQuery.refetch()}
      />
    </>
  );
}
