import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { fetchEditorialBoard } from "../../api/editorial";
import type { Edition } from "../../api/editions";
import { startEditionProduction } from "../../api/production";
import { EditorialBoard } from "../../components/EditorialBoard";
import { navigate } from "../../routing";
import { DiscoveryPanel } from "../discovery/DiscoveryPanel";
import { EditionDashboard } from "../edition-dashboard/EditionDashboard";
import { ProductionBatchSelector } from "../edition-workflow/ProductionBatchSelector";
import {
  isEligibleSubject,
  orderedSelection,
  pruneToEligible,
} from "../edition-workflow/productionBatchSelection";
import { ProductionConsole } from "../edition-workflow/ProductionConsole";
import { PublicationConsole } from "../edition-workflow/PublicationConsole";
import { ReviewConsole } from "../edition-workflow/ReviewConsole";
import { FusionBoard } from "../fusion/FusionBoard";

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
  const queryClient = useQueryClient();
  const board = useQuery({
    queryKey: ["editorial-board", edition.id],
    queryFn: () => fetchEditorialBoard(edition.id),
    enabled: !readOnly,
  });
  const [selected, setSelected] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const eligibleGroups = useMemo(
    () => board.data?.groups.filter(isEligibleSubject) ?? [],
    [board.data],
  );
  const eligibleIds = useMemo(
    () => new Set(eligibleGroups.map((group) => group.subject_id)),
    [eligibleGroups],
  );

  useEffect(() => {
    setSelected((current) => {
      const next = pruneToEligible(current, eligibleIds);
      return next.size === current.size ? current : next;
    });
  }, [eligibleIds]);

  const selectedSubjectIds = useMemo(
    () => orderedSelection(eligibleGroups, selected),
    [eligibleGroups, selected],
  );

  const start = useMutation({
    mutationFn: () => startEditionProduction(edition.id, selectedSubjectIds),
    onSuccess: (batch) => {
      queryClient.setQueryData(["batch", edition.id], batch);
      void queryClient.invalidateQueries({ queryKey: ["batch", edition.id] });
      void queryClient.invalidateQueries({ queryKey: ["edition", edition.id] });
      navigate(`/editions/${edition.id}/production`);
    },
  });

  const eligibleCount = eligibleGroups.length;
  const selectedCount = selectedSubjectIds.length;

  return (
    <>
      <EditorialBoard editionId={edition.id} readOnly={readOnly} />
      {!readOnly ? (
        <>
          <ProductionBatchSelector
            groups={eligibleGroups}
            selected={selected}
            onToggle={(subjectId, checked) =>
              setSelected((current) => {
                const next = new Set(current);
                if (checked) next.add(subjectId);
                else next.delete(subjectId);
                return next;
              })
            }
            onSelectAll={() => setSelected(new Set(eligibleIds))}
            onSelectNone={() => setSelected(new Set())}
          />
          <section
            className="production-start-panel"
            aria-labelledby="production-start-heading"
          >
            <p className="eyebrow">Production</p>
            <h2 id="production-start-heading">
              {eligibleCount} sujet{eligibleCount > 1 ? "s" : ""} éligible
              {eligibleCount > 1 ? "s" : ""}
            </h2>
            <p className="production-batch-count" aria-live="polite">
              {`${selectedCount} sélectionné${selectedCount > 1 ? "s" : ""} pour ce lot`}
            </p>
            {start.error ? (
              <p className="error-message" role="alert">
                Le lancement de la production a échoué : {String(start.error)}
              </p>
            ) : null}
            <button
              className="button"
              disabled={start.isPending || selectedCount === 0}
              onClick={() => start.mutate()}
            >
              {start.isPending
                ? "Lancement…"
                : selectedCount > 0
                  ? `Lancer la production de ${selectedCount} sujet${selectedCount > 1 ? "s" : ""}`
                  : "Sélectionnez au moins un sujet"}
            </button>
          </section>
        </>
      ) : null}
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
      return <ProductionConsole editionId={edition.id} readOnly={readOnly} />;
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
