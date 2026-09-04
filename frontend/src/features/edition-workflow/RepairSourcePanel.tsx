import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  archiveManualSourceContent,
  getSubjectWorkbench,
} from "../../api/collection";
import {
  prepareEditionRepairSource,
  type EditionRepairDetail,
} from "../../api/publication";

export function RepairSourcePanel({
  editionId,
  subjectId,
  detail,
  readOnly,
  resolved,
  onArchived,
}: {
  editionId: string;
  subjectId: string;
  detail: EditionRepairDetail;
  readOnly: boolean;
  resolved: boolean;
  onArchived: () => void;
}) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [content, setContent] = useState("");
  const [mimeType, setMimeType] = useState("text/html");
  const [archived, setArchived] = useState(false);
  const sourceQuery = useQuery({
    queryKey: ["subject-workbench", subjectId],
    queryFn: () => getSubjectWorkbench(subjectId),
    enabled: Boolean(detail.collection_id),
  });
  const source = sourceQuery.data?.sources.find(
    (candidate) => candidate.id === detail.collection_id,
  );
  // The backend owns the state: `archived_pending_references` means the
  // content exists and only the deterministic REFERENCES rebuild is missing.
  const repairState = detail.repair_state ?? null;
  const pendingReferences = repairState === "archived_pending_references";
  const collectionMissing =
    repairState === "collection_missing" || !detail.collection_id;

  const prepare = useMutation({
    mutationFn: () => prepareEditionRepairSource(editionId, detail.repair_key),
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["edition-repair-detail", editionId, detail.repair_key],
      });
      void queryClient.invalidateQueries({
        queryKey: ["edition-repair", editionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["subject-workbench", subjectId],
      });
    },
  });

  const archive = useMutation({
    mutationFn: () => {
      if (!detail.collection_id) {
        return Promise.reject(
          new Error("La collection de cette source est inconnue."),
        );
      }
      const sourceContent = file ?? content;
      if (
        !sourceContent ||
        (typeof sourceContent === "string" && !sourceContent.trim())
      ) {
        return Promise.reject(
          new Error("Déposez un fichier ou collez un contenu."),
        );
      }
      return archiveManualSourceContent(subjectId, detail.collection_id, {
        content: sourceContent,
        declaredMimeType: file?.type || mimeType,
        finalUrl: detail.source_url,
      });
    },
    retry: false,
    onSuccess: () => {
      setArchived(true);
      onArchived();
    },
  });

  const publisher = detail.publisher ?? source?.publisher ?? null;
  const title = detail.source_title ?? source?.title ?? "Source proposée";
  const url = detail.source_url ?? source?.requested_url ?? null;
  const canUpload = !readOnly && !resolved && !collectionMissing;

  return (
    <section
      className="repair-source-panel"
      aria-labelledby="repair-source-heading"
    >
      <h4 id="repair-source-heading">
        Source proposée par {detail.source_id ?? "—"}
      </h4>
      <dl>
        <div>
          <dt>Titre</dt>
          <dd>{title}</dd>
        </div>
        <div>
          <dt>Publisher</dt>
          <dd>{publisher ?? "—"}</dd>
        </div>
        <div>
          <dt>URL</dt>
          <dd>
            {url ? (
              <a href={url} target="_blank" rel="noreferrer">
                {url}
              </a>
            ) : (
              "—"
            )}
          </dd>
        </div>
        <div>
          <dt>État</dt>
          <dd>
            {collectionMissing
              ? "source non attachée"
              : pendingReferences
                ? "archivée — références à reconstruire"
                : "non archivée"}
          </dd>
        </div>
      </dl>

      {collectionMissing ? (
        <>
          <p>
            Cette source proposée par Q1 n&apos;a jamais reçu de collecte. Elle
            doit être attachée avant de pouvoir recevoir un contenu.
          </p>
          {!readOnly ? (
            <button
              className="button"
              type="button"
              disabled={prepare.isPending}
              onClick={() => prepare.mutate()}
            >
              {prepare.isPending ? "Préparation…" : "Préparer cette source"}
            </button>
          ) : null}
          {prepare.error ? (
            <p className="error-message" role="alert">
              La source n&apos;a pas pu être préparée : {prepare.error.message}
            </p>
          ) : null}
        </>
      ) : pendingReferences ? (
        <p className="repair-source-panel__success" role="status">
          Contenu archivé. Reconstruisez cet article pour réintégrer la source
          dans les références.
        </p>
      ) : (
        <p>Le collecteur n&apos;a pas pu l&apos;archiver.</p>
      )}

      {canUpload ? (
        <form
          className="repair-source-panel__form"
          onSubmit={(event) => {
            event.preventDefault();
            archive.mutate();
          }}
        >
          <label htmlFor={`repair-source-file-${detail.repair_key}`}>
            Déposer un fichier
          </label>
          <input
            id={`repair-source-file-${detail.repair_key}`}
            type="file"
            accept=".html,.htm,.txt,.md,.pdf,.json"
            onChange={(event) =>
              setFile(event.currentTarget.files?.[0] ?? null)
            }
          />
          <span aria-hidden="true">ou</span>
          <label htmlFor={`repair-source-content-${detail.repair_key}`}>
            Coller le contenu
          </label>
          <textarea
            id={`repair-source-content-${detail.repair_key}`}
            rows={8}
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
          <label htmlFor={`repair-source-mime-${detail.repair_key}`}>
            MIME pour contenu collé
          </label>
          <select
            id={`repair-source-mime-${detail.repair_key}`}
            value={mimeType}
            onChange={(event) => setMimeType(event.target.value)}
          >
            <option value="text/html">text/html</option>
            <option value="text/plain">text/plain</option>
            <option value="text/markdown">text/markdown</option>
            <option value="application/json">application/json</option>
            <option value="application/pdf">application/pdf</option>
          </select>
          <button className="button" type="submit" disabled={archive.isPending}>
            {archive.isPending ? "Archivage…" : "Archiver cette source"}
          </button>
        </form>
      ) : null}
      {archived ? (
        <p className="repair-source-panel__success" role="status">
          Source archivée — reconstruction des références nécessaire.
        </p>
      ) : null}
      {archive.error ? (
        <p className="error-message" role="alert">
          La source n&apos;a pas pu être archivée : {archive.error.message}
        </p>
      ) : null}
      {sourceQuery.isError ? (
        <p className="error-message" role="alert">
          Les métadonnées de la source sont inaccessibles :{" "}
          {sourceQuery.error.message}
        </p>
      ) : null}
    </section>
  );
}
