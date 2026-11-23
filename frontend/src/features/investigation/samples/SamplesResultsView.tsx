import { useId, useState, type KeyboardEvent } from "react";

import {
  isWorkshopApiError,
  useAgentSessions,
  useBootstrapInventory,
  useBootstrapPlan,
  useBootstrapSnapshot,
  useContributions,
  useRunBootstrap,
  useTasks,
  type BootstrapCoverageEntryV1,
  type BootstrapDownloadOutcomeV1,
  type BootstrapIocV1,
  type BootstrapPlanV1,
  type BootstrapQuarantinedSampleV1,
  type BootstrapSnapshotIocV1,
  type BootstrapSnapshotV1,
  type IOCInventoryV1,
  type SampleOrigin,
  type SampleQualification,
  type SampleState,
  type WorkContributionCategory,
  type WorkContributionView,
  type TaskView,
  type AgentSessionView,
  type WorkspaceFileListV1,
  type WorkspaceFileViewV1,
} from "../api";
import { useWorkspaceFiles } from "../api/workspace";
import {
  useInvestigation,
  type InvestigationFinding,
  type InvestigationObservation,
} from "../../../api/investigation";
import {
  useInvestigationSamples,
  type InvestigationSampleDetail,
} from "../../../api/investigationGovernance";
import {
  CONTRIBUTION_CATEGORY_LABELS,
  CONTRIBUTION_IMPORT_LABELS,
  COVERAGE_KIND_LABELS,
  DOWNLOAD_STATUS_LABELS,
  INVOCATION_STATUS_LABELS,
  IOC_KIND_LABELS,
  IOC_ORIGIN_LABELS,
  PLAN_STATE_LABELS,
  PLAN_STATE_REASONS,
  REASON_LABELS,
  ROUTE_STATUS_LABELS,
  SAMPLE_ORIGIN_LABELS,
  SAMPLE_QUALIFICATION_LABELS,
  SAMPLE_STATE_LABELS,
  VT_TOOL_LABELS,
  formatTimestamp,
  labelOf,
  operationOutcomeLabel,
} from "./samplesLabels";
import { WorkshopErrorNotice } from "../shell/WorkshopErrorNotice";
import { WorkshopLongOperationState } from "../shell/WorkshopStates";
import { SampleQualificationPanel } from "./SampleQualificationPanel";
import { handleInvestigationLinkClick, investigationHref } from "../navigation";
import { EVIDENCE_KIND_LABELS } from "../opinion/labels";
import "./samples.css";

const CHIP_KINDS: Record<WorkContributionCategory, string> = {
  work_contribution: "Note de travail — non probante",
  observation: "Observation — référence canonique",
  finding: "Finding — vérification humaine requise",
};

const AVAILABILITY_LABELS = {
  available: "Disponible",
  limited: "Disponibilité limitée",
  unavailable: "Indisponible",
  unknown: "Non établie",
} as const;

const FINDING_KIND_LABELS: Record<string, string> = {
  new_sample: "Nouvel échantillon",
  infection_chain: "Chaîne d’infection",
  capability: "Capacité",
  network_behaviour: "Comportement réseau",
  claim_verification: "Vérification d’affirmation",
  detection: "Détection",
};

const FINDING_ASSESSMENT_LABELS: Record<string, string> = {
  supported: "Étayé",
  refuted: "Réfuté",
  qualified: "Nuancé",
  inconclusive: "Non concluant",
};

const SESSION_STATE_LABELS: Record<string, string> = {
  created: "Créée",
  active: "Active",
  interrupted: "Interrompue",
  closed: "Terminée",
  failed: "Échec",
};

interface SampleRow {
  key: string;
  sha256: string;
  ioc: BootstrapIocV1;
  origin: SampleOrigin | null;
  qualification: SampleQualification | null;
  state: SampleState | null;
  associationId: string | null;
  sampleId: string | null;
  version: number | null;
  download: BootstrapDownloadOutcomeV1 | null;
}

type Availability = keyof typeof AVAILABILITY_LABELS;

interface ObjectRow {
  key: string;
  rowKind: "ioc" | "sample";
  value: string;
  kind: BootstrapIocV1["kind"];
  hashType: BootstrapIocV1["hash_type"];
  ioc: BootstrapIocV1 | null;
  iocOrigin: BootstrapIocV1["origin"] | null;
  sampleOrigin: SampleOrigin | null;
  associationId: string | null;
  sampleId: string | null;
  qualification: SampleQualification | null;
  state: SampleState | null;
  download: BootstrapDownloadOutcomeV1 | null;
  snapshotIoc: BootstrapSnapshotIocV1 | null;
  availability: Availability;
  sourceKey: string;
  sourceLabel: string;
}

type SortColumn =
  "value" | "kind" | "origin" | "availability" | "qualification" | "source";

function buildSampleRows(
  inventory: IOCInventoryV1,
  snapshot: BootstrapSnapshotV1 | undefined,
): SampleRow[] {
  const snapshotByIoc = new Map<string, BootstrapSnapshotIocV1>();
  for (const ioc of snapshot?.iocs ?? []) snapshotByIoc.set(ioc.ioc_id, ioc);
  const rows: SampleRow[] = [];
  const seen = new Set<string>();
  for (const ioc of [...inventory.seeds, ...inventory.discovered]) {
    const iocDownload = snapshotByIoc.get(ioc.ioc_id)?.download ?? null;
    const quarantinedByAssociation = new Map<
      string,
      BootstrapQuarantinedSampleV1
    >();
    for (const sample of ioc.quarantined) {
      if (sample.association_id !== null) {
        quarantinedByAssociation.set(sample.association_id, sample);
      }
    }
    for (const association of ioc.associations) {
      const key = `${ioc.ioc_id}:${association.association_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const quarantined = quarantinedByAssociation.get(
        association.association_id,
      );
      const download = downloadForAssociation(
        iocDownload,
        association.association_id,
        association.sample_sha256,
      );
      rows.push({
        key,
        sha256: association.sample_sha256,
        ioc,
        origin: association.origin,
        qualification: association.qualification,
        state: quarantined?.state ?? null,
        associationId: association.association_id,
        sampleId: quarantined?.sample_id ?? null,
        version: association.version,
        download,
      });
    }
    for (const sample of ioc.quarantined) {
      if (
        sample.association_id !== null &&
        ioc.associations.some(
          (association) => association.association_id === sample.association_id,
        )
      ) {
        continue;
      }
      const key = `${ioc.ioc_id}:quarantine:${sample.sample_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const download = downloadForAssociation(
        iocDownload,
        sample.association_id,
        sample.sha256,
      );
      rows.push({
        key,
        sha256: sample.sha256,
        ioc,
        origin: null,
        qualification: sample.qualification,
        state: sample.state,
        associationId: sample.association_id,
        sampleId: sample.sample_id,
        version: null,
        download,
      });
    }
  }
  return rows;
}

function downloadForAssociation(
  download: BootstrapDownloadOutcomeV1 | null,
  associationId: string | null,
  sampleSha256: string,
): BootstrapDownloadOutcomeV1 | null {
  if (download === null) return null;
  if (
    download.association_id !== null &&
    download.association_id === associationId
  ) {
    return download;
  }
  if (download.sample_sha256 !== null) {
    return download.sample_sha256 === sampleSha256 ? download : null;
  }
  return null;
}

function findSnapshotIoc(
  snapshot: BootstrapSnapshotV1 | undefined,
  ioc: BootstrapIocV1 | null,
  value: string,
): BootstrapSnapshotIocV1 | null {
  if (snapshot === undefined) return null;
  return (
    snapshot.iocs.find((item) => item.ioc_id === ioc?.ioc_id) ??
    snapshot.iocs.find(
      (item) => item.value === value || item.value === ioc?.value,
    ) ??
    null
  );
}

function availabilityFor(
  rowKind: ObjectRow["rowKind"],
  objectKind: BootstrapIocV1["kind"],
  sampleId: string | null,
  download: BootstrapDownloadOutcomeV1 | null,
  snapshotIoc: BootstrapSnapshotIocV1 | null,
): Availability {
  if (rowKind === "sample" || objectKind === "sample") {
    if (
      sampleId !== null ||
      download?.status === "downloaded" ||
      download?.status === "reused"
    ) {
      return "available";
    }
    if (download?.status === "failed") return "unavailable";
    return "unknown";
  }
  if (snapshotIoc?.status === "partial") return "limited";
  if (
    snapshotIoc?.status === "unavailable" ||
    snapshotIoc?.status === "failed" ||
    snapshotIoc?.status === "policy_denied" ||
    snapshotIoc?.status === "needs_review"
  ) {
    return "unavailable";
  }
  if (snapshotIoc?.status === "succeeded") {
    return "available";
  }
  return "unknown";
}

function sourceForIoc(ioc: BootstrapIocV1): [string, string] {
  return [ioc.origin, IOC_ORIGIN_LABELS[ioc.origin]];
}

function sourceForSample(
  sampleOrigin: SampleOrigin | null,
  iocOrigin: BootstrapIocV1["origin"] | null,
): [string, string] {
  if (sampleOrigin !== null) {
    return [sampleOrigin, SAMPLE_ORIGIN_LABELS[sampleOrigin]];
  }
  if (iocOrigin !== null) return [iocOrigin, IOC_ORIGIN_LABELS[iocOrigin]];
  return ["unknown", "Source non exposée"];
}

function hashMatchesIoc(ioc: BootstrapIocV1, hash: string): boolean {
  return (
    ioc.value === hash ||
    ioc.hashes.some((item) => item.value === hash) ||
    ioc.associations.some((item) => item.sample_sha256 === hash)
  );
}

function buildObjectRows(
  inventory: IOCInventoryV1 | undefined,
  snapshot: BootstrapSnapshotV1 | undefined,
  samples: readonly InvestigationSampleDetail[] | undefined,
): ObjectRow[] {
  const iocs = [...(inventory?.seeds ?? []), ...(inventory?.discovered ?? [])];
  const sampleRows =
    inventory === undefined ? [] : buildSampleRows(inventory, snapshot);
  const representedAssociationIds = new Set(
    sampleRows.flatMap((row) =>
      row.associationId === null ? [] : [row.associationId],
    ),
  );
  const representedHashes = new Set(sampleRows.map((row) => row.sha256));
  const unlistedSamples = (samples ?? []).filter(
    (sample) =>
      !representedAssociationIds.has(sample.id) &&
      !representedHashes.has(sample.sample_sha256),
  );
  const rows: ObjectRow[] = [];

  for (const row of sampleRows) {
    const canonical = samples?.find(
      (sample) =>
        sample.id === row.associationId || sample.sample_sha256 === row.sha256,
    );
    const associationId = canonical?.id ?? row.associationId;
    const ioc = row.ioc;
    const snapshotIoc = findSnapshotIoc(snapshot, ioc, row.sha256);
    const download = downloadForAssociation(
      snapshotIoc?.download ?? row.download,
      associationId,
      row.sha256,
    );
    const sampleOrigin = canonical?.origin ?? row.origin;
    const [sourceKey, sourceLabel] = sourceForSample(sampleOrigin, ioc.origin);
    rows.push({
      key: `sample:${row.key}`,
      rowKind: "sample",
      value: row.sha256,
      kind: "sample",
      hashType: "sha256",
      ioc,
      iocOrigin: ioc.origin,
      sampleOrigin,
      associationId,
      sampleId: row.sampleId,
      qualification: canonical?.qualification ?? row.qualification,
      state: row.state,
      download,
      snapshotIoc,
      availability: availabilityFor(
        "sample",
        "sample",
        row.sampleId,
        download,
        snapshotIoc,
      ),
      sourceKey,
      sourceLabel,
    });
  }

  for (const sample of unlistedSamples) {
    const ioc =
      iocs.find((candidate) =>
        hashMatchesIoc(candidate, sample.sample_sha256),
      ) ?? null;
    const snapshotIoc = findSnapshotIoc(snapshot, ioc, sample.sample_sha256);
    const download = downloadForAssociation(
      snapshotIoc?.download ?? null,
      sample.id,
      sample.sample_sha256,
    );
    const [sourceKey, sourceLabel] = sourceForSample(
      sample.origin,
      ioc?.origin ?? null,
    );
    rows.push({
      key: `sample:canonical:${sample.id}`,
      rowKind: "sample",
      value: sample.sample_sha256,
      kind: "sample",
      hashType: "sha256",
      ioc,
      iocOrigin: ioc?.origin ?? null,
      sampleOrigin: sample.origin,
      associationId: sample.id,
      sampleId: null,
      qualification: sample.qualification,
      state: null,
      download,
      snapshotIoc,
      availability: availabilityFor(
        "sample",
        "sample",
        null,
        download,
        snapshotIoc,
      ),
      sourceKey,
      sourceLabel,
    });
  }

  for (const ioc of iocs) {
    const hasSameHashSample = rows.some(
      (row) =>
        row.rowKind === "sample" &&
        row.ioc?.ioc_id === ioc.ioc_id &&
        row.value === ioc.value,
    );
    if (ioc.kind === "sample" && hasSameHashSample) continue;
    const snapshotIoc = findSnapshotIoc(snapshot, ioc, ioc.value);
    const [sourceKey, sourceLabel] = sourceForIoc(ioc);
    rows.push({
      key: `ioc:${ioc.ioc_id}`,
      rowKind: "ioc",
      value: ioc.value,
      kind: ioc.kind,
      hashType: ioc.hash_type,
      ioc,
      iocOrigin: ioc.origin,
      sampleOrigin: null,
      associationId: null,
      sampleId: null,
      qualification: null,
      state: null,
      download: snapshotIoc?.download ?? null,
      snapshotIoc,
      availability: availabilityFor(
        "ioc",
        ioc.kind,
        null,
        snapshotIoc?.download ?? null,
        snapshotIoc,
      ),
      sourceKey,
      sourceLabel,
    });
  }
  return rows;
}

function rowSearchText(row: ObjectRow): string {
  return [
    row.value,
    row.ioc?.value,
    ...(row.ioc?.hashes.map((hash) => hash.value) ?? []),
  ]
    .filter((value): value is string => value !== undefined)
    .join("\n")
    .toLocaleLowerCase();
}

function rowMatchesSearch(row: ObjectRow, search: string): boolean {
  return rowSearchText(row).includes(search.trim().toLocaleLowerCase());
}

function sortValue(row: ObjectRow, column: SortColumn): string {
  switch (column) {
    case "value":
      return row.value;
    case "kind":
      return IOC_KIND_LABELS[row.kind];
    case "origin":
      return row.iocOrigin === null ? "" : IOC_ORIGIN_LABELS[row.iocOrigin];
    case "availability":
      return AVAILABILITY_LABELS[row.availability];
    case "qualification":
      return row.qualification === null
        ? ""
        : SAMPLE_QUALIFICATION_LABELS[row.qualification];
    case "source":
      return row.sourceLabel;
  }
}

function objectTokens(row: ObjectRow): Set<string> {
  return new Set(
    [
      row.value,
      row.ioc?.value,
      row.ioc?.ioc_id,
      row.associationId,
      row.sampleId,
      ...(row.ioc?.hashes.map((hash) => hash.value) ?? []),
    ].filter((value): value is string => value !== null && value !== undefined),
  );
}

function intersects(left: readonly string[], right: ReadonlySet<string>) {
  return left.some((value) => right.has(value));
}

interface RelatedEvidence {
  observations: InvestigationObservation[];
  staticAnalyses: InvestigationObservation[];
  findings: InvestigationFinding[];
  tasks: TaskView[];
  sessions: AgentSessionView[];
  contributions: WorkContributionView[];
  workspaceFiles: WorkspaceFileViewV1[];
}

function findRelatedEvidence(
  row: ObjectRow,
  investigation:
    | {
        observations: InvestigationObservation[];
        findings: InvestigationFinding[];
      }
    | undefined,
  tasks: readonly TaskView[],
  sessions: readonly AgentSessionView[],
  contributions: readonly WorkContributionView[],
  workspaceFiles: readonly WorkspaceFileViewV1[],
): RelatedEvidence {
  const tokens = objectTokens(row);
  const claimIds = new Set(row.ioc?.claims ?? []);
  const vtObservationIds = new Set(
    row.snapshotIoc?.operations.flatMap(
      (operation) => operation.observation_refs,
    ) ?? [],
  );
  const directlyLinkedContributions = contributions.filter(
    (item) =>
      row.associationId !== null &&
      item.sample_association_id === row.associationId,
  );
  const directObservationIds = new Set(
    directlyLinkedContributions.flatMap((item) =>
      item.observation_id === null ? [] : [item.observation_id],
    ),
  );
  let observations = (investigation?.observations ?? []).filter(
    (item) =>
      tokens.has(item.sha256) ||
      tokens.has(item.object_id) ||
      vtObservationIds.has(item.id) ||
      directObservationIds.has(item.id),
  );
  const observationIds = new Set(observations.map((item) => item.id));
  const directTasks = tasks.filter(
    (task) =>
      intersects(task.sample_refs, tokens) ||
      intersects(task.evidence_refs, tokens) ||
      intersects(task.claim_refs, claimIds) ||
      task.evidence_refs.some((reference) => observationIds.has(reference)) ||
      directlyLinkedContributions.some((item) => item.task_id === task.id),
  );
  const taskIds = new Set(directTasks.map((task) => task.id));
  let relatedContributions = contributions.filter(
    (item) =>
      directlyLinkedContributions.includes(item) ||
      (item.task_id !== null && taskIds.has(item.task_id)),
  );
  const contributionObservationIds = new Set(
    relatedContributions.flatMap((item) =>
      item.observation_id === null ? [] : [item.observation_id],
    ),
  );
  observations = (investigation?.observations ?? []).filter(
    (item) =>
      tokens.has(item.sha256) ||
      tokens.has(item.object_id) ||
      vtObservationIds.has(item.id) ||
      contributionObservationIds.has(item.id),
  );
  const allObservationIds = new Set(observations.map((item) => item.id));
  const findings = (investigation?.findings ?? []).filter(
    (finding) =>
      intersects(finding.observation_refs, allObservationIds) ||
      intersects(finding.claim_refs, claimIds) ||
      relatedContributions.some((item) => item.finding_id === finding.id),
  );
  const findingIds = new Set(findings.map((finding) => finding.id));
  relatedContributions = contributions.filter(
    (item) =>
      directlyLinkedContributions.includes(item) ||
      (item.task_id !== null && taskIds.has(item.task_id)) ||
      (item.observation_id !== null &&
        allObservationIds.has(item.observation_id)) ||
      (item.finding_id !== null && findingIds.has(item.finding_id)),
  );
  const relatedTaskIds = new Set([
    ...taskIds,
    ...relatedContributions.flatMap((item) =>
      item.task_id === null ? [] : [item.task_id],
    ),
  ]);
  const relatedTasks = tasks.filter((task) => relatedTaskIds.has(task.id));
  const relatedSessions = sessions.filter((session) =>
    relatedTaskIds.has(session.task_id),
  );
  const relatedWorkspaceFiles = workspaceFiles.filter(
    (file) => file.task_id !== null && relatedTaskIds.has(file.task_id),
  );
  const staticAnalyses = observations.filter((item) =>
    ["binary_analysis", "binary_string", "yara_match"].includes(item.kind),
  );
  return {
    observations,
    staticAnalyses,
    findings,
    tasks: relatedTasks,
    sessions: relatedSessions,
    contributions: relatedContributions,
    workspaceFiles: relatedWorkspaceFiles,
  };
}

function CoverageList({ coverage }: { coverage: BootstrapCoverageEntryV1[] }) {
  if (coverage.length === 0)
    return <p>Aucune limite de couverture déclarée.</p>;
  return (
    <ul className="samples-results__list">
      {coverage.map((entry) => (
        <li key={`${entry.kind}:${entry.target}:${entry.detail}`}>
          <span className="badge">{COVERAGE_KIND_LABELS[entry.kind]}</span>{" "}
          <code>{entry.target}</code> — {entry.detail}
          {entry.blocking ? " (bloquant)" : ""}
        </li>
      ))}
    </ul>
  );
}

function FindingSection({ findings }: { findings: InvestigationFinding[] }) {
  if (findings.length === 0)
    return <p>Aucun finding relié par observation, claim ou contribution.</p>;
  return (
    <ul className="samples-results__list">
      {findings.map((finding) => (
        <li key={finding.id}>
          <strong>{FINDING_KIND_LABELS[finding.kind] ?? finding.kind}</strong> ·{" "}
          {FINDING_ASSESSMENT_LABELS[finding.assessment] ?? finding.assessment}:{" "}
          {finding.statement}
          <p className="samples-results__meta">
            Finding <code>{finding.id}</code> · version {finding.version}
            {finding.limitations.length === 0
              ? ""
              : ` · ${finding.limitations.join(" · ")}`}
          </p>
        </li>
      ))}
    </ul>
  );
}

function ObservationSection({
  observations,
  emptyText,
  loading = false,
}: {
  observations: InvestigationObservation[];
  emptyText: string;
  loading?: boolean;
}) {
  if (loading) return <p role="status">Chargement des observations…</p>;
  if (observations.length === 0) return <p>{emptyText}</p>;
  return (
    <ul className="samples-results__list">
      {observations.map((observation) => (
        <li key={observation.id}>
          {labelOf(EVIDENCE_KIND_LABELS, observation.kind)} ·{" "}
          <code>{observation.sha256}</code> · {observation.source}{" "}
          {observation.source_version} ·{" "}
          <time dateTime={observation.observed_at}>
            {formatTimestamp(observation.observed_at)}
          </time>
          <div className="samples-results__meta">
            {observation.object_type} <code>{observation.object_id}</code>
            {observation.limitations.length === 0
              ? ""
              : ` · ${observation.limitations.join(" · ")}`}
          </div>
        </li>
      ))}
    </ul>
  );
}

function DetailPanel({
  row,
  investigation,
  tasks,
  sessions,
  contributions,
  workspaceFileList,
  loading,
  onCopy,
}: {
  row: ObjectRow;
  investigation: ReturnType<typeof useInvestigation>["data"];
  tasks: readonly TaskView[];
  sessions: readonly AgentSessionView[];
  contributions: readonly WorkContributionView[];
  workspaceFileList: WorkspaceFileListV1 | undefined;
  loading: {
    investigation: boolean;
    tasks: boolean;
    sessions: boolean;
    contributions: boolean;
    workspaceFiles: boolean;
  };
  onCopy: (value: string) => void;
}) {
  const related = findRelatedEvidence(
    row,
    investigation,
    tasks,
    sessions,
    contributions,
    workspaceFileList?.files ?? [],
  );
  const operationFailures = (row.snapshotIoc?.operations ?? []).filter(
    (operation) =>
      [
        "unavailable",
        "policy_denied",
        "needs_review",
        "failed",
        "partial",
      ].includes(operation.status),
  );
  const limitations = [
    ...(row.ioc?.limitations ?? []),
    ...(row.snapshotIoc?.operations.flatMap((item) => item.limitations) ?? []),
    ...related.observations.flatMap((item) => item.limitations),
    ...related.findings.flatMap((item) => item.limitations),
  ];
  const uniqueLimitations = [...new Set(limitations)];

  return (
    <section
      className="samples-results__section samples-results__detail"
      aria-label={`Détail de ${row.value}`}
    >
      <header className="samples-results__detail-header">
        <div>
          <h3>Détail de l’objet</h3>
          <p>
            <code>{row.value}</code>
          </p>
        </div>
        <button
          type="button"
          className="button button--secondary"
          aria-label={`Copier la valeur exacte ${row.value}`}
          onClick={() => onCopy(row.value)}
        >
          Copier
        </button>
      </header>
      <p className="samples-results__badges">
        <span className="badge">{AVAILABILITY_LABELS[row.availability]}</span>
        <span className="badge">
          {row.qualification === null
            ? "État G3 non exposé"
            : SAMPLE_QUALIFICATION_LABELS[row.qualification]}
        </span>
      </p>

      <section aria-label="Provenance">
        <h4>Provenance</h4>
        {row.ioc === null ? (
          <p>Association d’échantillon sans IOC d’inventaire correspondant.</p>
        ) : (
          <>
            <p>
              {IOC_KIND_LABELS[row.ioc.kind]} ·{" "}
              {IOC_ORIGIN_LABELS[row.ioc.origin]} · <code>{row.ioc.value}</code>
            </p>
            {row.ioc.sources.length === 0 ? (
              <p>Aucune source documentaire reliée à cet IOC.</p>
            ) : (
              <ul className="samples-results__list">
                {row.ioc.sources.map((source, index) => (
                  <li key={`${source.source_document_ids.join(":")}:${index}`}>
                    Documents{" "}
                    {source.source_document_ids.map((id) => (
                      <code key={id}>{id}</code>
                    ))}{" "}
                    · références {source.source_refs.join(", ") || "—"} · claims{" "}
                    {source.claim_ids.map((id) => (
                      <code key={id}>{id}</code>
                    ))}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
        {row.associationId === null ? null : (
          <p>
            Association <code>{row.associationId}</code> · origine{" "}
            {row.sampleOrigin === null
              ? "non exposée"
              : SAMPLE_ORIGIN_LABELS[row.sampleOrigin]}{" "}
            ·{" "}
            {row.qualification === null
              ? "qualification non exposée"
              : SAMPLE_QUALIFICATION_LABELS[row.qualification]}
            {row.state === null ? "" : ` · ${SAMPLE_STATE_LABELS[row.state]}`}
          </p>
        )}
        {row.ioc?.claims.length ? (
          <p>
            Claims reliés :{" "}
            {row.ioc.claims.map((id) => (
              <code key={id}>{id}</code>
            ))}
          </p>
        ) : null}
      </section>

      <section aria-label="Collecte VirusTotal">
        <h4>Collecte VT</h4>
        {row.snapshotIoc === null ? (
          <p>Aucun résultat de collecte VT enregistré pour cet objet.</p>
        ) : (
          <>
            <p>
              {INVOCATION_STATUS_LABELS[row.snapshotIoc.status]} ·{" "}
              {operationOutcomeLabel(row.snapshotIoc.status)}
              {row.snapshotIoc.reason === null
                ? ""
                : ` · ${labelOf(REASON_LABELS, row.snapshotIoc.reason)}`}
            </p>
            {row.snapshotIoc.operations.length === 0 ? (
              <p>Aucune opération exécutée.</p>
            ) : (
              <ul className="samples-results__list">
                {row.snapshotIoc.operations.map((operation) => (
                  <li key={operation.operation_key}>
                    {labelOf(VT_TOOL_LABELS, operation.tool_name)} ·{" "}
                    {operationOutcomeLabel(operation.status)}
                    {operation.reason === null
                      ? ""
                      : ` · ${labelOf(REASON_LABELS, operation.reason)}`}
                    {operation.observation_refs.length === 0
                      ? ""
                      : ` · ${operation.observation_refs.length} observation(s) référencée(s)`}
                    {operation.limitations.length === 0
                      ? ""
                      : ` · ${operation.limitations.join(" · ")}`}
                  </li>
                ))}
              </ul>
            )}
            {row.download === null ? null : (
              <p>
                Téléchargement :{" "}
                {labelOf(DOWNLOAD_STATUS_LABELS, row.download.status)} ·{" "}
                {row.download.hash_verified_quarantine
                  ? "SHA-256 vérifié en quarantaine"
                  : "hash non vérifié"}
                {row.download.reason === null
                  ? ""
                  : ` · ${labelOf(REASON_LABELS, row.download.reason)}`}
              </p>
            )}
          </>
        )}
      </section>

      <section aria-label="Analyse statique">
        <h4>Analyse statique</h4>
        <ObservationSection
          observations={related.staticAnalyses}
          loading={loading.investigation}
          emptyText="Aucune observation d’analyse statique reliée par hash, identifiant d’objet ou contribution."
        />
      </section>

      <section aria-label="Sessions de travail">
        <h4>Sessions ayant travaillé sur l’objet</h4>
        {loading.sessions ? (
          <p role="status">Chargement des sessions…</p>
        ) : related.sessions.length === 0 ? (
          <p>
            Aucune session reliée aux tâches ou contributions correspondantes.
          </p>
        ) : (
          <ul className="samples-results__list">
            {related.sessions.map((session) => (
              <li key={session.id}>
                Session <code>{session.id}</code> ·{" "}
                {labelOf(SESSION_STATE_LABELS, session.state)} ·{" "}
                {session.adapter_kind} ·{" "}
                <time dateTime={session.opened_at}>
                  {formatTimestamp(session.opened_at)}
                </time>
                {session.closed_at === null
                  ? ""
                  : ` — ${formatTimestamp(session.closed_at)}`}
                <span className="samples-results__meta">
                  {" "}
                  · tâche <code>{session.task_id}</code>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Observations">
        <h4>Observations</h4>
        <ObservationSection
          observations={related.observations}
          loading={loading.investigation}
          emptyText="Aucune observation canonique reliée par hash, identifiant d’objet ou référence VT."
        />
      </section>

      <section aria-label="Findings">
        <h4>Findings</h4>
        {loading.investigation ? (
          <p role="status">Chargement des findings…</p>
        ) : (
          <FindingSection findings={related.findings} />
        )}
      </section>

      <section aria-label="Fichiers associés">
        <h4>Fichiers associés</h4>
        {loading.contributions || loading.workspaceFiles ? (
          <p role="status">Chargement des fichiers associés…</p>
        ) : workspaceFileList?.workspace_available === false ? (
          <p>Le workspace de cette investigation est indisponible.</p>
        ) : related.contributions.length === 0 &&
          related.workspaceFiles.length === 0 ? (
          <p>
            Aucun fichier de contribution ne fournit de liaison à cet objet. Les
            contrats ne relient pas directement un chemin de fichier à un hash,
            IOC ou finding; les chemins de workspace sont reliés via leurs
            tâches.
          </p>
        ) : (
          <ul className="samples-results__list">
            {related.contributions.map((contribution) => (
              <li key={contribution.id}>
                <code>{contribution.file_relpath}</code> · SHA-256{" "}
                <code>{contribution.file_sha256}</code> ·{" "}
                {CONTRIBUTION_CATEGORY_LABELS[contribution.category]} ·{" "}
                {CONTRIBUTION_IMPORT_LABELS[contribution.import_status]} ·{" "}
                {CHIP_KINDS[contribution.category]}
                {contribution.validation_error_code === null
                  ? ""
                  : ` · échec ${contribution.validation_error_code}`}
              </li>
            ))}
          </ul>
        )}
        {related.workspaceFiles.length === 0 ? null : (
          <ul className="samples-results__list">
            {related.workspaceFiles
              .filter(
                (file) =>
                  !related.contributions.some(
                    (contribution) =>
                      contribution.file_relpath === file.path &&
                      contribution.file_sha256 === file.sha256,
                  ),
              )
              .map((file) => (
                <li key={file.path + ":" + file.revision_id}>
                  <code>{file.path}</code> · SHA-256 <code>{file.sha256}</code>{" "}
                  · lié à la tâche <code>{file.task_id}</code> · workspace
                  {" · "}
                  {file.import_status}
                </li>
              ))}
          </ul>
        )}
        {workspaceFileList?.next_page_token !== null &&
        workspaceFileList !== undefined ? (
          <p className="samples-results__meta">
            La liste workspace contient d’autres pages; seuls les fichiers de la
            première page ont été joints.
          </p>
        ) : null}
      </section>

      <section aria-label="Tâches liées">
        <h4>Tâches liées</h4>
        {loading.tasks ? (
          <p role="status">Chargement des tâches…</p>
        ) : related.tasks.length === 0 ? (
          <p>
            Aucune tâche reliée par sample_refs, evidence_refs ou contribution.
          </p>
        ) : (
          <ul className="samples-results__list">
            {related.tasks.map((task) => {
              const href = investigationHref({ vue: "plan", task: task.id });
              return (
                <li key={task.id}>
                  <a
                    href={href}
                    onClick={(event) =>
                      handleInvestigationLinkClick(event, href)
                    }
                  >
                    {task.title} · {task.status} · <code>{task.id}</code>
                  </a>
                  {task.question ? <div>{task.question}</div> : null}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-label="Limites et échecs">
        <h4>Limites et échecs</h4>
        {uniqueLimitations.length === 0 &&
        operationFailures.length === 0 &&
        related.contributions.every(
          (item) => item.validation_error_code === null,
        ) ? (
          <p>Aucune limite ni échec déclaré dans les éléments reliés.</p>
        ) : (
          <ul className="samples-results__list">
            {uniqueLimitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
            {operationFailures.map((operation) => (
              <li key={`failure:${operation.operation_key}`}>
                {labelOf(VT_TOOL_LABELS, operation.tool_name)} ·{" "}
                {operationOutcomeLabel(operation.status)}
                {operation.error_code === null
                  ? ""
                  : ` · ${operation.error_code}`}
              </li>
            ))}
            {related.contributions.flatMap((item) =>
              item.validation_error_code === null
                ? []
                : [
                    <li key={`contribution:${item.id}`}>
                      Contribution {item.id} · {item.validation_error_code}
                    </li>,
                  ],
            )}
          </ul>
        )}
      </section>
    </section>
  );
}

function TechnicalDiagnostics({
  inventory,
  plan,
  snapshot,
}: {
  inventory: IOCInventoryV1 | undefined;
  plan: BootstrapPlanV1 | undefined;
  snapshot: BootstrapSnapshotV1 | undefined;
}) {
  return (
    <details className="samples-results__diagnostics">
      <summary>Diagnostics techniques</summary>
      <section aria-label="Couverture de l’inventaire IOC">
        <h3>Couverture de l’inventaire IOC</h3>
        {inventory === undefined ? (
          <>
            <p>Couverture de l’inventaire indisponible.</p>
            {snapshot?.limits.inventory_truncated ? (
              <p>
                Le snapshot signale une troncature; des IOC ou résultats peuvent
                manquer dans les objets affichés.
              </p>
            ) : null}
          </>
        ) : (
          <>
            <p>
              {inventory.limits.iocs_considered} IOC considérés ·{" "}
              {inventory.limits.truncated
                ? "inventaire tronqué"
                : "inventaire non tronqué"}
              .
            </p>
            {inventory.limits.truncated ||
            snapshot?.limits.inventory_truncated === true ? (
              <p>
                Des IOC ou résultats peuvent manquer dans les objets affichés.
              </p>
            ) : null}
            {inventory.limitations.length === 0 ? null : (
              <ul className="samples-results__list">
                {inventory.limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
      <section aria-label="État technique du plan VT">
        <h3>Plan et capacité</h3>
        {plan === undefined ? (
          <p>Plan technique indisponible.</p>
        ) : (
          <>
            <p>
              État {PLAN_STATE_LABELS[plan.state]} · enveloppe G2{" "}
              {plan.envelope_version ?? "non définie"} · budget VT restant{" "}
              {plan.budget.vt_units_remaining ?? "inconnu"} · téléchargements
              restants {plan.budget.downloads_remaining ?? "inconnu"}.
            </p>
            <ul className="samples-results__list">
              {plan.operations.map((operation) => (
                <li key={operation.operation_key}>
                  Clé <code>{operation.operation_key}</code> · IOC{" "}
                  <code>{operation.ioc_id}</code> ·{" "}
                  {labelOf(VT_TOOL_LABELS, operation.tool_name)} ·{" "}
                  {INVOCATION_STATUS_LABELS[operation.status]} · route{" "}
                  {ROUTE_STATUS_LABELS[operation.route_status]} · quota réservé{" "}
                  {operation.reserved_vt_units} VT /{" "}
                  {operation.reserved_downloads} téléchargement(s)
                  {operation.limitations.length === 0
                    ? ""
                    : ` · ${operation.limitations.join(" · ")}`}
                </li>
              ))}
            </ul>
            <CoverageList coverage={plan.coverage} />
          </>
        )}
      </section>
      <section aria-label="Pagination et clés techniques VT">
        <h3>Pagination et références techniques des résultats</h3>
        {snapshot === undefined ? (
          <p>Aucun snapshot de collecte enregistré.</p>
        ) : (
          <>
            <p>
              Snapshot <code>{snapshot.snapshot_id}</code> · généré{" "}
              {formatTimestamp(snapshot.generated_at)} ·{" "}
              {snapshot.limits.operations_recorded}/
              {snapshot.limits.max_operations} opérations consignées
              {snapshot.limits.operations_truncated ? " (liste tronquée)" : ""}.
            </p>
            {snapshot.iocs.flatMap((ioc) =>
              ioc.operations.map((operation) => (
                <article key={operation.operation_key}>
                  <h4>
                    {ioc.value} · <code>{operation.operation_key}</code>
                  </h4>
                  <p>
                    {labelOf(VT_TOOL_LABELS, operation.tool_name)} · pages{" "}
                    {operation.pages ?? "inconnues"} · champs observés{" "}
                    {operation.observed_count ?? "inconnu"} · pagination{" "}
                    {operation.pagination_complete === null
                      ? "inconnue"
                      : operation.pagination_complete
                        ? "complète"
                        : `incomplète, curseur ${operation.next_cursor ?? "inconnu"}`}
                    .
                  </p>
                  <p>
                    Routes {operation.provider_routes.join(", ") || "—"} ·
                    invocation <code>{operation.invocation_id ?? "—"}</code> ·
                    références observation{" "}
                    {operation.observation_refs.map((id) => (
                      <code key={id}>{id}</code>
                    ))}{" "}
                    · codes {operation.detail_code ?? "—"} /{" "}
                    {operation.error_code ?? "—"}.
                  </p>
                </article>
              )),
            )}
            <CoverageList coverage={snapshot.coverage} />
            {snapshot.limitations.length ? (
              <ul className="samples-results__list">
                {snapshot.limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </section>
    </details>
  );
}

export function SamplesResultsView({
  investigationId,
}: {
  investigationId: string;
}) {
  const baseId = useId();
  const [activeView, setActiveView] = useState<"objects" | "results">(
    "objects",
  );
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [originFilter, setOriginFilter] = useState("all");
  const [availabilityFilter, setAvailabilityFilter] = useState("all");
  const [qualificationFilter, setQualificationFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [sortColumn, setSortColumn] = useState<SortColumn>("value");
  const [sortDirection, setSortDirection] = useState<
    "ascending" | "descending"
  >("ascending");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [copyMessage, setCopyMessage] = useState("");

  const inventory = useBootstrapInventory(investigationId);
  const plan = useBootstrapPlan(investigationId);
  const snapshot = useBootstrapSnapshot(investigationId);
  const contributions = useContributions(investigationId);
  const investigation = useInvestigation(investigationId);
  const samples = useInvestigationSamples(investigationId);
  const tasks = useTasks(investigationId);
  const sessions = useAgentSessions(investigationId);
  const workspaceFiles = useWorkspaceFiles(investigationId, { limit: 500 });
  const run = useRunBootstrap(investigationId);

  const snapshotMissing =
    snapshot.error !== null &&
    isWorkshopApiError(snapshot.error) &&
    snapshot.error.code === "bootstrap_snapshot_not_found";
  const snapshotData = snapshotMissing ? undefined : snapshot.data;
  const allRows = buildObjectRows(inventory.data, snapshotData, samples.data);
  const sourceOptions = [
    ...new Map(
      allRows.map((row) => [row.sourceKey, row.sourceLabel]),
    ).entries(),
  ].sort((left, right) => left[1].localeCompare(right[1], "fr"));
  const visibleRows = allRows
    .filter((row) => rowMatchesSearch(row, search))
    .filter((row) => typeFilter === "all" || row.kind === typeFilter)
    .filter(
      (row) =>
        originFilter === "all" ||
        (originFilter === "unlinked" && row.iocOrigin === null) ||
        row.iocOrigin === originFilter,
    )
    .filter(
      (row) =>
        availabilityFilter === "all" || row.availability === availabilityFilter,
    )
    .filter(
      (row) =>
        qualificationFilter === "all" ||
        (qualificationFilter === "none" && row.qualification === null) ||
        row.qualification === qualificationFilter,
    )
    .filter((row) => sourceFilter === "all" || row.sourceKey === sourceFilter)
    .sort((left, right) => {
      const result = sortValue(left, sortColumn).localeCompare(
        sortValue(right, sortColumn),
        "fr",
        { numeric: true, sensitivity: "base" },
      );
      return sortDirection === "ascending" ? result : -result;
    });
  const selected = allRows.find((row) => row.key === selectedKey) ?? null;
  const taskData = tasks.data?.items ?? [];
  const contributionData = contributions.data ?? [];
  const sessionData = sessions.data ?? [];
  const workspaceFileList = workspaceFiles.data;

  async function copyValue(value: string) {
    try {
      if (navigator.clipboard?.writeText === undefined) {
        throw new Error("clipboard API unavailable");
      }
      await navigator.clipboard.writeText(value);
      setCopyMessage(`Valeur copiée exactement : ${value}`);
    } catch {
      setCopyMessage(
        "La copie est indisponible dans ce contexte du navigateur.",
      );
    }
  }

  function changeSort(column: SortColumn) {
    if (sortColumn === column) {
      setSortDirection((current) =>
        current === "ascending" ? "descending" : "ascending",
      );
      return;
    }
    setSortColumn(column);
    setSortDirection("ascending");
  }

  function onTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === "Home"
        ? "objects"
        : event.key === "End"
          ? "results"
          : activeView === "objects"
            ? "results"
            : "objects";
    setActiveView(next);
    document.getElementById(`${baseId}-tab-${next}`)?.focus();
  }

  const planData = plan.data;
  const planReady = planData?.state === "ready";

  return (
    <div className="samples-results" aria-label="Échantillons et résultats">
      <section
        className="samples-results__section samples-results__filters"
        aria-label="Recherche et filtres"
      >
        <label htmlFor={`${baseId}-search`}>Rechercher un IOC ou un hash</label>
        <input
          id={`${baseId}-search`}
          type="search"
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
          placeholder="Hash ou valeur IOC complète"
        />
        <div className="samples-results__filter-grid">
          <label>
            Type
            <select
              value={typeFilter}
              onChange={(event) => setTypeFilter(event.currentTarget.value)}
            >
              <option value="all">Tous les types</option>
              {Object.entries(IOC_KIND_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Statut seed ou découvert
            <select
              value={originFilter}
              onChange={(event) => setOriginFilter(event.currentTarget.value)}
            >
              <option value="all">Tous</option>
              <option value="seed">Seed — article</option>
              <option value="discovered">Découvert</option>
              <option value="unlinked">Sans IOC d’inventaire</option>
            </select>
          </label>
          <label>
            Disponibilité
            <select
              value={availabilityFilter}
              onChange={(event) =>
                setAvailabilityFilter(event.currentTarget.value)
              }
            >
              <option value="all">Toutes</option>
              {Object.entries(AVAILABILITY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            État G3
            <select
              value={qualificationFilter}
              onChange={(event) =>
                setQualificationFilter(event.currentTarget.value)
              }
            >
              <option value="all">Tous</option>
              <option value="none">Non exposé</option>
              {Object.entries(SAMPLE_QUALIFICATION_LABELS).map(
                ([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ),
              )}
            </select>
          </label>
          <label>
            Source
            <select
              value={sourceFilter}
              onChange={(event) => setSourceFilter(event.currentTarget.value)}
            >
              <option value="all">Toutes les sources</option>
              {sourceOptions.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="samples-results__meta" aria-live="polite">
          {visibleRows.length} objet(s) affiché(s) sur {allRows.length} ·
          valeurs de hash et d’IOC conservées intégralement.
        </p>
      </section>

      {inventory.isPending ? (
        <p role="status">Chargement de l’inventaire IOC…</p>
      ) : null}
      {inventory.error !== null ? (
        <WorkshopErrorNotice
          label="L’inventaire IOC n’a pas pu être lu."
          error={inventory.error}
        />
      ) : null}
      {snapshot.error !== null && !snapshotMissing ? (
        <WorkshopErrorNotice
          label="Les résultats VT n’ont pas pu être lus."
          error={snapshot.error}
        />
      ) : null}

      <div
        id={`${baseId}-tabs`}
        className="samples-results__tabs"
        role="tablist"
        aria-label="Sous-vues des échantillons et résultats"
      >
        <button
          id={`${baseId}-tab-objects`}
          type="button"
          role="tab"
          aria-selected={activeView === "objects"}
          aria-controls={`${baseId}-panel-objects`}
          tabIndex={activeView === "objects" ? 0 : -1}
          onKeyDown={onTabKeyDown}
          onClick={() => setActiveView("objects")}
        >
          IOC et échantillons
        </button>
        <button
          id={`${baseId}-tab-results`}
          type="button"
          role="tab"
          aria-selected={activeView === "results"}
          aria-controls={`${baseId}-panel-results`}
          tabIndex={activeView === "results" ? 0 : -1}
          onKeyDown={onTabKeyDown}
          onClick={() => setActiveView("results")}
        >
          Résultats et analyses
        </button>
      </div>

      <section
        id={`${baseId}-panel-objects`}
        className="samples-results__section"
        role="tabpanel"
        aria-labelledby={`${baseId}-tab-objects`}
        hidden={activeView !== "objects"}
      >
        <h2>IOC et échantillons</h2>
        {visibleRows.length === 0 ? (
          <p>
            Aucun IOC ou échantillon ne correspond à la recherche et aux
            filtres.
          </p>
        ) : (
          <div className="table-scroll">
            <table>
              <caption className="samples-results__caption">
                Inventaire filtré — {visibleRows.length} ligne(s)
              </caption>
              <thead>
                <tr>
                  <th scope="col">Détail</th>
                  {(
                    [
                      ["value", "Valeur"],
                      ["kind", "Type"],
                      ["origin", "Seed / découvert"],
                      ["availability", "Disponibilité"],
                      ["qualification", "État G3"],
                      ["source", "Source"],
                    ] as const
                  ).map(([column, label]) => (
                    <th
                      key={column}
                      scope="col"
                      aria-sort={sortColumn === column ? sortDirection : "none"}
                    >
                      <button
                        type="button"
                        className="samples-results__sort"
                        onClick={() => changeSort(column)}
                      >
                        {label}
                        {sortColumn === column
                          ? sortDirection === "ascending"
                            ? " ↑"
                            : " ↓"
                          : ""}
                      </button>
                    </th>
                  ))}
                  <th scope="col">Copier</th>
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row) => (
                  <tr key={row.key}>
                    <td>
                      <button
                        type="button"
                        className="button button--secondary"
                        onClick={() => setSelectedKey(row.key)}
                      >
                        Détail
                      </button>
                    </td>
                    <td>
                      <code>{row.value}</code>
                      {row.hashType === null ? null : (
                        <span className="samples-results__meta">
                          {" "}
                          · {row.hashType}
                        </span>
                      )}
                    </td>
                    <td>{IOC_KIND_LABELS[row.kind]}</td>
                    <td>
                      {row.iocOrigin === null
                        ? "—"
                        : IOC_ORIGIN_LABELS[row.iocOrigin]}
                    </td>
                    <td>{AVAILABILITY_LABELS[row.availability]}</td>
                    <td>
                      {row.qualification === null
                        ? "—"
                        : SAMPLE_QUALIFICATION_LABELS[row.qualification]}
                    </td>
                    <td>{row.sourceLabel}</td>
                    <td>
                      <button
                        type="button"
                        className="button button--secondary"
                        aria-label={`Copier la valeur exacte ${row.value}`}
                        onClick={() => void copyValue(row.value)}
                      >
                        Copier
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {selected === null ? null : (
          <DetailPanel
            row={selected}
            investigation={investigation.data}
            tasks={taskData}
            sessions={sessionData}
            contributions={contributionData}
            workspaceFileList={workspaceFileList}
            loading={{
              investigation: investigation.isPending,
              tasks: tasks.isPending,
              sessions: sessions.isPending,
              contributions: contributions.isPending,
              workspaceFiles: workspaceFiles.isPending,
            }}
            onCopy={(value) => void copyValue(value)}
          />
        )}
      </section>

      <section
        id={`${baseId}-panel-results`}
        className="samples-results__section"
        role="tabpanel"
        aria-labelledby={`${baseId}-tab-results`}
        hidden={activeView !== "results"}
      >
        <h2>Résultats et analyses</h2>
        <p className="samples-results__meta">
          Les résultats sont regroupés par IOC ou échantillon. Ouvrez un objet
          pour consulter les liaisons et leurs limites.
        </p>
        {visibleRows.length === 0 ? (
          <p>Aucun résultat ne correspond à la recherche et aux filtres.</p>
        ) : (
          <div className="samples-results__groups">
            {[
              ...new Map(
                visibleRows.map((row) => [
                  row.ioc?.ioc_id ?? row.key,
                  row.ioc?.value ?? row.value,
                ]),
              ).entries(),
            ].map(([groupKey, label]) => {
              const groupRows = visibleRows.filter(
                (row) => (row.ioc?.ioc_id ?? row.key) === groupKey,
              );
              return (
                <section
                  className="samples-results__result-group"
                  key={groupKey}
                  aria-label={`Résultats pour ${label}`}
                >
                  <h3>
                    {groupRows[0]?.ioc === null
                      ? "Échantillon"
                      : "IOC / échantillon"}{" "}
                    · <code>{label}</code>
                  </h3>
                  <ul className="samples-results__result-list">
                    {groupRows.map((row) => (
                      <li key={row.key}>
                        <button
                          type="button"
                          className="samples-results__result-select"
                          aria-label={`Ouvrir les résultats pour ${row.value}`}
                          aria-pressed={selectedKey === row.key}
                          onClick={() => setSelectedKey(row.key)}
                        >
                          <code>{row.value}</code>
                          <span>
                            {IOC_KIND_LABELS[row.kind]} ·{" "}
                            {AVAILABILITY_LABELS[row.availability]}
                          </span>
                          <span>
                            {row.qualification === null
                              ? "État G3 non exposé"
                              : SAMPLE_QUALIFICATION_LABELS[row.qualification]}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
          </div>
        )}
        {selected === null ? null : (
          <DetailPanel
            row={selected}
            investigation={investigation.data}
            tasks={taskData}
            sessions={sessionData}
            contributions={contributionData}
            workspaceFileList={workspaceFileList}
            loading={{
              investigation: investigation.isPending,
              tasks: tasks.isPending,
              sessions: sessions.isPending,
              contributions: contributions.isPending,
              workspaceFiles: workspaceFiles.isPending,
            }}
            onCopy={(value) => void copyValue(value)}
          />
        )}
      </section>

      {copyMessage ? (
        <p className="samples-results__copy-status" role="status">
          {copyMessage}
        </p>
      ) : null}

      <section className="samples-results__section" aria-label="Collecte VT">
        <h2>Collecte VT</h2>
        {plan.isPending ? (
          <p role="status">Chargement du plan de collecte…</p>
        ) : null}
        {plan.error !== null ? (
          <WorkshopErrorNotice
            label="Le plan de collecte n’a pas pu être lu."
            error={plan.error}
          />
        ) : null}
        {planReady ? (
          <div className="samples-results__action">
            <button
              className="button"
              type="button"
              disabled={run.isPending}
              onClick={() => run.mutate()}
            >
              {run.isPending
                ? "Collecte VT en cours…"
                : snapshotData === undefined
                  ? "Lancer la collecte VT"
                  : "Relancer la collecte VT"}
            </button>
            <span className="samples-results__meta">
              Collecte bornée par l’enveloppe G2 approuvée.
            </span>
          </div>
        ) : null}
        {planData !== undefined && planData.state !== "ready" ? (
          <p className="samples-results__blocked" role="status">
            {PLAN_STATE_REASONS[planData.state]}
          </p>
        ) : null}
        {run.isPending ? (
          <WorkshopLongOperationState
            label="Collecte VT en cours"
            detail="Les résultats partiels restent consultables par objet."
          />
        ) : null}
        {run.error !== null ? (
          <WorkshopErrorNotice
            label="La collecte VT a échoué."
            error={run.error}
          />
        ) : null}
      </section>

      {selected !== null && selected.associationId !== null ? (
        <details className="samples-results__g3">
          <summary>Historique et décision humaine G3</summary>
          <SampleQualificationPanel investigationId={investigationId} />
        </details>
      ) : null}

      {investigation.isError ? (
        <p className="samples-results__warn" role="note">
          Les observations et findings canoniques ne sont pas disponibles :{" "}
          {investigation.error.message}
        </p>
      ) : null}
      {samples.error !== null ? (
        <p className="samples-results__warn" role="note">
          Les associations G3 canoniques ne sont pas disponibles :{" "}
          {samples.error.message}
        </p>
      ) : null}
      {tasks.error !== null ? (
        <p className="samples-results__warn" role="note">
          Les tâches ne sont pas disponibles : {tasks.error.message}
        </p>
      ) : null}
      {sessions.error !== null ? (
        <p className="samples-results__warn" role="note">
          Les sessions ne sont pas disponibles : {sessions.error.message}
        </p>
      ) : null}
      {contributions.error !== null ? (
        <p className="samples-results__warn" role="note">
          Les contributions ne sont pas disponibles :{" "}
          {contributions.error.message}
        </p>
      ) : null}
      {workspaceFiles.error !== null ? (
        <p className="samples-results__warn" role="note">
          Les fichiers du workspace ne sont pas disponibles :{" "}
          {workspaceFiles.error.message}
        </p>
      ) : null}
      <TechnicalDiagnostics
        inventory={inventory.data}
        plan={planData}
        snapshot={snapshotData}
      />
    </div>
  );
}
