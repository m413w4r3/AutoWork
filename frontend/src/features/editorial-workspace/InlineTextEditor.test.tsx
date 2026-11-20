import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { EditorialWorkspaceLayout } from "./EditorialWorkspaceLayout";
import { useEditorialDrafts } from "./EditorialDrafts";
import type { InlineTextEditorProps } from "./InlineTextEditor";

vi.mock("./EditorialConversation", () => ({
  EditorialConversation: () => null,
}));

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: "subject-local-drafts",
  production_run_id: "run-local-drafts",
  publication_kind: "article",
  synthesis_artifact_id: "artifact-local-drafts",
  synthesis_version: 1,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [
    {
      block_id: "block-occurrence-a",
      kind: "paragraph",
      parent_block_id: null,
      position: 0,
      text: "Synthetic paragraph A",
      evidence_refs: [],
      revision_target: {
        block_id: "block-occurrence-a",
        legacy_target_id: null,
      },
      before_hash: "b".repeat(64),
      allowed_operations: ["rewrite_paragraph"],
      revision_status: "ready",
    },
    {
      block_id: "block-occurrence-b",
      kind: "paragraph",
      parent_block_id: null,
      position: 1,
      text: "Synthetic paragraph B",
      evidence_refs: [],
      revision_target: {
        block_id: "block-occurrence-b",
        legacy_target_id: null,
      },
      before_hash: "c".repeat(64),
      allowed_operations: ["rewrite_paragraph"],
      revision_status: "ready",
    },
  ],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: true,
    can_add_enrichment: false,
    can_preview_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  snapshot_id: "snapshot-local-drafts",
};

function WorkspaceHarness({
  onSave,
  nextSnapshot = snapshot,
}: {
  onSave?: InlineTextEditorProps["onSave"];
  nextSnapshot?: EditorialWorkspaceSnapshot;
}) {
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [activeSnapshot, setActiveSnapshot] = useState(snapshot);
  const localDrafts = useEditorialDrafts(activeSnapshot);
  return (
    <>
      <button type="button" onClick={() => setActiveSnapshot(nextSnapshot)}>
        Charger le nouveau snapshot
      </button>
      <EditorialWorkspaceLayout
        subjectId={activeSnapshot.subject_id}
        snapshot={activeSnapshot}
        localDrafts={localDrafts}
        selectedBlockId={selectedBlockId}
        onSelectBlock={setSelectedBlockId}
        onRefresh={() => undefined}
        onSave={onSave}
      />
    </>
  );
}

function renderWorkspace() {
  return render(<WorkspaceHarness />);
}

async function selectBlock(
  user: ReturnType<typeof userEvent.setup>,
  id: string,
) {
  const block = document.querySelector<HTMLElement>(
    `.editorial-document__block[data-block-id="${id}"]`,
  );
  if (!block) throw new Error(`Missing synthetic block occurrence ${id}`);
  await user.click(block);
}

async function beginEditing(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Modifier" }));
  return screen.getByRole("textbox", { name: "Texte du bloc" });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("local editorial block drafts", () => {
  it("test_switching_blocks_keeps_draft_by_block_id", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Draft for occurrence A");

    await selectBlock(user, "block-occurrence-b");
    const plan = screen.getByRole("navigation", { name: "Plan éditorial" });
    const resumeDraft = within(plan).getByRole("button", {
      name: "Reprendre le brouillon : Synthetic paragraph A",
    });
    expect(resumeDraft).toBeVisible();

    await user.click(resumeDraft);
    expect(screen.getByRole("textbox", { name: "Texte du bloc" })).toHaveValue(
      "Draft for occurrence A",
    );
  });

  it("test_cancel_clears_only_active_draft", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await selectBlock(user, "block-occurrence-a");
    let editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Draft A");
    await selectBlock(user, "block-occurrence-b");
    editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Draft B");

    const plan = screen.getByRole("navigation", { name: "Plan éditorial" });
    await user.click(
      within(plan).getByRole("button", {
        name: "Reprendre le brouillon : Synthetic paragraph A",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Annuler" }));

    expect(
      within(plan).queryByRole("button", {
        name: "Reprendre le brouillon : Synthetic paragraph A",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(plan).getByRole("button", {
        name: "Reprendre le brouillon : Synthetic paragraph B",
      }),
    ).toBeVisible();
  });

  it("test_close_warns_when_unsaved", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWorkspace();

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Unsaved local prose");

    await user.click(screen.getByRole("link", { name: /retour au sujet/i }));
    expect(confirm).toHaveBeenCalledWith(
      "Quitter l’atelier ? Le brouillon local en mémoire sera perdu.",
    );
    expect(
      screen.getByRole("heading", { name: "Document éditorial" }),
    ).toBeVisible();

    const beforeUnload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(beforeUnload);
    expect(beforeUnload.defaultPrevented).toBe(true);
  });

  it("test_browser_drafts_are_memory_only", async () => {
    const user = userEvent.setup();
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const indexedDbOpen = window.indexedDB
      ? vi.spyOn(window.indexedDB, "open")
      : null;
    renderWorkspace();

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Sensitive synthetic draft text");

    expect(storageWrite).not.toHaveBeenCalled();
    if (indexedDbOpen) expect(indexedDbOpen).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(screen.queryByRole("button", { name: "Enregistrer" })).toBeNull();
  });

  it("asks before Escape abandons the active local draft", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWorkspace();

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Draft retained until confirmed");

    fireEvent.keyDown(editor, { key: "Escape" });
    expect(confirm).toHaveBeenCalledWith(
      "Abandonner le brouillon local de ce bloc ?",
    );
    expect(screen.getByRole("textbox", { name: "Texte du bloc" })).toHaveValue(
      "Draft retained until confirmed",
    );

    confirm.mockReturnValue(true);
    fireEvent.keyDown(editor, { key: "Escape" });
    expect(
      screen.queryByRole("textbox", { name: "Texte du bloc" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "Reprendre le brouillon : Synthetic paragraph A",
      }),
    ).not.toBeInTheDocument();
  });

  it("handles Ctrl+S without claiming a server save is available", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Draft is still available to resume");
    fireEvent.keyDown(editor, { key: "s", ctrlKey: true });

    expect(
      document.querySelector(".editorial-inline-editor__announcement"),
    ).toHaveTextContent(/enregistrement serveur sera disponible/i);
    expect(editor).toHaveValue("Draft is still available to resume");
  });

  it("calls the optional save extension when supplied", async () => {
    const onSave = vi.fn<(blockId: string, value: string) => void>();
    const user = userEvent.setup();
    render(<WorkspaceHarness onSave={onSave} />);

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    await user.clear(editor);
    await user.type(editor, "Updated synthetic text");
    await user.click(screen.getByRole("button", { name: "Enregistrer" }));
    expect(onSave).toHaveBeenCalledWith(
      "block-occurrence-a",
      "Updated synthetic text",
    );
  });

  it.each([
    ["subject_id", { subject_id: "subject-local-drafts-next" }],
    ["production_run_id", { production_run_id: "run-local-drafts-next" }],
    ["synthesis_artifact_id", { synthesis_artifact_id: "synthesis-next" }],
    ["synthesis_version", { synthesis_version: 2 }],
    ["enrichment_artifact_id", { enrichment_artifact_id: "enrichment-next" }],
    ["enrichment_version", { enrichment_version: 2 }],
    [
      "publication_artifact_id",
      { publication_artifact_id: "publication-next" },
    ],
    ["publication_version", { publication_version: 2 }],
    [
      "synthesis_canonical_sha256",
      { synthesis_canonical_sha256: "d".repeat(64) },
    ],
  ] as const)(
    "marks a draft stale when snapshot identity changes at %s",
    async (_field, changedIdentity) => {
      const user = userEvent.setup();
      const nextSnapshot = {
        ...snapshot,
        ...changedIdentity,
        blocks: snapshot.blocks.map((block) =>
          block.block_id === "block-occurrence-a"
            ? { ...block, text: "New canonical paragraph A" }
            : block,
        ),
      };
      render(<WorkspaceHarness nextSnapshot={nextSnapshot} />);

      await selectBlock(user, "block-occurrence-a");
      const editor = await beginEditing(user);
      fireEvent.change(editor, {
        target: { value: "Draft from previous base" },
      });
      await selectBlock(user, "block-occurrence-b");
      await user.click(
        screen.getByRole("button", { name: "Charger le nouveau snapshot" }),
      );

      const plan = screen.getByRole("navigation", { name: "Plan éditorial" });
      expect(
        within(plan).getByText(
          "La version du document a changé depuis ce brouillon",
        ),
      ).toBeVisible();
      expect(
        within(plan).getByRole("button", {
          name: /^Reprendre le brouillon/,
        }),
      ).toBeVisible();
    },
  );

  it("keeps stale draft text recoverable, shows it in the editor, and disables save", async () => {
    const user = userEvent.setup();
    const nextSnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      synthesis_version: 2,
      synthesis_canonical_sha256: "d".repeat(64),
      blocks: snapshot.blocks.map((block) =>
        block.block_id === "block-occurrence-a"
          ? { ...block, text: "New canonical paragraph A" }
          : block,
      ),
    };
    render(<WorkspaceHarness nextSnapshot={nextSnapshot} onSave={vi.fn()} />);

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    fireEvent.change(editor, { target: { value: "Draft from previous base" } });
    await selectBlock(user, "block-occurrence-b");
    await user.click(
      screen.getByRole("button", { name: "Charger le nouveau snapshot" }),
    );

    expect(screen.getByText("Version périmée")).toBeVisible();
    const plan = screen.getByRole("navigation", { name: "Plan éditorial" });
    expect(
      within(plan).getByRole("button", {
        name: /^Reprendre le brouillon/,
      }),
    ).toBeVisible();
    await user.click(
      within(plan).getByRole("button", {
        name: /^Reprendre le brouillon/,
      }),
    );
    expect(screen.getByRole("textbox", { name: "Texte du bloc" })).toHaveValue(
      "Draft from previous base",
    );
    const inlineEditor = document.querySelector<HTMLElement>(
      ".editorial-inline-editor",
    );
    if (!inlineEditor) throw new Error("Expected the inline editor to reopen");
    expect(
      within(inlineEditor).getByText(
        "La version du document a changé depuis ce brouillon",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Enregistrer" })).toBeDisabled();
  });

  it("lists a removed block draft as recoverable and cancellable", async () => {
    const user = userEvent.setup();
    const nextSnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      synthesis_version: 2,
      synthesis_canonical_sha256: "d".repeat(64),
      blocks: snapshot.blocks.filter(
        (block) => block.block_id !== "block-occurrence-a",
      ),
    };
    render(<WorkspaceHarness nextSnapshot={nextSnapshot} />);

    await selectBlock(user, "block-occurrence-a");
    const editor = await beginEditing(user);
    fireEvent.change(editor, {
      target: { value: "Recover this old paragraph" },
    });
    await selectBlock(user, "block-occurrence-b");
    await user.click(
      screen.getByRole("button", { name: "Charger le nouveau snapshot" }),
    );

    const plan = screen.getByRole("navigation", { name: "Plan éditorial" });
    expect(within(plan).getByText("Bloc introuvable")).toBeVisible();
    await user.click(
      within(plan).getByRole("button", { name: "Reprendre le brouillon" }),
    );
    expect(
      within(plan).getByRole("textbox", {
        name: "Texte récupéré du brouillon",
      }),
    ).toHaveValue("Recover this old paragraph");
    await user.click(within(plan).getByRole("button", { name: "Annuler" }));
    expect(
      within(plan).queryByText("Bloc introuvable"),
    ).not.toBeInTheDocument();
  });
});
