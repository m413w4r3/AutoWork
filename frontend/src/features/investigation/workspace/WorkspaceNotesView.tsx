import { useState, type FormEvent } from "react";

import {
  isWorkshopApiError,
  useEnsureWorkspace,
  useExportWorkspace,
  useSyncWorkspace,
  useWorkspaceAgentStatus,
  useWorkspaceFileContent,
  useWorkspaceNoteView,
  useWorkspaceStatus,
  useWriteWorkspaceNote,
  type WorkspaceCountsV1,
  type WorkspaceContentViewV1,
  type WorkspaceExportReportV1,
  type WorkspaceFileViewV1,
  type WorkspaceSyncReportV1,
} from "../api";
import { ConflictsPanel } from "./ConflictsPanel";
import { NoteDetailPanel } from "./NoteDetailPanel";
import { WorkspaceFilesPanel } from "./WorkspaceFilesPanel";
import {
  AGENT_STATUS_LABELS,
  AVAILABILITY_LABELS,
  EXPORT_ACTION_LABELS,
  EXPORT_SOURCE_LABELS,
  IMPORT_STATUS_LABELS,
  ORIGIN_LABELS,
  POLICY_LABELS,
  WORKSPACE_STATE_LABELS,
  formatBytes,
  formatShortDate,
  formatTimestamp,
  humanNoteLabelFromPath,
  isHumanNotePath,
  labelOf,
  workshopErrorText,
} from "./workspaceLabels";
import { WorkshopErrorNotice } from "../shell/WorkshopErrorNotice";
import "./workspace.css";

const DEFAULT_NOTE_PATH = "notes/humaines/";

function Counts({ counts }: { counts: WorkspaceCountsV1 }) {
  const entries: Array<[string, number]> = [
    ["Fichiers", counts.files],
    ["Notes humaines", counts.human_notes],
    ["Conflits non résolus", counts.unresolved_conflicts],
    ["Violations de politique", counts.policy_violations],
    ["Plans", counts.plans],
    ["Tâches", counts.tasks],
    ["Findings", counts.findings],
  ];
  return (
    <dl className="workspace-notes__counts">
      {entries.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** French agreement: 0 and 1 are singular, 2+ plural. */
function countOf(count: number, singular: string, plural: string): string {
  return `${count} ${count > 1 ? plural : singular}`;
}

function SyncReport({ report }: { report: WorkspaceSyncReportV1 }) {
  const unresolved = report.unresolved_conflicts;
  return (
    <details
      className="workspace-notes__note workspace-notes__sync-summary"
      role="status"
    >
      <summary>
        <strong>Synchronisation terminée</strong>
        <span>
          {countOf(
            report.files_observed,
            "fichier observé",
            "fichiers observés",
          )}
          {" · "}
          {countOf(
            report.events_inserted,
            "événement ajouté",
            "événements ajoutés",
          )}
          {" · "}
          {countOf(report.revisions_inserted, "révision", "révisions")}
          {" · "}
          {countOf(unresolved, "conflit à traiter", "conflits à traiter")}
          {" · "}
          {countOf(
            report.policy_violations,
            "violation de politique",
            "violations de politique",
          )}
        </span>
      </summary>
      <div className="workspace-notes__sync-details">
        <p>
          Génération {report.generation} · curseur {report.cursor} ·{" "}
          {countOf(
            report.conflicts_observed,
            "conflit observé",
            "conflits observés",
          )}
          .
        </p>
        {report.resync ? (
          <p>Resynchronisation complète : le curseur a été réinitialisé.</p>
        ) : null}
        {report.truncated ? (
          <p>Événements tronqués — relancez une synchronisation.</p>
        ) : null}
        {report.new_human_notes.length > 0 ? (
          <p>
            Nouvelles notes humaines :{" "}
            {report.new_human_notes.map((path, index) => (
              <span key={path} title={path}>
                {index > 0 ? ", " : ""}
                {humanNoteLabelFromPath(path)}
              </span>
            ))}
            .
          </p>
        ) : null}
        {report.changed_human_notes.length > 0 ? (
          <p>
            Notes humaines modifiées :{" "}
            {report.changed_human_notes.map((path, index) => (
              <span key={path} title={path}>
                {index > 0 ? ", " : ""}
                {humanNoteLabelFromPath(path)}
              </span>
            ))}
            .
          </p>
        ) : null}
        {report.human_notes_truncated ? (
          <p>Liste des notes humaines tronquée.</p>
        ) : null}
      </div>
    </details>
  );
}

function ExportReport({ report }: { report: WorkspaceExportReportV1 }) {
  return (
    <div className="workspace-notes__note" role="status">
      <p>
        Export terminé — {report.exported} exporté(s), {report.already_exported}{" "}
        déjà exporté(s), {report.preserved} préservé(s), {report.conflicts}{" "}
        conflit(s). Instantané VT :{" "}
        {report.vt_snapshot === "exported" ? "exporté" : "aucun"}.
      </p>
      <ul className="workspace-notes__list">
        {report.entries.map((entry) => (
          <li key={entry.path}>
            {EXPORT_SOURCE_LABELS[entry.source]} · <code>{entry.path}</code> ·{" "}
            {labelOf(EXPORT_ACTION_LABELS, entry.action)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function noteWriteError(error: unknown): string {
  if (isWorkshopApiError(error)) {
    switch (error.code) {
      case "workspace_revision_conflict":
        return "Conflit de révision : la note a changé sur le disque. Les deux versions sont conservées ; consultez la section Conflits pour enregistrer une décision.";
      case "workspace_local_unavailable":
        return "Workspace local indisponible : l’écriture n’a pas été envoyée. Aucune action n’est mise en file d’attente hors ligne.";
      case "workspace_path_not_editable":
        return "Ce chemin n’est pas éditable par l’interface.";
      case "workspace_file_too_large":
        return "Le fichier dépasse la limite d’écriture de l’interface.";
      default:
        return `L’écriture a été refusée (${error.code}).`;
    }
  }
  return "L’écriture a échoué (erreur réseau).";
}

function fileName(path: string): string {
  return path.split("/").at(-1) ?? path;
}

function fileTitle(
  file: WorkspaceFileViewV1,
  content?: WorkspaceContentViewV1,
): string {
  if (isHumanNotePath(file.path)) {
    if (content?.text !== null && content?.text !== undefined) {
      const firstLine = content.text
        .split(/\r?\n/)
        .map((line) => line.trim())
        .find((line) => line !== "" && !/^---+$/.test(line));
      if (firstLine !== undefined) {
        const readableLine = firstLine
          .replace(/^#{1,6}\s+/, "")
          .replace(/^[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ0-9_-]{2,}:\s*/, "")
          .replace(/\*\*(.+?)\*\*/g, "$1")
          .replace(/([*_`])/g, "")
          .trim();
        if (readableLine !== "") {
          const titledLine =
            readableLine.charAt(0).toLocaleUpperCase("fr-FR") +
            readableLine.slice(1);
          return `Note humaine — ${titledLine}`;
        }
      }
    }
    return `Note humaine · ${formatShortDate(file.observed_at)} · ${file.task_id === null ? "Sans tâche associée" : "Tâche associée"}`;
  }
  return fileName(file.path);
}

function FilePathDetails({ path }: { path: string }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle",
  );

  async function copyPath() {
    try {
      await navigator.clipboard.writeText(path);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  return (
    <details className="workspace-notes__path">
      <summary>Chemin exact</summary>
      <div className="workspace-notes__path-tools">
        <code>{path}</code>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => void copyPath()}
        >
          Copier le chemin exact
        </button>
        {copyState === "copied" ? (
          <span role="status">Chemin copié.</span>
        ) : null}
        {copyState === "failed" ? (
          <span role="status">Copie indisponible dans ce navigateur.</span>
        ) : null}
      </div>
    </details>
  );
}

function FileProvenance({ file }: { file: WorkspaceFileViewV1 }) {
  return (
    <details className="workspace-notes__provenance">
      <summary>Versions &amp; provenance</summary>
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
          <dd>{file.task_id === null ? "Aucune" : "Tâche associée"}</dd>
        </div>
      </dl>
      <h3>Révisions</h3>
      {file.history.length === 0 ? (
        <p>Aucune révision indexée.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <caption className="workspace-notes__caption">
              Historique des versions
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
                  <td>{version.task_id === null ? "—" : "Tâche associée"}</td>
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
    </details>
  );
}

export function WorkspaceNotesView({
  investigationId,
}: {
  investigationId: string;
}) {
  const status = useWorkspaceStatus(investigationId);
  const current = status.data;
  const agent = useWorkspaceAgentStatus();
  const sync = useSyncWorkspace(investigationId);
  const exportRun = useExportWorkspace(investigationId);
  const ensure = useEnsureWorkspace(investigationId);
  const write = useWriteWorkspaceNote(investigationId);
  const [selectedFile, setSelectedFile] = useState<WorkspaceFileViewV1 | null>(
    null,
  );
  const [creatingNote, setCreatingNote] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [noteDraftPath, setNoteDraftPath] = useState(DEFAULT_NOTE_PATH);
  const [noteText, setNoteText] = useState("");
  const [noteValidation, setNoteValidation] = useState<string | null>(null);
  const [writeSaved, setWriteSaved] = useState(false);

  const fileContent = useWorkspaceFileContent(
    investigationId,
    selectedFile?.path ?? "",
    selectedFile?.sha256,
    { enabled: selectedFile !== null && !creatingNote },
  );
  const notePath =
    selectedFile !== null && isHumanNotePath(selectedFile.path) && !creatingNote
      ? selectedFile.path
      : "";
  const note = useWorkspaceNoteView(investigationId, notePath, {
    enabled: notePath !== "",
  });

  if (status.isPending)
    return <p role="status">Chargement du statut du workspace…</p>;
  if (status.error !== null) {
    return (
      <WorkshopErrorNotice
        label="Le statut du workspace n’a pas pu être lu."
        error={status.error}
      />
    );
  }
  if (current === undefined) return null;

  const agentStatus = agent.data?.status;
  const agentDown =
    agentStatus === "unavailable" || agentStatus === "unconfigured";
  const agentAvailable = agentStatus === "available";
  const availabilityCode = current.availability_code;
  const localUnavailable =
    availabilityCode === "unavailable" ||
    availabilityCode === "unconfigured" ||
    agentDown;
  const isBound = current.workspace_state === "bound";
  const canWrite = isBound && current.workspace_available && agentAvailable;
  const canEnsure =
    isBound === false &&
    agentAvailable &&
    (availabilityCode === "not_bound" || availabilityCode === "available");
  const logicalPath =
    current.binding !== null
      ? current.binding.delivery_code + " / " + current.binding.article_slug
      : current.manifest !== null
        ? current.manifest.delivery_code + " / " + current.manifest.article_slug
        : null;
  const agentLabel = agent.isPending
    ? "Vérification de l’agent…"
    : agent.error !== null
      ? "État de l’agent inconnu"
      : agent.data === undefined
        ? ""
        : AGENT_STATUS_LABELS[agent.data.status];
  const blockedReason =
    current.availability_reason ??
    (agentDown
      ? "L’agent hôte ne répond pas."
      : agent.isPending
        ? "La disponibilité de l’agent n’est pas encore prouvée."
        : agent.error !== null
          ? "L’état de l’agent est inconnu (lecture en échec)."
          : "Le workspace n’est pas disponible.");

  function selectFile(file: WorkspaceFileViewV1 | null) {
    setSelectedFile(file);
    setCreatingNote(false);
    setIsEditing(false);
    setNoteValidation(null);
    setWriteSaved(false);
    write.reset();
  }

  function beginNewNote() {
    write.reset();
    setSelectedFile(null);
    setCreatingNote(true);
    setIsEditing(false);
    setNoteDraftPath(DEFAULT_NOTE_PATH);
    setNoteText("");
    setNoteValidation(null);
    setWriteSaved(false);
  }

  function beginEdit() {
    if (
      !canWrite ||
      selectedFile === null ||
      selectedFile.policy_class !== "human_editable" ||
      fileContent.data === undefined ||
      fileContent.data.text === null
    )
      return;
    write.reset();
    setCreatingNote(false);
    setIsEditing(true);
    setNoteDraftPath(selectedFile.path);
    setNoteText(fileContent.data.text);
    setNoteValidation(null);
    setWriteSaved(false);
  }

  function cancelEditor() {
    setIsEditing(false);
    setCreatingNote(false);
    setNoteValidation(null);
  }

  function submitNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const path = noteDraftPath.trim();
    if (!isHumanNotePath(path)) {
      setNoteValidation(
        "Le chemin doit être une note Markdown sous notes/humaines/ (extension .md, sans « .. »).",
      );
      return;
    }
    setNoteValidation(null);
    write.mutate(
      {
        path,
        expected_sha256:
          !creatingNote && selectedFile !== null && selectedFile.path === path
            ? selectedFile.sha256
            : "new",
        text: noteText,
      },
      {
        onSuccess: (result) => {
          setIsEditing(false);
          setCreatingNote(false);
          setWriteSaved(true);
          if (creatingNote) {
            setSelectedFile(null);
          } else {
            setSelectedFile((previous) =>
              previous !== null && previous.path === result.path
                ? {
                    ...previous,
                    revision_id: result.revision.revision_id,
                    sha256: result.revision.sha256,
                    size: result.revision.size,
                    observed_at: result.revision.observed_at,
                    origin: result.revision.origin,
                    task_id: result.revision.task_id,
                    history: [result.revision, ...previous.history],
                  }
                : previous,
            );
          }
        },
      },
    );
  }

  const selectedContent = fileContent.data;
  const isHumanSelection =
    selectedFile !== null && isHumanNotePath(selectedFile.path);
  const title =
    selectedFile === null ? "" : fileTitle(selectedFile, selectedContent);

  return (
    <div className="workspace-notes" aria-label="Workspace et notes">
      <header
        className="workspace-notes__header"
        aria-label="Statut du workspace"
      >
        <div>
          <h2>
            {logicalPath === null
              ? "Workspace non lié"
              : "Workspace : " + logicalPath}
          </h2>
          <p className="workspace-notes__status">
            <span>
              {current.lastly_observed_at === null
                ? "Pas encore synchronisé"
                : "Synchronisé · Dernière observation " +
                  formatTimestamp(current.lastly_observed_at)}
            </span>
          </p>
        </div>
        <div
          className="workspace-notes__actions"
          aria-label="Actions du workspace"
        >
          {canEnsure ? (
            <button
              className="button"
              type="button"
              disabled={ensure.isPending}
              onClick={() => ensure.mutate()}
            >
              {ensure.isPending
                ? "Création du workspace…"
                : "Créer le workspace"}
            </button>
          ) : null}
          {canWrite ? (
            <>
              <button
                className="button button--secondary"
                type="button"
                disabled={sync.isPending}
                onClick={() => sync.mutate()}
              >
                {sync.isPending ? "Actualisation…" : "Actualiser"}
              </button>
              <button className="button" type="button" onClick={beginNewNote}>
                Nouvelle note
              </button>
              <button
                className="button button--secondary"
                type="button"
                disabled={exportRun.isPending}
                onClick={() => exportRun.mutate()}
              >
                {exportRun.isPending ? "Export…" : "Exporter"}
              </button>
            </>
          ) : null}
        </div>
      </header>

      {localUnavailable ? (
        <div className="workspace-notes__blocked" role="alert">
          <strong>Workspace local indisponible</strong>
          <p>
            {blockedReason} Les écritures, la synchronisation et l’export sont
            bloqués ; aucune action n’est mise en file d’attente hors ligne.
          </p>
        </div>
      ) : isBound === false ? (
        <div className="workspace-notes__note">
          <p>
            Aucun workspace lié. {current.availability_reason ?? ""} Créez-le
            pour matérialiser l’arborescence de travail.
          </p>
        </div>
      ) : canWrite === false ? (
        <div className="workspace-notes__blocked" role="alert">
          <strong>Workspace indisponible</strong>
          <p>
            {blockedReason} Les écritures, la synchronisation et l’export sont
            bloqués.
          </p>
        </div>
      ) : null}

      <details className="workspace-notes__evidence-warning">
        <summary>
          <strong>Fichier local ≠ preuve canonique</strong>
          <span> · Une note humaine reste une hypothèse non vérifiée</span>
        </summary>
        <p>
          Les fichiers du workspace restent des archives de travail ; seules les
          observations et findings canoniques font preuve.
        </p>
      </details>

      <details className="workspace-notes__summary">
        <summary>
          {WORKSPACE_STATE_LABELS[current.workspace_state]} ·{" "}
          {agentLabel === ""
            ? AVAILABILITY_LABELS[availabilityCode]
            : agentLabel}{" "}
          · {countOf(current.counts.files, "fichier", "fichiers")} ·{" "}
          {countOf(
            current.counts.human_notes,
            "note humaine",
            "notes humaines",
          )}{" "}
          ·{" "}
          {countOf(
            current.counts.unresolved_conflicts,
            "conflit à traiter",
            "conflits à traiter",
          )}
        </summary>
        <p>
          Génération {current.generation ?? "—"}
          {current.cursor === null ? "" : " · curseur " + current.cursor}
        </p>
        <Counts counts={current.counts} />
      </details>

      {ensure.error !== null ? (
        <WorkshopErrorNotice
          label="La création du workspace a échoué."
          error={ensure.error}
        />
      ) : null}
      {ensure.data === undefined ? null : (
        <p role="status">
          Workspace lié —{" "}
          {ensure.data.workspace_created
            ? "arborescence créée"
            : "arborescence déjà présente"}
          {ensure.data.binding_created ? " · liaison enregistrée" : ""}.
        </p>
      )}
      {sync.isPending ? (
        <p role="status">
          Actualisation en cours — la lecture des notes reste disponible.
        </p>
      ) : null}
      {sync.error !== null ? (
        <WorkshopErrorNotice
          label="La synchronisation a échoué."
          error={sync.error}
        />
      ) : null}
      {sync.data === undefined ? null : <SyncReport report={sync.data} />}
      {exportRun.error !== null ? (
        <WorkshopErrorNotice
          label="L’export du workspace a échoué."
          error={exportRun.error}
        />
      ) : null}
      {exportRun.data === undefined ? null : (
        <ExportReport report={exportRun.data} />
      )}

      <div className="workspace-notes__explorer">
        <WorkspaceFilesPanel
          investigationId={investigationId}
          selectedPath={selectedFile?.path ?? ""}
          onSelectFile={selectFile}
        />
        <section
          className="workspace-notes__section workspace-notes__content-pane"
          aria-label="Contenu sélectionné"
        >
          {creatingNote ? (
            <>
              <h2>Nouvelle note humaine</h2>
              <p className="workspace-notes__meta">
                Markdown accepté sans métadonnée. La note est un fichier local
                et reste une hypothèse, jamais une preuve canonique.
              </p>
              <form className="workspace-notes__editor" onSubmit={submitNote}>
                <label htmlFor="workspace-note-path">Chemin de la note</label>
                <input
                  id="workspace-note-path"
                  value={noteDraftPath}
                  onChange={(event) => setNoteDraftPath(event.target.value)}
                />
                <label htmlFor="workspace-note-text">Contenu Markdown</label>
                <textarea
                  id="workspace-note-text"
                  value={noteText}
                  onChange={(event) => setNoteText(event.target.value)}
                />
                {noteValidation === null ? null : (
                  <p className="error-message" role="alert">
                    {noteValidation}
                  </p>
                )}
                <div className="workspace-notes__actions">
                  <button
                    className="button"
                    type="submit"
                    disabled={write.isPending}
                  >
                    {write.isPending
                      ? "Enregistrement…"
                      : "Enregistrer la note"}
                  </button>
                  <button
                    className="button button--secondary"
                    type="button"
                    onClick={cancelEditor}
                  >
                    Annuler
                  </button>
                </div>
              </form>
            </>
          ) : selectedFile === null ? (
            <div className="workspace-notes__empty">
              <h2>Contenu sélectionné</h2>
              {writeSaved ? (
                <p role="status">
                  Note enregistrée. Sélectionnez-la dans l’explorateur pour la
                  consulter.
                </p>
              ) : null}
              <p>
                Choisissez un fichier dans l’explorateur pour afficher son
                contenu et ses actions.
              </p>
            </div>
          ) : (
            <article className="workspace-notes__selected">
              <header className="workspace-notes__file-heading">
                <div>
                  <h2
                    title={selectedFile.path}
                    aria-label={
                      isHumanSelection ? fileName(selectedFile.path) : undefined
                    }
                    aria-description={isHumanSelection ? title : undefined}
                  >
                    {title}
                  </h2>
                  <FilePathDetails path={selectedFile.path} />
                </div>
                <div className="workspace-notes__badges">
                  <span className="badge">
                    {selectedFile.policy_class === null
                      ? "Politique inconnue"
                      : POLICY_LABELS[selectedFile.policy_class]}
                  </span>
                  <span className="badge">
                    {ORIGIN_LABELS[selectedFile.origin]}
                  </span>
                  <span className="badge">
                    {IMPORT_STATUS_LABELS[selectedFile.import_status]}
                  </span>
                  {selectedFile.conflict ? (
                    <span className="badge">Conflit</span>
                  ) : null}
                </div>
              </header>

              <dl className="workspace-notes__selected-meta">
                <div>
                  <dt>Modifié</dt>
                  <dd>
                    <time dateTime={selectedFile.observed_at}>
                      {formatTimestamp(selectedFile.observed_at)}
                    </time>
                  </dd>
                </div>
                <div>
                  <dt>Tâche</dt>
                  <dd>
                    {selectedFile.task_id === null
                      ? "Aucune tâche associée"
                      : "Tâche associée"}
                  </dd>
                </div>
              </dl>

              {fileContent.isPending ? (
                <p role="status">Chargement du contenu…</p>
              ) : null}
              {fileContent.error !== null ? (
                <p className="error-message" role="alert">
                  {workshopErrorText(
                    fileContent.error,
                    "Le contenu du fichier n’a pas pu être lu.",
                  )}
                </p>
              ) : null}
              {selectedContent?.metadata_only ? (
                <p>
                  Fichier volumineux : seules les métadonnées sont exposées, le
                  contenu n’est pas chargé par le navigateur.
                </p>
              ) : selectedContent?.text !== null &&
                selectedContent?.text !== undefined ? (
                isEditing ? (
                  <form
                    className="workspace-notes__editor"
                    onSubmit={submitNote}
                  >
                    <label htmlFor="workspace-note-text">
                      Contenu Markdown
                    </label>
                    <textarea
                      id="workspace-note-text"
                      value={noteText}
                      onChange={(event) => setNoteText(event.target.value)}
                    />
                    {noteValidation === null ? null : (
                      <p className="error-message" role="alert">
                        {noteValidation}
                      </p>
                    )}
                    <div className="workspace-notes__actions">
                      <button
                        className="button"
                        type="submit"
                        disabled={write.isPending}
                      >
                        {write.isPending ? "Enregistrement…" : "Enregistrer"}
                      </button>
                      <button
                        className="button button--secondary"
                        type="button"
                        onClick={cancelEditor}
                      >
                        Annuler
                      </button>
                    </div>
                  </form>
                ) : (
                  <div className="workspace-notes__markdown">
                    <h3>Contenu Markdown</h3>
                    <pre className="workspace-notes__content">
                      {selectedContent.text}
                    </pre>
                  </div>
                )
              ) : selectedContent?.content_base64 !== null &&
                selectedContent?.content_base64 !== undefined ? (
                <p>Contenu binaire — non affiché dans le navigateur.</p>
              ) : selectedContent === undefined ? null : (
                <p>Contenu vide.</p>
              )}

              {write.isPending ? (
                <p role="status">Enregistrement de la note en cours…</p>
              ) : null}
              {write.error !== null ? (
                <p className="error-message" role="alert">
                  {noteWriteError(write.error)}
                </p>
              ) : null}
              {writeSaved ? (
                <p role="status">
                  Note enregistrée. La nouvelle version est disponible dans les
                  détails.
                </p>
              ) : null}

              {isHumanSelection ? (
                <>
                  {note.isPending ? (
                    <p role="status">Chargement des actions de la note…</p>
                  ) : null}
                  {note.error !== null ? (
                    <WorkshopErrorNotice
                      label="La vue de la note n’a pas pu être lue."
                      error={note.error}
                    />
                  ) : null}
                  {note.data === undefined ? null : (
                    <NoteDetailPanel
                      key={selectedFile.path}
                      investigationId={investigationId}
                      notePath={notePath}
                      note={note.data}
                      file={selectedFile}
                      canWrite={
                        canWrite &&
                        selectedFile.policy_class === "human_editable" &&
                        selectedContent?.text !== null &&
                        selectedContent?.text !== undefined
                      }
                      onEdit={beginEdit}
                    />
                  )}
                </>
              ) : (
                <FileProvenance file={selectedFile} />
              )}
            </article>
          )}
        </section>
      </div>

      <ConflictsPanel investigationId={investigationId} />
    </div>
  );
}
