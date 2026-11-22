import { useState, type FormEvent } from "react";

import {
  useWorkspaceFileContent,
  useWorkspaceFiles,
  type WorkspaceFileViewV1,
} from "../api";
import {
  IMPORT_STATUS_LABELS,
  ORIGIN_LABELS,
  POLICY_LABELS,
  formatBytes,
  formatTimestamp,
  isHumanNotePath,
  workshopErrorText,
} from "./workspaceLabels";

const PAGE_SIZE = 100;

const DIRECTORY_FILTERS = [
  { prefix: "", label: "Tout" },
  { prefix: "notes/humaines", label: "Notes humaines" },
  { prefix: "notes/bridge", label: "Notes Bridge" },
  { prefix: "notes/agents", label: "Notes d’agents" },
  { prefix: "plans", label: "Plans" },
  { prefix: "vt", label: "Rapports VT" },
  { prefix: "results", label: "Résultats" },
  { prefix: "base_samples", label: "Échantillons de base" },
];

function FileDetail({
  file,
  investigationId,
  canWrite,
  onLoadIntoEditor,
}: {
  file: WorkspaceFileViewV1;
  investigationId: string;
  canWrite: boolean;
  onLoadIntoEditor: (path: string, text: string) => void;
}) {
  const content = useWorkspaceFileContent(
    investigationId,
    file.path,
    file.sha256,
  );
  const editableText =
    content.data !== undefined && content.data.text !== null
      ? content.data.text
      : null;
  const canLoad =
    canWrite &&
    file.policy_class === "human_editable" &&
    isHumanNotePath(file.path) &&
    editableText !== null;

  return (
    <div className="workspace-notes__file-detail">
      <h3>{file.path}</h3>
      <p className="workspace-notes__badges">
        <span className="badge">
          {file.policy_class === null
            ? "Politique inconnue"
            : POLICY_LABELS[file.policy_class]}
        </span>
        <span className="badge">{ORIGIN_LABELS[file.origin]}</span>
        <span className="badge">
          Import : {IMPORT_STATUS_LABELS[file.import_status]}
        </span>
        {file.conflict ? <span className="badge">Conflit</span> : null}
      </p>
      <dl className="workspace-notes__counts">
        <div>
          <dt>Révision</dt>
          <dd>
            <code>{file.revision_id}</code>
          </dd>
        </div>
        <div>
          <dt>SHA-256</dt>
          <dd>
            <code>{file.sha256}</code>
          </dd>
        </div>
        <div>
          <dt>Taille</dt>
          <dd>{formatBytes(file.size)}</dd>
        </div>
        <div>
          <dt>Mode</dt>
          <dd>
            <code>{file.mode}</code>
          </dd>
        </div>
        <div>
          <dt>Observé le</dt>
          <dd>
            <time dateTime={file.observed_at}>
              {formatTimestamp(file.observed_at)}
            </time>
          </dd>
        </div>
        <div>
          <dt>Tâche liée</dt>
          <dd>{file.task_id === null ? "—" : file.task_id}</dd>
        </div>
      </dl>

      <h4>Révisions</h4>
      {file.history.length === 0 ? (
        <p>Aucune révision indexée.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <caption className="workspace-notes__caption">
              Historique de {file.path}
            </caption>
            <thead>
              <tr>
                <th scope="col">Révision</th>
                <th scope="col">SHA-256</th>
                <th scope="col">Observé le</th>
                <th scope="col">Origine</th>
                <th scope="col">Tâche</th>
                <th scope="col">Remplace</th>
              </tr>
            </thead>
            <tbody>
              {file.history.map((version) => (
                <tr key={version.revision_id}>
                  <td>
                    <code>{version.revision_id}</code>
                  </td>
                  <td>
                    <code>{version.sha256}</code>
                  </td>
                  <td>
                    <time dateTime={version.observed_at}>
                      {formatTimestamp(version.observed_at)}
                    </time>
                  </td>
                  <td>{ORIGIN_LABELS[version.origin]}</td>
                  <td>{version.task_id === null ? "—" : version.task_id}</td>
                  <td>
                    {version.supersedes_revision_id === null ? (
                      "—"
                    ) : (
                      <code>{version.supersedes_revision_id}</code>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4>Contenu (lecture via l’API, jamais le système de fichiers)</h4>
      {content.isPending ? <p role="status">Chargement du contenu…</p> : null}
      {content.error !== null ? (
        <p className="error-message" role="alert">
          {workshopErrorText(
            content.error,
            "Le contenu du fichier n’a pas pu être lu.",
          )}
        </p>
      ) : null}
      {content.data === undefined ? null : content.data.metadata_only ? (
        <p>
          Fichier volumineux : seules les métadonnées sont exposées, le contenu
          n’est pas chargé par le navigateur.
        </p>
      ) : content.data.text !== null ? (
        <pre className="workspace-notes__content">{content.data.text}</pre>
      ) : content.data.content_base64 !== null ? (
        <p>Contenu binaire — non affiché dans le navigateur.</p>
      ) : (
        <p>Contenu vide.</p>
      )}
      {canLoad ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => {
            if (editableText !== null)
              onLoadIntoEditor(file.path, editableText);
          }}
        >
          Charger cette note dans l’éditeur
        </button>
      ) : null}
    </div>
  );
}

export function WorkspaceFilesPanel({
  investigationId,
  canWrite,
  onSelectFile,
  onLoadIntoEditor,
}: {
  investigationId: string;
  canWrite: boolean;
  onSelectFile: (file: WorkspaceFileViewV1 | null) => void;
  onLoadIntoEditor: (path: string, text: string) => void;
}) {
  const [prefixDraft, setPrefixDraft] = useState("");
  const [prefix, setPrefix] = useState("");
  const [pageStack, setPageStack] = useState<string[]>([]);
  const [selectedPath, setSelectedPath] = useState("");
  const pageToken = pageStack.at(-1) ?? "";
  const files = useWorkspaceFiles(investigationId, {
    prefix: prefix === "" ? undefined : prefix,
    page_token: pageToken === "" ? undefined : pageToken,
    limit: PAGE_SIZE,
  });
  const fileList = files.data?.files ?? [];
  const selectedFile = fileList.find((file) => file.path === selectedPath);
  const nextToken = files.data?.next_page_token ?? null;

  function applyPrefix(nextPrefix: string) {
    setPrefix(nextPrefix);
    setPrefixDraft(nextPrefix);
    setPageStack([]);
    setSelectedPath("");
    onSelectFile(null);
  }

  function applyFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    applyPrefix(prefixDraft.trim());
  }

  function selectFile(file: WorkspaceFileViewV1 | null) {
    setSelectedPath(file?.path ?? "");
    onSelectFile(file);
  }

  return (
    <section
      className="workspace-notes__section"
      aria-label="Fichiers du workspace"
    >
      <h2>Arborescence et fichiers</h2>
      <form className="workspace-notes__filter" onSubmit={applyFilter}>
        <label htmlFor="workspace-prefix">Préfixe de chemin</label>
        <input
          id="workspace-prefix"
          value={prefixDraft}
          onChange={(event) => setPrefixDraft(event.target.value)}
          placeholder="ex. notes/humaines"
        />
        <button className="button button--secondary" type="submit">
          Filtrer
        </button>
      </form>
      <div className="workspace-notes__actions" aria-label="Dossiers">
        {DIRECTORY_FILTERS.map((directory) => (
          <button
            key={directory.prefix}
            className="button button--secondary"
            type="button"
            aria-pressed={prefix === directory.prefix}
            onClick={() => applyPrefix(directory.prefix)}
          >
            {directory.label}
          </button>
        ))}
      </div>

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
        <p>Aucun fichier pour ce préfixe.</p>
      ) : null}
      {fileList.length > 0 ? (
        <ul className="workspace-notes__file-list">
          {fileList.map((file) => (
            <li key={file.path}>
              <button
                className="workspace-notes__file-button"
                type="button"
                aria-pressed={file.path === selectedPath}
                onClick={() => selectFile(file)}
              >
                <span>
                  <code>{file.path}</code>
                </span>
                <span className="workspace-notes__badges">
                  <span className="badge">
                    {file.policy_class === null
                      ? "Politique inconnue"
                      : POLICY_LABELS[file.policy_class]}
                  </span>
                  <span className="badge">
                    {IMPORT_STATUS_LABELS[file.import_status]}
                  </span>
                  {file.conflict ? (
                    <span className="badge">Conflit</span>
                  ) : null}
                  {file.task_id === null ? null : (
                    <span className="badge">Tâche {file.task_id}</span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="workspace-notes__actions">
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

      {selectedPath !== "" && selectedFile === undefined ? (
        <p>
          Le fichier sélectionné n’est plus dans la page courante ;
          sélectionnez-le à nouveau.
        </p>
      ) : null}
      {selectedFile === undefined ? (
        <p>Sélectionnez un fichier pour lire ses révisions et son contenu.</p>
      ) : (
        <FileDetail
          key={selectedFile.path}
          file={selectedFile}
          investigationId={investigationId}
          canWrite={canWrite}
          onLoadIntoEditor={onLoadIntoEditor}
        />
      )}
    </section>
  );
}
