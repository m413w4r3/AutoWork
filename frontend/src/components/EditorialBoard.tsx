import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  confirmEditorialDecisions,
  fetchEditorialBoard,
  mergeEditorialGroups,
  splitEditorialGroup,
  type EditorialBoardResult,
  type EditorialDecision,
  type EditorialGroup,
} from "../api/editorial";

const scoreLabels: Record<string, string> = {
  impact: "Impact",
  novelty: "Nouveauté",
  technical_depth: "Profondeur technique",
  hunting_potential: "Potentiel de chasse",
  actionability: "Actionnabilité",
  source_quality: "Qualité des sources",
};

const typeLabels: Record<string, string> = {
  ipv4: "IPv4",
  ipv6: "IPv6",
  domain: "domaines",
  url: "URL",
  md5: "MD5",
  sha1: "SHA-1",
  sha256: "SHA-256",
  email: "adresses e-mail",
  cve: "CVE",
  other: "autres",
};

type DecisionChoice = EditorialDecision | "undecided";

export function EditorialBoard({ editionId }: { editionId: string }) {
  const queryClient = useQueryClient();
  const [checkedGroups, setCheckedGroups] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Record<string, EditorialDecision>>({});
  const board = useQuery({
    queryKey: ["editorial-board", editionId],
    queryFn: () => fetchEditorialBoard(editionId),
  });
  const action = useMutation({
    mutationFn: (operation: () => Promise<EditorialBoardResult>) => operation(),
    onSuccess: (updated) => {
      queryClient.setQueryData(["editorial-board", editionId], updated);
      setCheckedGroups([]);
      setDrafts({});
    },
  });

  const draftEntries = useMemo(() => Object.entries(drafts), [drafts]);

  if (board.isPending) return <p role="status">Regroupement des candidats…</p>;
  if (board.isError)
    return (
      <p role="alert" className="error-message">
        La sélection éditoriale est inaccessible.
      </p>
    );

  const proposed = board.data.groups.filter(
    (group) => group.status === "proposed",
  );
  const ready = board.data.groups.filter(
    (group) => group.status === "selected",
  );
  const proposedIds = new Set(proposed.map((group) => group.id));
  const activeDraftEntries = draftEntries.filter(([groupId]) =>
    proposedIds.has(groupId),
  );
  const draftBriefs = activeDraftEntries.filter(
    ([, value]) => value === "brief",
  ).length;
  const draftMajor = activeDraftEntries.filter(
    ([, value]) => value === "major",
  ).length;
  const draftIgnored = activeDraftEntries.filter(
    ([, value]) => value === "ignore",
  ).length;
  const currentBriefs = board.data.selected_briefs + draftBriefs;
  const currentMajor = board.data.selected_major + draftMajor;
  const currentIgnored =
    (board.data.ignored ??
      board.data.groups.filter((group) => group.status === "rejected").length) +
    draftIgnored;
  const currentUndecided = proposed.length - activeDraftEntries.length;
  const checkedProposed = checkedGroups.filter((id) => proposedIds.has(id));

  return (
    <section
      className="editorial-board"
      aria-labelledby="editorial-board-heading"
    >
      <div className="editorial-board__heading">
        <div>
          <p className="eyebrow">Choix éditorial</p>
          <h2 id="editorial-board-heading">Sélection des sujets</h2>
        </div>
        <div className="selection-counter" aria-label="Décisions courantes">
          <strong>{currentBriefs}</strong> brèves ·{" "}
          <strong>{currentMajor}</strong> articles approfondis ·{" "}
          <strong>{currentIgnored}</strong> ignorés ·{" "}
          <strong>{currentUndecided}</strong> encore à décider
        </div>
      </div>
      <p className="verification-warning" role="note">
        IOC repérés pendant la recherche — non encore vérifiés depuis les
        sources.
      </p>
      {action.error ? (
        <p role="alert" className="error-message">
          {action.error.message}
        </p>
      ) : null}

      <section aria-labelledby="to-review-heading">
        <h3 id="to-review-heading">À examiner</h3>
        {proposed.length === 0 ? (
          <p className="empty-state">Aucun groupe en attente de décision.</p>
        ) : (
          <div className="editorial-group-list">
            {proposed.map((group) => (
              <EditorialDecisionCard
                key={group.id}
                group={group}
                decision={drafts[group.id] ?? "undecided"}
                pending={action.isPending}
                onDecision={(decision) =>
                  setDrafts((current) => {
                    if (decision === "undecided") {
                      const next = { ...current };
                      delete next[group.id];
                      return next;
                    }
                    return { ...current, [group.id]: decision };
                  })
                }
              />
            ))}
          </div>
        )}
        <button
          className="button confirm-selection"
          disabled={activeDraftEntries.length === 0 || action.isPending}
          onClick={() =>
            action.mutate(() =>
              confirmEditorialDecisions(
                editionId,
                activeDraftEntries.map(([groupId, decision]) => ({
                  group_id: groupId,
                  version:
                    proposed.find((group) => group.id === groupId)?.version ??
                    0,
                  decision,
                })),
              ),
            )
          }
        >
          Confirmer la sélection ({activeDraftEntries.length})
        </button>
      </section>

      <section className="ready-subjects" aria-labelledby="ready-heading">
        <h3 id="ready-heading">Prêts à traiter</h3>
        <p className="ready-indicator">
          {ready.length} sujets prêts · {board.data.selected_briefs} brève
          {board.data.selected_briefs > 1 ? "s" : ""} ·{" "}
          {board.data.selected_major} article
          {board.data.selected_major > 1 ? "s" : ""} principal
          {board.data.selected_major > 1 ? "ux" : ""}
        </p>
        {ready.length === 0 ? (
          <p className="empty-state">Aucun sujet confirmé pour le moment.</p>
        ) : (
          <ul className="ready-subject-list">
            {ready.map((group) => (
              <li key={group.id}>
                <div>
                  <strong>{group.title}</strong>
                  <small>
                    {group.editorial_type === "brief"
                      ? "Brève"
                      : "Article principal + pivots"}
                  </small>
                </div>
                {group.subject_id ? (
                  <a href={`/subjects/${group.subject_id}`}>Ouvrir le sujet</a>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <details className="advanced-editorial-panel">
        <summary>Organiser les publications</summary>
        <p>
          Fusion, séparation et signaux de regroupement restent disponibles pour
          les cas ambigus.
        </p>
        <button
          className="button button--secondary"
          disabled={checkedProposed.length < 2 || action.isPending}
          onClick={() =>
            action.mutate(() =>
              mergeEditorialGroups(editionId, checkedProposed),
            )
          }
        >
          Fusionner les groupes cochés
        </button>
        <div className="advanced-group-list">
          {proposed.map((group) => (
            <AdvancedGroupControls
              key={group.id}
              group={group}
              checked={checkedGroups.includes(group.id)}
              pending={action.isPending}
              editionId={editionId}
              onChecked={(checked) =>
                setCheckedGroups((current) =>
                  checked
                    ? [...new Set([...current, group.id])]
                    : current.filter((id) => id !== group.id),
                )
              }
              onAction={(operation) => action.mutate(operation)}
            />
          ))}
        </div>
      </details>
    </section>
  );
}

function EditorialDecisionCard({
  group,
  decision,
  pending,
  onDecision,
}: {
  group: EditorialGroup;
  decision: DecisionChoice;
  pending: boolean;
  onDecision: (decision: DecisionChoice) => void;
}) {
  const presentation = group.presentation ?? group.candidates[0]?.summary;
  const publications =
    group.publications ??
    group.candidates.flatMap((candidate) =>
      (candidate.source_urls ?? []).map((url) => ({
        title: candidate.title,
        url,
        publisher: null,
        role: "unknown",
        published_at: candidate.event_date,
      })),
    );
  const iocs = group.provisional_iocs ?? [];
  const visibleCount = group.provisional_ioc_count ?? iocs.length;
  const typeCounts = group.provisional_ioc_type_counts ?? {};
  const announcedCounts =
    group.publisher_ioc_counts ??
    (group.publisher_ioc_count_total !== null &&
    group.publisher_ioc_count_total !== undefined
      ? [group.publisher_ioc_count_total]
      : []);

  return (
    <article className="editorial-group-card">
      <h4>{group.title}</h4>
      {presentation ? (
        <p className="group-presentation">{presentation}</p>
      ) : null}
      <dl className="group-facts">
        {group.actor_or_campaign ? (
          <div>
            <dt>Acteur ou campagne</dt>
            <dd>{group.actor_or_campaign}</dd>
          </div>
        ) : null}
        {group.technical_potential !== undefined ? (
          <div>
            <dt>Potentiel technique</dt>
            <dd>
              {group.technical_potential}/4
              {group.technical_potential_reason
                ? ` — ${group.technical_potential_reason}`
                : ""}
            </dd>
          </div>
        ) : null}
        {group.artifacts?.length ? (
          <div>
            <dt>Artefacts annoncés</dt>
            <dd>{group.artifacts.join(" · ")}</dd>
          </div>
        ) : null}
      </dl>
      {publications.length ? (
        <div className="main-publications">
          <strong>Publications principales</strong>
          <ul>
            {publications.slice(0, 3).map((publication) => (
              <li key={publication.url}>
                <a href={publication.url}>{publication.title}</a>
                {publication.publisher ? ` — ${publication.publisher}` : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {group.uncertainties?.length ? (
        <p className="uncertainties">
          <strong>Incertitudes :</strong> {group.uncertainties.join(" · ")}
        </p>
      ) : null}
      {group.metadata_incomplete ? (
        <p className="metadata-note">
          Certaines métadonnées seront complétées après collecte.
        </p>
      ) : null}
      {announcedCounts.length || visibleCount > 0 ? (
        <div className="provisional-ioc-summary">
          <strong>IOC repérés</strong>
          <p>
            {announcedCounts.length === 1
              ? `${announcedCounts[0]} annoncés · `
              : announcedCounts.length > 1
                ? `totaux annoncés ${announcedCounts.join(", ")} · `
                : ""}
            {visibleCount} valeur{visibleCount > 1 ? "s" : ""} visible
            {visibleCount > 1 ? "s" : ""}
          </p>
          {Object.keys(typeCounts).length ? (
            <p>
              Types visibles :{" "}
              {Object.entries(typeCounts)
                .map(([type, count]) => `${count} ${typeLabels[type] ?? type}`)
                .join(" · ")}
            </p>
          ) : null}
          <p>Statut : non vérifié</p>
          {iocs.length ? (
            <ul className="ioc-examples">
              {iocs.slice(0, 5).map((ioc) => (
                <li key={`${ioc.proposed_type}:${ioc.raw_value}`}>
                  <code>{ioc.raw_value}</code>
                </li>
              ))}
            </ul>
          ) : null}
          {iocs.length > 5 ? (
            <details>
              <summary>Voir les {iocs.length} valeurs</summary>
              <ul>
                {iocs.map((ioc) => (
                  <li key={`full:${ioc.proposed_type}:${ioc.raw_value}`}>
                    <code>{ioc.raw_value}</code> —{" "}
                    {typeLabels[ioc.proposed_type] ?? ioc.proposed_type}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      ) : null}
      <fieldset className="decision-options">
        <legend>Décision éditoriale</legend>
        {(
          [
            ["undecided", "À décider"],
            ["brief", "Brève"],
            ["major", "Article approfondi + pivots"],
            ["ignore", "Ignorer"],
          ] as const
        ).map(([value, label]) => (
          <label key={value}>
            <input
              type="radio"
              name={`decision-${group.id}`}
              value={value}
              checked={decision === value}
              disabled={pending}
              onChange={() => onDecision(value)}
            />
            {label}
          </label>
        ))}
      </fieldset>
    </article>
  );
}

function AdvancedGroupControls({
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
  const [splitIds, setSplitIds] = useState<string[]>([]);
  const canSplit =
    splitIds.length > 0 && splitIds.length < group.candidates.length;

  return (
    <article className="advanced-group-card">
      <h4>{group.title}</h4>
      <label className="group-check">
        <input
          type="checkbox"
          checked={checked}
          disabled={pending}
          onChange={(event) => onChecked(event.target.checked)}
        />
        Retenir pour une fusion
      </label>
      <p>
        <strong>Justification du regroupement :</strong>{" "}
        {group.grouping_justification}
      </p>
      {group.historical_comparison ? (
        <aside className="historical-comparison">
          <strong>Comparaison historique</strong>
          <p>{group.historical_comparison.title}</p>
        </aside>
      ) : null}
      <details className="score-details">
        <summary>Détails du score — {group.score.total}/24</summary>
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
              disabled={group.candidates.length < 2 || pending}
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
      <button
        className="button button--secondary"
        disabled={!canSplit || pending}
        onClick={() =>
          onAction(() => splitEditorialGroup(editionId, group.id, splitIds))
        }
      >
        Séparer les publications cochées
      </button>
    </article>
  );
}
