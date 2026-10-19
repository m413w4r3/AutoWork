import { useState } from "react";
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
import { InvestigationLabViews } from "./InvestigationLabViews";

const HASH_PATTERN = /^[0-9a-f]{64}$/;

export function InvestigationTab({
  subjectId,
  editionId,
}: {
  subjectId: string;
  editionId: string;
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

  return (
    <section
      className="subject-workbench"
      aria-labelledby="investigation-tab-heading"
    >
      <h2 id="investigation-tab-heading">Enquête / Avis de l’analyste</h2>
      <section
        className="job-card"
        aria-labelledby="investigation-target-heading"
      >
        <h3 id="investigation-target-heading">Synthèse exacte à investiguer</h3>
        {synthesisQuery.isPending ? (
          <p role="status">Chargement de la synthèse vérifiée…</p>
        ) : synthesisQuery.isError ? (
          synthesisQuery.error instanceof ApiError &&
          synthesisQuery.error.status === 404 ? (
            <div className="empty-state">
              <p>Aucune synthèse disponible pour ce sujet.</p>
              <a href={`/editions/${encodeURIComponent(editionId)}/production`}>
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
                <dd>{synthesisQuery.data.status}</dd>
              </div>
            </dl>
            {!exactSynthesis ? (
              <p className="verification-warning" role="note">
                Lancer une enquête exige un run, un artefact vérifié et son
                SHA-256 canonique exact.
              </p>
            ) : null}
            <button
              type="button"
              className="button button--secondary"
              disabled={!exactSynthesis || launch.isPending}
              onClick={() => void startExactSynthesis()}
            >
              {launch.isPending
                ? "Lancement…"
                : existingForExactSynthesis
                  ? "Ouvrir l’enquête sur cette version"
                  : "Investiguer cette version exacte"}
            </button>
          </>
        ) : (
          <p>Aucune synthèse disponible.</p>
        )}
        {launch.error ? (
          <p className="error-message" role="alert">
            {launch.error.message}
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
                  {statusLabel(item)} · run {item.production_run_id} · synthèse{" "}
                  {item.synthesis_artifact_id} · version {item.version}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

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
            onResume={() =>
              resume.mutate({
                investigationId,
                expected_version: investigationQuery.data.version,
              })
            }
            onInterrupt={() =>
              interrupt.mutate({
                investigationId,
                expected_version: investigationQuery.data.version,
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
  onResume,
  onInterrupt,
  actionPending,
  actionError,
}: {
  investigation: InvestigationView;
  editionId: string;
  onResume: () => void;
  onInterrupt: () => void;
  actionPending: boolean;
  actionError: Error | null;
}) {
  const isRunning =
    investigation.status === "queued" || investigation.status === "running";
  const canInterrupt = isRunning || investigation.status === "awaiting_review";
  const canResume = Boolean(
    investigation.stop_reason &&
    !isRunning &&
    investigation.status !== "completed" &&
    investigation.status !== "cancelled" &&
    investigation.stop_reason !== "permission_required" &&
    investigation.stop_reason !== "budget_exhausted",
  );
  return (
    <section aria-labelledby="investigation-detail-heading">
      <header className="job-card__heading">
        <div>
          <h2 id="investigation-detail-heading">Enquête {investigation.id}</h2>
          <p>
            {statusLabel(investigation)} · version CAS {investigation.version} ·
            run {investigation.production_run_id} · synthèse{" "}
            {investigation.synthesis_artifact_id} · SHA-256{" "}
            <code>{investigation.synthesis_sha256}</code>
          </p>
          <p>Décisions attribuées à l’identité authentifiée côté serveur.</p>
        </div>
      </header>
      {investigation.stop_reason === "model_unavailable" ? (
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
      <div>
        {canResume ? (
          <button
            type="button"
            className="button button--secondary"
            disabled={actionPending}
            onClick={onResume}
          >
            Reprendre l’enquête
          </button>
        ) : null}{" "}
        {canInterrupt ? (
          <button
            type="button"
            className="button button--secondary"
            disabled={actionPending}
            onClick={onInterrupt}
          >
            Interrompre l’enquête
          </button>
        ) : null}
      </div>
      <InvestigationLabViews
        investigation={investigation}
        editionId={editionId}
        showG2Prompt={investigation.stop_reason === "permission_required"}
      />
    </section>
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
