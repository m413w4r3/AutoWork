import type { EditorialReadOnlyReason } from "../../api/editorialReadOnly";

export function editorialReadOnlyReasonText(
  reason: EditorialReadOnlyReason,
): string {
  switch (reason) {
    case "format_not_editable":
      return "Raison : format non éditable.";
    case "publication_kind_ambiguous":
      return "Raison : type de publication ambigu.";
    case "publication_kind_unknown":
      return "Type de publication indéterminé. Raison : type de publication inconnu.";
    case "historical_version":
      return "Raison : version historique.";
  }
}
