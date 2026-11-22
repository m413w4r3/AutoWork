/**
 * Placeholder de la vue « Note de l’analyste ».
 *
 * Le lot 09D remplace ce fichier ; le shell importe dès maintenant ce nom fixe
 * pour figer le contrat de montage.
 */

import { createElement } from "react";

export function OpinionNotesView({
  investigationId,
}: {
  investigationId: string;
}) {
  return createElement(
    "section",
    { className: "job-card", "aria-label": "Note de l’analyste" },
    createElement("h2", null, "Note de l’analyste"),
    createElement("p", { role: "note" }, "Vue en cours d’implémentation."),
    createElement("p", null, `Enquête ${investigationId}.`),
  );
}
