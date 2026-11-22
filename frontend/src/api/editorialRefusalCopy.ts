/**
 * Closed-code French copy for editorial refusals.
 *
 * One mapping shared by the API error adapter, the proposal card, the replace
 * contract and the insertion panels. Distinct no-proof codes must stay
 * distinct: the operator needs to know whether the evidence has no numbers at
 * all (`bar_chart_insufficient_data`) or whether one proposed value is not
 * backed by an admitted record (`bar_chart_value_not_evidenced`).
 */
export const EDITORIAL_REFUSAL_MESSAGES = {
  revision_not_applicable:
    "Les données disponibles ne permettent pas de proposer cette révision.",
  bar_chart_insufficient_data:
    "Les preuves admises ne contiennent pas assez de données quantitatives pour construire un graphique. Un tableau descriptif peut être proposé.",
  bar_chart_value_not_evidenced:
    "Une valeur du graphique n’est pas étayée par un enregistrement de preuve admis. Aucun graphique quantitatif ne peut être proposé.",
  editorial_figure_not_eligible:
    "Cette figure archivée ne peut pas être intégrée.",
  editorial_figure_not_changed:
    "La figure sélectionnée est déjà celle de cet élément.",
  editorial_figure_not_found:
    "Cette figure n’est plus disponible dans l’inventaire.",
  brief_figure_limit_exceeded:
    "Une brève ne peut contenir plus de trois figures intégrées.",
  source_figure_inventory_limit_exceeded:
    "Le nombre de figures dépasse la limite de l’inventaire archivé.",
  figure_catalog_unavailable:
    "La galerie de figures est temporairement indisponible. Réessayez dans quelques instants.",
  editorial_caption_text_violation:
    "La légende contient un format ou un balisage non autorisé.",
  editorial_anchor_stale:
    "Le paragraphe d’ancrage a changé ou a été supprimé. Actualisez l’atelier, puis choisissez un paragraphe disponible.",
  editorial_read_only:
    "Cette version est en lecture seule : elle ne peut pas être modifiée.",
  enrichment_revision_patch_invalid:
    "La révision visuelle est invalide. Vérifiez les champs, puis réessayez.",
  enrichment_element_not_found:
    "Cet élément enrichi n’existe plus dans cette version.",
  enrichment_element_kind_mismatch:
    "Le type de cet élément enrichi ne correspond pas à la révision demandée.",
  enrichment_target_stale:
    "Cet élément enrichi a changé. Rechargez l’atelier avant de réessayer.",
  enrichment_placement_changed:
    "L’emplacement de cet élément a changé. Rechargez l’atelier.",
  enrichment_caption_invalid:
    "La légende proposée ne respecte pas les règles de l’atelier.",
  enrichment_move_anchor_invalid: "Le paragraphe de destination est invalide.",
  enrichment_order_invalid:
    "L’ordre demandé ne correspond pas aux éléments de ce paragraphe.",
  enrichment_order_group_unsupported:
    "Ce groupe d’éléments ne peut pas être réordonné.",
  enrichment_revision_no_change:
    "La révision ne modifie rien. Aucune nouvelle version n’a été créée.",
  enrichment_revision_unsupported_operation:
    "Cette opération de révision visuelle n’est pas encore disponible.",
  proposal_operation_unsupported:
    "Cette insertion n’est pas autorisée pour le paragraphe sélectionné. Actualisez l’atelier.",
  proposal_base_stale:
    "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
  proposal_base_unavailable:
    "Le texte de référence de cette proposition est indisponible.",
} as const satisfies Record<string, string>;

export type EditorialRefusalCode = keyof typeof EDITORIAL_REFUSAL_MESSAGES;

export const EDITORIAL_REFUSAL_FALLBACK =
  "Le Bridge n’a pas produit de proposition exploitable.";

/** French copy for one closed refusal code, or the supplied fallback. */
export function editorialRefusalMessage(
  code: unknown,
  fallback: string = EDITORIAL_REFUSAL_FALLBACK,
): string {
  if (typeof code !== "string") return fallback;
  if (!Object.prototype.hasOwnProperty.call(EDITORIAL_REFUSAL_MESSAGES, code)) {
    return fallback;
  }
  return EDITORIAL_REFUSAL_MESSAGES[code as EditorialRefusalCode];
}
