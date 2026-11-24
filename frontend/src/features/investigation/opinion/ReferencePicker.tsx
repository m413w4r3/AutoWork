import { useEffect, useMemo, useRef, useState } from "react";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import { EVIDENCE_KIND_LABELS, FINDING_KIND_LABELS, truncate } from "./labels";
import { CopyableTechnicalValue } from "./CopyableTechnicalValue";

interface TechnicalValue {
  label: string;
  value: string;
}

interface Entry {
  id: string;
  section: string;
  groupKey: string;
  label: string;
  searchText: string;
  values: TechnicalValue[];
  observedAt?: string;
}

interface Group {
  key: string;
  section: string;
  label: string;
  entries: Entry[];
}

interface SectionGroup {
  key: string;
  label: string;
  groups: Group[];
}

function shortObjectId(value: string): string {
  return value.length > 12 ? value.slice(0, 10) + "…" : value;
}

function findingEntries(findings: InvestigationFinding[]): Entry[] {
  return findings.map((finding) => {
    const kind = FINDING_KIND_LABELS[finding.kind] ?? finding.kind;
    return {
      id: finding.id,
      section: kind,
      groupKey: JSON.stringify([finding.kind, finding.statement]),
      label: truncate(finding.statement, 110),
      searchText: [
        kind,
        finding.statement,
        finding.id,
        finding.logical_id,
        finding.current_version_id,
      ].join(" "),
      values: [
        { label: "ID du finding", value: finding.id },
        { label: "ID logique", value: finding.logical_id },
        { label: "ID de version", value: finding.current_version_id },
      ],
    };
  });
}

function observationEntries(observations: InvestigationObservation[]): Entry[] {
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

function groupEntries(entries: Entry[]): Group[] {
  const groups = new Map<string, Group>();
  for (const entry of entries) {
    const key = JSON.stringify([entry.section, entry.groupKey]);
    const existing = groups.get(key);
    if (existing === undefined) {
      groups.set(key, {
        key,
        section: entry.section,
        label: entry.label,
        entries: [entry],
      });
    } else {
      existing.entries.push(entry);
    }
  }
  return [...groups.values()].sort((left, right) =>
    (left.section + " " + left.label).localeCompare(
      right.section + " " + right.label,
      "fr",
    ),
  );
}

function toggleGroup(selectedIds: string[], groupIds: string[]): string[] {
  const allSelected = groupIds.every((id) => selectedIds.includes(id));
  if (allSelected) {
    const removing = new Set(groupIds);
    return selectedIds.filter((id) => !removing.has(id));
  }
  return [...new Set([...selectedIds, ...groupIds])];
}

function GroupRow({
  group,
  selectedIds,
  onChange,
  itemName,
}: {
  group: Group;
  selectedIds: string[];
  onChange: (next: string[]) => void;
  itemName: "finding" | "observation";
}) {
  const checkboxRef = useRef<HTMLInputElement>(null);
  const selectedCount = group.entries.filter((entry) =>
    selectedIds.includes(entry.id),
  ).length;
  const ids = group.entries.map((entry) => entry.id);
  const allSelected = selectedCount === group.entries.length;

  useEffect(() => {
    if (checkboxRef.current !== null) {
      checkboxRef.current.indeterminate = selectedCount > 0 && !allSelected;
    }
  }, [allSelected, selectedCount]);

  return (
    <div className="opinion-reference-picker__group">
      <label className="opinion-reference-picker__option">
        <input
          ref={checkboxRef}
          type="checkbox"
          checked={allSelected}
          aria-label={group.label}
          onChange={() => onChange(toggleGroup(selectedIds, ids))}
        />
        <span className="opinion-reference-picker__option-text">
          <span className="opinion-reference-picker__label">{group.label}</span>
          {group.entries.length > 1 ? (
            <span className="opinion-reference-picker__count">
              {group.entries.length} références regroupées
            </span>
          ) : null}
        </span>
      </label>
      <details className="opinion-reference-picker__technical">
        <summary>Provenance technique</summary>
        <ol>
          {group.entries.map((entry, index) => (
            <li key={entry.id}>
              {group.entries.length > 1 ? (
                <p>
                  {itemName === "finding" ? "Finding" : "Observation"}{" "}
                  {index + 1}
                  {entry.observedAt === undefined
                    ? ""
                    : " · " + new Date(entry.observedAt).toLocaleString()}
                </p>
              ) : null}
              {entry.values.map((value) => (
                <div
                  className="opinion-reference-picker__technical-value"
                  key={value.label}
                >
                  <span>{value.label}</span>
                  <CopyableTechnicalValue
                    label={value.label}
                    value={value.value}
                  />
                </div>
              ))}
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}

function ReferencePicker({
  entries,
  legend,
  itemName,
  selectedIds,
  onChange,
}: {
  entries: Entry[];
  legend: string;
  itemName: "finding" | "observation";
  selectedIds: string[];
  onChange: (next: string[]) => void;
}) {
  const [search, setSearch] = useState("");
  const filteredEntries = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    if (term === "") return entries;
    return entries.filter((entry) =>
      entry.searchText.toLocaleLowerCase().includes(term),
    );
  }, [entries, search]);
  const groups = useMemo(
    () => groupEntries(filteredEntries),
    [filteredEntries],
  );
  const sections = useMemo(() => {
    const grouped = new Map<string, SectionGroup>();
    for (const group of groups) {
      const section = grouped.get(group.section);
      if (section === undefined) {
        grouped.set(group.section, {
          key: group.section,
          label: group.section,
          groups: [group],
        });
      } else {
        section.groups.push(group);
      }
    }
    return [...grouped.values()];
  }, [groups]);

  return (
    <fieldset className="opinion-reference-picker">
      <legend>{legend}</legend>
      {entries.length === 0 ? (
        <p>
          {itemName === "finding"
            ? "Aucun finding disponible dans cette enquête."
            : "Aucune observation disponible dans cette enquête."}
        </p>
      ) : (
        <>
          <label className="opinion-reference-picker__search">
            <span>
              Rechercher{" "}
              {itemName === "finding" ? "un finding" : "une observation"}
            </span>
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.currentTarget.value)}
            />
          </label>
          {groups.length === 0 ? (
            <p role="status">Aucun résultat pour cette recherche.</p>
          ) : (
            <div className="opinion-reference-picker__groups">
              {sections.map((section) => (
                <section
                  className="opinion-reference-picker__source"
                  key={section.key}
                  aria-label={
                    (itemName === "observation" ? "Source " : "Type ") +
                    section.label
                  }
                >
                  <h4>{section.label}</h4>
                  {section.groups.map((group) => (
                    <GroupRow
                      key={group.key}
                      group={group}
                      selectedIds={selectedIds}
                      onChange={onChange}
                      itemName={itemName}
                    />
                  ))}
                </section>
              ))}
            </div>
          )}
        </>
      )}
    </fieldset>
  );
}

export function FindingReferencePicker({
  findings,
  totalCount = findings.length,
  selectedIds,
  onChange,
}: {
  findings: InvestigationFinding[];
  totalCount?: number;
  selectedIds: string[];
  onChange: (next: string[]) => void;
}) {
  const entries = useMemo(() => findingEntries(findings), [findings]);
  return (
    <ReferencePicker
      entries={entries}
      legend={"Findings disponibles (" + totalCount + ")"}
      itemName="finding"
      selectedIds={selectedIds}
      onChange={onChange}
    />
  );
}

export function ObservationReferencePicker({
  observations,
  totalCount = observations.length,
  selectedIds,
  onChange,
}: {
  observations: InvestigationObservation[];
  totalCount?: number;
  selectedIds: string[];
  onChange: (next: string[]) => void;
}) {
  const entries = useMemo(
    () => observationEntries(observations),
    [observations],
  );
  return (
    <ReferencePicker
      entries={entries}
      legend={"Observations disponibles (" + totalCount + ")"}
      itemName="observation"
      selectedIds={selectedIds}
      onChange={onChange}
    />
  );
}
