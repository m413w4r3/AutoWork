/**
 * Explicit session-opening action of one transferred task.
 *
 * The action exists only when the real probe answers `available` and proves
 * the loopback-isolated confinement the backend enforces. The open request
 * carries the probe's own `executable` echo, the current handoff and no
 * invented model alias. Every refusal is rendered with its typed French
 * reason; a failed probe hides the action instead of offering a dead end.
 */

import { useState } from "react";

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

export interface OpenSessionActionProps {
  investigationId: string;
  target: SessionLaunchTarget;
}

export function OpenSessionAction({
  investigationId,
  target,
}: OpenSessionActionProps) {
  const capabilities = useAgentSessionCapabilities(target.capability);
  const open = useOpenAgentSession(investigationId);
  const [message, setMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const label = openSessionLabel(target.adapterKind);

  function openSession() {
    const view = capabilities.data;
    if (view === undefined) return;
    setMessage(null);
    setErrorMessage(null);
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
          setMessage(
            `${label} : session ouverte ; la tâche passe en cours et l’opérateur apparaît dans le panneau.`,
          );
        },
        onError: (error) => {
          setErrorMessage(describeSessionRefusal(toWorkshopApiError(error)));
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
      {message === null ? null : (
        <p role="status" className="m24-message">
          {message}
        </p>
      )}
      {errorMessage === null ? null : (
        <p role="alert" className="error-message">
          {errorMessage}
        </p>
      )}
    </div>
  );
}
