import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";

import { ApiError } from "../../api/editions";
import { getSynthesisArtifact } from "../../api/production";
import {
  useInterruptInvestigation,
  useInvestigation,
  useResumeInvestigation,
  useStartInvestigation,
  useSubjectInvestigations,
  type InvestigationView,
} from "../../api/investigation";
import { useInvestigationActivity } from "../../api/investigationOperator";
import { InvestigationLabViews } from "./InvestigationLabViews";
import { WORKSPACE_AVAILABILITY_LABELS } from "./overview/overviewPresentation";
import { usePendingDecisions } from "./overview/usePendingDecisions";
import { WorkshopCockpit } from "./shell/WorkshopCockpit";
import { formatTimestamp, shortSha } from "./tasks/labels";

const HASH_PATTERN = /^[0-9a-f]{64}$/;

/** Artifact verification status of the synthesis, translated for the UI. */
const SYNTHESIS_STATUS_LABELS: Record<string, string> = {
  verified: "Vérifiée",
  stale: "Périmée",
  needs_review: "À réviser",
};

export function InvestigationTab({
  subjectId,
  editionId,
  articleTitle,
}: {
  subjectId: string;
  editionId: string;
  /** The article title from the parent subject page, when available. */
  articleTitle?: string;
}) {
  const [selectedInvestigationId, setSelectedInvestigationId] = useState<
    string | null
  >(null);
  const synthesisQuery = useQuery({
    queryKey: ["subject-synthesis-artifact", subjectId],
    queryFn: () => getSynthesisArtifact(subjectId),
    retry: false,
  });
  const investigationsQuery = useSubjectInvestigations(subjectId);
  const launch = useStartInvestigation(subjectId);
  const workshopLaunch = useStartInvestigation(subjectId);
  const exactSynthesis =
    synthesisQuery.data?.status === "verified" &&
    synthesisQuery.data.canonical_sha256 &&
    HASH_PATTERN.test(synthesisQuery.data.canonical_sha256) &&
    synthesisQuery.data.production_run_id
      ? synthesisQuery.data
      : null;
  const existingForExactSynthesis = investigationsQuery.data?.find(
    (item) =>
      item.production_run_id === exactSynthesis?.production_run_id &&
      item.synthesis_artifact_id === exactSynthesis?.artifact_id &&
      item.synthesis_sha256 === exactSynthesis?.canonical_sha256,
  );
  const investigationId =
    selectedInvestigationId ?? existingForExactSynthesis?.id ?? null;
  const investigationQuery = useInvestigation(investigationId);
  // The workshop cockpit must be reachable without scrolling: once a workshop
  // investigation is selected, fold the launch/list cards above it.
  const workshopActive =
    investigationQuery.data?.prose_authority === "bridge_only";
  const resume = useResumeInvestigation();
  const interrupt = useInterruptInvestigation();

  async function startExactSynthesis() {
    if (!exactSynthesis?.production_run_id || !exactSynthesis.canonical_sha256)
      return;
    const result = await launch.mutateAsync({
      production_run_id: exactSynthesis.production_run_id,
      synthesis_artifact_id: exactSynthesis.artifact_id,
      synthesis_sha256: exactSynthesis.canonical_sha256,
    });
    setSelectedInvestigationId(result.investigation.id);
  }

  async function startWorkshop() {
    if (!exactSynthesis?.production_run_id || !exactSynthesis.canonical_sha256)
      return;
    const result = await workshopLaunch.mutateAsync({
      production_run_id: exactSynthesis.production_run_id,
      synthesis_artifact_id: exactSynthesis.artifact_id,
      synthesis_sha256: exactSynthesis.canonical_sha256,
      creation_profile: "m24_workshop",
      execution_mode: "operator_led",
    });
    setSelectedInvestigationId(result.investigation.id);
  }

  return (
    <section
      className="subject-workbench"
      aria-labelledby="investigation-tab-heading"
    >
      <h2 id="investigation-tab-heading">Enquête / Avis de l’analyste</h2>
      <details className="investigation-source-fold" open={!workshopActive}>
        <summary>Synthèse et enquêtes du sujet</summary>
        <section
          className="job-card"
          aria-labelledby="investigation-target-heading"
        >
          <h3 id="investigation-target-heading">
            Synthèse exacte à investiguer
          </h3>
          {synthesisQuery.isPending ? (
            <p role="status">Chargement de la synthèse vérifiée…</p>
          ) : synthesisQuery.isError ? (
            synthesisQuery.error instanceof ApiError &&
            synthesisQuery.error.status === 404 ? (
              <div className="empty-state">
                <p>Aucune synthèse disponible pour ce sujet.</p>
                <a
                  href={`/editions/${encodeURIComponent(editionId)}/production`}
                >
                  Ouvrir le pipeline de production
                </a>
              </div>
            ) : (
              <p className="error-message" role="alert">
                La synthèse ne peut pas être chargée :{" "}
                {synthesisQuery.error.message}
              </p>
            )
          ) : synthesisQuery.data ? (
            <>
              <dl>
                <div>
                  <dt>Production run</dt>
                  <dd>
                    {synthesisQuery.data.production_run_id ?? "Non renseigné"}
                  </dd>
                </div>
                <div>
                  <dt>Artefact de synthèse</dt>
                  <dd>
                    {synthesisQuery.data.artifact_id} · version{" "}
                    {synthesisQuery.data.version}
                  </dd>
                </div>
                <div>
                  <dt>SHA-256 canonique</dt>
                  <dd>
                    <code>
                      {synthesisQuery.data.canonical_sha256 ?? "Indisponible"}
                    </code>
                  </dd>
                </div>
                <div>
                  <dt>État de vérification</dt>
                  <dd>
                    {SYNTHESIS_STATUS_LABELS[synthesisQuery.data.status] ??
                      synthesisQuery.data.status}
                  </dd>
                </div>
              </dl>
              {!exactSynthesis ? (
                <p className="verification-warning" role="note">
                  Lancer une enquête exige un run, un artefact vérifié et son
                  SHA-256 canonique exact.
                </p>
              ) : null}
              <div>
                <button
                  type="button"
                  className="button button--secondary"
                  disabled={!exactSynthesis || launch.isPending}
                  onClick={() => {
                    if (existingForExactSynthesis !== undefined) {
                      // Une enquête existe déjà pour cette synthèse exacte : la
                      // sélectionner ne mute rien. Un nouveau start legacy serait
                      // refusé par le serveur (`prose_authority_conflict`).
                      setSelectedInvestigationId(existingForExactSynthesis.id);
                      return;
                    }
                    void startExactSynthesis();
                  }}
                >
                  {launch.isPending
                    ? "Lancement…"
                    : existingForExactSynthesis
                      ? "Ouvrir l’enquête sur cette version"
                      : "Investiguer cette version exacte"}
                </button>{" "}
                <button
                  type="button"
                  className="button"
                  disabled={
                    !exactSynthesis ||
                    workshopLaunch.isPending ||
                    Boolean(existingForExactSynthesis)
                  }
                  onClick={() => void startWorkshop()}
                >
                  {workshopLaunch.isPending
                    ? "Lancement de l’atelier…"
                    : "Lancer l’atelier d’investigation"}
                </button>
              </div>
              <p>
                Atelier M24 : workspace local, Bridge rédacteur unique et
                opérateurs agentiques. Une seule enquête peut exister par
                version de synthèse.
              </p>
            </>
          ) : (
            <p>Aucune synthèse disponible.</p>
          )}
          {launch.error ? (
            <p className="error-message" role="alert">
              {launch.error.message}
            </p>
          ) : null}
          {workshopLaunch.error ? (
            <p className="error-message" role="alert">
              {workshopLaunch.error.message}
            </p>
          ) : null}
        </section>

        <section
          className="job-card"
          aria-labelledby="investigation-list-heading"
        >
          <h3 id="investigation-list-heading">Enquêtes du sujet</h3>
          {investigationsQuery.isPending ? (
            <p role="status">Chargement des enquêtes…</p>
          ) : investigationsQuery.isError ? (
            <p className="error-message" role="alert">
              La liste des enquêtes est inaccessible :{" "}
              {investigationsQuery.error.message}
            </p>
          ) : investigationsQuery.data.length === 0 ? (
            <p>Aucune enquête n’a encore été lancée.</p>
          ) : (
            <ul>
              {investigationsQuery.data.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    aria-pressed={investigationId === item.id}
                    onClick={() => setSelectedInvestigationId(item.id)}
                  >
                    {statusLabel(item)} · run {item.production_run_id} ·
                    synthèse {item.synthesis_artifact_id} · version{" "}
                    {item.version}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </details>

      {investigationId ? (
        investigationQuery.isPending ? (
          <p role="status">Chargement de l’enquête…</p>
        ) : investigationQuery.isError ? (
          <p className="error-message" role="alert">
            L’enquête est inaccessible : {investigationQuery.error.message}
          </p>
        ) : investigationQuery.data ? (
          <InvestigationDetail
            investigation={investigationQuery.data}
            editionId={editionId}
            articleTitle={articleTitle ?? "Article de l’enquête"}
            onResume={(expectedVersion) =>
              resume.mutate({
                investigationId,
                expected_version: expectedVersion,
              })
            }
            onInterrupt={(expectedVersion) =>
              interrupt.mutate({
                investigationId,
                expected_version: expectedVersion,
              })
            }
            actionPending={resume.isPending || interrupt.isPending}
            actionError={resume.error ?? interrupt.error}
          />
        ) : null
      ) : null}
      {!investigationId &&
      investigationsQuery.data &&
      investigationsQuery.data.length > 0 ? (
        <p>Sélectionnez une enquête pour consulter ses constats et son avis.</p>
      ) : null}
    </section>
  );
}

function InvestigationDetail({
  investigation,
  editionId,
  articleTitle,
  onResume,
  onInterrupt,
  actionPending,
  actionError,
}: {
  investigation: InvestigationView;
  editionId: string;
  articleTitle: string;
  onResume: (expectedVersion: number) => void;
  onInterrupt: (expectedVersion: number) => void;
  actionPending: boolean;
  actionError: Error | null;
}) {
  const activityQuery = useInvestigationActivity(investigation.id);
  // Discriminant M24 explicite : `creation_profile='m24_workshop'` fixe
  // `prose_authority='bridge_only'` à l'INSERT (ADR-0011 D4). Le binding du
  // workspace n'est qu'un état best-effort au démarrage, jamais le discriminant.
  const workshopActive = investigation.prose_authority === "bridge_only";
  const latest = activityQuery.data?.investigation;
  const pendingDecisions = usePendingDecisions(investigation.id, {
    enabled: workshopActive,
    workspaceViewsAvailable: workshopActive,
  });
  const workspaceQuery = pendingDecisions.workspaceQuery;
  const pendingDecisionCount = pendingDecisions.pendingDecisionCount;
  const currentStatus = latest?.status ?? investigation.status;
  const currentStopReason = latest
    ? latest.stop_reason
    : investigation.stop_reason;
  const currentVersion = latest?.version ?? investigation.version;
  const isRunning = currentStatus === "queued" || currentStatus === "running";
  const canInterrupt = isRunning || currentStatus === "awaiting_review";
  const canResume = Boolean(
    currentStopReason &&
    !isRunning &&
    currentStatus !== "completed" &&
    currentStatus !== "cancelled" &&
    currentStopReason !== "permission_required" &&
    currentStopReason !== "budget_exhausted",
  );
  const currentStatusLabel = statusLabel({
    status: currentStatus,
    stop_reason: currentStopReason,
  });
  const workspaceStatus = workspaceQuery.isPending
    ? "Vérification en cours"
    : workspaceQuery.isError
      ? "Indisponible"
      : workspaceQuery.data
        ? WORKSPACE_AVAILABILITY_LABELS[workspaceQuery.data.availability_code]
        : "Inconnu";
  return (
    <section aria-labelledby="investigation-detail-heading">
      {workshopActive ? (
        <section
          className="workshop-context-bar"
          aria-labelledby="investigation-detail-heading"
        >
          <div className="workshop-context-bar__heading">
            <p className="eyebrow">Atelier d’investigation</p>
            <h2 id="investigation-detail-heading">{articleTitle}</h2>
            <details className="investigation-identity-details">
              <summary>Identité technique de l’enquête</summary>
              <p>
                Enquête{" "}
                <code title={investigation.id} className="m24-id-ref">
                  {shortSha(investigation.id)}
                </code>{" "}
                · version de l’état {currentVersion} · run{" "}
                {investigation.production_run_id} · synthèse{" "}
                {investigation.synthesis_artifact_id} · SHA-256{" "}
                <code title={investigation.synthesis_sha256}>
                  {shortSha(investigation.synthesis_sha256)}
                </code>
              </p>
              <p>
                Décisions attribuées à l’identité authentifiée côté serveur.
              </p>
            </details>
          </div>
          <dl className="workshop-context-meta">
            <div>
              <dt>Enquête</dt>
              <dd>{currentStatusLabel}</dd>
            </div>
            <div>
              <dt>Workspace</dt>
              <dd>{workspaceStatus}</dd>
            </div>
            <div>
              <dt>Décisions en attente</dt>
              <dd>
                {pendingDecisionCount ?? (
                  <span aria-label="Nombre de décisions non disponible">—</span>
                )}
              </dd>
            </div>
          </dl>
          <div className="workshop-context-bar__actions">
            <ActivityDisclosure query={activityQuery} />
            <InvestigationActionsMenu
              currentVersion={currentVersion}
              canResume={canResume}
              canInterrupt={canInterrupt}
              actionPending={actionPending}
              onResume={onResume}
              onInterrupt={onInterrupt}
            />
          </div>
        </section>
      ) : (
        <>
          <header className="job-card__heading">
            <div>
              <h2 id="investigation-detail-heading">
                Enquête{" "}
                <code title={investigation.id} className="m24-id-ref">
                  {investigation.id}
                </code>
              </h2>
              <p>
                {currentStatusLabel} ·{" "}
                <span title="Version de l’état de travail (CAS)">
                  version de l’état {currentVersion}
                </span>{" "}
                · run {investigation.production_run_id} · synthèse{" "}
                {investigation.synthesis_artifact_id} · SHA-256{" "}
                <code title={investigation.synthesis_sha256}>
                  {investigation.synthesis_sha256}
                </code>
              </p>
              <p>
                Décisions attribuées à l’identité authentifiée côté serveur.
              </p>
            </div>
          </header>
          <div className="investigation-secondary-actions">
            <InvestigationActionsMenu
              currentVersion={currentVersion}
              canResume={canResume}
              canInterrupt={canInterrupt}
              actionPending={actionPending}
              onResume={onResume}
              onInterrupt={onInterrupt}
            />
          </div>
        </>
      )}
      {currentStopReason === "model_unavailable" ? (
        <p className="verification-warning" role="note">
          Le modèle est indisponible. C’est une limite locale de cette piste ;
          les constats déjà enregistrés restent consultables et l’enquête n’est
          pas déclarée en échec global.
        </p>
      ) : null}
      {actionError ? (
        <p className="error-message" role="alert">
          {actionError.message}
        </p>
      ) : null}
      {workshopActive ? (
        <WorkshopCockpit
          investigationId={investigation.id}
          subjectId={investigation.subject_id}
        />
      ) : (
        <InvestigationLabViews
          investigation={investigation}
          editionId={editionId}
          showG2Prompt={currentStopReason === "permission_required"}
        />
      )}
    </section>
  );
}

function ActivityDisclosure({
  query,
}: {
  query: ReturnType<typeof useInvestigationActivity>;
}) {
  return (
    <details className="workshop-context-activity">
      <summary>Activité</summary>
      <div className="workshop-context-activity__content">
        {query.isPending ? (
          <p role="status">Chargement de l’activité…</p>
        ) : query.isError ? (
          <p role="status">L’activité est indisponible.</p>
        ) : query.data ? (
          <>
            <p>
              Dernière activité : {formatTimestamp(query.data.last_activity_at)}
              {query.data.last_activity_source
                ? ` · ${query.data.last_activity_source}`
                : ""}
            </p>
            {query.data.timeline.length > 0 ? (
              <ul>
                {query.data.timeline.slice(0, 5).map((item) => (
                  <li key={item.id}>
                    <span>{item.activity_label}</span>{" "}
                    <time dateTime={item.created_at}>
                      {formatTimestamp(item.created_at)}
                    </time>
                  </li>
                ))}
              </ul>
            ) : (
              <p>Aucune activité récente.</p>
            )}
          </>
        ) : (
          <p>Aucune activité disponible.</p>
        )}
      </div>
    </details>
  );
}

function InvestigationActionsMenu({
  currentVersion,
  canResume,
  canInterrupt,
  actionPending,
  onResume,
  onInterrupt,
}: {
  currentVersion: number;
  canResume: boolean;
  canInterrupt: boolean;
  actionPending: boolean;
  onResume: (expectedVersion: number) => void;
  onInterrupt: (expectedVersion: number) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const menuFocusDirection = useRef<"first" | "last">("first");

  useEffect(() => {
    if (!menuOpen) return;
    const items = menuRef.current?.querySelectorAll<HTMLButtonElement>(
      '[role="menuitem"]:not(:disabled)',
    );
    if (!items?.length) return;
    const item =
      menuFocusDirection.current === "last"
        ? items.item(items.length - 1)
        : items.item(0);
    item?.focus();
  }, [menuOpen]);

  useEffect(() => {
    if (confirmationOpen) cancelRef.current?.focus();
  }, [confirmationOpen]);

  const closeMenu = () => {
    setMenuOpen(false);
    triggerRef.current?.focus();
  };

  const cancelInterruption = () => {
    setConfirmationOpen(false);
    triggerRef.current?.focus();
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
      return;
    }
    if (
      event.key !== "ArrowDown" &&
      event.key !== "ArrowUp" &&
      event.key !== "Home" &&
      event.key !== "End"
    ) {
      return;
    }
    event.preventDefault();
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ) ?? [],
    );
    if (items.length === 0) return;
    const currentIndex = items.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const nextIndex =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : event.key === "ArrowDown"
            ? (currentIndex + 1) % items.length
            : (currentIndex - 1 + items.length) % items.length;
    items[nextIndex]?.focus();
  };

  const hasActions = canResume || canInterrupt;

  return (
    <div className="investigation-actions">
      <button
        ref={triggerRef}
        type="button"
        className="button button--secondary"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-controls="investigation-actions-menu"
        disabled={!hasActions || actionPending}
        onClick={() => setMenuOpen((open) => !open)}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          event.preventDefault();
          menuFocusDirection.current =
            event.key === "ArrowUp" ? "last" : "first";
          setMenuOpen(true);
        }}
      >
        Actions de l’enquête
      </button>
      {menuOpen ? (
        <div
          ref={menuRef}
          id="investigation-actions-menu"
          className="investigation-actions__menu"
          role="menu"
          aria-label="Actions de l’enquête"
          onKeyDown={handleMenuKeyDown}
        >
          {canResume ? (
            <button
              type="button"
              role="menuitem"
              disabled={actionPending}
              onClick={() => {
                onResume(currentVersion);
                closeMenu();
              }}
            >
              Reprendre l’enquête
            </button>
          ) : null}
          {canInterrupt ? (
            <button
              type="button"
              role="menuitem"
              disabled={actionPending}
              onClick={() => {
                setMenuOpen(false);
                setConfirmationOpen(true);
              }}
            >
              Interrompre l’enquête
            </button>
          ) : null}
        </div>
      ) : null}
      {confirmationOpen ? (
        <div
          className="investigation-actions__confirmation"
          role="group"
          aria-labelledby="investigation-interrupt-title"
          aria-describedby="investigation-interrupt-description"
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.preventDefault();
            event.stopPropagation();
            cancelInterruption();
          }}
        >
          <h3 id="investigation-interrupt-title">Confirmer l’interruption</h3>
          <p id="investigation-interrupt-description">
            Voulez-vous interrompre cette enquête maintenant ?
          </p>
          <div className="investigation-actions__confirmation-buttons">
            <button
              ref={cancelRef}
              type="button"
              className="button button--secondary"
              onClick={cancelInterruption}
            >
              Annuler
            </button>
            <button
              type="button"
              className="button"
              disabled={actionPending}
              onClick={() => {
                onInterrupt(currentVersion);
                setConfirmationOpen(false);
                triggerRef.current?.focus();
              }}
            >
              Confirmer l’interruption
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function statusLabel(
  investigation: Pick<InvestigationView, "status" | "stop_reason">,
): string {
  if (investigation.stop_reason === "permission_required")
    return "Permission requise";
  if (investigation.stop_reason === "model_unavailable")
    return "Modèle indisponible";
  if (investigation.status === "queued") return "En attente";
  if (investigation.status === "running") return "En cours";
  if (investigation.status === "awaiting_review") return "À réviser";
  if (investigation.status === "completed") return "Terminée";
  if (investigation.status === "exhausted") return "Budget atteint";
  if (investigation.status === "failed") return "Enquête interrompue";
  return "Annulée";
}
