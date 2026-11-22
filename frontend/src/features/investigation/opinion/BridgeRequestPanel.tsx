import { useEffect, useState } from "react";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import {
  useRequestNoteProposal,
  type AnalystObservationRelation,
  type AnalystObservationState,
  type NoteProposalRequestV1,
  type ProseOpinionBlockType,
} from "../api/opinionProposals";
import type { TaskView } from "../api/tasks";
import { ANCHOR_EXAMPLES, classifyAnchor } from "./anchor";
import { describeApiError } from "./errors";
import {
  DRAFTING_INTENTS,
  buildDraftingQuestion,
  getDraftingIntent,
  type DraftingIntent,
} from "./intents";
import {
  BLOCK_LABELS,
  BLOCK_ORDER,
  EVIDENCE_KIND_LABELS,
  FINDING_KIND_LABELS,
  NOTE_RELATION_LABELS,
  NOTE_STATE_LABELS,
  TARGET_BLOCK_LABELS,
  truncate,
} from "./labels";
import type { AnalystNoteV1, OpinionVersionV1 } from "./types";

function toggle(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter((item) => item !== id) : [...list, id];
}

export function BridgeRequestPanel({
  investigationId,
  opinion,
  notes,
  findings,
  observations,
  tasks,
  workingStateVersion,
  workingStateError,
  anchor,
  blockType,
  onAnchorChange,
  onBlockTypeChange,
  onRequested,
}: {
  investigationId: string;
  opinion: OpinionVersionV1 | null;
  notes: AnalystNoteV1[];
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
  tasks: TaskView[];
  workingStateVersion: number | null;
  workingStateError: boolean;
  anchor: string;
  blockType: ProseOpinionBlockType;
  onAnchorChange: (anchor: string) => void;
  onBlockTypeChange: (blockType: ProseOpinionBlockType) => void;
  onRequested: (proposalId: string) => void;
}) {
  const request = useRequestNoteProposal(investigationId);
  const [intent, setIntent] = useState<DraftingIntent>("rediger");
  const [findingRefs, setFindingRefs] = useState<string[]>([]);
  const [observationRefs, setObservationRefs] = useState<string[]>([]);
  const [taskId, setTaskId] = useState("");
  const [noteState, setNoteState] =
    useState<AnalystObservationState>("unresolved");
  const [noteRelation, setNoteRelation] =
    useState<AnalystObservationRelation>("unresolved");

  const targetType = classifyAnchor(anchor);
  const currentNote = notes.find((note) => note.anchor === anchor) ?? null;
  const reviewAvailable = currentNote?.bridge_authored === true;
  const selectedIntent = getDraftingIntent(intent);

  useEffect(() => {
    if (selectedIntent.requiresCurrentBridgeNote && !reviewAvailable) {
      setIntent("rediger");
    }
  }, [reviewAvailable, selectedIntent.requiresCurrentBridgeNote]);

  const sourcesMissing =
    noteState !== "unresolved" &&
    findingRefs.length === 0 &&
    observationRefs.length === 0;

  const submitBlockedReason =
    targetType === null
      ? `Ancre invalide. Utilisez ${ANCHOR_EXAMPLES}.`
      : workingStateError
        ? "L’état de travail de l’enquête est indisponible : la demande est désactivée."
        : workingStateVersion === null
          ? "La version de l’état de travail n’est pas encore chargée."
          : selectedIntent.requiresCurrentBridgeNote && !reviewAvailable
            ? "La vérification exige une note existante rédigée par Bridge à cette ancre."
            : sourcesMissing
              ? "Une note observée ou inférée doit citer au moins un finding ou une observation."
              : null;

  function submit() {
    if (targetType === null || workingStateVersion === null) return;
    const common = {
      anchor,
      target_block_type: targetType,
      bridge_block_type: blockType,
      consultation_type: selectedIntent.consultationType,
      note_state: noteState,
      note_relation: noteRelation,
      finding_refs: findingRefs,
      observation_refs: observationRefs,
      ...(taskId === "" ? {} : { task_id: taskId }),
      question: buildDraftingQuestion(intent, {
        anchor,
        blockType,
        state: noteState,
        relation: noteRelation,
        findingRefs,
        observationRefs,
      }),
      expected_working_state_version: workingStateVersion,
    };
    const payload: NoteProposalRequestV1 = opinion
      ? {
          ...common,
          base_opinion_version: opinion.version,
          base_opinion_sha256: opinion.content_sha256,
        }
      : common;
    request.mutate(payload, {
      onSuccess: (response) => onRequested(response.proposal.id),
    });
  }

  const knownAnchors = [...new Set(notes.map((note) => note.anchor))];

  return (
    <section className="job-card" aria-labelledby="opinion-bridge-request">
      <h3 id="opinion-bridge-request">Demander une proposition à Bridge</h3>
      <p>
        La demande produit une proposition ciblée et versionnée. Elle ne réécrit
        jamais silencieusement tout l’avis : seul le texte stocké de la réponse
        Bridge pourra être accepté, et uniquement par une décision humaine.
      </p>

      <label htmlFor="opinion-note-anchor">Ancre cible</label>
      <input
        id="opinion-note-anchor"
        list="opinion-note-anchor-options"
        value={anchor}
        onChange={(event) => onAnchorChange(event.currentTarget.value)}
        aria-describedby="opinion-note-anchor-help"
      />
      <datalist id="opinion-note-anchor-options">
        {knownAnchors.map((item) => (
          <option key={item} value={item} />
        ))}
      </datalist>
      <p id="opinion-note-anchor-help">
        {ANCHOR_EXAMPLES}.{" "}
        {targetType === null
          ? "Ancre invalide."
          : `Espace d’ancrage : ${TARGET_BLOCK_LABELS[targetType]}.`}
      </p>

      <label htmlFor="opinion-note-block">Bloc de l’avis visé</label>
      <select
        id="opinion-note-block"
        value={blockType}
        onChange={(event) =>
          onBlockTypeChange(event.currentTarget.value as ProseOpinionBlockType)
        }
      >
        {BLOCK_ORDER.map((value) => (
          <option key={value} value={value}>
            {BLOCK_LABELS[value]}
          </option>
        ))}
      </select>

      <fieldset>
        <legend>Action demandée à Bridge</legend>
        {DRAFTING_INTENTS.map((option) => {
          const disabled = option.requiresCurrentBridgeNote && !reviewAvailable;
          return (
            <label key={option.id}>
              <input
                type="radio"
                name="opinion-drafting-intent"
                value={option.id}
                checked={intent === option.id}
                disabled={disabled}
                onChange={() => setIntent(option.id)}
              />{" "}
              {option.label}
              {disabled ? " (note Bridge existante requise)" : ""}
            </label>
          );
        })}
      </fieldset>

      <label htmlFor="opinion-note-state">État de la note</label>
      <select
        id="opinion-note-state"
        value={noteState}
        onChange={(event) =>
          setNoteState(event.currentTarget.value as AnalystObservationState)
        }
      >
        {Object.entries(NOTE_STATE_LABELS).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>

      <label htmlFor="opinion-note-relation">Relation aux références</label>
      <select
        id="opinion-note-relation"
        value={noteRelation}
        onChange={(event) =>
          setNoteRelation(
            event.currentTarget.value as AnalystObservationRelation,
          )
        }
      >
        {Object.entries(NOTE_RELATION_LABELS).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>

      <fieldset>
        <legend>Findings et revendications cités</legend>
        {findings.length === 0 ? (
          <p>Aucun finding disponible dans cette enquête.</p>
        ) : (
          findings.slice(0, 50).map((finding) => (
            <label key={finding.id}>
              <input
                type="checkbox"
                checked={findingRefs.includes(finding.id)}
                onChange={() =>
                  setFindingRefs((current) => toggle(current, finding.id))
                }
              />{" "}
              {FINDING_KIND_LABELS[finding.kind] ?? finding.kind} ·{" "}
              {truncate(finding.statement)}
            </label>
          ))
        )}
        {findings.length > 50 ? (
          <p>Liste bornée aux 50 premiers findings.</p>
        ) : null}
      </fieldset>

      <fieldset>
        <legend>Observations citées</legend>
        {observations.length === 0 ? (
          <p>Aucune observation disponible dans cette enquête.</p>
        ) : (
          observations.slice(0, 50).map((observation) => (
            <label key={observation.id}>
              <input
                type="checkbox"
                checked={observationRefs.includes(observation.id)}
                onChange={() =>
                  setObservationRefs((current) =>
                    toggle(current, observation.id),
                  )
                }
              />{" "}
              {EVIDENCE_KIND_LABELS[observation.kind] ?? observation.kind} ·
              SHA-256 {observation.sha256.slice(0, 12)}… · {observation.source}
            </label>
          ))
        )}
        {observations.length > 50 ? (
          <p>Liste bornée aux 50 premières observations.</p>
        ) : null}
      </fieldset>

      <label htmlFor="opinion-note-task">Tâche liée (facultatif)</label>
      <select
        id="opinion-note-task"
        value={taskId}
        onChange={(event) => setTaskId(event.currentTarget.value)}
      >
        <option value="">Aucune tâche</option>
        {tasks.map((task) => (
          <option key={task.id} value={task.id}>
            {task.task_key} · {task.title} · {task.status}
          </option>
        ))}
      </select>

      {submitBlockedReason ? (
        <p className="verification-warning" role="note">
          {submitBlockedReason}
        </p>
      ) : null}
      {request.isError ? (
        <p className="error-message" role="alert">
          Impossible de demander la proposition :{" "}
          {describeApiError(request.error).message} · code :{" "}
          {describeApiError(request.error).code}
        </p>
      ) : null}
      {request.isPending ? (
        <p role="status" aria-live="polite">
          Demande à Bridge en cours ; cette opération peut prendre plusieurs
          minutes.
        </p>
      ) : null}
      {request.data ? (
        <p role="status">
          Proposition demandée · invocation {request.data.invocation_id} ·
          statut {request.data.invocation_status} ·{" "}
          {request.data.materialised
            ? "texte Bridge stocké disponible"
            : "réponse Bridge en attente"}
          .
        </p>
      ) : null}

      <button
        type="button"
        className="button button--secondary"
        disabled={submitBlockedReason !== null || request.isPending}
        aria-busy={request.isPending}
        onClick={submit}
      >
        Demander à Bridge
      </button>
    </section>
  );
}
