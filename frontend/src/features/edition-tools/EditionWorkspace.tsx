import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { startEditionProduction } from "../../api/production";
import { fetchSelectionBoard } from "../../api/selection";
import type { Edition } from "../../api/editions";
import { navigate } from "../../routing";
import { DiscoveryPanel } from "../discovery/DiscoveryPanel";
import { EditionDashboard } from "../edition-dashboard/EditionDashboard";
import { ProductionBatchSelector } from "../edition-workflow/ProductionBatchSelector";
import { ProductionConsole } from "../edition-workflow/ProductionConsole";
import {
  isEligibleSubject,
  orderedSelection,
  pruneToEligible,
} from "../edition-workflow/productionBatchSelection";
import { PublicationConsole } from "../edition-workflow/PublicationConsole";
import { ReviewConsole } from "../edition-workflow/ReviewConsole";
import { FusionBoard } from "../fusion/FusionBoard";
import { SelectionBoard } from "../selection/SelectionBoard";

export type EditionTool =
  | "discovery"
  | "fusion"
  | "selection"
  | "production"
  | "review"
  | "publication";

export type EditionSurface = "overview" | EditionTool;

const EDITION_NAVIGATION: ReadonlyArray<{
  surface: EditionSurface;
  label: string;
}> = [
  { surface: "overview", label: "Vue d’ensemble" },
  { surface: "discovery", label: "Découverte" },
  { surface: "fusion", label: "Fusion" },
  { surface: "selection", label: "Sélection" },
  { surface: "production", label: "Productions" },
  { surface: "review", label: "Revue" },
  { surface: "publication", label: "Publication" },
];

function editionSurfacePath(
  editionId: string,
  surface: EditionSurface,
): string {
  return surface === "overview"
    ? `/editions/${editionId}`
    : `/editions/${editionId}/${surface}`;
}

export function EditionNavigation({
  editionId,
  current,
}: {
  editionId: string;
  current: EditionSurface;
}) {
  return (
    <nav aria-label="Outils de l’édition">
      <ul className="edition-navigation">
        {EDITION_NAVIGATION.map(({ surface, label }) => {
          const path = editionSurfacePath(editionId, surface);
          return (
            <li key={surface}>
              <a
                href={path}
                aria-current={surface === current ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(path);
                }}
              >
                {label}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function SelectionTool({
  edition,
  readOnly,
}: {
  edition: Edition;
  readOnly: boolean;
}) {
  return <SelectionBoard editionId={edition.id} readOnly={readOnly} />;
}

function ProductionTool({
  edition,
  readOnly,
}: {
  edition: Edition;
  readOnly: boolean;
}) {
  const queryClient = useQueryClient();
  const [selectedSubjectIds, setSelectedSubjectIds] = useState<Set<string>>(
    () => new Set(),
  );
  const board = useQuery({
    queryKey: ["selection-board", edition.id],
    queryFn: () => fetchSelectionBoard(edition.id),
    refetchInterval: false,
  });
  const subjects = useMemo(
    () => board.data?.items.filter(isEligibleSubject) ?? [],
    [board.data],
  );
  const eligibleIds = useMemo(
    () => new Set(subjects.map((subject) => subject.subject_id)),
    [subjects],
  );

  useEffect(() => {
    setSelectedSubjectIds((selected) => pruneToEligible(selected, eligibleIds));
  }, [eligibleIds]);

  const start = useMutation({
    mutationFn: () =>
      startEditionProduction(
        edition.id,
        orderedSelection(subjects, selectedSubjectIds),
      ),
    onSuccess: (batch) => {
      setSelectedSubjectIds(new Set());
      queryClient.setQueryData(["batch", edition.id], batch);
    },
  });

  return (
    <>
      {!readOnly && board.isPending ? (
        <p role="status">Chargement des sujets sélectionnés…</p>
      ) : null}
      {!readOnly && board.isError ? (
        <p className="error-message" role="alert">
          La sélection est inaccessible.
        </p>
      ) : null}
      {!readOnly && board.data ? (
        <>
          <ProductionBatchSelector
            subjects={subjects}
            selected={selectedSubjectIds}
            onToggle={(subjectId, checked) =>
              setSelectedSubjectIds((current) => {
                const next = new Set(current);
                if (checked) next.add(subjectId);
                else next.delete(subjectId);
                return next;
              })
            }
            onSelectAll={() =>
              setSelectedSubjectIds(
                new Set(subjects.map((subject) => subject.subject_id)),
              )
            }
            onSelectNone={() => setSelectedSubjectIds(new Set())}
          />
          {start.error ? (
            <p className="error-message" role="alert">
              {start.error instanceof Error
                ? start.error.message
                : "Le lot de production n’a pas pu être démarré."}
            </p>
          ) : null}
          <button
            type="button"
            className="button"
            disabled={selectedSubjectIds.size === 0 || start.isPending}
            onClick={() => start.mutate()}
          >
            {start.isPending ? "Démarrage…" : "Démarrer le lot de production"}
          </button>
        </>
      ) : null}
      <ProductionConsole editionId={edition.id} readOnly={readOnly} />
    </>
  );
}

export function EditionToolSurface({
  edition,
  tool,
}: {
  edition: Edition;
  tool: EditionTool;
}) {
  const readOnly = edition.state === "archived";

  switch (tool) {
    case "discovery":
      return <DiscoveryPanel editionId={edition.id} readOnly={readOnly} />;
    case "fusion":
      return <FusionBoard editionId={edition.id} readOnly={readOnly} />;
    case "selection":
      return <SelectionTool edition={edition} readOnly={readOnly} />;
    case "production":
      return <ProductionTool edition={edition} readOnly={readOnly} />;
    case "review":
      return <ReviewConsole editionId={edition.id} readOnly={readOnly} />;
    case "publication":
      return <PublicationConsole editionId={edition.id} readOnly={readOnly} />;
  }
}

export function EditionWorkspace({
  edition,
  current,
}: {
  edition: Edition;
  current: EditionSurface;
}) {
  return (
    <section
      className="edition-workspace"
      aria-label="Espace de travail de l’édition"
    >
      <EditionNavigation editionId={edition.id} current={current} />
      {current === "overview" ? (
        <EditionDashboard edition={edition} />
      ) : (
        <EditionToolSurface edition={edition} tool={current} />
      )}
    </section>
  );
}
