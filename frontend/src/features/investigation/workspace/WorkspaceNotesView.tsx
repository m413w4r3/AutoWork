import { useState, type FormEvent } from "react";

import {
  isWorkshopApiError,
  useEnsureWorkspace,
  useExportWorkspace,
  useSyncWorkspace,
  useWorkspaceAgentStatus,
  useWorkspaceNoteView,
  useWorkspaceStatus,
  useWriteWorkspaceNote,
  type WorkspaceCountsV1,
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
  WORKSPACE_STATE_LABELS,
  formatTimestamp,
  isHumanNotePath,
  labelOf,
  workshopErrorText,
} from "./workspaceLabels";
import "./workspace.css";

const DEFAULT_NOTE_PATH = "notes/humaines/";

function ErrorNotice({ label, error }: { label: string; error: unknown }) {
  return (
    <p className="error-message" role="alert">
      {workshopErrorText(error, label)}
    </p>
  );
}

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

function SyncReport({ report }: { report: WorkspaceSyncReportV1 }) {
  return (
    <div className="workspace-notes__note" role="status">
      <p>
        Synchronisation terminée — génération {report.generation}, curseur{" "}
        {report.cursor}. {report.files_observed} fichier(s) observé(s),{" "}
        {report.events_inserted} nouvel/nouveaux événement(s),{" "}
        {report.revisions_inserted} révision(s), {report.conflicts_observed}{" "}
        conflit(s) observé(s), {report.unresolved_conflicts} non résolu(s),{" "}
        {report.policy_violations} violation(s) de politique.
      </p>
      {report.resync ? (
        <p>Resynchronisation complète : le curseur a été réinitialisé.</p>
      ) : null}
      {report.truncated ? (
        <p>Événements tronqués — relancez une synchronisation.</p>
      ) : null}
      {report.new_human_notes.length > 0 ? (
        <p>Nouvelles notes humaines : {report.new_human_notes.join(", ")}.</p>
      ) : null}
      {report.changed_human_notes.length > 0 ? (
        <p>
          Notes humaines modifiées : {report.changed_human_notes.join(", ")}.
        </p>
      ) : null}
      {report.human_notes_truncated ? (
        <p>Liste des notes humaines tronquée.</p>
      ) : null}
    </div>
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

export function WorkspaceNotesView({
  investigationId,
}: {
  investigationId: string;
}) {
  const status = useWorkspaceStatus(investigationId);
  const agent = useWorkspaceAgentStatus();
  const sync = useSyncWorkspace(investigationId);
  const exportRun = useExportWorkspace(investigationId);
  const ensure = useEnsureWorkspace(investigationId);
  const write = useWriteWorkspaceNote(investigationId);
  const [selectedFile, setSelectedFile] = useState<WorkspaceFileViewV1 | null>(
    null,
  );
  const [noteDraftPath, setNoteDraftPath] = useState(DEFAULT_NOTE_PATH);
  const [noteText, setNoteText] = useState("");
  const [noteValidation, setNoteValidation] = useState<string | null>(null);
  const notePath =
    selectedFile !== null && isHumanNotePath(selectedFile.path)
      ? selectedFile.path
      : "";
  const note = useWorkspaceNoteView(investigationId, notePath, {
    enabled: notePath !== "",
  });

  if (status.isPending) {
    return <p role="status">Chargement du statut du workspace…</p>;
  }
  if (status.error !== null) {
    return (
      <ErrorNotice
        label="Le statut du workspace n’a pas pu être lu."
        error={status.error}
      />
    );
  }
  const current = status.data;
  if (current === undefined) return null;

  const agentStatus = agent.data?.status;
  const agentDown =
    agentStatus === "unavailable" || agentStatus === "unconfigured";
  const availabilityCode = current.availability_code;
  const localUnavailable =
    availabilityCode === "unavailable" ||
    availabilityCode === "unconfigured" ||
    agentDown;
  const isBound = current.workspace_state === "bound";
  const canWrite =
    isBound && current.workspace_available && agentDown === false;
  const canEnsure =
    isBound === false &&
    agentDown === false &&
    (availabilityCode === "not_bound" || availabilityCode === "available");
  const logicalPath =
    current.binding !== null
      ? `${current.binding.delivery_code}/${current.binding.article_slug}`
      : current.manifest !== null
        ? `${current.manifest.delivery_code}/${current.manifest.article_slug}`
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
      : "Le workspace n’est pas disponible.");

  const normalizedDraftPath = noteDraftPath.trim();
  const expectedSha =
    selectedFile !== null && selectedFile.path === normalizedDraftPath
      ? selectedFile.sha256
      : "new";

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
          selectedFile !== null && selectedFile.path === path
            ? selectedFile.sha256
            : "new",
        text: noteText,
      },
      {
        onSuccess: (result) => {
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
        },
      },
    );
  }

  return (
    <div className="workspace-notes" aria-label="Workspace et notes">
      <section
        className="workspace-notes__section"
        aria-label="Statut du workspace"
      >
        <h2>Workspace et notes</h2>
        <p className="workspace-notes__badges">
          <span className="badge">
            {WORKSPACE_STATE_LABELS[current.workspace_state]}
          </span>
          {agentLabel === "" ? null : (
            <span className="badge">{agentLabel}</span>
          )}
          <span className="badge">{AVAILABILITY_LABELS[availabilityCode]}</span>
          {current.generation === null ? null : (
            <span className="badge">Génération {current.generation}</span>
          )}
        </p>
        {logicalPath === null ? (
          <p>Aucun workspace lié à cette enquête.</p>
        ) : (
          <p>
            Chemin logique : <code>{logicalPath}</code>
            {current.cursor === null ? "" : ` · curseur ${current.cursor}`}
            {current.lastly_observed_at === null
              ? ""
              : ` · dernière observation le ${formatTimestamp(
                  current.lastly_observed_at,
                )}`}
            .
          </p>
        )}
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
        <p className="workspace-notes__evidence-warning">
          Fichier local ≠ preuve canonique. Les fichiers du workspace sont des
          archives de travail ; seules les observations et findings canoniques
          (PostgreSQL + MinIO, SHA-256) font preuve. Une note humaine reste une
          hypothèse non vérifiée jusqu’à conversion en observations vérifiables.
        </p>
        <Counts counts={current.counts} />
        <div className="workspace-notes__actions">
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
            <button
              className="button"
              type="button"
              disabled={sync.isPending}
              onClick={() => sync.mutate()}
            >
              {sync.isPending
                ? "Synchronisation en cours…"
                : "Synchroniser maintenant"}
            </button>
          ) : null}
          {canWrite ? (
            <button
              className="button button--secondary"
              type="button"
              disabled={exportRun.isPending}
              onClick={() => exportRun.mutate()}
            >
              {exportRun.isPending
                ? "Export en cours…"
                : "Exporter plan, VT et résultats"}
            </button>
          ) : null}
        </div>
        {ensure.error !== null ? (
          <ErrorNotice
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
            Synchronisation en cours — la lecture des notes reste disponible.
          </p>
        ) : null}
        {sync.error !== null ? (
          <ErrorNotice
            label="La synchronisation a échoué."
            error={sync.error}
          />
        ) : null}
        {sync.data === undefined ? null : <SyncReport report={sync.data} />}
        {exportRun.error !== null ? (
          <ErrorNotice
            label="L’export du workspace a échoué."
            error={exportRun.error}
          />
        ) : null}
        {exportRun.data === undefined ? null : (
          <ExportReport report={exportRun.data} />
        )}
      </section>

      <WorkspaceFilesPanel
        investigationId={investigationId}
        canWrite={canWrite}
        onSelectFile={setSelectedFile}
        onLoadIntoEditor={(path, text) => {
          setNoteDraftPath(path);
          setNoteText(text);
          setNoteValidation(null);
        }}
      />

      <section
        className="workspace-notes__section"
        aria-label="Écriture de note humaine"
      >
        <h2>Écrire une note humaine</h2>
        <p className="workspace-notes__meta">
          Markdown accepté sans métadonnée. La note reste une hypothèse humaine
          non vérifiée et un fichier local, jamais une preuve canonique.
        </p>
        {canWrite ? (
          <form className="workspace-notes__editor" onSubmit={submitNote}>
            <label htmlFor="workspace-note-path">Chemin de la note</label>
            <input
              id="workspace-note-path"
              value={noteDraftPath}
              onChange={(event) => setNoteDraftPath(event.target.value)}
            />
            <p className="workspace-notes__meta">
              Chemin attendu : <code>notes/humaines/&lt;nom&gt;.md</code> ·
              SHA-256 attendu : <code>{expectedSha}</code>. Pour modifier une
              note existante, sélectionnez-la d’abord dans l’arborescence.
            </p>
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
                {write.isPending ? "Enregistrement…" : "Enregistrer la note"}
              </button>
            </div>
          </form>
        ) : (
          <p className="workspace-notes__blocked" role="status">
            Écriture bloquée :{" "}
            {localUnavailable
              ? "workspace local indisponible"
              : "workspace non disponible"}
            . Aucune action n’est mise en file d’attente.
          </p>
        )}
        {write.isPending ? (
          <p role="status">Enregistrement de la note en cours…</p>
        ) : null}
        {write.error !== null ? (
          <p className="error-message" role="alert">
            {noteWriteError(write.error)}
          </p>
        ) : null}
        {write.data === undefined ? null : (
          <p role="status">
            Note enregistrée — révision{" "}
            <code>{write.data.revision.revision_id}</code>, SHA-256{" "}
            <code>{write.data.revision.sha256}</code>. Elle reste une note de
            travail non probante.
          </p>
        )}
      </section>

      {notePath === "" ? null : (
        <>
          {note.isPending ? (
            <p role="status">Chargement de la note {notePath}…</p>
          ) : null}
          {note.error !== null ? (
            <ErrorNotice
              label="La vue de la note n’a pas pu être lue."
              error={note.error}
            />
          ) : null}
          {note.data === undefined ? null : (
            <NoteDetailPanel
              investigationId={investigationId}
              notePath={notePath}
              note={note.data}
            />
          )}
        </>
      )}

      <ConflictsPanel investigationId={investigationId} />
    </div>
  );
}
