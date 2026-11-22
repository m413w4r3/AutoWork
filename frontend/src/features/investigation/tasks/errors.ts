/**
 * Typed API refusals rendered in French. The code is always kept verbatim in
 * the message, so a new server code is visible instead of being swallowed.
 */

import type { WorkshopApiError } from "../api/client";

export function describeTaskError(error: WorkshopApiError): string {
  switch (error.code) {
    case "task_transition_not_allowed":
      return "Transition refusée par la machine à états (ADR-0011 D3).";
    case "task_actor_not_allowed":
      return "Cette action n’est pas autorisée pour votre rôle.";
    case "lease_expired":
    case "stale_fence":
      return "Le bail de la tâche a expiré ; l’agent doit être réassigné avant de continuer.";
    case "task_limit_exceeded":
      return "La limite de tâches actives de cette enquête est atteinte.";
    case "task_dependency_invalid":
      return "Une dépendance de cette tâche ne permet pas cette opération.";
    case "agent_session_inactive":
      return "La session de l’agent n’est plus active.";
    case "resume_unavailable":
    case "new_handoff_required":
      return "Reprise indisponible : ouvrez une nouvelle session avec une nouvelle passation.";
    case "agent_sessions_unavailable":
      return "Agent absent : les sessions d’opérateurs sont indisponibles (503).";
    case "agent_session_state_conflict":
      return "La session a changé d’état ; rechargez avant de réessayer.";
    case "workspace_local_unavailable":
      return "Workspace local indisponible : aucune écriture ni passation n’est possible.";
    case "workspace_sync_incomplete":
      return "Synchronisation du workspace incomplète ; réessayez après le prochain cycle.";
    case "workspace_revision_conflict":
      return "Une note existe déjà à ce chemin ou a changé entre-temps.";
    case "workspace_path_not_editable":
      return "Ce chemin du workspace n’accepte pas d’écriture depuis l’interface.";
    case "transfer_status_not_allowed":
      return "Cette tâche ne peut pas être transférée dans son état actuel.";
    case "transfer_forbidden":
      return "Le transfert est réservé à un humain vérifié.";
    case "handoff_task_stale":
      return "La tâche a changé depuis la passation ; une nouvelle passation est requise.";
    case "network_error":
      return "Réseau indisponible : la requête n’a pas atteint le serveur.";
    default:
      return `${error.code} (HTTP ${error.status})`;
  }
}
