import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  useApproveAnalystOpinion,
  usePublishAnalystOpinion,
  type PublishOpinionResponse,
} from "../../../api/analystOpinion";
import type { CitationViolationPayload } from "../../../api/investigation";
import { describeApiError, isBridgeAcknowledgementError } from "./errors";
import { shortHash } from "./labels";
import { buildPreApprovalReport } from "./preApprovalChecks";
import { opinionNotesKeys } from "./queries";
import type { AnalystNoteV1, OpinionVersionV1 } from "./types";

const CHECK_STATUS_LABELS = {
  ok: "Conforme",
  warning: "À examiner",
  blocked: "Bloquant",
} as const;

export function PreApprovalPanel({
  investigationId,
  opinion,
  citationViolations,
  notes,
}: {
  investigationId: string;
  opinion: OpinionVersionV1 | null;
  citationViolations: CitationViolationPayload[];
  notes: AnalystNoteV1[];
}) {
  const queryClient = useQueryClient();
  const approve = useApproveAnalystOpinion(investigationId);
  const publish = usePublishAnalystOpinion(investigationId);
  const [reason, setReason] = useState("");
  const [previewSeen, setPreviewSeen] = useState(false);
  const [gapAcknowledged, setGapAcknowledged] = useState(false);
  const [published, setPublished] = useState<PublishOpinionResponse | null>(
    null,
  );

  const report =
    opinion === null
      ? null
      : buildPreApprovalReport(opinion, citationViolations, notes);
  const gapRequired =
    report?.bridgeGapRequiresAcknowledgement === true ||
    isBridgeAcknowledgementError(approve.error);
  const gapSatisfied = !gapRequired || gapAcknowledged;
  const canApprove =
    opinion !== null &&
    opinion.status === "draft" &&
    report !== null &&
    !report.blocked &&
    previewSeen &&
    gapSatisfied &&
    reason.trim() !== "" &&
    !approve.isPending;
  const canPublish =
    opinion !== null &&
    opinion.status === "approved" &&
    report !== null &&
    !report.blocked &&
    previewSeen &&
    gapSatisfied &&
    !publish.isPending;

  useEffect(() => {
    setPreviewSeen(false);
    setGapAcknowledged(false);
    setReason("");
  }, [opinion?.id, opinion?.version, opinion?.content_sha256]);

  function approveOpinion() {
    if (opinion === null) return;
    approve.mutate(
      {
        opinionId: opinion.id,
        version: opinion.version,
        expected_sha256: opinion.content_sha256,
        reason: reason.trim(),
        ...(gapAcknowledged ? { bridge_drafting_gap_acknowledged: true } : {}),
      },
      {
        onSuccess: () => {
          void queryClient.invalidateQueries({
            queryKey: opinionNotesKeys.detail(investigationId),
          });
        },
      },
    );
  }

  function publishOpinion() {
    if (opinion === null) return;
    publish.mutate(
      {
        opinionId: opinion.id,
        version: opinion.version,
        expected_base_publication_sha256:
          opinion.provenance.base_publication_sha256,
        ...(gapAcknowledged ? { bridge_drafting_gap_acknowledged: true } : {}),
      },
      { onSuccess: (result) => setPublished(result) },
    );
  }

  return (
    <section className="job-card" aria-labelledby="opinion-preapproval-heading">
      <h3 id="opinion-preapproval-heading">Contrôles pré-approbation</h3>
      <p>
        L’approbation vise explicitement une version et son hash. Elle ne publie
        rien : la publication et le réépinglage dans la composition restent deux
        décisions humaines distinctes.
      </p>

      {opinion === null || report === null ? (
        <p>Aucun avis n’est disponible pour l’approbation.</p>
      ) : (
        <>
          <ul aria-label="Contrôles avant approbation">
            {report.checks.map((check) => (
              <li key={check.key}>
                <strong>
                  {CHECK_STATUS_LABELS[check.status]} · {check.label}
                </strong>{" "}
                — {check.detail}
              </li>
            ))}
          </ul>

          <div>
            <h4>Aperçu PDF réel</h4>
            <p>
              L’approbation exige la consultation de l’aperçu PDF réel de cette
              version.{" "}
              <a
                href={report.previewUrl ?? "#"}
                target="_blank"
                rel="noreferrer"
              >
                Ouvrir l’aperçu PDF réel de l’avis v{opinion.version}
              </a>
            </p>
            <label>
              <input
                type="checkbox"
                checked={previewSeen}
                onChange={(event) =>
                  setPreviewSeen(event.currentTarget.checked)
                }
              />{" "}
              J’ai consulté l’aperçu PDF réel de la version v{opinion.version} ·
              SHA-256 {shortHash(opinion.content_sha256)}.
            </label>
          </div>

          {gapRequired ? (
            <div className="verification-warning" role="alert">
              <p>
                La couverture de rédaction Bridge est incomplète : ce texte ne
                peut pas être présenté comme entièrement rédigé avec Bridge.
              </p>
              <label>
                <input
                  type="checkbox"
                  checked={gapAcknowledged}
                  onChange={(event) =>
                    setGapAcknowledged(event.currentTarget.checked)
                  }
                />{" "}
                Je reconnais l’absence de rédaction Bridge liée pour les
                sections signalées.
              </label>
            </div>
          ) : null}

          {citationViolations.length > 0 ? (
            <ul className="error-message" aria-label="Violations de citation">
              {citationViolations.map((violation, index) => (
                <li key={`${violation.code}-${index}`}>
                  {violation.code} · {violation.block_type} ·{" "}
                  {violation.message}
                </li>
              ))}
            </ul>
          ) : null}

          {opinion.status === "draft" ? (
            <fieldset>
              <legend>Approbation de la version précise</legend>
              <label htmlFor="opinion-approval-reason">
                Raison de la décision d’approbation (obligatoire, journalisée)
              </label>
              <textarea
                id="opinion-approval-reason"
                value={reason}
                onChange={(event) => setReason(event.currentTarget.value)}
              />
              <p>
                Version {opinion.version} · SHA-256{" "}
                <code>{opinion.content_sha256}</code>
              </p>
              <button
                type="button"
                className="button button--secondary"
                disabled={!canApprove}
                aria-busy={approve.isPending}
                onClick={approveOpinion}
              >
                {approve.isPending
                  ? "Approbation…"
                  : `Approuver l’avis v${
                      opinion.version
                    } · SHA-256 ${shortHash(opinion.content_sha256)}`}
              </button>
              {approve.isError ? (
                <p className="error-message" role="alert">
                  Approbation refusée :{" "}
                  {describeApiError(approve.error).message} · code :{" "}
                  {describeApiError(approve.error).code}
                </p>
              ) : null}
              {approve.isSuccess ? (
                <p role="status">
                  Version approuvée : v{approve.data.approved_version} (revue de
                  la v{approve.data.reviewed_version}). Aucune publication n’a
                  été créée.
                </p>
              ) : null}
            </fieldset>
          ) : null}

          {opinion.status === "approved" ? (
            <section aria-labelledby="opinion-publication-heading">
              <h4 id="opinion-publication-heading">
                Publication : décision humaine distincte
              </h4>
              <p>
                Proposer une publication crée un nouvel artefact PUBLICATION.
                L’approbation n’a créé aucune publication. Le réépinglage dans
                la composition reste une action humaine distincte : cette vue ne
                publie ni ne réépingle jamais automatiquement.
              </p>
              <button
                type="button"
                className="button button--secondary"
                disabled={!canPublish}
                aria-busy={publish.isPending}
                onClick={publishOpinion}
              >
                {publish.isPending
                  ? "Publication en cours…"
                  : `Proposer une PUBLICATION depuis l’avis v${opinion.version}`}
              </button>
              {publish.isError ? (
                <p className="error-message" role="alert">
                  Publication refusée :{" "}
                  {describeApiError(publish.error).message} · code :{" "}
                  {describeApiError(publish.error).code}
                </p>
              ) : null}
              {published === null ? null : (
                <section className="verification-warning" role="status">
                  <h4>PUBLICATION proposée</h4>
                  <p>
                    Artefact {published.publication.artifact_id} · version{" "}
                    {published.publication.version} · SHA-256{" "}
                    <code>{published.publication.sha256}</code>
                  </p>
                  <p>{published.message}</p>
                  {published.composition_repin_required ? (
                    <p>
                      Réépinglage requis dans la composition : décision humaine
                      distincte, non effectuée ici.
                    </p>
                  ) : null}
                </section>
              )}
            </section>
          ) : null}

          {opinion.status === "rejected" ? (
            <p>
              Cet avis est rejeté ; aucune nouvelle décision n’est possible.
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}
