import { ApiError } from "../../api/editions";
import type {
  ProductionBatchPhase,
  ProductionRunStatus,
} from "../../api/production";

export const STATUS_LABELS: Record<ProductionRunStatus, string> = {
  queued: "En attente",
  running: "En cours",
  ready: "Prêt",
  needs_review: "À vérifier",
  failed: "Échec",
  cancelled: "Annulé",
};

export { PRODUCTION_STAGE_LABELS as STAGE_LABELS } from "./productionStages";

export const PHASE_LABELS: Record<ProductionBatchPhase, string> = {
  initial: "Production initiale",
  recovery: "Récupération automatique",
  review: "Finalisation",
};

/** Why the production board refuses to start a Subject right now. */
export const BLOCKING_REASON_LABELS: Record<string, string> = {
  production_edition_archived: "Édition archivée : lecture seule.",
  production_batch_active: "Un lot de production est déjà en cours.",
  production_subject_active: "Une production de ce sujet est déjà en cours.",
};

/** Operator-facing messages for the production batch command refusals. */
export const PRODUCTION_ERROR_LABELS: Record<string, string> = {
  production_edition_archived:
    "L’édition est archivée : aucune production ne peut être démarrée.",
  production_batch_active:
    "Un lot de production est déjà en cours. Attendez sa fin avant d’en lancer un autre.",
  production_subject_active:
    "Au moins un sujet sélectionné a déjà une production en cours.",
  production_idempotency_conflict:
    "Cette demande a déjà été utilisée pour un autre lot. Relancez la sélection.",
  production_subject_not_found: "Un sujet sélectionné n’existe plus.",
  production_subject_discovery_origin_missing:
    "Un sujet sélectionné n’a pas d’origine Discovery.",
  production_subject_lineage_unavailable:
    "La lignée Discovery d’un sujet sélectionné est indisponible.",
};

export function blockingReasonLabel(reason: string): string {
  return BLOCKING_REASON_LABELS[reason] ?? reason;
}

export function productionErrorMessage(
  error: unknown,
  fallback: string,
): string {
  if (error instanceof ApiError) {
    return PRODUCTION_ERROR_LABELS[error.code] ?? error.message;
  }
  return error instanceof Error ? error.message : fallback;
}

/** Operator-facing explanation for a production run requiring intervention. */
export function productionIssueMessage(
  errorCode: string | null,
): string | null {
  switch (errorCode) {
    case "publication_routing_needs_review":
      return "Le routage de publication est bloqué : consultez le diagnostic des sources avant de poursuivre.";
    case "brief_chronology_unavailable":
      return "La chronologie de la brève est indisponible : une acquisition de source datée est nécessaire.";
    case "synthesis_output_invalid":
      return "La proposition de synthèse n’a pas pu être validée ou relue depuis son archive. Un tour correctif peut ne pas avoir eu lieu. Une relance ouvre une nouvelle génération et peut déclencher un nouvel appel modèle facturable.";
    case "synthesis_correction_context_too_large":
      return "Le contexte correctif dépasse le budget de 512 Kio. Aucun contenu n’a été tronqué et aucun second appel n’a été fait. Réduisez le périmètre des preuves ou demandez une révision ciblée. Une relance ouvre une nouvelle génération et peut déclencher un nouvel appel modèle facturable.";
    case "synthesis_correction_changed_untargeted_content":
      return "Le tour correctif a modifié des affirmations qui n’étaient pas visées par la demande et a été refusé. Une relance ouvre une nouvelle génération et peut déclencher un nouvel appel modèle facturable.";
    case "synthesis_corrective_parent_identity_mismatch":
      return "L’identité du run initial de synthèse n’a pas pu être vérifiée ; aucun appel correctif n’a été envoyé. Une relance ouvre une nouvelle génération et peut déclencher un nouvel appel modèle facturable.";
    case "synthesis_corrective_parent_raw_output_hash_mismatch":
      return "L’empreinte de la sortie brute du run initial n’a pas pu être vérifiée ; aucun appel correctif n’a été envoyé. Une relance ouvre une nouvelle génération et peut déclencher un nouvel appel modèle facturable.";
    case "model_submission_reconciliation_required":
      return "La soumission du modèle doit être réconciliée avant de poursuivre.";
    case "bridge_timeout":
    case "bridge_server_error":
    case "bridge_ui_timeout":
    case "bridge_idle_timeout":
    case "bridge_total_timeout":
    case "conversation_unavailable":
    case "conversation_profile_mismatch":
    case "conversation_busy":
      return "L’appel au modèle n’a pas abouti. Le système ne renvoie pas de lui-même une soumission dont l’état est incertain. Si un panneau de réconciliation apparaît, utilisez-le ; sinon, relancer l’étape est possible, et le système ne réémettra jamais de lui-même une soumission incertaine.";
    default:
      return null;
  }
}

/** Retry only transport failures: a refusal answers the same way every time. */
export function retryTransientProductionFailure(
  failureCount: number,
  error: unknown,
): boolean {
  return !(error instanceof ApiError && error.status < 500) && failureCount < 2;
}
