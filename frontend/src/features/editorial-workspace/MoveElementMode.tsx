import { useMemo, useState } from "react";

import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import {
  paragraphExcerpt,
  visualElementAnchorId,
  visualElementKindLabel,
  visualElementTitle,
} from "./visualElementOperations";

interface MoveModeBannerProps {
  element: EditorialWorkspaceBlock;
  currentAnchorText: string | null;
  draftAnchor: EditorialWorkspaceBlock | null;
  error: string | null;
  saving: boolean;
  onCancel: () => void;
}

export function MoveModeBanner({
  element,
  currentAnchorText,
  draftAnchor,
  error,
  saving,
  onCancel,
}: MoveModeBannerProps) {
  return (
    <section
      className="visual-move-banner"
      role="region"
      aria-label="Déplacement du visuel"
      tabIndex={-1}
    >
      <h3>Choisir un nouvel emplacement</h3>
      <p className="visual-move-banner__target">
        Déplacement de {visualElementKindLabel(element).toLowerCase()} ·{" "}
        {visualElementTitle(element)}
      </p>
      <p className="visual-move-banner__current">
        Emplacement actuel :{" "}
        {currentAnchorText
          ? `après « ${currentAnchorText} »`
          : "aucun paragraphe d’ancrage identifié"}
      </p>
      {draftAnchor ? (
        <p className="visual-move-banner__future" aria-live="polite">
          Nouvel emplacement : après « {paragraphExcerpt(draftAnchor)} ».
          Confirmez depuis l’aperçu ci-dessous.
        </p>
      ) : (
        <p className="visual-move-banner__future">
          Cliquez un paragraphe, ou placez-y le focus et appuyez sur Entrée.
          Échap annule.
        </p>
      )}
      {!draftAnchor && error ? (
        <p className="visual-move-banner__error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="visual-move-banner__actions">
        <button
          className="button button--secondary"
          type="button"
          disabled={saving}
          onClick={onCancel}
        >
          Annuler
        </button>
      </div>
      <p
        className="visual-move-banner__status"
        role="status"
        aria-live="polite"
      >
        {saving ? "Enregistrement de l’emplacement en cours." : ""}
      </p>
    </section>
  );
}

interface MoveProvisionalPreviewProps {
  element: EditorialWorkspaceBlock;
  anchor: EditorialWorkspaceBlock;
  saving: boolean;
  samePosition: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function MoveProvisionalPreview({
  element,
  anchor,
  saving,
  samePosition,
  error,
  onConfirm,
  onCancel,
}: MoveProvisionalPreviewProps) {
  return (
    <section
      className="visual-move-preview"
      aria-label="Aperçu provisoire de l’emplacement"
      data-move-element-key={element.element_key ?? undefined}
    >
      <p
        className="visual-move-preview__label"
        role="status"
        aria-live="polite"
      >
        Insertion prévue : après « {paragraphExcerpt(anchor)} »
      </p>
      <p className="visual-move-preview__element">
        {visualElementKindLabel(element)} · {visualElementTitle(element)}
      </p>
      <p className="visual-move-preview__note">
        Aperçu non canonique : rien n’est enregistré tant que vous ne confirmez
        pas.
      </p>
      {samePosition ? (
        <p className="visual-move-preview__same" role="status">
          Le visuel est déjà placé après ce paragraphe. Choisissez un autre
          paragraphe.
        </p>
      ) : null}
      {error ? (
        <p className="visual-move-preview__error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="visual-move-preview__actions">
        <button
          className="button"
          type="button"
          disabled={samePosition || saving}
          onClick={onConfirm}
        >
          {saving ? "Enregistrement…" : "Enregistrer l’emplacement"}
        </button>
        <button
          className="button button--secondary"
          type="button"
          disabled={saving}
          onClick={onCancel}
        >
          Annuler
        </button>
      </div>
      <p
        className="visual-move-preview__status"
        role="status"
        aria-live="polite"
      >
        {saving ? "Enregistrement de l’emplacement en cours." : ""}
      </p>
    </section>
  );
}

interface MoveElementPanelProps {
  snapshot: EditorialWorkspaceSnapshot;
  element: EditorialWorkspaceBlock;
  draftAnchorId: string | null;
  saving: boolean;
  error: string | null;
  onChooseAnchor: (anchorBlockId: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Inspector alternative for choosing a move anchor by search and section.
 * Exposed for the `Visuels` inspector slot owned by the gallery task; it
 * shares the same draft anchor state as the in-document move mode.
 */
export function MoveElementPanel({
  snapshot,
  element,
  draftAnchorId,
  saving,
  error,
  onChooseAnchor,
  onConfirm,
  onCancel,
}: MoveElementPanelProps) {
  const [query, setQuery] = useState("");
  const [sectionId, setSectionId] = useState<string>("all");
  const currentAnchorId = visualElementAnchorId(element);
  const paragraphs = snapshot.blocks.filter(
    (block) =>
      (block.kind === "paragraph" || block.kind === "lead_paragraph") &&
      block.revision_status === "ready",
  );
  const sections = snapshot.blocks.filter(
    (block) => block.kind === "section_heading",
  );
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return paragraphs.filter((block) => {
      if (sectionId !== "all" && block.parent_block_id !== sectionId) {
        return false;
      }
      if (!needle) return true;
      return (block.text ?? "").toLowerCase().includes(needle);
    });
  }, [paragraphs, query, sectionId]);
  const anchorByBlockId = new Map(
    snapshot.blocks.map((block) => [block.block_id, block] as const),
  );
  const currentAnchor = currentAnchorId
    ? anchorByBlockId.get(currentAnchorId)
    : undefined;
  const draftAnchor = draftAnchorId
    ? anchorByBlockId.get(draftAnchorId)
    : undefined;
  const samePosition =
    draftAnchorId !== null && draftAnchorId === currentAnchorId;

  return (
    <section className="visual-move-panel" aria-label="Déplacer le visuel">
      <h3>Déplacer {visualElementTitle(element)}</h3>
      <p>
        Emplacement actuel :{" "}
        {currentAnchor
          ? `après « ${paragraphExcerpt(currentAnchor)} »`
          : "ancrage non identifié"}
      </p>
      <label className="visual-move-panel__search">
        Rechercher un paragraphe
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
        />
      </label>
      <label className="visual-move-panel__section">
        Section
        <select
          value={sectionId}
          onChange={(event) => setSectionId(event.currentTarget.value)}
        >
          <option value="all">Toutes les sections</option>
          {sections.map((section) => (
            <option key={section.block_id} value={section.block_id}>
              {section.text ?? "Section"}
            </option>
          ))}
        </select>
      </label>
      <fieldset className="visual-move-panel__anchors">
        <legend>Paragraphe d’ancrage</legend>
        {filtered.length === 0 ? (
          <p>Aucun paragraphe ne correspond à cette recherche.</p>
        ) : (
          filtered.map((paragraph) => {
            const isCurrent = paragraph.block_id === currentAnchorId;
            return (
              <label
                key={paragraph.block_id}
                className="visual-move-panel__anchor"
              >
                <input
                  type="radio"
                  name="visual-move-anchor"
                  value={paragraph.block_id}
                  checked={draftAnchorId === paragraph.block_id}
                  disabled={isCurrent}
                  onChange={() => onChooseAnchor(paragraph.block_id)}
                />
                <span>{paragraphExcerpt(paragraph, 90)}</span>
                {isCurrent ? (
                  <span className="visual-move-panel__current">
                    Emplacement actuel
                  </span>
                ) : null}
              </label>
            );
          })
        )}
      </fieldset>
      <p className="visual-move-panel__future" aria-live="polite">
        Emplacement prévu :{" "}
        {draftAnchor
          ? `après « ${paragraphExcerpt(draftAnchor)} »`
          : "choisissez un paragraphe"}
      </p>
      {samePosition ? (
        <p className="visual-move-panel__same" role="status">
          Le visuel est déjà placé après ce paragraphe.
        </p>
      ) : null}
      {error ? (
        <p className="visual-move-panel__error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="visual-move-panel__actions">
        <button
          className="button"
          type="button"
          disabled={!draftAnchor || samePosition || saving}
          onClick={onConfirm}
        >
          {saving ? "Enregistrement…" : "Enregistrer l’emplacement"}
        </button>
        <button
          className="button button--secondary"
          type="button"
          disabled={saving}
          onClick={onCancel}
        >
          Annuler
        </button>
      </div>
    </section>
  );
}
