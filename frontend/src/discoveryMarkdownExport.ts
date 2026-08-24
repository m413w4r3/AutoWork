import type {
  CandidateTopic,
  IncompleteSourceCandidate,
  ProvisionalDiscoveryIoc,
  SourceCandidate,
} from "./api/discovery";

/**
 * Re-serializes the discovery state (subjects, publications, provisional
 * IOCs) back into the SUBJECT/PUBLICATION Markdown schema that
 * `discovery_report_parser.py` consumes. Round-tripping through this export
 * and the "Coller une réponse ChatGPT" import must reproduce the same
 * candidates, URLs and IOCs — this is the format ChatGPT's own research
 * answers are expected to follow, not a bespoke dump format.
 *
 * Deliberately excludes anything the merge/consolidation step derived
 * (member references, contribution counts, duplicate counts, merge
 * warnings): those aren't part of a single report and would confuse a
 * fresh parse.
 *
 * SUBJECT/PUBLICATION refs are renumbered sequentially (S1, S2… and, within
 * each subject, P1, P2…) rather than reusing `local_ref` as-is. Once
 * candidates from several research batches are consolidated, their original
 * refs were only unique within their own batch — two merged subjects can
 * both be "S1". The backend parser keys candidates by local_ref and keeps
 * only one per ref (`_best_candidate_revision_by_subject`), so re-exporting
 * duplicate refs verbatim silently drops every subject/publication but one
 * per collided ref on re-import.
 */
export function renderDiscoveryMarkdown(candidates: CandidateTopic[]): string {
  const blocks = candidates.map((candidate, index) =>
    renderSubjectBlock(candidate, `S${index + 1}`),
  );
  return ["# SUJETS CANDIDATS", "", ...blocks].join("\n").trimEnd() + "\n";
}

function renderSubjectBlock(
  candidate: CandidateTopic,
  subjectRef: string,
): string {
  const lines: string[] = [`## SUBJECT ${subjectRef}`, ""];
  lines.push(`title: ${oneLine(candidate.title)}`);
  lines.push(`presentation: ${oneLine(candidate.summary)}`);
  lines.push(
    `actor_or_campaign: ${oneLine(candidate.actor_or_campaign || "unknown")}`,
  );
  lines.push(`technical_potential: ${candidate.technical_potential}`);
  lines.push(
    `technical_potential_reason: ${oneLine(candidate.technical_potential_reason)}`,
  );
  lines.push(
    `artifacts: ${candidate.likely_artifacts.length ? candidate.likely_artifacts.join(", ") : "unknown"}`,
  );
  lines.push(
    `uncertainties: ${candidate.uncertainties.length ? candidate.uncertainties.join("; ") : "unknown"}`,
  );
  if (candidate.actors.length)
    lines.push(`actors: ${candidate.actors.join(", ")}`);
  if (candidate.campaigns.length)
    lines.push(`campaigns: ${candidate.campaigns.join(", ")}`);
  if (candidate.malware.length)
    lines.push(`malware: ${candidate.malware.join(", ")}`);
  lines.push("");

  // Provisional IOCs reference publications by SourceCandidate.id
  // (publication_ids), not by the batch-local `local_ref` string ("P1"):
  // once several research batches are consolidated into one subject, two
  // different sources can share the same original local_ref, so matching
  // on the ref alone misattributes IOCs to the wrong publication. The id
  // stays correct through merge (see remap_ioc_publication_ids backend-side).
  const iocsBySourceId = new Map<string, ProvisionalDiscoveryIoc[]>();
  for (const ioc of candidate.provisional_iocs ?? []) {
    for (const id of ioc.publication_ids ?? []) {
      const bucket = iocsBySourceId.get(id);
      if (bucket) bucket.push(ioc);
      else iocsBySourceId.set(id, [ioc]);
    }
  }

  let publicationOrdinal = 1;
  for (const source of candidate.sources) {
    const iocs = iocsBySourceId.get(source.id) ?? [];
    lines.push(
      ...renderPublicationBlock(`P${publicationOrdinal}`, source, iocs),
    );
    publicationOrdinal += 1;
  }
  for (const incomplete of candidate.incomplete_sources) {
    lines.push(
      ...renderIncompletePublicationBlock(`P${publicationOrdinal}`, incomplete),
    );
    publicationOrdinal += 1;
  }

  return lines.join("\n");
}

function renderIncompletePublicationBlock(
  publicationRef: string,
  incomplete: IncompleteSourceCandidate,
): string[] {
  return [
    `### PUBLICATION ${publicationRef}`,
    "",
    `title: ${oneLine(incomplete.title)}`,
    `url: ${incomplete.raw_url ?? ""}`,
    `publisher: ${oneLine(incomplete.publisher || "unknown")}`,
    `published_at: ${incomplete.published_at ?? "unknown"}`,
    `period_relation: ${incomplete.period_relation}`,
    `source_role: ${incomplete.role}`,
    `ioc_presence: ${incomplete.ioc_presence}`,
    `ioc_declared_count: ${incomplete.ioc_declared_count ?? "unknown"}`,
    `ioc_visible_count: ${incomplete.ioc_visible_count ?? "unknown"}`,
    "",
  ];
}

function renderPublicationBlock(
  publicationRef: string,
  source: SourceCandidate,
  iocs: ProvisionalDiscoveryIoc[],
): string[] {
  const visibleTypes = Array.from(
    new Set(iocs.map((ioc) => ioc.proposed_type)),
  );
  return [
    `### PUBLICATION ${publicationRef}`,
    "",
    `title: ${oneLine(source.title)}`,
    `url: ${source.raw_url ?? source.url}`,
    `publisher: ${oneLine(source.publisher || "unknown")}`,
    `published_at: ${source.published_at ?? "unknown"}`,
    `period_relation: ${source.period_relation}`,
    `source_role: ${source.role}`,
    `ioc_presence: ${source.ioc_presence}`,
    `ioc_declared_count: ${source.ioc_declared_count ?? "unknown"}`,
    `ioc_visible_count: ${source.ioc_visible_count ?? "unknown"}`,
    `visible_ioc_types: ${visibleTypes.length ? visibleTypes.join(", ") : "none"}`,
    `visible_iocs: ${iocs.length ? iocs.map((ioc) => ioc.raw_value).join("; ") : "none"}`,
    "",
  ];
}

// Field values are parsed line-by-line; collapse embedded newlines so a
// multi-paragraph summary doesn't get mistaken for the start of a new field.
function oneLine(value: string | null | undefined): string {
  return (value ?? "").replace(/\s*\n\s*/g, " ").trim();
}
