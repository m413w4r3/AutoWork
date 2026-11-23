/**
 * Common filterable timeline of one investigation.
 *
 * The server projection already deduplicates status changes and their human
 * decision, and Bridge consultation observations. Each item keeps its exact
 * canonical object id and navigation refs; advisory items are labelled
 * "non probant" and never presented as evidence. The B7 working objects
 * (handoffs, contributions, agent sessions, instructions) are appended by the
 * server as `evidence: false` and carry their ids and SHA-256 refs only.
 */

import { useMemo, useState } from "react";
import type { MouseEvent } from "react";

import { toWorkshopApiError } from "../api/client";
import {
  useTimeline,
  type TimelineActorKind,
  type TimelineItemType,
  type TimelineItemV1,
} from "../api/tasks";
import {
  actorRefLabel,
  ADAPTER_LABELS,
  DECISION_TARGET_LABELS,
  formatTimestamp,
  SESSION_STATE_LABELS,
  shortSha,
  TIMELINE_ACTOR_LABELS,
  TIMELINE_TYPE_LABELS,
} from "./labels";
import { timelineSummaryLabel } from "./timelinePresentation";

const TYPE_OPTIONS = Object.entries(TIMELINE_TYPE_LABELS) as Array<
  [TimelineItemType, string]
>;
const ACTOR_OPTIONS = Object.entries(TIMELINE_ACTOR_LABELS) as Array<
  [TimelineActorKind, string]
>;

type EvidenceFilter = "all" | "evidence" | "non_evidence";

/** B7 projections: persisted working objects, never canonical evidence. */
const WORKING_ITEM_TYPES: ReadonlySet<TimelineItemType> = new Set([
  "handoff",
  "contribution",
  "agent_session",
  "instruction",
]);

const REF_LABELS: Record<string, string> = {
  handoff_id: "Passation liée",
  contribution_id: "Contribution liée",
  instruction_id: "Instruction liée",
  task_id: "Tâche",
  event_id: "Événement",
  decision_id: "Décision",
  invocation_id: "Invocation",
  plan_version_id: "Version de plan",
  source_invocation_id: "Invocation source",
  source_observation_id: "Observation source",
  target_type: "Type de cible",
  target_id: "Cible",
  blob_id: "Blob",
  operation_key: "Clé d’opération",
  predecessor_id: "Prédécesseur",
  version: "Version",
  sha256: "SHA-256",
  workspace_id: "Workspace",
  workspace_revision_id: "Révision du workspace",
  file_relpath: "Fichier",
  file_sha256: "SHA-256 du fichier",
  observation_id: "Observation liée",
  finding_id: "Finding lié",
  validation_error_code: "Erreur de validation",
  session_id: "Session liée",
  adapter_kind: "Adaptateur",
  state: "État",
  state_version: "Version d’état",
  text_sha256: "SHA-256 du texte",
  delivery_attempts: "Tentatives de livraison",
  delivery_mode: "Mode de livraison",
  delivery_sha256: "SHA-256 de livraison",
};

/** Closed-union ref values translated; unknown tokens stay verbatim. */
const REF_VALUE_LABELS: Record<string, Record<string, string>> = {
  adapter_kind: ADAPTER_LABELS,
  state: SESSION_STATE_LABELS,
  target_type: DECISION_TARGET_LABELS,
  delivery_mode: {
    live: "Livraison directe",
    next_run: "Au prochain démarrage",
  },
};

function isDigestRef(name: string): boolean {
  return name === "sha256" || name.endsWith("_sha256");
}

/** Persisted ids: abbreviated with the full value copied on click. */
function isIdentifierRef(name: string): boolean {
  return name === "target_id" || name.endsWith("_id");
}

function taskHref(taskId: string): string {
  const url = new URL(window.location.href);
  url.searchParams.set("task", taskId);
  return `${url.pathname}${url.search}${url.hash}`;
}

function openTask(event: MouseEvent<HTMLAnchorElement>, taskId: string) {
  event.preventDefault();
  const url = new URL(window.location.href);
  if (url.searchParams.get("task") !== taskId) {
    url.searchParams.set("task", taskId);
    const state = window.history.state as unknown;
    window.history.pushState(
      {
        ...(typeof state === "object" && state !== null ? state : {}),
        investigationTask: taskId,
      },
      "",
      url,
    );
  }
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function TimelineRefValue({ name, value }: { name: string; value: string }) {
  if (name === "task_id") {
    return (
      <a href={taskHref(value)} onClick={(event) => openTask(event, value)}>
        Tâche liée
      </a>
    );
  }
  if (isDigestRef(name)) {
    return <code title={value}>{shortSha(value)}</code>;
  }
  const closed = REF_VALUE_LABELS[name];
  if (closed !== undefined) {
    return <code>{closed[value] ?? value}</code>;
  }
  if (isIdentifierRef(name)) {
    return (
      <code title={value} className="m24-id-ref">
        {shortSha(value)}
      </code>
    );
  }
  return <code>{value}</code>;
}

function itemMatchesText(item: TimelineItemV1, needle: string): boolean {
  if (needle === "") return true;
  const haystack = [
    item.summary,
    // The visible sentence must be searchable, not only the raw token.
    timelineSummaryLabel(item),
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
  taskId,
  title,
}: {
  investigationId: string;
  taskId?: string;
  title?: string;
}) {
  const timeline = useTimeline(investigationId, { limit: 50 });
  const [type, setType] = useState<TimelineItemType | "all">("all");
  const [actor, setActor] = useState<TimelineActorKind | "all">("all");
  const [evidence, setEvidence] = useState<EvidenceFilter>("all");
  const [text, setText] = useState("");

  const allItems = useMemo(() => timeline.data?.items ?? [], [timeline.data]);
  const items = useMemo(
    () =>
      allItems.filter(
        (item) => taskId === undefined || item.task_id === taskId,
      ),
    [allItems, taskId],
  );
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
      <h3 id="m24-timeline-heading">
        {title ??
          (taskId === undefined
            ? "Chronologie commune"
            : "Historique de la tâche")}
      </h3>
      <p className="m24-muted">
        {taskId === undefined
          ? "Chaque entrée pointe vers son objet métier exact. Les plans, passations, notes, contributions et consultations Bridge sont des avis ou des éléments de travail, jamais des preuves."
          : "Événements, décisions, passations et résultats liés à cette tâche. Les contributions agentiques restent non probantes tant qu’elles ne sont pas vérifiées."}
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
            <option value="non_evidence">Non probants</option>
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
        <p className="empty-state">
          {taskId === undefined
            ? "Aucun événement pour cette enquête."
            : "Aucun événement pour cette tâche."}
        </p>
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
                  {item.actor_id === null
                    ? ""
                    : ` · ${actorRefLabel(item.actor_id)}`}
                </span>
                {item.evidence ? (
                  <span className="m24-badge m24-badge--evidence">
                    Preuve canonique
                  </span>
                ) : item.item_type === "bridge_consultation" ? (
                  <span className="m24-badge m24-badge--advisory">
                    Avis Bridge — non probant
                  </span>
                ) : WORKING_ITEM_TYPES.has(item.item_type) ? (
                  <span className="m24-badge m24-badge--muted">
                    Non probant — élément de travail
                  </span>
                ) : (
                  <span className="m24-badge m24-badge--muted">
                    Non probant
                  </span>
                )}
              </div>
              <p title={item.summary}>{timelineSummaryLabel(item)}</p>
              <dl className="m24-metadata m24-metadata--refs">
                <div>
                  <dt>Objet</dt>
                  <dd>
                    <code title={item.object_id} className="m24-id-ref">
                      {shortSha(item.object_id)}
                    </code>
                  </dd>
                </div>
                {Object.entries(item.refs)
                  // The object id is already the canonical id chip; only the
                  // other refs add information.
                  .filter(
                    ([key, value]) =>
                      key === "task_id" || value !== item.object_id,
                  )
                  .map(([key, value]) => (
                    <div key={`${item.object_id}-${key}`}>
                      <dt>{REF_LABELS[key] ?? key}</dt>
                      <dd>
                        <TimelineRefValue name={key} value={value} />
                      </dd>
                    </div>
                  ))}
              </dl>
              {item.task_id === null ? null : (
                <a
                  href={taskHref(item.task_id)}
                  onClick={(event) => openTask(event, item.task_id!)}
                >
                  Voir la tâche
                </a>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
