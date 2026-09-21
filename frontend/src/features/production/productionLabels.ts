import { ApiError } from "../../api/editions";
import type {
  ProductionBatchPhase,
  ProductionStage,
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

export const STAGE_LABELS: Record<ProductionStage, string> = {
  sources: "Sources",
  references: "Références",
  extraction: "Extraction",
  synthesis: "Synthèse",
  assembly: "Assemblage",
};

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

/** Retry only transport failures: a refusal answers the same way every time. */
export function retryTransientProductionFailure(
  failureCount: number,
  error: unknown,
): boolean {
  return !(error instanceof ApiError && error.status < 500) && failureCount < 2;
}
