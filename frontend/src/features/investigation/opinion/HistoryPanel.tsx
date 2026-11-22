import { useState } from "react";

import { WorkshopReadOnlyState } from "../shell/WorkshopStates";
import { formatTimestamp } from "../tasks/labels";
import { groupNotesByAnchor } from "./anchor";
import {
  BLOCK_LABELS,
  BLOCK_ORDER,
  OPINION_STATUS_LABELS,
  shortHash,
} from "./labels";
import type { OpinionVersionV1 } from "./types";

export function HistoryPanel({ history }: { history: OpinionVersionV1[] }) {
  const [selectedId, setSelectedId] = useState("");
  const selected =
    history.find((item) => item.id === selectedId) ?? history.at(-1) ?? null;

  if (history.length === 0) {
    return (
      <section className="job-card" aria-labelledby="opinion-history-heading">
        <h3 id="opinion-history-heading">Historique des versions</h3>
        <p>Aucune version d’avis n’existe encore.</p>
      </section>
    );
  }

  return (
    <section className="job-card" aria-labelledby="opinion-history-heading">
      <h3 id="opinion-history-heading">Historique des versions</h3>
      <label htmlFor="opinion-history-version">
        Version historique à consulter
      </label>
      <select
        id="opinion-history-version"
        value={selected?.id ?? ""}
        onChange={(event) => setSelectedId(event.currentTarget.value)}
      >
        {history.map((item) => (
          <option key={`${item.id}-${item.version}`} value={item.id}>
            v{item.version} · {OPINION_STATUS_LABELS[item.status]} ·{" "}
            {shortHash(item.content_sha256)}
          </option>
        ))}
      </select>

      {selected === null ? null : (
        <section
          aria-label="Version historique en lecture seule"
          aria-readonly="true"
        >
          <h4>
            Lecture seule · version {selected.version} ·{" "}
            {OPINION_STATUS_LABELS[selected.status]}
          </h4>
          <p>
            SHA-256 <code>{selected.content_sha256}</code> · créée le{" "}
            {formatTimestamp(selected.created_at)}
          </p>
          <WorkshopReadOnlyState reason="Cette version antérieure ne peut être ni modifiée, ni approuvée, ni publiée depuis cet aperçu historique." />

          <div>
            <h5>Blocs</h5>
            {BLOCK_ORDER.map((type) => {
              const block = selected.blocks.find(
                (item) => item.type === type && item.text.trim() !== "",
              );
              if (block === undefined) return null;
              return (
                <section key={type}>
                  <h6>{BLOCK_LABELS[type]}</h6>
                  <p>{block.text}</p>
                </section>
              );
            })}
          </div>

          <div>
            <h5>Notes par ancre</h5>
            {groupNotesByAnchor(selected.observations ?? []).map((group) => (
              <section key={group.anchor}>
                <h6>
                  Ancre <code>{group.anchor}</code>
                </h6>
                {group.notes.map((note) => (
                  <p key={note.id}>{note.text}</p>
                ))}
              </section>
            ))}
          </div>
        </section>
      )}
    </section>
  );
}
