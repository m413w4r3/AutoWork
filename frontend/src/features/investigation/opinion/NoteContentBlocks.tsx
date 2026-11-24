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
  IocTableNoteBlockV1,
  NoteArticleElementKind,
  NoteContentBlockV1,
  ProtocolFigureKind,
  ProtocolFigureNoteBlockV1,
  ResolvedNoteContentBlockV1,
} from "../api/opinionProposals";
import { CanonicalCodeSelector } from "./CanonicalCodeSelector";
import { CopyableTechnicalValue } from "./CopyableTechnicalValue";
import { GroupedReferencePicker } from "./ReferencePicker";
import {
  observationReferenceEntries,
  type ReferencePickerEntry,
} from "./references";
import { describeApiError, describeErrorCode } from "./errors";
import {
  ARTICLE_ELEMENT_REFUSAL_LABELS,
  PROTOCOL_FIGURE_LABELS,
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
/** Short option labels keep the page width independent of id lengths. */
const OPTION_SHORT_MAX = 32;

function shortObjectId(value: string): string {
  return value.length > 12 ? value.slice(0, 10) + "…" : value;
}

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

/** The server's typed refusal, translated; unknown codes stay readable. */
function elementRefusalLabel(reason: string | null): string {
  if (reason === null) return "élément non proposable";
  return ARTICLE_ELEMENT_REFUSAL_LABELS[reason] ?? describeErrorCode(reason);
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
          <CanonicalCodeSelector
            investigationId={investigationId}
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
              : `Base de publication épinglée · version ${basePublication.version ?? "—"}`}
          </p>
          {basePublication?.artifactId === null ||
          basePublication?.artifactId === undefined ||
          basePublication.sha256 === null ? null : (
            <TechnicalProvenance
              values={[
                {
                  label: "Artefact de publication",
                  value: basePublication.artifactId,
                },
                {
                  label: "SHA-256 de la publication",
                  value: basePublication.sha256,
                },
              ]}
            />
          )}
          {resolvedBlocks.length === 0 ? (
            invalid ? null : (
              <p>Aucun bloc résolu pour cette proposition.</p>
            )
          ) : (
            <div className="note-content-blocks__resolved">
              {resolvedBlocks.map((block, index) => (
                <article
                  className="note-content-blocks__resolved-item"
                  key={`${block.block_type}-${index}`}
                >
                  <ResolvedBlockPreview block={block} />
                </article>
              ))}
            </div>
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
  const elementEntries = useMemo<ReferencePickerEntry[]>(
    () =>
      catalog === null
        ? []
        : elements.map((element) => {
            const title = element.title ?? element.key;
            const kindLabel = ARTICLE_ELEMENT_KIND_LABELS[element.kind];
            const disabledReason = element.proposable
              ? undefined
              : elementRefusalLabel(element.refusal_reason);
            return {
              id: `${element.kind}:${element.key}`,
              section: kindLabel,
              groupKey: JSON.stringify([element.kind, element.key]),
              label: `${kindLabel} · ${truncate(title, OPTION_SHORT_MAX)}`,
              searchText: [
                kindLabel,
                title,
                element.caption ?? "",
                element.key,
                element.element_sha256,
                element.anchor ?? "",
                element.refusal_reason ?? "",
              ].join(" "),
              values: [
                { label: "Clé de l’élément", value: element.key },
                {
                  label: "SHA-256 de l’élément",
                  value: element.element_sha256,
                },
                ...(element.anchor === null
                  ? []
                  : [{ label: "Ancre", value: element.anchor }]),
                {
                  label: "ID de la publication de base",
                  value: catalog.base_publication_artifact_id,
                },
                {
                  label: "SHA-256 de la publication de base",
                  value: catalog.base_publication_sha256,
                },
              ],
              ...(disabledReason === undefined ? {} : { disabledReason }),
            };
          }),
    [catalog, elements],
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
            · version {catalog.base_publication_version}
          </p>
          <TechnicalProvenance
            values={[
              {
                label: "ID de la publication de base",
                value: catalog.base_publication_artifact_id,
              },
              {
                label: "SHA-256 de la publication de base",
                value: catalog.base_publication_sha256,
              },
            ]}
          />
          {elements.length === 0 ? (
            <p>
              Aucun élément M23 n’est rendu à cet emplacement. Un élément placé
              sur une autre ancre ne peut pas être référencé ici (
              <code>note_block_ref_stale</code>).
            </p>
          ) : (
            <>
              <GroupedReferencePicker
                entries={elementEntries}
                legend="Élément à référencer"
                itemName="element"
                selection={{
                  mode: "single",
                  selectedId: choice,
                  onSelect: setChoice,
                }}
              />
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
  const candidates = useMemo(
    () => observations.slice(0, MAX_IOC_OBSERVATIONS),
    [observations],
  );
  const entries = useMemo(
    () => observationReferenceEntries(candidates),
    [candidates],
  );
  const duplicate = blocks.some(
    (block) =>
      block.block_type === "ioc_table" &&
      sameObservationSet(block.observation_ids, selected),
  );

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
          <GroupedReferencePicker
            entries={entries}
            legend="Observations à inclure"
            itemName="observation"
            selection={{
              mode: "multiple",
              selectedIds: selected,
              onChange: setSelected,
            }}
          />
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
  const usable = useMemo(
    () =>
      figures.filter(
        (
          figure,
        ): figure is OpinionProtocolFigureV1 & {
          kind: ProtocolFigureKind;
        } => isProtocolFigureKind(figure.kind),
      ),
    [figures],
  );
  const entries = useMemo<ReferencePickerEntry[]>(
    () =>
      usable.map((figure) => {
        const label = PROTOCOL_FIGURE_LABELS[figure.kind] ?? figure.kind;
        return {
          id: figure.kind,
          section: "Figures de protocole",
          groupKey: figure.kind,
          label: `${label} · ${truncate(figure.caption, OPTION_SHORT_MAX)}`,
          searchText: [
            label,
            figure.caption,
            figure.asset_id,
            figure.asset_sha256,
            figure.model_sha256,
            figure.spec_sha256,
            figure.preview_sha256,
            figure.pin_sha256,
          ].join(" "),
          values: [
            { label: "ID de la figure", value: figure.asset_id },
            { label: "SHA-256 de la figure", value: figure.asset_sha256 },
            { label: "SHA-256 du modèle", value: figure.model_sha256 },
            { label: "SHA-256 de la spécification", value: figure.spec_sha256 },
            { label: "SHA-256 de l’aperçu", value: figure.preview_sha256 },
            { label: "SHA-256 du lien", value: figure.pin_sha256 },
          ],
        };
      }),
    [usable],
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
          <GroupedReferencePicker
            entries={entries}
            legend="Figure à référencer"
            itemName="figure"
            selection={{
              mode: "single",
              selectedId: choice,
              onSelect: setChoice,
            }}
          />
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

function TechnicalProvenance({
  values,
}: {
  values: Array<{ label: string; value: string }>;
}) {
  return (
    <details className="opinion-technical-details">
      <summary>Provenance technique</summary>
      <dl>
        {values.map((item) => (
          <div key={item.label}>
            <dt>{item.label}</dt>
            <dd>
              <CopyableTechnicalValue label={item.label} value={item.value} />
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function SelectedBlockSummary({ block }: { block: NoteContentBlockV1 }) {
  switch (block.block_type) {
    case "article_element":
      return (
        <div>
          <p>
            <strong>Élément d’article</strong> ·{" "}
            {ARTICLE_ELEMENT_KIND_LABELS[block.kind]}
          </p>
          <TechnicalProvenance
            values={[
              { label: "Clé de l’élément", value: block.element_key },
              { label: "Ancre", value: block.anchor },
              { label: "SHA-256 de l’élément", value: block.element_sha256 },
            ]}
          />
        </div>
      );
    case "canonical_code":
      return (
        <div>
          <p>
            <strong>Extrait de code canonique</strong> · octets {block.offset}–
            {block.offset + block.length} · langage{" "}
            <code>{block.language}</code>
          </p>
          <TechnicalProvenance
            values={[
              { label: "ID de l’observation", value: block.observation_id },
              { label: "SHA-256 du blob", value: block.blob_sha256 },
              { label: "SHA-256 de l’extrait", value: block.excerpt_sha256 },
            ]}
          />
        </div>
      );
    case "ioc_table":
      return (
        <div>
          <p>
            <strong>{block.title}</strong> · {block.observation_ids.length}{" "}
            observation(s) canonique(s)
          </p>
          <TechnicalProvenance
            values={block.observation_ids.map((id, index) => ({
              label: "ID de l’observation " + (index + 1),
              value: id,
            }))}
          />
        </div>
      );
    case "protocol_figure":
      return (
        <div>
          <p>
            <strong>Figure de protocole</strong> ·{" "}
            {PROTOCOL_FIGURE_LABELS[block.kind] ?? block.kind}
          </p>
          <TechnicalProvenance
            values={[
              { label: "SHA-256 de la figure", value: block.asset_sha256 },
            ]}
          />
        </div>
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
              {block.title ?? ARTICLE_ELEMENT_KIND_LABELS[block.kind]} »
            </strong>{" "}
            — cross-référence vers le paragraphe correspondant de l’article ;
            l’élément reste rendu à son placement et n’est jamais dupliqué dans
            la Note.
          </p>
          {block.caption === null ? null : <p>{block.caption}</p>}
          <TechnicalProvenance
            values={[
              { label: "Clé de l’élément", value: block.element_key },
              { label: "Ancre", value: block.anchor },
              { label: "SHA-256 de l’élément", value: block.element_sha256 },
            ]}
          />
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
          <TechnicalProvenance
            values={[
              { label: "ID de l’observation", value: block.observation_id },
              { label: "Source", value: block.observation_source },
              {
                label: "Type d’objet",
                value: block.observation_object_type,
              },
              {
                label: "Identifiant exact de l’objet",
                value: block.observation_object_id,
              },
              { label: "SHA-256 du blob", value: block.blob_sha256 },
              { label: "SHA-256 de l’extrait", value: block.excerpt_sha256 },
            ]}
          />
        </div>
      );
    case "ioc_table":
      return (
        <div>
          <div className="table-scroll">
            <table>
              <caption>{block.title}</caption>
              <thead>
                <tr>
                  <th scope="col">Référence</th>
                  <th scope="col">Objet</th>
                  <th scope="col">Source</th>
                  <th scope="col">Observée le</th>
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, index) => (
                  <tr key={row.observation_id}>
                    <td>Observation {index + 1}</td>
                    <td>
                      {row.object_type} · {shortObjectId(row.object_id)}
                    </td>
                    <td>{row.source}</td>
                    <td>{row.observed_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <TechnicalProvenance
            values={block.rows.flatMap((row, index) => [
              {
                label: "ID de l’observation " + (index + 1),
                value: row.observation_id,
              },
              {
                label: "Identifiant exact de l’objet " + (index + 1),
                value: row.object_id,
              },
              {
                label: "SHA-256 du blob " + (index + 1),
                value: row.blob_sha256,
              },
            ])}
          />
        </div>
      );
    case "protocol_figure":
      return (
        <div>
          <p>
            <strong>Figure de protocole</strong> ·{" "}
            {PROTOCOL_FIGURE_LABELS[block.kind] ?? block.kind} · {block.caption}
          </p>
          <TechnicalProvenance
            values={[
              { label: "SHA-256 de la figure", value: block.asset_sha256 },
            ]}
          />
        </div>
      );
  }
}
