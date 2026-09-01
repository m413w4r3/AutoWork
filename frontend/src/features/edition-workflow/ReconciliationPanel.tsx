import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import {
  adoptProductionReconciliationManual,
  adoptProductionReconciliationVisible,
  previewProductionReconciliationManual,
  previewProductionReconciliationVisible,
  type ProductionReconciliation,
  type ProductionRecoveryPreview,
} from "../../api/production";
import { STAGE_LABELS } from "./productionLabels";

/**
 * The single recovery flow for an ambiguous ChatGPT submission.
 *
 * The exact answer may already exist on the provider side, so this is never a
 * retry: the operator reads the exact response, checks its SHA-256, and adopts
 * it — or pastes the Markdown when the visible target is gone.  Production and
 * Review show the very same flow, driven by the identity the backend persisted
 * next to the run.
 */
export function ReconciliationPanel({
  runId,
  reconciliation,
  onRecovered,
}: {
  runId: string;
  reconciliation: ProductionReconciliation;
  onRecovered: () => void;
}) {
  const [preview, setPreview] = useState<ProductionRecoveryPreview | null>(
    null,
  );
  const [manual, setManual] = useState("");
  const [showManual, setShowManual] = useState(false);
  const visiblePreview = useMutation({
    mutationFn: () => previewProductionReconciliationVisible(runId),
    retry: false,
    onSuccess: (result) => setPreview(result),
    onError: () => setShowManual(true),
  });
  const visibleAdopt = useMutation({
    mutationFn: (sha256: string) =>
      adoptProductionReconciliationVisible(runId, sha256),
    retry: false,
    onSuccess: onRecovered,
  });
  const manualPreview = useMutation({
    mutationFn: () => previewProductionReconciliationManual(runId, manual),
    retry: false,
    onSuccess: (result) => setPreview(result),
  });
  const manualAdopt = useMutation({
    mutationFn: (sha256: string) =>
      adoptProductionReconciliationManual(runId, manual, sha256),
    retry: false,
    onSuccess: onRecovered,
  });

  const error =
    visiblePreview.error ||
    visibleAdopt.error ||
    manualPreview.error ||
    manualAdopt.error;
  const isManualPreview = preview?.metadata.source === "manual_import";
  return (
    <div
      className="production-reconciliation"
      aria-label="Récupération ChatGPT"
    >
      <h3>Récupérer la réponse ChatGPT</h3>
      <dl>
        <div>
          <dt>ModelRun</dt>
          <dd>{reconciliation.model_run_id}</dd>
        </div>
        <div>
          <dt>Étape</dt>
          <dd>
            {STAGE_LABELS[reconciliation.stage]} · génération{" "}
            {reconciliation.pipeline_generation}
          </dd>
        </div>
        <div>
          <dt>Soumission</dt>
          <dd>{reconciliation.submission_state}</dd>
        </div>
        {reconciliation.bridge_response_id ? (
          <div>
            <dt>Réponse bridge</dt>
            <dd>{reconciliation.bridge_response_id}</dd>
          </div>
        ) : null}
      </dl>
      <button
        className="button"
        type="button"
        disabled={visiblePreview.isPending}
        onClick={() => visiblePreview.mutate()}
      >
        {visiblePreview.isPending
          ? "Lecture de la réponse…"
          : "Récupérer la réponse ChatGPT"}
      </button>
      {preview ? (
        <div className="production-reconciliation__preview">
          <p>
            SHA-256 : <code>{preview.sha256}</code> · {preview.chars} caractères
          </p>
          <pre>{preview.text}</pre>
          <button
            className="button button--primary"
            type="button"
            disabled={visibleAdopt.isPending || manualAdopt.isPending}
            onClick={() =>
              isManualPreview
                ? manualAdopt.mutate(preview.sha256)
                : visibleAdopt.mutate(preview.sha256)
            }
          >
            {visibleAdopt.isPending || manualAdopt.isPending
              ? "Adoption…"
              : "Confirmer et reprendre la production"}
          </button>
        </div>
      ) : null}
      <button
        className="button button--secondary"
        type="button"
        onClick={() => setShowManual((current) => !current)}
      >
        {showManual
          ? "Masquer l’import Markdown"
          : "Réponse ChatGPT indisponible ? Coller le Markdown"}
      </button>
      {showManual ? (
        <div className="production-reconciliation__manual">
          <label>
            Réponse Markdown
            <textarea
              value={manual}
              onChange={(event) => setManual(event.target.value)}
              rows={8}
            />
          </label>
          <button
            className="button"
            type="button"
            disabled={!manual.trim() || manualPreview.isPending}
            onClick={() => manualPreview.mutate()}
          >
            {manualPreview.isPending
              ? "Prévisualisation…"
              : "Prévisualiser l’import"}
          </button>
        </div>
      ) : null}
      {error ? (
        <p className="error-message" role="alert">
          {error instanceof Error
            ? error.message
            : "La récupération n’a pas abouti."}
        </p>
      ) : null}
    </div>
  );
}
