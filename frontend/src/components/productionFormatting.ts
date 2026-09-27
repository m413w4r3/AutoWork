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

/** The CORE source that stopped the canonical EXTRACTION stage, if any. */
export function getBlockingSources(
  errorDetails: Record<string, unknown> | null | undefined,
  progress: ExtractionProgress | null | undefined,
): BlockingSource[] {
  const details = asRecord(errorDetails);
  const sourceId = stringField(details, "source_document_id");
  if (!sourceId) return [];
  const progressSource = sourceFromProgress(progress, sourceId);
  return [
    {
      sourceId,
      title: stringValue(progressSource?.title),
      url:
        stringField(details, "canonical_url") ??
        progressSource?.canonical_url ??
        null,
      errorCode: stringField(details, "source_failure_code"),
    },
  ];
}

/** Complementary sources whose extraction failed without blocking the run. */
export function getSkippedSources(
  progress: ExtractionProgress | null | undefined,
): SkippedSource[] {
  return (progress?.sources ?? [])
    .filter((source) => source.status === "failed" && source.tier !== "core")
    .map((source) => ({
      sourceId: source.source_id,
      title: stringValue(source.title),
      url: source.canonical_url,
    }));
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

  if (parsed.code === "q2_detection_rules_lost") {
    const count = stringValue(parsed.fields.count) ?? "?";
    return {
      code: parsed.code,
      title: "Règles de détection perdues",
      source,
      url,
      message:
        `${count} règle(s) YARA ou Sigma n’ont pas pu être prouvées dans leur ` +
        `source et n’apparaîtront pas dans la publication. Consultez ` +
        `l’extraction avant de publier.`,
      raw,
    };
  }

  if (parsed.code === "q2_source_evidence_rejected") {
    const count = stringValue(parsed.fields.count) ?? "?";
    const reason = stringValue(parsed.fields.reason);
    const segments = raw.split(":");
    const sourceId = segments.length >= 2 ? (segments[1] ?? null) : null;
    const artifactType = segments.length >= 3 ? (segments[2] ?? null) : null;
    return {
      code: parsed.code,
      title: "Indicateurs écartés",
      source: source ?? sourceId,
      url,
      message:
        `${count} valeur(s) de type ${artifactType ?? "inconnu"} proposées ` +
        `pour ${sourceId ?? "cette source"} n’ont pas pu être retrouvées dans ` +
        `le texte archivé` +
        (reason ? ` (${reason})` : "") +
        `. Elles ne seront pas publiées.`,
      raw,
    };
  }

  if (parsed.code === "q2_batch_source_evidence_rejected") {
    const count = stringValue(parsed.fields.count) ?? "?";
    const reason = stringValue(parsed.fields.reason);
    const segments = raw.split(":");
    const sourceId = segments.length >= 3 ? (segments[2] ?? null) : null;
    const artifactType = segments.length >= 4 ? (segments[3] ?? null) : null;
    return {
      code: parsed.code,
      title: "Indicateurs écartés",
      source: source ?? sourceId,
      url,
      message:
        `${count} valeur(s) de type ${artifactType ?? "inconnu"} proposées ` +
        `pour ${sourceId ?? "cette source"} n’ont pas pu être retrouvées dans ` +
        `le texte archivé` +
        (reason ? ` (${reason})` : "") +
        `. Elles ne seront pas publiées.`,
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
