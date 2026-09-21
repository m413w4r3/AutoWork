import type { EligibleSubject } from "./productionBatchSelection";
import { Link } from "../../routing";

/**
 * Lets the operator hand-pick which already-materialized Subjects go into
 * the next production batch. Selection and batch selection are deliberately
 * separate: this component never takes a Selection decision, it only reports
 * which of the already-eligible subjects are checked.
 */
export function ProductionBatchSelector({
  subjects,
  selected,
  onToggle,
  onSelectAll,
  onSelectNone,
}: {
  subjects: readonly EligibleSubject[];
  selected: ReadonlySet<string>;
  onToggle: (subjectId: string, checked: boolean) => void;
  onSelectAll: () => void;
  onSelectNone: () => void;
}) {
  return (
    <section
      className="production-batch-selector"
      aria-labelledby="production-batch-selector-heading"
    >
      <div className="production-batch-selector__heading">
        <h3 id="production-batch-selector-heading">
          Sélecteur du lot de production
        </h3>
        <div className="production-batch-selector__bulk-actions">
          <button
            type="button"
            className="button button--secondary"
            disabled={
              subjects.length === 0 || selected.size === subjects.length
            }
            onClick={onSelectAll}
          >
            Tout sélectionner
          </button>
          <button
            type="button"
            className="button button--secondary"
            disabled={selected.size === 0}
            onClick={onSelectNone}
          >
            Tout désélectionner
          </button>
        </div>
      </div>
      {subjects.length === 0 ? (
        <p className="empty-state">Aucun sujet éligible pour le moment.</p>
      ) : (
        <ul className="production-batch-selector__list">
          {subjects.map((subject) => (
            <li key={subject.subject_id}>
              <label className="production-batch-selector__choice">
                <input
                  type="checkbox"
                  checked={selected.has(subject.subject_id)}
                  onChange={(event) =>
                    onToggle(subject.subject_id, event.target.checked)
                  }
                />
                {subject.title}
              </label>
              <Link to={`/subjects/${subject.subject_id}`}>
                Ouvrir le sujet
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
