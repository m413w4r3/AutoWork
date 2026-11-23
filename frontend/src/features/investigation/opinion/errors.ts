import { InvestigationApiError } from "../../../api/investigation";
import { WorkshopApiError } from "../api/client";

export interface DescribedApiError {
  code: string;
  status: number;
  message: string;
}

const MESSAGES: Record<string, string> = {
  network_error:
    "Bridge ou l’API est indisponible ; aucune écriture n’a été effectuée.",
  bridge_checkpoint_pending:
    "Un point de contrôle Bridge est en attente ; réessayez après sa résolution.",
  bridge_response_pending:
    "Bridge n’a pas encore renvoyé de réponse exploitable ; la proposition reste en attente.",
  bridge_response_unusable:
    "La réponse Bridge est inutilisable : aucune observation de réponse ne peut fonder une note.",
  bridge_provenance_invalid:
    "La provenance Bridge de cette proposition est invalide.",
  bridge_authorship_required:
    "Le serveur exige une provenance Bridge pour ce texte ; la saisie libre est refusée.",
  bridge_prose_forbidden:
    "La prose non issue de Bridge est refusée sur cette enquête.",
  bridge_modified_prose_forbidden:
    "La prose Bridge modifiée ne peut pas être acceptée.",
  external_disclosure_denied:
    "La divulgation externe est refusée par l’enveloppe G2.",
  operator_mode_required:
    "L’enquête n’est pas en mode opérateur ; la consultation Bridge est impossible.",
  investigation_not_started:
    "L’enquête n’a pas encore démarré ; la demande Bridge est impossible.",
  stale_working_state:
    "L’état de travail a changé depuis l’affichage ; rechargez avant de redemander.",
  stale_note_proposal:
    "Proposition périmée : l’avis a changé depuis la demande. Rechargement des données…",
  stale_version:
    "Version périmée : une version plus récente existe. Rechargement des données…",
  stale_base:
    "La base de publication a changé depuis la proposition. Rechargez les ancres et la proposition.",
  version_hash_mismatch:
    "La version et le hash ne correspondent pas à la proposition affichée.",
  invalid_note_anchor:
    "Ancre invalide ou absente de la base de publication (lead:NNNN ou section:I:paragraph:NNNN). Rechargez les ancres.",
  invalid_observation_anchor:
    "Ancre absente de la base de publication épinglée de l’avis. Rechargez les ancres avant de redemander.",
  note_anchor_text_changed:
    "Le texte du paragraphe ciblé a changé depuis la proposition. Rechargez les ancres et la proposition.",
  note_block_ref_stale:
    "Un élément d’article référencé a changé (hash ou ancre). Rechargez les ancres et la proposition.",
  note_code_ref_stale:
    "Une référence de code ne correspond plus aux octets de l’observation. Rechargez la proposition.",
  note_chart_without_evidence:
    "Un graphique référencé ne porte pas de preuve. Rechargez la proposition et faites-la réviser.",
  note_figure_source_missing:
    "Une figure référencée n’a pas de source connue dans le document. Rechargez la proposition.",
  note_ioc_ref_unresolved:
    "Une référence IOC ne résout aucune observation de cette enquête. Rechargez la proposition.",
  invalid_note_target: "Le type de bloc cible ne correspond pas à l’ancre.",
  invalid_note_citation:
    "Une référence citée est invalide ou n’appartient pas à cette enquête.",
  advisory_observation_not_evidence:
    "Une observation consultative ne peut jamais être citée comme preuve.",
  citations_invalid:
    "Les citations de la note acceptée ne sont pas vérifiables.",
  invalid_note_proposal_status:
    "Le statut de la proposition ne permet pas cette action.",
  invalid_opinion_state: "L’état de l’avis ne permet pas cette action.",
  note_proposal_not_found: "La proposition n’existe plus pour cette enquête.",
  opinion_not_found: "Aucun avis n’existe encore pour cette enquête.",
  bridge_drafting_ack_required:
    "La couverture de rédaction Bridge doit être reconnue avant l’approbation.",
  idempotency_conflict:
    "Conflit d’idempotence : la demande a déjà été enregistrée avec un autre contenu.",
  idempotency_key_invalid: "Clé d’idempotence invalide.",
  idempotency_scope_invalid: "Portée d’idempotence invalide.",
  operator_payload_invalid_or_sensitive:
    "Le contenu de la demande est refusé (données sensibles ou invalides).",
  requester_actor_id_required: "Identité d’auteur requise par le serveur.",
  invocation_attribution_invalid: "L’attribution de l’invocation est invalide.",
  investigation_not_found: "Enquête introuvable.",
  investigation_api_error:
    "L’API a refusé la requête sans code métier exploitable.",
  verified_human_operator_required:
    "Une identité humaine vérifiée est requise pour cette action.",
};

function recordOf(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function describeApiError(error: unknown): DescribedApiError {
  if (error instanceof WorkshopApiError) {
    return {
      code: error.code,
      status: error.status,
      message: MESSAGES[error.code] ?? error.message,
    };
  }
  if (error instanceof InvestigationApiError) {
    return {
      code: error.code,
      status: error.status,
      message: MESSAGES[error.code] ?? error.message,
    };
  }
  return {
    code: "network_error",
    status: 0,
    message: MESSAGES.network_error ?? "Erreur inconnue.",
  };
}

export function isStaleProposalError(error: unknown): boolean {
  const described = describeApiError(error);
  return (
    described.code === "stale_note_proposal" ||
    described.code === "stale_version" ||
    described.code === "stale_base" ||
    described.code === "version_hash_mismatch"
  );
}

export function isBridgeAcknowledgementError(error: unknown): boolean {
  return describeApiError(error).code === "bridge_drafting_ack_required";
}

/**
 * Refusals that invalidate the displayed anchor space or its pinned base: the
 * picker must be reloaded before another drafting request.
 */
const ANCHOR_RELOAD_CODES = new Set<string>([
  "invalid_note_anchor",
  "invalid_observation_anchor",
  "note_anchor_text_changed",
  "note_block_ref_stale",
  "note_code_ref_stale",
  "stale_base",
]);

export function isAnchorReloadError(error: unknown): boolean {
  return ANCHOR_RELOAD_CODES.has(describeApiError(error).code);
}

/**
 * The ingest route may answer 202 with a `detail.code` body when Bridge has
 * not produced a persisted answer yet; 202 is a transport success, so the
 * view has to read that body instead of trusting the declared shape.
 */
export function pendingIngestCode(value: unknown): DescribedApiError | null {
  const record = recordOf(value);
  const detail = recordOf(record?.detail);
  const code = detail?.code;
  if (typeof code !== "string") return null;
  const message = detail?.message;
  return {
    code,
    status: 202,
    message:
      MESSAGES[code] ??
      (typeof message === "string" && message.length > 0
        ? message
        : "Bridge n’a pas encore renvoyé de réponse exploitable."),
  };
}
