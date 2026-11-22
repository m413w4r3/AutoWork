/**
 * Common filterable timeline of one investigation.
 *
 * The server projection already deduplicates status changes and their human
 * decision, and Bridge consultation observations. Each item keeps its exact
 * canonical object id and navigation refs; advisory items are labelled
 * "non-preuve" and never presented as evidence.
 */

import { useMemo, useState } from "react";

import { toWorkshopApiError } from "../api/client";
import {
  useTimeline,
  type TimelineActorKind,
  type TimelineItemType,
  type TimelineItemV1,
} from "../api/tasks";
import {
  formatTimestamp,
  TIMELINE_ACTOR_LABELS,
  TIMELINE_TYPE_LABELS,
} from "./labels";

const TYPE_OPTIONS = Object.entries(TIMELINE_TYPE_LABELS) as Array<
  [TimelineItemType, string]
>;
const ACTOR_OPTIONS = Object.entries(TIMELINE_ACTOR_LABELS) as Array<
  [TimelineActorKind, string]
>;

type EvidenceFilter = "all" | "evidence" | "non_evidence";

function itemMatchesText(item: TimelineItemV1, needle: string): boolean {
  if (needle === "") return true;
  const haystack = [
    item.summary,
    item.object_id,
    item.actor_id ?? "",
    ...Object.entries(item.refs).flatMap(([key, value]) => [key, value]),
  ];
  return haystack.some((value) =>
    value.toLocaleLowerCase("fr").includes(needle),
  );
}

export function TimelinePanel({
  investigationId,
}: {
  investigationId: string;
}) {
  const timeline = useTimeline(investigationId, { limit: 50 });
  const [type, setType] = useState<TimelineItemType | "all">("all");
  const [actor, setActor] = useState<TimelineActorKind | "all">("all");
  const [evidence, setEvidence] = useState<EvidenceFilter>("all");
  const [text, setText] = useState("");

  const items = useMemo(() => timeline.data?.items ?? [], [timeline.data]);
  const visible = useMemo(() => {
    const needle = text.trim().toLocaleLowerCase("fr");
    return items
      .filter((item) => type === "all" || item.item_type === type)
      .filter((item) => actor === "all" || item.actor_kind === actor)
      .filter((item) => {
        if (evidence === "all") return true;
        return evidence === "evidence" ? item.evidence : !item.evidence;
      })
      .filter((item) => itemMatchesText(item, needle));
  }, [items, type, actor, evidence, text]);

  return (
    <section className="m24-card" aria-labelledby="m24-timeline-heading">
      <h3 id="m24-timeline-heading">Chronologie commune</h3>
      <p className="m24-muted">
        Chaque entrée pointe vers son objet métier exact. Les plans, passations,
        notes, contributions et consultations Bridge sont des avis ou des
        éléments de travail, jamais des preuves.
      </p>

      <div
        className="m24-filters"
        role="group"
        aria-label="Filtres de la chronologie"
      >
        <label>
          Type d’événement
          <select
            value={type}
            onChange={(event) =>
              setType(event.target.value as TimelineItemType | "all")
            }
          >
            <option value="all">Tous</option>
            {TYPE_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Acteur
          <select
            value={actor}
            onChange={(event) =>
              setActor(event.target.value as TimelineActorKind | "all")
            }
          >
            <option value="all">Tous</option>
            {ACTOR_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Preuve
          <select
            value={evidence}
            onChange={(event) =>
              setEvidence(event.target.value as EvidenceFilter)
            }
          >
            <option value="all">Tout</option>
            <option value="evidence">Preuves canoniques</option>
            <option value="non_evidence">Non-preuves</option>
          </select>
        </label>
        <label>
          Recherche
          <input
            type="search"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Résumé, identifiant, référence…"
          />
        </label>
      </div>

      <p role="status" aria-live="polite" className="m24-muted">
        {visible.length} entrée(s) affichée(s) sur {items.length}.
      </p>

      {timeline.isPending ? (
        <p role="status">Chargement de la chronologie…</p>
      ) : timeline.isError ? (
        <p role="alert" className="error-message">
          Chronologie indisponible : {toWorkshopApiError(timeline.error).code}.
        </p>
      ) : items.length === 0 ? (
        <p className="empty-state">Aucun événement pour cette enquête.</p>
      ) : visible.length === 0 ? (
        <p className="empty-state">Aucune entrée ne correspond aux filtres.</p>
      ) : (
        <ol className="m24-timeline">
          {visible.map((item) => (
            <li
              key={`${item.item_type}-${item.object_id}`}
              className="m24-timeline__item"
            >
              <div className="m24-timeline__heading">
                <time dateTime={item.occurred_at}>
                  {formatTimestamp(item.occurred_at)}
                </time>
                <span className="m24-badge">
                  {TIMELINE_TYPE_LABELS[item.item_type]}
                </span>
                <span className="m24-muted">
                  {TIMELINE_ACTOR_LABELS[item.actor_kind]}
                  {item.actor_id === null ? "" : ` · ${item.actor_id}`}
                </span>
                {item.evidence ? (
                  <span className="m24-badge m24-badge--evidence">
                    Preuve canonique
                  </span>
                ) : item.item_type === "bridge_consultation" ? (
                  <span className="m24-badge m24-badge--advisory">
                    Avis Bridge — non preuve
                  </span>
                ) : (
                  <span className="m24-badge m24-badge--muted">Non-preuve</span>
                )}
              </div>
              <p>{item.summary}</p>
              <dl className="m24-metadata m24-metadata--refs">
                <div>
                  <dt>Objet</dt>
                  <dd>
                    <code>{item.object_id}</code>
                  </dd>
                </div>
                {Object.entries(item.refs).map(([key, value]) => (
                  <div key={`${item.object_id}-${key}`}>
                    <dt>{key}</dt>
                    <dd>
                      <code>{value}</code>
                    </dd>
                  </div>
                ))}
              </dl>
              {item.task_id === null ? null : (
                <a href={`#task-${item.task_id}`}>Voir la tâche</a>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
