import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import {
  archiveManualSourceContent,
  getSubjectWorkbench,
} from "../../api/collection";
import type { EditionRepairDetail } from "../../api/publication";

export function RepairSourcePanel({
  subjectId,
  detail,
  readOnly,
  resolved,
  onArchived,
}: {
  subjectId: string;
  detail: EditionRepairDetail;
  readOnly: boolean;
  resolved: boolean;
  onArchived: () => void;
}) {
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
      </dl>
      <p>Le collecteur n&apos;a pas pu l&apos;archiver.</p>
      {!readOnly && !resolved ? (
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
