import type {
  ExtractionProgress,
  ExtractionProgressSource,
} from "../api/production";

export interface PresentedProductionWarning {
  code: string;
  title: string;
  source: string | null;
  url: string | null;
  message: string;
  raw: string;
}

export interface BlockingSource {
  sourceId: string;
  title: string | null;
  url: string | null;
  errorCode: string | null;
}

export interface SkippedSource {
  sourceId: string;
  title: string | null;
  url: string | null;
}

type StringRecord = Record<string, unknown>;

function asRecord(value: unknown): StringRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as StringRecord)
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function nestedDetails(details: StringRecord | null): StringRecord | null {
  return asRecord(details?.details) ?? details;
}

function recordField(
  details: StringRecord | null,
  key: string,
): StringRecord | null {
  const direct = asRecord(details?.[key]);
  if (direct) return direct;
  return asRecord(nestedDetails(details)?.[key]);
}

function stringField(
  details: StringRecord | null,
  ...keys: string[]
): string | null {
  for (const key of keys) {
    const value = stringValue(details?.[key]);
    if (value) return value;
  }
  return null;
}

function sourceFromProgress(
  progress: ExtractionProgress | null | undefined,
  sourceId: string,
): ExtractionProgressSource | undefined {
  return progress?.sources.find((source) => source.source_id === sourceId);
}

function sourceTitle(
  details: StringRecord | null,
  progressSource: ExtractionProgressSource | undefined,
): string | null {
  return (
    stringField(details, "title", "source_title", "source_name") ??
    stringValue(progressSource?.title)
  );
}

function sourceUrl(
  details: StringRecord | null,
  progressSource: ExtractionProgressSource | undefined,
): string | null {
  return (
    stringField(details, "source_url", "url", "canonical_url") ??
    stringValue(progressSource?.source_url) ??
    stringValue(progressSource?.url)
  );
}

export function getBlockingSources(
  errorDetails: Record<string, unknown> | null | undefined,
  progress: ExtractionProgress | null | undefined,
): BlockingSource[] {
  const details = asRecord(errorDetails);
  const failures = recordField(details, "source_failures");
  const ids = [
    ...Object.keys(failures ?? {}),
    ...stringArray(details?.failed_source_ids),
    ...stringArray(nestedDetails(details)?.failed_source_ids),
  ];
  const uniqueIds = [...new Set(ids)];

  return uniqueIds.map((sourceId) => {
    const failure = asRecord(failures?.[sourceId]);
    const progressSource = sourceFromProgress(progress, sourceId);
    return {
      sourceId,
      title: sourceTitle(failure, progressSource),
      url: sourceUrl(failure, progressSource),
      errorCode: stringField(failure, "error_code", "code"),
    };
  });
}

export function getSkippedSources(
  errorDetails: Record<string, unknown> | null | undefined,
  progress: ExtractionProgress | null | undefined,
): SkippedSource[] {
  const details = asRecord(errorDetails);
  const skips = new Map<string, StringRecord>();
  const progressSkips = asRecord(progress?.source_skips);
  const detailSkips = recordField(details, "source_skips");

  for (const [sourceId, skip] of Object.entries(progressSkips ?? {})) {
    const record = asRecord(skip);
    if (record && booleanValue(record.blocking) !== true)
      skips.set(sourceId, record);
  }
  for (const [sourceId, skip] of Object.entries(detailSkips ?? {})) {
    const record = asRecord(skip);
    if (record && booleanValue(record.blocking) !== true)
      skips.set(sourceId, record);
  }
  for (const source of progress?.sources ?? []) {
    if (source.status !== "skipped" || !source.skip) continue;
    if (booleanValue(source.skip.blocking) !== true) {
      skips.set(source.source_id, source.skip);
    }
  }

  const skippedIds = [
    ...stringArray(progress?.skipped_source_ids),
    ...stringArray(details?.skipped_source_ids),
    ...stringArray(nestedDetails(details)?.skipped_source_ids),
  ];
  for (const sourceId of skippedIds) {
    if (!skips.has(sourceId)) skips.set(sourceId, {});
  }

  return [...skips.entries()].map(([sourceId, skip]) => {
    const progressSource = sourceFromProgress(progress, sourceId);
    return {
      sourceId,
      title: sourceTitle(skip, progressSource),
      url: sourceUrl(skip, progressSource),
    };
  });
}

function parseWarning(raw: string): {
  code: string;
  fields: StringRecord;
} {
  const code = raw.match(/^([^:]+)(?::|$)/)?.[1] ?? raw;
  const fields: StringRecord = {};
  const fieldPattern =
    /(?:^|:)([a-z][a-z0-9_]*)=(.*?)(?=:[a-z][a-z0-9_]*=|$)/gi;
  for (const match of raw.matchAll(fieldPattern)) {
    const key = match[1];
    const value = match[2];
    if (key !== undefined && value !== undefined) {
      fields[key.toLowerCase()] = value;
    }
  }
  return { code, fields };
}

function sourceNameFromUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const lastPathPart = parsed.pathname.split("/").filter(Boolean).pop();
    if (lastPathPart) return decodeURIComponent(lastPathPart);
    return parsed.hostname;
  } catch {
    return url;
  }
}

function humanizeCode(code: string): string {
  return code
    .replace(/[_-]+/g, " ")
    .replace(/^\w/, (letter) => letter.toUpperCase());
}

export function formatProductionWarning(
  raw: string,
): PresentedProductionWarning {
  const parsed = parseWarning(raw);
  const url = stringValue(parsed.fields.url ?? parsed.fields.source_url);
  const source =
    stringValue(parsed.fields.title ?? parsed.fields.source_name) ??
    sourceNameFromUrl(url) ??
    stringValue(parsed.fields.source_id);

  if (parsed.code === "supplemental_collection_failed") {
    return {
      code: parsed.code,
      title: "Source supplémentaire non archivée",
      source,
      url,
      message:
        "Cette source n’a pas pu être collectée. La production peut continuer.",
      raw,
    };
  }

  if (parsed.code === "q2_ioc_rules_fact_dropped") {
    return {
      code: parsed.code,
      title: "Éléments factuels écartés",
      source,
      url,
      message:
        "Les règles IOC ont été conservées; la production peut continuer.",
      raw,
    };
  }

  return {
    code: parsed.code,
    title: "Avertissement non bloquant",
    source,
    url,
    message: humanizeCode(parsed.code),
    raw,
  };
}
