import type {
  NoteProposalStatus,
  NoteProposalViewV1,
} from "../api/opinionProposals";
import { describeApiError } from "./errors";
import {
  BLOCK_LABELS,
  CONSULTATION_LABELS,
  PROPOSAL_STATUS_LABELS,
  TARGET_BLOCK_LABELS,
  shortHash,
} from "./labels";

const STATUS_ORDER: NoteProposalStatus[] = [
  "proposed",
  "accepted",
  "rejected",
  "stale",
  "cancelled",
];

export function ProposalList({
  proposals,
  isPending,
  error,
  selectedProposalId,
  onSelect,
  statusFilter,
  onStatusFilterChange,
}: {
  proposals: NoteProposalViewV1[];
  isPending: boolean;
  error: unknown;
  selectedProposalId: string | null;
  onSelect: (proposalId: string) => void;
  statusFilter: NoteProposalStatus | "";
  onStatusFilterChange: (status: NoteProposalStatus | "") => void;
}) {
  return (
    <section className="job-card" aria-labelledby="opinion-proposals-heading">
      <h3 id="opinion-proposals-heading">Propositions Bridge</h3>
      <label htmlFor="opinion-proposal-status">Filtrer par statut</label>
      <select
        id="opinion-proposal-status"
        value={statusFilter}
        onChange={(event) =>
          onStatusFilterChange(
            event.currentTarget.value as NoteProposalStatus | "",
          )
        }
      >
        <option value="">Tous les statuts</option>
        {STATUS_ORDER.map((status) => (
          <option key={status} value={status}>
            {PROPOSAL_STATUS_LABELS[status]}
          </option>
        ))}
      </select>

      {isPending ? (
        <p role="status">Chargement des propositions…</p>
      ) : error !== null && error !== undefined ? (
        <p className="error-message" role="alert">
          Impossible de charger les propositions :{" "}
          {describeApiError(error).message} · code :{" "}
          {describeApiError(error).code}
        </p>
      ) : proposals.length === 0 ? (
        <p>Aucune proposition pour ce filtre.</p>
      ) : (
        <ul>
          {proposals.map((proposal) => (
            <li key={proposal.id}>
              <p>
                <strong>{PROPOSAL_STATUS_LABELS[proposal.status]}</strong> ·
                ancre <code>{proposal.anchor}</code> ·{" "}
                {TARGET_BLOCK_LABELS[proposal.target_block_type]} ·{" "}
                {BLOCK_LABELS[proposal.bridge_block_type]} ·{" "}
                {CONSULTATION_LABELS[proposal.consultation_type] ??
                  proposal.consultation_type}
              </p>
              <p>
                Statut v{proposal.status_version} ·{" "}
                {proposal.materialised
                  ? `texte Bridge stocké · SHA-256 ${shortHash(
                      proposal.proposed_text_sha256,
                    )}`
                  : "aucun texte stocké (réponse Bridge en attente)"}
              </p>
              <button
                type="button"
                className="button button--secondary"
                aria-pressed={selectedProposalId === proposal.id}
                onClick={() => onSelect(proposal.id)}
              >
                Examiner la proposition
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
