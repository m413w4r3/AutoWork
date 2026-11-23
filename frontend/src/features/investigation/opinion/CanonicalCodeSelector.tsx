import { useEffect, useMemo, useState } from "react";
import type {
  KeyboardEvent as ReactKeyboardEvent,
  SyntheticEvent,
} from "react";

import type { InvestigationObservation } from "../../../api/investigation";
import {
  useObservationExcerpt,
  type ObservationExcerptParams,
} from "../api/observationExcerpts";
import type {
  CanonicalCodeNoteBlockV1,
  NoteContentBlockV1,
} from "../api/opinionProposals";
import { describeApiError } from "./errors";
import { observationLabel } from "./references";
import { truncate } from "./labels";

const MAX_EXCERPT_BYTES = 4096;
const CODE_LANGUAGE_PATTERN = /^[a-z0-9][a-z0-9+.#_-]*$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface ByteSpan {
  start: number;
  end: number;
}

interface DisplayExcerpt {
  value: string;
  byteSpans: Array<ByteSpan | null>;
}

interface SelectedByteRange {
  offset: number;
  length: number;
}

function textDisplayExcerpt(text: string): DisplayExcerpt {
  let value = "";
  const byteSpans: Array<ByteSpan | null> = [];

  for (let index = 0; index < text.length; index += 1) {
    const character = text[index] ?? "";
    if (character === "\r") {
      const hasLineFeed = text[index + 1] === "\n";
      value += "\n";
      byteSpans.push({ start: index, end: index + (hasLineFeed ? 2 : 1) });
      if (hasLineFeed) index += 1;
    } else {
      value += character;
      byteSpans.push({ start: index, end: index + 1 });
    }
  }

  return { value, byteSpans };
}

/** Map hex digit positions to bytes; absolute address columns are not bytes. */
function hexDisplayExcerpt(hex: string): DisplayExcerpt {
  const byteSpans: Array<ByteSpan | null> = Array.from(
    { length: hex.length },
    () => null,
  );
  let byteIndex = 0;
  let lineOffset = 0;

  for (const line of hex.split("\n")) {
    const addressPrefix = /^[0-9a-f]+ {2}/.exec(line)?.[0];
    if (addressPrefix !== undefined) {
      const payloadOffset = lineOffset + addressPrefix.length;
      const payload = line.slice(addressPrefix.length);
      const bytePattern = /[0-9a-f]{2}/g;
      for (const match of payload.matchAll(bytePattern)) {
        const localOffset = match.index;
        if (localOffset === undefined) continue;
        byteSpans[payloadOffset + localOffset] = {
          start: byteIndex,
          end: byteIndex + 1,
        };
        byteSpans[payloadOffset + localOffset + 1] = {
          start: byteIndex,
          end: byteIndex + 1,
        };
        byteIndex += 1;
      }
    }
    lineOffset += line.length + 1;
  }

  return { value: hex, byteSpans };
}

function selectedBytes(
  display: DisplayExcerpt,
  start: number,
  end: number,
): SelectedByteRange | null {
  if (start >= end) return null;
  const spans = display.byteSpans
    .slice(start, end)
    .filter((span): span is ByteSpan => span !== null);
  if (spans.length === 0) return null;

  const byteStart = Math.min(...spans.map((span) => span.start));
  const byteEnd = Math.max(...spans.map((span) => span.end));
  const length = byteEnd - byteStart;
  if (length < 1 || length > MAX_EXCERPT_BYTES) return null;
  return { offset: byteStart, length };
}

function formatOffset(offset: number): string {
  return `0x${offset.toString(16).padStart(4, "0")}`;
}

function optionLabel(
  observations: InvestigationObservation[],
  observation: InvestigationObservation,
): string {
  const object = UUID_PATTERN.test(observation.object_id)
    ? observation.object_type
    : `${observation.object_type}/${truncate(observation.object_id, 48)}`;
  return `${observationLabel(observations, observation.id)} · ${object}`;
}

function refusalMessage(error: unknown): string {
  const described = describeApiError(error);
  const status = described.status === 0 ? "" : ` (HTTP ${described.status})`;
  return `${described.message} Code serveur : ${described.code}${status}`;
}

export function CanonicalCodeSelector({
  investigationId,
  observations,
  advisoryCount,
  blocks,
  onAdd,
}: {
  investigationId: string;
  observations: InvestigationObservation[];
  advisoryCount: number;
  blocks: NoteContentBlockV1[];
  onAdd: (block: CanonicalCodeNoteBlockV1) => void;
}) {
  const [observationId, setObservationId] = useState("");
  const [pageOffset, setPageOffset] = useState(0);
  const [view, setView] = useState<"text" | "hex">("text");
  const [textFallback, setTextFallback] = useState(false);
  const [selectedRange, setSelectedRange] = useState<SelectedByteRange | null>(
    null,
  );
  const [keyboardCaret, setKeyboardCaret] = useState(0);
  const [keyboardAnchor, setKeyboardAnchor] = useState<number | null>(null);
  const [language, setLanguage] = useState("python");
  const [adding, setAdding] = useState(false);
  const [verificationError, setVerificationError] = useState<unknown>(null);

  const [manualOffset, setManualOffset] = useState("");
  const [manualLength, setManualLength] = useState("");
  const [manualExcerptSha, setManualExcerptSha] = useState("");

  const observation =
    observations.find((item) => item.id === observationId) ?? null;
  const metadataParams: ObservationExcerptParams = {
    offset: 0,
    length: 1,
    view: "hex",
  };
  const metadataQuery = useObservationExcerpt(
    investigationId,
    observation?.id ?? null,
    metadataParams,
    observation !== null,
  );
  const totalSize = metadataQuery.data?.total_size;
  const pageLength =
    totalSize === undefined
      ? 1
      : Math.min(MAX_EXCERPT_BYTES, Math.max(0, totalSize - pageOffset));
  const pageParams: ObservationExcerptParams = {
    offset: pageOffset,
    length: Math.max(1, pageLength),
    view,
  };
  const pageQuery = useObservationExcerpt(
    investigationId,
    observation?.id ?? null,
    pageParams,
    observation !== null && totalSize !== undefined && pageLength > 0,
  );
  const verificationParams: ObservationExcerptParams = {
    offset: selectedRange?.offset ?? 0,
    length: selectedRange?.length ?? 1,
    view,
  };
  const verificationQuery = useObservationExcerpt(
    investigationId,
    observation?.id ?? null,
    verificationParams,
    false,
  );

  const pageDisplay = useMemo(() => {
    const page = pageQuery.data;
    if (page === undefined) return null;
    return page.view === "text"
      ? textDisplayExcerpt(page.text)
      : hexDisplayExcerpt(page.hex);
  }, [pageQuery.data]);

  useEffect(() => {
    if (
      view === "text" &&
      pageQuery.error !== null &&
      describeApiError(pageQuery.error).code === "excerpt_not_safe_text"
    ) {
      setView("hex");
      setTextFallback(true);
      setSelectedRange(null);
      setKeyboardCaret(0);
      setKeyboardAnchor(null);
    }
  }, [pageQuery.error, view]);

  const selectionDuplicate = blocks.some(
    (block) =>
      block.block_type === "canonical_code" &&
      block.observation_id === observationId &&
      block.offset === selectedRange?.offset &&
      block.length === selectedRange?.length,
  );
  const languageValid = CODE_LANGUAGE_PATTERN.test(language.trim());
  const manualOffsetValue = Number(manualOffset);
  const manualLengthValue = Number(manualLength);
  const manualValid =
    observation !== null &&
    /^\d+$/.test(manualOffset.trim()) &&
    Number.isSafeInteger(manualOffsetValue) &&
    manualOffsetValue >= 0 &&
    /^\d+$/.test(manualLength.trim()) &&
    Number.isSafeInteger(manualLengthValue) &&
    manualLengthValue > 0 &&
    manualLengthValue <= MAX_EXCERPT_BYTES &&
    SHA256_PATTERN.test(manualExcerptSha.trim()) &&
    languageValid;

  function resetForObservation(nextId: string) {
    setObservationId(nextId);
    setPageOffset(0);
    setView("text");
    setTextFallback(false);
    setSelectedRange(null);
    setKeyboardCaret(0);
    setKeyboardAnchor(null);
    setVerificationError(null);
    setManualOffset("");
    setManualLength("");
    setManualExcerptSha("");
  }

  function changeView(nextView: "text" | "hex") {
    setView(nextView);
    setSelectedRange(null);
    setKeyboardCaret(0);
    setKeyboardAnchor(null);
    setVerificationError(null);
    if (nextView === "text") setTextFallback(false);
  }

  function handleExcerptSelection(event: SyntheticEvent<HTMLTextAreaElement>) {
    if (pageDisplay === null || pageQuery.data === undefined) return;
    applyDisplaySelection(
      event.currentTarget.selectionStart,
      event.currentTarget.selectionEnd,
    );
  }

  function handleExcerptMouseUp(event: SyntheticEvent<HTMLTextAreaElement>) {
    handleExcerptSelection(event);
    setKeyboardCaret(event.currentTarget.selectionEnd);
    setKeyboardAnchor(null);
  }

  function applyDisplaySelection(start: number, end: number) {
    if (pageDisplay === null || pageQuery.data === undefined) return;
    const localRange = selectedBytes(pageDisplay, start, end);
    setSelectedRange(
      localRange === null
        ? null
        : {
            offset: pageQuery.data.offset + localRange.offset,
            length: localRange.length,
          },
    );
    setVerificationError(null);
  }

  function handleExcerptKeyDown(
    event: ReactKeyboardEvent<HTMLTextAreaElement>,
  ) {
    if (pageDisplay === null) return;
    const length = pageDisplay.value.length;
    const currentCaret = Math.min(keyboardCaret, length);
    let nextCaret: number | null = null;

    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
      event.preventDefault();
      event.currentTarget.setSelectionRange(0, length);
      setKeyboardCaret(length);
      setKeyboardAnchor(0);
      applyDisplaySelection(0, length);
      return;
    }
    if (event.key === "Home") nextCaret = 0;
    else if (event.key === "End") nextCaret = length;
    else if (event.key === "ArrowLeft")
      nextCaret = Math.max(0, currentCaret - 1);
    else if (event.key === "ArrowRight")
      nextCaret = Math.min(length, currentCaret + 1);
    else return;

    event.preventDefault();
    if (event.shiftKey) {
      const anchor = keyboardAnchor ?? currentCaret;
      const selectionStart = Math.min(anchor, nextCaret);
      const selectionEnd = Math.max(anchor, nextCaret);
      event.currentTarget.setSelectionRange(selectionStart, selectionEnd);
      setKeyboardCaret(nextCaret);
      setKeyboardAnchor(anchor);
      applyDisplaySelection(selectionStart, selectionEnd);
      return;
    }

    event.currentTarget.setSelectionRange(nextCaret, nextCaret);
    setKeyboardCaret(nextCaret);
    setKeyboardAnchor(null);
    setSelectedRange(null);
    setVerificationError(null);
  }

  async function addSelectedExcerpt() {
    if (
      observation === null ||
      selectedRange === null ||
      !languageValid ||
      selectionDuplicate
    ) {
      return;
    }

    setAdding(true);
    setVerificationError(null);
    try {
      const result = await verificationQuery.refetch({ throwOnError: true });
      const excerpt = result.data;
      if (excerpt === undefined) return;
      onAdd({
        block_type: "canonical_code",
        observation_id: observation.id,
        blob_sha256: excerpt.blob_sha256,
        offset: excerpt.offset,
        length: excerpt.length,
        excerpt_sha256: excerpt.excerpt_sha256,
        language: language.trim(),
      });
    } catch (error) {
      setVerificationError(error);
    } finally {
      setAdding(false);
    }
  }

  function addManualDiagnosticExcerpt() {
    if (observation === null || !manualValid) return;
    onAdd({
      block_type: "canonical_code",
      observation_id: observation.id,
      blob_sha256: observation.sha256,
      offset: manualOffsetValue,
      length: manualLengthValue,
      excerpt_sha256: manualExcerptSha.trim(),
      language: language.trim(),
    });
    setManualOffset("");
    setManualLength("");
    setManualExcerptSha("");
  }

  const pageError = pageQuery.error;
  const pageErrorCode =
    pageError === null ? null : describeApiError(pageError).code;
  const pageErrorMessage =
    pageError === null ? null : refusalMessage(pageError);

  return (
    <fieldset className="canonical-code-selector">
      <legend>Extrait de code canonique</legend>
      <p>
        Choisissez une plage dans l’extrait canonique affiché. Le serveur relit
        ensuite exactement ces octets et fournit les métadonnées du bloc ; il
        revalidera encore la référence lors de la proposition.
      </p>
      {observations.length === 0 ? (
        <p>
          Aucune observation canonique disponible dans cette enquête.
          {advisoryCount === 0
            ? ""
            : ` ${advisoryCount} observation(s) consultative(s) sont exclues.`}
        </p>
      ) : (
        <>
          <label htmlFor="note-content-code-observation">
            Observation source
          </label>
          <select
            id="note-content-code-observation"
            value={observationId}
            onChange={(event) => resetForObservation(event.currentTarget.value)}
          >
            <option value="">— Choisir une observation —</option>
            {observations.map((item) => {
              const label = optionLabel(observations, item);
              return (
                <option key={item.id} value={item.id} title={label}>
                  {truncate(label, 120)}
                </option>
              );
            })}
          </select>

          {observation === null ? (
            <p role="note">Choisissez une observation canonique.</p>
          ) : (
            <>
              {metadataQuery.isPending ? (
                <p role="status">Lecture des métadonnées de l’observation…</p>
              ) : null}
              {metadataQuery.error !== null ? (
                <p role="alert">{refusalMessage(metadataQuery.error)}</p>
              ) : null}
              {metadataQuery.data !== undefined ? (
                <>
                  <div
                    className="canonical-code-selector__view-switch"
                    role="group"
                    aria-label="Vue de l’extrait"
                  >
                    <button
                      type="button"
                      className="button button--secondary"
                      aria-pressed={view === "text"}
                      onClick={() => changeView("text")}
                    >
                      Texte
                    </button>
                    <button
                      type="button"
                      className="button button--secondary"
                      aria-pressed={view === "hex"}
                      onClick={() => changeView("hex")}
                    >
                      Hexadécimal
                    </button>
                  </div>

                  {textFallback ? (
                    <p role="note">
                      La vue texte a été refusée (
                      <code>excerpt_not_safe_text</code>) car l’extrait contient
                      des octets non imprimables ; affichage automatique en
                      hexadécimal.
                    </p>
                  ) : null}
                  {pageQuery.isPending ? (
                    <p role="status">Lecture de la fenêtre de l’extrait…</p>
                  ) : null}
                  {pageError !== null &&
                  pageErrorCode !== "excerpt_not_safe_text" ? (
                    <p role="alert">{pageErrorMessage}</p>
                  ) : null}
                  {pageQuery.data !== undefined && pageDisplay !== null ? (
                    <>
                      <p>
                        Fenêtre de {pageQuery.data.length} octets à partir de{" "}
                        {formatOffset(pageQuery.data.offset)} sur{" "}
                        {pageQuery.data.total_size} octets.
                      </p>
                      <div className="canonical-code-selector__pagination">
                        <button
                          type="button"
                          className="button button--secondary"
                          disabled={pageOffset === 0}
                          onClick={() => {
                            setPageOffset(
                              Math.max(0, pageOffset - MAX_EXCERPT_BYTES),
                            );
                            setSelectedRange(null);
                            setKeyboardCaret(0);
                            setKeyboardAnchor(null);
                            setVerificationError(null);
                          }}
                        >
                          Fenêtre précédente
                        </button>
                        <button
                          type="button"
                          className="button button--secondary"
                          disabled={
                            pageOffset + pageQuery.data.length >=
                            pageQuery.data.total_size
                          }
                          onClick={() => {
                            setPageOffset(pageOffset + MAX_EXCERPT_BYTES);
                            setSelectedRange(null);
                            setKeyboardCaret(0);
                            setKeyboardAnchor(null);
                            setVerificationError(null);
                          }}
                        >
                          Fenêtre suivante
                        </button>
                      </div>
                      <label htmlFor="note-content-code-excerpt">
                        Extrait canonique en{" "}
                        {view === "text" ? "texte" : "hexadécimal"}
                      </label>
                      <textarea
                        key={`${observation.id}-${pageOffset}-${view}`}
                        id="note-content-code-excerpt"
                        className="canonical-code-selector__excerpt"
                        aria-describedby="note-content-code-selection-help"
                        readOnly
                        spellCheck={false}
                        autoComplete="off"
                        value={pageDisplay.value}
                        onSelect={handleExcerptSelection}
                        onMouseUp={handleExcerptMouseUp}
                        onKeyDown={handleExcerptKeyDown}
                      />
                      <p id="note-content-code-selection-help">
                        Sélectionnez à la souris ou au clavier une plage
                        contiguë d’octets dans cette fenêtre.
                      </p>
                      <p role="status" aria-live="polite">
                        {selectedRange === null
                          ? "Aucune plage sélectionnée."
                          : `${selectedRange.length} octets à partir de ${formatOffset(selectedRange.offset)}.`}
                      </p>
                      <label htmlFor="note-content-code-language">
                        Langage
                      </label>
                      <input
                        id="note-content-code-language"
                        value={language}
                        onChange={(event) =>
                          setLanguage(event.currentTarget.value)
                        }
                      />
                      {languageValid ? null : (
                        <p role="note">
                          Le langage doit être un jeton court en minuscules (ex.
                          python, c, bash).
                        </p>
                      )}
                      {selectedRange !== null ? (
                        <details>
                          <summary>Détails techniques</summary>
                          <dl>
                            <dt>Offset (octets)</dt>
                            <dd>
                              <output>
                                {verificationQuery.data?.offset ??
                                  selectedRange.offset}
                              </output>
                            </dd>
                            <dt>Longueur (octets)</dt>
                            <dd>
                              <output>
                                {verificationQuery.data?.length ??
                                  selectedRange.length}
                              </output>
                            </dd>
                            <dt>SHA-256 de l’extrait</dt>
                            <dd>
                              <output>
                                {verificationQuery.data?.excerpt_sha256 ??
                                  "fourni par le serveur lors de l’ajout"}
                              </output>
                            </dd>
                          </dl>
                        </details>
                      ) : null}
                      {verificationError !== null ? (
                        <p role="alert">{refusalMessage(verificationError)}</p>
                      ) : null}
                      <button
                        type="button"
                        className="button button--secondary"
                        disabled={
                          selectedRange === null ||
                          !languageValid ||
                          selectionDuplicate ||
                          adding
                        }
                        onClick={() => void addSelectedExcerpt()}
                      >
                        {adding
                          ? "Vérification serveur…"
                          : "Vérifier et référencer la plage"}
                      </button>
                      {selectionDuplicate ? (
                        <p role="note">
                          Cette plage d’octets est déjà référencée par cette
                          proposition.
                        </p>
                      ) : null}
                    </>
                  ) : null}

                  <details className="canonical-code-selector__manual">
                    <summary>Saisie manuelle (repli de diagnostic)</summary>
                    <p>
                      Utilisez ce repli uniquement pour diagnostiquer une
                      sélection impossible. Le serveur revalide toujours la
                      référence.
                    </p>
                    <label htmlFor="note-content-code-manual-offset">
                      Offset manuel (octets)
                    </label>
                    <input
                      id="note-content-code-manual-offset"
                      inputMode="numeric"
                      value={manualOffset}
                      onChange={(event) =>
                        setManualOffset(event.currentTarget.value)
                      }
                    />
                    <label htmlFor="note-content-code-manual-length">
                      Longueur manuelle (octets)
                    </label>
                    <input
                      id="note-content-code-manual-length"
                      inputMode="numeric"
                      value={manualLength}
                      onChange={(event) =>
                        setManualLength(event.currentTarget.value)
                      }
                    />
                    <label htmlFor="note-content-code-manual-sha">
                      SHA-256 manuel de l’extrait
                    </label>
                    <input
                      id="note-content-code-manual-sha"
                      value={manualExcerptSha}
                      onChange={(event) =>
                        setManualExcerptSha(event.currentTarget.value)
                      }
                    />
                    <button
                      type="button"
                      className="button button--secondary"
                      disabled={!manualValid}
                      onClick={addManualDiagnosticExcerpt}
                    >
                      Référencer manuellement (diagnostic)
                    </button>
                  </details>
                </>
              ) : null}
            </>
          )}
        </>
      )}
    </fieldset>
  );
}
