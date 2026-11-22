import { ApiError } from "../../api/editions";
import {
  acceptEditorialProposal,
  getEditorialProposal,
  prepareEnrichmentRevision,
  type EditorialEnrichmentRevisionInput,
  type EditorialProposalAcceptResult,
  type EditorialProposalRead,
} from "../../api/editorialWorkspace";

export interface ManualVisualRevisionOutcome {
  /** Latest proposal state returned by the server. */
  proposal: EditorialProposalRead;
  /**
   * The adoption result, or `null` when the proposal was already accepted
   * (the change committed even though the previous accept response was lost).
   */
  acceptance: EditorialProposalAcceptResult | null;
}

export interface PrepareAndAcceptKeys {
  /** Stable until the whole sequence succeeds. */
  prepareIdempotencyKey: string;
  /** Stable for the same accept decision, so a replay is idempotent. */
  acceptIdempotencyKey?: string;
}

const STATE_CONFLICT_CODE = "proposal_state_conflict";

/**
 * Prepare one typed manual visual revision and adopt it through the single
 * adoption writer. Replay-safe:
 *
 * - a stable prepare key makes a retry return the same proposal, including one
 *   already `accepted` after a lost accept response;
 * - a stable accept key lets the server replay the persisted acceptance
 *   instead of answering `proposal_state_conflict`;
 * - if a state conflict still surfaces, the proposal is re-read and reconciled
 *   before any failure is reported.
 *
 * Caption, move, reorder, remove and replace all go through this helper.
 */
export async function prepareAndAcceptVisualRevision(
  subjectId: string,
  occurrenceId: string,
  revision: EditorialEnrichmentRevisionInput,
  keys: PrepareAndAcceptKeys,
): Promise<ManualVisualRevisionOutcome> {
  const proposal = await prepareEnrichmentRevision(
    subjectId,
    occurrenceId,
    keys.prepareIdempotencyKey,
    revision,
  );
  if (proposal.state === "accepted") {
    // The earlier attempt committed; the stable prepare key replayed it.
    return { proposal, acceptance: null };
  }
  if (!proposal.patch_sha256) {
    throw new Error("L’empreinte du patch est indisponible.");
  }
  try {
    const acceptance = await acceptEditorialProposal(
      subjectId,
      occurrenceId,
      proposal.id,
      {
        expected_state_version: proposal.state_version,
        patch_sha256: proposal.patch_sha256,
      },
      keys.acceptIdempotencyKey,
    );
    return { proposal, acceptance };
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== STATE_CONFLICT_CODE) {
      throw error;
    }
    let current: EditorialProposalRead;
    try {
      current = await getEditorialProposal(
        subjectId,
        occurrenceId,
        proposal.id,
      );
    } catch {
      throw error;
    }
    if (current.state === "accepted") {
      return { proposal: current, acceptance: null };
    }
    if (
      current.state === "proposed" &&
      current.patch_sha256 !== null &&
      current.patch_sha256 === proposal.patch_sha256
    ) {
      // A benign state-version bump keeps the same patch proposed; retry once
      // against the reconciled version instead of reporting a false conflict.
      const acceptance = await acceptEditorialProposal(
        subjectId,
        occurrenceId,
        proposal.id,
        {
          expected_state_version: current.state_version,
          patch_sha256: current.patch_sha256,
        },
        keys.acceptIdempotencyKey,
      );
      return { proposal: current, acceptance };
    }
    throw error;
  }
}
