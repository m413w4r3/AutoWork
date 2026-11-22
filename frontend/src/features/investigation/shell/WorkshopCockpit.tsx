/**
 * Shell du cockpit M24 — cinq vues, desktop uniquement.
 *
 * La vue active est portée par le paramètre d’URL `vue` et écrite avec le
 * `history.pushState` déjà utilisé par l’application ; aucun routeur n’est
 * introduit. Le tablist suit le motif ARIA tabs : flèches, Home/End, roving
 * `tabIndex` et focus visible.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { OpinionNotesView } from "../opinion";
import { OverviewView } from "../overview/OverviewView";
import { PlanTasksView } from "../plan";
import { SamplesResultsView } from "../samples";
import { WorkspaceNotesView } from "../workspace";
import "./workshop.css";

const VIEW_QUERY_PARAM = "vue";
const PANEL_ID = "workshop-view-panel";

const WORKSHOP_VIEWS = [
  { id: "overview", label: "Vue d’ensemble" },
  { id: "plan", label: "Plan et tâches" },
  { id: "samples", label: "Échantillons et résultats" },
  { id: "workspace", label: "Workspace et notes" },
  { id: "opinion", label: "Note de l’analyste" },
] as const;

type WorkshopViewId = (typeof WORKSHOP_VIEWS)[number]["id"];

function isWorkshopViewId(value: string | null): value is WorkshopViewId {
  return WORKSHOP_VIEWS.some((view) => view.id === value);
}

function readViewFromLocation(): WorkshopViewId {
  const value = new URL(window.location.href).searchParams.get(
    VIEW_QUERY_PARAM,
  );
  return isWorkshopViewId(value) ? value : "overview";
}

function tabId(view: WorkshopViewId): string {
  return `workshop-tab-${view}`;
}

export function WorkshopCockpit({
  investigationId,
  subjectId,
}: {
  investigationId: string;
  subjectId: string;
}) {
  const [activeView, setActiveView] =
    useState<WorkshopViewId>(readViewFromLocation);
  const tabRefs = useRef(new Map<WorkshopViewId, HTMLButtonElement>());

  useEffect(() => {
    const handlePopState = () => setActiveView(readViewFromLocation());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const selectView = useCallback((view: WorkshopViewId) => {
    setActiveView(view);
    const url = new URL(window.location.href);
    url.searchParams.set(VIEW_QUERY_PARAM, view);
    window.history.pushState({ workshopView: view }, "", url);
  }, []);

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    const keys = ["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"];
    if (
      !keys.includes(event.key) &&
      event.key !== "Home" &&
      event.key !== "End"
    )
      return;
    event.preventDefault();
    const nextIndex =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? WORKSHOP_VIEWS.length - 1
          : event.key === "ArrowRight" || event.key === "ArrowDown"
            ? (index + 1) % WORKSHOP_VIEWS.length
            : (index - 1 + WORKSHOP_VIEWS.length) % WORKSHOP_VIEWS.length;
    const next = WORKSHOP_VIEWS[nextIndex];
    if (!next) return;
    selectView(next.id);
    tabRefs.current.get(next.id)?.focus({ preventScroll: true });
  };

  return (
    <section className="workshop-cockpit" aria-label="Cockpit d’enquête">
      <div
        className="workshop-tablist"
        role="tablist"
        aria-label="Vues du cockpit d’enquête"
      >
        {WORKSHOP_VIEWS.map((view, index) => {
          const selected = activeView === view.id;
          return (
            <button
              key={view.id}
              ref={(element) => {
                if (element) tabRefs.current.set(view.id, element);
                else tabRefs.current.delete(view.id);
              }}
              id={tabId(view.id)}
              type="button"
              role="tab"
              className={
                selected ? "workshop-tab workshop-tab--active" : "workshop-tab"
              }
              aria-selected={selected}
              aria-controls={PANEL_ID}
              tabIndex={selected ? 0 : -1}
              onClick={() => selectView(view.id)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
            >
              {view.label}
            </button>
          );
        })}
      </div>
      <div
        id={PANEL_ID}
        className="workshop-panel"
        role="tabpanel"
        aria-labelledby={tabId(activeView)}
        tabIndex={0}
      >
        {activeView === "overview" ? (
          <OverviewView
            investigationId={investigationId}
            subjectId={subjectId}
          />
        ) : null}
        {activeView === "plan" ? (
          <PlanTasksView investigationId={investigationId} />
        ) : null}
        {activeView === "samples" ? (
          <SamplesResultsView investigationId={investigationId} />
        ) : null}
        {activeView === "workspace" ? (
          <WorkspaceNotesView investigationId={investigationId} />
        ) : null}
        {activeView === "opinion" ? (
          <OpinionNotesView investigationId={investigationId} />
        ) : null}
      </div>
    </section>
  );
}
