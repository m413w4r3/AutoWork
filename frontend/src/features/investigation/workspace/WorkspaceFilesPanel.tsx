import { useState } from "react";

import { useWorkspaceFiles, type WorkspaceFileViewV1 } from "../api";
import {
  IMPORT_STATUS_LABELS,
  ORIGIN_LABELS,
  formatShortDate,
  formatTimestamp,
  isHumanNotePath,
  workshopErrorText,
} from "./workspaceLabels";

const PAGE_SIZE = 100;

const DIRECTORY_FILTERS = [
  { prefix: "notes/humaines", label: "Notes humaines" },
  { prefix: "notes/agents", label: "Notes d’agent" },
  { prefix: "notes/bridge", label: "Notes de Bridge" },
  { prefix: "results", label: "Résultats" },
  { prefix: "vt", label: "VirusTotal" },
  { prefix: "plans", label: "Plans" },
  { prefix: "base_samples", label: "Échantillons de référence" },
  { prefix: "", label: "Tous les fichiers" },
];

function fileName(path: string): string {
  return path.split("/").at(-1) ?? path;
}

function fileLabel(file: WorkspaceFileViewV1): string {
  return isHumanNotePath(file.path)
    ? `Note humaine · ${formatShortDate(file.observed_at)}`
    : fileName(file.path);
}

export function WorkspaceFilesPanel({
  investigationId,
  selectedPath,
  onSelectFile,
}: {
  investigationId: string;
  selectedPath: string;
  onSelectFile: (file: WorkspaceFileViewV1 | null) => void;
}) {
  const [prefix, setPrefix] = useState("notes/humaines");
  const [pageStack, setPageStack] = useState<string[]>([]);
  const pageToken = pageStack.at(-1) ?? "";
  const files = useWorkspaceFiles(investigationId, {
    prefix: prefix === "" ? undefined : prefix,
    page_token: pageToken === "" ? undefined : pageToken,
    limit: PAGE_SIZE,
  });
  const fileList = files.data?.files ?? [];
  const nextToken = files.data?.next_page_token ?? null;

  function selectFile(file: WorkspaceFileViewV1 | null) {
    onSelectFile(file);
  }

  function selectDirectory(nextPrefix: string) {
    setPrefix(nextPrefix);
    setPageStack([]);
    selectFile(null);
  }

  return (
    <section
      className="workspace-notes__section workspace-notes__browser"
      aria-label="Dossiers et fichiers"
    >
      <h2>Dossiers / fichiers</h2>
      <nav aria-label="Dossiers" className="workspace-notes__directories">
        {DIRECTORY_FILTERS.map((directory) => (
          <button
            key={directory.prefix}
            className="workspace-notes__directory"
            type="button"
            aria-label={directory.prefix || directory.label}
            aria-description={directory.prefix ? directory.label : undefined}
            aria-pressed={prefix === directory.prefix}
            onClick={() => selectDirectory(directory.prefix)}
          >
            {directory.label}
          </button>
        ))}
      </nav>

      {files.isPending ? <p role="status">Chargement des fichiers…</p> : null}
      {files.error !== null ? (
        <p className="error-message" role="alert">
          {workshopErrorText(
            files.error,
            "Les fichiers du workspace n’ont pas pu être lus.",
          )}
        </p>
      ) : null}
      {files.data?.workspace_available === false ? (
        <p className="workspace-notes__blocked" role="status">
          Workspace local indisponible — la lecture des fichiers est bloquée.
        </p>
      ) : null}
      {files.data !== undefined &&
      files.data.workspace_available &&
      fileList.length === 0 ? (
        <p>Aucun fichier dans ce dossier.</p>
      ) : null}
      {fileList.length > 0 ? (
        <ul className="workspace-notes__file-list">
          {fileList.map((file) => (
            <li key={file.path}>
              <button
                className="workspace-notes__file-button"
                type="button"
                title={file.path}
                aria-pressed={file.path === selectedPath}
                onClick={() => selectFile(file)}
              >
                <span className="workspace-notes__file-name">
                  {fileLabel(file)}
                </span>
                <span className="sr-only" aria-hidden="true">
                  {fileName(file.path)}
                </span>
                <span className="workspace-notes__badges">
                  <span className="badge">{ORIGIN_LABELS[file.origin]}</span>
                  <span className="badge">
                    {IMPORT_STATUS_LABELS[file.import_status]}
                  </span>
                  {file.conflict ? (
                    <span className="badge">Conflit</span>
                  ) : null}
                </span>
                <span className="workspace-notes__file-meta">
                  <time dateTime={file.observed_at}>
                    {formatTimestamp(file.observed_at)}
                  </time>
                  {file.task_id === null ? null : <span>Tâche associée</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="workspace-notes__pagination">
        {pageStack.length > 0 ? (
          <button
            className="button button--secondary"
            type="button"
            onClick={() => {
              setPageStack(pageStack.slice(0, -1));
              selectFile(null);
            }}
          >
            Page précédente
          </button>
        ) : null}
        {nextToken === null ? null : (
          <button
            className="button button--secondary"
            type="button"
            onClick={() => {
              setPageStack([...pageStack, nextToken]);
              selectFile(null);
            }}
          >
            Page suivante
          </button>
        )}
      </div>
    </section>
  );
}
