import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { fetchEditorialBoard } from "../../api/editorial";
import { startEditionProduction } from "../../api/production";
import type { Edition } from "../../api/editions";
import { EditorialBoard } from "../../components/EditorialBoard";
import { DiscoveryPanel } from "../discovery/DiscoveryPanel";
import { discoveryJobStorageKey } from "../discovery/discoveryStorage";
import { navigate } from "../../routing";
import { ProductionBatchSelector } from "./ProductionBatchSelector";
import {
  isEligibleSubject,
  orderedSelection,
  pruneToEligible,
} from "./productionBatchSelection";
import { ProductionConsole } from "./ProductionConsole";
import { PublicationConsole } from "./PublicationConsole";
import { ReviewConsole } from "./ReviewConsole";

const WORKFLOW_STEPS = [
  ["discovery", "Découverte"],
  ["selection", "Sélection"],
  ["production", "Production"],
  ["review", "Revue"],
  ["publication", "Publication"],
] as const;

type WorkflowPhase = (typeof WORKFLOW_STEPS)[number][0];

function isWorkflowPhase(value: string | null): value is WorkflowPhase {
  return WORKFLOW_STEPS.some(([phase]) => phase === value);
}

function phaseFromLocation(): WorkflowPhase {
  const requested = new URLSearchParams(window.location.search).get("phase");
  return isWorkflowPhase(requested) ? requested : "discovery";
}

function phaseUrl(phase: WorkflowPhase): string {
  const params = new URLSearchParams(window.location.search);
  params.set("phase", phase);
  const query = params.toString();
  return `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
}

function useViewedPhase(): {
  phase: WorkflowPhase;
  selectPhase: (phase: WorkflowPhase) => void;
} {
  const [, setLocationVersion] = useState(0);

  useEffect(() => {
    const update = () => setLocationVersion((version) => version + 1);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  return {
    phase: phaseFromLocation(),
    selectPhase: (phase) => navigate(phaseUrl(phase)),
  };
}

function WorkflowStepper({
  viewedPhase,
  onSelect,
}: {
  viewedPhase: WorkflowPhase;
  onSelect: (phase: WorkflowPhase) => void;
}) {
  return (
    <ol className="workflow-steps" aria-label="Workflow de l’édition">
      {WORKFLOW_STEPS.map(([phase, label]) => {
        const current = phase === viewedPhase;
        return (
          <li
            key={phase}
            className={
              [current ? "is-viewed" : null].filter(Boolean).join(" ") ||
              undefined
            }
            data-phase={phase}
          >
            <a
              href={phaseUrl(phase)}
              aria-current={current ? "step" : undefined}
              onClick={(event) => {
                event.preventDefault();
                onSelect(phase);
              }}
            >
              {label}
            </a>
          </li>
        );
      })}
    </ol>
  );
}

function DiscoveryPhase({
  edition,
  readOnly = false,
}: {
  edition: Edition;
  readOnly?: boolean;
}) {
  const [discoveryRunning, setDiscoveryRunning] = useState(() =>
    Boolean(window.localStorage.getItem(discoveryJobStorageKey(edition.id))),
  );

  return (
    <>
      <DiscoveryPanel
        editionId={edition.id}
        onRunningChange={setDiscoveryRunning}
        readOnly={readOnly}
      />
      {!readOnly && discoveryRunning ? (
        <p className="workflow-note" role="status">
          La recherche en cours doit se terminer avant la sélection.
        </p>
      ) : null}
    </>
  );
}

function SelectionPhase({
  edition,
  readOnly = false,
}: {
  edition: Edition;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const board = useQuery({
    queryKey: ["editorial-board", edition.id],
    queryFn: () => fetchEditorialBoard(edition.id),
    enabled: !readOnly,
  });

  // The production-batch selection: which of the editorially eligible
  // subjects the operator has explicitly checked for the *next* batch.
  // Starts empty on every mount/reload — opening or refreshing this page
  // must never silently pre-arm every eligible subject.
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

  // If TanStack Query hands back a fresher board, drop any selected id that
  // is no longer eligible (or gone) — never add ids back in.
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
              {eligibleCount} article{eligibleCount > 1 ? "s" : ""} éligible
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
                  ? `Lancer la production de ${selectedCount} article${selectedCount > 1 ? "s" : ""}`
                  : "Sélectionnez au moins un article"}
            </button>
          </section>
        </>
      ) : null}
    </>
  );
}

export function EditionWorkflow({ edition }: { edition: Edition }) {
  const { phase, selectPhase } = useViewedPhase();
  const readOnly = edition.state === "archived";

  return (
    <section className="edition-workflow" aria-label="Workflow de l’édition">
      <WorkflowStepper viewedPhase={phase} onSelect={selectPhase} />
      {phase === "discovery" ? (
        <DiscoveryPhase edition={edition} readOnly={readOnly} />
      ) : null}
      {phase === "selection" ? (
        <SelectionPhase edition={edition} readOnly={readOnly} />
      ) : null}
      {phase === "production" ? (
        <ProductionConsole editionId={edition.id} readOnly={readOnly} />
      ) : null}
      {phase === "review" ? (
        <ReviewConsole editionId={edition.id} readOnly={readOnly} />
      ) : null}
      {phase === "publication" ? (
        <PublicationConsole editionId={edition.id} readOnly={readOnly} />
      ) : null}
    </section>
  );
}
