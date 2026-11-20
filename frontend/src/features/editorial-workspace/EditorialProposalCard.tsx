import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { ApiError } from "../../api/editions";
import {
  acceptEditorialProposal,
  getEditorialProposal,
  rejectEditorialProposal,
  type EditorialProposalRead,
  type EditorialProposalRefusalCode,
  type EditorialProposalState,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";
import { EditorialDiffView } from "./EditorialDiffView";

interface EditorialProposalCardProps {
  subjectId: string;
  occurrenceId: string;
  proposalId: string;
  referenceOrdinal?: number;
  onRequestVariant?: () => void;
  onAccepted?: () => void;
}

const stateLabels: Record<EditorialProposalState, string> = {
  preparing: "Préparation en cours",
  proposed: "Proposition à consulter",
  refused: "Refus Bridge",
  needs_review: "Revue nécessaire",
  accepted: "Acceptée",
  rejected: "Rejetée",
  stale: "Périmée",
};

const refusalLabels: Record<EditorialProposalRefusalCode, string> = {
  revision_not_applicable:
    "Les données disponibles ne permettent pas de proposer cette révision.",
};

function refusalLabel(code: unknown): string {
  if (code === "revision_not_applicable") {
    return refusalLabels[code];
  }
  return "Le Bridge n’a pas produit de proposition exploitable.";
}

export function EditorialProposalCard({
  subjectId,
  occurrenceId,
  proposalId,
  referenceOrdinal,
  onRequestVariant,
  onAccepted,
}: EditorialProposalCardProps) {
  const queryClient = useQueryClient();
  const acceptButtonRef = useRef<HTMLButtonElement>(null);
  const restoreAcceptFocus = useRef(false);
  const proposalQueryKey = editorialWorkspaceQueryKeys.proposal(
    subjectId,
    occurrenceId,
    proposalId,
  );
  const proposalQuery = useQuery({
    queryKey: proposalQueryKey,
    queryFn: () => getEditorialProposal(subjectId, occurrenceId, proposalId),
  });
  const invalidateDecisionContext = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.snapshot(subjectId),
      }),
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.threadsForOccurrence(
          subjectId,
          occurrenceId,
        ),
      }),
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.proposalsForOccurrence(
          subjectId,
          occurrenceId,
        ),
      }),
    ]);
  };
  const acceptMutation = useMutation({
    mutationFn: () => {
      const proposal = proposalQuery.data;
      if (!proposal?.patch_sha256) {
        throw new Error("L’empreinte du patch est indisponible.");
      }
      return acceptEditorialProposal(subjectId, occurrenceId, proposalId, {
        expected_state_version: proposal.state_version,
        patch_sha256: proposal.patch_sha256,
      });
    },
    onSuccess: async (accepted) => {
      queryClient.setQueryData<EditorialProposalRead>(
        proposalQueryKey,
        (current) =>
          current
            ? {
                ...current,
                state: "accepted",
                state_version: accepted.state_version,
              }
            : current,
      );
      await invalidateDecisionContext();
      onAccepted?.();
    },
  });
  const rejectMutation = useMutation({
    mutationFn: () => {
      const proposal = proposalQuery.data;
      if (!proposal?.patch_sha256) {
        throw new Error("L’empreinte du patch est indisponible.");
      }
      return rejectEditorialProposal(subjectId, occurrenceId, proposalId, {
        expected_state_version: proposal.state_version,
        patch_sha256: proposal.patch_sha256,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: proposalQueryKey,
      });
    },
  });

  useEffect(() => {
    if (acceptMutation.isSuccess && restoreAcceptFocus.current) {
      restoreAcceptFocus.current = false;
      acceptButtonRef.current?.focus({ preventScroll: true });
    }
  }, [acceptMutation.isSuccess]);

  if (proposalQuery.isPending) {
    return (
      <p className="editorial-proposal-card__status" role="status">
        Chargement de la proposition…
      </p>
    );
  }

  if (proposalQuery.isError) {
    return (
      <p className="editorial-proposal-card__status" role="alert">
        {proposalQuery.error.message}
      </p>
    );
  }

  const proposal = proposalQuery.data;
  const proposedText = proposal.patch?.after;
  const acceptanceConflict =
    acceptMutation.isError &&
    acceptMutation.error instanceof ApiError &&
    acceptMutation.error.status === 409;
  const recoverableProposal = proposal.state === "stale" || acceptanceConflict;
  const acceptanceComplete =
    proposal.state === "accepted" || acceptMutation.isSuccess;
  const decisionPending = acceptMutation.isPending || rejectMutation.isPending;

  return (
    <article
      className="editorial-proposal-card"
      aria-label="Proposition éditoriale"
      aria-busy={proposalQuery.isFetching}
    >
      <header className="editorial-proposal-card__header">
        <h4>
          {referenceOrdinal === undefined
            ? "Proposition"
            : `Proposition ${referenceOrdinal}`}
        </h4>
        <span>{stateLabels[proposal.state]}</span>
      </header>
      <p className="editorial-proposal-card__base">
        Base canonique · version {proposal.base_artifact_version}
      </p>
      {typeof proposedText === "string" ? (
        typeof proposal.base_text === "string" ? (
          <EditorialDiffView
            baseText={proposal.base_text}
            proposedText={proposedText}
            baseLabel={
              proposal.state === "stale"
                ? "Version de base"
                : "Version actuelle"
            }
          />
        ) : (
          <div className="editorial-proposal-card__content">
            <h5>Texte proposé</h5>
            <p>{proposedText}</p>
          </div>
        )
      ) : proposal.state === "refused" ? (
        <p className="editorial-proposal-card__status" role="status">
          {refusalLabel(proposal.diagnostics.code)}
        </p>
      ) : proposal.patch === null ? (
        <p className="editorial-proposal-card__status">
          Aucun patch éditorial n’est disponible pour cette proposition.
        </p>
      ) : (
        <p className="editorial-proposal-card__status">
          Patch éditorial typé disponible.
        </p>
      )}
      {typeof proposal.patch?.rationale === "string" ? (
        <p className="editorial-proposal-card__rationale">
          <strong>Justification · </strong>
          {proposal.patch.rationale}
        </p>
      ) : null}
      {proposal.state === "proposed" || proposal.state === "accepted" ? (
        <div className="editorial-proposal-card__actions">
          <button
            ref={acceptButtonRef}
            className="editorial-proposal-card__accept"
            type="button"
            aria-disabled={
              acceptanceComplete ||
              acceptanceConflict ||
              decisionPending ||
              !proposal.patch_sha256
            }
            disabled={decisionPending}
            onClick={() => {
              if (
                acceptanceComplete ||
                acceptanceConflict ||
                decisionPending ||
                !proposal.patch_sha256
              ) {
                return;
              }
              restoreAcceptFocus.current =
                !onAccepted &&
                document.activeElement === acceptButtonRef.current;
              acceptMutation.mutate();
            }}
          >
            {acceptanceComplete
              ? "Acceptée"
              : acceptMutation.isPending
                ? "Acceptation en cours…"
                : "Accepter"}
          </button>
          {proposal.state === "proposed" ? (
            <button
              className="editorial-proposal-card__reject"
              type="button"
              onClick={() => rejectMutation.mutate()}
              disabled={
                decisionPending || acceptanceConflict || !proposal.patch_sha256
              }
            >
              Rejeter
            </button>
          ) : null}
          {rejectMutation.isError ? (
            <p role="alert">{rejectMutation.error.message}</p>
          ) : null}
        </div>
      ) : null}
      {acceptMutation.isPending ? (
        <p
          className="editorial-proposal-card__status"
          role="status"
          aria-live="polite"
        >
          Acceptation en cours…
        </p>
      ) : null}
      {rejectMutation.isPending ? (
        <p
          className="editorial-proposal-card__status"
          role="status"
          aria-live="polite"
        >
          Rejet en cours…
        </p>
      ) : null}
      {acceptMutation.isSuccess ? (
        <p
          className="editorial-proposal-card__status"
          role="status"
          aria-live="polite"
        >
          Proposition acceptée. Le document et les éléments associés sont
          actualisés.
        </p>
      ) : null}
      {acceptMutation.isError && !acceptanceConflict ? (
        <p className="editorial-proposal-card__error" role="alert">
          {acceptMutation.error.message}
        </p>
      ) : null}
      {recoverableProposal ? (
        <div className="editorial-proposal-card__recovery">
          {acceptanceConflict || proposal.state === "stale" ? (
            <p role="alert" aria-live="polite">
              {acceptanceConflict
                ? acceptMutation.error.message
                : "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition."}
            </p>
          ) : null}
          <button
            className="button button--secondary"
            type="button"
            onClick={() => {
              acceptMutation.reset();
              void invalidateDecisionContext();
            }}
          >
            Comparer à nouveau
          </button>
          {onRequestVariant ? (
            <button
              className="button button--secondary"
              type="button"
              onClick={() => {
                acceptMutation.reset();
                onRequestVariant();
              }}
            >
              Demander une variante
            </button>
          ) : null}
        </div>
      ) : null}
      <details className="editorial-proposal-card__details">
        <summary>Détails de la proposition</summary>
        {proposal.patch?.enrichment_patch ? (
          <pre aria-label="Patch éditorial typé">
            {JSON.stringify(proposal.patch.enrichment_patch, null, 2)}
          </pre>
        ) : null}
        <dl>
          <div>
            <dt>Opération</dt>
            <dd>{proposal.operation}</dd>
          </div>
          <div>
            <dt>SHA-256 de la base</dt>
            <dd>{proposal.base_canonical_sha256}</dd>
          </div>
          {proposal.patch_sha256 ? (
            <div>
              <dt>SHA-256 du patch</dt>
              <dd>{proposal.patch_sha256}</dd>
            </div>
          ) : null}
        </dl>
      </details>
    </article>
  );
}
