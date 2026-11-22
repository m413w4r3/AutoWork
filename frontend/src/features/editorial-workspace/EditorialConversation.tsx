import { useCallback, useEffect, useRef, useState } from "react";

import { EditorialConversationComposer } from "./EditorialConversationComposer";
import { EditorialConversationStatus } from "./EditorialConversationStatus";
import { EditorialConversationTimeline } from "./EditorialConversationTimeline";
import { EditorialProposalCard } from "./EditorialProposalCard";
import { useEditorialConversationDrafts } from "./useEditorialConversationDrafts";
import { useEditorialThreadLifecycle } from "./useEditorialThreadLifecycle";
import {
  isVisualElementBlock,
  visualBridgeOperation,
  visualElementKindLabel,
  visualElementTitle,
} from "./visualElementOperations";
import { visualInstructionChips } from "./visualInstructionChips";
import { useVisualWorkspaceContext } from "./visualWorkspaceContext";
import type { EditorialProposalState } from "../../api/editorialWorkspace";

interface EditorialConversationProps {
  subjectId: string;
  occurrenceId: string;
  snapshotId: string;
  targetLabel: string;
  allowedOperations?: readonly string[];
  proposalPrompt?: string | null;
  onProposalPromptApplied?: () => void;
  onChangeTarget: () => void;
  onPendingProposalsChange?: (pending: boolean) => void;
}

export function EditorialConversation({
  subjectId,
  occurrenceId,
  snapshotId,
  targetLabel,
  allowedOperations = [],
  proposalPrompt = null,
  onProposalPromptApplied,
  onChangeTarget,
  onPendingProposalsChange,
}: EditorialConversationProps) {
  const drafts = useEditorialConversationDrafts(occurrenceId);
  const visualContext = useVisualWorkspaceContext();
  const currentBlock =
    visualContext?.snapshot.blocks.find(
      (block) => block.block_id === occurrenceId,
    ) ?? null;
  const visualBlock =
    currentBlock && isVisualElementBlock(currentBlock) ? currentBlock : null;
  const visualKind = visualBlock?.element_kind ?? null;
  const canProposeVisual = Boolean(
    visualKind && allowedOperations.includes(visualBridgeOperation(visualKind)),
  );
  const discussionTargetLabel =
    visualBlock && visualKind
      ? `${visualElementTitle(visualBlock)} — ${visualElementKindLabel(visualBlock)}`
      : targetLabel;
  const [proposalStates, setProposalStates] = useState<
    Record<string, EditorialProposalState>
  >({});
  const reportProposalState = useCallback(
    (proposalId: string, state: EditorialProposalState) => {
      setProposalStates((current) =>
        current[proposalId] === state
          ? current
          : { ...current, [proposalId]: state },
      );
    },
    [],
  );
  useEffect(() => {
    setProposalStates({});
  }, [occurrenceId]);
  useEffect(() => {
    if (!onPendingProposalsChange) return;
    onPendingProposalsChange(
      Object.values(proposalStates).some(
        (state) =>
          state === "preparing" ||
          state === "proposed" ||
          state === "needs_review",
      ),
    );
  }, [onPendingProposalsChange, proposalStates]);
  const lifecycle = useEditorialThreadLifecycle({
    subjectId,
    occurrenceId,
    snapshotId,
    onMessageSent: drafts.clearDraft,
  });
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const appliedProposalPromptRef = useRef<string | null>(null);
  const proposalIds =
    lifecycle.threadQuery.data && lifecycle.threadQuery.data.kind !== "unopened"
      ? Array.from(
          new Set(
            lifecycle.threadQuery.data.timeline.flatMap((event) =>
              event.proposal_id ? [event.proposal_id] : [],
            ),
          ),
        )
      : [];

  useEffect(() => {
    if (!proposalPrompt) {
      appliedProposalPromptRef.current = null;
      return;
    }
    if (appliedProposalPromptRef.current === proposalPrompt) return;
    appliedProposalPromptRef.current = proposalPrompt;
    drafts.setDraft(proposalPrompt);
    lifecycle.clearSendResult();
    composerRef.current?.focus({ preventScroll: true });
    onProposalPromptApplied?.();
  }, [drafts, lifecycle, onProposalPromptApplied, proposalPrompt]);

  function submitMessage(kind: "message" | "propose") {
    if (
      lifecycle.submitMessage(drafts.draft, kind) &&
      document.activeElement !== composerRef.current
    ) {
      composerRef.current?.focus({ preventScroll: true });
    }
  }

  function requestVariant() {
    if (!drafts.draft.trim()) {
      drafts.setDraft("Propose une variante à partir de la version actuelle.");
    }
    lifecycle.clearSendResult();
    composerRef.current?.focus({ preventScroll: true });
  }

  return (
    <section
      className="editorial-conversation"
      aria-labelledby="editorial-conversation-heading"
      aria-busy={lifecycle.threadQuery.isPending}
    >
      <h3 id="editorial-conversation-heading">Discussion</h3>
      <div className="editorial-conversation__target">
        <p className="editorial-conversation__target-label">
          Discussion sur : {discussionTargetLabel}
        </p>
        <button
          className="editorial-conversation__change-target"
          type="button"
          onClick={onChangeTarget}
        >
          Changer de cible
        </button>
      </div>
      <EditorialConversationStatus
        data={lifecycle.threadQuery.data}
        isPending={lifecycle.threadQuery.isPending}
        queryErrorMessage={
          lifecycle.threadQuery.isError
            ? lifecycle.threadQuery.error.message
            : null
        }
        reconcile={lifecycle.reconcileState}
        startSession={lifecycle.startSessionState}
        onReconcile={lifecycle.reconcile}
        onStartSession={lifecycle.startNewSession}
      />
      {lifecycle.threadQuery.data &&
      lifecycle.threadQuery.data.kind !== "unopened" ? (
        <EditorialConversationTimeline
          events={lifecycle.threadQuery.data.timeline}
        />
      ) : null}
      {proposalIds.length > 0 ? (
        <div className="editorial-conversation__proposals">
          {proposalIds.map((proposalId, index) => (
            <EditorialProposalCard
              key={proposalId}
              subjectId={subjectId}
              occurrenceId={occurrenceId}
              proposalId={proposalId}
              referenceOrdinal={index + 1}
              anchorText={discussionTargetLabel}
              onRequestVariant={
                lifecycle.canCompose ? requestVariant : undefined
              }
              onStateChange={reportProposalState}
            />
          ))}
        </div>
      ) : null}
      {lifecycle.canCompose ? (
        <>
          {visualKind && canProposeVisual ? (
            <div
              className="visual-instruction-chips"
              role="group"
              aria-label="Consignes de correction du visuel"
            >
              <p className="visual-instruction-chips__help">
                Consignes de correction : elles préremplissent le message
                modifiable et n’envoient rien.
              </p>
              <div className="visual-instruction-chips__list">
                {visualInstructionChips(visualKind).map((chip) => (
                  <button
                    key={chip.label}
                    type="button"
                    disabled={lifecycle.controlsLocked || lifecycle.active}
                    onClick={() => {
                      drafts.setDraft(chip.instruction);
                      lifecycle.clearSendResult();
                      composerRef.current?.focus({ preventScroll: true });
                    }}
                  >
                    {chip.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          <EditorialConversationComposer
            draft={drafts.draft}
            active={lifecycle.active}
            canPropose={
              canProposeVisual ||
              allowedOperations.some(
                (operation) =>
                  operation === "rewrite_title" ||
                  operation === "rewrite_paragraph" ||
                  operation === "create_table",
              )
            }
            proposalLabel={
              allowedOperations.includes("create_table")
                ? "Demander au Bridge"
                : undefined
            }
            controlsLocked={lifecycle.controlsLocked}
            hasQueryError={lifecycle.threadQuery.isError}
            sendPending={lifecycle.sendState.isPending}
            sendSuccess={lifecycle.sendState.isSuccess}
            sendKind={lifecycle.submissionKind}
            sendErrorMessage={
              lifecycle.sendState.isError
                ? lifecycle.sendState.errorMessage
                : null
            }
            submissionBelongsToCurrentTarget={
              lifecycle.submissionBelongsToCurrentTarget
            }
            composerRef={composerRef}
            onDraftChange={(value) => {
              drafts.setDraft(value);
              lifecycle.clearSendResult();
            }}
            onSuggestion={(value) => {
              drafts.setDraft(value);
              composerRef.current?.focus();
            }}
            onSubmit={submitMessage}
          />
        </>
      ) : null}
    </section>
  );
}
