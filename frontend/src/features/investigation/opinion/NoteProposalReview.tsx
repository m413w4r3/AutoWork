import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import { EditorialDiffView } from "../../editorial-workspace/EditorialDiffView";
import {
  useAcceptNoteProposal,
  useIngestNoteProposalResponse,
  useNoteProposal,
  useRejectNoteProposal,
  opinionProposalsKeys,
  type NoteAcceptanceViewV1,
} from "../api/opinionProposals";
import {
  describeApiError,
  isStaleProposalError,
  pendingIngestCode,
} from "./errors";
import {
  BLOCK_LABELS,
  CONSULTATION_LABELS,
  PROPOSAL_STATUS_LABELS,
  TARGET_BLOCK_LABELS,
  shortHash,
} from "./labels";
import { opinionNotesKeys } from "./queries";
import { ReferenceList } from "./ReferenceList";
import { findingLabel, observationLabel } from "./references";

export function NoteProposalReview({
  investigationId,
  proposalId,
  findings,
  observations,
  onClose,
}: {
  investigationId: string;
  proposalId: string;
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const query = useNoteProposal(investigationId, proposalId);
  const ingest = useIngestNoteProposalResponse(investigationId, proposalId);
  const accept = useAcceptNoteProposal(investigationId, proposalId);
  const reject = useRejectNoteProposal(investigationId, proposalId);
  const [reason, setReason] = useState("");
  const [previewAcknowledged, setPreviewAcknowledged] = useState(false);
  const [accepted, setAccepted] = useState<NoteAcceptanceViewV1 | null>(null);
  const [ingestNotice, setIngestNotice] = useState<string | null>(null);
  const [ingestRefusal, setIngestRefusal] = useState<string | null>(null);

  const diff = query.data;

  function reload() {
    void queryClient.invalidateQueries({
      queryKey: opinionProposalsKeys.all(investigationId),
    });
    void queryClient.invalidateQueries({
      queryKey: opinionNotesKeys.detail(investigationId),
    });
  }

  function acceptProposal() {
    if (diff === undefined) return;
    const proposal = diff.proposal;
    const basePin =
      proposal.base_opinion_version !== null &&
      proposal.base_opinion_sha256 !== null
        ? {
            expected_base_version: proposal.base_opinion_version,
            expected_base_sha256: proposal.base_opinion_sha256,
          }
        : {};
    accept.mutate(
      {
        expected_status_version: proposal.status_version,
        ...basePin,
        ...(reason.trim() === "" ? {} : { reason: reason.trim() }),
      },
      {
        onSuccess: (view) => {
          setAccepted(view);
          reload();
        },
        onError: (error) => {
          if (isStaleProposalError(error)) reload();
        },
      },
    );
  }

  function rejectProposal() {
    if (diff === undefined) return;
    reject.mutate(
      {
        expected_status_version: diff.proposal.status_version,
        ...(reason.trim() === "" ? {} : { reason: reason.trim() }),
      },
      { onSuccess: reload },
    );
  }

  function ingestResponse() {
    if (diff === undefined) return;
    setIngestNotice(null);
    setIngestRefusal(null);
    ingest.mutate(
      { expected_status_version: diff.proposal.status_version },
      {
        onSuccess: (value) => {
          const pending = pendingIngestCode(value);
          if (pending !== null) {
            setIngestRefusal(`${pending.message} (code : ${pending.code})`);
            reload();
            return;
          }
          setIngestNotice(
            "Réponse Bridge ingérée : le texte stocké est prêt à être examiné.",
          );
          reload();
        },
      },
    );
  }

  if (query.isPending) {
    return (
      <section className="job-card" aria-labelledby="opinion-proposal-review">
        <h3 id="opinion-proposal-review">Examen de la proposition</h3>
        <p role="status">Chargement de la proposition…</p>
      </section>
    );
  }
  if (query.isError) {
    return (
      <section className="job-card" aria-labelledby="opinion-proposal-review">
        <h3 id="opinion-proposal-review">Examen de la proposition</h3>
        <p className="error-message" role="alert">
          Impossible de charger la proposition :{" "}
          {describeApiError(query.error).message} · code :{" "}
          {describeApiError(query.error).code}
        </p>
        <button type="button" onClick={onClose}>
          Fermer l’examen
        </button>
      </section>
    );
  }
  if (diff === undefined) return null;

  const proposal = diff.proposal;
  const acceptReady =
    diff.accept_ready &&
    !diff.stale &&
    proposal.materialised &&
    diff.proposed_text !== null;
  const canReject = proposal.status === "proposed";

  return (
    <section className="job-card" aria-labelledby="opinion-proposal-review">
      <h3 id="opinion-proposal-review">
        Proposition ciblée · ancre <code>{diff.anchor}</code>
      </h3>
      <button type="button" onClick={onClose}>
        Fermer l’examen
      </button>

      <dl>
        <div>
          <dt>Statut</dt>
          <dd>
            {PROPOSAL_STATUS_LABELS[proposal.status]} · v
            {proposal.status_version}
          </dd>
        </div>
        <div>
          <dt>Ancre et section touchée</dt>
          <dd>
            <code>{diff.anchor}</code> ·{" "}
            {TARGET_BLOCK_LABELS[diff.target_block_type]} ·{" "}
            {BLOCK_LABELS[proposal.bridge_block_type]}
          </dd>
        </div>
        <div>
          <dt>Action Bridge</dt>
          <dd>
            {CONSULTATION_LABELS[proposal.consultation_type] ??
              proposal.consultation_type}
          </dd>
        </div>
        <div>
          <dt>Version de base de l’avis</dt>
          <dd>
            {diff.current_opinion_version === null
              ? "Aucun avis existant"
              : `v${diff.current_opinion_version} · SHA-256 ${shortHash(
                  diff.current_opinion_sha256,
                )}`}
            {proposal.base_opinion_version === null
              ? ""
              : ` (base figée v${proposal.base_opinion_version} · ${shortHash(
                  proposal.base_opinion_sha256,
                )})`}
          </dd>
        </div>
        <div>
          <dt>Texte proposé SHA-256</dt>
          <dd>
            <code>{proposal.proposed_text_sha256 ?? "aucun texte stocké"}</code>
          </dd>
        </div>
        <div>
          <dt>Réponse Bridge</dt>
          <dd>
            invocation {proposal.bridge_invocation_id ?? "—"} · sortie{" "}
            <code>{shortHash(proposal.bridge_output_sha256)}</code> ·
            observation de réponse{" "}
            <code>{proposal.bridge_response_observation_id ?? "—"}</code>
          </dd>
        </div>
      </dl>

      {diff.stale || proposal.status === "stale" ? (
        <div className="verification-warning" role="alert">
          <p>
            Proposition périmée : l’avis a changé depuis la demande. Son texte
            ne peut plus être accepté.
          </p>
          <button type="button" onClick={reload}>
            Recharger les données
          </button>
        </div>
      ) : null}

      {!proposal.materialised ? (
        <div className="verification-warning" role="status">
          <p>
            Aucun texte stocké : la réponse Bridge n’a pas encore produit
            d’observation de réponse exploitable. Une proposition non
            matérialisée ne peut jamais être acceptée.
          </p>
          <button
            type="button"
            className="button button--secondary"
            disabled={proposal.status !== "proposed" || ingest.isPending}
            onClick={ingestResponse}
          >
            {ingest.isPending
              ? "Ingestion de la réponse…"
              : "Ingérer la réponse Bridge"}
          </button>
          {ingest.isError ? (
            <p className="error-message" role="alert">
              Réponse Bridge refusée : {describeApiError(ingest.error).message}{" "}
              · code : {describeApiError(ingest.error).code}
            </p>
          ) : null}
          {ingestNotice ? <p role="status">{ingestNotice}</p> : null}
          {ingestRefusal ? (
            <p className="error-message" role="alert">
              {ingestRefusal}
            </p>
          ) : null}
        </div>
      ) : null}

      <section aria-labelledby="opinion-current-note">
        <h4 id="opinion-current-note">Texte actuel à cette ancre</h4>
        {diff.current_note_text === null ? (
          <p>Aucune note existante à cette ancre.</p>
        ) : (
          <p>{diff.current_note_text}</p>
        )}
        {diff.current_note_id !== null ? (
          <p>
            L’acceptation remplace la note existante{" "}
            <code>{diff.current_note_id}</code> de cette ancre ; les notes des
            autres ancres ne sont pas modifiées.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="opinion-proposed-text">
        <h4 id="opinion-proposed-text">Proposition Bridge stockée</h4>
        {diff.proposed_text === null ? (
          <p>Aucun texte stocké pour l’instant.</p>
        ) : (
          <p>{diff.proposed_text}</p>
        )}
        {diff.proposed_text !== null && diff.current_note_text !== null ? (
          <EditorialDiffView
            baseText={diff.current_note_text}
            proposedText={diff.proposed_text}
          />
        ) : null}
      </section>

      <section aria-labelledby="opinion-proposal-references">
        <h4 id="opinion-proposal-references">Références et limites</h4>
        <ReferenceList
          title="Findings cités"
          ids={diff.cited_finding_refs}
          resolve={(id) => findingLabel(findings, id)}
        />
        <ReferenceList
          title="Observations citées"
          ids={diff.cited_observation_refs}
          resolve={(id) => observationLabel(observations, id)}
        />
        <h5>Limites signalées par Bridge</h5>
        {diff.limits.length === 0 ? (
          <p>Aucune limite transmise avec la réponse Bridge.</p>
        ) : (
          <ul>
            {diff.limits.map((limit) => (
              <li key={limit}>{limit}</li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="opinion-proposal-preview">
        <h4 id="opinion-proposal-preview">Aperçu du rendu de la note</h4>
        {diff.proposed_text === null ? (
          <p>Aucun rendu tant que le texte stocké n’existe pas.</p>
        ) : (
          <article aria-label="Rendu de la note proposée">
            <h5>Note de l’analyste · ancre {diff.anchor}</h5>
            <p>{diff.proposed_text}</p>
          </article>
        )}
        {diff.anchor_excerpt === null ? null : (
          <p>Extrait de l’article à cette ancre : « {diff.anchor_excerpt} »</p>
        )}
        <p>
          Ce rendu HTML montre le texte et son ancre. Les tableaux, graphiques,
          figures, légendes et placements de l’article sont rendus par l’aperçu
          PDF réel ci-dessous ; la proposition n’y entre qu’après acceptation.
        </p>
      </section>

      {accepted === null ? (
        <section aria-labelledby="opinion-accept-proposal">
          <h4 id="opinion-accept-proposal">Décision humaine d’acceptation</h4>
          <label htmlFor="opinion-proposal-reason">
            Raison de la décision (journalisée, jamais publiée)
          </label>
          <textarea
            id="opinion-proposal-reason"
            value={reason}
            onChange={(event) => setReason(event.currentTarget.value)}
          />
          <label>
            <input
              type="checkbox"
              checked={previewAcknowledged}
              onChange={(event) =>
                setPreviewAcknowledged(event.currentTarget.checked)
              }
            />{" "}
            J’ai examiné le texte stocké, le diff et l’aperçu du rendu de la
            proposition v{proposal.status_version} · SHA-256{" "}
            {shortHash(proposal.proposed_text_sha256)}.
          </label>
          <button
            type="button"
            className="button button--secondary"
            disabled={!acceptReady || !previewAcknowledged || accept.isPending}
            aria-busy={accept.isPending}
            onClick={acceptProposal}
          >
            {accept.isPending
              ? "Acceptation…"
              : `Accepter la proposition v${
                  proposal.status_version
                } · SHA-256 ${shortHash(proposal.proposed_text_sha256)}`}
          </button>{" "}
          <button
            type="button"
            className="button button--secondary"
            disabled={!canReject || reject.isPending}
            onClick={rejectProposal}
          >
            {reject.isPending ? "Refus…" : "Refuser la proposition"}
          </button>
          {accept.isError ? (
            <p className="error-message" role="alert">
              Acceptation refusée : {describeApiError(accept.error).message} ·
              code : {describeApiError(accept.error).code}
            </p>
          ) : null}
          {reject.isError ? (
            <p className="error-message" role="alert">
              Refus impossible : {describeApiError(reject.error).message} · code
              : {describeApiError(reject.error).code}
            </p>
          ) : null}
          {reject.isSuccess ? (
            <p role="status">
              Proposition refusée ; aucune version d’avis n’a été écrite.
            </p>
          ) : null}
        </section>
      ) : (
        <section className="verification-warning" role="status">
          <h4>Note acceptée</h4>
          <p>
            Avis v{accepted.opinion_version} · SHA-256{" "}
            <code>{accepted.opinion_sha256}</code> · note{" "}
            <code>{accepted.note_id}</code> · décision{" "}
            <code>{accepted.decision_id}</code>
            {accepted.idempotent_replay ? " · rejeu idempotent" : ""}.
          </p>
          <p>
            L’approbation de cette version et sa publication restent deux
            décisions humaines distinctes.
          </p>
        </section>
      )}
    </section>
  );
}
