import type {
  RepairApplicationDiagnostic,
  RepairRemediation,
} from "../../api/publication";

/** La seule action qui débloque la situation, décidée par le backend. */
const REMEDIATION_LABELS: Record<RepairRemediation, string> = {
  reload_repair_queue:
    "Rechargez la file de réparation, puis relancez l’application.",
  reauthenticate:
    "Reconnectez-vous : l’identité de l’analyste est requise pour tracer la décision.",
  wait_for_run:
    "L’exécution de production est en cours. Attendez sa fin avant d’appliquer.",
  reopen_edition:
    "L’édition est figée par un bulletin publié. Rouvrez une révision pour la corriger.",
  rerun_extraction:
    "Relancez l’étape Extraction pour régénérer les preuves, ou excluez la valeur.",
  resubmit_correction: "Ressaisissez la valeur corrigée, puis revalidez-la.",
  rerun_assembly: "Relancez l’étape Assemblage de cet article.",
  rerun_references: "Relancez l’étape Références de cet article.",
  open_qa_report:
    "Le contrôle QA refuse le livrable réparé. Ouvrez son rapport QA.",
  reconcile_submission:
    "Une soumission attend une réconciliation humaine. Traitez-la dans le panneau de réconciliation.",
  contact_operations:
    "Un service interne est indisponible. Réessayez, puis alertez l’exploitation.",
};

export function repairRemediationLabel(
  diagnostic: RepairApplicationDiagnostic,
): string {
  return REMEDIATION_LABELS[diagnostic.remediation];
}
