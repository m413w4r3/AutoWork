import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../api/editions";
import { getEditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { Link } from "../../routing";
import { EditorialWorkspaceLayout } from "./EditorialWorkspaceLayout";
import { useEditorialDrafts } from "./EditorialDrafts";
import { EditorialWorkspaceStatus } from "./EditorialWorkspaceStatus";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";
import { useManualEditorialEdit } from "./useManualEditorialEdit";
import "./editorialWorkspace.css";

interface EditorialWorkspacePageProps {
  subjectId: string;
}

export function EditorialWorkspacePage({
  subjectId,
}: EditorialWorkspacePageProps) {
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [savedManualEdit, setSavedManualEdit] = useState<{
    proposalId: string;
    artifactVersion: number;
  } | null>(null);
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
  const localDrafts = useEditorialDrafts(snapshotQuery.data);
  const manualEdit = useManualEditorialEdit(subjectId);

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
    ? (localDrafts.drafts[selectedBlockId] ?? "")
    : "";
  const recoverServerDraftText = (blockId: string, text: string) => {
    const block = snapshot.blocks.find((item) => item.block_id === blockId);
    if (block) localDrafts.updateDraft(blockId, text, block.text ?? "");
  };
  const hasNoProducedContent =
    snapshot.blocks.length === 0 && snapshot.enrichments.length === 0;
  if (hasNoProducedContent && !localDrafts.hasDrafts) {
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
      {savedManualEdit ? (
        <p
          key={savedManualEdit.proposalId}
          className="editorial-manual-edit-confirmation"
          role="status"
          data-proposal-id={savedManualEdit.proposalId}
        >
          Modification manuelle enregistrée (version{" "}
          {savedManualEdit.artifactVersion}).
        </p>
      ) : null}
      {hasNoProducedContent ? (
        <p className="editorial-route-state" role="note">
          Aucun contenu n’est disponible pour cette version. Les brouillons
          conservés restent accessibles dans le plan.
        </p>
      ) : null}
      <EditorialWorkspaceLayout
        subjectId={subjectId}
        snapshot={snapshot}
        localDrafts={localDrafts}
        selectedBlockId={selectedBlockId}
        localDraftText={selectedLocalDraft}
        onSelectBlock={setSelectedBlockId}
        onLocalDraftChange={recoverServerDraftText}
        onRefresh={() => void snapshotQuery.refetch()}
        onSave={async (blockId, text) => {
          const block = snapshot.blocks.find(
            (item) => item.block_id === blockId,
          );
          if (!block)
            throw new Error("Ce bloc n’existe plus dans cette version.");
          const operation =
            block.kind === "title"
              ? "rewrite_title"
              : block.kind === "lead_paragraph" || block.kind === "paragraph"
                ? "rewrite_paragraph"
                : null;
          if (!operation || !block.allowed_operations.includes(operation)) {
            throw new Error("Ce bloc ne peut pas être modifié directement.");
          }
          setSavedManualEdit(null);
          const accepted = await manualEdit.mutateAsync({
            occurrenceId: blockId,
            snapshotId: snapshot.snapshot_id,
            operation,
            text,
          });
          setSavedManualEdit({
            proposalId: accepted.id,
            artifactVersion: accepted.artifact_version,
          });
        }}
      />
    </>
  );
}
