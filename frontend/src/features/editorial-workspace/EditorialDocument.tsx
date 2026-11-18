import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";

interface EditorialDocumentProps {
  snapshot: EditorialWorkspaceSnapshot;
  selectedBlockId: string | null;
  onSelectBlock: (blockId: string) => void;
}

export function EditorialDocument({
  snapshot,
  selectedBlockId,
  onSelectBlock,
}: EditorialDocumentProps) {
  return (
    <article aria-label="Document éditorial">
      {snapshot.blocks.map((block) => (
        <button
          key={block.block_id}
          type="button"
          aria-pressed={selectedBlockId === block.block_id}
          aria-label={block.text ?? block.kind}
          data-block-id={block.block_id}
          className="editorial-document__block"
          onClick={() => onSelectBlock(block.block_id)}
        >
          {block.text}
        </button>
      ))}
    </article>
  );
}
