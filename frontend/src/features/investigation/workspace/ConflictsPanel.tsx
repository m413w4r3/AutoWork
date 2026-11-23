import { useId, useState, type FormEvent } from "react";

import {
  useResolveWorkspaceConflict,
  useWorkspaceConflicts,
  type ConflictChoice,
  type ConflictResolveRequest,
  type WorkspaceConflictViewV1,
} from "../api";
import {
  CONFLICT_CHOICE_LABELS,
  formatTimestamp,
  labelOf,
  workshopErrorText,
} from "./workspaceLabels";

function isConflictChoice(value: string): value is ConflictChoice {
  return (
    value === "keep_current" ||
    value === "keep_proposed" ||
    value === "merged_text"
  );
}

function ConflictCard({
  conflict,
  pending,
  onResolve,
}: {
  conflict: WorkspaceConflictViewV1;
  pending: boolean;
  onResolve: (input: ConflictResolveRequest) => void;
}) {
  const baseId = useId();
  const [choice, setChoice] = useState<ConflictChoice>("keep_current");
  const [reason, setReason] = useState("");
  const [mergedText, setMergedText] = useState("");
  const currentSha = conflict.current_sha256;
  const canResolve = currentSha !== null;
  const mergedMissing = choice === "merged_text" && mergedText.trim() === "";

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (currentSha === null) return;
    const input: ConflictResolveRequest = {
      choice,
      expected_current_sha256: currentSha,
      expected_proposed_sha256: conflict.proposed_sha256,
    };
    const trimmedReason = reason.trim();
    if (trimmedReason !== "") input.reason = trimmedReason;
    if (choice === "merged_text") input.merged_text = mergedText;
    onResolve(input);
  }

  return (
    <article
      className="workspace-notes__conflict"
      aria-label={`Conflit sur ${conflict.path}`}
    >
      <h3>{conflict.path}</h3>
      <p className="workspace-notes__meta">
        Conflit <code>{conflict.conflict_id}</code>
        {conflict.conflict_path === null ? null : (
          <>
            {" "}
            · copie conservée : <code>{conflict.conflict_path}</code>
          </>
        )}{" "}
        · observé le{" "}
        <time dateTime={conflict.observed_at}>
          {formatTimestamp(conflict.observed_at)}
        </time>
      </p>
      <p>
        Les deux versions sont conservées sur le disque. La résolution est une
        décision humaine enregistrée ; l’agent hôte n’auto-résout jamais.
      </p>
      <div className="workspace-notes__versions">
        <h4>Version locale (conservée)</h4>
        {conflict.current === null ? (
          <p>Version locale indisponible.</p>
        ) : conflict.current.text === null ? (
          <p>Version locale non textuelle — contenu non affiché.</p>
        ) : (
          <pre className="workspace-notes__content">
            {conflict.current.text}
          </pre>
        )}
        <h4>Version proposée (conservée)</h4>
        {conflict.proposed === null ? (
          <p>Version proposée indisponible.</p>
        ) : conflict.proposed.text === null ? (
          <p>Version proposée non textuelle — contenu non affiché.</p>
        ) : (
          <pre className="workspace-notes__content">
            {conflict.proposed.text}
          </pre>
        )}
      </div>
      <h4>Diff</h4>
      {conflict.diff === null ? (
        <p>Aucun diff textuel disponible.</p>
      ) : (
        <pre
          className="workspace-notes__diff"
          aria-label="Diff des deux versions"
        >
          {conflict.diff}
        </pre>
      )}
      {canResolve && currentSha !== null ? (
        <form className="workspace-notes__form" onSubmit={submit}>
          <label htmlFor={`${baseId}-choice`}>Résolution</label>
          <select
            id={`${baseId}-choice`}
            value={choice}
            onChange={(event) => {
              const value = event.target.value;
              if (isConflictChoice(value)) setChoice(value);
            }}
          >
            <option value="keep_current">
              {CONFLICT_CHOICE_LABELS.keep_current}
            </option>
            <option value="keep_proposed">
              {CONFLICT_CHOICE_LABELS.keep_proposed}
            </option>
            <option value="merged_text">
              {CONFLICT_CHOICE_LABELS.merged_text}
            </option>
          </select>
          {choice === "merged_text" ? (
            <>
              <label htmlFor={`${baseId}-merged`}>Texte fusionné</label>
              <textarea
                id={`${baseId}-merged`}
                value={mergedText}
                onChange={(event) => setMergedText(event.target.value)}
              />
            </>
          ) : null}
          <label htmlFor={`${baseId}-reason`}>Raison (facultatif)</label>
          <input
            id={`${baseId}-reason`}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <div className="workspace-notes__actions">
            <button
              className="button"
              type="submit"
              disabled={pending || mergedMissing}
            >
              {pending ? "Résolution…" : "Résoudre le conflit"}
            </button>
            <span className="workspace-notes__meta">
              CAS : locale {currentSha.slice(0, 12)}… · proposée{" "}
              {conflict.proposed_sha256.slice(0, 12)}…
            </span>
          </div>
        </form>
      ) : (
        <p className="workspace-notes__unavailable" role="status">
          Résolution indisponible : la version locale n’a pas de SHA-256
          courant. Aucune écriture n’est proposée.
        </p>
      )}
    </article>
  );
}

export function ConflictsPanel({
  investigationId,
}: {
  investigationId: string;
}) {
  const conflicts = useWorkspaceConflicts(investigationId);
  const resolve = useResolveWorkspaceConflict(investigationId);
  const conflictCount = conflicts.data?.conflicts.length;
  const summary = conflicts.isPending
    ? "Conflits — chargement"
    : conflicts.error !== null
      ? "Conflits — lecture impossible"
      : conflicts.data?.workspace_available === false
        ? "Conflits — workspace indisponible"
        : conflictCount === 0
          ? "Conflits — aucun conflit non résolu"
          : `Conflits — ${conflictCount ?? 0} non résolu${conflictCount === 1 ? "" : "s"}`;

  return (
    <section
      className="workspace-notes__conflicts"
      aria-label="Conflits du workspace"
    >
      <details className="workspace-notes__conflicts-details">
        <summary>{summary}</summary>
        <div className="workspace-notes__conflicts-content">
          <p className="workspace-notes__meta">
            Chaque conflit garde les deux versions ; la résolution envoie les
            deux SHA-256 attendus et enregistre une décision humaine.
          </p>
          {conflicts.isPending ? (
            <p role="status">Chargement des conflits…</p>
          ) : null}
          {conflicts.error !== null ? (
            <p className="error-message" role="alert">
              {workshopErrorText(
                conflicts.error,
                "Les conflits n’ont pas pu être lus.",
              )}
            </p>
          ) : null}
          {conflicts.data?.workspace_available === false ? (
            <p className="workspace-notes__blocked" role="status">
              Workspace local indisponible — les conflits ne peuvent pas être
              relus ni résolus.
            </p>
          ) : null}
          {conflicts.data !== undefined &&
          conflicts.data.workspace_available &&
          conflicts.data.conflicts.length === 0 ? (
            <p>Aucun conflit non résolu.</p>
          ) : null}
          {conflicts.data?.conflicts.map((conflict) => (
            <ConflictCard
              key={conflict.conflict_id}
              conflict={conflict}
              pending={resolve.isPending}
              onResolve={(input) =>
                resolve.mutate({ conflictId: conflict.conflict_id, input })
              }
            />
          ))}
          {resolve.isPending ? <p role="status">Résolution en cours…</p> : null}
          {resolve.error !== null ? (
            <p className="error-message" role="alert">
              {workshopErrorText(
                resolve.error,
                "La résolution du conflit a été refusée.",
              )}
            </p>
          ) : null}
          {resolve.data === undefined ? null : (
            <p role="status">
              Décision enregistrée : <code>{resolve.data.decision_id}</code> ·{" "}
              {labelOf(CONFLICT_CHOICE_LABELS, resolve.data.choice)}
              {resolve.data.result_revision === null
                ? ""
                : ` · révision ${resolve.data.result_revision.revision_id}`}
              .
            </p>
          )}
        </div>
      </details>
    </section>
  );
}
