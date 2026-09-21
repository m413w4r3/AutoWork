import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import {
  getEditionProduction,
  startProductionBatch,
} from "../../api/production";
import type { Edition } from "../../api/editions";
import { navigate } from "../../routing";
import { DiscoveryPanel } from "../discovery/DiscoveryPanel";
import { EditionDashboard } from "../edition-dashboard/EditionDashboard";
import { ProductionBatchSelector } from "../edition-workflow/ProductionBatchSelector";
import { ProductionConsole } from "../edition-workflow/ProductionConsole";
import {
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
  const [startRequest, setStartRequest] = useState<{
    subjectIds: string[];
    idempotencyKey: string;
  } | null>(null);
  const board = useQuery({
    queryKey: ["production-board", edition.id],
    queryFn: () => getEditionProduction(edition.id),
    refetchInterval: false,
  });
  const subjects = useMemo(() => board.data?.subjects ?? [], [board.data]);
  const eligibleIds = useMemo(
    () =>
      new Set(
        subjects
          .filter((subject) => subject.can_start)
          .map((subject) => subject.subject_id),
      ),
    [subjects],
  );

  useEffect(() => {
    const pruned = pruneToEligible(selectedSubjectIds, eligibleIds);
    if (pruned.size !== selectedSubjectIds.size) {
      setStartRequest(null);
      setSelectedSubjectIds(pruned);
    }
  }, [eligibleIds, selectedSubjectIds]);

  const start = useMutation({
    mutationFn: (request: { subjectIds: string[]; idempotencyKey: string }) =>
      startProductionBatch(
        edition.id,
        request.subjectIds,
        request.idempotencyKey,
      ),
    retry: 2,
    onSuccess: () => {
      setSelectedSubjectIds(new Set());
      setStartRequest(null);
      void queryClient.invalidateQueries({
        queryKey: ["production-board", edition.id],
      });
    },
  });

  const updateSelection = (update: (current: Set<string>) => Set<string>) => {
    setStartRequest(null);
    setSelectedSubjectIds(update);
  };

  const submitBatch = () => {
    const request = startRequest ?? {
      subjectIds: orderedSelection(subjects, selectedSubjectIds),
      idempotencyKey: crypto.randomUUID(),
    };
    setStartRequest(request);
    start.mutate(request);
  };

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
      {board.data ? (
        <>
          <ProductionBatchSelector
            subjects={subjects}
            selected={selectedSubjectIds}
            readOnly={readOnly}
            onToggle={(subjectId, checked) =>
              updateSelection((current) => {
                const next = new Set(current);
                if (checked) next.add(subjectId);
                else next.delete(subjectId);
                return next;
              })
            }
            onSelectAll={() =>
              updateSelection(
                () =>
                  new Set(
                    subjects
                      .filter((subject) => subject.can_start)
                      .map((subject) => subject.subject_id),
                  ),
              )
            }
            onSelectNone={() => updateSelection(() => new Set())}
          />
          {!readOnly && start.error ? (
            <p className="error-message" role="alert">
              {start.error instanceof Error
                ? start.error.message
                : "Le lot de production n’a pas pu être démarré."}
            </p>
          ) : null}
          {!readOnly ? (
            <button
              type="button"
              className="button"
              disabled={selectedSubjectIds.size === 0 || start.isPending}
              onClick={submitBatch}
            >
              {start.isPending ? "Démarrage…" : "Démarrer le lot de production"}
            </button>
          ) : null}
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
