import type { MouseEvent } from "react";

export const INVESTIGATION_VIEWS = [
  "overview",
  "plan",
  "samples",
  "workspace",
  "opinion",
] as const;

export type InvestigationView = (typeof INVESTIGATION_VIEWS)[number];

export interface InvestigationNavigation {
  vue: InvestigationView;
  task?: string;
}

function isInvestigationView(value: string | null): value is InvestigationView {
  return INVESTIGATION_VIEWS.some((view) => view === value);
}

/** Build a real same-page URL while preserving the current route context. */
export function investigationHref({ vue, task }: InvestigationNavigation) {
  const url = new URL(window.location.href);
  url.searchParams.set("vue", vue);
  if (task !== undefined) url.searchParams.set("task", task);
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Push an investigation view and notify the existing URL subscribers. */
export function navigateInvestigation(navigation: InvestigationNavigation) {
  const currentState: unknown = window.history.state;
  const state =
    typeof currentState === "object" &&
    currentState !== null &&
    !Array.isArray(currentState)
      ? currentState
      : {};
  window.history.pushState(
    {
      ...state,
      workshopView: navigation.vue,
      ...(navigation.task === undefined
        ? {}
        : { investigationTask: navigation.task }),
    },
    "",
    investigationHref(navigation),
  );
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** Keep modified clicks and links to other pages native for browser behavior. */
export function handleInvestigationLinkClick(
  event: MouseEvent<HTMLAnchorElement>,
  href: string,
) {
  const anchor = event.currentTarget;
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey ||
    (anchor.target !== "" && anchor.target !== "_self") ||
    anchor.hasAttribute("download")
  )
    return;

  const target = new URL(href, window.location.href);
  if (
    target.origin !== window.location.origin ||
    target.pathname !== window.location.pathname
  )
    return;

  const vue = target.searchParams.get("vue");
  if (!isInvestigationView(vue)) return;

  const task = target.searchParams.get("task");
  event.preventDefault();
  navigateInvestigation({
    vue,
    ...(task === null ? {} : { task }),
  });
}
