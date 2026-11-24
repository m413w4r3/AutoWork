import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import { EditorialDiffView } from "../../editorial-workspace/EditorialDiffView";
import {
  articleAnchorsKeys,
  useArticleAnchors,
  type ArticleAnchorViewV1,
  type ArticleSectionViewV1,
} from "../api/articleAnchors";
import {
  useAcceptNoteProposal,
  useIngestNoteProposalResponse,
  useNoteProposal,
  useRejectNoteProposal,
  opinionProposalsKeys,
  type NoteAcceptanceViewV1,
} from "../api/opinionProposals";
import {
  BLOCK_LABELS,
  EVIDENCE_KIND_LABELS,
  FINDING_KIND_LABELS,
  PROPOSAL_STATUS_LABELS,
  truncate,
} from "./labels";
import { NoteContentBlocks } from "./NoteContentBlocks";
import { CopyableTechnicalValue } from "./CopyableTechnicalValue";
import { opinionNotesKeys } from "./queries";
import {
  describeApiError,
  isStaleProposalError,
  pendingIngestCode,
} from "./errors";
import type { BridgeCorrectionSeed } from "./BridgeRequestPanel";

function proposalLocationLabel(
  anchor: string,
  anchors: ArticleAnchorViewV1[],
  sections: ArticleSectionViewV1[],
): string {
  const item = anchors.find((candidate) => candidate.anchor === anchor);
  if (item === undefined) return "Emplacement de l’article non disponible";
  const paragraph = (item.paragraph_index ?? 0) + 1;
  if (item.section_index === null) return `Chapô, paragraphe ${paragraph}`;
  const heading = sections.find(
    (section) => section.section_index === item.section_index,
  )?.heading;
  return `Section ${item.section_index + 1}${heading ? ` · ${heading}` : ""}, paragraphe ${paragraph}`;
}

export function NoteProposalReview({
  investigationId,
  proposalId,
  findings,
  observations,
  subjectId,
  onClose,
  onRequestCorrection,
}: {
  investigationId: string;
  proposalId: string;
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
  subjectId: string | null;
  onClose: () => void;
  onRequestCorrection: (seed: BridgeCorrectionSeed) => void;
}) {
  const queryClient = useQueryClient();
  const query = useNoteProposal(investigationId, proposalId);
  const articleAnchorsQuery = useArticleAnchors(investigationId);
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
    // Content-block references resolve against the pinned publication base; a
    // stale reference may also mean the anchor catalog moved.
    void queryClient.invalidateQueries({
      queryKey: articleAnchorsKeys.all(investigationId),
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
    diff.content_blocks_valid &&
    !diff.stale &&
    proposal.materialised &&
    diff.proposed_text !== null;
  const canDecide = proposal.status === "proposed";
  const provenanceVerified =
    proposal.materialised &&
    diff.proposed_text !== null &&
    proposal.proposed_text_sha256 !== null &&
    proposal.bridge_invocation_id !== null &&
    proposal.bridge_output_sha256 !== null &&
    proposal.bridge_response_observation_id !== null;
  const statusLabel =
    diff.stale || proposal.status === "stale"
      ? "Périmée"
      : proposal.status === "proposed"
        ? proposal.materialised
          ? "Prête à examiner"
          : "Réponse Bridge en attente"
        : PROPOSAL_STATUS_LABELS[proposal.status];
  const location =
    proposalLocationLabel(
      diff.anchor,
      articleAnchorsQuery.data?.anchors ?? [],
      articleAnchorsQuery.data?.sections ?? [],
    ) ?? "Emplacement de l’article";

  function requestCorrection() {
    if (diff === undefined) return;
    onRequestCorrection({
      anchor: diff.anchor,
      blockType: proposal.bridge_block_type,
      contentBlocks: diff.content_blocks,
      findingRefs: diff.cited_finding_refs,
      observationRefs: diff.cited_observation_refs,
      noteState: proposal.note_state,
      noteRelation: proposal.note_relation,
      taskId: proposal.task_id,
    });
  }

  return (
    <section
      className="job-card opinion-proposal-review"
      aria-labelledby="opinion-proposal-review"
    >
      <header className="opinion-proposal-review__header">
        <h3 id="opinion-proposal-review">
          Proposition Bridge — version {proposal.status_version}
        </h3>
        <button type="button" onClick={onClose}>
          Fermer l’examen
        </button>
        <dl>
          <div>
            <dt>Emplacement</dt>
            <dd>
              {location}
              {diff.anchor_excerpt === null ? null : (
                <span> · « {diff.anchor_excerpt} »</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Statut</dt>
            <dd>{statusLabel}</dd>
          </div>
          <div>
            <dt>Sources citées</dt>
            <dd>
              {diff.cited_finding_refs.length}{" "}
              {diff.cited_finding_refs.length === 1 ? "finding" : "findings"} ·{" "}
              {diff.cited_observation_refs.length}{" "}
              {diff.cited_observation_refs.length === 1
                ? "observation"
                : "observations"}
            </dd>
          </div>
          <div>
            <dt>Provenance</dt>
            <dd>{provenanceVerified ? "Vérifiée" : "Non vérifiée"}</dd>
          </div>
        </dl>

        <details className="opinion-proposal-review__technical">
          <summary>Provenance technique</summary>
          <dl>
            <div>
              <dt>Proposition</dt>
              <dd>
                <CopyableTechnicalValue
                  label="ID de la proposition"
                  value={proposal.id}
                />{" "}
                · version de statut {proposal.status_version}
              </dd>
            </div>
            <div>
              <dt>Ancre exacte</dt>
              <dd>
                <CopyableTechnicalValue
                  label="Ancre exacte"
                  value={diff.anchor}
                />
              </dd>
            </div>
            <div>
              <dt>Bloc visé</dt>
              <dd>{BLOCK_LABELS[proposal.bridge_block_type]}</dd>
            </div>
            <div>
              <dt>Note remplacée</dt>
              <dd>
                {diff.current_note_id === null ? (
                  "Aucune"
                ) : (
                  <CopyableTechnicalValue
                    label="ID de la note remplacée"
                    value={diff.current_note_id}
                  />
                )}
              </dd>
            </div>
            <div>
              <dt>Version de base de l’avis</dt>
              <dd>
                {diff.current_opinion_version === null
                  ? "Aucun avis existant"
                  : `v${diff.current_opinion_version}`}{" "}
                · SHA-256{" "}
                {diff.current_opinion_sha256 === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="SHA-256 de l’avis actuel"
                    value={diff.current_opinion_sha256}
                  />
                )}
              </dd>
            </div>
            <div>
              <dt>Base épinglée par la proposition</dt>
              <dd>
                v{proposal.base_opinion_version ?? "—"} · SHA-256{" "}
                {proposal.base_opinion_sha256 === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="SHA-256 de l’avis de base"
                    value={proposal.base_opinion_sha256}
                  />
                )}
              </dd>
            </div>
            <div>
              <dt>Texte Bridge</dt>
              <dd>
                {proposal.proposed_text_sha256 === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="SHA-256 du texte Bridge"
                    value={proposal.proposed_text_sha256}
                  />
                )}
              </dd>
            </div>
            <div>
              <dt>Invocation et sortie Bridge</dt>
              <dd>
                {proposal.bridge_invocation_id === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="ID de l’invocation Bridge"
                    value={proposal.bridge_invocation_id}
                  />
                )}{" "}
                · SHA-256{" "}
                {proposal.bridge_output_sha256 === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="SHA-256 de la sortie Bridge"
                    value={proposal.bridge_output_sha256}
                  />
                )}
              </dd>
            </div>
            <div>
              <dt>Observation de réponse</dt>
              <dd>
                {proposal.bridge_response_observation_id === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="ID de l’observation de réponse"
                    value={proposal.bridge_response_observation_id}
                  />
                )}
              </dd>
            </div>
            <div>
              <dt>Base de publication</dt>
              <dd>
                {proposal.base_publication_artifact_id === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="ID de la base de publication"
                    value={proposal.base_publication_artifact_id}
                  />
                )}{" "}
                · v{proposal.base_publication_version ?? "—"} · SHA-256{" "}
                {proposal.base_publication_sha256 === null ? (
                  "—"
                ) : (
                  <CopyableTechnicalValue
                    label="SHA-256 de la base de publication"
                    value={proposal.base_publication_sha256}
                  />
                )}
              </dd>
            </div>
            <div>
              <dt>Findings cités</dt>
              <dd>
                {diff.cited_finding_refs.length === 0
                  ? "Aucun"
                  : diff.cited_finding_refs.map((id) => (
                      <CopyableTechnicalValue
                        key={id}
                        label="ID du finding cité"
                        value={id}
                      />
                    ))}
              </dd>
            </div>
            <div>
              <dt>Observations citées</dt>
              <dd>
                {diff.cited_observation_refs.length === 0
                  ? "Aucune"
                  : diff.cited_observation_refs.map((id) => (
                      <CopyableTechnicalValue
                        key={id}
                        label="ID de l’observation citée"
                        value={id}
                      />
                    ))}
              </dd>
            </div>
            {accepted === null ? null : (
              <div>
                <dt>Décision d’acceptation</dt>
                <dd>
                  Version d’avis {accepted.opinion_version} · SHA-256{" "}
                  <CopyableTechnicalValue
                    label="SHA-256 de l’avis accepté"
                    value={accepted.opinion_sha256}
                  />{" "}
                  · note{" "}
                  <CopyableTechnicalValue
                    label="ID de la note acceptée"
                    value={accepted.note_id}
                  />{" "}
                  · décision{" "}
                  <CopyableTechnicalValue
                    label="ID de la décision"
                    value={accepted.decision_id}
                  />
                </dd>
              </div>
            )}
          </dl>
        </details>

        {diff.stale || proposal.status === "stale" ? (
          <div className="verification-warning" role="alert">
            <p>
              Proposition périmée : l’avis a changé depuis la demande. Son texte
              ne peut plus être accepté.
            </p>
            <button type="button" onClick={reload}>
              Recharger la proposition
            </button>
          </div>
        ) : null}
      </header>

      <section aria-labelledby="opinion-proposed-text">
        <h4 id="opinion-proposed-text">Texte proposé</h4>
        {diff.proposed_text === null ? (
          <>
            <p>Aucun texte Bridge stocké pour l’instant.</p>
            {!proposal.materialised ? (
              <div className="verification-warning" role="status">
                <p>
                  La réponse Bridge n’a pas encore produit d’observation
                  exploitable.
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
                    Réponse Bridge refusée :{" "}
                    {describeApiError(ingest.error).message} · code :{" "}
                    {describeApiError(ingest.error).code}
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
          </>
        ) : (
          <p>{diff.proposed_text}</p>
        )}
      </section>

      {diff.current_note_text === null ? null : (
        <section aria-labelledby="opinion-proposal-diff">
          <h4 id="opinion-proposal-diff">Diff avec la Note actuelle</h4>
          {diff.proposed_text === null ? (
            <p>Le diff sera disponible quand le texte Bridge sera stocké.</p>
          ) : (
            <EditorialDiffView
              baseText={diff.current_note_text}
              proposedText={diff.proposed_text}
              baseLabel="Note actuelle"
            />
          )}
        </section>
      )}

      <section aria-labelledby="opinion-proposal-references">
        <h4 id="opinion-proposal-references">Références citées</h4>
        {diff.cited_finding_refs.length === 0 &&
        diff.cited_observation_refs.length === 0 ? (
          <p>Aucune référence citée.</p>
        ) : (
          <ul>
            {diff.cited_finding_refs.map((id) => {
              const finding = findings.find((item) => item.id === id);
              return (
                <li key={id}>
                  {finding === undefined
                    ? "Finding indisponible dans cette enquête"
                    : `${FINDING_KIND_LABELS[finding.kind] ?? finding.kind} · ${truncate(finding.statement)}`}
                </li>
              );
            })}
            {diff.cited_observation_refs.map((id) => {
              const observation = observations.find((item) => item.id === id);
              return (
                <li key={id}>
                  {observation === undefined
                    ? "Observation indisponible dans cette enquête"
                    : (EVIDENCE_KIND_LABELS[observation.kind] ??
                        observation.kind) +
                      " · " +
                      observation.source +
                      " · " +
                      observation.object_type +
                      "/" +
                      truncate(observation.object_id, 12)}
                </li>
              );
            })}
          </ul>
        )}
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
        <h4 id="opinion-proposal-preview">Aperçu à cet emplacement</h4>
        {diff.proposed_text === null ? (
          <p>Aucun aperçu tant que le texte stocké n’existe pas.</p>
        ) : (
          <article aria-label="Rendu de la note proposée">
            <h5>Note de l’analyste</h5>
            <p>{diff.proposed_text}</p>
          </article>
        )}
        {diff.anchor_excerpt === null ? null : (
          <blockquote>
            <p>Paragraphe de l’article à cet emplacement</p>
            <p>{diff.anchor_excerpt}</p>
          </blockquote>
        )}
        <NoteContentBlocks
          investigationId={investigationId}
          anchor={diff.anchor}
          observations={observations}
          blocks={diff.content_blocks}
          resolvedBlocks={diff.resolved_content_blocks}
          contentBlockError={diff.content_block_error}
          basePublication={{
            artifactId: proposal.base_publication_artifact_id,
            version: proposal.base_publication_version,
            sha256: proposal.base_publication_sha256,
          }}
          subjectId={subjectId}
          onReload={reload}
        />
      </section>

      {accepted === null ? (
        <section
          className="opinion-proposal-review__decision"
          aria-labelledby="opinion-proposal-decision"
        >
          <h4 id="opinion-proposal-decision">Décision</h4>
          <details>
            <summary>Motif de décision facultatif</summary>
            <label htmlFor="opinion-proposal-reason">
              Motif (journalisé et jamais publié)
            </label>
            <textarea
              id="opinion-proposal-reason"
              value={reason}
              onChange={(event) => setReason(event.currentTarget.value)}
            />
          </details>
          <label className="opinion-proposal-review__attestation">
            <input
              type="checkbox"
              checked={previewAcknowledged}
              onChange={(event) =>
                setPreviewAcknowledged(event.currentTarget.checked)
              }
            />{" "}
            J’ai relu ce texte et ses références ; je confirme qu’il peut être
            publié à cet emplacement.
          </label>
          <div
            className="opinion-proposal-review__actions"
            aria-label="Actions de décision"
          >
            <button
              type="button"
              className="button button--secondary"
              disabled={
                !acceptReady || !previewAcknowledged || accept.isPending
              }
              aria-busy={accept.isPending}
              onClick={acceptProposal}
            >
              {accept.isPending ? "Acceptation…" : "Accepter"}
            </button>
            <button
              type="button"
              className="button button--secondary"
              disabled={!canDecide}
              onClick={requestCorrection}
            >
              Demander une correction à Bridge
            </button>
            <button
              type="button"
              className="button button--secondary"
              disabled={!canDecide || reject.isPending}
              onClick={rejectProposal}
            >
              {reject.isPending ? "Refus…" : "Refuser"}
            </button>
          </div>
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
            Avis v{accepted.opinion_version} enregistré. L’approbation de cette
            version et sa publication restent deux décisions humaines
            distinctes.
          </p>
        </section>
      )}
    </section>
  );
}
