/**
 * Bandeaux d’état réutilisables du cockpit M24.
 *
 * Chaque bandeau est un export nommé aux props typées. Les erreurs passent
 * toujours par `WorkshopApiError` : le code typé, le statut HTTP et les clés du
 * `detail.details` borné sont affichés, jamais le corps brut de la réponse.
 */

import type { ReactNode } from "react";

import type { WorkshopApiError } from "../api/client";

export type WorkshopStateTone = "info" | "warning" | "error" | "success";

const TONE_CLASS: Record<WorkshopStateTone, string> = {
  info: "workshop-state workshop-state--info",
  warning: "workshop-state workshop-state--warning",
  error: "workshop-state workshop-state--error",
  success: "workshop-state workshop-state--success",
};

function toneRole(tone: WorkshopStateTone): "status" | "note" | "alert" {
  if (tone === "error") return "alert";
  if (tone === "warning") return "note";
  return "status";
}

export function WorkshopStateBanner({
  tone,
  title,
  children,
  live = false,
}: {
  tone: WorkshopStateTone;
  title: string;
  children?: ReactNode;
  live?: boolean;
}) {
  return (
    <div
      className={TONE_CLASS[tone]}
      role={toneRole(tone)}
      aria-live={live ? "polite" : undefined}
    >
      <p className="workshop-state__title">
        <strong>{title}</strong>
      </p>
      {children}
    </div>
  );
}

export function WorkshopLoadingState({
  label,
  detail,
}: {
  label: string;
  detail?: string;
}) {
  return (
    <WorkshopStateBanner tone="info" title={label} live>
      {detail ? <p>{detail}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopLongOperationState({
  label,
  detail,
  progress,
}: {
  label: string;
  detail?: string;
  /** Avancement borné 0–100 ; `null`/absent = durée inconnue, jamais simulée. */
  progress?: number | null;
}) {
  return (
    <WorkshopStateBanner tone="info" title={label} live>
      {progress !== undefined && progress !== null ? (
        <progress max={100} value={progress}>
          {progress} %
        </progress>
      ) : (
        <p>Durée restante inconnue : l’opération reste signalée en cours.</p>
      )}
      {detail ? <p>{detail}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopTypedErrorState({
  error,
  title = "Erreur de l’atelier",
  onRetry,
  retryLabel = "Réessayer",
}: {
  error: WorkshopApiError;
  title?: string;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const detailKeys =
    error.details === null ? [] : Object.keys(error.details).sort();
  return (
    <WorkshopStateBanner tone="error" title={title}>
      <p>{error.message}</p>
      <p>
        Code <code>{error.code}</code> ·{" "}
        {error.status === 0
          ? "échec de transport (network_error)"
          : `statut HTTP ${error.status}`}
      </p>
      {detailKeys.length > 0 ? (
        <p>Champs de détail : {detailKeys.join(", ")}.</p>
      ) : null}
      {onRetry ? (
        <button
          type="button"
          className="button button--secondary"
          onClick={onRetry}
        >
          {retryLabel}
        </button>
      ) : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopToolUnavailableState({
  tools,
  reasons,
}: {
  tools: readonly string[];
  reasons?: Readonly<Record<string, string>>;
}) {
  return (
    <WorkshopStateBanner tone="warning" title="Outil indisponible">
      <p>
        Capacités locales indisponibles :{" "}
        {tools.length === 0 ? "aucune n’est déclarée" : tools.join(", ")}.
      </p>
      {tools.length > 0 ? (
        <ul>
          {tools.map((tool) => (
            <li key={tool}>
              <code>{tool}</code>
              {reasons?.[tool] ? ` — ${reasons[tool]}` : ""}
            </li>
          ))}
        </ul>
      ) : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopG2DeniedState({ reason }: { reason?: string | null }) {
  return (
    <WorkshopStateBanner tone="warning" title="G2 refusé">
      <p>
        Une décision humaine a refusé cette action. Aucun droit externe n’est
        accordé par ce refus.
      </p>
      {reason ? <p>{reason}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopConflictState({
  reason,
  expectedVersion,
  currentVersion,
}: {
  reason?: string | null;
  expectedVersion?: number;
  currentVersion?: number;
}) {
  return (
    <WorkshopStateBanner tone="warning" title="Conflit de version">
      <p>
        L’objet a changé depuis la lecture
        {expectedVersion !== undefined
          ? ` (version attendue ${expectedVersion}`
          : ""}
        {currentVersion !== undefined
          ? `${expectedVersion !== undefined ? ", " : " ("}version courante ${currentVersion}`
          : ""}
        {expectedVersion !== undefined || currentVersion !== undefined
          ? ")."
          : "."}{" "}
        Rechargez l’objet avant de redécider.
      </p>
      {reason ? <p>{reason}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkspaceUnavailableState({
  reason,
  absent = false,
  onCreate,
  createPending = false,
}: {
  reason?: string | null;
  absent?: boolean;
  onCreate?: () => void;
  createPending?: boolean;
}) {
  const creatable = absent && onCreate !== undefined;
  return (
    <WorkshopStateBanner
      tone="warning"
      title={
        creatable
          ? "Workspace local à créer"
          : absent
            ? "Workspace local absent"
            : "Workspace local indisponible"
      }
    >
      <p>
        {absent
          ? "Aucun workspace n’est lié à cette enquête. Les actions qui en dépendent restent bloquées."
          : "L’agent hôte ne répond pas ou le workspace n’est pas monté. Les écritures ne sont pas mises en file hors ligne."}
      </p>
      {reason ? <p>{reason}</p> : null}
      {creatable ? (
        <button
          type="button"
          className="button"
          disabled={createPending}
          onClick={onCreate}
        >
          {createPending ? "Création…" : "Créer le workspace"}
        </button>
      ) : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopAgentAbsentState({
  reason,
  status,
}: {
  reason?: string | null;
  status?: string;
}) {
  return (
    <WorkshopStateBanner tone="warning" title="Agent hôte absent">
      <p>
        Les actions sur le workspace local sont bloquées
        {status ? ` (état déclaré : ${status})` : ""}. Aucune relance
        automatique n’est tentée.
      </p>
      {reason ? <p>{reason}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopSyncingState({ detail }: { detail?: string }) {
  return (
    <WorkshopStateBanner tone="info" title="Synchronisation en cours" live>
      <p>
        Le workspace et les révisions sont en cours de synchronisation avec
        l’agent hôte.
      </p>
      {detail ? <p>{detail}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopSessionLostState({
  reason,
}: {
  reason?: string | null;
}) {
  return (
    <WorkshopStateBanner tone="warning" title="Session agent perdue">
      <p>
        La session doit être réconciliée avant toute relance ; la continuité ne
        dépend pas de la mémoire du CLI.
      </p>
      {reason ? <p>{reason}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopBridgeUnavailableState({
  reason,
}: {
  reason?: string | null;
}) {
  return (
    <WorkshopStateBanner tone="warning" title="Bridge indisponible">
      <p>
        La rédaction ou la revue Bridge attend la disponibilité du service.
        Aucune provenance Bridge n’est fabriquée entre-temps.
      </p>
      {reason ? <p>{reason}</p> : null}
    </WorkshopStateBanner>
  );
}

export function WorkshopPartialState({
  reason,
  children,
}: {
  reason: string;
  children?: ReactNode;
}) {
  return (
    <WorkshopStateBanner tone="warning" title="Résultat partiel">
      <p>{reason}</p>
      {children}
    </WorkshopStateBanner>
  );
}

export function WorkshopReadOnlyState({ reason }: { reason: string }) {
  return (
    <WorkshopStateBanner tone="info" title="Lecture seule">
      <p>{reason}</p>
    </WorkshopStateBanner>
  );
}
