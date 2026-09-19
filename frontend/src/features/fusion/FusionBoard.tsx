import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../api/editions";
import {
  FUSION_SNAPSHOT_STALE,
  fetchFusionBoard,
  mergeFusionSubjects,
  resolveFusionReview,
  splitFusionSubject,
  type FusionCandidate,
  type FusionGroup,
  type FusionHistoryEntry,
  type FusionModelSuggestion,
  type FusionPendingReview,
  type FusionReviewAction,
  type FusionReviewDecision,
  type FusionReviewGroup,
  type FusionSignal,
} from "../../api/fusion";

type Operation =
  | { kind: "review"; mergeRunId: string; decisions: FusionReviewDecision[] }
  | { kind: "merge"; subjectIds: string[] }
  | { kind: "split"; subjectId: string; candidateIds: string[] };

interface ReviewChoice {
  action: FusionReviewAction;
  target: string;
}

interface GroupOption {
  id: string;
  title: string;
}

type ComparisonRow = [string, (candidate: FusionCandidate) => string];

const INTRO =
  "Comparez les candidates, les signaux déterministes et la suggestion du modèle avant de modifier la structure des groupes.";
const MODEL_WARNING =
  "Suggestion consultative : ce n’est pas une preuve déterministe.";
const NO_MODEL_SUGGESTION = "Aucune suggestion du modèle pour ce groupe.";
const NO_SIGNAL = "Aucun signal déterministe commun.";
const NO_DIFFERENCE = "Aucune différence relevée.";
const NO_HISTORY = "Aucun historique.";
const NO_REVIEW = "Aucun cas à revoir.";
const NO_UNSTABILIZED = "Toutes les candidates actives sont placées.";
const CHOOSE_DECISION = "Choisissez une décision pour voir son effet.";
const MERGE_EFFECT =
  "Effet : les groupes cochés deviennent un seul groupe dans un nouveau snapshot.";
const SPLIT_EFFECT =
  "Effet : les candidates cochées forment un nouveau groupe dans un nouveau snapshot.";
const STALE_MESSAGE =
  "L’état de fusion a changé. Reprenez la décision sur l’état rafraîchi.";
const STALE_REVIEW =
  "Proposition calculée sur un état antérieur : la résoudre la fera recalculer.";

const REVIEW_ACTIONS: FusionReviewAction[] = [
  "accept",
  "separate",
  "attach",
  "defer",
];

const actionLabels: Record<FusionReviewAction, string> = {
  accept: "Accepter le regroupement",
  separate: "Séparer",
  attach: "Rattacher à un groupe existant",
  defer: "Différer",
};

const actionEffects: Record<FusionReviewAction, string> = {
  accept: "le regroupement proposé est appliqué.",
  separate: "chaque candidate devient un groupe distinct.",
  attach: "les candidates rejoignent le groupe choisi.",
  defer: "rien n’est appliqué, le cas reste à revoir.",
};

const signalLabels: Record<string, string> = {
  shared_canonical_url: "URL canonique identique",
  same_publication: "Même publication",
  shared_source_domain: "Domaine source commun",
  shared_actor: "Acteur commun",
  shared_campaign: "Campagne commune",
  shared_malware: "Malware commun",
  shared_cve: "CVE commune",
  shared_ioc: "IOC commun",
  shared_country: "Pays commun",
  shared_sector: "Secteur commun",
  shared_artifact: "Artefact technique commun",
  title_similarity: "Titres similaires",
  date_proximity: "Dates proches",
};

const recommendationLabels: Record<string, string> = {
  merge: "regrouper",
  attach: "rattacher",
  separate: "séparer",
};

const originLabels: Record<string, string> = {
  deterministic_bootstrap: "règle déterministe",
  heuristic: "règle déterministe",
  chatgpt: "proposition du modèle",
  human: "décision humaine",
};

const historyLabels: Record<string, string> = {
  created: "Création",
  split_created: "Création par séparation",
  candidate_added: "Candidate ajoutée",
  merged: "Fusion",
  split: "Séparation",
};

const COMPARISON_ROWS: ComparisonRow[] = [
  ["Date", (candidate) => candidate.event_date ?? "—"],
  ["Acteurs", (candidate) => formatList(candidate.actors)],
  ["Campagnes", (candidate) => formatList(candidate.campaigns)],
  ["Malware", (candidate) => formatList(candidate.malware)],
  ["CVE", (candidate) => formatList(candidate.cves)],
  ["Sources", (candidate) => sourceNames([candidate])],
  ["IOC", (candidate) => String(candidate.iocs.length)],
];

export function FusionBoard({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const board = useQuery({
    queryKey: ["fusion", editionId],
    queryFn: () => fetchFusionBoard(editionId),
  });
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);
  const [splitSelection, setSplitSelection] = useState<
    Record<string, string[]>
  >({});
  const [reviewChoices, setReviewChoices] = useState<
    Record<string, ReviewChoice>
  >({});
  const [staleMessage, setStaleMessage] = useState<string | null>(null);

  const clearSelections = () => {
    setSelectedGroups([]);
    setSplitSelection({});
    setReviewChoices({});
  };

  const mutation = useMutation({
    // The version is read from the board displayed when the analyst submits.
    mutationFn: (operation: Operation) => {
      const version = board.data?.snapshot_version ?? 0;
      if (operation.kind === "review") {
        return resolveFusionReview(editionId, operation.mergeRunId, {
          snapshot_version: version,
          decisions: operation.decisions,
        });
      }
      if (operation.kind === "merge") {
        return mergeFusionSubjects(editionId, {
          snapshot_version: version,
          discovery_subject_ids: operation.subjectIds,
        });
      }
      return splitFusionSubject(editionId, {
        snapshot_version: version,
        discovery_subject_id: operation.subjectId,
        candidate_ids: operation.candidateIds,
      });
    },
    onSuccess: (nextBoard) => {
      queryClient.setQueryData(["fusion", editionId], nextBoard);
      clearSelections();
      setStaleMessage(null);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === FUSION_SNAPSHOT_STALE) {
        clearSelections();
        setStaleMessage(STALE_MESSAGE);
        void queryClient.invalidateQueries({ queryKey: ["fusion", editionId] });
      }
    },
  });

  if (board.isPending) {
    return <p className="status-message">Chargement de la Fusion…</p>;
  }
  if (board.isError) {
    return (
      <p className="error-message" role="alert">
        La Fusion n’a pas pu être chargée : {board.error.message}
      </p>
    );
  }

  const data = board.data;
  if (!data) return null;
  const locked = readOnly || data.read_only;
  const pending = mutation.isPending;
  const groupOptions = data.groups.map((group) => ({
    id: group.discovery_subject_id,
    title: group.title,
  }));
  const counters: Array<[string, string]> = [
    ["Snapshot", versionLabel(data.snapshot_version)],
    ["Candidates actives", String(data.candidate_count)],
    ["Groupes actuels", String(data.group_count)],
    ["Cas à revoir", String(data.pending_review_count)],
  ];

  const toggleGroup = (group: FusionGroup, checked: boolean) => {
    const id = group.discovery_subject_id;
    setSelectedGroups((current) => toggled(current, id, checked));
  };
  const toggleSplit = (group: FusionGroup, id: string, checked: boolean) => {
    const key = group.discovery_subject_id;
    setSplitSelection((current) => ({
      ...current,
      [key]: toggled(current[key] ?? [], id, checked),
    }));
  };
  const chooseReview = (key: string, choice: ReviewChoice) => {
    setReviewChoices((current) => ({ ...current, [key]: choice }));
  };
  const submitMerge = () => {
    mutation.mutate({ kind: "merge", subjectIds: selectedGroups });
  };
  const submitSplit = (group: FusionGroup) => {
    mutation.mutate({
      kind: "split",
      subjectId: group.discovery_subject_id,
      candidateIds: splitSelection[group.discovery_subject_id] ?? [],
    });
  };
  const submitReview = (
    review: FusionPendingReview,
    decisions: FusionReviewDecision[],
  ) => {
    mutation.mutate({
      kind: "review",
      mergeRunId: review.merge_run_id,
      decisions,
    });
  };

  return (
    <section className="fusion-board" aria-labelledby="fusion-heading">
      <div className="fusion-board__heading">
        <div>
          <p className="eyebrow">Édition · Fusion</p>
          <h1 id="fusion-heading">Fusion explicable</h1>
          <p>{INTRO}</p>
        </div>
        {locked ? (
          <span className="badge badge--archived">Archive · lecture seule</span>
        ) : null}
      </div>

      {staleMessage ? (
        <p className="fusion-stale-message" role="alert">
          {staleMessage}
        </p>
      ) : null}
      {mutation.isError && !staleMessage ? (
        <p className="error-message" role="alert">
          {mutation.error.message}
        </p>
      ) : null}

      <dl className="fusion-counters" aria-label="Compteurs de fusion">
        {counters.map(([label, value]) => (
          <Fact key={label} label={label} value={value} />
        ))}
      </dl>

      <section className="fusion-section" aria-labelledby="fusion-reviews">
        <h2 id="fusion-reviews">Cas à revoir</h2>
        {data.pending_reviews.length === 0 ? <p>{NO_REVIEW}</p> : null}
        {data.pending_reviews.map((review) => (
          <ReviewCard
            key={review.merge_run_id}
            review={review}
            locked={locked}
            pending={pending}
            choices={reviewChoices}
            targets={groupOptions}
            onChoose={chooseReview}
            onSubmit={(decisions) => submitReview(review, decisions)}
          />
        ))}
      </section>

      <section className="fusion-section" aria-labelledby="fusion-groups">
        <div className="fusion-section__heading">
          <h2 id="fusion-groups">Groupes actuels</h2>
          {!locked ? (
            <button
              className="button"
              disabled={selectedGroups.length < 2 || pending}
              onClick={submitMerge}
            >
              Fusionner les groupes sélectionnés
            </button>
          ) : null}
        </div>
        {!locked ? <p className="fusion-effect">{MERGE_EFFECT}</p> : null}
        {data.groups.map((group) => (
          <GroupCard
            key={group.discovery_subject_id}
            group={group}
            locked={locked}
            pending={pending}
            selected={selectedGroups.includes(group.discovery_subject_id)}
            splitIds={splitSelection[group.discovery_subject_id] ?? []}
            onToggleGroup={(checked) => toggleGroup(group, checked)}
            onToggleCandidate={(id, value) => toggleSplit(group, id, value)}
            onSplit={() => submitSplit(group)}
          />
        ))}
      </section>

      <section className="fusion-section" aria-labelledby="fusion-pending">
        <h2 id="fusion-pending">Candidates non encore stabilisées</h2>
        <TextList
          items={data.unstabilized_candidates.map((item) => item.title)}
          empty={NO_UNSTABILIZED}
        />
      </section>
    </section>
  );
}

function GroupCard({
  group,
  locked,
  pending,
  selected,
  splitIds,
  onToggleGroup,
  onToggleCandidate,
  onSplit,
}: {
  group: FusionGroup;
  locked: boolean;
  pending: boolean;
  selected: boolean;
  splitIds: string[];
  onToggleGroup: (checked: boolean) => void;
  onToggleCandidate: (candidateId: string, checked: boolean) => void;
  onSplit: () => void;
}) {
  const members = group.candidates;
  const canSplit =
    splitIds.length > 0 && splitIds.length < group.candidates.length;
  return (
    <article className="fusion-group-card">
      <div className="fusion-group-card__heading">
        <h3>{group.title}</h3>
        <span className="badge">{publicationCount(members)}</span>
      </div>
      {!locked ? (
        <label className="fusion-check">
          <input
            type="checkbox"
            checked={selected}
            disabled={pending}
            onChange={(event) => onToggleGroup(event.target.checked)}
          />
          Sélectionner pour fusion
        </label>
      ) : null}
      <p>{group.summary}</p>
      <dl className="fusion-facts">
        <Fact label="Dates" value={candidateDates(members)} />
        <Fact label="Acteur / campagne" value={actorsAndCampaigns(members)} />
        <Fact label="Sources" value={sourceNames(members)} />
        <Fact label="Entités techniques" value={technicalEntities(members)} />
        <Fact label="IOC visibles" value={iocSummary(members)} />
        <Fact label="Confiance" value={group.confidence ?? "—"} />
        <Fact label="Origine" value={originLabel(group.origin)} />
      </dl>
      {group.candidates.length > 1 ? (
        <CandidateComparison candidates={members} />
      ) : null}
      <Explanation
        signals={group.deterministic_signals}
        suggestion={group.model_suggestion}
        differences={group.differences}
      />
      <details className="fusion-history">
        <summary>Historique</summary>
        <TextList items={group.history.map(historyText)} empty={NO_HISTORY} />
      </details>
      {!locked && members.length > 1 ? (
        <fieldset className="fusion-split">
          <legend>Séparer des candidates</legend>
          {members.map((candidate) => (
            <label key={candidate.id}>
              <input
                type="checkbox"
                checked={splitIds.includes(candidate.id)}
                disabled={pending}
                onChange={(event) =>
                  onToggleCandidate(candidate.id, event.target.checked)
                }
              />
              {candidate.title}
            </label>
          ))}
          <p className="fusion-effect">{SPLIT_EFFECT}</p>
          <button
            className="button button--secondary"
            disabled={!canSplit || pending}
            onClick={onSplit}
          >
            Séparer les candidates sélectionnées
          </button>
        </fieldset>
      ) : null}
    </article>
  );
}

function ReviewCard({
  review,
  locked,
  pending,
  choices,
  targets,
  onChoose,
  onSubmit,
}: {
  review: FusionPendingReview;
  locked: boolean;
  pending: boolean;
  choices: Record<string, ReviewChoice>;
  targets: GroupOption[];
  onChoose: (key: string, choice: ReviewChoice) => void;
  onSubmit: (decisions: FusionReviewDecision[]) => void;
}) {
  const decisions = reviewDecisions(review, choices);
  return (
    <article className="fusion-review-card">
      <h3>{reviewTitle(review)}</h3>
      {review.stale ? (
        <p className="fusion-stale-message">{STALE_REVIEW}</p>
      ) : null}
      {review.groups.map((group, index) => (
        <ReviewGroupCard
          key={groupKey(review, index)}
          group={group}
          locked={locked}
          pending={pending}
          choice={choices[groupKey(review, index)]}
          targets={targets}
          onChoose={(choice) => onChoose(groupKey(review, index), choice)}
        />
      ))}
      {!locked ? (
        <button
          className="button"
          disabled={decisions === null || pending}
          onClick={() => onSubmit(decisions ?? [])}
        >
          Appliquer les décisions
        </button>
      ) : null}
    </article>
  );
}

function ReviewGroupCard({
  group,
  locked,
  pending,
  choice,
  targets,
  onChoose,
}: {
  group: FusionReviewGroup;
  locked: boolean;
  pending: boolean;
  choice: ReviewChoice | undefined;
  targets: GroupOption[];
  onChoose: (choice: ReviewChoice) => void;
}) {
  const members = group.candidates;
  const target = choice?.target ?? "";
  return (
    <section className="fusion-review-group">
      <h4>{proposalText(group, targets)}</h4>
      <dl className="fusion-facts">
        <Fact label="Dates" value={candidateDates(members)} />
        <Fact label="Acteur / campagne" value={actorsAndCampaigns(members)} />
        <Fact label="Sources" value={sourceNames(members)} />
        <Fact label="Entités techniques" value={technicalEntities(members)} />
        <Fact label="IOC visibles" value={iocSummary(members)} />
        <Fact label="Confiance" value={group.confidence} />
      </dl>
      <CandidateComparison candidates={members} />
      <Explanation
        signals={group.deterministic_signals}
        suggestion={group.model_suggestion}
        differences={group.differences}
      />
      {!locked && group.requires_decision ? (
        <fieldset className="fusion-review-actions">
          <legend>Décision</legend>
          {REVIEW_ACTIONS.map((action) => (
            <label key={action}>
              <input
                type="radio"
                checked={choice?.action === action}
                disabled={pending}
                onChange={() => onChoose({ action, target })}
              />
              {actionLabels[action]}
            </label>
          ))}
          <label>
            Groupe cible
            <select
              value={target}
              disabled={pending || choice?.action !== "attach"}
              onChange={(event) =>
                onChoose({ action: "attach", target: event.target.value })
              }
            >
              <option value="">Choisir un groupe courant</option>
              {targets.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.title}
                </option>
              ))}
            </select>
          </label>
          <p className="fusion-effect">{effectText(choice)}</p>
        </fieldset>
      ) : null}
    </section>
  );
}

function Explanation({
  signals,
  suggestion,
  differences,
}: {
  signals: FusionSignal[];
  suggestion: FusionModelSuggestion | null;
  differences: string[];
}) {
  return (
    <div className="fusion-explanation">
      <section className="fusion-zone">
        <h4>Signaux déterministes</h4>
        <TextList items={signals.map(signalText)} empty={NO_SIGNAL} />
      </section>
      <aside className="fusion-zone fusion-zone--model">
        <h4>Suggestion du modèle</h4>
        <p className="fusion-model-warning">{MODEL_WARNING}</p>
        <p>{suggestionText(suggestion)}</p>
      </aside>
      <section className="fusion-zone">
        <h4>Différences</h4>
        <TextList items={differences} empty={NO_DIFFERENCE} />
      </section>
    </div>
  );
}

function CandidateComparison({
  candidates,
}: {
  candidates: FusionCandidate[];
}) {
  return (
    <table className="fusion-comparison">
      <thead>
        <tr>
          <th scope="col">Critère</th>
          {candidates.map((candidate) => (
            <th scope="col" key={candidate.id}>
              {candidate.title}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {COMPARISON_ROWS.map(([label, read]) => (
          <tr key={label}>
            <th scope="row">{label}</th>
            {candidates.map((candidate) => (
              <td key={candidate.id}>{read(candidate)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function TextList({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <p className="fusion-empty">{empty}</p>;
  return (
    <ul>
      {items.map((item, index) => (
        <li key={`${index}:${item}`}>{item}</li>
      ))}
    </ul>
  );
}

function groupKey(review: FusionPendingReview, index: number): string {
  return `${review.merge_run_id}:${index}`;
}

function reviewDecisions(
  review: FusionPendingReview,
  choices: Record<string, ReviewChoice>,
): FusionReviewDecision[] | null {
  const decisions: FusionReviewDecision[] = [];
  for (const [index, group] of review.groups.entries()) {
    if (!group.requires_decision) continue;
    const choice = choices[groupKey(review, index)];
    if (!choice) return null;
    if (choice.action === "attach" && !choice.target) return null;
    const decision: FusionReviewDecision = {
      action: choice.action,
      candidate_ids: group.candidate_ids,
    };
    if (choice.action === "attach") {
      decision.target_discovery_subject_id = choice.target;
    }
    decisions.push(decision);
  }
  return decisions.length > 0 ? decisions : null;
}

function toggled(values: string[], value: string, checked: boolean) {
  const rest = values.filter((item) => item !== value);
  return checked ? [...rest, value] : rest;
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

function formatList(values: string[]): string {
  return values.length > 0 ? values.join(", ") : "—";
}

function candidateDates(candidates: FusionCandidate[]): string {
  const dates = candidates.flatMap((item) => item.event_date ?? []);
  dates.sort((left, right) => left.localeCompare(right));
  const first = dates[0];
  const last = dates[dates.length - 1];
  if (!first || !last) return "—";
  return first === last ? first : `${first} → ${last}`;
}

function actorsAndCampaigns(candidates: FusionCandidate[]): string {
  const actors = candidates.flatMap((item) => item.actors);
  const campaigns = candidates.flatMap((item) => item.campaigns);
  return formatList(unique([...actors, ...campaigns]));
}

function technicalEntities(candidates: FusionCandidate[]): string {
  const malware = candidates.flatMap((item) => item.malware);
  const cves = candidates.flatMap((item) => item.cves);
  return formatList(unique([...malware, ...cves]));
}

function sourceNames(candidates: FusionCandidate[]): string {
  const publications = candidates.flatMap((item) => item.publications);
  return formatList(unique(publications.map((item) => item.publisher)));
}

function iocSummary(candidates: FusionCandidate[]): string {
  const iocs = unique(candidates.flatMap((item) => item.iocs));
  if (iocs.length === 0) return "aucun";
  return `${iocs.length} : ${iocs.slice(0, 5).join(", ")}`;
}

function publicationCount(candidates: FusionCandidate[]): string {
  const publications = candidates.flatMap((item) => item.publications);
  return `${candidates.length} candidate(s) · ${publications.length} publication(s)`;
}

function versionLabel(version: number | null): string {
  return version === null ? "aucun" : `v${version}`;
}

function signalText(signal: FusionSignal): string {
  const label = signalLabels[signal.kind] ?? signal.kind;
  return `${label} : ${signal.value} (${signal.candidate_ids.length})`;
}

function suggestionText(suggestion: FusionModelSuggestion | null): string {
  if (!suggestion) return NO_MODEL_SUGGESTION;
  const label = recommendationLabels[suggestion.recommendation];
  return `Recommandation : ${label ?? suggestion.recommendation}. ${suggestion.summary}`;
}

function originLabel(kind: string | null): string {
  if (!kind) return "—";
  return originLabels[kind] ?? kind;
}

function historyText(entry: FusionHistoryEntry): string {
  const label = historyLabels[entry.action] ?? entry.action;
  const origin = originLabel(entry.planner_kind);
  return `${entry.created_at.slice(0, 10)} · ${label} · ${origin}`;
}

function effectText(choice: ReviewChoice | undefined): string {
  return choice ? `Effet : ${actionEffects[choice.action]}` : CHOOSE_DECISION;
}

function reviewTitle(review: FusionPendingReview): string {
  const count = review.candidate_ids.length;
  return `Proposition du ${review.created_at.slice(0, 10)} · ${count} candidate(s)`;
}

function proposalText(group: FusionReviewGroup, targets: GroupOption[]) {
  const [targetId] = group.proposed_discovery_subject_ids;
  const target = targets.find((option) => option.id === targetId);
  if (target) return `Rattachement proposé à « ${target.title} »`;
  if (targetId) return "Rattachement proposé à un groupe existant";
  const count = group.candidate_ids.length;
  return count > 1 ? `${count} candidates à regrouper` : "Candidate isolée";
}
