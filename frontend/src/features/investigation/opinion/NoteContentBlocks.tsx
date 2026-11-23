/**
 * Typed Note content blocks (M24.1 B5a contract, F3).
 *
 * The human picks REFERENCES only: M23 elements already rendered in the pinned
 * article, exact byte ranges of canonical observation blobs, canonical
 * non-advisory observations, and protocol figures carried by the opinion. No
 * prose field is offered, because Bridge remains the only author of publishable
 * text (`bridge_only`); article captions stay edited in the M23 workshop. The
 * server revalidates every reference against the pinned base and refuses a
 * stale one with a typed code; this view only previews the resolved server
 * projection and offers a reload action.
 */

import { useId, useMemo, useState } from "react";

import type { InvestigationObservation } from "../../../api/investigation";
import {
  useArticleAnchors,
  type ArticleElementViewV1,
} from "../api/articleAnchors";
import type {
  ArticleElementNoteBlockV1,
  CanonicalCodeNoteBlockV1,
  IocTableNoteBlockV1,
  NoteArticleElementKind,
  NoteContentBlockV1,
  ProtocolFigureKind,
  ProtocolFigureNoteBlockV1,
  ResolvedNoteContentBlockV1,
} from "../api/opinionProposals";
import { describeApiError, describeErrorCode } from "./errors";
import {
  ARTICLE_ELEMENT_REFUSAL_LABELS,
  EVIDENCE_KIND_LABELS,
  PROTOCOL_FIGURE_LABELS,
  shortHash,
  truncate,
} from "./labels";
import type { OpinionProtocolFigureV1 } from "./types";

const ARTICLE_ELEMENT_KIND_LABELS: Record<NoteArticleElementKind, string> = {
  table: "Tableau",
  chart: "Graphique",
  figure: "Figure",
  diagram: "Diagramme",
};

/** Mirrors the server union bounds (`MAX_NOTE_IOC_OBSERVATIONS`). */
const MAX_IOC_OBSERVATIONS = 64;
const OPTION_TEXT_MAX = 120;

const CODE_LANGUAGE_PATTERN = /^[a-z0-9][a-z0-9+.#_-]*$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

/** The opinion wire type keeps `kind` as text; the server enum is closed. */
function isProtocolFigureKind(value: string): value is ProtocolFigureKind {
  return (
    value === "sequence" ||
    value === "state_machine" ||
    value === "message_format"
  );
}

/**
 * Advisory observations (Bridge advice) are readable in the consultation
 * ledger but are never canonical evidence; the server mirrors this predicate.
 */
function isAdvisoryObservation(observation: InvestigationObservation): boolean {
  return (
    observation.source.startsWith("advisor.") ||
    observation.object_type.startsWith("advisor_")
  );
}

function observationLabel(observation: InvestigationObservation): string {
  const kind = EVIDENCE_KIND_LABELS[observation.kind] ?? observation.kind;
  return `${kind} · ${observation.object_type}/${truncate(
    observation.object_id,
    OPTION_TEXT_MAX,
  )} · SHA-256 ${shortHash(observation.sha256)} · ${observation.source}`;
}

function elementLabel(element: ArticleElementViewV1): string {
  const title = element.title ?? element.key;
  const caption =
    element.caption === null ? "" : ` · ${truncate(element.caption, 80)}`;
  return `${ARTICLE_ELEMENT_KIND_LABELS[element.kind]} · ${truncate(
    title,
    OPTION_TEXT_MAX,
  )}${caption} · SHA-256 ${shortHash(element.element_sha256)}`;
}

/** The server's typed refusal, translated; unknown codes stay readable. */
function elementRefusalLabel(reason: string | null): string {
  if (reason === null) return "élément non proposable";
  return ARTICLE_ELEMENT_REFUSAL_LABELS[reason] ?? describeErrorCode(reason);
}

function parseInteger(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function sameObservationSet(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const leftSorted = [...left].sort();
  const rightSorted = [...right].sort();
  return leftSorted.every((item, index) => item === rightSorted[index]);
}

function articleElementSelected(
  blocks: NoteContentBlockV1[],
  element: ArticleElementViewV1,
): boolean {
  return blocks.some(
    (block) =>
      block.block_type === "article_element" &&
      block.kind === element.kind &&
      block.element_key === element.key,
  );
}

/** Provenance of the pinned publication a block set is resolved against. */
export interface NoteContentBasePublicationV1 {
  artifactId: string | null;
  version: number | null;
  sha256: string | null;
}

export interface NoteContentBlocksProps {
  investigationId: string;
  anchor: string;
  observations: InvestigationObservation[];
  protocolFigures?: OpinionProtocolFigureV1[];
  blocks: NoteContentBlockV1[];
  /** Selection mode: attach or remove references for the next request. */
  onChange?: (blocks: NoteContentBlockV1[]) => void;
  /** Review mode: the server-resolved blocks of the proposal diff. */
  resolvedBlocks?: ResolvedNoteContentBlockV1[];
  /** Review mode: typed refusal code when the references no longer resolve. */
  contentBlockError?: string | null;
  basePublication?: NoteContentBasePublicationV1 | null;
  onReload?: () => void;
  subjectId?: string | null;
}

/**
 * Choose/position block references beside the Bridge request, or render the
 * read-only resolved projection of an existing proposal. The M23 workshop is
 * the only place where article elements and captions are edited; this view
 * never duplicates that edition and never accepts note prose.
 */
export function NoteContentBlocks({
  investigationId,
  anchor,
  observations,
  protocolFigures = [],
  blocks,
  onChange,
  resolvedBlocks,
  contentBlockError = null,
  basePublication = null,
  onReload,
  subjectId = null,
}: NoteContentBlocksProps) {
  const idPrefix = useId();
  const selectable = onChange !== undefined;
  const canonicalObservations = useMemo(
    () => observations.filter((item) => !isAdvisoryObservation(item)),
    [observations],
  );
  const advisoryCount = observations.length - canonicalObservations.length;
  const invalid = contentBlockError !== null;

  function addBlock(block: NoteContentBlockV1) {
    onChange?.([...blocks, block]);
  }

  function removeBlock(index: number) {
    onChange?.(blocks.filter((_, position) => position !== index));
  }

  return (
    <div className="note-content-blocks">
      {selectable ? (
        <>
          <p>
            Les blocs ci-dessous sont des <strong>références vérifiées</strong>{" "}
            : aucun texte publiable n’est saisi ici. Le serveur résout chaque
            référence contre la base de publication épinglée et refuse toute
            référence périmée avec un code typé.
          </p>
          <ArticleElementSelector
            investigationId={investigationId}
            anchor={anchor}
            blocks={blocks}
            onAdd={addBlock}
          />
          <IocTablePicker
            observations={canonicalObservations}
            advisoryCount={advisoryCount}
            blocks={blocks}
            onAdd={addBlock}
          />
          <CanonicalCodeForm
            observations={canonicalObservations}
            advisoryCount={advisoryCount}
            blocks={blocks}
            onAdd={addBlock}
          />
          <ProtocolFigurePicker
            figures={protocolFigures}
            blocks={blocks}
            onAdd={addBlock}
          />
        </>
      ) : null}

      <section aria-labelledby={`${idPrefix}-selected`}>
        <h4 id={`${idPrefix}-selected`}>
          Références de contenu ({blocks.length})
        </h4>
        <p>
          Chaque bloc reste rendu à son placement d’article ou dérivé des octets
          canoniques ; la Note ne duplique jamais un tableau, une figure ou une
          légende.
        </p>
        {blocks.length === 0 ? (
          <p>Aucune référence de contenu n’est attachée à cette proposition.</p>
        ) : (
          <ul className="note-content-blocks__list">
            {blocks.map((block, index) => (
              <li key={`${block.block_type}-${index}`}>
                <SelectedBlockSummary block={block} />
                {selectable ? (
                  <button
                    type="button"
                    className="button button--secondary"
                    onClick={() => removeBlock(index)}
                  >
                    Retirer cette référence
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      {resolvedBlocks === undefined ? null : (
        <section aria-labelledby={`${idPrefix}-resolved`}>
          <h4 id={`${idPrefix}-resolved`}>
            Aperçu lecture seule des blocs vérifiés
          </h4>
          {invalid ? (
            <div className="verification-warning" role="alert">
              <p>
                Blocs de contenu invalides :{" "}
                {describeErrorCode(contentBlockError)} · code :{" "}
                <code>{contentBlockError}</code>
              </p>
              {onReload === undefined ? null : (
                <button type="button" onClick={onReload}>
                  Recharger la proposition
                </button>
              )}
            </div>
          ) : null}
          <p>
            {basePublication === null ||
            basePublication.artifactId === null ||
            basePublication.sha256 === null
              ? "Base de publication : non épinglée (proposition antérieure au pin)."
              : `Base de publication épinglée : artefact ${basePublication.artifactId} · version ${
                  basePublication.version ?? "—"
                } · SHA-256 ${shortHash(basePublication.sha256)}`}
          </p>
          {resolvedBlocks.length === 0 ? (
            invalid ? null : (
              <p>Aucun bloc résolu pour cette proposition.</p>
            )
          ) : (
            <ul className="note-content-blocks__resolved">
              {resolvedBlocks.map((block, index) => (
                <li key={`${block.block_type}-${index}`}>
                  <ResolvedBlockPreview block={block} />
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {subjectId === null ? null : (
        <p>
          Les tableaux, figures et légendes s’éditent dans l’atelier éditorial ;
          toute modification de l’article crée une nouvelle version de
          publication et rend la proposition périmée jusqu’à rebase.{" "}
          <a href={`/subjects/${encodeURIComponent(subjectId)}/editor`}>
            Ouvrir dans l’atelier éditorial
          </a>
        </p>
      )}
    </div>
  );
}

function ArticleElementSelector({
  investigationId,
  anchor,
  blocks,
  onAdd,
}: {
  investigationId: string;
  anchor: string;
  blocks: NoteContentBlockV1[];
  onAdd: (block: ArticleElementNoteBlockV1) => void;
}) {
  const query = useArticleAnchors(investigationId);
  const [choice, setChoice] = useState("");
  const catalog = query.data ?? null;
  const elements = useMemo(
    () =>
      catalog === null
        ? []
        : catalog.elements.filter((element) => element.anchor === anchor),
    [catalog, anchor],
  );
  const proposableCount = elements.filter(
    (element) => element.proposable,
  ).length;
  const selected =
    elements.find(
      (element) =>
        element.proposable && `${element.kind}:${element.key}` === choice,
    ) ?? null;
  const duplicate =
    selected === null ? false : articleElementSelected(blocks, selected);

  function addSelected() {
    if (selected === null || selected.anchor === null) return;
    onAdd({
      block_type: "article_element",
      kind: selected.kind,
      element_key: selected.key,
      element_sha256: selected.element_sha256,
      anchor: selected.anchor,
    });
    setChoice("");
  }

  return (
    <fieldset>
      <legend>Élément M23 rendu à l’ancre de la Note</legend>
      <p>
        La Note porte une <strong>cross-référence</strong> vers un tableau,
        graphique, figure ou diagramme déjà rendu à cette ancre dans la
        publication épinglée. L’élément reste rendu à son placement d’article et
        n’est jamais dupliqué ; ses légendes s’éditent dans l’atelier éditorial.
      </p>
      {query.isPending ? (
        <p role="status">Chargement des éléments M23…</p>
      ) : null}
      {query.isError ? (
        <div className="verification-warning" role="alert">
          <p>
            Éléments de l’article indisponibles :{" "}
            {describeApiError(query.error).message} · code :{" "}
            {describeApiError(query.error).code}
          </p>
          <button type="button" onClick={() => void query.refetch()}>
            Recharger les éléments
          </button>
        </div>
      ) : null}
      {catalog === null ? null : (
        <>
          <p>
            Base des éléments M23 :{" "}
            {catalog.source === "pinned_opinion_base"
              ? "base épinglée de l’avis"
              : "base courante vérifiée"}{" "}
            · artefact <code>{catalog.base_publication_artifact_id}</code> ·
            version {catalog.base_publication_version} · SHA-256{" "}
            <code title={catalog.base_publication_sha256}>
              {shortHash(catalog.base_publication_sha256)}
            </code>
          </p>
          {elements.length === 0 ? (
            <p>
              Aucun élément M23 n’est rendu à l’ancre <code>{anchor}</code>. Un
              élément placé sur une autre ancre ne peut pas être référencé ici (
              <code>note_block_ref_stale</code>).
            </p>
          ) : (
            <>
              <label htmlFor="note-content-block-element">
                Élément à référencer
              </label>
              <select
                id="note-content-block-element"
                value={selected === null ? "" : choice}
                onChange={(event) => setChoice(event.currentTarget.value)}
              >
                <option value="">— Choisir un élément de l’article —</option>
                {elements.map((element) => (
                  <option
                    key={`${element.kind}:${element.key}`}
                    value={`${element.kind}:${element.key}`}
                    disabled={!element.proposable}
                  >
                    {element.proposable
                      ? elementLabel(element)
                      : `${elementLabel(element)} — non proposable : ${elementRefusalLabel(
                          element.refusal_reason,
                        )}`}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="button button--secondary"
                disabled={selected === null || duplicate}
                onClick={addSelected}
              >
                Référencer cet élément
              </button>
              {duplicate ? (
                <p role="note">
                  Cet élément est déjà référencé par cette proposition.
                </p>
              ) : null}
              {elements.length > proposableCount ? (
                <p role="note">
                  {elements.length - proposableCount === 1
                    ? "Un élément M23 de cette ancre est grisé"
                    : `${elements.length - proposableCount} éléments M23 de cette ancre sont grisés`}{" "}
                  : le serveur refuserait le bloc correspondant et la raison est
                  indiquée dans la liste.{" "}
                  {proposableCount === 0
                    ? "Aucun élément de cette ancre n’est donc proposable comme bloc de Note."
                    : null}
                </p>
              ) : null}
            </>
          )}
        </>
      )}
    </fieldset>
  );
}

function IocTablePicker({
  observations,
  advisoryCount,
  blocks,
  onAdd,
}: {
  observations: InvestigationObservation[];
  advisoryCount: number;
  blocks: NoteContentBlockV1[];
  onAdd: (block: IocTableNoteBlockV1) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const candidates = observations.slice(0, MAX_IOC_OBSERVATIONS);
  const duplicate = blocks.some(
    (block) =>
      block.block_type === "ioc_table" &&
      sameObservationSet(block.observation_ids, selected),
  );

  function toggle(id: string) {
    setSelected((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id],
    );
  }

  function addTable() {
    if (selected.length === 0) return;
    onAdd({
      block_type: "ioc_table",
      observation_ids: selected,
      title: `Tableau IOC (${selected.length})`,
    });
    setSelected([]);
  }

  return (
    <fieldset>
      <legend>Tableau IOC depuis les observations canoniques</legend>
      <p>
        Les lignes sont construites de façon déterministe depuis les
        observations citées ; le titre <code>Tableau IOC (n)</code> est dérivé
        et aucune prose n’est saisie. Une observation consultative est refusée (
        <code>note_ioc_ref_unresolved</code>).
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
          {candidates.map((observation) => (
            <label key={observation.id}>
              <input
                type="checkbox"
                checked={selected.includes(observation.id)}
                onChange={() => toggle(observation.id)}
              />{" "}
              {observationLabel(observation)}
            </label>
          ))}
          {observations.length > MAX_IOC_OBSERVATIONS ? (
            <p>
              Liste bornée aux {MAX_IOC_OBSERVATIONS} premières observations
              canoniques.
            </p>
          ) : null}
          <button
            type="button"
            className="button button--secondary"
            disabled={selected.length === 0 || duplicate}
            onClick={addTable}
          >
            {`Ajouter le tableau IOC (${selected.length})`}
          </button>
          {duplicate ? (
            <p role="note">
              Un tableau IOC identique est déjà référencé par cette proposition.
            </p>
          ) : null}
        </>
      )}
    </fieldset>
  );
}

function CanonicalCodeForm({
  observations,
  advisoryCount,
  blocks,
  onAdd,
}: {
  observations: InvestigationObservation[];
  advisoryCount: number;
  blocks: NoteContentBlockV1[];
  onAdd: (block: CanonicalCodeNoteBlockV1) => void;
}) {
  const [observationId, setObservationId] = useState("");
  const [offset, setOffset] = useState("");
  const [length, setLength] = useState("");
  const [language, setLanguage] = useState("python");
  const [excerptSha, setExcerptSha] = useState("");

  const observation =
    observations.find((item) => item.id === observationId) ?? null;
  const offsetValue = parseInteger(offset);
  const lengthValue = parseInteger(length);
  const languageValid = CODE_LANGUAGE_PATTERN.test(language.trim());
  const shaValid = SHA256_PATTERN.test(excerptSha.trim());
  const blockedReason =
    observation === null
      ? "Choisissez une observation canonique."
      : offsetValue === null
        ? "L’offset doit être un entier d’octets positif ou nul."
        : lengthValue === null || lengthValue < 1
          ? "La longueur doit être un entier d’octets strictement positif."
          : !languageValid
            ? "Le langage doit être un jeton court en minuscules (ex. python, c, bash)."
            : !shaValid
              ? "Le SHA-256 de l’extrait doit être 64 caractères hexadécimaux minuscules."
              : null;
  const duplicate = blocks.some(
    (block) =>
      block.block_type === "canonical_code" &&
      block.observation_id === observationId &&
      block.offset === offsetValue &&
      block.length === lengthValue,
  );

  function addCode() {
    if (
      observation === null ||
      offsetValue === null ||
      lengthValue === null ||
      lengthValue < 1 ||
      !languageValid ||
      !shaValid
    ) {
      return;
    }
    onAdd({
      block_type: "canonical_code",
      observation_id: observation.id,
      blob_sha256: observation.sha256,
      offset: offsetValue,
      length: lengthValue,
      excerpt_sha256: excerptSha.trim(),
      language: language.trim(),
    });
    setOffset("");
    setLength("");
    setExcerptSha("");
  }

  return (
    <fieldset>
      <legend>Extrait de code canonique</legend>
      <p>
        Le serveur extrait les octets exacts du blob de l’observation ; aucun
        texte de code n’est saisi ni accepté comme preuve. Une référence qui ne
        correspond pas octet pour octet est refusée (
        <code>note_code_ref_stale</code>). L’offset, la longueur et le SHA-256
        de l’extrait se lisent dans l’outillage d’analyse de l’observation.
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
            onChange={(event) => setObservationId(event.currentTarget.value)}
          >
            <option value="">— Choisir une observation —</option>
            {observations.slice(0, MAX_IOC_OBSERVATIONS).map((item) => (
              <option key={item.id} value={item.id}>
                {observationLabel(item)}
              </option>
            ))}
          </select>
          <label htmlFor="note-content-code-offset">Offset (octets)</label>
          <input
            id="note-content-code-offset"
            inputMode="numeric"
            value={offset}
            onChange={(event) => setOffset(event.currentTarget.value)}
          />
          <label htmlFor="note-content-code-length">Longueur (octets)</label>
          <input
            id="note-content-code-length"
            inputMode="numeric"
            value={length}
            onChange={(event) => setLength(event.currentTarget.value)}
          />
          <label htmlFor="note-content-code-language">Langage</label>
          <input
            id="note-content-code-language"
            value={language}
            onChange={(event) => setLanguage(event.currentTarget.value)}
          />
          <label htmlFor="note-content-code-sha">SHA-256 de l’extrait</label>
          <input
            id="note-content-code-sha"
            value={excerptSha}
            onChange={(event) => setExcerptSha(event.currentTarget.value)}
          />
          {blockedReason === null ? (
            <p role="note">
              Blob référencé : SHA-256 {shortHash(observation?.sha256 ?? null)}{" "}
              · les octets seront revérifiés par le serveur.
            </p>
          ) : (
            <p role="note">{blockedReason}</p>
          )}
          <button
            type="button"
            className="button button--secondary"
            disabled={blockedReason !== null || duplicate}
            onClick={addCode}
          >
            Référencer cet extrait de code
          </button>
          {duplicate ? (
            <p role="note">
              Cette plage d’octets est déjà référencée par cette proposition.
            </p>
          ) : null}
        </>
      )}
    </fieldset>
  );
}

function ProtocolFigurePicker({
  figures,
  blocks,
  onAdd,
}: {
  figures: OpinionProtocolFigureV1[];
  blocks: NoteContentBlockV1[];
  onAdd: (block: ProtocolFigureNoteBlockV1) => void;
}) {
  const [choice, setChoice] = useState("");
  const usable = figures.filter(
    (
      figure,
    ): figure is OpinionProtocolFigureV1 & {
      kind: ProtocolFigureKind;
    } => isProtocolFigureKind(figure.kind),
  );
  const selected = usable.find((figure) => figure.kind === choice) ?? null;
  const duplicate = blocks.some(
    (block) => block.block_type === "protocol_figure" && block.kind === choice,
  );

  function addFigure() {
    if (selected === null) return;
    onAdd({
      block_type: "protocol_figure",
      kind: selected.kind,
      asset_sha256: selected.asset_sha256,
    });
    setChoice("");
  }

  return (
    <fieldset>
      <legend>Figure de protocole portée par l’avis</legend>
      <p>
        Une figure D2 déjà portée par l’avis est référencée par son type et son
        empreinte ; elle reste rendue en annexe. Une figure absente de l’avis
        est refusée (<code>note_block_ref_stale</code>).
      </p>
      {usable.length === 0 ? (
        <p>L’avis courant ne porte aucune figure de protocole.</p>
      ) : (
        <>
          <label htmlFor="note-content-block-figure">Figure à référencer</label>
          <select
            id="note-content-block-figure"
            value={choice}
            onChange={(event) => setChoice(event.currentTarget.value)}
          >
            <option value="">— Choisir une figure de protocole —</option>
            {usable.map((figure) => (
              <option key={figure.kind} value={figure.kind}>
                {PROTOCOL_FIGURE_LABELS[figure.kind] ?? figure.kind} · SHA-256{" "}
                {shortHash(figure.asset_sha256)} ·{" "}
                {truncate(figure.caption, OPTION_TEXT_MAX)}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="button button--secondary"
            disabled={selected === null || duplicate}
            onClick={addFigure}
          >
            Référencer cette figure
          </button>
          {duplicate ? (
            <p role="note">
              Cette figure est déjà référencée par cette proposition.
            </p>
          ) : null}
        </>
      )}
    </fieldset>
  );
}

function SelectedBlockSummary({ block }: { block: NoteContentBlockV1 }) {
  switch (block.block_type) {
    case "article_element":
      return (
        <p>
          <strong>Élément d’article</strong> ·{" "}
          {ARTICLE_ELEMENT_KIND_LABELS[block.kind]} · clé{" "}
          <code>{block.element_key}</code> · ancre <code>{block.anchor}</code> ·
          SHA-256 <code>{shortHash(block.element_sha256)}</code>
        </p>
      );
    case "canonical_code":
      return (
        <p>
          <strong>Extrait de code canonique</strong> · observation{" "}
          <code>{block.observation_id}</code> · octets {block.offset}–
          {block.offset + block.length} · langage <code>{block.language}</code>{" "}
          · blob SHA-256 <code>{shortHash(block.blob_sha256)}</code> · extrait
          SHA-256 <code>{shortHash(block.excerpt_sha256)}</code>
        </p>
      );
    case "ioc_table":
      return (
        <p>
          <strong>{block.title}</strong> · {block.observation_ids.length}{" "}
          observation(s) canonique(s) ·{" "}
          {block.observation_ids.map((id) => (
            <code key={id}>{shortHash(id)} </code>
          ))}
        </p>
      );
    case "protocol_figure":
      return (
        <p>
          <strong>Figure de protocole</strong> ·{" "}
          {PROTOCOL_FIGURE_LABELS[block.kind] ?? block.kind} · SHA-256{" "}
          <code>{shortHash(block.asset_sha256)}</code>
        </p>
      );
  }
}

function ResolvedBlockPreview({
  block,
}: {
  block: ResolvedNoteContentBlockV1;
}) {
  switch (block.block_type) {
    case "article_element":
      return (
        <div>
          <p>
            <strong>
              {ARTICLE_ELEMENT_KIND_LABELS[block.kind]} «{" "}
              {block.title ?? block.element_key} »
            </strong>{" "}
            — cross-référence vers l’ancre <code>{block.anchor}</code> de
            l’article ; l’élément reste rendu à son placement et n’est jamais
            dupliqué dans la Note.
          </p>
          {block.caption === null ? null : <p>{block.caption}</p>}
          <p>
            Provenance : élément <code>{block.element_key}</code> · SHA-256{" "}
            <code>{block.element_sha256}</code>
          </p>
        </div>
      );
    case "canonical_code":
      return (
        <div>
          <p>
            <strong>Extrait de code canonique</strong> · langage{" "}
            <code>{block.language}</code> · octets {block.offset}–
            {block.offset + block.length}
          </p>
          <pre>
            <code>{block.excerpt}</code>
          </pre>
          <p>
            Provenance : observation <code>{block.observation_id}</code> (
            {block.observation_source} · {block.observation_object_type}/
            {block.observation_object_id}) · blob SHA-256{" "}
            <code>{block.blob_sha256}</code> · extrait SHA-256{" "}
            <code>{block.excerpt_sha256}</code>
          </p>
        </div>
      );
    case "ioc_table":
      return (
        <div>
          <table>
            <caption>{block.title}</caption>
            <thead>
              <tr>
                <th scope="col">Observation</th>
                <th scope="col">Objet</th>
                <th scope="col">Source</th>
                <th scope="col">Observée le</th>
                <th scope="col">Blob SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row) => (
                <tr key={row.observation_id}>
                  <td>
                    <code>{row.observation_id}</code>
                  </td>
                  <td>
                    {row.object_type}/{row.object_id}
                  </td>
                  <td>{row.source}</td>
                  <td>{row.observed_at}</td>
                  <td>
                    <code title={row.blob_sha256}>
                      {shortHash(row.blob_sha256)}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "protocol_figure":
      return (
        <div>
          <p>
            <strong>Figure de protocole</strong> ·{" "}
            {PROTOCOL_FIGURE_LABELS[block.kind] ?? block.kind} · {block.caption}
          </p>
          <p>
            Provenance : asset SHA-256 <code>{block.asset_sha256}</code>
          </p>
        </div>
      );
  }
}
