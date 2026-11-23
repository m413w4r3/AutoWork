/**
 * "Plan et tâches" view of the M24 investigation cockpit (desktop only).
 *
 * Fixed shell contract: `PlanTasksView({ investigationId })`. Server state is
 * owned by TanStack Query hooks of the frozen API client; only selection and
 * form drafts live in local state.
 */

import { TaskWorkspace } from "../tasks/TaskWorkspace";
import { TimelinePanel } from "../tasks/TimelinePanel";
import { PlanPanel } from "./PlanPanel";
import "./plan-tasks.css";

export function PlanTasksView({
  investigationId,
}: {
  investigationId: string;
}) {
  return (
    <section
      className="m24-plan-tasks"
      aria-labelledby="m24-plan-tasks-heading"
    >
      <header className="m24-plan-tasks__header">
        <h2 id="m24-plan-tasks-heading">Plan et tâches</h2>
        <p className="m24-muted">
          Plan Bridge, tâches transférables et opérateurs Codex / Claude Code.
          Chaque décision humaine porte la version exacte de son objet (CAS) ;
          les avis Bridge restent non probants.
        </p>
        <div className="m24-plan-tasks__plan-selector">
          <PlanPanel investigationId={investigationId} />
        </div>
      </header>
      <TaskWorkspace investigationId={investigationId} />
      <details className="m24-global-activity">
        <summary>Activité globale</summary>
        <TimelinePanel
          investigationId={investigationId}
          title="Activité globale"
        />
      </details>
    </section>
  );
}
