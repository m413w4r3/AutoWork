import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EditorialServerDraftStatus } from "./EditorialServerDraftStatus";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function serverDraft(isStale = false, expiresAt = "2026-12-20T12:00:00Z") {
  return {
    id: "draft-1",
    subject_id: "subject-1",
    production_run_id: "run-1",
    occurrence_id: "occurrence-1",
    base_snapshot_id: "snapshot-1",
    base_artifact_id: "artifact-1",
    base_artifact_version: 4,
    base_canonical_sha256: "a".repeat(64),
    text_sha256: "b".repeat(64),
    version: 1,
    text: "private editorial text",
    is_stale: isStale,
    created_at: "2026-11-20T12:00:00Z",
    updated_at: "2026-11-20T12:00:00Z",
    expires_at: expiresAt,
  };
}

function renderStatus({
  isStale = false,
  localDraftText = "",
  fetchImplementation,
}: {
  isStale?: boolean;
  localDraftText?: string;
  fetchImplementation?: typeof fetch;
} = {}) {
  const fetchMock =
    fetchImplementation ??
    vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(JSON.stringify(serverDraft(isStale)), {
          status: 200,
          headers: { "Content-Type": "application/json", ETag: '"1"' },
        }),
      ),
    );
  const onRecover = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const { container } = render(
    <QueryClientProvider client={client}>
      <EditorialServerDraftStatus
        subjectId="subject-1"
        occurrenceId="occurrence-1"
        snapshotId="snapshot-1"
        localDraftText={localDraftText}
        onRecover={onRecover}
      />
    </QueryClientProvider>,
  );
  return { client, container, fetchMock, onRecover };
}

describe("EditorialServerDraftStatus", () => {
  it("does not render an empty status container when no server or local draft exists", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: { code: "draft_not_found" } }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const { client, container } = renderStatus({
      fetchImplementation: fetchMock,
    });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(
        client.getQueryState(
          editorialWorkspaceQueryKeys.serverDraft("subject-1", "occurrence-1"),
        )?.status,
      ).toBe("success");
      expect(
        container.querySelector(".editorial-server-draft-status"),
      ).toBeNull();
    });
  });

  it("identifies a server draft and its expiry without rendering its text", async () => {
    const { fetchMock } = renderStatus();

    expect(
      await screen.findByText(
        /Brouillon enregistré sur le serveur · expire le/,
      ),
    ).toBeVisible();
    expect(screen.queryByText("private editorial text")).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/subjects/subject-1/editor/blocks/occurrence-1/draft",
      { cache: "no-store" },
    );
  });

  it("marks a draft whose base pins changed", async () => {
    renderStatus({ isStale: true });

    expect(
      await screen.findByText(/base périmée, rechargez l’atelier/),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveAttribute("data-stale", "true");
  });

  it("requires confirmation before replacing a non-empty local draft", async () => {
    const { onRecover } = renderStatus({ localDraftText: "local text" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Reprendre depuis le serveur",
      }),
    );
    expect(confirm).toHaveBeenCalledWith(
      "Remplacer le brouillon local non vide par le brouillon serveur ?",
    );
    expect(onRecover).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    fireEvent.click(
      screen.getByRole("button", { name: "Reprendre depuis le serveur" }),
    );
    expect(onRecover).toHaveBeenCalledWith("private editorial text");
  });

  it("signals an expired server draft and its recorded expiry date", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            detail: {
              code: "draft_expired",
              message: "Ce brouillon a expiré.",
              expires_at: "2026-11-19T12:00:00Z",
            },
          }),
          { status: 410, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    renderStatus({ fetchImplementation: fetchMock });

    expect(
      await screen.findByText(/Le brouillon serveur a expiré le/),
    ).toBeVisible();
    expect(screen.getByText(/Son texte n’est plus disponible/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Reprendre depuis le serveur" }),
    ).toBeNull();
  });

  it("deletes a server draft after explicit confirmation", async () => {
    const fetchMock = vi.fn<typeof fetch>((_input, init) =>
      init?.method === "DELETE"
        ? Promise.resolve(new Response(null, { status: 204 }))
        : Promise.resolve(
            new Response(JSON.stringify(serverDraft()), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          ),
    );
    renderStatus({ fetchImplementation: fetchMock });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Supprimer le brouillon serveur",
      }),
    );
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/subjects/subject-1/editor/blocks/occurrence-1/draft",
        {
          method: "DELETE",
          headers: { "If-Match": '"1"' },
          cache: "no-store",
        },
      );
    });
  });

  it("saves the in-memory local draft with a bounded version and no-store request", async () => {
    const fetchMock = vi.fn<typeof fetch>((_input, init) =>
      init?.method === "PUT"
        ? Promise.resolve(
            new Response(JSON.stringify(serverDraft()), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          )
        : Promise.resolve(
            new Response(
              JSON.stringify({ detail: { code: "draft_not_found" } }),
              {
                status: 404,
                headers: { "Content-Type": "application/json" },
              },
            ),
          ),
    );
    renderStatus({
      localDraftText: "local text",
      fetchImplementation: fetchMock,
    });

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Enregistrer le brouillon serveur",
      }),
    );
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/subjects/subject-1/editor/blocks/occurrence-1/draft",
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "If-Match": '"0"',
          },
          body: JSON.stringify({
            snapshot_id: "snapshot-1",
            text: "local text",
          }),
          cache: "no-store",
        },
      );
    });
  });
});
