import { useId, useState, type FormEvent } from "react";

import {
  useAnnotateWorkspaceNote,
  useAskAgentToContinueFromNote,
  useProposeNoteObservationConversion,
  type NoteAskAgentRequestV1,
  type NoteContributionMethod,
  type NoteContributionObjectType,
  type NoteIocType,
  type NoteObservationProposalRequestV1,
  type NoteViewResponseV1,
} from "../api";
import {
  ANNOTATION_KIND_LABELS,
  CONTRIBUTION_CATEGORY_LABELS,
  CONTRIBUTION_IMPORT_LABELS,
  CONTRIBUTION_METHODS,
  CONTRIBUTION_METHOD_LABELS,
  CONTRIBUTION_OBJECT_LABELS,
  CONTRIBUTION_OBJECT_TYPES,
  IOC_TYPE_LABELS,
  INSTRUCTION_STATE_LABELS,
  INVOCATION_STATUS_LABELS,
  NOTE_IOC_TYPES,
  ORIGIN_LABELS,
  formatTimestamp,
  labelOf,
  workshopErrorText,
} from "./workspaceLabels";

function isIocType(value: string): value is NoteIocType {
  return NOTE_IOC_TYPES.some((candidate) => candidate === value);
}

function isObjectType(value: string): value is NoteContributionObjectType {
  return CONTRIBUTION_OBJECT_TYPES.some((candidate) => candidate === value);
}

function isMethod(value: string): value is NoteContributionMethod {
  return CONTRIBUTION_METHODS.some((candidate) => candidate === value);
}

export function NoteDetailPanel({
  investigationId,
  notePath,
  note,
}: {
  investigationId: string;
  notePath: string;
  note: NoteViewResponseV1;
}) {
  const baseId = useId();
  const revisionId = note.latest.revision_id;
  const annotation = useAnnotateWorkspaceNote(
    investigationId,
    revisionId,
    notePath,
  );
  const askAgent = useAskAgentToContinueFromNote(
    investigationId,
    revisionId,
    notePath,
  );
  const proposal = useProposeNoteObservationConversion(
    investigationId,
    revisionId,
  );

  const [taskId, setTaskId] = useState("");
  const [iocType, setIocType] = useState<NoteIocType>("domain");
  const [iocValue, setIocValue] = useState("");
  const [sampleId, setSampleId] = useState("");
  const [agentQuestion, setAgentQuestion] = useState("");
  const defaultAgentTask = note.linked_task_ids[0] ?? "";
  const [agentTaskId, setAgentTaskId] = useState(defaultAgentTask);
  const selectedAgentTask = note.linked_task_ids.includes(agentTaskId)
    ? agentTaskId
    : defaultAgentTask;
  const [proposalObjectType, setProposalObjectType] =
    useState<NoteContributionObjectType>("domain");
  const [proposalMethod, setProposalMethod] =
    useState<NoteContributionMethod>("dns_query");
  const [proposalTool, setProposalTool] = useState("");
  const [proposalObjectId, setProposalObjectId] = useState("");

  function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = taskId.trim();
    if (value === "") return;
    annotation.mutate({ kind: "link_task", task_id: value });
  }

  function submitIoc(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = iocValue.trim();
    if (value === "") return;
    annotation.mutate({
      kind: "link_ioc",
      ioc_type: iocType,
      ioc_value: value,
    });
  }

  function submitSample(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = sampleId.trim();
    if (value === "") return;
    annotation.mutate({ kind: "link_sample", sample_id: value });
  }

  function submitAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = agentQuestion.trim();
    if (question === "" || selectedAgentTask === "") return;
    const input: NoteAskAgentRequestV1 = {
      task_id: selectedAgentTask,
      question,
    };
    askAgent.mutate(input);
  }

  function submitProposal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const tool = proposalTool.trim();
    if (tool === "") return;
    const input: NoteObservationProposalRequestV1 = {
      object_type: proposalObjectType,
      method: proposalMethod,
      tool,
    };
    const objectId = proposalObjectId.trim();
    if (objectId !== "") input.object_id = objectId;
    proposal.mutate(input);
  }

  return (
    <section
      className="workspace-notes__section"
      aria-label={`Note ${notePath}`}
    >
      <h2>Note sélectionnée</h2>
      <h3>{notePath}</h3>
      <div className="badge-row">
        {note.is_evidence ? (
          <span className="badge">Preuve canonique</span>
        ) : (
          <span className="badge">Note de travail — non probante</span>
        )}
        {note.unverified_hypothesis ? (
          <span className="badge">Hypothèse humaine — non vérifiée</span>
        ) : null}
        {note.observation_imported ? (
          <span className="badge">Observation canonique importée</span>
        ) : null}
        {note.useful ? <span className="badge">Marquée utile</span> : null}
        {note.dismissed ? <span className="badge">Écartée</span> : null}
      </div>
      <p className="workspace-notes__meta">
        Un fichier local n’est pas une preuve canonique. Cette note reste une
        hypothèse de travail tant qu’aucune observation vérifiable n’a été
        importée par le circuit canonique.
      </p>
      <dl className="workspace-notes__counts">
        <div>
          <dt>Révision</dt>
          <dd>
            <code>{revisionId}</code>
          </dd>
        </div>
        <div>
          <dt>SHA-256</dt>
          <dd>
            <code>{note.latest.sha256}</code>
          </dd>
        </div>
        <div>
          <dt>Observé le</dt>
          <dd>
            <time dateTime={note.latest.observed_at}>
              {formatTimestamp(note.latest.observed_at)}
            </time>
          </dd>
        </div>
        <div>
          <dt>Origine</dt>
          <dd>{ORIGIN_LABELS[note.latest.origin]}</dd>
        </div>
      </dl>

      <h4>Révisions</h4>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Révision</th>
              <th scope="col">SHA-256</th>
              <th scope="col">Observé le</th>
              <th scope="col">Origine</th>
              <th scope="col">Remplace</th>
            </tr>
          </thead>
          <tbody>
            {note.versions.map((version) => (
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

      <h4>Liens de la note</h4>
      <ul className="workspace-notes__list">
        <li>
          Tâches :{" "}
          {note.linked_task_ids.length === 0
            ? "aucune"
            : note.linked_task_ids.join(", ")}
        </li>
        <li>
          IOC :{" "}
          {note.linked_iocs.length === 0
            ? "aucun"
            : note.linked_iocs
                .map((ioc) => `${IOC_TYPE_LABELS[ioc.ioc_type]} ${ioc.value}`)
                .join(", ")}
        </li>
        <li>
          Échantillons :{" "}
          {note.linked_sample_ids.length === 0
            ? "aucun"
            : note.linked_sample_ids.join(", ")}
        </li>
        <li>
          Auteurs attribués :{" "}
          {note.attributed_authors.length === 0
            ? "aucun (jamais déduit du nom de dossier)"
            : note.attributed_authors.join(", ")}
        </li>
      </ul>

      <h4>Annoter la note</h4>
      <div className="workspace-notes__actions">
        <button
          className="button button--secondary"
          type="button"
          disabled={annotation.isPending}
          onClick={() =>
            annotation.mutate({
              kind: note.useful ? "unmark_useful" : "mark_useful",
            })
          }
        >
          {note.useful ? "Retirer « utile »" : "Marquer utile"}
        </button>
      </div>
      <div className="workspace-notes__forms">
        <form className="workspace-notes__form" onSubmit={submitTask}>
          <label htmlFor={`${baseId}-task`}>Lier à une tâche</label>
          <input
            id={`${baseId}-task`}
            value={taskId}
            onChange={(event) => setTaskId(event.target.value)}
            placeholder="identifiant de tâche"
          />
          <button
            className="button button--secondary"
            type="submit"
            disabled={annotation.isPending || taskId.trim() === ""}
          >
            Lier la tâche
          </button>
        </form>
        <form className="workspace-notes__form" onSubmit={submitIoc}>
          <label htmlFor={`${baseId}-ioc-type`}>Type d’IOC</label>
          <select
            id={`${baseId}-ioc-type`}
            value={iocType}
            onChange={(event) => {
              const value = event.target.value;
              if (isIocType(value)) setIocType(value);
            }}
          >
            {NOTE_IOC_TYPES.map((value) => (
              <option key={value} value={value}>
                {IOC_TYPE_LABELS[value]}
              </option>
            ))}
          </select>
          <label htmlFor={`${baseId}-ioc-value`}>Valeur de l’IOC</label>
          <input
            id={`${baseId}-ioc-value`}
            value={iocValue}
            onChange={(event) => setIocValue(event.target.value)}
          />
          <button
            className="button button--secondary"
            type="submit"
            disabled={annotation.isPending || iocValue.trim() === ""}
          >
            Lier l’IOC
          </button>
        </form>
        <form className="workspace-notes__form" onSubmit={submitSample}>
          <label htmlFor={`${baseId}-sample`}>Lier un échantillon</label>
          <input
            id={`${baseId}-sample`}
            value={sampleId}
            onChange={(event) => setSampleId(event.target.value)}
            placeholder="identifiant d’échantillon"
          />
          <button
            className="button button--secondary"
            type="submit"
            disabled={annotation.isPending || sampleId.trim() === ""}
          >
            Lier l’échantillon
          </button>
        </form>
      </div>
      {annotation.isPending ? <p role="status">Annotation en cours…</p> : null}
      {annotation.error !== null ? (
        <p className="error-message" role="alert">
          {workshopErrorText(annotation.error, "L’annotation a été refusée.")}
        </p>
      ) : null}
      {annotation.isSuccess ? (
        <p role="status">Annotation enregistrée.</p>
      ) : null}

      <h4>Annotations</h4>
      {note.annotations.length === 0 ? (
        <p>Aucune annotation.</p>
      ) : (
        <ul className="workspace-notes__list">
          {note.annotations.map((entry) => (
            <li key={entry.id}>
              {labelOf(ANNOTATION_KIND_LABELS, entry.kind)} ·{" "}
              <time dateTime={entry.created_at}>
                {formatTimestamp(entry.created_at)}
              </time>
              {entry.target_task_id === null
                ? ""
                : ` · tâche ${entry.target_task_id}`}
              {entry.ioc_value === null
                ? ""
                : ` · IOC ${entry.ioc_type ?? ""} ${entry.ioc_value}`}
              {entry.target_sample_id === null
                ? ""
                : ` · échantillon ${entry.target_sample_id}`}
              {entry.detail === null ? "" : ` · ${entry.detail}`}
            </li>
          ))}
        </ul>
      )}

      <h4>Demander à l’agent de continuer</h4>
      {note.linked_task_ids.length === 0 ? (
        <p className="workspace-notes__hint">
          Action indisponible : liez d’abord la note à une tâche ; une
          instruction de session doit citer une tâche de cette enquête.
        </p>
      ) : (
        <form className="workspace-notes__form" onSubmit={submitAgent}>
          <label htmlFor={`${baseId}-agent-task`}>Tâche liée</label>
          <select
            id={`${baseId}-agent-task`}
            value={selectedAgentTask}
            onChange={(event) => setAgentTaskId(event.target.value)}
          >
            {note.linked_task_ids.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
          <label htmlFor={`${baseId}-agent-question`}>Question à l’agent</label>
          <textarea
            id={`${baseId}-agent-question`}
            value={agentQuestion}
            onChange={(event) => setAgentQuestion(event.target.value)}
          />
          <button
            className="button"
            type="submit"
            disabled={askAgent.isPending || agentQuestion.trim() === ""}
          >
            {askAgent.isPending
              ? "Instruction en cours d’envoi…"
              : "Demander à l’agent de continuer"}
          </button>
        </form>
      )}
      {askAgent.error !== null ? (
        <p className="error-message" role="alert">
          {workshopErrorText(
            askAgent.error,
            "L’instruction à l’agent a été refusée.",
          )}
        </p>
      ) : null}
      {askAgent.data === undefined ? null : (
        <p role="status">
          Instruction{" "}
          {labelOf(INSTRUCTION_STATE_LABELS, askAgent.data.instruction_state)} —
          session <code>{askAgent.data.session_id}</code>, tâche{" "}
          <code>{askAgent.data.task_id}</code>.
        </p>
      )}

      <h4>Demander à Bridge de relire</h4>
      <div className="workspace-notes__unavailable">
        <p>
          Action indisponible : le client API figé n’expose pas la version
          d’état de travail requise (<code>expected_working_state_version</code>
          ). Aucune demande Bridge n’est envoyée et aucune provenance n’est
          inventée.
        </p>
      </div>

      <h4>Proposer la conversion en observations vérifiables</h4>
      <form className="workspace-notes__form" onSubmit={submitProposal}>
        <label htmlFor={`${baseId}-proposal-type`}>Type d’objet</label>
        <select
          id={`${baseId}-proposal-type`}
          value={proposalObjectType}
          onChange={(event) => {
            const value = event.target.value;
            if (isObjectType(value)) setProposalObjectType(value);
          }}
        >
          {CONTRIBUTION_OBJECT_TYPES.map((value) => (
            <option key={value} value={value}>
              {CONTRIBUTION_OBJECT_LABELS[value]}
            </option>
          ))}
        </select>
        <label htmlFor={`${baseId}-proposal-method`}>Méthode</label>
        <select
          id={`${baseId}-proposal-method`}
          value={proposalMethod}
          onChange={(event) => {
            const value = event.target.value;
            if (isMethod(value)) setProposalMethod(value);
          }}
        >
          {CONTRIBUTION_METHODS.map((value) => (
            <option key={value} value={value}>
              {CONTRIBUTION_METHOD_LABELS[value]}
            </option>
          ))}
        </select>
        <label htmlFor={`${baseId}-proposal-tool`}>Outil</label>
        <input
          id={`${baseId}-proposal-tool`}
          value={proposalTool}
          onChange={(event) => setProposalTool(event.target.value)}
        />
        <label htmlFor={`${baseId}-proposal-object`}>
          Identifiant d’objet (facultatif)
        </label>
        <input
          id={`${baseId}-proposal-object`}
          value={proposalObjectId}
          onChange={(event) => setProposalObjectId(event.target.value)}
        />
        <button
          className="button"
          type="submit"
          disabled={proposal.isPending || proposalTool.trim() === ""}
        >
          {proposal.isPending
            ? "Déclaration en cours…"
            : "Proposer la conversion en observations vérifiables"}
        </button>
      </form>
      <p className="workspace-notes__meta">
        Cette action est une déclaration : elle n’écrit aucune observation et ne
        crée aucune preuve. L’import reste une décision humaine via le circuit
        des contributions.
      </p>
      {proposal.error !== null ? (
        <p className="error-message" role="alert">
          {workshopErrorText(
            proposal.error,
            "La proposition de conversion a été refusée.",
          )}
        </p>
      ) : null}
      {proposal.data === undefined ? null : (
        <div className="workspace-notes__note" role="status">
          <p>
            Proposition déclarée (<code>{proposal.data.import_service}</code>) —
            catégorie d’import{" "}
            {CONTRIBUTION_CATEGORY_LABELS[proposal.data.import_category]} ·{" "}
            {proposal.data.requires_human_decision
              ? "décision humaine requise"
              : "décision humaine non requise"}
            .
          </p>
          <p>
            Aucune écriture canonique : {proposal.data.canonical_writes.length}{" "}
            écriture(s). Preuve canonique :{" "}
            {proposal.data.is_evidence ? "oui" : "non"}.
          </p>
        </div>
      )}

      <h4>Contributions liées à la note</h4>
      {note.contributions.length === 0 ? (
        <p>Aucune contribution liée.</p>
      ) : (
        <ul className="workspace-notes__list">
          {note.contributions.map((contribution) => (
            <li key={contribution.id}>
              {CONTRIBUTION_CATEGORY_LABELS[contribution.category]} ·{" "}
              {CONTRIBUTION_IMPORT_LABELS[contribution.import_status]}
              {contribution.observation_id === null
                ? ""
                : ` · observation ${contribution.observation_id}`}
              {contribution.finding_id === null
                ? ""
                : ` · finding ${contribution.finding_id}`}
            </li>
          ))}
        </ul>
      )}

      <h4>Instructions et transferts liés</h4>
      <ul className="workspace-notes__list">
        <li>
          Instructions :{" "}
          {note.instructions.length === 0
            ? "aucune"
            : note.instructions
                .map(
                  (instruction) =>
                    `${instruction.instruction_id} (${labelOf(
                      INSTRUCTION_STATE_LABELS,
                      instruction.state,
                    )}, session ${instruction.session_id})`,
                )
                .join(" · ")}
        </li>
        <li>
          Passations :{" "}
          {note.handoffs.length === 0
            ? "aucune"
            : note.handoffs
                .map(
                  (handoff) =>
                    `${handoff.handoff_id} (tâche ${handoff.task_id}, v${handoff.version})`,
                )
                .join(" · ")}
        </li>
        <li>
          Consultations Bridge :{" "}
          {note.consultations.length === 0
            ? "aucune"
            : note.consultations
                .map(
                  (consultation) =>
                    `${consultation.consultation_type} (${labelOf(
                      INVOCATION_STATUS_LABELS,
                      consultation.status,
                    )}, invocation ${consultation.invocation_id})`,
                )
                .join(" · ")}
        </li>
      </ul>
      <p className="workspace-notes__meta">
        Un transfert de tâche reste une décision de la vue « Plan et tâches » ;
        cette vue se limite aux usages rattachés à la note.
      </p>
    </section>
  );
}
