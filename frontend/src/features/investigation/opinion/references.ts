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

export interface ReferenceTechnicalValue {
  label: string;
  value: string;
}

export interface ReferencePickerEntry {
  id: string;
  section: string;
  groupKey: string;
  label: string;
  searchText: string;
  values: ReferenceTechnicalValue[];
  observedAt?: string;
  disabledReason?: string;
}

function shortObjectId(value: string): string {
  return value.length > 12 ? value.slice(0, 10) + "…" : value;
}

export function observationReferenceEntries(
  observations: InvestigationObservation[],
): ReferencePickerEntry[] {
  return observations.map((observation) => {
    const kind = EVIDENCE_KIND_LABELS[observation.kind] ?? observation.kind;
    const object =
      observation.object_type + "/" + shortObjectId(observation.object_id);
    return {
      id: observation.id,
      section: observation.source,
      groupKey: JSON.stringify([
        observation.source,
        observation.kind,
        observation.object_type,
        observation.object_id,
      ]),
      label: kind + " · " + object,
      searchText: [
        kind,
        observation.source,
        observation.object_type,
        observation.object_id,
        observation.id,
        observation.sha256,
      ].join(" "),
      values: [
        { label: "ID de l’observation", value: observation.id },
        { label: "Identifiant exact de l’objet", value: observation.object_id },
        { label: "SHA-256 de l’observation", value: observation.sha256 },
      ],
      observedAt: observation.observed_at,
    };
  });
}

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
