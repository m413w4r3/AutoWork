import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkshopCockpit } from "./WorkshopCockpit";

const INVESTIGATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const PATH = "/subjects/subject-1";

beforeEach(() => {
  window.history.replaceState({}, "", `${PATH}?vue=plan`);
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        Response.json(
          { detail: { code: "workspace_error", message: "Indisponible" } },
          { status: 503 },
        ),
      ),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

function renderCockpit() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkshopCockpit
        investigationId={INVESTIGATION_ID}
        subjectId={SUBJECT_ID}
      />
    </QueryClientProvider>,
  );
}

describe("WorkshopCockpit", () => {
  it("expose les cinq vues et suit la vue portée par l’URL", async () => {
    renderCockpit();

    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Vue d’ensemble",
      "Plan et tâches",
      "Échantillons et résultats",
      "Workspace et notes",
      "Note de l’analyste",
    ]);
    expect(screen.getByRole("tab", { name: "Plan et tâches" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      await screen.findByRole("heading", { name: "Plan et tâches" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tabpanel")).toBeInTheDocument();
    expect(screen.getByText("Vue en cours d’implémentation.")).toBeTruthy();
  });

  it("navigue dans le tablist aux flèches, Home et End", async () => {
    const user = userEvent.setup();
    renderCockpit();

    screen.getByRole("tab", { name: "Plan et tâches" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(
      screen.getByRole("tab", { name: "Échantillons et résultats" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("tab", { name: "Échantillons et résultats" }),
    ).toHaveFocus();
    expect(window.location.search).toBe("?vue=samples");

    await user.keyboard("{End}");
    expect(
      screen.getByRole("tab", { name: "Note de l’analyste" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(window.location.search).toBe("?vue=opinion");

    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Vue d’ensemble" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(window.location.search).toBe("?vue=overview");

    await user.keyboard("{ArrowLeft}");
    expect(
      screen.getByRole("tab", { name: "Note de l’analyste" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(window.location.search).toBe("?vue=opinion");
  });

  it("écrit la vue au clic, préserve les autres paramètres et suit popstate", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", `${PATH}?edition=E1&vue=plan`);
    renderCockpit();

    await user.click(screen.getByRole("tab", { name: "Workspace et notes" }));
    expect(window.location.search).toBe("?edition=E1&vue=workspace");

    act(() => {
      window.history.pushState({}, "", `${PATH}?edition=E1&vue=samples`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    expect(
      await screen.findByRole("heading", { name: "Échantillons et résultats" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Échantillons et résultats" }),
    ).toHaveAttribute("aria-selected", "true");
  });
});
