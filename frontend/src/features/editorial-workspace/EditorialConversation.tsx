import { useEffect, useRef } from "react";

import { EditorialConversationComposer } from "./EditorialConversationComposer";
import { EditorialConversationStatus } from "./EditorialConversationStatus";
import { EditorialConversationTimeline } from "./EditorialConversationTimeline";
import { EditorialProposalCard } from "./EditorialProposalCard";
import { useEditorialConversationDrafts } from "./useEditorialConversationDrafts";
import { useEditorialThreadLifecycle } from "./useEditorialThreadLifecycle";

interface EditorialConversationProps {
  subjectId: string;
  occurrenceId: string;
  snapshotId: string;
  targetLabel: string;
  onChangeTarget: () => void;
}

export function EditorialConversation({
  subjectId,
  occurrenceId,
  snapshotId,
  targetLabel,
  onChangeTarget,
}: EditorialConversationProps) {
  const drafts = useEditorialConversationDrafts(occurrenceId);
  const lifecycle = useEditorialThreadLifecycle({
    subjectId,
    occurrenceId,
    snapshotId,
    onMessageSent: drafts.clearDraft,
  });
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const bridgeStatusRef = useRef<HTMLParagraphElement>(null);
  const sendAnnouncementRef = useRef<HTMLParagraphElement>(null);
  const focusAfterSubmitRef = useRef<string | null>(null);
  const restoreComposerFocusRef = useRef<string | null>(null);
  const controlsWereLockedRef = useRef(false);
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
    if (lifecycle.submissionUncertain && lifecycle.controlsLocked) {
      bridgeStatusRef.current?.focus({ preventScroll: true });
      return;
    }

    if (focusAfterSubmitRef.current === occurrenceId) {
      focusAfterSubmitRef.current = null;
      restoreComposerFocusRef.current = occurrenceId;
      if (lifecycle.controlsLocked) {
        sendAnnouncementRef.current?.focus({ preventScroll: true });
        controlsWereLockedRef.current = true;
      } else {
        restoreComposerFocusRef.current = null;
        composerRef.current?.focus({ preventScroll: true });
      }
      return;
    }

    if (controlsWereLockedRef.current && !lifecycle.controlsLocked) {
      controlsWereLockedRef.current = false;
      const restoreOccurrenceId = restoreComposerFocusRef.current;
      restoreComposerFocusRef.current = null;
      if (restoreOccurrenceId === occurrenceId) {
        composerRef.current?.focus({ preventScroll: true });
      }
      return;
    }

    if (lifecycle.controlsLocked) controlsWereLockedRef.current = true;
  }, [lifecycle.controlsLocked, lifecycle.submissionUncertain, occurrenceId]);

  function submitMessage() {
    if (lifecycle.submitMessage(drafts.draft))
      focusAfterSubmitRef.current = occurrenceId;
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
          Discussion sur : {targetLabel}
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
        bridgeStatusRef={bridgeStatusRef}
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
          {proposalIds.map((proposalId) => (
            <EditorialProposalCard
              key={proposalId}
              subjectId={subjectId}
              occurrenceId={occurrenceId}
              proposalId={proposalId}
            />
          ))}
        </div>
      ) : null}
      {lifecycle.canCompose ? (
        <EditorialConversationComposer
          draft={drafts.draft}
          active={lifecycle.active}
          controlsLocked={lifecycle.controlsLocked}
          hasQueryError={lifecycle.threadQuery.isError}
          sendPending={lifecycle.sendState.isPending}
          sendSuccess={lifecycle.sendState.isSuccess}
          sendErrorMessage={
            lifecycle.sendState.isError
              ? lifecycle.sendState.errorMessage
              : null
          }
          submissionBelongsToCurrentTarget={
            lifecycle.submissionBelongsToCurrentTarget
          }
          composerRef={composerRef}
          sendAnnouncementRef={sendAnnouncementRef}
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
      ) : null}
    </section>
  );
}
