/** Decision-oriented overview for the canonical investigation projections. */

import { useState } from "react";

import {
  useInvestigation,
  type AnalystInvestigationStatus,
} from "../../../api/investigation";
import { useInvestigationActivity } from "../../../api/investigationOperator";
import { useInvestigationEnvelope } from "../../../api/investigationGovernance";
import { useAgentSessions } from "../api/agentSessions";
import {
  useBootstrapEnvelopeProposal,
  useBootstrapInventory,
  useBootstrapSnapshot,
} from "../api/bootstrap";
import { useContributions } from "../api/contributions";
import { useNoteProposals } from "../api/opinionProposals";
import { useTasks } from "../api/tasks";
import { toWorkshopApiError } from "../api/client";
import {
  useEnsureWorkspace,
  useWorkspaceConflicts,
  useWorkspaceStatus,
  type WorkspaceStatusV1,
} from "../api/workspace";
import { useWorkspaceAgentStatus } from "../api/workspaceAgent";
import {
  WorkspaceUnavailableState,
  WorkshopAgentAbsentState,
  WorkshopLoadingState,
  WorkshopStateBanner,
  WorkshopSyncingState,
  WorkshopTypedErrorState,
} from "../shell/WorkshopStates";
import { SESSION_STATE_LABELS, formatTimestamp } from "../tasks/labels";
import { BootstrapSection } from "./BootstrapSection";
import { EnvelopeLifecycleSection } from "./EnvelopeLifecycleSection";
import { EnvelopeSection } from "./EnvelopeSection";
import {
  AGENT_STATUS_LABELS,
  deriveOverviewDecisions,
  isActiveAgentSession,
  isActiveTask,
  PLAN_STATE_LABELS,
  redactTechnicalIdentifiers,
  WORKSPACE_AVAILABILITY_LABELS,
  formatCount,
  verificationLabel,
} from "./overviewPresentation";

const INVESTIGATION_STATUS_LABELS: Record<AnalystInvestigationStatus, string> =
  {
    queued: "En attente",
    running: "En cours",
    awaiting_review: "À réviser",
    completed: "Terminée",
    exhausted: "Budget atteint",
    failed: "Interrompue",
    cancelled: "Annulée",
  };

const ADAPTER_LABELS = {
  codex_cli: "Codex CLI",
  claude_code: "Claude Code",
} as const;

export function OverviewView({
  investigationId,
  subjectId,
}: {
  investigationId: string;
  subjectId: string;
}) {
  const [g2DetailsOpen, setG2DetailsOpen] = useState(false);
  const investigationQuery = useInvestigation(investigationId);
  const activityQuery = useInvestigationActivity(investigationId);
  const agentQuery = useWorkspaceAgentStatus();
  const workspaceQuery = useWorkspaceStatus(investigationId);
  const ensureWorkspace = useEnsureWorkspace(investigationId);
  const tasksQuery = useTasks(investigationId);
  const sessionsQuery = useAgentSessions(investigationId);
  const proposalsQuery = useNoteProposals(investigationId);
  const conflictsQuery = useWorkspaceConflicts(investigationId);
  const contributionsQuery = useContributions(investigationId);
  const inventoryQuery = useBootstrapInventory(investigationId);
  const snapshotQuery = useBootstrapSnapshot(investigationId);
  const bootstrapEnvelopeQuery = useBootstrapEnvelopeProposal(investigationId);
  const envelopeQuery = useInvestigationEnvelope(investigationId);
  const workspace = workspaceQuery.data;
  const workspaceReady =
    workspace?.workspace_state === "bound" && workspace.workspace_available;
  const agentAvailable = agentQuery.data?.status === "available";
  const tasks = tasksQuery.data?.items ?? [];
  const sessions = sessionsQuery.data ?? [];
  const proposals = proposalsQuery.data ?? [];
  const conflicts = conflictsQuery.data?.conflicts ?? [];
  const contributions = contributionsQuery.data ?? [];
  const investigation = investigationQuery.data;
  const latestActivity = [...(activityQuery.data?.timeline ?? [])].sort(
    (left, right) => right.updated_at.localeCompare(left.updated_at),
  )[0];
  const activeSessions = sessions.filter(isActiveAgentSession);
  const activeTasks = tasks.filter(isActiveTask);
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
    workspace: workspace ?? null,
    envelopeProposal: bootstrapEnvelopeQuery.data ?? null,
    envelope: envelopeQuery.data
      ? {
          approved: envelopeQuery.data.approved,
          currently_authorized:
            "currently_authorized" in envelopeQuery.data &&
            envelopeQuery.data.currently_authorized === true,
        }
      : null,
    workspaceViewsAvailable:
      investigationQuery.data?.prose_authority === "bridge_only",
  });
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  const articleTitle = workspace?.binding?.article_slug
    ? `Article ${redactTechnicalIdentifiers(workspace.binding.article_slug)}`
    : "Article de l’enquête";
  const currentStatus =
    activityQuery.data?.investigation.status ?? investigation?.status;
  const investigationStatus = currentStatus
    ? INVESTIGATION_STATUS_LABELS[currentStatus]
    : "Indisponible";
  const latestEventAt =
    latestActivity?.updated_at ?? activityQuery.data?.last_activity_at;
  const latestEventLabel =
    latestActivity?.activity_label ??
    activityQuery.data?.last_activity_source ??
    "Aucun événement consigné";

  return (
    <section
      className="workshop-overview"
      aria-label="Vue d’ensemble de l’enquête"
    >
      <header className="job-card" aria-labelledby="overview-article-heading">
        <p>ARTICLE</p>
        <h2 id="overview-article-heading">{articleTitle}</h2>
        <p>
          Statut : <strong>{investigationStatus}</strong>
        </p>
        <p>
          Dernier événement :{" "}
          <strong>{redactTechnicalIdentifiers(latestEventLabel)}</strong>
          {latestEventAt ? (
            <>
              {" · "}
              <time dateTime={latestEventAt}>
                {formatTimestamp(latestEventAt)}
              </time>
            </>
          ) : null}
        </p>
      </header>

      <section className="job-card" aria-label="Indicateurs de l’enquête">
        <h2>Indicateurs</h2>
        <dl className="workshop-meta">
          <dt>Décisions requises</dt>
          <dd>{decisionsLoaded ? formatCount(decisions.length) : "—"}</dd>
          <dt>Agents en cours</dt>
          <dd>
            {sessionsQuery.data ? formatCount(activeSessions.length) : "—"}
          </dd>
          <dt>Tâches actives</dt>
          <dd>{tasksQuery.data ? formatCount(activeTasks.length) : "—"}</dd>
          <dt>Résultats nouveaux</dt>
          <dd>
            {contributionsQuery.data ? formatCount(contributions.length) : "—"}
          </dd>
        </dl>
      </section>

      <section
        className="job-card"
        aria-labelledby="overview-decisions-heading"
      >
        <h2 id="overview-decisions-heading">À traiter maintenant</h2>
        {decisions.length === 0 ? (
          <p>
            {decisionsLoaded
              ? "Aucune décision ou vérification en attente dans les données chargées."
              : decisionsPending
                ? "Chargement des décisions en attente…"
                : "État des décisions indisponible."}
          </p>
        ) : (
          <ul>
            {decisions.map((decision) => (
              <li key={decision.key}>
                <strong>{decision.label}</strong> —{" "}
                {redactTechnicalIdentifiers(decision.detail)}{" "}
                {decision.href ? (
                  <a
                    href={decision.href}
                    onClick={() => {
                      if (decision.href === "#overview-g2-details")
                        setG2DetailsOpen(true);
                    }}
                  >
                    Ouvrir
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="job-card" aria-labelledby="overview-progress-heading">
        <h2 id="overview-progress-heading">Progression de l’enquête</h2>
        <dl className="workshop-meta">
          <dt>Collecte IOC</dt>
          <dd>
            {inventoryQuery.data
              ? `${formatCount(inventoryQuery.data.seeds.length + inventoryQuery.data.discovered.length)} IOC collecté(s) · ${formatCount(inventoryQuery.data.seeds.length)} graine(s), ${formatCount(inventoryQuery.data.discovered.length)} découvert(s)`
              : inventoryQuery.isPending
                ? "Chargement"
                : "Indisponible"}
          </dd>
          <dt>Couverture VT</dt>
          <dd>
            {snapshotQuery.data
              ? `${formatCount(snapshotQuery.data.limits.operations_recorded)} opération(s) sur ${formatCount(snapshotQuery.data.limits.max_operations)} · ${PLAN_STATE_LABELS[snapshotQuery.data.plan_state]}`
              : snapshotQuery.isPending
                ? "Chargement"
                : "Indisponible"}
          </dd>
          <dt>Plan / tâches</dt>
          <dd>
            {workspace
              ? tasksQuery.data
                ? `${formatCount(workspace.counts.plans)} plan(s) · ${formatCount(activeTasks.length)} tâche(s) active(s) sur ${formatCount(tasks.length)}`
                : `${formatCount(workspace.counts.plans)} plan(s) · ${formatCount(workspace.counts.tasks)} tâche(s) · états indisponibles`
              : "Indisponible"}
          </dd>
          <dt>Findings</dt>
          <dd>
            {investigation
              ? formatCount(investigation.findings.length)
              : workspace
                ? formatCount(workspace.counts.findings)
                : "Indisponible"}
          </dd>
        </dl>
        <BootstrapSection investigationId={investigationId} />
      </section>

      <section
        className="job-card workshop-overview-activity"
        aria-labelledby="overview-activity-heading"
      >
        <h2 id="overview-activity-heading">Activité récente</h2>
        <section aria-labelledby="overview-active-agents-heading">
          <h3 id="overview-active-agents-heading">Agents en cours</h3>
          {!sessionsQuery.data ? (
            <p>
              {sessionsQuery.isPending
                ? "Chargement des sessions agent…"
                : "Sessions agent indisponibles."}
            </p>
          ) : activeSessions.length === 0 ? (
            <p>Aucun agent en cours.</p>
          ) : (
            <ul>
              {activeSessions.map((session) => {
                const task = taskById.get(session.task_id);
                return (
                  <li key={session.id}>
                    <strong>{ADAPTER_LABELS[session.adapter_kind]}</strong> ·{" "}
                    {task
                      ? redactTechnicalIdentifiers(task.title)
                      : "Tâche indisponible"}{" "}
                    · {SESSION_STATE_LABELS[session.state]} · dernière activité{" "}
                    {session.last_activity_at ? (
                      <time dateTime={session.last_activity_at}>
                        {formatTimestamp(session.last_activity_at)}
                      </time>
                    ) : (
                      " non déclarée"
                    )}{" "}
                    {task &&
                    investigationQuery.data?.prose_authority ===
                      "bridge_only" ? (
                      <a href={`?vue=plan&task=${encodeURIComponent(task.id)}`}>
                        Ouvrir la tâche
                      </a>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
        <section aria-labelledby="overview-new-results-heading">
          <h3 id="overview-new-results-heading">Résultats nouveaux</h3>
          {!contributionsQuery.data ? (
            <p>
              {contributionsQuery.isPending
                ? "Chargement des résultats…"
                : "Résultats indisponibles."}
            </p>
          ) : contributions.length === 0 ? (
            <p>Aucun résultat récent n’est chargé.</p>
          ) : (
            <ul>
              {[...contributions]
                .sort((left, right) =>
                  right.created_at.localeCompare(left.created_at),
                )
                .slice(0, 10)
                .map((contribution) => {
                  const task = contribution.task_id
                    ? taskById.get(contribution.task_id)
                    : undefined;
                  return (
                    <li key={contribution.id}>
                      <strong>
                        {contributionTypeLabel(contribution.category)}
                      </strong>{" "}
                      · source{" "}
                      {contribution.origin === "human" ? "humaine" : "hôte"} ·{" "}
                      {task
                        ? redactTechnicalIdentifiers(task.title)
                        : "Sans tâche associée"}{" "}
                      · {verificationLabel(task)} ·{" "}
                      <time dateTime={contribution.created_at}>
                        {formatTimestamp(contribution.created_at)}
                      </time>
                    </li>
                  );
                })}
            </ul>
          )}
          {contributions.length > 10 ? (
            <p role="note">
              Affichage limité aux dix résultats les plus récents.
            </p>
          ) : null}
        </section>
        <section aria-labelledby="overview-recent-events-heading">
          <h3 id="overview-recent-events-heading">Événements récents</h3>
          {(activityQuery.data?.timeline.length ?? 0) === 0 ? (
            <p>Aucun événement récent n’est disponible.</p>
          ) : (
            <ol>
              {[...(activityQuery.data?.timeline ?? [])]
                .sort((left, right) =>
                  right.updated_at.localeCompare(left.updated_at),
                )
                .slice(0, 5)
                .map((event) => (
                  <li key={`${event.id}-${event.version}`}>
                    {redactTechnicalIdentifiers(event.activity_label)} ·{" "}
                    {event.status} ·{" "}
                    <time dateTime={event.updated_at}>
                      {formatTimestamp(event.updated_at)}
                    </time>
                  </li>
                ))}
            </ol>
          )}
        </section>
      </section>

      <details className="job-card" aria-label="Détails techniques">
        <summary>Détails techniques</summary>
        <section aria-labelledby="overview-host-heading">
          <h2 id="overview-host-heading">Hôte et workspace</h2>
          <dl className="workshop-meta">
            <dt>UUID enquête</dt>
            <dd>
              <code>{investigationId}</code>
            </dd>
            <dt>UUID sujet</dt>
            <dd>
              <code>{subjectId}</code>
            </dd>
          </dl>
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
            agentAvailable={agentAvailable}
            ensurePending={ensureWorkspace.isPending}
            ensureError={ensureWorkspace.error}
            onEnsure={() => ensureWorkspace.mutate()}
          />
        </section>
      </details>

      <details
        id="overview-g2-details"
        className="job-card workshop-overview-g2"
        open={g2DetailsOpen}
        onToggle={(event) => setG2DetailsOpen(event.currentTarget.open)}
      >
        <summary>Autorisations / budgets G2</summary>
        <EnvelopeSection
          investigationId={investigationId}
          workspaceReady={workspaceReady}
          workspaceBlockedReason={workspaceBlockedReason(workspaceQuery)}
        />
        <EnvelopeLifecycleSection investigationId={investigationId} />
      </details>
    </section>
  );
}

function contributionTypeLabel(
  category: "work_contribution" | "observation" | "finding",
): string {
  switch (category) {
    case "work_contribution":
      return "Contribution de travail";
    case "observation":
      return "Observation";
    case "finding":
      return "Finding";
  }
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
  if (!query.data.workspace_available)
    return (
      query.data.availability_reason ??
      WORKSPACE_AVAILABILITY_LABELS[query.data.availability_code]
    );
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
        <dd>
          {instanceId === null ? "Non déclarée" : <code>{instanceId}</code>}
        </dd>
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
  agentAvailable,
  ensurePending,
  ensureError,
  onEnsure,
}: {
  query: ReturnType<typeof useWorkspaceStatus>;
  workspace: WorkspaceStatusV1 | null;
  agentAvailable: boolean;
  ensurePending: boolean;
  ensureError: Error | null;
  onEnsure: () => void;
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
    if (!agentAvailable) return <WorkspaceUnavailableState absent />;
    return (
      <>
        <WorkspaceUnavailableState
          absent
          onCreate={onEnsure}
          createPending={ensurePending}
        />
        {ensureError ? (
          <WorkshopTypedErrorState
            error={toWorkshopApiError(ensureError)}
            title="Création du workspace refusée"
          />
        ) : null}
      </>
    );
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
          {workspace.binding === null ? (
            <code>Non lié</code>
          ) : (
            <code>{workspace.binding.workspace_id}</code>
          )}
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
              {formatTimestamp(workspace.lastly_observed_at)}
            </time>
          ) : (
            "Aucune"
          )}
        </dd>
      </dl>
    </section>
  );
}
