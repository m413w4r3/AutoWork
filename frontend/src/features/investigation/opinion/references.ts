import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import {
  EVIDENCE_KIND_LABELS,
  FINDING_KIND_LABELS,
  shortHash,
  truncate,
} from "./labels";

export function findingLabel(
  findings: InvestigationFinding[],
  id: string,
): string {
  const finding = findings.find((item) => item.id === id);
  if (finding === undefined) return `${id} (introuvable dans cette enquête)`;
  const kind = FINDING_KIND_LABELS[finding.kind] ?? finding.kind;
  return `${kind} · ${truncate(finding.statement)}`;
}

export function observationLabel(
  observations: InvestigationObservation[],
  id: string,
): string {
  const observation = observations.find((item) => item.id === id);
  if (observation === undefined) {
    return `${id} (introuvable dans cette enquête)`;
  }
  const kind = EVIDENCE_KIND_LABELS[observation.kind] ?? observation.kind;
  return `${kind} · SHA-256 ${shortHash(observation.sha256)} · ${
    observation.source
  }`;
}
