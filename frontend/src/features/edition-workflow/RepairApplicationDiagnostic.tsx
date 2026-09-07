import type {
  RepairApplicationDiagnostic,
  RepairApplicationStage,
  RepairRemediation,
} from "../../api/publication";

/**
 * Où l’application s’est arrêtée. L’étape situe la panne dans la chaîne ; sans
 * elle, « la revue n’a pas pu être mise à jour » ne dit rien à l’analyste.
 */
const STAGE_LABELS: Record<RepairApplicationStage, string> = {
  fence: "Contrôle de cohérence",
  payload: "Récupération de la valeur",
  projection: "Projection de l’extraction",
  assembly: "Assemblage de la publication",
  qa: "Contrôle QA",
  references: "Reconstruction des références",
};

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

export function RepairApplicationDiagnosticView({
  diagnostic,
}: {
  diagnostic: RepairApplicationDiagnostic;
}) {
  return (
    <div className="repair-application-error" role="alert">
      <p className="repair-application-error__headline">
        L’application a échoué à l’étape « {STAGE_LABELS[diagnostic.stage]} ».
      </p>
      <p className="repair-application-error__remediation">
        {repairRemediationLabel(diagnostic)}
      </p>
      <details className="repair-application-error__technical">
        <summary>Code technique</summary>
        <dl>
          <div>
            <dt>error_code</dt>
            <dd>
              <code>{diagnostic.error_code}</code>
            </dd>
          </div>
          <div>
            <dt>repair_id</dt>
            <dd>
              <code>{diagnostic.repair_id}</code>
            </dd>
          </div>
          <div>
            <dt>message</dt>
            <dd>
              <code>{diagnostic.message}</code>
            </dd>
          </div>
        </dl>
      </details>
    </div>
  );
}
