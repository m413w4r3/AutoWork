import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { analystOpinionPreviewPdfUrl } from "../../../api/analystOpinion";
import { useInvestigation } from "../../../api/investigation";
import { useArticleAnchors } from "../api/articleAnchors";
import {
  useNoteProposals,
  type NoteContentBlockV1,
  type NoteProposalStatus,
  type ProseOpinionBlockType,
} from "../api/opinionProposals";
import { useTasks } from "../api/tasks";
import { groupAnchors } from "./AnchorPicker";
import { ArticleReadingPane } from "./ArticleReadingPane";
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

type OpinionTab = "notes" | "proposals" | "opinion" | "pdf";

export function OpinionNotesView({
  investigationId,
}: {
  investigationId: string;
}) {
  const queryClient = useQueryClient();
  const opinionQuery = useOpinionRead(investigationId);
  const articleAnchorsQuery = useArticleAnchors(investigationId);
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
  const [activeTab, setActiveTab] = useState<OpinionTab>("notes");

  const draft = opinionQuery.data?.draft ?? null;
  const notes = useMemo(() => draft?.observations ?? [], [draft?.observations]);
  const groups = useMemo(() => groupNotesByAnchor(notes), [notes]);
  const findings = investigationQuery.data?.findings ?? [];
  const observations = investigationQuery.data?.observations ?? [];
  const tasks = tasksQuery.data?.items ?? [];
  const proposals = proposalsQuery.data ?? [];
  const articleCatalog = articleAnchorsQuery.data ?? null;
  const workingStateVersion =
    activityQuery.data?.working_state?.version ?? null;

  useEffect(() => {
    if (selectedAnchor !== "") return;
    const firstGroup = groups[0];
    if (firstGroup !== undefined) {
      setSelectedAnchor(firstGroup.anchor);
      const firstNote = firstGroup.notes[0];
      if (
        firstNote?.bridge_block_type !== undefined &&
        firstNote.bridge_block_type !== null
      ) {
        setBlockType(firstNote.bridge_block_type);
      }
      return;
    }
    const firstArticleAnchor = groupAnchors(articleCatalog?.anchors ?? [])[0]
      ?.anchors[0];
    if (firstArticleAnchor !== undefined) {
      setSelectedAnchor(firstArticleAnchor.anchor);
    }
  }, [articleCatalog, groups, selectedAnchor]);

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

  const selectedArticleParagraph =
    articleCatalog?.anchors.find((item) => item.anchor === selectedAnchor) ??
    null;
  const selectedSectionHeading =
    selectedArticleParagraph?.section_index === null ||
    selectedArticleParagraph === null
      ? null
      : ((articleCatalog?.sections ?? []).find(
          (section) =>
            section.section_index === selectedArticleParagraph.section_index,
        )?.heading ?? null);
  const selectedTargetLabel =
    selectedArticleParagraph === null
      ? "Aucun paragraphe du catalogue n’est sélectionné."
      : selectedArticleParagraph.section_index === null
        ? `Chapô, paragraphe ${(selectedArticleParagraph.paragraph_index ?? 0) + 1}`
        : `Section ${selectedArticleParagraph.section_index + 1}, §${
            (selectedArticleParagraph.paragraph_index ?? 0) + 1
          }${selectedSectionHeading === null ? "" : ` · ${selectedSectionHeading}`}`;
  const previewPdfUrl =
    draft === null
      ? null
      : analystOpinionPreviewPdfUrl(
          draft,
          draft.provenance.base_publication_sha256,
        );

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

      <div className="opinion-workspace">
        <div className="opinion-workspace__article">
          <ArticleReadingPane
            catalog={articleCatalog}
            isPending={articleAnchorsQuery.isPending}
            errorMessage={
              articleAnchorsQuery.isError
                ? describeApiError(articleAnchorsQuery.error).message
                : null
            }
            notes={notes}
            proposals={proposals}
            selectedAnchor={selectedAnchor}
            onSelectAnchor={changeAnchor}
          />
        </div>

        <div className="opinion-workspace__panel">
          <div role="tablist" aria-label="Panneau de la note de l’analyste">
            <button
              type="button"
              role="tab"
              id="opinion-tab-notes"
              aria-selected={activeTab === "notes"}
              aria-controls="opinion-tabpanel"
              onClick={() => setActiveTab("notes")}
            >
              Notes {notes.length}
            </button>
            <button
              type="button"
              role="tab"
              id="opinion-tab-proposals"
              aria-selected={activeTab === "proposals"}
              aria-controls="opinion-tabpanel"
              onClick={() => setActiveTab("proposals")}
            >
              Propositions {proposals.length}
            </button>
            <button
              type="button"
              role="tab"
              id="opinion-tab-opinion"
              aria-selected={activeTab === "opinion"}
              aria-controls="opinion-tabpanel"
              onClick={() => setActiveTab("opinion")}
            >
              Avis {draft === null ? "—" : `v${draft.version}`}
            </button>
            <button
              type="button"
              role="tab"
              id="opinion-tab-pdf"
              aria-selected={activeTab === "pdf"}
              aria-controls="opinion-tabpanel"
              onClick={() => setActiveTab("pdf")}
            >
              PDF
            </button>
          </div>

          <div
            id="opinion-tabpanel"
            role="tabpanel"
            aria-labelledby={`opinion-tab-${activeTab}`}
            className="opinion-workspace__tabpanel"
          >
            {activeTab === "notes" ? (
              <>
                <section
                  className="job-card opinion-target-context"
                  aria-label="Cible de la note"
                >
                  <h3>Cible sélectionnée</h3>
                  <p>{selectedTargetLabel}</p>
                  {selectedArticleParagraph === null ? null : (
                    <>
                      <h4>Contexte</h4>
                      <p>{selectedArticleParagraph.text}</p>
                    </>
                  )}
                </section>

                <details className="opinion-notes__existing">
                  <summary>Notes existantes ({notes.length})</summary>
                  <NoteAnchorList
                    groups={groups}
                    selectedAnchor={selectedAnchor}
                    onSelectNote={selectNote}
                  />
                </details>

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
                  onRequested={(proposalId) => {
                    setSelectedProposalId(proposalId);
                    setActiveTab("proposals");
                  }}
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
              </>
            ) : null}

            {activeTab === "proposals" ? (
              <>
                <ProposalList
                  proposals={proposals}
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
              </>
            ) : null}

            {activeTab === "opinion" ? (
              <>
                {draft === null ? (
                  <section className="job-card" aria-label="Aperçu du rendu">
                    <h3>Aperçu du rendu de l’avis courant</h3>
                    <p>
                      Aucun avis n’est encore composé. Une proposition Bridge
                      peut être demandée puis acceptée ; l’avis sera alors créé
                      par le serveur.
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
                  citationViolations={
                    opinionQuery.data?.citation_violations ?? []
                  }
                  notes={notes}
                />

                <HistoryPanel history={opinionQuery.data?.history ?? []} />
              </>
            ) : null}

            {activeTab === "pdf" ? (
              previewPdfUrl === null ? (
                <p>Aucun aperçu PDF n’est disponible sans avis courant.</p>
              ) : (
                <section
                  className="job-card"
                  aria-labelledby="opinion-pdf-heading"
                >
                  <h3 id="opinion-pdf-heading">
                    Aperçu PDF réel de l’avis v{draft?.version}
                  </h3>
                  <p>
                    <a href={previewPdfUrl} target="_blank" rel="noreferrer">
                      Ouvrir le PDF dans un nouvel onglet
                    </a>
                  </p>
                  <iframe
                    title="Aperçu PDF réel de l’avis"
                    src={previewPdfUrl}
                    width="100%"
                    height="720"
                  >
                    Votre navigateur ne peut pas afficher l’aperçu PDF.
                  </iframe>
                </section>
              )
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}
