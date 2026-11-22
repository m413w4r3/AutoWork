/**
 * Rules for the explicit "open the operator session" action after a transfer.
 *
 * A transfer (or an assignment) creates a versioned handoff; it never starts an
 * agent by itself. The cockpit may offer the session only when the real
 * capability probe answers for the adapter and attests the loopback-isolated
 * CLI confinement the backend also enforces (`agentic_sessions.py
 * ::_require_network_attestation`). The executable is never typed by the human:
 * it is the PATH name the host itself resolves
 * (`sidecars/workspace_agent/sessions/adapters/{codex,claude}.py`), and the
 * open request echoes the value recorded by the probe. The capability payload
 * exposes no model alias, so the action never invents one.
 */

import type {
  AgentSessionCapabilitiesView,
  AgentSessionCapabilityTarget,
} from "../api/agentSessions";
import type { WorkshopApiError } from "../api/client";
import type { TaskView } from "../api/tasks";
import type { OperatorAdapterKind } from "../api/types";
import { describeTaskError } from "./errors";
import { ADAPTER_LABELS } from "./labels";

/**
 * The host resolves these names from PATH; they are the values the human would
 * pass to `POST /agent-sessions`, never a free-form path.
 */
export const ADAPTER_EXECUTABLES: Readonly<
  Record<OperatorAdapterKind, string>
> = {
  codex_cli: "codex",
  claude_code: "claude",
};

/** `investigation_transfers.py::_operator_actor_id` convention. */
export function operatorAdapterForTask(
  task: TaskView,
): OperatorAdapterKind | null {
  if (task.assigned_actor_kind !== "operator") return null;
  if (task.assigned_actor_id === "operator:codex_cli") return "codex_cli";
  if (task.assigned_actor_id === "operator:claude_code") return "claude_code";
  return null;
}

export interface SessionLaunchTarget {
  taskId: string;
  adapterKind: OperatorAdapterKind;
  handoffId: string;
  capability: AgentSessionCapabilityTarget;
}

/**
 * A launcher-eligible task: an operator owns it, the transfer left it
 * `assigned`, and its current handoff version is known. `ready` tasks are not
 * eligible (no destination handoff yet), and a task with an active or
 * resumable session is handled by that session instead.
 */
export function sessionLaunchTarget(
  task: TaskView,
): SessionLaunchTarget | null {
  if (task.status !== "assigned") return null;
  const adapterKind = operatorAdapterForTask(task);
  if (adapterKind === null || task.handoff_ref === null) return null;
  return {
    taskId: task.id,
    adapterKind,
    handoffId: task.handoff_ref,
    capability: {
      adapterKind,
      executable: ADAPTER_EXECUTABLES[adapterKind],
    },
  };
}

/** The exact attestation the backend requires before it opens a session. */
export function confinementAttested(
  view: AgentSessionCapabilitiesView,
): boolean {
  const confinement = view.confinement;
  return (
    view.available &&
    confinement !== null &&
    confinement.network_mode === "isolated_egress" &&
    confinement.loopback_isolated &&
    confinement.netns
  );
}

/** Why a 200 capability probe still does not authorize opening a session. */
export function confinementGap(view: AgentSessionCapabilitiesView): string {
  const confinement = view.confinement;
  if (confinement === null) {
    return "l’hôte n’atteste aucun confinement CLI";
  }
  const network = confinement.network_mode ?? "réseau non attesté";
  const loopback = confinement.loopback_isolated
    ? "loopback isolé"
    : "loopback non isolé";
  const netns = confinement.netns ? "netns prouvé" : "netns non prouvé";
  return `confinement non attesté (profil ${confinement.profile}, ${network}, ${loopback}, ${netns})`;
}

export function openSessionLabel(adapterKind: OperatorAdapterKind): string {
  return `Ouvrir la session ${ADAPTER_LABELS[adapterKind]}`;
}

/**
 * Typed refusals of the probe and of the open action, in French. The raw code
 * stays visible for codes this cockpit does not know yet.
 */
export function describeSessionRefusal(error: WorkshopApiError): string {
  switch (error.code) {
    case "agent_session_external_llm_not_allowed": {
      const reason = error.details?.["reason"];
      if (reason === "gate_unavailable") {
        return "Divulgation externe refusée : la politique G2/TLP de cette enquête n’est pas disponible, donc aucune session Codex/Claude ne peut être ouverte (agent_session_external_llm_not_allowed).";
      }
      return "Divulgation externe refusée : la politique G2/TLP ou external_llm_allowed de cette enquête n’autorise pas une session Codex/Claude (agent_session_external_llm_not_allowed).";
    }
    case "agent_session_network_isolation_unavailable": {
      const reason = error.details?.["reason"];
      if (reason === "attestation_absent") {
        return "Confinement indisponible : l’hôte n’atteste aucun mode réseau pour le CLI (agent_session_network_isolation_unavailable).";
      }
      if (reason === "loopback_not_isolated") {
        return "Confinement indisponible : le bac à sable du CLI n’est pas attesté loopback-isolé (agent_session_network_isolation_unavailable).";
      }
      return "Confinement indisponible : l’hôte ne prouve pas l’isolation réseau loopback du CLI (agent_session_network_isolation_unavailable).";
    }
    case "agentic_operator_unavailable":
      return "Agent hôte absent : aucun adaptateur opérateur n’est monté pour ouvrir la session (agentic_operator_unavailable).";
    case "agent_session_secret_delivery_unavailable":
      return "Agent hôte absent : le secret de session ne peut pas être remis à l’agent (agent_session_secret_delivery_unavailable).";
    case "agentic_operator_contract_invalid":
      return "L’adaptateur a répondu hors contrat ; aucune session n’a été ouverte (agentic_operator_contract_invalid).";
    case "agent_session_secret_delivery_failed":
      return "L’agent hôte a refusé le secret de session ; aucune session n’a été ouverte (agent_session_secret_delivery_failed).";
    case "agent_session_start_failed":
      return "Le démarrage de la session a échoué côté hôte (agent_session_start_failed).";
    case "agent_sessions_require_authenticated_identity":
      return "Ouverture refusée : les sessions exigent une identité opérateur authentifiée par jeton, pas le mode identité locale (agent_sessions_require_authenticated_identity).";
    case "active_session_exists":
      return "Une session active existe déjà pour cette tâche ; utilisez-la ou interrompez-la.";
    case "handoff_task_stale":
      return "La passation ne correspond plus à l’état de la tâche ; transférez de nouveau avant d’ouvrir la session.";
    case "new_handoff_required":
      return "Une nouvelle passation est requise pour ouvrir cette session.";
    case "workspace_local_unavailable":
      return "Agent hôte absent : le sondage des capacités et l’ouverture de session sont indisponibles (workspace_local_unavailable).";
    default:
      return describeTaskError(error);
  }
}
