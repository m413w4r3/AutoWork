import { useId, useMemo, useState } from "react";

import type {
  InvestigationFinding,
  InvestigationObservation,
} from "../../../api/investigation";
import { CopyableTechnicalValue } from "./CopyableTechnicalValue";
import { FINDING_KIND_LABELS, truncate } from "./labels";
import {
  observationReferenceEntries,
  type ReferencePickerEntry,
  type ReferenceTechnicalValue,
} from "./references";

interface Group {
  key: string;
  section: string;
  label: string;
  entries: ReferencePickerEntry[];
}

interface SectionGroup {
  key: string;
  label: string;
  groups: Group[];
}

function findingEntries(
  findings: InvestigationFinding[],
): ReferencePickerEntry[] {
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

function groupEntries(entries: ReferencePickerEntry[]): Group[] {
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

export type ReferenceSelection =
  | {
      mode: "multiple";
      selectedIds: string[];
      onChange: (next: string[]) => void;
    }
  | {
      mode: "single";
      selectedId: string;
      onSelect: (id: string) => void;
    };

type RowReferenceSelection =
  | Extract<ReferenceSelection, { mode: "multiple" }>
  | (Extract<ReferenceSelection, { mode: "single" }> & {
      radioName: string;
    });

function TechnicalProvenance({
  values,
}: {
  values: ReferenceTechnicalValue[];
}) {
  return (
    <details className="opinion-reference-picker__technical">
      <summary>Provenance technique</summary>
      <ol>
        {values.map((value) => (
          <li
            className="opinion-reference-picker__technical-value"
            key={value.label}
          >
            <span>{value.label}</span>
            <CopyableTechnicalValue label={value.label} value={value.value} />
          </li>
        ))}
      </ol>
    </details>
  );
}

function GroupRow({
  group,
  selection,
  itemName,
}: {
  group: Group;
  selection: RowReferenceSelection;
  itemName: "finding" | "observation" | "element" | "figure";
}) {
  const selectedIds =
    selection.mode === "multiple" ? selection.selectedIds : [];

  if (selection.mode === "single") {
    return (
      <div className="opinion-reference-picker__group">
        {group.entries.length > 1 ? (
          <p className="opinion-reference-picker__count">
            {group.entries.length} références regroupées
          </p>
        ) : null}
        {group.entries.map((entry, index) => (
          <div
            className="opinion-reference-picker__single-entry"
            key={entry.id}
          >
            <label className="opinion-reference-picker__option">
              <input
                type="radio"
                name={selection.radioName}
                value={entry.id}
                checked={selection.selectedId === entry.id}
                disabled={entry.disabledReason !== undefined}
                onChange={() => selection.onSelect(entry.id)}
              />
              <span className="opinion-reference-picker__option-text">
                <span className="opinion-reference-picker__label">
                  {entry.label}
                </span>
                {group.entries.length > 1 ? (
                  <span className="opinion-reference-picker__count">
                    {itemName === "observation"
                      ? `Observation ${index + 1}`
                      : `Référence ${index + 1}`}
                    {entry.observedAt === undefined
                      ? ""
                      : " · " + new Date(entry.observedAt).toLocaleString("fr")}
                  </span>
                ) : null}
                {entry.disabledReason === undefined ? null : (
                  <span className="opinion-reference-picker__disabled-reason">
                    Non proposable : {entry.disabledReason}
                  </span>
                )}
              </span>
            </label>
            <TechnicalProvenance values={entry.values} />
          </div>
        ))}
      </div>
    );
  }

  const { onChange } = selection;

  return (
    <div className="opinion-reference-picker__group">
      {group.entries.length > 1 ? (
        <p className="opinion-reference-picker__count">
          {group.entries.length} références regroupées
        </p>
      ) : null}
      {group.entries.map((entry, index) => (
        <div className="opinion-reference-picker__single-entry" key={entry.id}>
          <label className="opinion-reference-picker__option">
            <input
              type="checkbox"
              checked={selectedIds.includes(entry.id)}
              disabled={entry.disabledReason !== undefined}
              onChange={() => onChange(toggleGroup(selectedIds, [entry.id]))}
            />
            <span className="opinion-reference-picker__option-text">
              <span className="opinion-reference-picker__label">
                {entry.label}
              </span>
              {group.entries.length > 1 ? (
                <span className="opinion-reference-picker__count">
                  {itemName === "finding" ? "Finding" : "Observation"}{" "}
                  {index + 1}
                  {entry.observedAt === undefined
                    ? ""
                    : " · " + new Date(entry.observedAt).toLocaleString("fr")}
                </span>
              ) : null}
              {entry.disabledReason === undefined ? null : (
                <span className="opinion-reference-picker__disabled-reason">
                  Non proposable : {entry.disabledReason}
                </span>
              )}
            </span>
          </label>
          <TechnicalProvenance values={entry.values} />
        </div>
      ))}
    </div>
  );
}

export function GroupedReferencePicker({
  entries,
  legend,
  itemName,
  selection,
}: {
  entries: ReferencePickerEntry[];
  legend: string;
  itemName: "finding" | "observation" | "element" | "figure";
  selection: ReferenceSelection;
}) {
  const selectionId = useId();
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
            : itemName === "observation"
              ? "Aucune observation disponible dans cette enquête."
              : itemName === "figure"
                ? "Aucune figure disponible dans cet avis."
                : "Aucun élément disponible pour cet emplacement."}
        </p>
      ) : (
        <>
          <label className="opinion-reference-picker__search">
            <span>
              Rechercher{" "}
              {itemName === "finding"
                ? "un finding"
                : itemName === "observation"
                  ? "une observation"
                  : itemName === "figure"
                    ? "une figure"
                    : "un élément"}
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
                    itemName === "observation"
                      ? "Source " + section.label
                      : itemName === "finding"
                        ? "Type " + section.label
                        : section.label
                  }
                >
                  <h4>{section.label}</h4>
                  {section.groups.map((group) => (
                    <GroupRow
                      key={group.key}
                      group={group}
                      selection={
                        selection.mode === "single"
                          ? {
                              ...selection,
                              radioName: `${selectionId}-selection`,
                            }
                          : selection
                      }
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
    <GroupedReferencePicker
      entries={entries}
      legend={"Findings disponibles (" + totalCount + ")"}
      itemName="finding"
      selection={{ mode: "multiple", selectedIds, onChange }}
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
    () => observationReferenceEntries(observations),
    [observations],
  );
  return (
    <GroupedReferencePicker
      entries={entries}
      legend={"Observations disponibles (" + totalCount + ")"}
      itemName="observation"
      selection={{ mode: "multiple", selectedIds, onChange }}
    />
  );
}
