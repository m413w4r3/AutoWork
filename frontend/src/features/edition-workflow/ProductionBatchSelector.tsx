import type { EligibleGroup } from "./productionBatchSelection";
import { Link } from "../../routing";

/**
 * Lets the operator hand-pick which editorially eligible subjects go into
 * the next production batch. Editorial eligibility and batch selection are
 * deliberately separate: this component never touches editorial decisions,
 * it only reports which of the already-eligible subjects are checked.
 */
export function ProductionBatchSelector({
  groups,
  selected,
  onToggle,
  onSelectAll,
  onSelectNone,
}: {
  groups: readonly EligibleGroup[];
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
            disabled={groups.length === 0 || selected.size === groups.length}
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
      {groups.length === 0 ? (
        <p className="empty-state">Aucun article éligible pour le moment.</p>
      ) : (
        <ul className="production-batch-selector__list">
          {groups.map((group) => (
            <li key={group.id}>
              <label>
                <input
                  type="checkbox"
                  checked={selected.has(group.subject_id)}
                  onChange={(event) =>
                    onToggle(group.subject_id, event.target.checked)
                  }
                />
                {group.title}
              </label>
              <Link to={`/subjects/${group.subject_id}`}>Ouvrir le sujet</Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
