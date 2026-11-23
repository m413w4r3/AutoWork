import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import { articleAnchorsKeys, useArticleAnchors } from "../api/articleAnchors";
import {
  useRequestNoteProposal,
  type AnalystObservationRelation,
  type AnalystObservationState,
  type NoteContentBlockV1,
  type NoteProposalRequestV1,
  type ProseOpinionBlockType,
} from "../api/opinionProposals";
import type { TaskView } from "../api/tasks";
import { AnchorPicker } from "./AnchorPicker";
import { ANCHOR_EXAMPLES, classifyAnchor } from "./anchor";
import { describeApiError, isAnchorReloadError } from "./errors";
import {
  BLOCK_LABELS,
  BLOCK_ORDER,
  EVIDENCE_KIND_LABELS,
  FINDING_KIND_LABELS,
  NOTE_RELATION_LABELS,
  NOTE_STATE_LABELS,
  truncate,
} from "./labels";
import {
  buildDraftingQuestion,
  getDraftingIntent,
  type DraftingIntent,
} from "./intents";
import type { AnalystNoteV1, OpinionVersionV1 } from "./types";

export interface BridgeCorrectionSeed {
  anchor: string;
  blockType: ProseOpinionBlockType;
  contentBlocks: NoteContentBlockV1[];
  findingRefs: string[];
  observationRefs: string[];
  noteState: AnalystObservationState;
  noteRelation: AnalystObservationRelation;
  taskId: string | null;
}

function toggle(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter((item) => item !== id) : [...list, id];
}

/**
 * F1 task-card links open this view as
 * `?vue=opinion&task_id=<id>&bridge_intent=draft_text|review_text`.
 * Only these two values are canonical; anything else falls back to drafting.
 */
function bridgeIntentFromParam(value: string | null): DraftingIntent {
  return value === "review_text" ? "verifier" : "rediger";
}

function searchParam(name: string): string | null {
  return new URL(window.location.href).searchParams.get(name);
}

const REQUEST_MODES: Array<{ id: DraftingIntent; label: string }> = [
  { id: "rediger", label: "Rédiger" },
  { id: "corriger", label: "Réviser / corriger" },
  { id: "verifier", label: "Critiquer" },
];

function locationLabel(
  anchor: string,
  catalog: ReturnType<typeof useArticleAnchors>["data"],
): string {
  const item = catalog?.anchors.find(
    (candidate) => candidate.anchor === anchor,
  );
  if (item === undefined) return "Emplacement de l’article non disponible";
  const paragraph = (item.paragraph_index ?? 0) + 1;
  if (item.section_index === null) return `Chapô, paragraphe ${paragraph}`;
  const section = item.section_index + 1;
  const heading = catalog?.sections?.find(
    (candidate) => candidate.section_index === item.section_index,
  )?.heading;
  return `Section ${section}${heading ? ` · ${heading}` : ""}, paragraphe ${paragraph}`;
}

function observationOptionLabel(observation: InvestigationObservation): string {
  const kind = EVIDENCE_KIND_LABELS[observation.kind] ?? observation.kind;
  return `${kind} · ${observation.source} · ${observation.object_type} ${observation.object_id}`;
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
  contentBlocks,
  onAnchorChange,
  onBlockTypeChange,
  onRequested,
  correctionSeed = null,
  children,
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
  contentBlocks: NoteContentBlockV1[];
  onAnchorChange: (anchor: string) => void;
  onBlockTypeChange: (blockType: ProseOpinionBlockType) => void;
  onRequested: (proposalId: string) => void;
  correctionSeed?: BridgeCorrectionSeed | null;
  children?: ReactNode;
}) {
  const queryClient = useQueryClient();
  const request = useRequestNoteProposal(investigationId);
  const articleAnchorsQuery = useArticleAnchors(investigationId);
  const articleCatalog = articleAnchorsQuery.data;
  // A task-card link pre-fills the request; the URL is read once at mount so a
  // later manual change is never overwritten.
  const [requestedTaskId] = useState(() => searchParam("task_id") ?? "");
  const [intent, setIntent] = useState<DraftingIntent>(() =>
    correctionSeed === null
      ? bridgeIntentFromParam(searchParam("bridge_intent"))
      : "corriger",
  );
  const [correctionInstruction, setCorrectionInstruction] = useState("");
  const [findingRefs, setFindingRefs] = useState<string[]>(
    () => correctionSeed?.findingRefs ?? [],
  );
  const [observationRefs, setObservationRefs] = useState<string[]>(
    () => correctionSeed?.observationRefs ?? [],
  );
  const [taskId, setTaskId] = useState(() => correctionSeed?.taskId ?? "");
  const [noteState, setNoteState] = useState<AnalystObservationState>(
    () => correctionSeed?.noteState ?? "unresolved",
  );
  const [noteRelation, setNoteRelation] = useState<AnalystObservationRelation>(
    () => correctionSeed?.noteRelation ?? "unresolved",
  );

  const targetType = classifyAnchor(anchor);
  const currentNote = notes.find((note) => note.anchor === anchor) ?? null;
  const reviewAvailable = currentNote?.bridge_authored === true;
  const selectedIntent = getDraftingIntent(intent);

  useEffect(() => {
    if (correctionSeed !== null || requestedTaskId === "" || taskId !== "") {
      return;
    }
    if (tasks.some((task) => task.id === requestedTaskId)) {
      setTaskId(requestedTaskId);
    }
  }, [correctionSeed, requestedTaskId, taskId, tasks]);

  useEffect(() => {
    // The anchor is selected asynchronously; until one is chosen the note
    // existence is unknown, so a requested review intent is not discarded.
    if (
      selectedIntent.requiresCurrentBridgeNote &&
      !reviewAvailable &&
      anchor !== ""
    ) {
      setIntent("rediger");
    }
  }, [anchor, reviewAvailable, selectedIntent.requiresCurrentBridgeNote]);

  const sourcesMissing =
    noteState !== "unresolved" &&
    findingRefs.length === 0 &&
    observationRefs.length === 0;
  const correctionInstructionMissing =
    intent === "corriger" && correctionInstruction.trim() === "";

  const submitBlockedReason =
    targetType === null
      ? `Ancre invalide. Utilisez ${ANCHOR_EXAMPLES}.`
      : workingStateError
        ? "L’état de travail de l’enquête est indisponible : la demande est désactivée."
        : workingStateVersion === null
          ? "La version de l’état de travail n’est pas encore chargée."
          : selectedIntent.requiresCurrentBridgeNote && !reviewAvailable
            ? "La critique exige une note existante rédigée par Bridge à cette ancre."
            : sourcesMissing
              ? "Une note observée ou inférée doit citer au moins un finding ou une observation."
              : correctionInstructionMissing
                ? "Saisissez une consigne de correction pour Bridge."
                : null;

  function submit() {
    if (targetType === null || workingStateVersion === null) return;
    const baseQuestion = buildDraftingQuestion(intent, {
      anchor,
      blockType,
      state: noteState,
      relation: noteRelation,
      findingRefs,
      observationRefs,
    });
    const question =
      intent === "corriger"
        ? `${baseQuestion}\nConsigne de correction de l’analyste à Bridge (instruction uniquement, jamais texte publiable) :\n${correctionInstruction.trim()}`
        : baseQuestion;
    const common = {
      anchor,
      target_block_type: targetType,
      bridge_block_type: blockType,
      consultation_type: selectedIntent.consultationType,
      note_state: noteState,
      note_relation: noteRelation,
      finding_refs: findingRefs,
      observation_refs: observationRefs,
      ...(contentBlocks.length === 0 ? {} : { content_blocks: contentBlocks }),
      ...(taskId === "" ? {} : { task_id: taskId }),
      question,
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

  function reloadAnchors() {
    void queryClient.invalidateQueries({
      queryKey: articleAnchorsKeys.all(investigationId),
    });
  }

  const currentLocation = locationLabel(anchor, articleCatalog);

  return (
    <section
      className="job-card opinion-bridge-request"
      aria-labelledby="opinion-bridge-request"
    >
      <h3 id="opinion-bridge-request">
        {correctionSeed === null
          ? "Demander une proposition à Bridge"
          : "Demander une correction à Bridge"}
      </h3>

      <section
        className="opinion-bridge-request__context"
        aria-label="Contexte du paragraphe sélectionné"
      >
        <h4>Contexte du paragraphe</h4>
        <p>{currentLocation}</p>
        {articleAnchorsQuery.isPending ? (
          <p role="status">Chargement du paragraphe…</p>
        ) : articleAnchorsQuery.isError ? (
          <p className="verification-warning" role="note">
            Le texte du paragraphe sélectionné est indisponible.
          </p>
        ) : (
          <p>
            {articleCatalog?.anchors.find((item) => item.anchor === anchor)
              ?.text ?? "Aucun paragraphe correspondant dans le catalogue."}
          </p>
        )}
        {currentNote === null ? (
          <p>Aucune note existante à cet emplacement.</p>
        ) : (
          <section aria-label="Note existante">
            <h5>Note existante</h5>
            <p>{currentNote.text}</p>
          </section>
        )}
      </section>

      {correctionSeed === null ? (
        <AnchorPicker
          investigationId={investigationId}
          anchor={anchor}
          onAnchorChange={onAnchorChange}
          notes={notes}
          proposal={request.data?.proposal ?? null}
        />
      ) : null}

      <fieldset>
        <legend>Findings disponibles ({findings.length})</legend>
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
        <legend>Observations disponibles ({observations.length})</legend>
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
              {observationOptionLabel(observation)}
            </label>
          ))
        )}
        {observations.length > 50 ? (
          <p>Liste bornée aux 50 premières observations.</p>
        ) : null}
      </fieldset>

      <fieldset>
        <legend>Action demandée à Bridge</legend>
        {REQUEST_MODES.map((mode) => {
          const option = getDraftingIntent(mode.id);
          const disabled = option.requiresCurrentBridgeNote && !reviewAvailable;
          return (
            <label key={mode.id}>
              <input
                type="radio"
                name="opinion-drafting-intent"
                value={mode.id}
                checked={intent === mode.id}
                disabled={disabled}
                onChange={() => setIntent(mode.id)}
              />{" "}
              {mode.label}
              {disabled ? " (note Bridge existante requise)" : ""}
            </label>
          );
        })}
      </fieldset>

      {intent === "corriger" ? (
        <>
          <label htmlFor="opinion-correction-instruction">
            Consigne de correction pour Bridge (instruction, jamais texte
            publiable)
          </label>
          <textarea
            id="opinion-correction-instruction"
            value={correctionInstruction}
            onChange={(event) =>
              setCorrectionInstruction(event.currentTarget.value)
            }
          />
        </>
      ) : null}

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

      <details>
        <summary>Qualification et contenu vérifiable</summary>
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

        {children}
      </details>

      {submitBlockedReason ? (
        <p className="verification-warning" role="note">
          {submitBlockedReason}
        </p>
      ) : null}
      {request.isError ? (
        <>
          <p className="error-message" role="alert">
            Impossible de demander la proposition :{" "}
            {describeApiError(request.error).message} · code :{" "}
            {describeApiError(request.error).code}
          </p>
          {isAnchorReloadError(request.error) ? (
            <div className="verification-warning" role="alert">
              <p>
                L’ancre ou la base de publication a changé. Rechargez les ancres
                avant de redemander une proposition.
              </p>
              <button type="button" onClick={reloadAnchors}>
                Recharger les ancres
              </button>
            </div>
          ) : null}
        </>
      ) : null}
      {request.isPending ? (
        <p role="status" aria-live="polite">
          Demande à Bridge en cours ; cette opération peut prendre plusieurs
          minutes.
        </p>
      ) : null}
      {request.data ? (
        <p role="status">
          Nouvelle proposition Bridge demandée · statut{" "}
          {request.data.invocation_status} ·{" "}
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
        {request.isPending
          ? "Demande en cours…"
          : intent === "corriger"
            ? "Envoyer la demande de correction"
            : "Demander à Bridge"}
      </button>
    </section>
  );
}
