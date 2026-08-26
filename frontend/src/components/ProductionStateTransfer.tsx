import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import {
  exportProductionState,
  importProductionState,
} from "../api/production";
import type {
  ProductionStateSnapshotV1,
  ProductionStatus,
} from "../api/production";

export interface ProductionStateTransferProps {
  subjectId: string;
  productionStatus: ProductionStatus | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isSnapshot(value: unknown): value is ProductionStateSnapshotV1 {
  if (!isRecord(value)) return false;
  const origin = value.origin;
  const artifacts = value.artifacts;
  return (
    value.format === "autowork.production-state" &&
    value.schema_version === 1 &&
    typeof value.content_sha256 === "string" &&
    isRecord(origin) &&
    typeof origin.subject_title === "string" &&
    isRecord(artifacts) &&
    "references" in artifacts &&
    "extraction" in artifacts &&
    "synthesis" in artifacts
  );
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function slugify(title: string): string {
  const slug = title
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "subject";
}

function filesystemTimestamp(value: string): string {
  const date = new Date(value);
  if (!Number.isNaN(date.getTime())) {
    return date.toISOString().replace(/[:.]/g, "-");
  }
  return value.replace(/[^a-zA-Z0-9_-]+/g, "-");
}

function isImportAllowed(status: ProductionStatus | null): boolean {
  return (
    status === null ||
    ["ready", "needs_review", "failed", "cancelled"].includes(status.status)
  );
}

function readFileText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") resolve(reader.result);
      else reject(new Error("Unable to read file"));
    };
    reader.onerror = () =>
      reject(reader.error ?? new Error("Unable to read file"));
    reader.readAsText(file);
  });
}

export function ProductionStateTransfer({
  subjectId,
  productionStatus,
}: ProductionStateTransferProps) {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedSnapshot, setSelectedSnapshot] =
    useState<ProductionStateSnapshotV1 | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const exportMutation = useMutation({
    mutationFn: () => exportProductionState(subjectId),
    onSuccess: (snapshot) => {
      const json = JSON.stringify(snapshot, null, 2);
      const blob = new Blob([json], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `autowork-${slugify(snapshot.origin.subject_title)}-production-state-v1-${filesystemTimestamp(snapshot.exported_at)}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    },
  });

  const importMutation = useMutation({
    mutationFn: (snapshot: ProductionStateSnapshotV1) =>
      importProductionState(subjectId, snapshot),
    onSuccess: () => {
      setSuccess(
        "État importé. Références, extraction et synthèse ont été restaurées.",
      );
      setSelectedSnapshot(null);
      if (inputRef.current) inputRef.current.value = "";
      void queryClient.invalidateQueries({
        queryKey: ["production", subjectId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["production-artifact", subjectId],
      });
    },
  });

  const exportDisabled =
    productionStatus === null ||
    productionStatus.status === "queued" ||
    productionStatus.status === "running" ||
    exportMutation.isPending;
  const importDisabled =
    !isImportAllowed(productionStatus) || importMutation.isPending;

  function clearSelection() {
    setSelectedSnapshot(null);
    setParseError(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  async function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    setParseError(null);
    setSuccess(null);
    setSelectedSnapshot(null);
    if (!file) return;
    try {
      const text = await readFileText(file);
      const value: unknown = JSON.parse(text);
      if (!isSnapshot(value)) throw new Error("invalid");
      setSelectedSnapshot(value);
    } catch {
      setParseError(
        "Ce fichier n’est pas un état de production AutoWork valide.",
      );
    }
  }

  return (
    <section className="production-state-transfer">
      <h3>État de production</h3>
      <p>Exportez ou restaurez les artefacts de production de cette brève.</p>
      <div className="production-state-transfer__actions">
        <button
          className="button button--secondary"
          disabled={exportDisabled}
          onClick={() => exportMutation.mutate()}
        >
          {exportMutation.isPending ? "Export…" : "Exporter l’état"}
        </button>
        <label className="button button--secondary">
          Importer un état
          <input
            ref={inputRef}
            type="file"
            accept=".json,application/json"
            aria-label="Importer un état"
            disabled={importDisabled}
            onChange={(event) => void handleFileChange(event)}
            hidden
          />
        </label>
      </div>
      {productionStatus?.status === "queued" ||
      productionStatus?.status === "running" ? (
        <p> L’import est indisponible pendant une production en cours.</p>
      ) : null}
      {exportMutation.error ? (
        <p className="error-message" role="alert">
          L’état ne peut pas être exporté : {errorText(exportMutation.error)}
        </p>
      ) : null}
      {parseError ? (
        <p className="error-message" role="alert">
          {parseError}
        </p>
      ) : null}
      {importMutation.error ? (
        <p className="error-message" role="alert">
          L’état ne peut pas être importé : {errorText(importMutation.error)}
        </p>
      ) : null}
      {success ? <p role="status">{success}</p> : null}
      {selectedSnapshot ? (
        <div className="production-state-transfer__preview">
          <h4>État prêt à importer</h4>
          <p>Sujet d’origine : {selectedSnapshot.origin.subject_title}</p>
          <p>
            Exporté le :{" "}
            {new Date(selectedSnapshot.exported_at).toLocaleString("fr-FR")}
          </p>
          <p>Format : AutoWork production-state v1</p>
          <p>Contenu :</p>
          <ul className="production-state-transfer__stage-list">
            <li>✓ Références</li>
            <li>✓ Extraction</li>
            <li>✓ Synthèse</li>
          </ul>
          <p>
            L’import crée un nouveau run local. Il ne relance ni ChatGPT, ni la
            collecte des sources, ni VirusTotal.
          </p>
          {productionStatus?.title &&
          productionStatus.title !== selectedSnapshot.origin.subject_title ? (
            <p>
              Le fichier provient d’un autre sujet. Son contenu sera importé
              dans le sujet actuellement ouvert.
            </p>
          ) : null}
          <div className="production-state-transfer__actions">
            <button
              className="button"
              disabled={importMutation.isPending}
              onClick={() => importMutation.mutate(selectedSnapshot)}
            >
              {importMutation.isPending ? "Import…" : "Importer"}
            </button>
            <button
              className="button button--secondary"
              onClick={clearSelection}
            >
              Annuler
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
