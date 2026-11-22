/**
 * Placeholder de la vue « Workspace et notes ».
 *
 * Le lot 09C remplace ce fichier ; le shell importe dès maintenant ce nom fixe
 * pour figer le contrat de montage.
 */

import { createElement } from "react";

export function WorkspaceNotesView({
  investigationId,
}: {
  investigationId: string;
}) {
  return createElement(
    "section",
    { className: "job-card", "aria-label": "Workspace et notes" },
    createElement("h2", null, "Workspace et notes"),
    createElement("p", { role: "note" }, "Vue en cours d’implémentation."),
    createElement("p", null, `Enquête ${investigationId}.`),
  );
}
