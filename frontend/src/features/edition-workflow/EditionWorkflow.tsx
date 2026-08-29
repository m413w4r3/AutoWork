import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { fetchEditorialBoard, type EditorialGroup } from "../../api/editorial";
import { startEditionProduction } from "../../api/production";
import {
  transitionEdition,
  type Edition,
  type EditionStatus,
} from "../../api/editions";
import { EditorialBoard } from "../../components/EditorialBoard";
import { DiscoveryPanel } from "../discovery/DiscoveryPanel";
import { discoveryJobStorageKey } from "../discovery/discoveryStorage";
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

function stepForStatus(status: EditionStatus): string {
  if (status === "draft" || status === "discovery") return "discovery";
  if (status === "selection") return "selection";
  if (status === "production") return "production";
  if (status === "review") return "review";
  return "publication";
}

function WorkflowStepper({ status }: { status: EditionStatus }) {
  const currentStep = stepForStatus(status);
  return (
    <ol className="workflow-steps" aria-label="Workflow de l’édition">
      {WORKFLOW_STEPS.map(([step, label]) => (
        <li key={step} aria-current={step === currentStep ? "step" : undefined}>
          {label}
        </li>
      ))}
    </ol>
  );
}

function StatusAction({
  edition,
  target,
  disabled = false,
  children,
}: {
  edition: Edition;
  target: EditionStatus;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  const queryClient = useQueryClient();
  const transition = useMutation({
    mutationFn: () => transitionEdition(edition, target),
    onSuccess: (updated) => {
      queryClient.setQueryData(["edition", edition.id], updated);
      void queryClient.invalidateQueries({ queryKey: ["editions"] });
    },
  });

  if (!edition.allowed_transitions.includes(target)) return null;
  return (
    <div className="workflow-action">
      {transition.error ? (
        <p className="error-message" role="alert">
          {transition.error.message}
        </p>
      ) : null}
      <button
        className="button"
        disabled={transition.isPending || disabled}
        onClick={() => transition.mutate()}
      >
        {transition.isPending ? "Mise à jour…" : children}
      </button>
    </div>
  );
}

function DiscoveryPhase({ edition }: { edition: Edition }) {
  const [discoveryRunning, setDiscoveryRunning] = useState(() =>
    Boolean(window.localStorage.getItem(discoveryJobStorageKey(edition.id))),
  );

  if (edition.status === "draft") {
    return (
      <>
        <section
          className="workflow-placeholder"
          aria-labelledby="discovery-intro-heading"
        >
          <p className="eyebrow">Découverte</p>
          <h2 id="discovery-intro-heading">Préparer la découverte</h2>
          <p>
            Lancez la phase de découverte pour rechercher et examiner les sujets
            candidats de cette édition.
          </p>
        </section>
        <StatusAction edition={edition} target="discovery">
          Démarrer la découverte
        </StatusAction>
      </>
    );
  }

  return (
    <>
      <DiscoveryPanel
        editionId={edition.id}
        onRunningChange={setDiscoveryRunning}
      />
      <StatusAction
        edition={edition}
        target="selection"
        disabled={discoveryRunning}
      >
        Ouvrir la sélection
      </StatusAction>
      {discoveryRunning ? (
        <p className="workflow-note" role="status">
          La recherche en cours doit se terminer avant la sélection.
        </p>
      ) : null}
    </>
  );
}

function isEligibleSubject(group: EditorialGroup): boolean {
  return (
    group.status === "selected" &&
    group.editorial_type === "brief" &&
    group.subject_id !== null
  );
}

function SelectionPhase({ edition }: { edition: Edition }) {
  const queryClient = useQueryClient();
  const board = useQuery({
    queryKey: ["editorial-board", edition.id],
    queryFn: () => fetchEditorialBoard(edition.id),
  });
  const start = useMutation({
    mutationFn: () => startEditionProduction(edition.id),
    onSuccess: (batch) => {
      queryClient.setQueryData(["batch", edition.id], batch);
      void queryClient.invalidateQueries({ queryKey: ["batch", edition.id] });
      void queryClient.invalidateQueries({ queryKey: ["edition", edition.id] });
    },
  });
  const eligibleCount =
    board.data?.groups.filter(isEligibleSubject).length ?? 0;

  return (
    <>
      <EditorialBoard editionId={edition.id} />
      <section
        className="production-start-panel"
        aria-labelledby="production-start-heading"
      >
        <p className="eyebrow">Production</p>
        <h2 id="production-start-heading">
          {eligibleCount} article{eligibleCount > 1 ? "s" : ""} éligible
          {eligibleCount > 1 ? "s" : ""}
        </h2>
        <p>
          La sélection éditoriale détermine les sujets qui seront envoyés au lot
          de production.
        </p>
        {start.error ? (
          <p className="error-message" role="alert">
            Le lancement de la production a échoué : {String(start.error)}
          </p>
        ) : null}
        <button
          className="button"
          disabled={start.isPending || eligibleCount === 0}
          onClick={() => start.mutate()}
        >
          {start.isPending
            ? "Lancement…"
            : `Lancer la production de ${eligibleCount} article${eligibleCount > 1 ? "s" : ""}`}
        </button>
      </section>
    </>
  );
}

export function EditionWorkflow({ edition }: { edition: Edition }) {
  return (
    <section className="edition-workflow" aria-label="Workflow de l’édition">
      <WorkflowStepper status={edition.status} />
      {edition.status === "draft" || edition.status === "discovery" ? (
        <DiscoveryPhase edition={edition} />
      ) : null}
      {edition.status === "selection" ? (
        <SelectionPhase edition={edition} />
      ) : null}
      {edition.status === "production" ? (
        <ProductionConsole editionId={edition.id} />
      ) : null}
      {edition.status === "review" ? (
        <ReviewConsole editionId={edition.id} />
      ) : null}
      {edition.status === "assembling" ||
      edition.status === "published" ||
      edition.status === "archived" ? (
        <PublicationConsole
          editionId={edition.id}
          editionStatus={edition.status}
        />
      ) : null}
    </section>
  );
}
