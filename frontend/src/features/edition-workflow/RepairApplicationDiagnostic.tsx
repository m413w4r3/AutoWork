import type {
  RepairApplicationDiagnostic,
  RepairApplicationStage,
} from "../../api/publication";
import { repairRemediationLabel } from "./repairApplicationDiagnosticText";

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
