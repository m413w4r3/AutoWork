/**
 * Placeholder de la vue « Plan et tâches ».
 *
 * Le lot 09B remplace ce fichier ; le shell importe dès maintenant ce nom fixe
 * pour figer le contrat de montage.
 */

import { createElement } from "react";

export function PlanTasksView({
  investigationId,
}: {
  investigationId: string;
}) {
  return createElement(
    "section",
    { className: "job-card", "aria-label": "Plan et tâches" },
    createElement("h2", null, "Plan et tâches"),
    createElement("p", { role: "note" }, "Vue en cours d’implémentation."),
    createElement("p", null, `Enquête ${investigationId}.`),
  );
}
