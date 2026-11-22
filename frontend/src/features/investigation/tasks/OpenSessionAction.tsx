/**
 * Explicit session-opening action of one transferred task.
 *
 * The action exists only when the real probe answers `available` and proves
 * the loopback-isolated confinement the backend enforces. The open request
 * carries the probe's own `executable` echo, the current handoff and no
 * invented model alias. Every refusal is rendered with its typed French
 * reason; a failed probe hides the action instead of offering a dead end.
 *
 * The action's own eligibility disappears once the session exists or the task
 * moves to `running`, so the action unmounts right after a success. The
 * human-visible feedback is therefore owned by the caller (the stable task
 * card or operator panel), never by this transient component.
 */

import {
  useAgentSessionCapabilities,
  useOpenAgentSession,
} from "../api/agentSessions";
import { toWorkshopApiError } from "../api/client";
import { ADAPTER_LABELS } from "./labels";
import {
  confinementAttested,
  confinementGap,
  describeSessionRefusal,
  openSessionLabel,
  type SessionLaunchTarget,
} from "./sessionLaunch";

export interface OpenSessionFeedback {
  kind: "success" | "error";
  text: string;
}

export interface OpenSessionActionProps {
  investigationId: string;
  target: SessionLaunchTarget;
  /** Rendered by the stable parent so it survives this action's unmounting. */
  onFeedback: (feedback: OpenSessionFeedback) => void;
}

export function OpenSessionAction({
  investigationId,
  target,
  onFeedback,
}: OpenSessionActionProps) {
  const capabilities = useAgentSessionCapabilities(target.capability);
  const open = useOpenAgentSession(investigationId);
  const label = openSessionLabel(target.adapterKind);

  function openSession() {
    const view = capabilities.data;
    if (view === undefined) return;
    open.mutate(
      {
        task_id: target.taskId,
        adapter_kind: target.adapterKind,
        handoff_id: target.handoffId,
        executable: view.executable,
        // The capability contract exposes no model alias; never ask the human
        // for one. `null` means the canonical host default.
        model: null,
      },
      {
        onSuccess: () => {
          onFeedback({
            kind: "success",
            text: `${label} : session ouverte ; la tâche passe en cours et l’opérateur apparaît dans le panneau.`,
          });
        },
        onError: (error) => {
          onFeedback({
            kind: "error",
            text: describeSessionRefusal(toWorkshopApiError(error)),
          });
        },
      },
    );
  }

  if (capabilities.isPending) {
    return (
      <p className="m24-muted" role="status">
        Sondage des capacités {ADAPTER_LABELS[target.adapterKind]}…
      </p>
    );
  }

  if (capabilities.isError) {
    return (
      <p className="m24-muted" role="note">
        Ouverture de session masquée :{" "}
        {describeSessionRefusal(toWorkshopApiError(capabilities.error))}
      </p>
    );
  }

  const view = capabilities.data;
  if (!confinementAttested(view)) {
    return (
      <p className="m24-muted" role="note">
        Ouverture de session masquée : {confinementGap(view)}.
      </p>
    );
  }

  return (
    <div className="m24-inline-form__buttons">
      <button
        type="button"
        className="m24-action"
        onClick={openSession}
        disabled={open.isPending}
      >
        {open.isPending ? "Ouverture de la session…" : label}
      </button>
    </div>
  );
}
