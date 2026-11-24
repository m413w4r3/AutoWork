/** Shared pending-decision projections for the overview and context bar. */

import { useInvestigationEnvelope } from "../../../api/investigationGovernance";
import { envelopeAccessState } from "./envelopeAccess";
import { useBootstrapEnvelopeProposal } from "../api/bootstrap";
import { useNoteProposals } from "../api/opinionProposals";
import { useTasks } from "../api/tasks";
import { useWorkspaceConflicts, useWorkspaceStatus } from "../api/workspace";
import { deriveOverviewDecisions } from "./overviewPresentation";

export function usePendingDecisions(
  investigationId: string,
  options: {
    enabled?: boolean;
    workspaceViewsAvailable?: boolean;
  } = {},
) {
  const enabled = options.enabled ?? investigationId !== "";
  const tasksQuery = useTasks(investigationId, { enabled });
  const proposalsQuery = useNoteProposals(investigationId, undefined, {
    enabled,
  });
  const conflictsQuery = useWorkspaceConflicts(investigationId, { enabled });
  const workspaceQuery = useWorkspaceStatus(investigationId, { enabled });
  const bootstrapEnvelopeQuery = useBootstrapEnvelopeProposal(investigationId, {
    enabled,
  });
  const envelopeQuery = useInvestigationEnvelope(
    enabled ? investigationId : null,
  );

  const tasks = tasksQuery.data?.items ?? [];
  const proposals = proposalsQuery.data ?? [];
  const conflicts = conflictsQuery.data?.conflicts ?? [];
  const decisionsLoaded =
    tasksQuery.data !== undefined &&
    proposalsQuery.data !== undefined &&
    conflictsQuery.data !== undefined &&
    workspaceQuery.data !== undefined &&
    bootstrapEnvelopeQuery.data !== undefined &&
    envelopeQuery.data !== undefined;
  const decisionsPending =
    tasksQuery.isPending ||
    proposalsQuery.isPending ||
    conflictsQuery.isPending ||
    workspaceQuery.isPending ||
    bootstrapEnvelopeQuery.isPending ||
    envelopeQuery.isPending;
  const decisions = deriveOverviewDecisions({
    tasks,
    proposals,
    conflicts,
    workspace: workspaceQuery.data ?? null,
    envelopeProposal: bootstrapEnvelopeQuery.data ?? null,
    envelopeAccessKind: envelopeQuery.data
      ? envelopeAccessState(envelopeQuery.data).kind
      : null,
    workspaceViewsAvailable: options.workspaceViewsAvailable,
  });

  return {
    tasksQuery,
    workspaceQuery,
    tasks,
    decisions,
    decisionsLoaded,
    decisionsPending,
    pendingDecisionCount: decisionsLoaded ? decisions.length : undefined,
  };
}
