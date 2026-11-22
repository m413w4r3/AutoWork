/**
 * Vue d’ensemble du cockpit M24 : hôte hôte/workspace, bootstrap et enveloppe
 * G2 initiale. Aucune action n’est proposée hors de son droit réel : la
 * décision d’enveloppe dépend de l’état du workspace et de la proposition
 * courante.
 */

import { toWorkshopApiError } from "../api/client";
import { useWorkspaceStatus, type WorkspaceStatusV1 } from "../api/workspace";
import { useWorkspaceAgentStatus } from "../api/workspaceAgent";
import {
  WorkspaceUnavailableState,
  WorkshopAgentAbsentState,
  WorkshopLoadingState,
  WorkshopStateBanner,
  WorkshopSyncingState,
  WorkshopTypedErrorState,
} from "../shell/WorkshopStates";
import { BootstrapSection } from "./BootstrapSection";
import { EnvelopeSection } from "./EnvelopeSection";
import {
  AGENT_STATUS_LABELS,
  WORKSPACE_AVAILABILITY_LABELS,
  formatCount,
} from "./overviewPresentation";

export function OverviewView({
  investigationId,
  subjectId,
}: {
  investigationId: string;
  subjectId: string;
}) {
  const agentQuery = useWorkspaceAgentStatus();
  const workspaceQuery = useWorkspaceStatus(investigationId);
  const workspace = workspaceQuery.data;
  const workspaceReady = workspace?.workspace_state === "bound";

  return (
    <section
      className="workshop-overview"
      aria-label="Vue d’ensemble de l’enquête"
    >
      <section className="job-card" aria-labelledby="overview-host-heading">
        <h2 id="overview-host-heading">Hôte et workspace</h2>
        <p>
          Enquête <code>{investigationId}</code> · sujet{" "}
          <code>{subjectId}</code>
        </p>
        <AgentStatusPanel
          status={agentQuery.data?.status}
          instanceId={agentQuery.data?.instance_id ?? null}
          isPending={agentQuery.isPending}
          error={agentQuery.isError ? agentQuery.error : null}
          onRetry={() => void agentQuery.refetch()}
        />
        <WorkspaceStatusPanel
          query={workspaceQuery}
          workspace={workspace ?? null}
        />
      </section>

      <BootstrapSection investigationId={investigationId} />

      <EnvelopeSection
        investigationId={investigationId}
        workspaceReady={workspaceReady}
        workspaceBlockedReason={workspaceBlockedReason(workspaceQuery)}
      />
    </section>
  );
}

function workspaceBlockedReason(
  query: ReturnType<typeof useWorkspaceStatus>,
): string | null {
  if (query.isPending)
    return "L’état du workspace local est en cours de vérification : la décision G2 reste bloquée tant qu’il n’est pas lié.";
  if (query.isError)
    return "L’état du workspace local est indisponible : la décision G2 reste bloquée.";
  if (!query.data)
    return "L’état du workspace local est inconnu : la décision G2 reste bloquée.";
  if (query.data.workspace_state !== "bound")
    return "Le workspace local doit être créé avant toute décision G2.";
  return null;
}

function AgentStatusPanel({
  status,
  instanceId,
  isPending,
  error,
  onRetry,
}: {
  status: string | undefined;
  instanceId: string | null;
  isPending: boolean;
  error: Error | null;
  onRetry: () => void;
}) {
  if (isPending)
    return <WorkshopLoadingState label="Vérification de l’agent hôte…" />;
  if (error)
    return (
      <WorkshopTypedErrorState
        error={toWorkshopApiError(error)}
        title="État de l’agent hôte indisponible"
        onRetry={onRetry}
      />
    );
  if (!status)
    return (
      <WorkshopStateBanner tone="info" title="Agent hôte inconnu">
        <p>Le statut de l’agent hôte n’est pas encore disponible.</p>
      </WorkshopStateBanner>
    );
  const label =
    status in AGENT_STATUS_LABELS
      ? AGENT_STATUS_LABELS[status as keyof typeof AGENT_STATUS_LABELS]
      : status;
  return (
    <>
      <dl className="workshop-meta">
        <dt>Agent hôte</dt>
        <dd>{label}</dd>
        <dt>Instance</dt>
        <dd>{instanceId ?? "Non déclarée"}</dd>
      </dl>
      {status !== "available" ? (
        <WorkshopAgentAbsentState status={label} />
      ) : null}
    </>
  );
}

function WorkspaceStatusPanel({
  query,
  workspace,
}: {
  query: ReturnType<typeof useWorkspaceStatus>;
  workspace: WorkspaceStatusV1 | null;
}) {
  if (query.isPending)
    return <WorkshopLoadingState label="Chargement du workspace local…" />;
  if (query.isError)
    return (
      <WorkshopTypedErrorState
        error={toWorkshopApiError(query.error)}
        title="Workspace local indisponible"
        onRetry={() => void query.refetch()}
      />
    );
  if (!workspace) return null;
  if (workspace.workspace_state !== "bound") {
    return <WorkspaceUnavailableState absent />;
  }
  if (!workspace.workspace_available) {
    return (
      <WorkspaceUnavailableState
        reason={
          workspace.availability_reason ??
          WORKSPACE_AVAILABILITY_LABELS[workspace.availability_code]
        }
      />
    );
  }
  return (
    <section aria-label="État du workspace">
      {query.isFetching ? <WorkshopSyncingState /> : null}
      <dl className="workshop-meta">
        <dt>Livraison</dt>
        <dd>{workspace.binding?.delivery_code ?? "Non déclarée"}</dd>
        <dt>Article</dt>
        <dd>{workspace.binding?.article_slug ?? "Non déclaré"}</dd>
        <dt>Workspace</dt>
        <dd>
          <code>{workspace.binding?.workspace_id ?? "Non lié"}</code>
        </dd>
        <dt>Génération / curseur</dt>
        <dd>
          {workspace.generation ?? "inconnue"} / {workspace.cursor ?? "inconnu"}
        </dd>
        <dt>Contenus</dt>
        <dd>
          {formatCount(workspace.counts.files)} fichier(s) ·{" "}
          {formatCount(workspace.counts.human_notes)} note(s) humaine(s) ·{" "}
          {formatCount(workspace.counts.unresolved_conflicts)} conflit(s) non
          résolu(s) · {formatCount(workspace.counts.policy_violations)}{" "}
          violation(s) de politique
        </dd>
        <dt>Suivi</dt>
        <dd>
          {formatCount(workspace.counts.plans)} plan(s) ·{" "}
          {formatCount(workspace.counts.tasks)} tâche(s) ·{" "}
          {formatCount(workspace.counts.findings)} finding(s)
        </dd>
        <dt>Dernière observation</dt>
        <dd>
          {workspace.lastly_observed_at ? (
            <time dateTime={workspace.lastly_observed_at}>
              {workspace.lastly_observed_at}
            </time>
          ) : (
            "Aucune"
          )}
        </dd>
      </dl>
    </section>
  );
}
