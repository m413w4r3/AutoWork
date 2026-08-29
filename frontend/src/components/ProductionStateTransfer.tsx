import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError } from "../api/editions";
import {
  exportProductionState,
  importProductionState,
} from "../api/production";
import type {
  ProductionStateSnapshot,
  ProductionStatus,
} from "../api/production";

export interface ProductionStateTransferProps {
  subjectId: string;
  productionStatus: ProductionStatus | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]) {
  return (
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.prototype.hasOwnProperty.call(value, key))
  );
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isArtifacts(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const references = value.references;
  const extraction = value.extraction;
  const synthesis = value.synthesis;
  return (
    hasExactKeys(value, ["references", "extraction", "synthesis"]) &&
    isRecord(references) &&
    hasExactKeys(references, ["input_hash", "canonical_content"]) &&
    typeof references.input_hash === "string" &&
    isRecord(references.canonical_content) &&
    isRecord(extraction) &&
    hasExactKeys(extraction, ["input_hash", "canonical_content"]) &&
    typeof extraction.input_hash === "string" &&
    isRecord(extraction.canonical_content) &&
    isRecord(synthesis) &&
    hasExactKeys(synthesis, ["input_hash", "rendered_content"]) &&
    typeof synthesis.input_hash === "string" &&
    typeof synthesis.rendered_content === "string"
  );
}

function isProductionStateSnapshot(
  value: unknown,
): value is ProductionStateSnapshot {
  if (!isRecord(value)) return false;
  if (
    !hasExactKeys(value, [
      "format",
      "schema_version",
      "exported_at",
      "origin",
      "artifacts",
      "content_sha256",
    ]) ||
    value.format !== "autowork.production-state" ||
    (value.schema_version !== 1 && value.schema_version !== 2) ||
    typeof value.exported_at !== "string" ||
    typeof value.content_sha256 !== "string" ||
    !isArtifacts(value.artifacts) ||
    !isRecord(value.origin)
  ) {
    return false;
  }

  const origin = value.origin;
  if (value.schema_version === 1) {
    return (
      hasExactKeys(origin, [
        "subject_title",
        "editorial_type",
        "profile",
        "research_date",
      ]) &&
      typeof origin.subject_title === "string" &&
      origin.editorial_type === "brief" &&
      origin.profile === "brief_auto" &&
      isNullableString(origin.research_date)
    );
  }

  return (
    hasExactKeys(origin, ["subject_title", "research_date"]) &&
    typeof origin.subject_title === "string" &&
    isNullableString(origin.research_date)
  );
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const messages: Record<string, string> = {
      production_state_not_found: "Aucun état exportable n’est disponible.",
      production_state_active_run:
        "Cette action est indisponible pendant une production en cours.",
      production_state_incomplete:
        "Les trois artefacts vérifiés ne sont pas disponibles.",
      production_state_unverified:
        "Les artefacts de production ne sont pas tous vérifiés.",
      production_state_invalid_format: "Le format du fichier est invalide.",
      production_state_version_unsupported:
        "La version de cet état n’est pas prise en charge.",
      production_state_invalid: "Le contenu de cet état est invalide.",
      production_state_checksum_mismatch:
        "La vérification d’intégrité du fichier a échoué.",
      production_state_too_large:
        "Le fichier ou un artefact est trop volumineux.",
    };
    return (
      messages[error.code] ??
      "Une erreur est survenue avec l’état de production."
    );
  }
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
    useState<ProductionStateSnapshot | null>(null);
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
      link.download = `autowork-${slugify(snapshot.origin.subject_title)}-production-state-v2-${filesystemTimestamp(snapshot.exported_at)}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    },
  });

  const importMutation = useMutation({
    mutationFn: (snapshot: ProductionStateSnapshot) =>
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
      if (!isProductionStateSnapshot(value)) throw new Error("invalid");
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
      <p>Exportez ou restaurez les artefacts de production de cet article.</p>
      <div className="production-state-transfer__actions">
        <button
          className="button button--secondary"
          disabled={exportDisabled}
          onClick={() => exportMutation.mutate()}
        >
          {exportMutation.isPending ? "Export…" : "Exporter l’état"}
        </button>
        <button
          type="button"
          className="button button--secondary"
          disabled={importDisabled}
          onClick={() => inputRef.current?.click()}
        >
          Importer un état
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".json,application/json"
          aria-label="Importer un état"
          disabled={importDisabled}
          onChange={(event) => void handleFileChange(event)}
          hidden
        />
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
          <p>
            Format : AutoWork production-state v
            {selectedSnapshot.schema_version}
          </p>
          <p>Contenu :</p>
          <ul className="production-state-transfer__stage-list">
            <li>✓ Références</li>
            <li>✓ Extraction</li>
            <li>✓ Synthèse</li>
          </ul>
          <p>
            Ce fichier restaure les résultats coûteux de recherche, extraction
            et synthèse. Aucun appel ChatGPT, collecte de source ou analyse
            VirusTotal ne sera lancé.
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
              disabled={importDisabled || importMutation.isPending}
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
