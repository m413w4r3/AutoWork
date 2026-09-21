import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  confirmSelectionDecisions,
  fetchSelectionBoard,
  type SelectionBoard as SelectionBoardData,
  type SelectionDecision,
  type SelectionItem,
} from "../../api/selection";
import { ApiError } from "../../api/editions";
import { Link } from "../../routing";

const IOC_TYPE_LABELS: Record<string, string> = {
  ipv4: "IPv4",
  ipv6: "IPv6",
  domain: "domaines",
  url: "URL",
  md5: "MD5",
  sha1: "SHA-1",
  sha256: "SHA-256",
  email: "adresses e-mail",
  cve: "CVE",
};

const staleMessages: Record<string, string> = {
  selection_snapshot_stale:
    "La sélection a changé. Rechargez la page avant de confirmer.",
  selection_decision_stale:
    "Une décision a changé. Rechargez la page avant de confirmer.",
};

export function SelectionBoard({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, SelectionDecision>>({});
  const [reloadMessage, setReloadMessage] = useState<string | null>(null);
  const board = useQuery({
    queryKey: ["selection-board", editionId],
    queryFn: () => fetchSelectionBoard(editionId),
    refetchInterval: false,
  });

  const confirmation = useMutation({
    mutationFn: (payload: {
      board: SelectionBoardData;
      snapshotVersion: number;
      decisions: Array<[string, SelectionDecision]>;
    }) =>
      confirmSelectionDecisions(
        editionId,
        {
          snapshot_version: payload.snapshotVersion,
          decisions: payload.decisions.map(([discoverySubjectId, decision]) => {
            const item = payload.board.items.find(
              (candidate) =>
                candidate.discovery_subject_id === discoverySubjectId,
            );
            return {
              discovery_subject_id: discoverySubjectId,
              decision,
              expected_decision_id: item?.last_decision?.id ?? null,
            };
          }),
        },
        createIdempotencyKey(),
      ),
    onSuccess: (updated) => {
      queryClient.setQueryData(["selection-board", editionId], updated);
      setDrafts({});
      setReloadMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["selection-board", editionId],
      });
    },
    onError: (error) => {
      const staleMessage =
        error instanceof ApiError ? staleMessages[error.code] : undefined;
      if (staleMessage) {
        setDrafts({});
        setReloadMessage(staleMessage);
        void queryClient.invalidateQueries({
          queryKey: ["selection-board", editionId],
        });
      }
    },
  });

  if (board.isPending) return <p role="status">Chargement de la sélection…</p>;
  if (board.isError) {
    return (
      <p role="alert" className="error-message">
        La sélection est inaccessible.
      </p>
    );
  }

  const data = board.data;
  const snapshotVersion = data.snapshot_version;
  const draftEntries = Object.entries(drafts);
  const counts = {
    undecided: data.counts.undecided,
    ignored: data.counts.ignored,
    selected: data.counts.selected,
  };
  for (const [id, decision] of draftEntries) {
    const item = data.items.find(
      (candidate) => candidate.discovery_subject_id === id,
    );
    if (!item || item.state === "selected") continue;
    if (item.state === "ignored") {
      if (decision === "select") {
        counts.ignored -= 1;
        counts.selected += 1;
      }
    } else {
      counts.undecided -= 1;
      if (decision === "select") counts.selected += 1;
      else counts.ignored += 1;
    }
  }

  return (
    <section
      className="selection-board"
      aria-labelledby="selection-board-heading"
    >
      <div className="selection-board__heading">
        <div>
          <p className="eyebrow">Sélection canonique</p>
          <h1 id="selection-board-heading">Sélection des sujets</h1>
          <p className="selection-board__summary">
            Les sujets issus de Fusion sont traités ici avant leur création.
          </p>
        </div>
        <dl
          className="selection-board__counts"
          aria-label="État de la sélection"
        >
          <div>
            <dt>À décider</dt>
            <dd>{counts.undecided}</dd>
          </div>
          <div>
            <dt>Ignorés</dt>
            <dd>{counts.ignored}</dd>
          </div>
          <div>
            <dt>Sujets créés</dt>
            <dd>{counts.selected}</dd>
          </div>
        </dl>
      </div>
      {readOnly ? (
        <p className="workflow-read-only-note" role="note">
          Consultation historique : les décisions ne sont pas modifiables.
        </p>
      ) : null}
      {reloadMessage ? (
        <p className="selection-board__stale" role="alert">
          {reloadMessage}
        </p>
      ) : null}
      {confirmation.error && !reloadMessage ? (
        <p className="error-message" role="alert">
          {confirmation.error.message}
        </p>
      ) : null}
      {data.recommendation ? (
        <p className="selection-board__recommendation" role="note">
          <strong>Recommandation :</strong> {data.recommendation}
        </p>
      ) : null}
      <div className="selection-board__items">
        {data.items.map((item) => (
          <SelectionCard
            key={item.discovery_subject_id}
            editionId={editionId}
            item={item}
            draft={drafts[item.discovery_subject_id]}
            pending={confirmation.isPending}
            readOnly={readOnly}
            onDecision={(decision) =>
              setDrafts((current) => ({
                ...current,
                [item.discovery_subject_id]: decision,
              }))
            }
          />
        ))}
      </div>
      {!readOnly ? (
        <button
          className="button selection-board__confirm"
          disabled={
            draftEntries.length === 0 ||
            confirmation.isPending ||
            snapshotVersion === null
          }
          onClick={() =>
            snapshotVersion === null
              ? undefined
              : confirmation.mutate({
                  board: data,
                  snapshotVersion,
                  decisions: draftEntries,
                })
          }
        >
          {confirmation.isPending
            ? "Confirmation…"
            : `Confirmer les décisions (${draftEntries.length})`}
        </button>
      ) : null}
    </section>
  );
}

function SelectionCard({
  editionId,
  item,
  draft,
  pending,
  readOnly,
  onDecision,
}: {
  editionId: string;
  item: SelectionItem;
  draft: SelectionDecision | undefined;
  pending: boolean;
  readOnly: boolean;
  onDecision: (decision: SelectionDecision) => void;
}) {
  const summary = item.summary ?? item.presentation;
  const artifacts = item.announced_artifacts.length
    ? item.announced_artifacts
    : (item.artifacts ?? []);
  const effectiveState = draftState(item.state, draft);
  const isFusionBlocked = item.state === "undecided" && !item.selectable;
  const potentialReason = item.technical_potential_reason
    ? ` — ${item.technical_potential_reason}`
    : "";
  const potential =
    item.technical_potential === null
      ? null
      : `${item.technical_potential}/4${potentialReason}`;
  const announced = item.publisher_ioc_count_total;
  const announcedLabel = announced === null ? "" : `${announced} annoncés · `;
  const count = item.provisional_ioc_count;
  const plural = count === 1 ? "" : "s";
  const iocLabel = `${announcedLabel}${count} provisoire${plural}`;
  const iocTypes = Object.entries(item.provisional_ioc_type_counts)
    .map(([type, total]) => `${total} ${IOC_TYPE_LABELS[type] ?? type}`)
    .join(" · ");
  const hasIocs = announced !== null || count > 0;

  return (
    <article className={`selection-card selection-card--${effectiveState}`}>
      <div className="selection-card__heading">
        <div>
          <p className="selection-card__state">
            {stateLabel(item.state, draft)}
          </p>
          <h2>{item.title}</h2>
        </div>
        {item.updated_since_decision ? (
          <span className="selection-card__updated">
            Mis à jour depuis la décision
          </span>
        ) : null}
      </div>
      {summary ? <p className="selection-card__summary">{summary}</p> : null}
      <dl className="selection-card__facts">
        {item.actor_or_campaign ? (
          <div>
            <dt>Acteur ou campagne</dt>
            <dd>{item.actor_or_campaign}</dd>
          </div>
        ) : null}
        <div>
          <dt>Candidats</dt>
          <dd>{item.candidate_count}</dd>
        </div>
        {potential ? (
          <div>
            <dt>Potentiel technique</dt>
            <dd>{potential}</dd>
          </div>
        ) : null}
        {artifacts.length ? (
          <div>
            <dt>Artefacts annoncés</dt>
            <dd>{artifacts.join(" · ")}</dd>
          </div>
        ) : null}
      </dl>
      {item.publications.length ? (
        <div className="selection-card__publications">
          <strong>Publications</strong>
          <ul>
            {item.publications.map((publication) => (
              <li key={publication.url}>
                <a href={publication.url}>{publication.title}</a>
                {publication.publisher ? ` — ${publication.publisher}` : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {hasIocs ? (
        <div className="selection-card__iocs">
          <strong>IOC et IOC provisoires</strong>
          <p>{iocLabel}</p>
          {iocTypes ? <p>Types : {iocTypes}</p> : null}
          {item.provisional_iocs.length ? (
            <ul>
              {item.provisional_iocs.slice(0, 5).map((ioc) => (
                <li key={`${ioc.proposed_type}:${ioc.raw_value}`}>
                  <code>{ioc.raw_value}</code>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {item.uncertainties.length ? (
        <p className="selection-card__uncertainties">
          <strong>Incertitudes :</strong> {item.uncertainties.join(" · ")}
        </p>
      ) : null}
      {item.recommendation ? (
        <p className="selection-card__recommendation">
          <strong>Recommandation :</strong> {item.recommendation}
        </p>
      ) : null}
      <p className="selection-card__effective-state">
        <strong>État effectif :</strong> {stateLabel(item.state, draft)}
      </p>
      {renderAction(
        editionId,
        item,
        draft,
        pending,
        readOnly,
        isFusionBlocked,
        onDecision,
      )}
    </article>
  );
}

function renderAction(
  editionId: string,
  item: SelectionItem,
  draft: SelectionDecision | undefined,
  pending: boolean,
  readOnly: boolean,
  isFusionBlocked: boolean,
  onDecision: (decision: SelectionDecision) => void,
) {
  if (item.state === "selected" && item.subject_id) {
    return <Link to={`/subjects/${item.subject_id}`}>Sujet créé</Link>;
  }
  if (isFusionBlocked) {
    return (
      <p className="selection-card__blocked">
        <span>À résoudre dans Fusion</span> —{" "}
        <Link to={`/editions/${editionId}/fusion`}>Ouvrir Fusion</Link>
      </p>
    );
  }
  if (readOnly) {
    return (
      <p className="selection-card__historical">
        {stateLabel(item.state, draft)}
      </p>
    );
  }
  if (item.state === "ignored") {
    return (
      <button
        className="button"
        disabled={pending}
        onClick={() => onDecision("select")}
      >
        Traiter finalement
      </button>
    );
  }
  if (item.selectable) {
    return (
      <div className="selection-card__actions">
        <button
          className="button"
          disabled={pending}
          aria-pressed={draft === "select"}
          onClick={() => onDecision("select")}
        >
          Traiter
        </button>
        <button
          className="button button-secondary"
          disabled={pending}
          aria-pressed={draft === "ignore"}
          onClick={() => onDecision("ignore")}
        >
          Ignorer
        </button>
      </div>
    );
  }
  return (
    <p className="selection-card__blocked">
      {item.blocking_reason ?? "Traitement indisponible."}
    </p>
  );
}

function draftState(
  state: SelectionItem["state"],
  draft: SelectionDecision | undefined,
): SelectionItem["state"] {
  if (draft === "select") return "selected";
  if (draft === "ignore") return "ignored";
  return state;
}

function stateLabel(
  state: SelectionItem["state"],
  draft: SelectionDecision | undefined,
): string {
  if (draft === "select") return "Décision préparée : traiter";
  if (draft === "ignore") return "Décision préparée : ignorer";
  if (state === "selected") return "Sujet créé";
  if (state === "ignored") return "Ignoré";
  return "À décider";
}

function createIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
