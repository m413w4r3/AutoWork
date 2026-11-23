import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { useInvestigation } from "../../../api/investigation";
import {
  useNoteProposals,
  type NoteContentBlockV1,
  type NoteProposalStatus,
  type ProseOpinionBlockType,
} from "../api/opinionProposals";
import { useTasks } from "../api/tasks";
import { groupNotesByAnchor } from "./anchor";
import { BridgeRequestPanel } from "./BridgeRequestPanel";
import { describeApiError } from "./errors";
import { HistoryPanel } from "./HistoryPanel";
import { NoteAnchorList } from "./NoteAnchorList";
import { NoteContentBlocks } from "./NoteContentBlocks";
import { NoteProposalReview } from "./NoteProposalReview";
import { OpinionPreview } from "./OpinionPreview";
import { PreApprovalPanel } from "./PreApprovalPanel";
import { ProposalList } from "./ProposalList";
import {
  opinionNotesKeys,
  useInvestigationWorkingState,
  useOpinionRead,
} from "./queries";
import type { AnalystNoteV1 } from "./types";
import "./opinion.css";

export function OpinionNotesView({
  investigationId,
}: {
  investigationId: string;
}) {
  const queryClient = useQueryClient();
  const opinionQuery = useOpinionRead(investigationId);
  const investigationQuery = useInvestigation(investigationId);
  const tasksQuery = useTasks(investigationId);
  const activityQuery = useInvestigationWorkingState(investigationId);
  const [statusFilter, setStatusFilter] = useState<NoteProposalStatus | "">("");
  const proposalsQuery = useNoteProposals(
    investigationId,
    statusFilter === "" ? undefined : statusFilter,
  );
  const [selectedAnchor, setSelectedAnchor] = useState("");
  const [blockType, setBlockType] =
    useState<ProseOpinionBlockType>("key_findings");
  const [contentBlocks, setContentBlocks] = useState<NoteContentBlockV1[]>([]);
  const [selectedProposalId, setSelectedProposalId] = useState<string | null>(
    null,
  );

  const draft = opinionQuery.data?.draft ?? null;
  const notes = useMemo(() => draft?.observations ?? [], [draft?.observations]);
  const groups = useMemo(() => groupNotesByAnchor(notes), [notes]);
  const findings = investigationQuery.data?.findings ?? [];
  const observations = investigationQuery.data?.observations ?? [];
  const tasks = tasksQuery.data?.items ?? [];
  const workingStateVersion =
    activityQuery.data?.working_state?.version ?? null;

  useEffect(() => {
    if (selectedAnchor !== "") return;
    const firstGroup = groups[0];
    if (firstGroup === undefined) return;
    setSelectedAnchor(firstGroup.anchor);
    const firstNote = firstGroup.notes[0];
    if (
      firstNote?.bridge_block_type !== undefined &&
      firstNote.bridge_block_type !== null
    ) {
      setBlockType(firstNote.bridge_block_type);
    }
  }, [groups, selectedAnchor]);

  function selectNote(note: AnalystNoteV1) {
    changeAnchor(note.anchor);
    if (
      note.bridge_block_type !== undefined &&
      note.bridge_block_type !== null
    ) {
      setBlockType(note.bridge_block_type);
    }
  }

  /**
   * An article element is only referencable at the note anchor, so a block
   * pinned to the previous anchor is dropped when the human picks another one.
   * Canonical code, IOC and protocol-figure references are anchor-independent.
   */
  function changeAnchor(next: string) {
    setSelectedAnchor(next);
    setContentBlocks((current) =>
      current.filter(
        (block) =>
          block.block_type !== "article_element" || block.anchor === next,
      ),
    );
  }

  if (opinionQuery.isPending) {
    return (
      <section
        className="job-card"
        aria-labelledby="opinion-notes-view-heading"
      >
        <h2 id="opinion-notes-view-heading">Note de l’analyste</h2>
        <p role="status">Chargement de la note de l’analyste…</p>
      </section>
    );
  }

  if (opinionQuery.isError) {
    const error = describeApiError(opinionQuery.error);
    return (
      <section
        className="job-card"
        aria-labelledby="opinion-notes-view-heading"
      >
        <h2 id="opinion-notes-view-heading">Note de l’analyste</h2>
        <p className="error-message" role="alert">
          Impossible de charger l’avis : {error.message} · code : {error.code}
        </p>
        <button
          type="button"
          onClick={() =>
            void queryClient.invalidateQueries({
              queryKey: opinionNotesKeys.detail(investigationId),
            })
          }
        >
          Recharger
        </button>
      </section>
    );
  }

  return (
    <section
      className="job-card opinion-notes"
      aria-labelledby="opinion-notes-view-heading"
    >
      <h2 id="opinion-notes-view-heading">Note de l’analyste</h2>
      <p>
        Cette vue ne propose que demander à Bridge, accepter ou refuser. Aucun
        champ ne permet de saisir du texte éditorial de l’avis : le texte
        publiable ne vient que du texte stocké et immuable d’une réponse Bridge,
        accepté par une décision humaine. L’approbation, la publication et le
        réépinglage dans la composition restent des décisions humaines
        distinctes ; une enquête terminée ne publie jamais automatiquement.
      </p>

      {investigationQuery.isError ? (
        <p className="verification-warning" role="status">
          Les findings et observations de l’enquête sont indisponibles :{" "}
          {describeApiError(investigationQuery.error).message}
        </p>
      ) : null}
      {tasksQuery.isError ? (
        <p className="verification-warning" role="status">
          Les tâches de l’enquête sont indisponibles :{" "}
          {describeApiError(tasksQuery.error).message}
        </p>
      ) : null}

      <NoteAnchorList
        groups={groups}
        selectedAnchor={selectedAnchor}
        onSelectNote={selectNote}
      />

      <BridgeRequestPanel
        investigationId={investigationId}
        opinion={draft}
        notes={notes}
        findings={findings}
        observations={observations}
        tasks={tasks}
        workingStateVersion={workingStateVersion}
        workingStateError={activityQuery.isError}
        anchor={selectedAnchor}
        blockType={blockType}
        contentBlocks={contentBlocks}
        onAnchorChange={changeAnchor}
        onBlockTypeChange={setBlockType}
        onRequested={setSelectedProposalId}
      >
        <NoteContentBlocks
          investigationId={investigationId}
          anchor={selectedAnchor}
          observations={observations}
          protocolFigures={draft?.protocol_figures ?? []}
          blocks={contentBlocks}
          onChange={setContentBlocks}
          subjectId={investigationQuery.data?.subject_id ?? null}
        />
      </BridgeRequestPanel>

      <ProposalList
        proposals={proposalsQuery.data ?? []}
        isPending={proposalsQuery.isPending}
        error={proposalsQuery.error}
        selectedProposalId={selectedProposalId}
        onSelect={setSelectedProposalId}
        statusFilter={statusFilter}
        onStatusFilterChange={setStatusFilter}
      />

      {selectedProposalId === null ? null : (
        <NoteProposalReview
          key={selectedProposalId}
          investigationId={investigationId}
          proposalId={selectedProposalId}
          findings={findings}
          observations={observations}
          subjectId={investigationQuery.data?.subject_id ?? null}
          onClose={() => setSelectedProposalId(null)}
        />
      )}

      {draft === null ? (
        <section className="job-card" aria-label="Aperçu du rendu">
          <h3>Aperçu du rendu de l’avis courant</h3>
          <p>
            Aucun avis n’est encore composé. Une proposition Bridge peut être
            demandée puis acceptée ; l’avis sera alors créé par le serveur.
          </p>
        </section>
      ) : (
        <OpinionPreview
          opinion={draft}
          findings={findings}
          observations={observations}
        />
      )}

      <PreApprovalPanel
        investigationId={investigationId}
        opinion={draft}
        citationViolations={opinionQuery.data?.citation_violations ?? []}
        notes={notes}
      />

      <HistoryPanel history={opinionQuery.data?.history ?? []} />
    </section>
  );
}
