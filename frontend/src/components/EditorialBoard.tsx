import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  fetchEditorialBoard,
  mergeEditorialGroups,
  rejectEditorialGroup,
  selectEditorialGroup,
  splitEditorialGroup,
  type EditorialBoardResult,
  type EditorialGroup,
  type EditorialType,
} from "../api/editorial";

const scoreLabels: Record<string, string> = {
  impact: "Impact",
  novelty: "Nouveauté",
  technical_depth: "Profondeur technique",
  hunting_potential: "Potentiel de chasse",
  actionability: "Actionnabilité",
  source_quality: "Qualité des sources",
};

export function EditorialBoard({ editionId }: { editionId: string }) {
  const queryClient = useQueryClient();
  const [checkedGroups, setCheckedGroups] = useState<string[]>([]);
  const board = useQuery({
    queryKey: ["editorial-board", editionId],
    queryFn: () => fetchEditorialBoard(editionId),
    refetchInterval: 5_000,
  });
  const action = useMutation({
    mutationFn: (operation: () => Promise<EditorialBoardResult>) => operation(),
    onSuccess: (updated) => {
      queryClient.setQueryData(["editorial-board", editionId], updated);
      setCheckedGroups([]);
    },
  });

  if (board.isPending) return <p role="status">Regroupement des candidats…</p>;
  if (board.isError)
    return (
      <p role="alert" className="error-message">
        Le board éditorial est inaccessible.
      </p>
    );

  const proposedIds = board.data.groups
    .filter((group) => group.status === "proposed")
    .map((group) => group.id);
  const checkedProposed = checkedGroups.filter((id) =>
    proposedIds.includes(id),
  );

  return (
    <section
      className="editorial-board"
      aria-labelledby="editorial-board-heading"
    >
      <div className="editorial-board__heading">
        <div>
          <p className="eyebrow">Sélection humaine</p>
          <h2 id="editorial-board-heading">Board éditorial</h2>
        </div>
        <div className="selection-counter" aria-label="Unités retenues">
          <strong>
            {board.data.selected_major}/{board.data.target_major}
          </strong>{" "}
          articles principaux ·{" "}
          <strong>
            {board.data.selected_briefs}/{board.data.target_briefs}
          </strong>{" "}
          brèves
        </div>
      </div>
      <p className="verification-warning" role="note">
        Recherche effectuée depuis les citations visibles de ChatGPT. La liste
        des sources et leurs relations seront vérifiées lors de la collecte.
      </p>
      <p>
        Le score aide à comparer les groupes. Il ne déclenche jamais une
        sélection automatique.
      </p>
      {action.error ? (
        <p role="alert" className="error-message">
          {action.error.message}
        </p>
      ) : null}
      <button
        className="button button--secondary"
        disabled={checkedProposed.length < 2 || action.isPending}
        onClick={() =>
          action.mutate(() => mergeEditorialGroups(editionId, checkedProposed))
        }
      >
        Fusionner les groupes cochés
      </button>
      {board.data.groups.filter((group) => group.status !== "superseded")
        .length === 0 ? (
        <p className="empty-state">Aucun groupe à examiner.</p>
      ) : null}
      <div className="editorial-group-list">
        {board.data.groups
          .filter((group) => group.status !== "superseded")
          .map((group) => (
            <EditorialGroupCard
              key={group.id}
              group={group}
              checked={checkedGroups.includes(group.id)}
              pending={action.isPending}
              onChecked={(checked) =>
                setCheckedGroups((current) =>
                  checked
                    ? [...new Set([...current, group.id])]
                    : current.filter((id) => id !== group.id),
                )
              }
              onAction={(operation) => action.mutate(operation)}
              editionId={editionId}
            />
          ))}
      </div>
    </section>
  );
}

function EditorialGroupCard({
  group,
  checked,
  pending,
  editionId,
  onChecked,
  onAction,
}: {
  group: EditorialGroup;
  checked: boolean;
  pending: boolean;
  editionId: string;
  onChecked: (checked: boolean) => void;
  onAction: (operation: () => Promise<EditorialBoardResult>) => void;
}) {
  const [editorialType, setEditorialType] = useState<EditorialType>("brief");
  const [splitIds, setSplitIds] = useState<string[]>([]);
  const proposed = group.status === "proposed";
  const canSplit =
    splitIds.length > 0 && splitIds.length < group.candidates.length;

  return (
    <article className="editorial-group-card">
      <div className="editorial-group-card__heading">
        <label className="group-check">
          <input
            type="checkbox"
            checked={checked}
            disabled={!proposed || pending}
            onChange={(event) => onChecked(event.target.checked)}
          />
          Retenir pour une fusion
        </label>
        <span className="badge">{group.status}</span>
      </div>
      <h3>{group.title}</h3>
      {group.status === "selected" && group.subject_id ? (
        <p>
          <a href={`/subjects/${group.subject_id}`}>
            Ouvrir le Workbench Sujet
          </a>
        </p>
      ) : null}
      <p>
        <strong>Résultat :</strong> {group.outcome} · relations de sources{" "}
        <strong>{group.source_relationship_status}</strong>
      </p>
      <p>
        <strong>Confiance de regroupement :</strong> {group.grouping_confidence}
        . {group.grouping_justification}
      </p>
      {group.historical_comparison ? (
        <aside className="historical-comparison">
          <strong>Rapprochement avec un sujet sélectionné à vérifier</strong>
          <p>{group.historical_comparison.title}</p>
          <small>L’ambiguïté est conservée pour décision humaine.</small>
        </aside>
      ) : null}
      <details className="score-details">
        <summary>Score éditorial explicable — {group.score.total}/24</summary>
        <dl>
          {Object.entries(scoreLabels).map(([key, label]) => (
            <div key={key}>
              <dt>
                {label} :{" "}
                {group.score[key as keyof typeof group.score] as number}/4
              </dt>
              <dd>{group.score.justifications[key]}</dd>
            </div>
          ))}
        </dl>
      </details>
      <fieldset className="group-candidates">
        <legend>Publications regroupées</legend>
        {group.candidates.map((candidate) => (
          <label key={candidate.id}>
            <input
              type="checkbox"
              checked={splitIds.includes(candidate.id)}
              disabled={!proposed || group.candidates.length < 2 || pending}
              onChange={(event) =>
                setSplitIds((current) =>
                  event.target.checked
                    ? [...current, candidate.id]
                    : current.filter((id) => id !== candidate.id),
                )
              }
            />
            {candidate.title}
          </label>
        ))}
      </fieldset>
      <div className="editorial-actions">
        <button
          className="button button--secondary"
          disabled={!proposed || !canSplit || pending}
          onClick={() =>
            onAction(() => splitEditorialGroup(editionId, group.id, splitIds))
          }
        >
          Séparer les publications cochées
        </button>
        <label>
          Format éditorial
          <select
            value={editorialType}
            disabled={!proposed || pending}
            onChange={(event) =>
              setEditorialType(event.target.value as EditorialType)
            }
          >
            <option value="brief">Brève</option>
            <option value="major">Article principal</option>
          </select>
        </label>
        <button
          className="button"
          disabled={!proposed || pending}
          onClick={() =>
            onAction(() =>
              selectEditorialGroup(editionId, group.id, editorialType),
            )
          }
        >
          Sélectionner comme{" "}
          {editorialType === "brief" ? "brève" : "article principal"}
        </button>
        <button
          className="button button--danger"
          disabled={!proposed || pending}
          onClick={() =>
            onAction(() =>
              rejectEditorialGroup(
                editionId,
                group.id,
                "Écarté depuis le board éditorial",
              ),
            )
          }
        >
          Rejeter
        </button>
      </div>
    </article>
  );
}
