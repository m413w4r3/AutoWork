import { useQuery } from "@tanstack/react-query";

import {
  getEditorialProposal,
  type EditorialProposalRefusalCode,
  type EditorialProposalState,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

interface EditorialProposalCardProps {
  subjectId: string;
  occurrenceId: string;
  proposalId: string;
}

const stateLabels: Record<EditorialProposalState, string> = {
  preparing: "Préparation en cours",
  proposed: "Proposition à consulter",
  refused: "Refus Bridge",
  needs_review: "Revue nécessaire",
  accepted: "Adoptée",
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
}: EditorialProposalCardProps) {
  const proposalQuery = useQuery({
    queryKey: editorialWorkspaceQueryKeys.proposal(
      subjectId,
      occurrenceId,
      proposalId,
    ),
    queryFn: () => getEditorialProposal(subjectId, occurrenceId, proposalId),
  });

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

  return (
    <article
      className="editorial-proposal-card"
      aria-label="Proposition éditoriale"
      aria-busy={proposalQuery.isFetching}
    >
      <header className="editorial-proposal-card__header">
        <h4>Proposition</h4>
        <span>{stateLabels[proposal.state]}</span>
      </header>
      <p className="editorial-proposal-card__base">
        Base canonique · version {proposal.base_artifact_version}
      </p>
      {typeof proposedText === "string" ? (
        <div className="editorial-proposal-card__content">
          <h5>Texte proposé</h5>
          <p>{proposedText}</p>
        </div>
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
